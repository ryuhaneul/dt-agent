# ruff: noqa: INP001
"""Chat log activation, connectivity and Telegram adapter contracts."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import time_machine

from ductor_bot.bus.bus import MessageBus
from ductor_bot.bus.envelope import Envelope, Origin
from ductor_bot.chat_log.events import EventHub
from ductor_bot.chat_log.link import OutboxDrainer, TelegramLinkMonitor
from ductor_bot.chat_log.runtime import ChatLogRuntime
from ductor_bot.config import AgentConfig, ChatLogConfig
from ductor_bot.messenger.telegram.app import TelegramBot
from ductor_bot.messenger.telegram.startup import run_startup
from ductor_bot.messenger.telegram.transport import TelegramTransport
from ductor_bot.messenger.telegram.webchat_ops import TelegramWebchatOps
from ductor_bot.workspace.paths import DuctorPaths


def make_bot(tmp_path):
    config = AgentConfig(chat_log=ChatLogConfig(enabled=True), allowed_user_ids=[123])
    orch = SimpleNamespace(
        paths=DuctorPaths(ductor_home=tmp_path), set_history_recorder=MagicMock()
    )
    bot = SimpleNamespace(
        config=config,
        _orch=orch,
        _orchestrator=orch,
        _agent_name="main",
        _chat_log_runtime=None,
        _chat_log_checked=False,
        _bus=MessageBus(),
        bot_instance=MagicMock(),
        file_roots=MagicMock(return_value=None),
        _sync_commands=AsyncMock(),
        _watch_restart_marker=AsyncMock(),
        audit_groups=AsyncMock(),
        _run_group_audit_loop=AsyncMock(),
    )
    bot.bot_instance.get_me = AsyncMock(return_value=SimpleNamespace(id=1, username="example"))
    bot.bot_instance.send_message = AsyncMock(return_value=SimpleNamespace(message_id=1))
    bot._telegram_transport = TelegramTransport(bot)
    bot._bus.register_transport(bot._telegram_transport)
    return bot


async def test_runtime_startup_idempotent_and_stop_detaches(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="ductor_bot.chat_log.runtime")
    bot = make_bot(tmp_path)
    bot.config.telegram_token = "123456:SENSITIVE_TOKEN"
    await run_startup(bot)
    runtime = bot._chat_log_runtime
    assert isinstance(runtime, ChatLogRuntime)
    link_task, drain_task = runtime.link._task, runtime.drainer._task
    assert bot.config.webchat.enabled is False
    assert await ChatLogRuntime.start(bot) is runtime
    await run_startup(bot)
    assert (runtime.link._task, runtime.drainer._task) == (link_task, drain_task)
    bot._orch.set_history_recorder.assert_called_once_with(runtime.recorder)
    assert bot._bus._delivery_observer == runtime.recorder.on_delivered
    assert bot._telegram_transport._outbox is runtime.outbox
    assert bot._telegram_transport._on_failure == runtime.link.report_failure
    assert bot._telegram_transport._on_retry_after == runtime.drainer.defer_retry
    runtime.recorder.blocked.add("123.7")
    assert bot._telegram_transport._is_blocked("123.7")
    assert not bot._telegram_transport._is_blocked("123.8")
    await runtime.stop()
    await runtime.stop()
    assert runtime.link._task is runtime.drainer._task is None
    assert bot._telegram_transport._outbox is None
    assert bot._telegram_transport._on_failure is None
    assert bot._telegram_transport._on_retry_after is None
    assert bot._telegram_transport._is_blocked is None
    assert bot._bus._delivery_observer is None
    assert bot._orch.set_history_recorder.call_args.args == (None,)
    assert await ChatLogRuntime.start(bot) is runtime
    assert [
        record.getMessage()
        for record in caplog.records
        if record.name == "ductor_bot.chat_log.runtime" and record.levelno == logging.INFO
    ] == ["Chat log runtime started"]
    assert bot.config.telegram_token not in caplog.text
    assert not bot._orch.paths.webchat_dir.exists()
    await bot._restart_watcher
    await bot._group_audit_task


@pytest.mark.parametrize("enabled", [False, True])
async def test_disabled_and_subagent_paths_do_not_touch_state_or_network(tmp_path, enabled):
    bot = make_bot(tmp_path)
    bot.config.chat_log.enabled = enabled
    if enabled:
        bot._agent_name = "worker"
    assert await ChatLogRuntime.start(bot) is None
    assert bot._chat_log_runtime is None
    bot.bot_instance.get_me.assert_not_awaited()
    bot.bot_instance.send_message.assert_not_awaited()
    bot._orch.set_history_recorder.assert_not_called()
    assert not bot._orch.paths.chat_log_dir.exists()
    assert not bot._orch.paths.webchat_dir.exists()


async def test_invalid_settings_log_once_and_do_not_start(tmp_path, caplog):
    bot = make_bot(tmp_path)
    bot.config.transport = "matrix"
    bot.config.transports = ["matrix"]
    assert await ChatLogRuntime.start(bot) is None
    assert await ChatLogRuntime.start(bot) is None
    assert caplog.text.count("Chat log disabled: telegram_required") == 1
    assert not bot._orch.paths.chat_log_dir.exists()
    bot.bot_instance.get_me.assert_not_awaited()


async def test_startup_creates_orchestrator_before_runtime(tmp_path):
    bot = make_bot(tmp_path)
    existing = bot._orchestrator
    bot._orchestrator = None
    order = []

    async def primary(target):
        order.append("orchestrator")
        target._orchestrator = existing

    async def start(_target):
        order.append("runtime")

    async def get_me():
        order.append("get_me")
        return SimpleNamespace(id=1, username="example")

    bot.bot_instance.get_me.side_effect = get_me
    with (
        patch("ductor_bot.messenger.telegram.startup._run_primary_startup", side_effect=primary),
        patch.object(ChatLogRuntime, "start", side_effect=start),
    ):
        await run_startup(bot)
    assert order == ["orchestrator", "runtime", "get_me"]
    await bot._restart_watcher
    await bot._group_audit_task


async def test_transport_failure_callback_updates_link_after_persistence(tmp_path):
    bot = make_bot(tmp_path)
    runtime = await ChatLogRuntime.start(bot)
    try:
        await runtime.link.stop()
        await runtime.drainer.stop()
        bot.bot_instance.send_message.side_effect = TimeoutError()
        env = Envelope(origin=Origin.WEBHOOK_WAKE, chat_id=123, result_text="visible")
        await bot._telegram_transport.deliver(env)
        assert await runtime.outbox.has_pending()
        assert runtime.link.state()["state"] == "down"
        assert env.delivered is True
    finally:
        await runtime.stop()


async def test_shutdown_stops_runtime_after_polling_before_orchestrator(tmp_path):
    config = AgentConfig(telegram_token="123456:TEST_TOKEN", ductor_home=str(tmp_path))
    bot = TelegramBot(config)
    order = []

    async def polling(_budget):
        order.append("polling")

    async def runtime_stop():
        order.append("runtime")

    async def orch_shutdown(_budget):
        order.append("orchestrator")

    bot._stop_polling_owned = AsyncMock(side_effect=polling)
    bot._chat_log_runtime = SimpleNamespace(stop=AsyncMock(side_effect=runtime_stop))
    bot._orchestrator = SimpleNamespace(
        run_pre_shutdown_hooks=AsyncMock(), shutdown=AsyncMock(side_effect=orch_shutdown)
    )
    bot._bot.delete_webhook = AsyncMock()
    bot._secret_input.close = AsyncMock()
    await bot.shutdown()
    assert order == ["polling", "runtime", "orchestrator"]


def test_monitor_unknown_history_buckets_and_down_since():
    hub = EventHub()
    queue = hub.subscribe()
    wake = MagicMock()
    link = TelegramLinkMonitor(SimpleNamespace(), hub, on_up=wake)
    with time_machine.travel(datetime(2026, 1, 1, 12, 0, 10, tzinfo=UTC), tick=False) as clock:
        assert link.state()["strip"] == ["none"] * 30
        assert link.state()["state"] == "unknown"
        assert link.state()["last_check"] is None
        link.record(up=True, probe=True)
        assert link.state()["strip"][-1] == "ok"
        clock.shift(10)
        link.report_failure()
        since = link.state()["down_since"]
        clock.shift(10)
        link.report_failure()
        assert link.state()["down_since"] == since
        assert link.state()["strip"][-1] == "mixed"
        clock.shift(40)
        link.record(up=False, probe=True)
        assert link.state()["strip"][-2:] == ["mixed", "down"]
        clock.shift(60)
        link.record(up=True, probe=True)
        assert link.state()["down_since"] is None
        assert wake.call_count == 2
        assert queue.qsize() == 5
        clock.shift(3600)
        assert link.state()["strip"] == ["none"] * 30
        assert not link._samples


async def test_probe_failure_is_down_and_events_publish_on_every_probe():
    ops = SimpleNamespace(probe=AsyncMock(side_effect=[True, TimeoutError(), True]))
    hub = EventHub()
    queue = hub.subscribe()
    link = TelegramLinkMonitor(ops, hub)
    await link.probe_once()
    await link.probe_once()
    assert link.probe_up is False
    assert link.state()["state"] == "down"
    await link.probe_once()
    assert link.probe_failure_generation == 1
    assert link.state()["state"] == "up"
    assert queue.qsize() == 3


async def test_monitor_starts_once_and_probes_at_30_seconds(monkeypatch):
    ops = SimpleNamespace(probe=AsyncMock(return_value=True))
    link = TelegramLinkMonitor(ops, EventHub())
    sleep = AsyncMock(side_effect=asyncio.CancelledError())
    monkeypatch.setattr("ductor_bot.chat_log.link.asyncio.sleep", sleep)
    monkeypatch.setattr("ductor_bot.chat_log.link.time", SimpleNamespace(monotonic=lambda: 0.0))
    link.start()
    first = link._task
    link.start()
    assert link._task is first
    with pytest.raises(asyncio.CancelledError):
        await first
    ops.probe.assert_awaited_once()
    sleep.assert_awaited_once_with(30)
    await link.stop()


async def test_probe_adapter_uses_ten_second_timeout(tmp_path):
    bot = make_bot(tmp_path)
    ops = TelegramWebchatOps(bot)

    with patch(
        "ductor_bot.messenger.telegram.webchat_ops.asyncio.timeout", wraps=asyncio.timeout
    ) as timeout:
        assert await ops.probe()
    timeout.assert_called_once_with(10)
    bot.bot_instance.get_me.assert_awaited_once()


async def test_link_up_wakes_existing_worker_without_waiting_for_interval(tmp_path):
    bot = make_bot(tmp_path)
    runtime = await ChatLogRuntime.start(bot)
    try:
        await runtime.link.stop()
        ran = asyncio.Event()

        async def drain():
            ran.set()

        with patch.object(runtime.drainer, "drain", side_effect=drain):
            runtime.drainer.wake()
            await asyncio.wait_for(ran.wait(), timeout=1)
            ran.clear()
            runtime.link.record(up=False, probe=True)
            runtime.link.record(up=True, probe=True)
            await asyncio.wait_for(ran.wait(), timeout=1)
    finally:
        await runtime.stop()


async def test_worker_prunes_at_start_and_after_24_hours(tmp_path, monkeypatch):
    bot = make_bot(tmp_path)
    ops = TelegramWebchatOps(bot)
    from ductor_bot.chat_log.outbox import Outbox

    outbox = Outbox(tmp_path / "outbox")
    drainer = OutboxDrainer(outbox, ops, TelegramLinkMonitor(ops, EventHub()))
    now = 0.0
    prunes = []
    ended = asyncio.Event()

    async def prune():
        prunes.append(now)
        if len(prunes) == 2:
            ended.set()

    async def drain():
        nonlocal now
        now = 86401.0
        drainer.wake()
        await asyncio.sleep(0)

    monkeypatch.setattr(outbox, "prune", prune)
    monkeypatch.setattr(drainer, "drain", drain)
    # Patch only the worker's clock module reference, leaving asyncio's clock intact.
    monkeypatch.setattr("ductor_bot.chat_log.link.time", SimpleNamespace(monotonic=lambda: now))
    drainer.start()
    try:
        await asyncio.wait_for(ended.wait(), timeout=1)
        assert prunes == [0.0, 86401.0]
    finally:
        await drainer.stop()


async def test_enabled_runtime_records_bus_without_web(tmp_path):
    bot = make_bot(tmp_path)
    runtime = await ChatLogRuntime.start(bot)
    try:
        await bot._bus.submit(
            Envelope(origin=Origin.WEBHOOK_WAKE, chat_id=123, result_text="Public report")
        )
        rows = await runtime.history.read("123")
        assert [(row.source, row.kind, row.text) for row in rows] == [
            ("system", "webhook", "Public report")
        ]
        assert not bot._orch.paths.webchat_dir.exists()
    finally:
        await runtime.stop()


async def test_probe_interval_includes_probe_duration(monkeypatch):
    now = 0.0

    async def probe():
        nonlocal now
        now = 10.0
        return False

    link = TelegramLinkMonitor(SimpleNamespace(probe=probe), EventHub())
    sleep = AsyncMock(side_effect=asyncio.CancelledError())
    monkeypatch.setattr("ductor_bot.chat_log.link.asyncio.sleep", sleep)
    monkeypatch.setattr("ductor_bot.chat_log.link.time", SimpleNamespace(monotonic=lambda: now))
    link.start()
    with pytest.raises(asyncio.CancelledError):
        await link._task
    sleep.assert_awaited_once_with(20.0)
    await link.stop()


def test_status_disabled_is_successful_and_does_not_create_state(tmp_path, monkeypatch, capsys):
    from ductor_bot.chat_log.cli import cmd_chat_log

    monkeypatch.setenv("DUCTOR_HOME", str(tmp_path))
    cmd_chat_log(["chat-log", "status"])
    text = " ".join(capsys.readouterr().out.split())
    assert "inactive" in text
    assert "files: 0" in text
    assert "entries: 0" in text
    assert not (tmp_path / "chat_log").exists()
    assert not (tmp_path / "webchat").exists()


def test_status_reports_activation_errors_and_counts(tmp_path, monkeypatch, capsys):
    import json

    from ductor_bot.chat_log.cli import cmd_chat_log
    from ductor_bot.chat_log.history import Entry, HistoryStore
    from ductor_bot.chat_log.outbox import Outbox

    paths = DuctorPaths(ductor_home=tmp_path)
    config = AgentConfig(chat_log=ChatLogConfig(enabled=True))
    paths.config_dir.mkdir()
    paths.config_path.write_text(config.model_dump_json())
    monkeypatch.setenv("DUCTOR_HOME", str(tmp_path))

    async def seed():
        history = HistoryStore(paths.chat_log_dir / "history")
        await history.append("1", Entry(role="user", source="telegram", text="visible"))
        await history.append("2", Entry(role="bot", source="telegram", text="reply"))
        outbox = Outbox(paths.chat_log_dir / "outbox")
        await outbox.enqueue(kind="bus", chat_id=1, text="pending")
        failed = await outbox.enqueue(kind="bus", chat_id=1, text="failed")
        sent = await outbox.enqueue(kind="bus", chat_id=1, text="sent")
        await outbox.mark_failed(failed.id)
        await outbox.mark_sent(sent.id)

    asyncio.run(seed())
    before = {path: path.read_bytes() for path in paths.chat_log_dir.rglob("*") if path.is_file()}
    cmd_chat_log(["status"])
    text = " ".join(capsys.readouterr().out.split())
    assert "Chat log: active" in text
    assert "files: 2" in text
    assert "entries: 2" in text
    assert "pending: 1" in text
    assert "failed: 1" in text
    assert before == {
        path: path.read_bytes() for path in paths.chat_log_dir.rglob("*") if path.is_file()
    }
    config.transport = "matrix"
    config.transports = ["matrix"]
    paths.config_path.write_text(json.dumps(config.model_dump(mode="json")))
    cmd_chat_log(["status"])
    text = " ".join(capsys.readouterr().out.split())
    assert "inactive" in text
    assert "telegram_required" in text
    assert "entries: 2" in text


@pytest.mark.parametrize("subcommand", ["status", "import"])
def test_main_dispatches_chat_log_subcommands(subcommand, monkeypatch):
    from ductor_bot import __main__ as entrypoint

    monkeypatch.setattr("sys.argv", ["ductor", "chat-log", subcommand])
    with (
        patch.object(entrypoint, "_cmd_chat_log") as dispatch,
        patch.object(entrypoint, "_init_console_language"),
    ):
        entrypoint.main()
    dispatch.assert_called_once_with(["chat-log", subcommand])


def test_status_tolerates_outbox_files_moving_during_scan(tmp_path, monkeypatch, capsys):
    import json

    from ductor_bot.chat_log.cli import cmd_chat_log
    from ductor_bot.chat_log.outbox import Outbox

    monkeypatch.setenv("DUCTOR_HOME", str(tmp_path))
    outbox = Outbox(tmp_path / "chat_log" / "outbox")
    asyncio.run(outbox.enqueue(kind="bus", chat_id=1, text="moving"))
    original = Outbox._load

    def load(path):
        if path.parent.name == "pending":
            data = json.loads(path.read_text())
            data["state"] = "sent"
            path.write_text(json.dumps(data))
            done = outbox.directory / "done"
            done.mkdir(exist_ok=True)
            path.replace(done / path.name)
            raise FileNotFoundError(path.name)
        return original(path)

    monkeypatch.setattr(Outbox, "_load", staticmethod(load))
    cmd_chat_log(["status"])
    text = " ".join(capsys.readouterr().out.split())
    assert "pending: 0" in text
    assert "failed: 0" in text


async def test_retention_disabled_by_default_preserves_history_and_backups(tmp_path):
    from ductor_bot.chat_log.history import Entry, HistoryStore

    bot = make_bot(tmp_path)
    history = HistoryStore(bot._orch.paths.chat_log_dir / "history")
    await history.append("123", Entry("user", "telegram", "old", ts="2000-01-01T00:00:00Z"))
    backup = bot._orch.paths.webchat_dir / "backups" / "2000-01-01" / "PERSONA.md.120000"
    backup.parent.mkdir(parents=True)
    backup.write_bytes(b"old")
    runtime = await ChatLogRuntime.start(bot)
    try:
        assert runtime._retention_task is None
        assert await runtime.history.count("123") == 1
        assert backup.read_bytes() == b"old"
    finally:
        await runtime.stop()


async def test_retention_runs_at_start_and_daily_then_stops(tmp_path):
    bot = make_bot(tmp_path)
    # model_copy permits testing the runtime before the config fields are implemented.
    bot.config.chat_log = bot.config.chat_log.model_copy(
        update={"retention_days": 7, "backup_retention_days": 30}
    )
    first, second = asyncio.Event(), asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def prune(**kwargs):
        nonlocal calls
        assert kwargs == {"history_days": 7, "backup_days": 30}
        calls += 1
        (first if calls == 1 else second).set()

    async def sleep(seconds):
        assert seconds == 86400
        await release.wait()
        release.clear()

    with (
        patch("ductor_bot.chat_log.runtime.RetentionPruner.prune", side_effect=prune),
        patch("ductor_bot.chat_log.runtime._retention_sleep", side_effect=sleep),
    ):
        runtime = await ChatLogRuntime.start(bot)
        try:
            async with asyncio.timeout(3):
                await first.wait()
            assert calls == 1
            assert await ChatLogRuntime.start(bot) is runtime
            release.set()
            async with asyncio.timeout(3):
                await second.wait()
            assert calls == 2
        finally:
            await runtime.stop()
        assert runtime._retention_task is None


def test_defer_retry_preserves_later_deadline(tmp_path, monkeypatch):
    runtime = ChatLogRuntime(make_bot(tmp_path))
    now = 100.0
    monkeypatch.setattr("ductor_bot.chat_log.link.time.monotonic", lambda: now)
    runtime.drainer.defer_retry(70)
    assert runtime.drainer._retry_at == 170.0
    now = 101.0
    runtime.drainer.defer_retry(1)
    assert runtime.drainer._retry_at == 170.0
    runtime.drainer.defer_retry(100)
    assert runtime.drainer._retry_at == 201.0
