"""Task questions retain single-transport behavior and select known origins."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from ductor_bot.messenger.matrix.bot import MatrixBot
from ductor_bot.messenger.multi import MultiBotAdapter
from ductor_bot.messenger.slack.bot import SlackBot
from ductor_bot.messenger.telegram.app import TelegramBot
from tests.messenger.test_multi import _make_bot, _make_config


@pytest.mark.parametrize(
    ("bot_class", "expected"), [(TelegramBot, "tg"), (MatrixBot, "mx"), (SlackBot, "sl")]
)
async def test_single_bot_keeps_own_transport_and_destination(bot_class, expected):
    bot = SimpleNamespace(_bus=SimpleNamespace(submit=AsyncMock()))
    await bot_class.on_task_question(bot, "sample-task", "Question", "Work", 42, 9, "api")
    bot._bus.submit.assert_awaited_once()
    envelope = bot._bus.submit.await_args.args[0]
    assert envelope.transport == expected
    assert envelope.chat_id == 42
    assert envelope.topic_id == 9


@pytest.mark.parametrize("transport", ["tg", "mx", "sl", "", "api", "unknown"])
async def test_multi_selects_known_transport_and_otherwise_fans_out(transport):
    bots = [_make_bot(name=name) for name in ("telegram", "matrix", "slack")]
    with patch("ductor_bot.messenger.registry._create_single_bot", side_effect=bots):
        adapter = MultiBotAdapter(_make_config(["telegram", "matrix", "slack"]))
    await adapter.on_task_question("sample-task", "Question", "Work", 42, 9, transport)
    for name, bot in zip(("tg", "mx", "sl"), bots, strict=True):
        if transport not in {"tg", "mx", "sl"} or name == transport:
            bot.on_task_question.assert_awaited_once_with(
                "sample-task", "Question", "Work", 42, 9, transport
            )
        else:
            bot.on_task_question.assert_not_awaited()


async def test_multi_missing_selected_bot_uses_existing_fanout():
    bots = [_make_bot(name=name) for name in ("telegram", "matrix")]
    with patch("ductor_bot.messenger.registry._create_single_bot", side_effect=bots):
        adapter = MultiBotAdapter(_make_config(["telegram", "matrix"]))
    await adapter.on_task_question("sample-task", "Question", "Work", 42, 9, "sl")
    for bot in bots:
        bot.on_task_question.assert_awaited_once_with(
            "sample-task", "Question", "Work", 42, 9, "sl"
        )
