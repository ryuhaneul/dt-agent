"""Usage command routing, allowlists, read-only snapshots, and in-place refresh."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message, User

from ductor_bot.cli.claude_accounts import SlotRegistry, SlotSnapshot, state_file_path
from ductor_bot.cli.claude_usage import UsageStore, usage_file_path
from ductor_bot.cli.codex_usage import CodexUsage, CodexWindow
from ductor_bot.commands import get_bot_commands, get_main_only_commands
from ductor_bot.config import AgentConfig
from ductor_bot.i18n import init, t
from ductor_bot.messenger.telegram import app
from ductor_bot.messenger.telegram.middleware import is_quick_command


@pytest.fixture
async def usage_bot(tmp_path, monkeypatch, request):
    init("en")
    api = MagicMock()
    api.me = AsyncMock(
        return_value=User(id=900, is_bot=True, first_name="Fixture", username="fixture_bot")
    )
    api.send_message = AsyncMock(
        return_value=Message.model_validate(
            {"message_id": 400, "date": 1000, "chat": {"id": 1, "type": "private"}}
        )
    )
    api.edit_message_text = AsyncMock()
    api.edit_message_reply_markup = AsyncMock()
    monkeypatch.setattr(app, "Bot", lambda **kwargs: api)
    agent_name = getattr(request, "param", "main")
    bot_home = tmp_path if agent_name == "main" else tmp_path / "agents" / agent_name
    config = AgentConfig(
        telegram_token="fixture:token",
        allowed_user_ids=[1],
        ductor_home=str(bot_home),
        claude_accounts=["claude-a"],
        user_timezone="UTC",
    )
    bot = app.TelegramBot(config, agent_name=agent_name)
    bot._bot_username = "fixture_bot"
    bot._bot_id = 900
    bot._sequential.set_bot_username("fixture_bot")
    bot._sequential.set_message_received_handler(AsyncMock())
    bot._orchestrator = MagicMock()
    bot._orchestrator.has_command.return_value = False
    bot._orchestrator.paths.ductor_home = tmp_path
    query = AsyncMock(
        return_value=CodexUsage(
            windows=(CodexWindow(used_percent=16, window_minutes=10080, resets_at=1900003600),),
            captured_at=1900000000,
        )
    )
    import ductor_bot.cli.codex_usage as codex

    monkeypatch.setattr(codex, "query_codex_usage", query)
    answer = AsyncMock()
    monkeypatch.setattr(CallbackQuery, "answer", answer)
    usage_path = usage_file_path(tmp_path / "workspace")
    UsageStore(usage_path).record(
        "claude-a",
        {
            "type": "rate_limit_event",
            "rate_limit_info": {
                "status": "allowed",
                "rateLimitType": "five_hour",
                "utilization": 0.3,
                "resetsAt": 1900003600,
            },
        },
        captured_at=1900000000,
    )
    SlotRegistry(state_file_path(tmp_path)).save(SlotSnapshot(cooldowns={"claude-a": 1900003600}))
    command = AsyncMock()
    monkeypatch.setattr(app, "handle_command", command)
    yield SimpleNamespace(
        bot=bot,
        api=api,
        query=query,
        answer=answer,
        usage_path=usage_path,
        home=tmp_path,
        command=command,
    )
    await bot._secret_input.close()
    init("en")


def message(text="/usage", user=1):
    return Message.model_validate(
        {
            "message_id": 200,
            "date": 1000,
            "chat": {"id": 1, "type": "private"},
            "from": {"id": user, "is_bot": False, "first_name": "Fixture"},
            "text": text,
            "entities": [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}],
        }
    )


def fingerprint(path):
    return path.read_bytes(), path.stat().st_mtime_ns


async def dispatch_normal(path, event):
    async def route(msg, data):
        return await path.bot._router.message.trigger(msg, bot=path.api)

    return await path.bot._auth(route, event, {})


@pytest.mark.parametrize("usage_bot", ["main", "helper"], indirect=True)
@pytest.mark.parametrize("text", ["/usage", "/USAGE", "/UsAgE@fixture_bot extra"])
async def test_normal_usage_registered_for_all_bots(usage_bot, text):
    path = usage_bot
    await dispatch_normal(path, message(text))
    path.query.assert_awaited_once()
    path.api.send_message.assert_awaited_once()
    path.api.edit_message_text.assert_awaited_once()
    path.command.assert_not_awaited()
    assert path.api.edit_message_text.call_args.kwargs["message_id"] == 400


@pytest.mark.parametrize("text", ["/usage", "/USAGE@fixture_bot", "/usage arg"])
async def test_quick_usage_bypasses_locked_turn_and_registry(usage_bot, text):
    path = usage_bot
    path.bot._agent_name = "helper"
    assert is_quick_command(text, "fixture_bot")
    normal = AsyncMock()
    lock = path.bot._sequential.get_lock(1)
    await lock.acquire()
    try:

        async def sequential(msg, data):
            return await path.bot._sequential(normal, msg, data)

        await asyncio.wait_for(path.bot._auth(sequential, message(text), {}), 1)
        assert lock.locked()
        normal.assert_not_awaited()
        path.query.assert_awaited_once()
        path.bot._orchestrator.has_command.assert_not_called()
        path.command.assert_not_awaited()
    finally:
        lock.release()


@pytest.mark.parametrize("failure", [None, "timeout", "login_required", "invalid_response"])
async def test_loading_then_same_message_final_and_no_writes(usage_bot, failure):
    path = usage_bot
    entered, proceed = asyncio.Event(), asyncio.Event()
    result = path.query.return_value if failure is None else CodexUsage(error=failure)

    async def query():
        entered.set()
        await proceed.wait()
        return result

    path.query.side_effect = query
    files = [path.usage_path, state_file_path(path.home)]
    before = [fingerprint(p) for p in files]
    task = asyncio.create_task(path.bot._on_usage(message()))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        first = path.api.send_message.call_args.kwargs
        assert first["text"].startswith("<pre>") and first["text"].endswith("</pre>")
        assert "Codex  " + t("usage.loading") in first["text"]
        assert "30%" in first["text"]
        path.api.edit_message_text.assert_not_awaited()
        proceed.set()
        await task
    finally:
        proceed.set()
        await asyncio.gather(task, return_exceptions=True)
    final = path.api.edit_message_text.call_args.kwargs
    assert final["chat_id"] == 1 and final["message_id"] == 400
    assert final["text"].startswith("<pre>") and final["text"].endswith("</pre>")
    assert t("usage.loading") not in final["text"]
    if failure is not None:
        assert t("usage.failed", reason=t("usage." + failure)) in final["text"]
    else:
        assert "16%" in final["text"]
    assert final["reply_markup"].inline_keyboard[0][0].callback_data == "usg:refresh"
    assert [fingerprint(p) for p in files] == before


async def test_missing_usage_store_is_not_created(usage_bot):
    path = usage_bot
    path.usage_path.unlink()
    lock = path.usage_path.with_name(path.usage_path.name + ".lock")
    lock.unlink()
    await path.bot._on_usage(message())
    assert not path.usage_path.exists() and not lock.exists()


@pytest.mark.parametrize("unchanged", [False, True])
async def test_refresh_reloads_snapshot_and_edits_once(usage_bot, unchanged):
    path = usage_bot
    UsageStore(path.usage_path).record(
        "claude-a",
        {
            "type": "rate_limit_event",
            "rate_limit_info": {
                "status": "allowed",
                "rateLimitType": "five_hour",
                "utilization": 0.7,
                "resetsAt": 1900003600,
            },
        },
        captured_at=1900000001,
    )
    before = fingerprint(path.usage_path)
    if unchanged:
        path.api.edit_message_text.side_effect = TelegramBadRequest(
            method=MagicMock(), message="message is not modified"
        )
    callback = CallbackQuery.model_validate(
        {
            "id": "fixture-refresh",
            "chat_instance": "fixture",
            "from": {"id": 1, "is_bot": False, "first_name": "Fixture"},
            "message": message().model_copy(update={"message_id": 400}),
            "data": "usg:refresh",
        }
    )

    async def route(event, data):
        return await path.bot._on_callback_query(event)

    await path.bot._auth(route, callback, {})
    path.answer.assert_awaited_once()
    path.query.assert_awaited_once()
    path.api.send_message.assert_not_awaited()
    path.api.edit_message_text.assert_awaited_once()
    edited = path.api.edit_message_text.call_args.kwargs
    assert edited["message_id"] == 400 and "70%" in edited["text"]
    assert fingerprint(path.usage_path) == before


@pytest.mark.parametrize("callback", [False, True])
async def test_unlisted_users_are_blocked_before_usage(usage_bot, callback):
    path = usage_bot
    event = message(user=2)
    if callback:
        event = CallbackQuery.model_validate(
            {
                "id": "fixture-denied",
                "chat_instance": "fixture",
                "from": {"id": 2, "is_bot": False, "first_name": "Fixture"},
                "message": event,
                "data": "usg:refresh",
            }
        )

    async def route(event, data):
        if callback:
            await path.bot._on_callback_query(event)
        else:
            await path.bot._on_usage(event)

    await path.bot._auth(route, event, {})
    path.query.assert_not_awaited()
    path.api.send_message.assert_not_awaited()
    path.api.edit_message_text.assert_not_awaited()
    path.answer.assert_not_awaited()


@pytest.mark.parametrize("condition", ["other-bot", "mention-only"])
async def test_usage_keeps_mention_filters(usage_bot, condition):
    path = usage_bot
    text = "/usage@another_bot" if condition == "other-bot" else "/usage"
    if condition == "mention-only":
        path.bot._config.group_mention_only = True
        path.bot._is_addressed = MagicMock(return_value=False)
    event = message(text)
    event = event.model_copy(update={"chat": event.chat.model_copy(update={"type": "supergroup"})})
    await path.bot._on_usage(event)
    path.query.assert_not_awaited()
    path.api.send_message.assert_not_awaited()


def test_usage_menu_is_not_main_only():
    assert "usage" in dict(get_bot_commands())
    assert "usage" not in dict(get_main_only_commands())



@pytest.mark.parametrize("after_send", [False, True])
async def test_display_error_before_or_after_initial_message(usage_bot, monkeypatch, after_send):
    from ductor_bot.i18n import t

    path = usage_bot
    original = path.bot._usage_response
    async def response(codex):
        if not after_send or codex is not None:
            raise RecursionError("synthetic-private-value")
        return await original(codex)
    monkeypatch.setattr(path.bot, "_usage_response", response)
    incoming = message().model_copy(update={
        "message_thread_id": 7, "is_topic_message": True,
        "chat": message().chat.model_copy(update={"type": "supergroup", "is_forum": True}),
    })
    await path.bot._on_usage(incoming)
    path.api.send_message.assert_awaited_once()
    if after_send:
        path.api.edit_message_text.assert_awaited_once()
        edited = path.api.edit_message_text.call_args.kwargs
        assert edited["message_id"] == 400
        assert t("usage.error") in edited["text"]
    else:
        path.api.edit_message_text.assert_not_awaited()
        sent = path.api.send_message.call_args.kwargs
        assert sent["text"] == t("usage.error")
        assert sent["reply_parameters"].message_id == incoming.message_id
        assert sent["message_thread_id"] == 7
        path.query.assert_not_awaited()


@pytest.mark.parametrize("throttled", [False, True])
async def test_refresh_handles_query_failure_and_edit_throttling(usage_bot, throttled, caplog):
    from aiogram.exceptions import TelegramRetryAfter
    from ductor_bot.i18n import t

    path = usage_bot
    if throttled:
        path.api.edit_message_text.side_effect = TelegramRetryAfter(
            method=MagicMock(), message="synthetic", retry_after=1)
    else:
        path.query.side_effect = RuntimeError("synthetic-private-value")
    await path.bot._handle_usage_refresh(1, 400)
    path.api.send_message.assert_not_awaited()
    path.api.edit_message_text.assert_awaited_once()
    if not throttled:
        assert t("usage.error") in path.api.edit_message_text.call_args.kwargs["text"]
    assert "synthetic-private-value" not in caplog.text
