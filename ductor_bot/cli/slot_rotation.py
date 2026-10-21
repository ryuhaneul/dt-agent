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
    SlotRegistry,
    SlotSnapshot,
    container_slot_config_dir,
    detect_rate_limit,
    host_slot_config_dir,
    parse_reset_time,
    resolve_ductor_home,
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
    """

    payload: T | None
    slot: str | None = None
    first_rl_slot: str | None = None
    first_rl_reset: datetime | None = None
    first_rl_reset_estimated: bool = False
    unparsable_stderr: str | None = None
    all_cooling: bool = False
    all_rate_limited: bool = False


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
    """Return the priority-ordered list of slots whose cooldown expired."""
    async with _get_lock():
        snapshot = await asyncio.to_thread(registry.load) if registry else SlotSnapshot()
    now = time.time()
    return [s for s in accounts if snapshot.cooldowns.get(s, 0.0) <= now]


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


def strip_fallback_prefix(result: str) -> str:
    """Remove a leading account switch notice before consuming structured output."""
    return _FALLBACK_NOTICE_PATTERN.sub("", result, count=1)


def build_unparsable_prefix(raw_stderr: str) -> str:
    """Render the prefix shown when a cooldown time could not be parsed (raw stderr follows)."""
    return f"[Claude: {t('slot.cooldown_unparsable')}]\n{raw_stderr}\n\n"


# ---------------------------------------------------------------------------
# Core rotation driver
# ---------------------------------------------------------------------------


async def run_with_rotation(
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
    * No hot slots → single attempt on the top priority; ``all_cooling``
      is set so the caller surfaces the raw response.
    * Otherwise, iterate hot slots; stop on the first non-rate-limited
      response. Missing reset times use a one-hour cooldown.
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
        top = account_list[0]
        outcome, payload = await attempt(top)
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
        return RotationResult(payload=payload, slot=top, all_cooling=True)

    first_rl_slot: str | None = None
    first_rl_reset: datetime | None = None
    first_rl_reset_estimated = False
    last_payload: T | None = None
    last_slot: str | None = None

    for slot in hot:
        outcome, payload = await attempt(slot)
        last_payload = payload
        last_slot = slot

        is_rate_limited = detect_rate_limit(
            returncode=outcome.returncode,
            is_error=outcome.is_error,
            stderr=outcome.stderr,
            result=outcome.result_text,
        )

        if not is_rate_limited:
            return RotationResult(
                payload=payload,
                slot=slot,
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

    # Every hot slot was rate-limited — surface the
    # final 429 response unchanged; a "switched" prefix would be misleading.
    logger.info("Claude: all %d hot slots rate-limited; surfacing last 429", len(hot))
    return RotationResult(
        payload=last_payload,
        slot=last_slot,
        first_rl_slot=first_rl_slot,
        first_rl_reset=first_rl_reset,
        first_rl_reset_estimated=first_rl_reset_estimated,
        all_rate_limited=True,
    )
