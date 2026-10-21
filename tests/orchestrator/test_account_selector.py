"""Account selector actions, fresh-file rendering, and Telegram access boundaries."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from aiogram.types import CallbackQuery, Message

from ductor_bot.cli import claude_slots
from ductor_bot.cli.claude_accounts import SlotRegistry, SlotSnapshot, state_file_path
from ductor_bot.config import AgentConfig
from ductor_bot.i18n import init, t
from ductor_bot.session.key import SessionKey

NOW = 1800000000.0


@pytest.fixture
def page(tmp_path, monkeypatch):
    from ductor_bot.orchestrator.selectors import account_selector as selector

    init("en")
    config_path = tmp_path / "config" / "config.json"
    config_path.parent.mkdir()
    config_path.write_text(
        json.dumps({"claude_accounts": ["claude-a", "claude-b", "claude-c"], "model": "opus"})
    )
    registry = SlotRegistry(state_file_path(tmp_path))
    registry.save(SlotSnapshot(cooldowns={"claude-a": NOW + 3720, "claude-c": NOW - 1}))
    (tmp_path / ".cli-auth" / "claude-a").mkdir()
    (tmp_path / ".cli-auth" / "claude-a" / ".credentials.json").write_text("{}")
    clock = SimpleNamespace(time=lambda: NOW)
    monkeypatch.setattr(selector, "time", clock)
    monkeypatch.setattr(claude_slots, "time", clock)
    config = AgentConfig(claude_accounts=["stale-memory"], user_timezone="Asia/Seoul")
    yield selector, tmp_path, config
    init("en")


def _names(home):
    return json.loads((home / "config" / "config.json").read_text())["claude_accounts"]


def _data(response):
    return [button.callback_data for row in response.buttons.rows for button in row]


def _fingerprint(home):
    paths = [home / "config" / "config.json", state_file_path(home)]
    return [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths]


async def test_page_reads_files_and_formats_timezone_and_controls(page):
    selector, home, config = page
    response = await selector.account_selector_start(home, config)
    expected_time = datetime.fromtimestamp(NOW + 3720, ZoneInfo("Asia/Seoul")).strftime("%H:%M")
    assert "1. claude-a  Authenticated" in response.text
    assert "1h 2m" in response.text
    assert expected_time in response.text
    assert "2. claude-b  Not authenticated · No cooldown" in response.text
    assert "3. claude-c  Not authenticated · No cooldown" in response.text
    assert "stale-memory" not in response.text
    assert config.claude_accounts == ["stale-memory"]
    data = _data(response)
    assert "acs:u:claude-a" not in data
    assert "acs:p:claude-a" not in data
    assert "acs:d:claude-c" not in data
    assert "acs:c:claude-a" in data
    assert "acs:c:claude-c" not in data
    assert data[-1] == "acs:r"
    assert t("accounts.subagent_note") in response.text


@pytest.mark.parametrize("language", ["en", "de", "nl", "es", "fr", "id", "pt", "ru", "ko"])
async def test_all_locales_and_empty_list_have_refresh_and_notice(page, language):
    selector, home, config = page
    init(language)
    response = await selector.account_selector_start(home, config)
    assert t("accounts.title") in response.text
    assert t("accounts.subagent_note") in response.text
    assert "accounts." not in response.text
    path = home / "config" / "config.json"
    path.write_text('{"claude_accounts": []}')
    response = await selector.account_selector_start(home, config)
    assert t("accounts.empty") in response.text
    assert _data(response) == ["acs:r"]


@pytest.mark.parametrize(
    ("action", "method", "extra"),
    [
        ("acs:p:claude-b", "prefer", ()),
        ("acs:u:claude-b", "move", (-1,)),
        ("acs:d:claude-b", "move", (1,)),
        ("acs:c:claude-a", "clear_cooldown", ()),
    ],
)
async def test_actions_delegate_to_shared_rules_off_event_loop(
    page, monkeypatch, action, method, extra
):
    selector, home, config = page
    real = getattr(claude_slots, method)
    calls = []

    def record(*args):
        calls.append(args)
        assert threading.current_thread() is not threading.main_thread()
        return real(*args)

    monkeypatch.setattr(claude_slots, method, record)
    response = await selector.handle_account_callback(home, config, action)
    name = action.split(":", 2)[2]
    assert calls == [(home, name, *extra)]
    current = AgentConfig(claude_accounts=_names(home))
    for index, slot in enumerate(claude_slots.overview(home, current)["slots"], 1):
        assert f"{index}. {slot['name']}" in response.text
    if method == "prefer" or extra == (-1,):
        assert _names(home)[0] == "claude-b"
    if extra == (1,):
        assert _names(home)[-1] == "claude-b"
    if method == "clear_cooldown":
        snapshot = SlotRegistry(state_file_path(home)).load()
        assert "claude-a" not in snapshot.cooldowns
        assert snapshot.cooldowns["claude-c"] == NOW - 1


@pytest.mark.parametrize("action", ["p", "c"])
async def test_stale_buttons_do_not_write_and_render_current_order(page, action):
    selector, home, config = page
    path = home / "config" / "config.json"
    path.write_text(json.dumps({"claude_accounts": ["claude-c", "claude-b"]}))
    before = _fingerprint(home)
    response = await selector.handle_account_callback(home, config, f"acs:{action}:claude-a")
    assert _fingerprint(home) == before
    assert t("accounts.stale") in response.text
    assert "1. claude-c" in response.text
    response = await selector.handle_account_callback(home, config, "acs:u:claude-b")
    assert _names(home) == ["claude-b", "claude-c"]
    assert "1. claude-b" in response.text


@pytest.mark.parametrize("name", ["x" * 58, "x" * 59, "\u00e9" * 29, "\u00e9" * 30])
async def test_callback_length_uses_bytes_and_never_uses_positions(page, name):
    selector, home, config = page
    path = home / "config" / "config.json"
    path.write_text(json.dumps({"claude_accounts": ["claude-a", name]}))
    response = await selector.account_selector_start(home, config)
    data = _data(response)
    assert all(len(value.encode("utf-8")) <= 64 for value in data)
    assert (f"acs:p:{name}" in data) is (len(name.encode("utf-8")) <= 58)
    assert selector.is_account_selector_callback("acs:r")
    assert not selector.is_account_selector_callback("tsc:r")


async def test_refresh_and_invalid_data_do_not_write(page):
    selector, home, config = page
    before = _fingerprint(home)
    for data in ["acs:r", "acs:unknown:claude-a", "acs:p:" + "x" * 59]:
        await selector.handle_account_callback(home, config, data)
        assert _fingerprint(home) == before


async def test_reload_updates_main_but_not_the_subagent_copy(page):
    from ductor_bot.config_reload import ConfigReloader
    from ductor_bot.multiagent.models import SubAgentConfig, merge_sub_agent_config

    selector, home, _config = page
    config = AgentConfig(ductor_home=str(home), claude_accounts=_names(home))
    child = merge_sub_agent_config(
        config, SubAgentConfig(name="helper", transport="headless"), home / "agents" / "helper"
    )
    path = home / "config" / "config.json"
    reloader = ConfigReloader(path, config)
    old_time = path.stat().st_mtime
    await selector.handle_account_callback(home, config, "acs:p:claude-c")
    os.utime(path, (old_time + 2, old_time + 2))
    await reloader._check()
    assert config.claude_accounts == ["claude-c", "claude-a", "claude-b"]
    assert child.claude_accounts == ["claude-a", "claude-b", "claude-c"]


@pytest.fixture
def telegram(page, monkeypatch):
    from ductor_bot.messenger.telegram import app

    selector, home, config = page
    fake = MagicMock()
    fake.get_my_commands = AsyncMock(return_value=[])
    fake.set_my_commands = AsyncMock()
    fake.delete_my_commands = AsyncMock()
    monkeypatch.setattr(app, "Bot", lambda **kwargs: fake)
    sent = AsyncMock()
    edited = AsyncMock()
    monkeypatch.setattr(app, "send_rich", sent)
    monkeypatch.setattr(app, "edit_selector_response", edited)

    def create(name):
        options = config.model_copy(update={"ductor_home": str(home), "allowed_user_ids": [1]})
        bot = app.TelegramBot(options, agent_name=name)
        bot._orchestrator = SimpleNamespace(paths=SimpleNamespace(ductor_home=home))
        return bot

    return create, fake, sent, edited


def _message(user=1):
    return Message.model_validate(
        {
            "message_id": 10,
            "date": 1000,
            "chat": {"id": 1, "type": "private"},
            "from": {"id": user, "is_bot": False, "first_name": "Fixture"},
            "text": "/accounts",
        }
    )


@pytest.mark.parametrize("agent_name", ["main", "helper"])
async def test_telegram_menu_registration_and_direct_command(telegram, page, agent_name):
    create, fake, sent, edited = telegram
    _selector, home, _config = page
    bot = create(agent_name)
    assert any(
        handler.callback.__name__ == "_on_accounts" for handler in bot._router.message.handlers
    )
    await bot._sync_commands()
    commands = fake.set_my_commands.call_args.args[0]
    assert ("accounts" in [command.command for command in commands]) is (agent_name == "main")
    before = _fingerprint(home)
    await bot._on_accounts(_message())
    assert _fingerprint(home) == before
    text = sent.call_args.args[2]
    assert (t("accounts.main_only") in text) is (agent_name != "main")
    if agent_name == "main":
        assert "1. claude-a" in text
        assert sent.call_args.args[3].reply_markup is not None


@pytest.mark.parametrize("agent_name", ["main", "helper"])
async def test_telegram_callback_checks_main_before_writing(telegram, page, agent_name):
    create, fake, sent, edited = telegram
    _selector, home, _config = page
    bot = create(agent_name)
    before = _fingerprint(home)
    assert await bot._route_special_callback(SessionKey(chat_id=1), 10, "acs:p:claude-c")
    response = edited.call_args.args[3]
    if agent_name == "main":
        assert _names(home)[0] == "claude-c"
        assert "1. claude-c" in response.text
    else:
        assert _fingerprint(home) == before
        assert response.text == t("accounts.main_only")
        assert response.buttons is None


@pytest.mark.parametrize("kind", ["message", "callback"])
async def test_existing_auth_middleware_blocks_account_entrypoints(telegram, page, kind):
    create, fake, sent, edited = telegram
    _selector, home, _config = page
    bot = create("main")
    assert bot._auth in bot._router.message.outer_middleware
    assert bot._auth in bot._router.callback_query.outer_middleware
    message = _message(user=2)
    before = _fingerprint(home)
    if kind == "message":
        event = message

        async def receive(event, data):
            await bot._on_accounts(event)
    else:
        event = CallbackQuery.model_validate(
            {
                "id": "fixture",
                "from": {"id": 2, "is_bot": False, "first_name": "Fixture"},
                "chat_instance": "fixture",
                "message": message.model_dump(by_alias=True),
                "data": "acs:p:claude-c",
            }
        )

        async def receive(event, data):
            await bot._route_special_callback(SessionKey(chat_id=1), 10, event.data)

    handler = AsyncMock(side_effect=receive)
    await bot._auth(handler, event, {})
    handler.assert_not_awaited()
    sent.assert_not_awaited()
    edited.assert_not_awaited()
    assert _fingerprint(home) == before


def test_account_command_is_only_in_main_command_definitions():
    from ductor_bot.commands import get_bot_commands, get_main_only_commands

    assert "accounts" in dict(get_main_only_commands())
    assert "accounts" not in dict(get_bot_commands())


@pytest.mark.parametrize("entry", ["start", "refresh", "action"])
@pytest.mark.parametrize("failure", ["lock", "config"])
async def test_selector_failures_return_localized_error(page, monkeypatch, entry, failure):
    selector, home, config = page
    if failure == "config":
        (home / "config" / "config.json").write_text("{broken")
    elif entry == "action":
        monkeypatch.setattr(claude_slots, "prefer", MagicMock(side_effect=OSError("private detail")))
    else:
        monkeypatch.setattr(claude_slots, "overview", MagicMock(side_effect=OSError("private detail")))
    if entry == "start":
        response = await selector.account_selector_start(home, config)
    else:
        data = "acs:r" if entry == "refresh" else "acs:p:claude-b"
        response = await selector.handle_account_callback(home, config, data)
    assert response.text == t("accounts.error")
    assert "private detail" not in response.text
    assert response.buttons is None


@pytest.mark.parametrize("agent_name", ["main", "helper"])
@pytest.mark.parametrize("text", [
    "/accounts", "/ACCOUNTS", "/accounts@fixture_bot",
    "/ACCOUNTS@FIXTURE_BOT ignored", "/accounts ignored", "  /accounts\tignored  ",
])
async def test_quick_accounts_uses_dedicated_handler(telegram, monkeypatch, agent_name, text):
    from ductor_bot.messenger.telegram import app
    from ductor_bot.messenger.telegram.middleware import is_quick_command

    create, _fake, sent, _edited = telegram
    bot = create(agent_name)
    bot._bot_username = "fixture_bot"
    handler = AsyncMock(wraps=bot._on_accounts)
    monkeypatch.setattr(bot, "_on_accounts", handler)
    orchestrator_command = AsyncMock()
    monkeypatch.setattr(app, "handle_command", orchestrator_command)
    message = _message().model_copy(update={"text": text})
    assert is_quick_command(text, bot._bot_username)
    assert await bot._on_quick_command(message.chat.id, message)
    handler.assert_awaited_once_with(message)
    orchestrator_command.assert_not_awaited()
    sent.assert_awaited_once()
    shown = sent.call_args.args[2]
    if agent_name == "main":
        assert t("accounts.title") in shown
        assert "1. claude-a" in shown
        assert sent.call_args.args[3].reply_markup is not None
    else:
        assert shown == t("accounts.main_only")
        assert sent.call_args.args[3].reply_markup is None


@pytest.mark.parametrize("text", ["", "/accounts_more", "/accounting"])
async def test_direct_accounts_requires_exact_command_token(telegram, monkeypatch, text):
    create, _fake, _sent, _edited = telegram
    bot = create("main")
    handler = AsyncMock()
    monkeypatch.setattr(bot, "_on_accounts", handler)
    assert await bot._dispatch_direct_command(1, _message(), text) is None
    handler.assert_not_awaited()



async def test_usage_line_below_each_slot_with_missing_and_expired_values(page):
    from ductor_bot.cli.claude_usage import UsageStore, usage_file_path
    from ductor_bot.orchestrator.selectors.usage_view import render_account_usage

    selector, home, config = page
    store = UsageStore(usage_file_path(home / "workspace"))
    for name, reset in [("claude-a", int(NOW + 3600)), ("claude-c", int(NOW - 1))]:
        store.record(name, {"type":"rate_limit_event", "rate_limit_info":{
            "status":"allowed", "rateLimitType":"five_hour", "utilization":0.11,
            "resetsAt":reset}}, captured_at=int(NOW - 60))
    response = await selector.account_selector_start(home, config)
    lines = response.text.splitlines()
    usages = store.load()
    for index, name in enumerate(["claude-a", "claude-b", "claude-c"], 1):
        row = next(i for i,line in enumerate(lines) if line.startswith(f"{index}. {name}"))
        assert lines[row + 1] == "   " + render_account_usage(usages.get(name), config, now=NOW)



@pytest.mark.parametrize("unexpected", [False, True])
async def test_bad_usage_does_not_remove_account_controls(page, monkeypatch, unexpected):
    from ductor_bot.cli.claude_usage import UsageStore
    from tests.cli.test_claude_usage import ALLOWED

    selector, home, config = page
    event = json.loads(json.dumps(ALLOWED))
    event["rate_limit_info"]["unifiedWindows"]["five_hour"]["resetsAt"] = 10**13
    UsageStore(selector.usage_file_path(home / "workspace")).record("claude-a", event, captured_at=int(NOW))
    if unexpected:
        original = selector.render_account_usage
        def render(usage, *args, **kwargs):
            if usage is not None:
                raise RuntimeError("synthetic")
            return original(usage, *args, **kwargs)
        monkeypatch.setattr(selector, "render_account_usage", render)
    response = await selector.account_selector_start(home, config)
    assert "1. claude-a" in response.text
    assert t("accounts.error") not in response.text
    assert "acs:c:claude-a" in _data(response)
    assert "acs:r" in _data(response)
    assert (t("usage.account_empty") if unexpected else "5h 11% · ?") in response.text
