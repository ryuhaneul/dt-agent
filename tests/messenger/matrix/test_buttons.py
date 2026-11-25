"""Tests for matrix/buttons.py: reaction-based button replacement."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from ductor_bot.cli.types import AgentResponse
from ductor_bot.config import AgentConfig, MatrixConfig
from ductor_bot.i18n import t
from ductor_bot.messenger.matrix.bot import MatrixBot
from ductor_bot.messenger.matrix.buttons import REACTION_DIGITS, ButtonTracker
from ductor_bot.orchestrator.registry import OrchestratorResult
from ductor_bot.session.key import SessionKey
from tests.orchestrator.conftest import orch, workspace  # noqa: F401


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


_ROOM = "!buttons:example.invalid"
_USER = "@reader:example.invalid"
_BOT = "@bot:example.invalid"
_REPLY = "Named session reply"
_CALLBACK_LABEL = "Continue"


@pytest.fixture
async def matrix_bot(orch, monkeypatch):  # noqa: F811 - imported fixture
    nio = pytest.importorskip("nio")
    client = Mock(user_id=_BOT, room_read_markers=AsyncMock())
    monkeypatch.setattr(nio, "AsyncClient", Mock(return_value=client))
    config = orch._config.model_copy(update={"ductor_home": str(orch.paths.ductor_home)})
    config.matrix.allowed_rooms = [_ROOM]
    config.matrix.allowed_users = [_USER]
    bot = MatrixBot(config)
    bot._orchestrator = orch
    bot._ready = True
    monkeypatch.setattr(bot, "_send_rich", AsyncMock())
    yield bot
    tasks = tuple(bot._background_tasks)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.fixture
def named_button(matrix_bot):
    key = SessionKey.matrix(matrix_bot.id_map.room_to_int(_ROOM))
    sessions = matrix_bot._orch._named_sessions
    session = sessions.create(key.chat_id, "claude", "opus", "Initial turn", key=key)
    sessions.update_after_response(key.chat_id, session.name, "previous-session")
    cli = matrix_bot._orch._cli_service
    response = AgentResponse(result=_REPLY, session_id="next-session")
    cli.execute.return_value = response
    cli.execute_streaming.return_value = response
    return key, session.name, f"ns:{session.name}:{_CALLBACK_LABEL}"


def _text_event(body, event_id):
    from nio import RoomMessageText

    return RoomMessageText(
        {"sender": _USER, "event_id": event_id, "origin_server_ts": 1_700_000_000_000},
        body,
        None,
        None,
    )


async def _press_named_button(bot, data, trigger):
    from nio import MatrixRoom, ReactionEvent

    room = MatrixRoom(_ROOM, _BOT)
    if trigger == "number":
        bot._button_tracker.extract_and_format(_ROOM, f"[button:{data}]")
        await bot._on_message(room, _text_event("1", "$input"))
    else:
        bot._button_tracker.register_buttons(_ROOM, "$buttons", [_CALLBACK_LABEL], [data])
        reaction = ReactionEvent(
            {"sender": _USER, "event_id": "$reaction", "origin_server_ts": 1_700_000_000_000},
            "$buttons",
            REACTION_DIGITS[0],
        )
        await bot._on_reaction(room, reaction)


async def _finish_button_tasks(bot):
    await asyncio.wait_for(asyncio.gather(*tuple(bot._background_tasks)), timeout=2)


@pytest.mark.parametrize("trigger", ["number", "reaction"])
@pytest.mark.parametrize("streaming", [False, True], ids=["nonstreaming", "streaming"])
@pytest.mark.parametrize("abort_scope", ["chat", "topic"])
async def test_named_button_replies_after_stale_abort(
    matrix_bot, named_button, trigger, streaming, abort_scope
):
    bot = matrix_bot
    key, name, data = named_button
    bot.config.streaming.enabled = streaming
    registry = bot._orch._process_registry
    if abort_scope == "chat":
        await registry.kill_all(key.chat_id)
    else:
        registry._aborted_topics.add((key.chat_id, key.topic_id))

    await _press_named_button(bot, data, trigger)
    await _finish_button_tasks(bot)

    cli = bot._orch._cli_service
    execute, unused = (
        (cli.execute_streaming, cli.execute)
        if streaming
        else (
            cli.execute,
            cli.execute_streaming,
        )
    )
    execute.assert_awaited_once()
    unused.assert_not_awaited()
    request = execute.await_args.args[0]
    assert request.resume_session == "previous-session"
    assert request.prompt == _CALLBACK_LABEL
    bot._send_rich.assert_awaited_once_with(_ROOM, f"**[{name} | claude]**\n{_REPLY}")
    assert not registry.was_aborted(key.chat_id)
    assert not registry.was_aborted_topic(key.chat_id, key.topic_id)


@pytest.mark.parametrize("trigger", ["number", "reaction"])
@pytest.mark.parametrize("streaming", [False, True], ids=["nonstreaming", "streaming"])
async def test_ended_named_button_reports_ended_without_cli(
    matrix_bot, named_button, trigger, streaming
):
    bot = matrix_bot
    key, name, data = named_button
    bot.config.streaming.enabled = streaming
    assert bot._orch._named_sessions.end_session(key.chat_id, name)

    await _press_named_button(bot, data, trigger)
    await _finish_button_tasks(bot)

    bot._orch._cli_service.execute.assert_not_awaited()
    bot._orch._cli_service.execute_streaming.assert_not_awaited()
    bot._send_rich.assert_awaited_once_with(_ROOM, t("session.ended", name=name))


async def test_named_button_returns_before_room_lock_is_available(matrix_bot, named_button):
    bot = matrix_bot
    key, name, data = named_button
    bot.config.streaming.enabled = False

    async with bot._lock_pool.get(key.lock_key):
        await asyncio.wait_for(bot._handle_button_callback(_ROOM, "", data), timeout=2)
        await asyncio.sleep(0)
        bot._orch._cli_service.execute.assert_not_awaited()
        bot._send_rich.assert_not_awaited()
        assert bot._message_queue.pending_count(key.chat_id) == 1

    await _finish_button_tasks(bot)
    bot._orch._cli_service.execute.assert_awaited_once()
    bot._send_rich.assert_awaited_once_with(_ROOM, f"**[{name} | claude]**\n{_REPLY}")


async def test_waiting_named_button_preserves_abort_marks_until_admitted(matrix_bot, named_button):
    bot = matrix_bot
    key, _, data = named_button
    registry = bot._orch._process_registry
    bot.config.streaming.enabled = False

    async with bot._lock_pool.get(key.lock_key):
        await registry.kill_all(key.chat_id)
        registry._aborted_topics.add((key.chat_id, key.topic_id))
        await asyncio.wait_for(bot._handle_button_callback(_ROOM, "", data), timeout=2)
        await asyncio.sleep(0)
        assert registry.was_aborted(key.chat_id)
        assert registry.was_aborted_topic(key.chat_id, key.topic_id)
        bot._orch._cli_service.execute.assert_not_awaited()

    await _finish_button_tasks(bot)
    assert not registry.was_aborted(key.chat_id)
    assert not registry.was_aborted_topic(key.chat_id, key.topic_id)
    bot._orch._cli_service.execute.assert_awaited_once()


async def test_stop_cancels_waiting_named_button(matrix_bot, named_button):
    from nio import MatrixRoom

    bot = matrix_bot
    key, _, data = named_button
    bot.config.streaming.enabled = False

    async with bot._lock_pool.get(key.lock_key):
        await asyncio.wait_for(bot._handle_button_callback(_ROOM, "", data), timeout=2)
        await asyncio.sleep(0)
        assert bot._message_queue.pending_count(key.chat_id) == 1
        (task,) = bot._background_tasks

        await asyncio.wait_for(
            bot._on_message(MatrixRoom(_ROOM, _BOT), _text_event("!stop", "$stop")), timeout=2
        )
        await asyncio.sleep(0)
        assert task.cancelled()
        assert bot._message_queue.pending_count(key.chat_id) == 0
        assert bot._orch._process_registry.was_aborted(key.chat_id)

    bot._orch._cli_service.execute.assert_not_awaited()
    bot._send_rich.assert_awaited_once_with(_ROOM, t("abort_all.done", count=1))


@pytest.mark.parametrize("data", ["tsc:list", "ordinary choice"])
async def test_other_button_callbacks_keep_existing_routes(matrix_bot, monkeypatch, data):
    bot = matrix_bot
    handle = AsyncMock(return_value=OrchestratorResult(text="Handled choice"))
    monkeypatch.setattr(bot._orch, "handle_message", handle)
    key = SessionKey.matrix(bot.id_map.room_to_int(_ROOM))

    await bot._handle_button_callback(_ROOM, "$buttons", data)

    if data == "tsc:list":
        handle.assert_not_awaited()
        bot._send_rich.assert_awaited_once_with(_ROOM, t("notice.task_system_unavailable"))
    else:
        handle.assert_awaited_once_with(key, data)
        bot._send_rich.assert_awaited_once_with(_ROOM, "Handled choice")
    assert not bot._background_tasks


@pytest.mark.parametrize(
    ("sender", "room_allowed", "leaving", "group_mention_only", "accepted", "rejected"),
    [
        ("@guest:s", True, False, False, False, True),
        ("@user:s", False, False, False, False, True),
        ("@user:s", True, False, False, True, False),
        ("@bot:s", True, False, False, False, False),
        ("@user:s", True, True, False, False, False),
        ("@user:s", True, False, True, True, False),
        ("@guest:s", True, False, True, True, False),
        ("@user:s", False, False, True, False, True),
    ],
    ids=[
        "user-denied",
        "room-denied",
        "authorized",
        "own-reaction",
        "leaving-room",
        "group-without-mention",
        "group-room-policy",
        "group-room-denied",
    ],
)
async def test_reaction_button_authorization(
    *,
    sender: str,
    room_allowed: bool,
    leaving: bool,
    group_mention_only: bool,
    accepted: bool,
    rejected: bool,
) -> None:
    nio = pytest.importorskip("nio")
    room = nio.MatrixRoom("!room:s", "@bot:s")
    if group_mention_only:
        room.name = "Group"
    allowed_rooms = [room.room_id] if room_allowed else ["!other:s"]
    bot = MatrixBot.__new__(MatrixBot)
    bot._ready = True
    bot._config = AgentConfig(
        group_mention_only=group_mention_only,
        matrix=MatrixConfig(allowed_rooms=allowed_rooms, allowed_users=["@user:s"]),
    )
    bot._client = SimpleNamespace(user_id="@bot:s")
    bot._allowed_rooms_set = set(allowed_rooms)
    bot._leaving_rooms = {room.room_id} if leaving else set()
    bot._rejections = Mock()
    bot._button_tracker = ButtonTracker()
    bot._button_tracker.register_buttons(room.room_id, "$options", ["Choice"], ["cb:choice"])
    callback = AsyncMock()
    bot._handle_button_callback = callback
    event = nio.ReactionEvent.from_dict(
        {
            "sender": sender,
            "event_id": "$reaction",
            "origin_server_ts": 1,
            "type": "m.reaction",
            "content": {
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": "$options",
                    "key": REACTION_DIGITS[0],
                },
            },
        },
    )
    assert isinstance(event, nio.ReactionEvent)

    await bot._on_reaction(room, event)

    if accepted:
        callback.assert_awaited_once_with(room.room_id, "$options", "cb:choice")
        assert bot._button_tracker.match_input(room.room_id, "1") is None
    else:
        callback.assert_not_awaited()
        assert bot._button_tracker.match_input(room.room_id, "1") == "cb:choice"
    if rejected:
        room_kind = "group" if group_mention_only else "private"
        bot._rejections.log.assert_called_once_with(room_kind, room.room_id, sender, None)
    else:
        bot._rejections.log.assert_not_called()
