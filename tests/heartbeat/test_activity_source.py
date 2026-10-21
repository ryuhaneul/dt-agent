"""Heartbeat targeting and dormancy follow user activity, not system turns."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
import time_machine

from ductor_bot.bus.adapters import from_cron_result
from ductor_bot.bus.bus import MessageBus
from ductor_bot.bus.envelope import DeliveryMode, Envelope, LockMode, Origin
from ductor_bot.cli.types import AgentRequest, AgentResponse
from ductor_bot.heartbeat.models import HeartbeatOutcome, HeartbeatStatus
from ductor_bot.heartbeat.observer import HeartbeatObserver, _KeyState
from ductor_bot.orchestrator.core import Orchestrator
from ductor_bot.orchestrator.flows import normal
from ductor_bot.session.key import SessionKey
from ductor_bot.session.manager import SessionData, SessionManager

CHAT = 100
OTHER = 200
KEY = SessionKey(chat_id=CHAT, topic_id=7)
OTHER_KEY = SessionKey(chat_id=CHAT, topic_id=8)


class _World:
    """A real orchestrator whose CLI is scripted: heartbeat prompts answer HEARTBEAT_OK."""

    def __init__(self, orch: Orchestrator) -> None:
        self.orch = orch
        orch._config.allowed_user_ids = [CHAT]
        orch._config.heartbeat.cooldown_minutes = 5
        self.heartbeat = orch._observers.heartbeat
        self.heartbeat_calls = 0
        self.session_ids = iter(f"sid-{n}" for n in range(1, 50))
        self.next_sid: str | None = None
        self.next_failure: str | None = None  # "timeout" or "error" for the next non-heartbeat turn
        orch._cli_service.execute = AsyncMock(side_effect=self._execute)

    async def _execute(self, request: AgentRequest) -> AgentResponse:
        if request.prompt == self.orch._config.heartbeat.prompt:
            self.heartbeat_calls += 1
            return AgentResponse(result="HEARTBEAT_OK", session_id=request.resume_session or "")
        sid = self.next_sid or request.resume_session or next(self.session_ids)
        self.next_sid = None
        failure, self.next_failure = self.next_failure, None
        if failure == "timeout":
            return AgentResponse(result="", session_id=sid, timed_out=True)
        if failure == "error":
            return AgentResponse(result="boom", session_id=sid, is_error=True)
        return AgentResponse(result="reply", session_id=sid)

    async def user_turn(self, key: SessionKey = KEY) -> None:
        await self.orch.handle_message(key, "hello")

    async def beat(self, key: SessionKey = KEY) -> int:
        """Run one heartbeat for *key* (10 minutes later); returns the CLI calls it made."""
        before = self.heartbeat_calls
        with time_machine.travel(datetime.now(UTC) + timedelta(minutes=10)):
            await self.heartbeat._run_for_chat(key.chat_id, key.topic_id)
        await asyncio.gather(*self.heartbeat._persist_tasks)
        return self.heartbeat_calls - before

    def state(self, key: SessionKey = KEY) -> _KeyState:
        return self.heartbeat._state[key.storage_key]

    async def session(self, key: SessionKey = KEY) -> SessionData:
        session = await self.orch._sessions.get_active(key)
        assert session is not None
        return session

    async def lint_turn(self, key: SessionKey = KEY) -> None:
        await self.orch.handle_message(key, "lint prompt", counts_as_activity=False)

    async def cron_turn(self, mode: str, key: SessionKey = KEY) -> None:
        """A cron inject / in_session turn, through the bus like a real job."""
        bus = MessageBus()
        transport = AsyncMock()
        transport.transport_name = "tg"
        bus.register_transport(transport)
        bus.set_injector(self.orch)  # type: ignore[arg-type]
        envelope = from_cron_result(
            "Job",
            "fallback",
            "success",
            chat_id=key.chat_id,
            topic_id=key.topic_id,
            prompt=f"cron prompt {mode}",
            job_id="job-1",
        )
        await bus.submit(envelope)
        assert envelope.injected


@pytest.fixture
def world(orch: Orchestrator) -> _World:
    return _World(orch)


async def _dormant_world(world: _World) -> _World:
    await world.user_turn()
    assert await world.beat() == 1  # the first check runs and is acknowledged
    assert world.state().acked_activity == 1
    assert await world.beat() == 0  # dormant now
    return world


# -- system turns do not wake a dormant topic ----------------------------------------------


@pytest.mark.parametrize("kind", ["lint", "inject", "in_session"])
async def test_a_system_turn_leaves_a_dormant_topic_dormant(world: _World, kind: str) -> None:
    await _dormant_world(world)
    before = await world.session()

    if kind == "lint":
        await world.lint_turn()
    else:
        await world.cron_turn(kind)

    after = await world.session()
    assert after.message_count == before.message_count + 1  # it was a turn...
    assert (after.activity_at, after.activity_turns) == (before.activity_at, before.activity_turns)
    assert await world.beat() == 0  # ...but not user activity


async def test_a_lint_turn_that_changes_the_provider_keeps_the_dormancy(world: _World) -> None:
    await _dormant_world(world)
    await world.orch._sessions.resolve_session_target(KEY, provider="codex", model="some-model")
    world.next_sid = "codex-sid"

    await world.lint_turn()

    session = await world.session()
    assert (session.provider, session.session_id) == ("codex", "codex-sid")
    assert await world.beat() == 0


async def test_a_system_turn_that_replaces_the_session_id_keeps_the_dormancy(
    world: _World,
) -> None:
    await _dormant_world(world)
    world.next_sid = "replacement-sid"

    await world.cron_turn("inject")

    assert (await world.session()).session_id == "replacement-sid"
    assert await world.beat() == 0


async def test_the_dormancy_survives_a_reload_from_disk(world: _World) -> None:
    await _dormant_world(world)
    await world.lint_turn()
    await world.cron_turn("in_session")

    paths = world.orch.paths
    config = world.orch._config
    sessions = SessionManager(paths.sessions_path, config)  # a restart: nothing in memory
    observer = HeartbeatObserver(config)
    observer.set_state_path(paths.heartbeat_state_path)
    observer.set_session_source(sessions.list_all, sessions.get_active)
    calls = AsyncMock(return_value=HeartbeatOutcome(HeartbeatStatus.ACK))
    observer.set_heartbeat_handler(calls)

    with time_machine.travel(datetime.now(UTC) + timedelta(minutes=10)):
        await observer._run_for_chat(KEY.chat_id, KEY.topic_id)

    calls.assert_not_awaited()


async def test_the_next_user_message_resumes_the_checks(world: _World) -> None:
    await _dormant_world(world)
    await world.lint_turn()
    await world.cron_turn("inject")
    assert await world.beat() == 0

    await world.user_turn()

    assert (await world.session()).activity_turns == 2
    assert await world.beat() == 1


@pytest.mark.parametrize("origin", [Origin.TASK_RESULT, Origin.INTERAGENT])
async def test_task_and_interagent_results_still_wake_the_topic(
    world: _World, origin: Origin
) -> None:
    await _dormant_world(world)
    bus = MessageBus()
    transport = AsyncMock()
    transport.transport_name = "tg"
    bus.register_transport(transport)
    bus.set_wake_hook(world.orch._on_bus_wake)

    await bus.submit(
        Envelope(
            origin=origin,
            chat_id=KEY.chat_id,
            topic_id=KEY.topic_id,
            result_text="done",
            delivery=DeliveryMode.UNICAST,
            lock_mode=LockMode.NONE,
        )
    )

    assert world.state().acked is None
    assert await world.beat() == 1


# -- a session made only by system turns ---------------------------------------------------


async def test_a_session_with_only_system_turns_is_no_target(world: _World) -> None:
    await world.lint_turn()  # creates the session
    session = await world.session()
    assert session.session_id
    assert (session.activity_at, session.activity_turns) == (None, 0)

    targets = await world.heartbeat._collect_targets()
    assert KEY.storage_key not in {t.key.storage_key for t in targets}

    world.heartbeat._recovery_pending = True
    await world.heartbeat._collect_targets()
    assert world.heartbeat._state.get(KEY.storage_key, _KeyState()).tracked is False

    assert await world.beat() == 0  # not even a manual run checks it


async def test_a_user_turn_makes_the_session_a_target(world: _World) -> None:
    await world.lint_turn()
    await world.user_turn()

    targets = await world.heartbeat._collect_targets()

    assert KEY.storage_key in {t.key.storage_key for t in targets}


# -- stored sessions of an older version ---------------------------------------------------


def _old_record(**extra: Any) -> dict[str, Any]:
    now = datetime.now(UTC).isoformat()
    record = {
        "chat_id": CHAT,
        "topic_id": 7,
        "provider": "claude",
        "model": "opus",
        "created_at": now,
        "last_active": now,
        "provider_sessions": {"claude": {"session_id": "old-sid", "message_count": 4}},
    }
    record.update(extra)
    return record


async def test_a_record_without_activity_keys_keeps_its_last_turn_as_activity(
    world: _World,
) -> None:
    record = _old_record()
    world.orch.paths.sessions_path.write_text(json.dumps({"tg:100:7": record}), encoding="utf-8")

    session = await world.session()

    assert session.activity_at == record["last_active"]
    assert session.activity_turns == 4
    targets = await world.heartbeat._collect_targets()
    assert KEY.storage_key in {t.key.storage_key for t in targets}


async def test_an_explicit_null_is_kept_as_no_activity(world: _World) -> None:
    record = _old_record(activity_at=None, activity_turns=0)
    world.orch.paths.sessions_path.write_text(json.dumps({"tg:100:7": record}), encoding="utf-8")

    session = await world.session()

    assert (session.activity_at, session.activity_turns) == (None, 0)


@pytest.mark.parametrize(
    ("acked", "dormant"),
    [
        (["claude", "old-sid", 4], True),
        (["claude", "old-sid", 3], False),  # a turn came after the ACK
        (["codex", "old-sid", 4], False),  # another provider
        (["claude", "other-sid", 4], False),  # another session
    ],
)
async def test_an_old_acked_state_is_converted_once(
    world: _World, acked: list[Any], dormant: bool
) -> None:
    world.orch.paths.sessions_path.write_text(
        json.dumps({"tg:100:7": _old_record()}), encoding="utf-8"
    )
    await world.heartbeat._ensure_loaded()
    world.heartbeat._state[KEY.storage_key] = _KeyState.from_json({"acked": acked, "tracked": True})

    assert await world.beat() == (0 if dormant else 1)

    if dormant:
        assert world.state().acked_activity == 4
        # a system turn afterwards (new session id) does not break the converted dormancy
        world.next_sid = "replacement-sid"
        await world.lint_turn()
        assert await world.beat() == 0


def test_the_state_file_keeps_the_physical_acked_and_adds_the_activity_key() -> None:
    state = _KeyState.from_json({"acked": ["claude", "sid", 3], "acked_activity": 2})
    assert (state.acked, state.acked_activity) == (("claude", "sid", 3), 2)
    old = _KeyState.from_json({"acked": ["claude", "sid", 3]})
    assert old.acked_activity is None
    unknown = _KeyState.from_json({"future_key": 1})  # unknown keys are ignored
    assert unknown.acked is None
    with pytest.raises(ValueError, match="activity"):
        _KeyState.from_json({"acked_activity": "x"})


# -- direct resets ----------------------------------------------------------------------------


async def test_new_wakes_that_topic_only(world: _World) -> None:
    await _dormant_world(world)
    await world.user_turn(OTHER_KEY)
    assert await world.beat(OTHER_KEY) == 1
    before = await world.session()

    provider = await world.orch.reset_active_provider_session(KEY)  # /new and /new @topic

    assert provider
    assert world.state().acked is None
    assert world.state().acked_activity is None
    assert world.state(OTHER_KEY).acked_activity == 1  # the other topic stays dormant
    session = await world.session()
    assert session.activity_turns == before.activity_turns + 1
    assert session.activity_at is not None


async def test_reset_command_wakes_through_the_message_path(world: _World) -> None:
    await _dormant_world(world)

    await world.orch.handle_message(KEY, "/reset")

    assert world.state().acked is None
    assert (await world.session()).activity_turns == 2


async def test_model_command_still_wakes(world: _World) -> None:
    await _dormant_world(world)

    await world.orch.handle_message(KEY, "/model")

    assert world.state().acked is None


async def test_a_reset_that_recovers_a_broken_session_is_not_user_activity(
    world: _World,
) -> None:
    await world.user_turn()
    before = await world.session()

    await world.orch._sessions.reset_provider_session(KEY, provider="claude", model="opus")

    after = await world.session()
    assert (after.activity_at, after.activity_turns) == (before.activity_at, before.activity_turns)


# -- quarantine follows the physical session id -----------------------------------------------


async def test_invalid_session_does_not_quarantine_a_replaced_session_id(world: _World) -> None:
    await world.user_turn()
    session = await world.session()
    hb = world.heartbeat
    await hb._ensure_loaded()
    stale = (session.provider, "sid-from-before", session.message_count)
    outcome = HeartbeatOutcome(
        HeartbeatStatus.FAILED,
        marker=stale,
        reason="invalid_session",
        activity=session.activity_turns,
    )
    hb._generation[KEY] = 0

    await hb._process_outcome(KEY, "auto", outcome, 0, 30)

    assert hb._state[KEY.storage_key].quarantine_sid is None

    same = HeartbeatOutcome(
        HeartbeatStatus.FAILED,
        marker=(session.provider, session.session_id, session.message_count),
        reason="invalid_session",
        activity=session.activity_turns,
    )
    hb._deliver_result = AsyncMock(return_value=True)  # type: ignore[method-assign]
    await hb._process_outcome(KEY, "auto", same, 0, 30)
    assert hb._state[KEY.storage_key].quarantine_sid == session.session_id


# -- the session fields ---------------------------------------------------------------------------


async def test_failed_and_stopped_turns_do_not_count_as_activity(world: _World) -> None:
    await world.user_turn()
    before = await world.session()
    world.orch._cli_service.execute = AsyncMock(
        return_value=AgentResponse(result="boom", is_error=True, session_id="x")
    )

    await normal(world.orch, KEY, "again")

    after = await world.session()
    assert (after.activity_at, after.activity_turns) == (before.activity_at, before.activity_turns)


# -- only completed turns count as activity ---------------------------------------------------------


async def _reloaded_beat(world: _World) -> int:
    """Heartbeat calls of a freshly started observer reading the stored sessions and state."""
    paths, config = world.orch.paths, world.orch._config
    sessions = SessionManager(paths.sessions_path, config)
    observer = HeartbeatObserver(config)
    observer.set_state_path(paths.heartbeat_state_path)
    observer.set_session_source(sessions.list_all, sessions.get_active)
    calls = AsyncMock(return_value=HeartbeatOutcome(HeartbeatStatus.ACK))
    observer.set_heartbeat_handler(calls)
    with time_machine.travel(datetime.now(UTC) + timedelta(minutes=10)):
        await observer._run_for_chat(KEY.chat_id, KEY.topic_id)
    return calls.await_count


async def test_a_lint_timeout_leaves_a_dormant_topic_dormant(world: _World) -> None:
    await _dormant_world(world)
    before = await world.session()
    world.next_failure = "timeout"

    await world.lint_turn()

    after = await world.session()
    assert (after.activity_at, after.activity_turns) == (before.activity_at, before.activity_turns)
    assert after.message_count == before.message_count + 1  # still stored like any turn
    assert after.session_id == before.session_id
    assert await world.beat() == 0
    assert await _reloaded_beat(world) == 0  # also after a restart


async def test_a_lint_timeout_in_a_new_session_is_no_activity_and_no_target(
    world: _World,
) -> None:
    world.next_failure = "timeout"

    await world.lint_turn()

    session = await world.session()
    assert session.session_id  # the id is kept so the session can be resumed
    assert (session.activity_at, session.activity_turns) == (None, 0)
    targets = await world.heartbeat._collect_targets()
    assert KEY.storage_key not in {t.key.storage_key for t in targets}
    world.heartbeat._recovery_pending = True
    await world.heartbeat._collect_targets()
    assert world.heartbeat._state.get(KEY.storage_key, _KeyState()).tracked is False


@pytest.mark.parametrize("failure", ["timeout", "error"])
async def test_a_user_turn_that_does_not_complete_is_no_activity(
    world: _World, failure: str
) -> None:
    await world.user_turn()
    before = await world.session()
    world.next_failure = failure

    await world.user_turn()

    after = await world.session()
    assert (after.activity_at, after.activity_turns) == (before.activity_at, before.activity_turns)
    assert after.activity_turns == 1
    if failure == "timeout":  # a timeout stores the turn; an error leaves the counters alone
        assert after.message_count == before.message_count + 1


@pytest.mark.parametrize("failure", ["timeout", "error"])
async def test_a_failed_task_result_injection_is_no_activity(world: _World, failure: str) -> None:
    from ductor_bot.orchestrator.injection import InjectionIncomplete

    await world.user_turn()
    before = await world.session()
    world.next_failure = failure

    with pytest.raises(InjectionIncomplete):
        await world.orch.inject_prompt("task result", KEY.chat_id, "task:1", topic_id=KEY.topic_id)

    after = await world.session()
    assert (after.activity_at, after.activity_turns) == (before.activity_at, before.activity_turns)
    assert after.message_count == before.message_count + 1  # the turn is stored as before


async def test_a_successful_task_result_injection_and_user_turn_still_count(
    world: _World,
) -> None:
    await world.user_turn()
    assert (await world.session()).activity_turns == 1

    await world.orch.inject_prompt("task result", KEY.chat_id, "task:1", topic_id=KEY.topic_id)
    assert (await world.session()).activity_turns == 2

    await world.user_turn()
    assert (await world.session()).activity_turns == 3
