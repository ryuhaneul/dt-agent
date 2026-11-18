"""Claude slot rotation shared between streaming and one-shot paths.

Extracted from :mod:`ductor_bot.cli.claude_provider` so the cron/one-shot
executor can reuse the exact same cooldown bookkeeping and fallback rules
as the interactive chat path. Preserves the three behaviours the provider
relied on:

* All configured slots cooling down → attempt the top-priority slot once
  and surface the raw 429 (no misleading fallback prefix).
* Every hot slot rate-limited → record cooldowns for
  each and surface the final 429 unchanged.
* First slot rate-limited, a later slot succeeded → record cooldown for
  the first and let the caller prepend a ``[Claude: A → B ...]`` prefix
  using :data:`first_rl_slot` / :data:`first_rl_reset`.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Generic, TypeVar
from zoneinfo import ZoneInfo

from ductor_bot.cli.claude_accounts import (
    CONTAINER_DUCTOR_MOUNT,
    SUBSCRIPTION_RETRY_INITIAL_SECONDS,
    SUBSCRIPTION_RETRY_MAX_SECONDS,
    AuthFailureDetails,
    AuthFailureKind,
    SlotRegistry,
    SlotSnapshot,
    container_slot_config_dir,
    detect_auth_failure_kind,
    detect_rate_limit,
    host_slot_config_dir,
    parse_reset_time,
    resolve_ductor_home,
    slot_is_api_key,
    state_file_path,
)
from ductor_bot.i18n import t

logger = logging.getLogger(__name__)

T = TypeVar("T")

UNPARSABLE_RESET_COOLDOWN_SECONDS = 3600


# Process-wide lock for cooldown state mutations. Shared between the chat
# provider and the cron one-shot runner so concurrent 429s can't clobber
# each other's snapshots.
_SLOT_LOCK: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    """Return the module-level asyncio.Lock, lazily constructing it.

    asyncio.Lock() before a running loop exists triggers a DeprecationWarning
    on 3.10+, so we defer creation to first use.
    """
    global _SLOT_LOCK  # noqa: PLW0603
    if _SLOT_LOCK is None:
        _SLOT_LOCK = asyncio.Lock()
    return _SLOT_LOCK


@dataclass(slots=True)
class AttemptOutcome:
    """Provider-agnostic summary of one subprocess attempt.

    The rotation driver only needs returncode, error flag, stderr, and the
    final result text to decide whether a rate-limit occurred and to parse
    the reset timestamp — all other payload fields stay on ``T``.
    """

    returncode: int | None
    is_error: bool
    stderr: str
    result_text: str


@dataclass(slots=True)
class RotationResult(Generic[T]):
    """Outcome of :func:`run_with_rotation`.

    Attributes:
    ----------
    payload:
        The provider-specific payload from the winning (or final) attempt.
        ``None`` only when no attempt was made (e.g. accounts empty).
    slot:
        The slot that produced ``payload`` (``None`` when rotation was
        disabled).
    first_rl_slot / first_rl_reset:
        Populated when any earlier slot was rate-limited, using its parsed
        reset timestamp or a one-hour fallback; callers use these to build
        the fallback prefix. ``first_rl_reset_estimated`` marks that fallback.
    unparsable_stderr:
        Legacy raw-stderr override. The rotation driver leaves this unset:
        unparseable reset times use a one-hour cooldown instead.
    all_cooling:
        True when every configured slot was already cooling down and the
        top-priority slot was tried once. The caller must NOT add a
        fallback prefix in this case.
    all_rate_limited:
        True when every hot slot was rate-limited;
        the caller surfaces the final 429 payload unchanged (no prefix).
    first_auth_slot:
        First login failure before a successful switch; used for its notice.
    reauth_required:
        All available attempts failed and at least one login expired, or
        the all-expired probe reports login expiry. Append re-login help to the real error.
    """

    payload: T | None
    slot: str | None = None
    first_rl_slot: str | None = None
    first_rl_reset: datetime | None = None
    first_rl_reset_estimated: bool = False
    unparsable_stderr: str | None = None
    all_cooling: bool = False
    all_rate_limited: bool = False
    first_auth_slot: str | None = None
    reauth_required: bool = False


# ---------------------------------------------------------------------------
# Helpers exposed to consumers (provider + task_runner)
# ---------------------------------------------------------------------------


def slot_extra_env(
    slot_name: str,
    *,
    working_dir: str | Path | None,
) -> dict[str, str]:
    """Return the env overrides that pin the CLI to *slot_name*.

    Every slot, including the default ``claude`` slot, gets a per-subprocess
    ``CLAUDE_CONFIG_DIR`` pointing at its directory.

    This project is container-only: cron/webhook one-shots that lack
    ``working_dir`` (because ``config.ductor_home`` is unset) still need a
    valid slot dir, so missing ``working_dir`` falls back to the container
    mount.
    """
    if not working_dir:
        return {"CLAUDE_CONFIG_DIR": container_slot_config_dir(slot_name)}
    ductor_home = resolve_ductor_home(working_dir)
    return {"CLAUDE_CONFIG_DIR": str(host_slot_config_dir(ductor_home, slot_name))}


def make_registry(
    *,
    working_dir: str | Path | None,
) -> SlotRegistry:
    """Build a SlotRegistry pointing at the shared cooldown state file.

    This project is container-only; cooldown state always lives under the
    ``/ductor`` mount so every agent (chat + cron) sees the same file.
    Host-mode callers derive ductor home from working_dir; cron/webhook
    one-shots that lack working_dir fall back to the container mount.
    """
    if not working_dir:
        ductor_home = Path(CONTAINER_DUCTOR_MOUNT)
    else:
        ductor_home = resolve_ductor_home(working_dir)
    return SlotRegistry(state_file_path(ductor_home))


async def hot_slots(
    registry: SlotRegistry | None,
    accounts: Sequence[str],
) -> list[str]:
    """Return ready slots, including subscription retries whose backoff has elapsed."""
    async with _get_lock():
        snapshot = await asyncio.to_thread(registry.load) if registry else SlotSnapshot()
    now = time.time()
    return [
        s
        for s in accounts
        if snapshot.cooldowns.get(s, 0.0) <= now
        and (
            s not in snapshot.auth_failures
            or (
                (detail := snapshot.auth_failure_details.get(s)) is not None
                and detail.kind == "subscription_unavailable"
                and detail.failed_at + detail.retry_delay <= now
            )
        )
    ]


async def fallback_slot(registry: SlotRegistry | None, accounts: Sequence[str]) -> str:
    """Choose the first non-expired slot, or probe top once if all expired."""
    snapshot = await asyncio.to_thread(registry.load) if registry else SlotSnapshot()
    return next((slot for slot in accounts if slot not in snapshot.auth_failures), accounts[0])


async def record_auth_failure(
    registry: SlotRegistry | None,
    slot: str,
    credential_mtimes: dict[str, int | None],
    *,
    kind: AuthFailureKind = "login_expired",
) -> None:
    """Quarantine only the credentials fingerprinted before the failed attempt."""
    if registry is None or slot_is_api_key(registry.state_path.parent / slot):
        return

    def record(snapshot: SlotSnapshot) -> None:
        if (
            not slot_is_api_key(registry.state_path.parent / slot)
            and registry.credential_mtimes(slot) == credential_mtimes
            and snapshot.auth_failures.get(slot, credential_mtimes) == credential_mtimes
        ):
            previous = snapshot.auth_failure_details.get(slot)
            delay = float(SUBSCRIPTION_RETRY_INITIAL_SECONDS)
            if previous is not None and previous.kind == "subscription_unavailable":
                delay = min(previous.retry_delay * 2, SUBSCRIPTION_RETRY_MAX_SECONDS)
            snapshot.auth_failures[slot] = credential_mtimes
            snapshot.auth_failure_details[slot] = (
                AuthFailureDetails(kind, time.time(), delay)
                if kind == "subscription_unavailable"
                else AuthFailureDetails()
            )

    async with _get_lock():
        await asyncio.to_thread(registry.update, record)


async def record_subscription_retry_failure(
    registry: SlotRegistry | None, slot: str, credential_mtimes: dict[str, int | None]
) -> bool:
    """Defer a subscription retry after an unrelated error without increasing backoff."""
    if registry is None:
        return False

    def record(snapshot: SlotSnapshot) -> None:
        detail = snapshot.auth_failure_details.get(slot)
        if (
            detail is not None
            and detail.kind == "subscription_unavailable"
            and snapshot.auth_failures.get(slot) == credential_mtimes
        ):
            snapshot.auth_failure_details[slot] = AuthFailureDetails(
                detail.kind, time.time(), detail.retry_delay
            )

    async with _get_lock():
        snapshot = await asyncio.to_thread(registry.load)
        detail = snapshot.auth_failure_details.get(slot)
        if (
            detail is None
            or detail.kind != "subscription_unavailable"
            or snapshot.auth_failures.get(slot) != credential_mtimes
        ):
            return False
        await asyncio.to_thread(registry.update, record)
        return True


async def clear_auth_failure(
    registry: SlotRegistry | None, slot: str, credential_mtimes: dict[str, int | None]
) -> None:
    """Allow a successful all-expired probe to recover without an mtime change."""
    if registry is None:
        return

    def clear(snapshot: SlotSnapshot) -> None:
        if snapshot.auth_failures.get(slot) == credential_mtimes:
            snapshot.auth_failures.pop(slot, None)
            snapshot.auth_failure_details.pop(slot, None)

    async with _get_lock():
        snapshot = await asyncio.to_thread(registry.load)
        if snapshot.auth_failures.get(slot) == credential_mtimes:
            await asyncio.to_thread(registry.update, clear)


async def record_cooldown(
    registry: SlotRegistry | None,
    slot_name: str,
    reset_epoch: float,
) -> None:
    """Persist cooldown for *slot_name* under the shared lock."""

    def record(snapshot: SlotSnapshot) -> None:
        snapshot.cooldowns[slot_name] = reset_epoch
        snapshot.last_used = slot_name

    async with _get_lock():
        if registry:
            await asyncio.to_thread(registry.update, record)


def parse_reset_from_text(text: str, default_tz: ZoneInfo) -> datetime | None:
    """Parse a ``"resets ..."`` timestamp relative to *default_tz*."""
    return parse_reset_time(
        text,
        now=datetime.now(tz=default_tz),
        default_tz=default_tz,
    )


_FALLBACK_NOTICE = "[Claude: {from_slot} → {to_slot} {message}]\n\n"
_FALLBACK_NOTICE_PATTERN = re.compile(
    "^"
    + re.escape(_FALLBACK_NOTICE)
    .replace(re.escape("{from_slot}"), r"[^\n]+?")
    .replace(re.escape("{to_slot}"), r"[^\n]+?")
    .replace(re.escape("{message}"), r"[^\n]+?")
)


def build_fallback_prefix(
    from_slot: str,
    to_slot: str,
    reset_dt: datetime,
    user_tz: ZoneInfo,
    *,
    reset_estimated: bool = False,
) -> str:
    """Render the user-facing ``[Claude: A → B ...]`` prefix.

    Converts ``reset_dt`` to the user's timezone and includes the TZ label
    so the reader can't mistake a UTC wall-clock for local time. The leading
    ``[Claude: A → B `` shape is fixed (consumers may match it); the rest of the
    text follows the configured language.
    """
    if reset_estimated:
        return _FALLBACK_NOTICE.format(
            from_slot=from_slot, to_slot=to_slot, message=t("slot.switched_estimated")
        )
    reset_local = reset_dt.astimezone(user_tz)
    reset_label = reset_local.strftime("%Y-%m-%d %H:%M %Z")
    return _FALLBACK_NOTICE.format(
        from_slot=from_slot, to_slot=to_slot, message=t("slot.switched", reset=reset_label)
    )


def build_auth_fallback_prefix(from_slot: str, to_slot: str) -> str:
    """Use the same prefix shape as quota switches for structured consumers."""
    return _FALLBACK_NOTICE.format(
        from_slot=from_slot, to_slot=to_slot, message=t("slot.switched_auth")
    )


def build_reauth_notice() -> str:
    """Tell the user how to re-login to the expired slots."""
    return "\n\n" + t("slot.reauth_required")


def strip_fallback_prefix(result: str) -> str:
    """Remove a leading account switch notice before consuming structured output."""
    return _FALLBACK_NOTICE_PATTERN.sub("", result, count=1)


def build_unparsable_prefix(raw_stderr: str) -> str:
    """Render the prefix shown when a cooldown time could not be parsed (raw stderr follows)."""
    return f"[Claude: {t('slot.cooldown_unparsable')}]\n{raw_stderr}\n\n"


# ---------------------------------------------------------------------------
# Core rotation driver
# ---------------------------------------------------------------------------


async def run_with_rotation(  # noqa: C901, PLR0912, PLR0915 -- auth/quota rotation shares one state machine
    *,
    accounts: Sequence[str],
    registry: SlotRegistry | None,
    default_tz: ZoneInfo,
    attempt: Callable[[str | None], Awaitable[tuple[AttemptOutcome, T]]],
) -> RotationResult[T]:
    """Drive the slot-rotation state machine around *attempt*.

    *attempt* is called at most ``len(accounts)`` times with a slot name
    (or ``None`` when rotation is disabled), and must return the provider
    payload plus a summary the driver inspects for rate-limit detection.

    The returned :class:`RotationResult` tells the caller how to shape
    the final user-visible response. The driver mirrors the policy the
    chat provider used before extraction:

    * Empty accounts → single ``attempt(None)`` call.
    * No hot slots → probe the first non-expired slot, or top if all expired; ``all_cooling``
      is set so the caller surfaces the raw response.
    * Otherwise, iterate hot slots, skipping login failures and rate limits.
      Missing reset times use a one-hour cooldown.
    """
    account_list = list(accounts)

    if not account_list:
        outcome, payload = await attempt(None)
        return RotationResult(payload=payload, slot=None)

    hot = await hot_slots(registry, account_list)

    if not hot:
        # Every configured slot is cooling down — try the top priority
        # once so the user still sees a response, but don't add a prefix
        # (it would misleadingly imply a successful switch).
        logger.info("Claude: all slots cooling down; attempting top slot once")
        top = await fallback_slot(registry, account_list)
        mtimes = await asyncio.to_thread(registry.credential_mtimes, top) if registry else {}
        outcome, payload = await attempt(top)
        api_key_slot = bool(registry and slot_is_api_key(registry.state_path.parent / top))
        failure_kind = (
            None
            if api_key_slot
            else detect_auth_failure_kind(
                returncode=outcome.returncode,
                is_error=outcome.is_error,
                stderr=outcome.stderr,
                result=outcome.result_text,
            )
        )
        if failure_kind is not None:
            await record_auth_failure(registry, top, mtimes, kind=failure_kind)
            return RotationResult(payload=payload, slot=top, all_cooling=True, reauth_required=True)
        if outcome.returncode == 0 and not outcome.is_error:
            await clear_auth_failure(registry, top, mtimes)
        elif outcome.returncode not in (0, None) or outcome.is_error:
            await record_subscription_retry_failure(registry, top, mtimes)
        if detect_rate_limit(
            returncode=outcome.returncode,
            is_error=outcome.is_error,
            stderr=outcome.stderr,
            result=outcome.result_text,
        ):
            reset_dt = parse_reset_from_text(
                outcome.stderr + "\n" + outcome.result_text,
                default_tz,
            )
            if reset_dt is None:
                logger.warning(
                    "Claude slot=%s reset time unparsable — cooling down for 1 hour", top
                )
                reset_dt = datetime.fromtimestamp(
                    time.time() + UNPARSABLE_RESET_COOLDOWN_SECONDS, tz=default_tz
                )
            await record_cooldown(registry, top, reset_dt.timestamp())
        return RotationResult(
            payload=payload,
            slot=top,
            all_cooling=True,
        )

    first_rl_slot: str | None = None
    first_auth_slot: str | None = None
    first_rl_reset: datetime | None = None
    first_rl_reset_estimated = False
    last_payload: T | None = None
    last_slot: str | None = None

    for slot in hot:
        mtimes = await asyncio.to_thread(registry.credential_mtimes, slot) if registry else {}
        outcome, payload = await attempt(slot)
        last_payload = payload
        last_slot = slot

        api_key_slot = bool(registry and slot_is_api_key(registry.state_path.parent / slot))
        failure_kind = (
            None
            if api_key_slot
            else detect_auth_failure_kind(
                returncode=outcome.returncode,
                is_error=outcome.is_error,
                stderr=outcome.stderr,
                result=outcome.result_text,
            )
        )
        if failure_kind is not None:
            await record_auth_failure(registry, slot, mtimes, kind=failure_kind)
            if first_auth_slot is None:
                first_auth_slot = slot
            continue

        retry_failed = False
        if outcome.returncode == 0 and not outcome.is_error:
            await clear_auth_failure(registry, slot, mtimes)
        elif outcome.returncode not in (0, None) or outcome.is_error:
            retry_failed = await record_subscription_retry_failure(registry, slot, mtimes)

        is_rate_limited = detect_rate_limit(
            returncode=outcome.returncode,
            is_error=outcome.is_error,
            stderr=outcome.stderr,
            result=outcome.result_text,
        )

        if not is_rate_limited:
            if retry_failed:
                continue
            return RotationResult(
                payload=payload,
                slot=slot,
                first_auth_slot=first_auth_slot
                if outcome.returncode == 0 and not outcome.is_error
                else None,
                first_rl_slot=first_rl_slot,
                first_rl_reset=first_rl_reset,
                first_rl_reset_estimated=first_rl_reset_estimated,
            )

        reset_dt = parse_reset_from_text(
            outcome.stderr + "\n" + outcome.result_text,
            default_tz,
        )
        reset_estimated = reset_dt is None
        if reset_dt is None:
            logger.warning(
                "Claude slot=%s reset time unparsable — cooling down for 1 hour",
                slot,
            )
            reset_dt = datetime.fromtimestamp(
                time.time() + UNPARSABLE_RESET_COOLDOWN_SECONDS, tz=default_tz
            )

        await record_cooldown(registry, slot, reset_dt.timestamp())
        if first_rl_slot is None:
            first_rl_slot = slot
            first_rl_reset = reset_dt
            first_rl_reset_estimated = reset_estimated
        logger.info(
            "Claude slot=%s rate-limited (reset %s)",
            slot,
            reset_dt.isoformat(timespec="minutes"),
        )

    # Every hot slot failed due to quota or login expiry; never imply success.
    logger.info("Claude: all %d hot slots unavailable; surfacing last error", len(hot))
    return RotationResult(
        payload=last_payload,
        slot=last_slot,
        first_rl_slot=first_rl_slot,
        first_rl_reset=first_rl_reset,
        first_rl_reset_estimated=first_rl_reset_estimated,
        all_rate_limited=first_auth_slot is None,
        reauth_required=first_auth_slot is not None,
    )
