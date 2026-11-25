"""Tests for matrix/buttons.py: reaction-based button replacement."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from ductor_bot.bus.lock_pool import LockPool
from ductor_bot.cli.types import AgentResponse
from ductor_bot.messenger.matrix.bot import MatrixBot
from ductor_bot.messenger.matrix.buttons import REACTION_DIGITS, ButtonTracker
from ductor_bot.session.key import SessionKey
from tests.cli.test_process_registry import _mock_process
from tests.orchestrator.conftest import orch as orch  # noqa: PLC0414 -- pytest fixture
from tests.orchestrator.conftest import workspace as workspace  # noqa: PLC0414 -- pytest fixture


class TestButtonTracker:
    def test_extract_single_button(self) -> None:
        bt = ButtonTracker()
        result = bt.extract_and_format("!room:s", "Pick one [button:Yes] [button:No]")
        assert "Yes" in result
        assert "No" in result
        assert REACTION_DIGITS[0] in result
        assert REACTION_DIGITS[1] in result
        assert "[button:" not in result

    def test_no_buttons_returns_unchanged(self) -> None:
        bt = ButtonTracker()
        text = "Just regular text"
        assert bt.extract_and_format("!room:s", text) == text

    def test_match_input_returns_callback_data(self) -> None:
        bt = ButtonTracker()
        bt.extract_and_format("!room:s", "Choose [button:Alpha] [button:Beta]")
        # For [button:] markers, callback_data == label
        assert bt.match_input("!room:s", "1") == "Alpha"

    def test_match_input_second_option(self) -> None:
        bt = ButtonTracker()
        bt.extract_and_format("!room:s", "Choose [button:A] [button:B] [button:C]")
        assert bt.match_input("!room:s", "3") == "C"

    def test_match_consumes_buttons(self) -> None:
        bt = ButtonTracker()
        bt.extract_and_format("!room:s", "Choose [button:A] [button:B]")
        bt.match_input("!room:s", "1")
        # After consumption, no match
        assert bt.match_input("!room:s", "2") is None

    def test_match_invalid_number(self) -> None:
        bt = ButtonTracker()
        bt.extract_and_format("!room:s", "Choose [button:A]")
        assert bt.match_input("!room:s", "5") is None

    def test_match_non_numeric(self) -> None:
        bt = ButtonTracker()
        bt.extract_and_format("!room:s", "Choose [button:A]")
        assert bt.match_input("!room:s", "hello") is None

    def test_match_no_active_buttons(self) -> None:
        bt = ButtonTracker()
        assert bt.match_input("!room:s", "1") is None

    def test_clear_removes_buttons(self) -> None:
        bt = ButtonTracker()
        bt.extract_and_format("!room:s", "Choose [button:A]")
        bt.clear("!room:s")
        assert bt.match_input("!room:s", "1") is None

    def test_different_rooms_isolated(self) -> None:
        bt = ButtonTracker()
        bt.extract_and_format("!r1:s", "Q [button:X]")
        bt.extract_and_format("!r2:s", "Q [button:Y]")
        assert bt.match_input("!r1:s", "1") == "X"
        assert bt.match_input("!r2:s", "1") == "Y"


class TestRegisterButtons:
    def test_register_and_match_reaction(self) -> None:
        bt = ButtonTracker()
        bt.register_buttons(
            "!room:s",
            "$evt1",
            labels=["CLAUDE", "CODEX"],
            callback_data=["ms:p:claude", "ms:p:codex"],
        )
        assert bt.match_reaction("!room:s", "$evt1", REACTION_DIGITS[0]) == "ms:p:claude"

    def test_reaction_second_option(self) -> None:
        bt = ButtonTracker()
        bt.register_buttons(
            "!room:s",
            "$evt1",
            labels=["CLAUDE", "CODEX", "GEMINI"],
            callback_data=["ms:p:claude", "ms:p:codex", "ms:p:gemini"],
        )
        assert bt.match_reaction("!room:s", "$evt1", REACTION_DIGITS[2]) == "ms:p:gemini"

    def test_reaction_wrong_event_id(self) -> None:
        bt = ButtonTracker()
        bt.register_buttons(
            "!room:s",
            "$evt1",
            labels=["A"],
            callback_data=["cb:a"],
        )
        assert bt.match_reaction("!room:s", "$wrong", REACTION_DIGITS[0]) is None

    def test_reaction_wrong_emoji(self) -> None:
        bt = ButtonTracker()
        bt.register_buttons(
            "!room:s",
            "$evt1",
            labels=["A"],
            callback_data=["cb:a"],
        )
        assert bt.match_reaction("!room:s", "$evt1", "👍") is None

    def test_reaction_consumes_buttons(self) -> None:
        bt = ButtonTracker()
        bt.register_buttons(
            "!room:s",
            "$evt1",
            labels=["A", "B"],
            callback_data=["cb:a", "cb:b"],
        )
        bt.match_reaction("!room:s", "$evt1", REACTION_DIGITS[0])
        assert bt.match_reaction("!room:s", "$evt1", REACTION_DIGITS[1]) is None

    def test_reaction_wrong_room(self) -> None:
        bt = ButtonTracker()
        bt.register_buttons(
            "!room:s",
            "$evt1",
            labels=["A"],
            callback_data=["cb:a"],
        )
        assert bt.match_reaction("!other:s", "$evt1", REACTION_DIGITS[0]) is None

    def test_text_input_fallback_with_registered_buttons(self) -> None:
        """Typed number still works for registered buttons."""
        bt = ButtonTracker()
        bt.register_buttons(
            "!room:s",
            "$evt1",
            labels=["CLAUDE", "CODEX"],
            callback_data=["ms:p:claude", "ms:p:codex"],
        )
        assert bt.match_input("!room:s", "2") == "ms:p:codex"


@pytest.mark.parametrize(
    ("streaming", "quick_status", "press_while_stopping"),
    [
        (True, True, False),
        (False, True, False),
        (True, False, False),
        (False, False, False),
        (True, False, True),
    ],
    ids=["stream-status", "plain-status", "stream-stop", "plain-stop", "wait-for-turn"],
)
async def test_named_button_after_stop(
    orch, monkeypatch, streaming, quick_status, press_while_stopping
):
    key = SessionKey.matrix(1)
    room_id = "!room:s"
    bot = MatrixBot.__new__(MatrixBot)
    bot._orchestrator = orch
    bot._config = orch.config
    bot._config.streaming.enabled = streaming
    bot._lock_pool = LockPool()
    bot._send_rich = AsyncMock()
    registry = orch._process_registry
    old_started, release_old = asyncio.Event(), asyncio.Event()
    named_cli = AsyncMock(
        return_value=AgentResponse(result="Named reply", session_id="continued-session")
    )

    async def execute(request, **callbacks):
        if request.session_kind == "named":
            return await named_cli(request, **callbacks)
        registry.register(key.chat_id, _mock_process(), "main", topic_id=key.topic_id)
        old_started.set()
        await release_old.wait()
        return AgentResponse(result="Stopped reply")

    async def dispatch(key, text, _room_id, _event):
        result = await orch.handle_message(key, text)
        assert result.text == ""
        assert registry.was_aborted(key.chat_id)

    bot._dispatch_message = dispatch
    orch._cli_service.execute.side_effect = execute
    orch._cli_service.execute_streaming.side_effect = execute
    monkeypatch.setattr(
        "ductor_bot.cli.process_registry._kill_processes", AsyncMock(return_value=1)
    )
    monkeypatch.setattr(
        "ductor_bot.orchestrator.flows._user_auth_warning", AsyncMock(return_value=None)
    )
    monkeypatch.setattr("ductor_bot.orchestrator.commands.check_all_auth", dict)

    previous = asyncio.create_task(bot._dispatch_with_lock(key, "previous", room_id, object()))
    pressed = None
    try:
        await asyncio.wait_for(old_started.wait(), 2)
        assert await orch.abort(key.chat_id) == 1
        session = orch._named_sessions.create(key.chat_id, "claude", "opus", "setup", key=key)
        orch._named_sessions.update_after_response(key.chat_id, session.name, "existing-session")
        data = f"ns:{session.name}:Yes"
        if press_while_stopping:
            lock = bot._lock_pool.get(key.lock_key)
            assert lock.locked()
            waiting = asyncio.Event()
            acquire = lock.acquire

            async def observe_acquire():
                waiting.set()
                return await acquire()

            monkeypatch.setattr(lock, "acquire", observe_acquire)
            pressed = asyncio.create_task(bot._handle_ns_callback(room_id, key, data))
            await asyncio.wait_for(waiting.wait(), 2)
            assert registry.was_aborted(key.chat_id)
            named_cli.assert_not_awaited()
    finally:
        release_old.set()
        await asyncio.wait_for(previous, 2)
        if pressed is not None:
            await asyncio.wait_for(pressed, 2)

    if quick_status:
        status = await orch.handle_message(key, "/status")
        assert status.text
        assert registry.was_aborted(key.chat_id)
    if pressed is None:
        await bot._handle_ns_callback(room_id, key, data)

    named_cli.assert_awaited_once()
    bot._send_rich.assert_awaited_once_with(room_id, f"**[{session.name} | claude]**\nNamed reply")
    assert orch._named_sessions.get(key.chat_id, session.name).session_id == "continued-session"
