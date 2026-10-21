"""Heartbeat observer: periodic background agent turns in the main session."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from ductor_bot.heartbeat.models import HeartbeatOutcome, HeartbeatStatus
from ductor_bot.i18n import t
from ductor_bot.infra.base_observer import BaseObserver
from ductor_bot.infra.json_store import atomic_json_save
from ductor_bot.log_context import set_log_context
from ductor_bot.session.key import SessionKey
from ductor_bot.utils.quiet_hours import check_quiet_hour

if TYPE_CHECKING:
    from ductor_bot.config import AgentConfig, HeartbeatConfig, HeartbeatTarget
    from ductor_bot.session.manager import SessionData

logger = logging.getLogger(__name__)

# Callback signature: (chat_id, alert_text, topic_id, transport)
HeartbeatResultCallback = Callable[[int, str, int | None, str], Awaitable[None]]

# Handler signature: (chat_id, topic_id, prompt_override, ack_token_override, transport)
HeartbeatHandler = Callable[
    [int, int | None, str | None, str | None, str], Awaitable[HeartbeatOutcome]
]

# Validator signature: (chat_id) -> is_accessible
ChatValidator = Callable[[int], Awaitable[bool]]

_VALIDATION_TTL = 3600


@dataclass
class _KeyState:
    acked: tuple[str, str, int] | None = None
    quarantine_sid: str | None = None
    quarantine_notified: bool = False
    fail_count: int = 0
    retry_after: float | None = None
    tracked: bool = False
    acked_activity: int | None = None

    @classmethod
    def from_json(cls, raw: object) -> _KeyState:
        if not isinstance(raw, dict):
            raise TypeError("key state must be an object")
        acked_raw = raw.get("acked")
        acked = None
        if acked_raw is not None:
            if (
                not isinstance(acked_raw, list)
                or len(acked_raw) != 3
                or not isinstance(acked_raw[0], str)
                or not isinstance(acked_raw[1], str)
                or type(acked_raw[2]) is not int
            ):
                raise ValueError("invalid acknowledgment marker")
            acked = (acked_raw[0], acked_raw[1], acked_raw[2])
        quarantine_sid = raw.get("quarantine_sid")
        quarantine_notified = raw.get("quarantine_notified", False)
        fail_count = raw.get("fail_count", 0)
        retry_after = raw.get("retry_after")
        tracked = raw.get("tracked", False)
        acked_activity = raw.get("acked_activity")
        if acked_activity is not None and (type(acked_activity) is not int or acked_activity < 0):
            raise ValueError("invalid acknowledged activity")
        if quarantine_sid is not None and not isinstance(quarantine_sid, str):
            raise ValueError("invalid quarantine session")
        if type(quarantine_notified) is not bool or type(tracked) is not bool:
            raise ValueError("invalid state flags")
        if type(fail_count) is not int or fail_count < 0:
            raise ValueError("invalid failure count")
        if retry_after is not None and not isinstance(retry_after, (int, float)):
            raise ValueError("invalid retry time")
        return cls(
            acked,
            quarantine_sid,
            quarantine_notified,
            fail_count,
            retry_after,
            tracked,
            acked_activity,
        )


@dataclass(frozen=True)
class _Target:
    key: SessionKey
    source: str
    config: HeartbeatTarget | None = None


class HeartbeatObserver(BaseObserver):
    """Sends periodic heartbeat prompts through the main session.

    Follows the CronObserver lifecycle pattern: start/stop with an asyncio
    background task. Results are delivered via a callback set by
    ``set_result_handler``.
    """

    def __init__(self, config: AgentConfig) -> None:
        super().__init__()
        self._config = config
        self._on_result: HeartbeatResultCallback | None = None
        self._handle_heartbeat: HeartbeatHandler | None = None
        self._is_chat_busy: Callable[[int, int | None], bool] | None = None
        self._stale_cleanup: Callable[[], Awaitable[int]] | None = None
        self._chat_validator: ChatValidator | None = None
        self._valid_targets: dict[int, float] = {}
        self._target_tasks: dict[tuple[str, int | None, int | None], asyncio.Task[None]] = {}
        self._state: dict[str, _KeyState] = {}
        self._reasons: dict[str, str] = {}
        self._generation: dict[SessionKey, int] = {}
        self._inflight: set[SessionKey] = set()
        self._state_path: Path | None = None
        self._list_sessions: Callable[[], Awaitable[list[SessionData]]] | None = None
        self._get_active: Callable[[SessionKey], Awaitable[SessionData | None]] | None = None
        self._persist_lock = asyncio.Lock()
        self._load_lock = asyncio.Lock()
        self._persist_tasks: set[asyncio.Task[bool]] = set()
        self._loaded = False
        self._pending_changes: list[tuple[str, SessionKey | None]] = []
        self._dirty = False
        self._recovery_pending = False
        self._auto_target_count = 0

    @property
    def _hb(self) -> HeartbeatConfig:
        return self._config.heartbeat

    def set_state_path(self, path: Path) -> None:
        self._state_path = path

    def set_session_source(
        self,
        list_all: Callable[[], Awaitable[list[SessionData]]],
        get_active: Callable[[SessionKey], Awaitable[SessionData | None]],
    ) -> None:
        self._list_sessions = list_all
        self._get_active = get_active

    def _config_fp(self) -> str:
        payload = {
            "heartbeat": self._config.heartbeat.model_dump(mode="json"),
            "allowed_user_ids": sorted(self._config.allowed_user_ids),
        }
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _snapshot(self) -> dict[str, object]:
        return {
            "config_fp": self._config_fp(),
            "recovery_pending": self._recovery_pending,
            "state": {key: asdict(value) for key, value in self._state.items()},
        }

    async def _persist_now(self) -> bool:
        if not self._loaded:
            await self._ensure_loaded()
        return await self._write_snapshot()

    async def _write_snapshot(self) -> bool:
        path = self._state_path
        if path is None:
            return True
        async with self._persist_lock:
            snapshot = self._snapshot()
            for _attempt in range(2):
                try:
                    await asyncio.to_thread(atomic_json_save, path, snapshot)
                except Exception as exc:
                    logger.debug("Heartbeat state save attempt failed: %s", exc)
                else:
                    self._dirty = False
                    return True
            try:
                await asyncio.to_thread(path.unlink, missing_ok=True)
            except Exception:
                self._dirty = True
                logger.exception("Heartbeat state save and removal failed: %s", path)
                return False
            self._dirty = False
            logger.error("Heartbeat state save failed; removed stale state: %s", path)
            return False

    def _schedule_persist(self) -> None:
        if self._state_path is None:
            return
        task = asyncio.create_task(self._persist_now())
        self._persist_tasks.add(task)
        task.add_done_callback(self._persist_tasks.discard)

    @staticmethod
    def _parse_state(raw: object) -> tuple[dict[str, _KeyState], str, bool]:
        if not isinstance(raw, dict):
            raise TypeError("state root must be an object")
        fp = raw["config_fp"]
        recovery = raw["recovery_pending"]
        entries = raw["state"]
        if (
            not isinstance(fp, str)
            or len(fp) != 64
            or any(c not in "0123456789abcdef" for c in fp)
            or type(recovery) is not bool
            or not isinstance(entries, dict)
        ):
            raise ValueError("invalid heartbeat state header")
        if any(not isinstance(key, str) for key in entries):
            raise ValueError("invalid heartbeat state key")
        state = {key: _KeyState.from_json(value) for key, value in entries.items()}
        return state, fp, recovery

    async def _restore_existing(self, path: Path, initialized: Path) -> None:
        try:
            raw = json.loads(await asyncio.to_thread(path.read_text, encoding="utf-8"))
            state, fp, recovery = self._parse_state(raw)
        except (OSError, ValueError, KeyError, TypeError):
            corrupt = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
            try:
                await asyncio.to_thread(path.rename, corrupt)
                logger.warning("Heartbeat state corrupt; preserved at %s", corrupt)
            except OSError:
                logger.exception(
                    "Heartbeat state corrupt; could not preserve %s; recovery will overwrite it",
                    path,
                )
            self._state = {}
            self._recovery_pending = True
            await self._write_snapshot()
            if not await asyncio.to_thread(initialized.exists):
                await self._create_initialized(initialized)
            return

        self._state = state
        self._recovery_pending = recovery
        if fp != self._config_fp():
            self._clear_suspend_state(via="reload")
            await self._write_snapshot()
        if not await asyncio.to_thread(initialized.exists):
            await self._create_initialized(initialized)

    async def _ensure_loaded(self) -> None:
        async with self._load_lock:
            if self._loaded:
                return
            path = self._state_path
            if path is not None:
                initialized = path.with_name("heartbeat_state.initialized")
                if path.exists():
                    await self._restore_existing(path, initialized)
                elif initialized.exists():
                    self._state = {}
                    self._recovery_pending = True
                    await self._write_snapshot()
                else:
                    self._state = {}
                    self._recovery_pending = False
                    if await self._write_snapshot():
                        await self._create_initialized(initialized)
            while self._pending_changes:
                pending, self._pending_changes = self._pending_changes, []
                for via, key in pending:
                    if key is None:
                        self._clear_suspend_state(via="reload")
                    else:
                        self._wake_state(key, via=via)
                if path is not None:
                    await self._write_snapshot()
            self._loaded = True

    async def _create_initialized(self, path: Path) -> None:
        try:
            await asyncio.to_thread(path.touch, exist_ok=True)
        except OSError:
            logger.exception("Heartbeat state initialization marker failed: %s", path)

    def _wake_state(self, key: SessionKey, *, via: str) -> None:
        storage_key = key.storage_key
        state = self._state.setdefault(storage_key, _KeyState())
        changed = (
            state.acked is not None
            or state.acked_activity is not None
            or state.retry_after is not None
        )
        state.acked = None
        state.acked_activity = None
        state.fail_count = 0
        state.retry_after = None
        state.tracked = True
        if key in self._inflight:
            self._generation[key] = self._generation.get(key, 0) + 1
        if changed:
            logger.info("Heartbeat wake key=%s via=%s", storage_key, via)

    async def wake(self, key: SessionKey) -> None:
        if self._loaded:
            self._wake_state(key, via="bus")
        else:
            self._pending_changes.append(("bus", key))
            await self._ensure_loaded()
        await self._persist_now()

    def set_result_handler(self, handler: HeartbeatResultCallback) -> None:
        """Set callback for delivering alert messages to the user."""
        self._on_result = handler

    def set_heartbeat_handler(self, handler: HeartbeatHandler) -> None:
        """Set the function that executes a heartbeat turn (orchestrator.handle_heartbeat)."""
        self._handle_heartbeat = handler

    def set_busy_check(self, check: Callable[[int, int | None], bool]) -> None:
        """Set the function that checks if a chat has active CLI processes."""
        self._is_chat_busy = check

    def set_stale_cleanup(self, cleanup: Callable[[], Awaitable[int]]) -> None:
        """Set the function that kills stale CLI processes (wall-clock based)."""
        self._stale_cleanup = cleanup

    def set_chat_validator(self, validator: ChatValidator) -> None:
        """Set the function that validates whether a group chat is accessible."""
        self._chat_validator = validator

    async def start(self) -> None:
        """Start the heartbeat background loop and per-target loops."""
        if not self._hb.enabled:
            logger.info("Heartbeat disabled in config")
            return
        if self._handle_heartbeat is None:
            logger.error("Heartbeat handler not set, cannot start")
            return
        await self._ensure_loaded()
        await super().start()
        self._start_target_loops()
        logger.info(
            "Heartbeat started (every %dm, quiet %d:00-%d:00, %d group target(s))",
            self._hb.interval_minutes,
            self._hb.quiet_start,
            self._hb.quiet_end,
            sum(1 for t in self._hb.group_targets if t.enabled and t.chat_id is not None),
        )

    async def stop(self) -> None:
        """Stop the heartbeat background loop and all target loops."""
        for task in self._target_tasks.values():
            task.cancel()
        for task in self._target_tasks.values():
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._target_tasks.clear()
        await super().stop()
        if self._persist_tasks:
            await asyncio.gather(*self._persist_tasks)
        self._generation.clear()
        self._inflight.clear()
        logger.info("Heartbeat stopped")

    def notify_activity(self, key: SessionKey) -> None:
        """Wake one key after accepted user activity; persist asynchronously."""
        if self._loaded:
            self._wake_state(key, via="user")
        else:
            self._pending_changes.append(("user", key))
        self._schedule_persist()

    def _clear_suspend_state(self, *, via: str) -> None:
        for storage_key, state in self._state.items():
            changed = (
                state.acked is not None
                or state.acked_activity is not None
                or state.retry_after is not None
            )
            state.acked = None
            state.acked_activity = None
            state.fail_count = 0
            state.retry_after = None
            if changed:
                logger.info("Heartbeat wake key=%s via=%s", storage_key, via)
        for key in self._inflight:
            self._generation[key] = self._generation.get(key, 0) + 1

    def reset_suspend_state(self) -> None:
        """Clear ACK and backoff while preserving session quarantine."""
        if self._loaded:
            self._clear_suspend_state(via="reload")
        else:
            self._pending_changes.append(("reload", None))
        self._schedule_persist()

    def _is_allowed_auto(self, key: SessionKey) -> bool:
        if key.transport != "tg":
            return False
        if key.chat_id in self._config.allowed_group_ids:
            return True
        return key.chat_id >= 0 and key.chat_id in self._config.allowed_user_ids

    def _note(self, key: SessionKey, reason: str) -> None:
        storage_key = key.storage_key
        if self._reasons.get(storage_key) != reason:
            logger.info("Heartbeat %s: %s", storage_key, reason)
            self._reasons[storage_key] = reason

    def _manual_targets(self) -> tuple[dict[str, _Target], set[str], set[str]]:
        targets: dict[str, _Target] = {}
        opted_out: set[str] = set()
        manual_keys: set[str] = set()
        for config in self._hb.group_targets:
            if config.chat_id is None:
                continue
            key = SessionKey.for_transport(config.transport, config.chat_id, config.topic_id)
            storage_key = key.storage_key
            if not config.enabled:
                opted_out.add(storage_key)
                targets.pop(storage_key, None)
                manual_keys.discard(storage_key)
            elif storage_key not in opted_out:
                targets[storage_key] = _Target(key, "manual", config)
                manual_keys.add(storage_key)
        return targets, opted_out, manual_keys

    def _prune_state(self, sessions: list[SessionData], manual_keys: set[str]) -> bool:
        known_keys = {session.session_key.storage_key for session in sessions}
        changed = False
        for storage_key in list(self._state):
            try:
                key = SessionKey.parse(storage_key)
            except ValueError:
                key = None
            if (
                storage_key not in known_keys
                or key is None
                or (storage_key not in manual_keys and not self._is_allowed_auto(key))
            ):
                del self._state[storage_key]
                changed = True
        return changed

    def _add_auto_targets(
        self,
        sessions: list[SessionData],
        targets: dict[str, _Target],
        opted_out: set[str],
        *,
        recovery_scan: bool,
    ) -> bool:
        changed = False
        cutoff = datetime.now(UTC) - timedelta(hours=self._hb.auto_target_window_hours)
        for session in sessions:
            key = session.session_key
            storage_key = key.storage_key
            if storage_key in opted_out or not self._is_allowed_auto(key) or not session.session_id:
                continue
            if session.activity_at is None:
                # Only system turns so far (no user activity): never a heartbeat target.
                self._note(key, "no_user_activity")
                continue
            if recovery_scan:
                state = self._state.setdefault(storage_key, _KeyState())
                if not state.tracked:
                    state.tracked = True
                    changed = True
            if storage_key in targets:
                continue
            if not recovery_scan:
                try:
                    last_active = datetime.fromisoformat(session.activity_at)
                    recent = last_active >= cutoff
                except (TypeError, ValueError):
                    self._note(key, "bad_timestamp")
                    continue
                tracked_state = self._state.get(storage_key)
                if not recent and not (tracked_state is not None and tracked_state.tracked):
                    continue
            targets[storage_key] = _Target(key, "auto")
        return changed

    async def _collect_targets(self) -> list[_Target]:  # noqa: C901
        """Select manual and eligible automatic targets once for this tick."""
        await self._ensure_loaded()
        targets, opted_out, manual_keys = self._manual_targets()
        if not self._hb.auto_targets:
            for chat_id in self._config.allowed_user_ids:
                key = SessionKey.telegram(chat_id)
                if key.storage_key not in opted_out and key.storage_key not in targets:
                    targets[key.storage_key] = _Target(key, "auto")

        sessions = await self._list_sessions() if self._list_sessions is not None else None
        if sessions is not None:
            changed = self._prune_state(sessions, manual_keys)
            if self._hb.auto_targets:
                recovery_scan = self._recovery_pending
                changed |= self._add_auto_targets(
                    sessions, targets, opted_out, recovery_scan=recovery_scan
                )
                if recovery_scan:
                    self._recovery_pending = False
                    await self._persist_now()
                elif changed:
                    self._schedule_persist()
            elif changed:
                self._schedule_persist()

        reason_keys = set(targets)
        if sessions is not None:
            reason_keys.update(session.session_key.storage_key for session in sessions)
        for storage_key in self._reasons.keys() - reason_keys:
            del self._reasons[storage_key]

        auto_count = (
            sum(target.source == "auto" for target in targets.values())
            if self._hb.auto_targets
            else 0
        )
        if auto_count != self._auto_target_count:
            logger.info("Heartbeat auto targets: %d", auto_count)
            self._auto_target_count = auto_count
        return list(targets.values())

    def _start_target_loops(self) -> None:
        """Launch independent loops for group targets with custom intervals."""
        for target in self._hb.group_targets:
            if not target.enabled or target.chat_id is None:
                continue
            interval = target.interval_minutes or self._hb.interval_minutes
            if interval == self._hb.interval_minutes and target.interval_minutes is None:
                continue  # No custom interval → runs with global tick
            key = (target.transport, target.chat_id, target.topic_id)
            task = asyncio.create_task(self._target_loop(target, interval))
            task.add_done_callback(lambda _: None)
            self._target_tasks[key] = task
            logger.info(
                "Heartbeat target %d/%s started (every %dm)",
                target.chat_id,
                target.topic_id,
                interval,
            )

    async def _target_loop(self, target: HeartbeatTarget, interval_minutes: int) -> None:
        """Independent loop for a single group target."""
        assert target.chat_id is not None
        try:
            while self._running:
                await asyncio.sleep(interval_minutes * 60)
                if not self._running or not self._hb.enabled:
                    continue
                if self._dirty:
                    await self._persist_now()
                if not target.enabled:
                    continue
                if not await self._validate_target(target.chat_id):
                    continue
                prompt, ack_token, quiet_start, quiet_end = self._resolve_target_settings(target)
                if self._is_target_quiet(target.chat_id, quiet_start, quiet_end):
                    continue
                await self._run_for_chat(
                    target.chat_id,
                    target.topic_id,
                    transport=target.transport,
                    prompt=prompt,
                    ack_token=ack_token,
                    quiet_start=quiet_start,
                    quiet_end=quiet_end,
                    source="manual",
                    interval_minutes=interval_minutes,
                )
        except asyncio.CancelledError:
            logger.debug("Target loop %d cancelled", target.chat_id)

    def _resolve_target_settings(self, target: HeartbeatTarget) -> tuple[str, str, int, int]:
        """Resolve per-target settings with global fallback.

        Returns ``(prompt, ack_token, quiet_start, quiet_end)``.
        """
        prompt = target.prompt or self._hb.prompt
        ack_token = target.ack_token or self._hb.ack_token
        quiet_start = target.quiet_start if target.quiet_start is not None else self._hb.quiet_start
        quiet_end = target.quiet_end if target.quiet_end is not None else self._hb.quiet_end
        return prompt, ack_token, quiet_start, quiet_end

    async def _validate_target(self, chat_id: int) -> bool:
        """Check if a group target is accessible, with TTL cache."""
        if self._chat_validator is None:
            return True

        now = time.time()
        last = self._valid_targets.get(chat_id)
        if last is not None and (now - last) < _VALIDATION_TTL:
            return True

        try:
            valid = await self._chat_validator(chat_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Chat validation failed for %d", chat_id)
            return False

        if valid:
            self._valid_targets[chat_id] = now
            return True

        logger.warning("Heartbeat target %d is not accessible, skipping", chat_id)
        return False

    async def _run(self) -> None:
        """Sleep -> check -> execute -> repeat."""
        last_wall = time.time()
        try:
            while self._running:
                interval = self._hb.interval_minutes * 60
                await asyncio.sleep(interval)
                if not self._running or not self._hb.enabled:
                    continue

                now_wall = time.time()
                wall_elapsed = now_wall - last_wall
                if wall_elapsed > interval * 2:
                    logger.warning(
                        "Wall-clock gap: %.0fs (expected ~%ds) -- system likely suspended",
                        wall_elapsed,
                        interval,
                    )
                last_wall = now_wall

                try:
                    await self._tick()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Heartbeat tick failed (continuing)")
        except asyncio.CancelledError:
            logger.debug("Heartbeat loop cancelled")

    async def _cleanup_stale(self) -> None:
        """Kill stale CLI processes (catches suspend hangovers)."""
        if not self._stale_cleanup:
            return
        try:
            killed = await self._stale_cleanup()
            if killed:
                logger.info("Cleaned up %d stale process(es)", killed)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Stale process cleanup failed")

    async def _tick(self) -> None:
        """Run one heartbeat cycle for the current target set."""
        await self._ensure_loaded()
        if self._dirty:
            await self._persist_now()
        await self._cleanup_stale()
        targets = await self._collect_targets()

        is_quiet, now_hour, tz = check_quiet_hour(
            quiet_start=self._hb.quiet_start,
            quiet_end=self._hb.quiet_end,
            user_timezone=self._config.user_timezone,
            global_quiet_start=self._hb.quiet_start,
            global_quiet_end=self._hb.quiet_end,
        )
        if is_quiet:
            logger.debug("Heartbeat skipped: quiet hours (%d:00 %s)", now_hour, tz.key)
            return

        logger.debug("Heartbeat tick: checking %d chat(s)", len(targets))
        await self._tick_group_targets(targets)

    async def _tick_group_targets(self, targets: list[_Target] | None = None) -> None:
        """Run collected targets that do not have an independent interval loop."""
        if targets is None:
            targets = await self._collect_targets()
        for target in targets:
            key = target.key
            config = target.config
            prompt: str | None = None
            ack_token: str | None = None
            quiet_start: int | None = None
            quiet_end: int | None = None
            if config is not None:
                if (key.transport, key.chat_id, key.topic_id) in self._target_tasks:
                    continue
                if not await self._validate_target(key.chat_id):
                    continue
                prompt, ack_token, quiet_start, quiet_end = self._resolve_target_settings(config)
            await self._run_for_chat(
                key.chat_id,
                key.topic_id,
                transport=key.transport,
                prompt=prompt,
                ack_token=ack_token,
                quiet_start=quiet_start,
                quiet_end=quiet_end,
                source=target.source,
                interval_minutes=config.interval_minutes if config is not None else None,
            )

    def _is_target_quiet(
        self, chat_id: int, quiet_start: int | None, quiet_end: int | None
    ) -> bool:
        """Check per-target quiet hours. Returns True if quiet."""
        if quiet_start is None or quiet_end is None:
            return False
        is_quiet, _hour, _tz = check_quiet_hour(
            quiet_start=quiet_start,
            quiet_end=quiet_end,
            user_timezone=self._config.user_timezone,
            global_quiet_start=self._hb.quiet_start,
            global_quiet_end=self._hb.quiet_end,
        )
        if is_quiet:
            logger.debug("Heartbeat skipped for %d: per-target quiet hours", chat_id)
        return is_quiet

    @staticmethod
    def _session_marker(session: SessionData | None) -> tuple[str, str, int] | None:
        if session is None:
            return None
        return (session.provider, session.session_id, session.message_count)

    @staticmethod
    def _activity_of(session: SessionData | None) -> int | None:
        """Identifier of the user activity of *session* (what ACK dormancy is judged on)."""
        return None if session is None else session.activity_turns

    def _acked_activity(self, state: _KeyState, current: SessionData | None) -> int | None:
        """The user-activity count an ACK was given at.

        A state written before ``acked_activity`` existed has only the physical marker; its
        count is taken over once, when the session still has the marker's provider and
        session id (otherwise it is not dormant, as before).
        """
        if state.acked_activity is not None:
            return state.acked_activity
        acked = state.acked
        if acked is None or current is None:
            return None
        if acked[0] != current.provider or acked[1] != current.session_id:
            return None
        state.acked_activity = acked[2]
        self._schedule_persist()
        return acked[2]

    def _is_dormant(self, state: _KeyState, current: SessionData | None) -> bool:
        acked = self._acked_activity(state, current)
        return acked is not None and acked == self._activity_of(current)

    @staticmethod
    def _marker_str(marker: tuple[str, str, int] | None) -> str:
        if marker is None:
            return "-"
        return f"{marker[0]}:{marker[1][:8]}:{marker[2]}"

    def _requires_allowlist(self, key: SessionKey, source: str) -> bool:
        return source == "auto" or key.transport == "tg"

    def _log_run(
        self,
        key: SessionKey,
        source: str,
        outcome: str,
        reason: str,
        marker: tuple[str, str, int] | None,
    ) -> None:
        logger.info(
            "Heartbeat run key=%s source=%s outcome=%s reason=%s marker=%s",
            key.storage_key,
            source,
            outcome,
            reason,
            self._marker_str(marker),
        )

    async def _deliver_result(self, key: SessionKey, source: str, text: str) -> bool:
        if self._on_result is None:
            return False
        if self._requires_allowlist(key, source) and not self._is_allowed_auto(key):
            self._note(key, "not_allowed")
            return False
        await self._on_result(key.chat_id, text, key.topic_id, key.transport)
        return True

    async def _notify_quarantine(self, key: SessionKey, source: str, state: _KeyState) -> None:
        if state.quarantine_notified:
            return
        quarantine_sid = state.quarantine_sid
        try:
            delivered = await self._deliver_result(
                key,
                source,
                t("heartbeat.quarantine_stopped"),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Heartbeat quarantine notification failed")
            return
        if delivered and quarantine_sid is not None and state.quarantine_sid == quarantine_sid:
            state.quarantine_notified = True
            await self._persist_now()

    async def _process_outcome(  # noqa: C901, PLR0911
        self,
        key: SessionKey,
        source: str,
        outcome: HeartbeatOutcome,
        start_gen: int,
        interval_minutes: int,
    ) -> None:
        if outcome.status is HeartbeatStatus.SKIPPED:
            self._note(key, outcome.reason or "skipped")
            return

        self._note(key, "ran")
        current = await self._get_active(key) if self._get_active is not None else None
        current_marker = self._session_marker(current)
        same_generation = self._generation.get(key) == start_gen
        state = self._state.setdefault(key.storage_key, _KeyState())
        if outcome.status is HeartbeatStatus.ACK:
            if outcome.activity is not None:
                unchanged = current is not None and self._activity_of(current) == outcome.activity
            else:
                unchanged = current_marker == outcome.marker
            if same_generation and outcome.marker is not None and unchanged:
                state.fail_count = 0
                state.retry_after = None
                if self._hb.suspend_on_ack:
                    state.acked = outcome.marker
                    state.acked_activity = outcome.activity
                    if await self._persist_now() and state.acked == outcome.marker:
                        logger.info(
                            "Heartbeat dormant key=%s marker=%s",
                            key.storage_key,
                            self._marker_str(outcome.marker),
                        )
            return

        if outcome.status is HeartbeatStatus.ALERT:
            await self._deliver_result(key, source, outcome.text)
            return

        if outcome.status is not HeartbeatStatus.FAILED or not same_generation:
            return
        if outcome.reason == "invalid_session":
            if outcome.marker is None or current is None:
                return
            if current.session_id != outcome.marker[1]:
                return
            state.quarantine_sid = outcome.marker[1]
            await self._notify_quarantine(key, source, state)
            return

        state.fail_count += 1
        multiplier = min(2 ** (state.fail_count - 1), 4)
        state.retry_after = time.time() + interval_minutes * 60 * multiplier

    async def _run_for_chat(  # noqa: C901, PLR0911, PLR0912, PLR0913, PLR0915
        self,
        chat_id: int,
        topic_id: int | None = None,
        *,
        transport: str = "tg",
        prompt: str | None = None,
        ack_token: str | None = None,
        quiet_start: int | None = None,
        quiet_end: int | None = None,
        source: str = "manual",
        interval_minutes: int | None = None,
    ) -> None:
        """Execute a heartbeat for one selected chat."""
        set_log_context(operation="hb", chat_id=chat_id)
        key = SessionKey.for_transport(transport, chat_id, topic_id)
        await self._ensure_loaded()
        state = self._state.setdefault(key.storage_key, _KeyState())
        if not state.tracked:
            state.tracked = True
            self._schedule_persist()
        current = await self._get_active(key) if self._get_active is not None else None

        if state.quarantine_sid is not None and (
            current is None or current.session_id != state.quarantine_sid
        ):
            state.quarantine_sid = None
            state.quarantine_notified = False
            self._schedule_persist()
        if self._requires_allowlist(key, source) and not self._is_allowed_auto(key):
            self._note(key, "not_allowed")
            return
        if self._hb.suspend_on_ack and self._is_dormant(state, current):
            self._note(key, "dormant")
            return
        if (
            state.quarantine_sid is not None
            and current is not None
            and state.quarantine_sid == current.session_id
        ):
            if not state.quarantine_notified:
                if key in self._inflight:
                    self._note(key, "inflight")
                    return
                self._inflight.add(key)
                self._generation.setdefault(key, 0)
                try:
                    self._note(key, "quarantined")
                    await self._notify_quarantine(key, source, state)
                finally:
                    self._generation.pop(key, None)
                    self._inflight.discard(key)
            else:
                self._note(key, "quarantined")
            return
        if state.retry_after is not None and state.retry_after > time.time():
            self._note(key, "backoff")
            return
        if self._is_target_quiet(chat_id, quiet_start, quiet_end):
            self._note(key, "quiet")
            return
        if self._is_chat_busy and self._is_chat_busy(chat_id, topic_id):
            self._note(key, "busy")
            return
        if self._handle_heartbeat is None:
            return
        if key in self._inflight:
            self._note(key, "inflight")
            return

        self._inflight.add(key)
        start_gen = self._generation.setdefault(key, 0)
        interval = interval_minutes or self._hb.interval_minutes
        try:
            try:
                outcome = await self._handle_heartbeat(
                    chat_id, topic_id, prompt, ack_token, transport
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log_run(key, source, "exception", type(exc).__name__, None)
                logger.exception("Heartbeat execution error")
                return
            if outcome.status is not HeartbeatStatus.SKIPPED:
                self._log_run(key, source, outcome.status.value, outcome.reason, outcome.marker)
            await self._process_outcome(key, source, outcome, start_gen, interval)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Heartbeat result delivery error")
        finally:
            self._state.setdefault(key.storage_key, _KeyState()).tracked = True
            self._schedule_persist()
            self._generation.pop(key, None)
            self._inflight.discard(key)
