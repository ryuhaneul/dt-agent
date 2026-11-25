"""Tests for Matrix button tracking and reaction handling."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from ductor_bot.config import AgentConfig, MatrixConfig
from ductor_bot.messenger.matrix.bot import MatrixBot
from ductor_bot.messenger.matrix.buttons import REACTION_DIGITS, ButtonTracker


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
