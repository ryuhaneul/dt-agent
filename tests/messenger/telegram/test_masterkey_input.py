"""Master-key DM flows never reach ordinary journal, history, or mirror handlers."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Router
from aiogram.types import ForceReply

from ductor_bot.commands import get_main_only_commands
from ductor_bot.i18n import init
from ductor_bot.infra.secret_runtime import start_secret_runtime, stop_secret_runtime
from ductor_bot.infra.secret_store import set_secret
from ductor_bot.infra.secret_vault import VaultError, VaultState
from ductor_bot.messenger.telegram.app import _build_help_text
from ductor_bot.messenger.telegram.masterkey_input import PREFIX
from ductor_bot.messenger.telegram.polling import PollingDispatcher
from ductor_bot.messenger.telegram.secret_input import PENDING_SECONDS, SecretInput
from ductor_bot.messenger.telegram.update_journal import UpdateJournal
from tests.messenger.telegram.test_secret_input import (
    OTHER,
    USER,
    Harness,
    callback,
    drain_deletions,
    listen,
    message,
    settled_intercept,
    update,
)

KEY = "synthetic-master-key-42"
NEW_KEY = "replacement-master-key-43"
SECRET = "synthetic-stored-value-44"


@pytest.fixture
async def mk(tmp_path, monkeypatch):
    init("en")
    h = Harness(tmp_path)
    monkeypatch.setenv("DUCTOR_SECRETS_FILE", str(h.path))
    runtime = start_secret_runtime(tmp_path)
    runtime.vault._clock = h.clock
    h.vault = runtime.vault
    yield h
    await h.input.close()
    await stop_secret_runtime(runtime)


async def enter(h, value, mid=10):
    await settled_intercept(h.input, update(message(value, mid=mid, reply=h.prompt())))


async def setup(h):
    await h.command("/masterkey setup")
    assert isinstance(h.bot.sent[-1][1]["reply_markup"], ForceReply)
    await enter(h, KEY)
    assert (await h.vault.status()).state is VaultState.UNCONFIGURED
    await enter(h, KEY, mid=11)
    assert (await h.vault.status()).state is VaultState.UNLOCKED


async def test_setup_unlock_change_and_status(mk, caplog):
    caplog.set_level(logging.DEBUG)
    set_secret(mk.path, "TOOL_TOKEN", SECRET)
    await setup(mk)
    assert not mk.path.exists()
    assert await mk.vault.get("TOOL_TOKEN") == SECRET
    await mk.command("/masterkey status")
    assert "Unlimited" in mk.texts()[-1]
    await mk.command("/masterkey lock")
    await mk.command("/masterkey status")
    assert "Remaining: —" in mk.texts()[-1]
    await mk.command("/masterkey change")
    assert "/masterkey unlock" in mk.texts()[-1]
    await mk.command("/masterkey unlock")
    await enter(mk, KEY)
    await mk.command("/masterkey change")
    await enter(mk, KEY)
    await enter(mk, NEW_KEY, mid=12)
    assert await mk.vault.reveal("TOOL_TOKEN", KEY) == SECRET
    await enter(mk, NEW_KEY, mid=13)
    await mk.vault.lock()
    with pytest.raises(VaultError, match="INVALID_MASTER_KEY"):
        await mk.vault.unlock(KEY)
    mk.clock.now += 2
    await mk.vault.unlock(NEW_KEY)
    assert not mk.input._masterkey._keys
    assert not mk.input._masterkey._timers
    for canary in (KEY, NEW_KEY, SECRET):
        assert canary not in caplog.text
        assert all(canary not in value for value in mk.texts())
        assert canary not in mk.state.read_text()
        assert all(canary not in str(record.args) for record in caplog.records)
    assert (USER, 10) in mk.bot.deleted
    assert (USER, 11) in mk.bot.deleted


@pytest.mark.parametrize("action", ["setup", "change"])
async def test_confirmation_mismatch_discards_context(mk, action):
    if action == "change":
        await setup(mk)
    await mk.command(f"/masterkey {action}")
    if action == "change":
        await enter(mk, KEY)
    await enter(mk, NEW_KEY)
    await enter(mk, "different-master-key", mid=12)
    assert "do not match" in mk.texts()[-1]
    assert not mk.input._masterkey._keys
    assert not mk.input._pending
    if action == "setup":
        assert not mk.vault.encrypted_path.exists()
    else:
        await mk.vault.lock()
        await mk.vault.unlock(KEY)


async def test_errors_and_migration_guard(mk):
    await mk.command("/masterkey setup")
    await enter(mk, "short")
    assert "MASTER_KEY_TOO_SHORT" in mk.texts()[-1]
    assert not mk.input._masterkey._keys
    set_secret(mk.path, "DT_WEBCHAT_PASSWORD_HASH", "synthetic-hash")
    await mk.command("/masterkey setup")
    await enter(mk, KEY)
    await enter(mk, KEY, mid=12)
    assert "WEB_AUTH_MIGRATION_REQUIRED" in mk.texts()[-1]
    assert mk.path.exists()
    assert not mk.input._masterkey._keys


async def test_wrong_key_and_rate_limit_drop_context(mk):
    await setup(mk)
    await mk.command("/masterkey lock")
    await mk.command("/masterkey unlock")
    await enter(mk, "incorrect-master-key")
    assert "incorrect" in mk.texts()[-1]
    await mk.command("/masterkey unlock")
    await enter(mk, KEY)
    assert "SECRETS_RATE_LIMITED" in mk.texts()[-1]
    assert not mk.input._masterkey._keys
    mk.clock.now += 2
    await mk.command("/masterkey unlock")
    await enter(mk, KEY)
    assert (await mk.vault.status()).state is VaultState.UNLOCKED


async def test_ttl_buttons_custom_direct_and_unlimited(mk):
    await setup(mk)
    await mk.command("/masterkey ttl")
    markup = mk.bot.sent[-1][1]["reply_markup"]
    assert {b.callback_data for row in markup.inline_keyboard for b in row} == {
        "mk:ttl:15",
        "mk:ttl:60",
        "mk:ttl:480",
        "mk:ttl:1440",
        "mk:ttl:0",
        "mk:ttl:custom",
    }
    await mk.input.handle_callback(callback("mk:ttl:15"))
    assert (await mk.vault.status()).ttl_seconds == 900
    assert '"ttl_seconds": 900' in (mk.home / "config/secret_vault.json").read_text()
    mk.clock.now += 61
    await mk.command("/masterkey ttl 1")
    assert (await mk.vault.status()).state is VaultState.LOCKED
    await mk.command("/masterkey ttl 0")
    await enter(mk, KEY)
    await mk.command("/masterkey unlock")
    await enter(mk, KEY)
    mk.clock.now += 100000
    assert (await mk.vault.status()).state is VaultState.UNLOCKED
    await mk.input.handle_callback(callback("mk:ttl:custom"))
    await enter(mk, "60")
    assert (await mk.vault.status()).ttl_seconds == 3600
    await mk.command("/masterkey ttl -1")
    assert "nonnegative" in mk.texts()[-1]


@pytest.mark.parametrize("kind", ["group", "unauthorized", "subagent"])
async def test_main_allowed_dm_only(mk, kind):
    original = mk.input
    if kind == "subagent":
        mk.input = SecretInput(
            mk.bot,
            is_main=False,
            allowed_users={USER},
            ductor_home=mk.home,
            store_path=mk.path,
            clock=mk.clock,
        )
    kwargs = (
        {"kind": "group", "chat": -123}
        if kind == "group"
        else {"user": 999}
        if kind == "unauthorized"
        else {}
    )
    await mk.command("/masterkey setup", **kwargs)
    assert not mk.input._pending
    assert not mk.vault.encrypted_path.exists()
    await mk.input.handle_callback(callback("mk:ttl:15", **kwargs))
    assert (await mk.vault.status()).ttl_seconds == 0
    if kind == "subagent":
        await mk.input.close()
        mk.input = original


async def test_cancel_timeout_restart_and_close_discard_keys(mk):
    await mk.command("/masterkey setup")
    await enter(mk, KEY)
    assert KEY not in repr(mk.input._masterkey._keys)
    timer = mk.input._masterkey._timers[(USER, USER)]
    assert timer.when() - asyncio.get_running_loop().time() <= PENDING_SECONDS
    # Fire exactly the callback installed on the real 120-second timer without sleeping.
    timer._run()
    assert not mk.input._masterkey._keys
    await enter(mk, KEY, mid=12)
    assert "expired" in mk.texts()[-1]
    await mk.command("/masterkey setup")
    await enter(mk, KEY)
    await mk.command("/masterkey cancel")
    assert not mk.input._masterkey._keys
    await mk.command("/masterkey setup")
    await enter(mk, KEY)
    await mk.input.close()
    mk.restart()
    await enter(mk, KEY, mid=13)
    assert "expired" in mk.texts()[-1]
    assert not mk.vault.encrypted_path.exists()


async def test_locked_secret_writes_and_stale_buttons_are_refused(mk):
    await mk.command("/secret set TOOL_TOKEN")
    await mk.vault.setup(KEY)
    await mk.vault.lock()
    await enter(mk, SECRET)
    assert "/masterkey unlock" in mk.texts()[-1]
    assert not mk.path.exists()
    for command in ("/secret set TOOL_TOKEN", "/secret delete TOOL_TOKEN"):
        await mk.command(command)
        assert "/masterkey unlock" in mk.texts()[-1]
    for data in ("sec:ow:TOOL_TOKEN", "sec:del:TOOL_TOKEN"):
        await mk.input.handle_callback(callback(data))
        assert "/masterkey unlock" in mk.texts()[-1]
    assert not mk.path.exists()


async def test_startup_once_and_expiry_once_per_unlock(mk):
    await setup(mk)
    await mk.vault.lock()
    before = len(mk.bot.sent)
    await mk.input.start()
    await mk.input.start()
    alerts = mk.bot.sent[before:]
    assert {msg.chat.id for msg, _ in alerts} == {USER, OTHER}
    assert len(alerts) == 2
    assert all("Restart" in msg.text for msg, _ in alerts)
    await mk.vault.set_ttl(60)
    for _ in range(2):
        await mk.vault.unlock(KEY)
        mk.clock.now += 61
        assert (await mk.vault.status()).state is VaultState.LOCKED
        await mk.vault.status()
        await mk.vault.status()
        await asyncio.gather(*mk.input._masterkey._notifications)
    assert len([msg for msg, _ in mk.bot.sent if "Expiry locked" in msg.text]) == 4
    await mk.input.close()
    assert mk.vault._expiry_callback is None


async def test_pending_inputs_never_reach_journal_history_or_mirror(mk, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    await mk.command("/masterkey setup")
    journal = UpdateJournal(mk.home / "journal.json", clock=mk.clock)
    observed = MagicMock(wraps=journal)
    dp = PollingDispatcher(journal=observed, secret_input=mk.input)
    middleware = AsyncMock()
    history = AsyncMock()
    mirror = AsyncMock()
    router = Router()
    router.message.outer_middleware(middleware)

    async def ordinary(event, **data):
        await history(event, **data)
        await mirror(event, **data)

    router.message.register(ordinary)
    dp.include_router(router)
    updates = [update(message(KEY, mid=20, reply=mk.prompt())), update(message(KEY, mid=21))]
    assert await listen(dp, mk, updates, monkeypatch, process=True) == []
    await drain_deletions(mk.input)
    observed.record_pending.assert_not_called()
    middleware.assert_not_awaited()
    history.assert_not_awaited()
    mirror.assert_not_awaited()
    assert journal.is_handled(20)
    assert journal.is_handled(21)
    for path in (mk.state, mk.home / "journal.json"):
        assert KEY not in path.read_text()
    assert KEY not in caplog.text
    assert all(KEY not in str(record.args) for record in caplog.records)


def test_command_and_help_registration():
    assert "masterkey" in dict(get_main_only_commands())
    assert "/masterkey" in _build_help_text(main_only=True)
    assert "/masterkey" not in _build_help_text()
    assert PREFIX not in _build_help_text(main_only=True)


async def test_app_security_alert_ignores_startup_target_opt_out(tmp_path, monkeypatch):
    from ductor_bot.config import AgentConfig, NotificationTarget
    from ductor_bot.messenger.telegram.app import TelegramBot
    from tests.messenger.telegram.test_secret_input import FakeBot

    init("en")
    monkeypatch.setenv("DUCTOR_SECRETS_FILE", str(tmp_path / "secrets.env"))
    runtime = start_secret_runtime(tmp_path)
    fake = FakeBot()
    fake.delete_webhook = AsyncMock()
    monkeypatch.setattr("ductor_bot.messenger.telegram.app.Bot", lambda **_kwargs: fake)
    config = AgentConfig(
        telegram_token="303:synthetic-token", allowed_user_ids=[USER], ductor_home=str(tmp_path)
    )
    config.notifications.startup_targets = [NotificationTarget(enabled=False)]
    bot = TelegramBot(config)
    bot._dp.start_polling = AsyncMock()
    try:
        await runtime.vault.setup(KEY)
        await runtime.vault.lock()
        await bot.run()
        assert len(fake.sent) == 1
        assert fake.sent[0][0].chat.id == USER
        assert "Restart locked" in fake.sent[0][0].text
        config.allowed_user_ids = [OTHER]
        bot._on_auth_hot_reload(config, {"allowed_user_ids": [OTHER]})
        await runtime.vault.set_ttl(60)
        await runtime.vault.unlock(KEY)
        runtime.vault._unlocked_at -= 61
        await runtime.vault.status()
        await asyncio.gather(*bot._secret_input._masterkey._notifications)
        assert fake.sent[-1][0].chat.id == OTHER
        await bot._on_callback_query(callback("mk:ttl:0", user=OTHER))
        assert (await runtime.vault.status()).ttl_seconds == 60
        assert any(p.key.endswith("ttl-key") for p in bot._secret_input._pending.values())
    finally:
        await bot._secret_input.close()
        await stop_secret_runtime(runtime)


async def test_unconfigured_and_duplicate_setup_feedback(mk):
    for action in ("unlock", "lock", "change"):
        await mk.command(f"/masterkey {action}")
        assert "No master key" in mk.texts()[-1]
    await setup(mk)
    await mk.command("/masterkey setup")
    assert "already configured" in mk.texts()[-1]


async def test_idle_expiry_timer_delivers_security_alert_without_status_poll(mk, monkeypatch):
    mk.vault._clock = asyncio.get_running_loop().time
    await mk.input.start()
    await setup(mk)
    delivered = asyncio.Event()
    original = mk.bot.send_message

    async def send(chat_id, content, **kwargs):
        result = await original(chat_id, content, **kwargs)
        if "Expiry locked" in content:
            delivered.set()
        return result

    monkeypatch.setattr(mk.bot, "send_message", send)
    await mk.vault.set_ttl(1)
    await asyncio.wait_for(delivered.wait(), timeout=3)
    assert (await mk.vault.status()).state is VaultState.LOCKED
    assert len([msg for msg, _ in mk.bot.sent if "Expiry locked" in msg.text]) == 2


async def test_failure_code_never_echoes_key_or_retains_context(mk, monkeypatch, caplog):
    await setup(mk)
    await mk.command("/masterkey lock")
    monkeypatch.setattr(mk.vault, "unlock", AsyncMock(side_effect=VaultError(KEY)))
    await mk.command("/masterkey unlock")
    await enter(mk, KEY)
    assert "operation failed" in mk.texts()[-1]
    assert not mk.input._masterkey._keys
    assert not mk.input._pending
    assert KEY not in caplog.text
    assert all(KEY not in text for text in mk.texts())


async def test_setup_symlink_error_is_explained_and_source_preserved(mk):
    original = mk.path.with_name("original.env")
    original.write_text("TOKEN=synthetic-symlink-value\n")
    mk.path.unlink(missing_ok=True)
    mk.path.symlink_to(original)
    before = original.read_bytes()
    await mk.command("/masterkey setup")
    await enter(mk, KEY)
    await enter(mk, KEY, mid=12)
    assert "SECRETS_FILE_SYMLINK" in mk.texts()[-1]
    assert "symbolic link" in mk.texts()[-1]
    assert "synthetic-symlink-value" not in mk.texts()[-1]
    assert original.read_bytes() == before
    assert mk.path.is_symlink()
    assert (await mk.vault.status()).state is VaultState.UNCONFIGURED
    assert not mk.input._masterkey._keys


async def test_ttl_extension_force_reply_never_reaches_history(mk, monkeypatch, caplog):
    await setup(mk)
    await mk.vault.set_ttl(60)
    await mk.command("/masterkey ttl 2")
    assert "extend" in mk.texts()[-1]
    assert (await mk.vault.status()).ttl_seconds == 60
    journal = UpdateJournal(mk.home / "ttl-journal.json", clock=mk.clock)
    observed = MagicMock(wraps=journal)
    dp = PollingDispatcher(journal=observed, secret_input=mk.input)
    history = AsyncMock()
    router = Router()
    async def ordinary(event, **data):
        await history(event, **data)

    router.message.register(ordinary)
    dp.include_router(router)
    await listen(
        dp, mk, [update(message(KEY, mid=80, reply=mk.prompt()))], monkeypatch, process=True
    )
    await drain_deletions(mk.input)
    observed.record_pending.assert_not_called()
    history.assert_not_awaited()
    assert (await mk.vault.status()).ttl_seconds == 120
    assert KEY not in caplog.text
    assert KEY not in mk.state.read_text()
    assert journal.is_handled(80)
    await mk.input.handle_callback(callback("mk:ttl:0"))
    assert (await mk.vault.status()).ttl_seconds == 120
    await enter(mk, "synthetic-wrong-master", mid=81)
    assert "incorrect" in mk.texts()[-1]
    await mk.command("/masterkey unlock")
    await enter(mk, KEY, mid=82)
    assert "SECRETS_RATE_LIMITED" in mk.texts()[-1]


async def test_invalid_config_startup_alert_includes_recovery(mk):
    await setup(mk)
    await mk.vault.lock()
    mk.vault.configuration_error = "INVALID_VAULT_CONFIG"
    before = len(mk.bot.sent)
    await mk.input.start()
    assert len(mk.bot.sent[before:]) == 2
    assert all("INVALID_VAULT_CONFIG" in msg.text for msg, _ in mk.bot.sent[before:])
    assert all("config/secret_vault.json" in msg.text for msg, _ in mk.bot.sent[before:])
    await mk.command("/masterkey unlock")
    await enter(mk, KEY)
    assert "INVALID_VAULT_CONFIG" in mk.texts()[-1]


@pytest.mark.parametrize("thread", [None, 707])
async def test_masterkey_flow_preserves_command_topic(mk, thread):
    async def command(text):
        await mk.command(text, thread=thread)

    # Value messages intentionally have no topic metadata: the pending request
    # must retain the command topic through every stage.
    await command("/masterkey setup")
    await enter(mk, KEY)
    await enter(mk, KEY, mid=11)
    await command("/masterkey")
    await command("/masterkey lock")
    await command("/masterkey unlock")
    await enter(mk, "incorrect-master-key")
    assert "incorrect" in mk.texts()[-1]
    mk.clock.now += 2
    await command("/masterkey unlock")
    await enter(mk, KEY)
    await command("/masterkey change")
    await enter(mk, KEY)
    await enter(mk, NEW_KEY, mid=12)
    await enter(mk, NEW_KEY, mid=13)
    await command("/masterkey ttl")
    await mk.input.handle_callback(callback("mk:ttl:15", thread=thread))
    await mk.input.handle_callback(callback("mk:ttl:custom", thread=thread))
    await enter(mk, "60")
    await command("/masterkey lock")
    await mk.input.handle_callback(callback("mk:ttl:0", thread=thread))
    await enter(mk, NEW_KEY)
    await command("/masterkey ttl -1")
    await command("/masterkey unlock")
    await command("/masterkey cancel")
    await command("/masterkey nonsense")
    await command("/masterkey unlock extra")
    assert all(kwargs["message_thread_id"] == thread for _, kwargs in mk.bot.sent)
    assert mk.bot.answered


@pytest.mark.parametrize("expiry", ["timer", "deadline", "restart"])
async def test_masterkey_expiry_preserves_pending_topic(mk, expiry):
    await mk.command("/masterkey setup", thread=707)
    await enter(mk, KEY)
    assert mk.input._pending[(USER, USER)].thread_id == 707
    if expiry == "timer":
        mk.input._masterkey._timers[(USER, USER)]._run()
    elif expiry == "deadline":
        mk.clock.now += PENDING_SECONDS
    else:
        await mk.input.close()
        mk.restart()
    await enter(mk, KEY, mid=12)
    assert "expired" in mk.texts()[-1]
    assert all(kwargs["message_thread_id"] == 707 for _, kwargs in mk.bot.sent)


@pytest.mark.parametrize("source", ["command", "callback", "value"])
async def test_locked_secret_notice_preserves_topic(mk, source):
    if source == "value":
        await mk.command("/secret set TOOL_TOKEN", thread=707)
    await mk.vault.setup(KEY)
    await mk.vault.lock()
    if source == "command":
        await mk.command("/secret set TOOL_TOKEN", thread=707)
    elif source == "callback":
        await mk.input.handle_callback(callback("sec:ow:TOOL_TOKEN", thread=707))
    else:
        await enter(mk, SECRET)
    assert "/masterkey unlock" in mk.texts()[-1]
    assert all(kwargs["message_thread_id"] == 707 for _, kwargs in mk.bot.sent)


@pytest.mark.parametrize("thread", [None, 707])
async def test_masterkey_group_denial_preserves_topic(mk, thread):
    await mk.command("/masterkey unlock", chat=-123, kind="supergroup", thread=thread)
    await mk.input.handle_callback(
        callback("mk:ttl:15", chat=-123, kind="supergroup", thread=thread)
    )
    assert not mk.input._pending
    assert (await mk.vault.status()).state is VaultState.UNCONFIGURED
    assert all(kwargs["message_thread_id"] == thread for _, kwargs in mk.bot.sent)
