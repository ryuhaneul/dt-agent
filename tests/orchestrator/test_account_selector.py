"""Account selector actions, fresh-file rendering, and Telegram access boundaries."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock
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
    assert t("accounts.unused") in response.text
    assert _data(response) == ["acs:l:claude-a", "acs:x:claude-a", "acs:r"]
    (home / ".cli-auth" / "claude-a" / ".credentials.json").unlink()
    (home / ".cli-auth" / "claude-a").rmdir()
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


@pytest.mark.parametrize("language", ["en", "de", "nl", "es", "fr", "id", "pt", "ru", "ko"])
async def test_expired_slot_is_visible_on_accounts_page(page, language):
    from ductor_bot.cli.slot_rotation import record_auth_failure

    selector, home, config = page
    registry = SlotRegistry(state_file_path(home))
    await record_auth_failure(registry, "claude-a", registry.credential_mtimes("claude-a"))
    init(language)
    response = await selector.account_selector_start(home, config)
    assert f"1. claude-a  {t('accounts.auth_expired')}" in response.text
    assert "accounts.auth_expired" not in response.text


@pytest.mark.parametrize("language", ["en", "de", "nl", "es", "fr", "id", "pt", "ru", "ko"])
async def test_api_key_account_has_label_without_usage_or_login_warning(page, language):
    selector, home, config = page
    init(language)
    (home / "config" / "config.json").write_text('{"claude_accounts": ["claude-api"]}')
    slot = home / ".cli-auth" / "claude-api"
    slot.mkdir()
    (slot / "settings.json").write_text('{"apiKeyHelper": "helper.sh"}')
    registry = SlotRegistry(state_file_path(home))
    registry.state_path.write_text(
        json.dumps(
            {
                "auth_failures": {"claude-api": registry.credential_mtimes("claude-api")},
            }
        )
    )
    response = await selector.account_selector_start(home, config)
    api_row, next_row = response.text.splitlines()[1:3]
    assert api_row == f"1. claude-api  {t('accounts.api_key')} · {t('accounts.no_cooldown')}"
    assert next_row.startswith("2. claude-a  ")
    for key in ("accounts.unauthenticated", "accounts.auth_expired", "usage.account_empty"):
        assert t(key) not in api_row


async def test_delete_confirmation_cancel_and_recreated_slot(page):
    selector, home, config = page
    folder = home / ".cli-auth" / "claude-a"
    confirmed = await selector.handle_account_callback(home, config, "acs:x:claude-a")
    cancel, delete = _data(confirmed)
    assert t("accounts.delete_confirm", name="claude-a") == confirmed.text
    await selector.handle_account_callback(home, config, cancel)
    await selector.handle_account_callback(home, config, delete)
    assert folder.is_dir()
    assert "claude-a" in _names(home)

    old = await selector.handle_account_callback(home, config, "acs:x:claude-a")
    current = await selector.handle_account_callback(home, config, "acs:x:claude-a")
    result = await selector.handle_account_callback(home, config, _data(current)[1])
    assert t("accounts.deleted", name="claude-a") in result.text
    assert "claude-a" not in _names(home)
    assert not folder.exists()

    folder.mkdir()
    result = await selector.handle_account_callback(home, config, _data(old)[1])
    assert t("accounts.stale") in result.text
    assert t("accounts.unused") in result.text
    assert folder.is_dir()


async def test_long_unregistered_slot_has_short_delete_controls(page):
    selector, home, config = page
    name = "claude-" + "x" * 120
    folder = home / ".cli-auth" / name
    folder.mkdir()
    (home / ".cli-auth" / "claude").mkdir()
    result = await selector.account_selector_start(home, config)
    delete = next(
        button.callback_data for row in result.buttons.rows for button in row
        if button.text == t("accounts.btn_delete", name=name)
    )
    assert t("accounts.unused") in result.text
    assert "acs:x:claude" not in _data(result)
    confirmed = await selector.handle_account_callback(home, config, delete)
    for data in [delete, *_data(confirmed)]:
        assert len(data.encode("utf-8")) <= 64
    result = await selector.handle_account_callback(home, config, _data(confirmed)[1])
    assert t("accounts.deleted", name=name) in result.text
    assert not folder.exists()


async def test_delete_confirmation_reports_cooldown_state_error(page, monkeypatch):
    from pathlib import Path

    selector, home, config = page
    confirmation = await selector.handle_account_callback(home, config, "acs:x:claude-a")
    state = state_file_path(home)
    saved = state.read_bytes()
    read_text = Path.read_text

    def deny_read(path, *args, **kwargs):
        if path == state:
            raise PermissionError(13, "Permission denied", path)
        return read_text(path, *args, **kwargs)

    with monkeypatch.context() as patcher:
        patcher.setattr(Path, "read_text", deny_read)
        result = await selector.handle_account_callback(home, config, _data(confirmation)[1])
    assert result.text == t("accounts.error")
    assert state.read_bytes() == saved
    assert "claude-a" in _names(home)
    assert (home / ".cli-auth" / "claude-a").is_dir()

    confirmation = await selector.handle_account_callback(home, config, "acs:x:claude-a")
    result = await selector.handle_account_callback(home, config, _data(confirmation)[1])
    assert t("accounts.deleted", name="claude-a") in result.text
    assert "claude-a" not in _names(home)


def _prepare_slot(home, slot, kind):
    folder = home / ".cli-auth" / slot
    folder.mkdir(exist_ok=True)
    if kind == "oauth":
        (folder / ".credentials.json").write_text("{}")
    elif kind == "api_key":
        (folder / "settings.json").write_text('{"apiKeyHelper": "helper.sh"}')
    elif kind == "api_key_detached":
        (folder / "api-key-helper.detached").write_text("helper.sh\n")
    return folder


@pytest.mark.parametrize(
    ("default_kind", "default_controls"), [("oauth", {"l"}), ("signed_out", set())]
)
async def test_slot_buttons_follow_credential_state(page, default_kind, default_controls):
    selector, home, config = page
    accounts = {
        "claude": (default_kind, default_controls),
        "claude-a": ("oauth", {"l", "x"}),
        "claude-b": ("signed_out", {"x"}),
        "claude-credit": ("api_key", {"k", "x"}),
        "claude-detached": ("api_key_detached", {"x"}),
    }
    (home / "config" / "config.json").write_text(json.dumps({"claude_accounts": list(accounts)}))
    for name, (kind, _) in accounts.items():
        _prepare_slot(home, name, kind)

    response = await selector.account_selector_start(home, config)
    actions = set(_data(response))
    for name, (_, expected) in accounts.items():
        assert {code for code in ("l", "k", "x") if f"acs:{code}:{name}" in actions} == expected
    assert t("accounts.api_key_detached") in response.text


@pytest.mark.parametrize(
    ("action", "code", "slot", "kind", "method"),
    [
        ("logout", "l", "claude", "oauth", "logout"),
        ("logout", "l", "claude-a", "oauth", "logout"),
        ("disconnect", "k", "claude-credit", "api_key", "disconnect_api_key"),
    ],
)
async def test_slot_auth_confirmation_cancel_and_success(
    page, monkeypatch, *, action, code, slot, kind, method
):
    selector, home, config = page
    folder = _prepare_slot(home, slot, kind)
    real_operation = getattr(claude_slots, method)
    calls = []

    def operation(ductor_home, name, *, expected):
        assert threading.current_thread() is not threading.main_thread()
        calls.append((ductor_home, name))
        real_operation(ductor_home, name, expected=expected)

    def fake_logout(argv, **_kwargs):
        assert argv == ("claude", "auth", "logout")
        (folder / ".credentials.json").unlink()
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(claude_slots, method, operation)
    monkeypatch.setattr(claude_slots.subprocess, "run", fake_logout)
    opening = f"acs:{code}:{slot}"
    confirmation = await selector.handle_account_callback(home, config, opening)
    assert confirmation.text == t(f"accounts.{action}_confirm", name=slot)
    buttons = confirmation.buttons.rows[0]
    assert [button.text for button in buttons] == [
        t("accounts.btn_cancel_delete"), t(f"accounts.btn_confirm_{action}")
    ]
    assert calls == []
    cancel, confirm = _data(confirmation)
    await selector.handle_account_callback(home, config, cancel)
    replay = await selector.handle_account_callback(home, config, confirm)
    assert t("accounts.stale") in replay.text
    assert calls == []

    confirmation = await selector.handle_account_callback(home, config, opening)
    before = _names(home)
    result = await selector.handle_account_callback(home, config, _data(confirmation)[1])
    assert calls == [(home, slot)]
    assert t(f"accounts.{action}_done", name=slot) in result.text
    assert opening not in _data(result)
    if action == "logout" and slot in before:
        assert f"{slot}  {t('accounts.unauthenticated')}" in result.text
    assert folder.is_dir()
    assert _names(home) == before
    await selector.handle_account_callback(home, config, _data(confirmation)[1])
    assert calls == [(home, slot)]


@pytest.mark.parametrize(
    ("action", "code", "slot", "kind"),
    [("logout", "l", "claude-a", "oauth"), ("disconnect", "k", "claude-credit", "api_key")],
)
async def test_slot_auth_confirmation_reports_busy(page, action, code, slot, kind):
    from ductor_bot.cli.slot_rotation import slot_use_lock

    selector, home, config = page
    folder = _prepare_slot(home, slot, kind)
    opening = f"acs:{code}:{slot}"
    confirmation = await selector.handle_account_callback(home, config, opening)
    before = _fingerprint(home)
    with slot_use_lock(folder.parent, slot) as held:
        assert held
        response = await selector.handle_account_callback(home, config, _data(confirmation)[1])
    assert t(f"accounts.{action}_busy") in response.text
    assert opening in _data(response)
    assert _fingerprint(home) == before


@pytest.mark.parametrize(
    ("method", "code", "slot", "kind"),
    [
        ("logout", "l", "claude", "oauth"),
        ("disconnect_api_key", "k", "claude-credit", "api_key"),
    ],
)
async def test_telegram_slot_auth_callbacks_reject_unauthorized_user(
    telegram, page, monkeypatch, *, method, code, slot, kind
):
    create, _fake, _sent, edited = telegram
    selector, home, config = page
    _prepare_slot(home, slot, kind)
    opening = f"acs:{code}:{slot}"
    confirmation = await selector.handle_account_callback(home, config, opening)
    operation = MagicMock()
    monkeypatch.setattr(claude_slots, method, operation)
    bot = create("main")
    message = _message(user=2)
    for data in (opening, _data(confirmation)[1]):
        event = CallbackQuery.model_validate(
            {
                "id": "fixture",
                "from": {"id": 2, "is_bot": False, "first_name": "Fixture"},
                "chat_instance": "fixture",
                "message": message.model_dump(by_alias=True),
                "data": data,
            }
        )
        callback = AsyncMock()
        await bot._auth(callback, event, {})
        callback.assert_not_awaited()
    operation.assert_not_called()
    edited.assert_not_awaited()


@pytest.mark.parametrize(("code", "kind"), [("l", "oauth"), ("k", "api_key")])
async def test_long_slot_auth_callback_is_short_and_bound_to_action(page, code, kind):
    selector, home, config = page
    name = "claude-" + "x" * 70
    _prepare_slot(home, name, kind)
    response = await selector.account_selector_start(home, config)
    opening = next(callback for callback in _data(response) if callback.startswith(f"acs:{code}:@"))
    other = "k" if code == "l" else "l"
    forged = opening.replace(f"acs:{code}:", f"acs:{other}:", 1)
    rejected = await selector.handle_account_callback(home, config, forged)
    assert t("accounts.stale") in rejected.text
    confirmation = await selector.handle_account_callback(home, config, opening)
    assert name in confirmation.text
    assert all(len(callback.encode("utf-8")) <= 64 for callback in [opening, *_data(confirmation)])


@pytest.mark.parametrize(
    ("code", "kind", "error"),
    [
        ("l", "api_key", "logout_api_key_slot"),
        ("k", "oauth", "disconnect_not_api_key_slot"),
        ("k", "api_key_detached", "disconnect_not_api_key_slot"),
    ],
)
async def test_slot_auth_rejects_an_ineligible_slot(page, code, kind, error):
    selector, home, config = page
    name = "claude-other"
    _prepare_slot(home, name, kind)

    response = await selector.handle_account_callback(home, config, f"acs:{code}:{name}")

    assert t(f"accounts.{error}") in response.text
    assert not any(value.startswith("acs:y:") for value in _data(response))


@pytest.mark.parametrize(
    ("action", "code", "kind", "method"),
    [
        ("logout", "l", "oauth", "logout"),
        ("disconnect", "k", "api_key", "disconnect_api_key"),
    ],
)
async def test_slot_auth_reports_backend_failure(page, monkeypatch, *, action, code, kind, method):
    selector, home, config = page
    name = "claude-other"
    _prepare_slot(home, name, kind)
    opening = f"acs:{code}:{name}"
    confirmation = await selector.handle_account_callback(home, config, opening)
    operation = MagicMock(side_effect=claude_slots.SlotDeleteError(f"{action}_failed"))
    monkeypatch.setattr(claude_slots, method, operation)

    response = await selector.handle_account_callback(home, config, _data(confirmation)[1])

    operation.assert_called_once_with(home, name, expected=ANY)
    assert t(f"accounts.{action}_failed") in response.text
    assert opening in _data(response)


async def test_logout_confirmation_rejects_recreated_default_slot(page, monkeypatch):
    selector, home, config = page
    folder = _prepare_slot(home, "claude", "oauth")
    confirmation = await selector.handle_account_callback(home, config, "acs:l:claude")
    folder.rename(folder.with_name("old-claude"))
    _prepare_slot(home, "claude", "oauth")
    operation = MagicMock()
    monkeypatch.setattr(claude_slots.subprocess, "run", operation)

    response = await selector.handle_account_callback(home, config, _data(confirmation)[1])

    assert t("accounts.stale") in response.text
    operation.assert_not_called()


@pytest.mark.parametrize(("code", "kind"), [("l", "oauth"), ("k", "api_key")])
async def test_slot_auth_confirmation_rechecks_identity_under_lock(page, monkeypatch, code, kind):
    from contextlib import contextmanager

    selector, home, config = page
    name = "claude-a" if kind == "oauth" else "claude-key"
    folder = _prepare_slot(home, name, kind)
    confirm = await selector.handle_account_callback(home, config, f"acs:{code}:{name}")
    preserved = folder.with_name("old-slot")
    real_lock = claude_slots.slot_use_lock

    @contextmanager
    def replace_on_lock(root, slot, *, deleting):
        with real_lock(root, slot, deleting=deleting) as reserved:
            assert reserved
            folder.rename(preserved)
            _prepare_slot(home, name, kind)
            yield reserved

    def unexpected(*_args, **_kwargs):
        pytest.fail("A replacement slot must not be mutated")

    monkeypatch.setattr(claude_slots, "slot_use_lock", replace_on_lock)
    monkeypatch.setattr(claude_slots.subprocess, "run", unexpected)
    monkeypatch.setattr(claude_slots, "atomic_text_save", unexpected)
    result = await selector.handle_account_callback(home, config, _data(confirm)[1])

    assert t("accounts.stale") in result.text
    assert preserved.is_dir()
    assert folder.is_dir()
    assert not (folder / "api-key-helper.detached").exists()
