"""Only a successful first foreground turn may bind unbound task provenance."""

from __future__ import annotations

# ruff: noqa: F811 - imported pytest fixture
import pytest

from ductor_bot.cli.types import AgentResponse
from ductor_bot.orchestrator.flows import (
    _preserve_session_from_response,
    _update_session,
    normal,
    normal_streaming,
)
from ductor_bot.session import SessionData, SessionKey
from tests.tasks.test_origin import create, hub  # noqa: F401


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("failed", [False, True])
async def test_first_turn_completion_binds_tasks_only_on_success(orch, hub, streaming, failed):
    orch.set_task_hub(hub)
    entries = []

    async def execute(request, **_kwargs):
        entries.append(
            create(
                hub,
                chat_id=request.chat_id,
                thread_id=request.topic_id,
                origin_transport=request.transport,
            )
        )
        return AgentResponse(result="synthetic reply", session_id="parent-session", is_error=failed)

    orch._cli_service.execute.side_effect = execute
    orch._cli_service.execute_streaming.side_effect = execute
    key = SessionKey("tg", 42, 9)
    if streaming:
        await normal_streaming(orch, key, "Work", transport_override="api")
    else:
        await normal(orch, key, "Work")
    assert len(entries) == 1
    assert entries[0].origin_session_id == ("" if failed else "parent-session")
    assert entries[0].origin_transport == ("api" if streaming else "tg")


@pytest.mark.parametrize("streaming", [False, True])
async def test_recovery_binds_only_tasks_created_during_successful_retry(orch, hub, streaming):
    orch.set_task_hub(hub)
    entries = []

    async def execute(request, **_kwargs):
        entries.append(
            create(
                hub,
                chat_id=request.chat_id,
                thread_id=request.topic_id,
                origin_transport=request.transport,
            )
        )
        if len(entries) == 1:
            return AgentResponse(result="No conversation found", is_error=True)
        return AgentResponse(result="recovered", session_id="recovered-parent")

    orch._cli_service.execute.side_effect = execute
    orch._cli_service.execute_streaming.side_effect = execute
    key = SessionKey("tg", 42, 9)
    await (normal_streaming(orch, key, "Work") if streaming else normal(orch, key, "Work"))
    assert len(entries) == 2
    assert entries[0].origin_session_id == ""
    assert entries[1].origin_session_id == "recovered-parent"


@pytest.mark.parametrize("path", ["no-timestamp", "preserve", "existing-session"])
async def test_unproven_first_turn_does_not_bind(orch, hub, path):
    orch.set_task_hub(hub)
    entry = create(hub)
    session = SessionData(
        chat_id=42,
        topic_id=9,
        provider="claude",
        session_id="existing" if path == "existing-session" else "",
    )
    response = AgentResponse(result="synthetic", session_id="parent-session")
    if path == "preserve":
        await _preserve_session_from_response(orch, session, response, reason="error")
    else:
        await _update_session(
            orch,
            session,
            response,
            turn_started_at=0 if path == "existing-session" else None,
        )
    assert entry.origin_session_id == ""
