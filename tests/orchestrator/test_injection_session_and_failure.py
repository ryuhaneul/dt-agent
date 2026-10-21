"""Non-isolated injection: a missing session is created and keeps the returned id; an error or
timeout is no reply (cron results then stay deliverable)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from ductor_bot.bus.adapters import from_cron_result
from ductor_bot.bus.bus import MessageBus
from ductor_bot.bus.envelope import Envelope
from ductor_bot.cli.types import AgentResponse
from ductor_bot.config import AgentConfig
from ductor_bot.orchestrator import flows
from ductor_bot.orchestrator.core import Orchestrator
from ductor_bot.orchestrator.injection import InjectionIncomplete
from ductor_bot.session.key import SessionKey
from ductor_bot.workspace.paths import DuctorPaths

CHAT = 4242
TOPIC = 7
OTHER_TOPIC = 8
TRANSPORTS = ["tg", "mx", "sl"]


@pytest.fixture
def orch(workspace: tuple[DuctorPaths, AgentConfig]) -> Orchestrator:
    paths, config = workspace
    config.allowed_user_ids = [12345]
    o = Orchestrator(config, paths)
    object.__setattr__(
        o._cli_service,
        "execute",
        AsyncMock(return_value=AgentResponse(result="r", session_id="sid")),
    )
    return o


def _key(transport: str, topic: int | None = TOPIC) -> SessionKey:
    return SessionKey(transport=transport, chat_id=CHAT, topic_id=topic)


def _respond(orch: Orchestrator, *responses: AgentResponse) -> AsyncMock:
    execute = AsyncMock(side_effect=list(responses))
    object.__setattr__(orch._cli_service, "execute", execute)
    return execute


# -- no active session -------------------------------------------------------------------


@pytest.mark.parametrize("transport", TRANSPORTS)
async def test_a_missing_session_is_created_and_keeps_the_returned_id(
    orch: Orchestrator, transport: str
) -> None:
    other, _ = await orch._sessions.resolve_session(_key(transport, OTHER_TOPIC))
    other.session_id = "other-sid"
    await orch._sessions.update_session(other)
    _respond(orch, AgentResponse(result="reply", session_id="sid-after-cron"))

    reply = await orch.inject_prompt(
        "prompt", CHAT, "cron:j:x", topic_id=TOPIC, transport=transport
    )

    assert reply == "reply"
    stored = await orch._sessions.get_active(_key(transport))
    assert stored is not None
    assert stored.session_id == "sid-after-cron"
    assert stored.provider
    assert stored.model  # the configured target, not blank
    untouched = await orch._sessions.get_active(_key(transport, OTHER_TOPIC))
    assert untouched is not None
    assert untouched.session_id == "other-sid"  # another topic's session is not touched
    assert await orch._sessions.get_active(_key(transport, None)) is None


@pytest.mark.parametrize("transport", TRANSPORTS)
async def test_the_next_user_turn_and_the_next_injection_resume_that_id(
    orch: Orchestrator, transport: str
) -> None:
    execute = _respond(
        orch,
        AgentResponse(result="reply", session_id="sid-after-cron"),
        AgentResponse(result="again", session_id="sid-after-cron"),
    )
    await orch.inject_prompt("first", CHAT, "cron:j:x", topic_id=TOPIC, transport=transport)

    request, session = await flows._prepare_normal(orch, _key(transport), "hello")
    assert request.resume_session == "sid-after-cron"
    assert session.session_id == "sid-after-cron"

    await orch.inject_prompt("second", CHAT, "cron:j:y", topic_id=TOPIC, transport=transport)
    assert execute.await_args_list[0].args[0].resume_session is None
    assert execute.await_args_list[1].args[0].resume_session == "sid-after-cron"


async def test_the_injection_that_starts_the_session_carries_the_memory(
    orch: Orchestrator,
) -> None:
    execute = _respond(orch, AgentResponse(result="reply", session_id="sid-after-cron"))
    await orch.inject_prompt("prompt", CHAT, "cron:j:x", topic_id=TOPIC, transport="tg")

    first = execute.await_args_list[0].args[0]
    assert "Main Memory" in (first.append_system_prompt or "")  # a new session, as before
    assert first.provider_override  # and it runs on the session's own target


async def test_task_and_other_injections_without_a_session_follow_the_same_rule(
    orch: Orchestrator,
) -> None:
    # Task results and inter-agent results use the same non-isolated injection.
    _respond(orch, AgentResponse(result="reply", session_id="sid-task"))
    await orch.inject_prompt("task result", CHAT, "task_result:x", topic_id=TOPIC, transport="tg")
    stored = await orch._sessions.get_active(_key("tg"))
    assert stored is not None
    assert stored.session_id == "sid-task"


async def test_an_isolated_injection_still_creates_no_session(orch: Orchestrator) -> None:
    _respond(orch, AgentResponse(result="reply", session_id="sid-iso"))
    await orch.inject_prompt("prompt", CHAT, "label", topic_id=TOPIC, transport="tg", isolated=True)
    assert await orch._sessions.get_active(_key("tg")) is None


# -- an error or timeout is no reply ----------------------------------------------------------


@pytest.mark.parametrize(
    "response",
    [
        AgentResponse(result="", is_error=True, timed_out=True),
        AgentResponse(result="boom", is_error=True),
    ],
    ids=["timeout", "error"],
)
async def test_an_error_or_timeout_raises_instead_of_returning_a_reply(
    orch: Orchestrator, response: AgentResponse
) -> None:
    _respond(orch, response)
    with pytest.raises(InjectionIncomplete):
        await orch.inject_prompt("prompt", CHAT, "cron:j:x", topic_id=TOPIC, transport="tg")


class _Transport:
    transport_name = "tg"

    def __init__(self) -> None:
        self.delivered: list[Envelope] = []

    async def deliver(self, envelope: Envelope) -> None:
        self.delivered.append(envelope)

    async def deliver_broadcast(self, envelope: Envelope) -> None:
        self.delivered.append(envelope)


async def _cron_through_the_bus(
    orch: Orchestrator, response: AgentResponse
) -> tuple[Envelope, Any]:
    bus = MessageBus()
    transport = _Transport()
    bus.register_transport(transport)
    bus.set_injector(orch)
    _respond(orch, response)
    env = from_cron_result(
        "Daily",
        "RAW RESULT SENTINEL",
        "success",
        chat_id=CHAT,
        topic_id=TOPIC,
        transport="tg",
        prompt="PROMPT",
        job_id="j",
    )
    await bus.submit(env)
    return env, transport


@pytest.mark.parametrize("with_session", [False, True], ids=["no-session", "active-session"])
@pytest.mark.parametrize(
    "response",
    [
        AgentResponse(result="", is_error=True, timed_out=True),
        AgentResponse(result="", is_error=True),
        AgentResponse(result="   ", session_id="s"),
    ],
    ids=["timeout", "error", "empty"],
)
async def test_a_failed_injection_delivers_the_jobs_own_result(
    orch: Orchestrator, response: AgentResponse, with_session: bool
) -> None:
    if with_session:
        session, _ = await orch._sessions.resolve_session(_key("tg"))
        session.session_id = "old-sid"
        await orch._sessions.update_session(session)

    env, transport = await _cron_through_the_bus(orch, response)

    assert not env.injected
    assert [e.result_text for e in transport.delivered] == ["RAW RESULT SENTINEL"]


async def test_a_good_injection_delivers_only_the_session_reply(orch: Orchestrator) -> None:
    env, transport = await _cron_through_the_bus(
        orch, AgentResponse(result="session reply", session_id="s")
    )
    assert env.injected
    assert [e.result_text for e in transport.delivered] == ["session reply"]
