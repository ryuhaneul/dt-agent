"""Framework hook text (first-run setup, memory check) reaches the AI only.

Stored in-flight previews and recovery notices keep the user's own words, and Named
sessions get the first-run hint under the same rules as normal chats.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from ductor_bot.background.models import BackgroundTask
from ductor_bot.background.observer import BackgroundObserver
from ductor_bot.cli.types import AgentResponse
from ductor_bot.orchestrator.flows import (
    named_session_flow,
    named_session_streaming,
    normal,
    normal_streaming,
)
from ductor_bot.session.key import SessionKey
from ductor_bot.text.response_format import recovery_notification_text
from ductor_bot.workspace.loader import PERSONA_UNSET_MARKER

HINT = "FIRST-RUN SETUP"


def _unset(orch) -> None:
    (orch.paths.workspace / "PERSONA.md").write_text(
        f"{PERSONA_UNSET_MARKER}\n# Persona\n", encoding="utf-8"
    )


@pytest.mark.parametrize("flow", [normal, normal_streaming])
async def test_inflight_preview_and_recovery_notice_hold_only_the_user_text(orch, flow) -> None:
    _unset(orch)
    seen: dict[str, object] = {}

    async def run(request, *_args, **_kwargs):
        turns = orch.inflight_tracker.load_interrupted(max_age_seconds=3600)
        seen["previews"] = [turn.prompt_preview for turn in turns]
        seen["notice"] = [recovery_notification_text("foreground", t.prompt_preview) for t in turns]
        seen["ai_input"] = request.prompt
        return AgentResponse(result="ok", session_id="synthetic")

    orch._cli_service.execute = AsyncMock(side_effect=run)
    orch._cli_service.execute_streaming = AsyncMock(side_effect=run)
    await flow(orch, SessionKey(chat_id=1), "hi")

    assert seen["previews"] == ["hi"]
    for notice in seen["notice"]:
        assert "hi" in notice
        assert HINT not in notice
        assert "MEMORY CHECK" not in notice
    assert HINT in seen["ai_input"]  # the AI still gets the hint
    assert seen["ai_input"].startswith("hi")


@pytest.mark.parametrize("streaming", [False, True])
async def test_named_session_first_turn_gets_the_hint_but_stores_the_original(
    orch, streaming
) -> None:
    _unset(orch)
    execute = AsyncMock(return_value=AgentResponse(result="reply", session_id="synthetic"))
    orch._cli_service.execute = execute
    orch._cli_service.execute_streaming = execute
    ns = orch._named_sessions.create(1, "claude", "opus", "setup")
    ns.status = "idle"  # a created session starts running its first turn
    key = SessionKey("tg", 1, None)

    await (named_session_streaming if streaming else named_session_flow)(
        orch, key, ns.name, "hello"
    )

    request = execute.await_args.args[0]
    assert request.prompt.startswith("hello")
    assert HINT in request.prompt
    assert orch._named_sessions.get(1, ns.name).last_prompt == "hello"


@pytest.mark.parametrize("streaming", [False, True])
async def test_named_session_hint_follows_the_cadence_and_the_switches(orch, streaming) -> None:
    _unset(orch)
    execute = AsyncMock(return_value=AgentResponse(result="reply", session_id="synthetic"))
    orch._cli_service.execute = execute
    orch._cli_service.execute_streaming = execute
    ns = orch._named_sessions.create(1, "claude", "opus", "setup")
    ns.status = "idle"  # a created session starts running its first turn
    key = SessionKey("tg", 1, None)
    flow = named_session_streaming if streaming else named_session_flow

    flags = []
    for i in range(7):
        await flow(orch, key, ns.name, f"m{i}")
        flags.append(HINT in execute.await_args.args[0].prompt)
    assert flags == [True, False, False, False, False, True, False]  # 1st and 6th message

    orch._config.persona_setup_prompt = False
    ns.message_count = 5  # would be due
    await flow(orch, key, ns.name, "off")
    assert HINT not in execute.await_args.args[0].prompt

    orch._config.persona_setup_prompt = True
    (orch.paths.workspace / "PERSONA.md").write_text("# Persona\nName: Ari\n", encoding="utf-8")
    ns.message_count = 5
    await flow(orch, key, ns.name, "done")
    assert HINT not in execute.await_args.args[0].prompt


@pytest.mark.parametrize(
    ("resume_id", "count", "expected"),
    [("", 0, True), ("sid", 0, True), ("sid", 1, False), ("sid", 5, True), ("sid", 6, False)],
)
async def test_background_named_session_new_and_resumed_turns(
    orch, resume_id: str, count: int, expected: bool
) -> None:
    _unset(orch)
    execute = AsyncMock(return_value=AgentResponse(result="reply", session_id="synthetic"))
    orch._cli_service.execute = execute
    observer = BackgroundObserver(
        orch.paths, timeout_seconds=5, cli_service=orch._cli_service, config=orch._config
    )
    observer.set_result_handler(AsyncMock())
    task = BackgroundTask(
        task_id="t1",
        chat_id=1,
        prompt="work",
        message_id=1,
        thread_id=None,
        transport="tg",
        provider="claude",
        model="opus",
        reasoning_effort="",
        submitted_at=0.0,
        session_name="s1",
        resume_session_id=resume_id,
        message_count=count,
    )

    await observer._run_with_session(task)

    request = execute.await_args.args[0]
    assert request.prompt.startswith("work")
    assert (HINT in request.prompt) is expected

    orch._config.persona_setup_prompt = False
    await observer._run_with_session(task)
    assert HINT not in execute.await_args.args[0].prompt


def test_followup_submission_carries_the_session_message_count(orch) -> None:
    ns = orch._named_sessions.create(1, "claude", "opus", "setup")
    orch._named_sessions.update_after_response(1, ns.name, "sid")
    orch._named_sessions.update_after_response(1, ns.name, "sid")
    captured = {}

    class FakeBackground:
        def submit(self, sub, _exec_config):
            captured["sub"] = sub
            return "task"

    orch._observers.background = FakeBackground()
    orch.submit_named_followup_bg(1, ns.name, "again", message_id=1, thread_id=None)
    assert captured["sub"].message_count == 2
    assert captured["sub"].resume_session_id == "sid"
