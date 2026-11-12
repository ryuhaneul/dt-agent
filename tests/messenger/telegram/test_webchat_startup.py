"""Web-enabled local startup precedes Telegram connectivity and runs hooks once."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)

from ductor_bot.chat_log.runtime import ChatLogRuntime
from ductor_bot.config import AgentConfig, ChatLogConfig, WebchatConfig
from ductor_bot.infra.inflight import InflightTracker, InflightTurn
from ductor_bot.messenger.telegram.app import TelegramBot
from ductor_bot.messenger.telegram.startup import (
    _run_primary_startup,
    run_local_notices,
    run_local_startup,
)
from ductor_bot.orchestrator.core import Orchestrator
from ductor_bot.orchestrator.registry import OrchestratorResult
from ductor_bot.session.key import SessionKey
from ductor_bot.workspace.paths import DuctorPaths


@pytest.fixture
async def make_bot(tmp_path):
    bots = []

    def make(*, enabled=True, valid=True, agent_name="main"):
        config = AgentConfig(
            telegram_token="123456:TEST_TOKEN",
            ductor_home=str(tmp_path),
            allowed_user_ids=[123],
            chat_log=ChatLogConfig(enabled=enabled),
            webchat=WebchatConfig(
                enabled=enabled,
                domain="chat.example.com" if valid else "",
                allowed_ips=["203.0.113.0/24"],
                acme_email="admin@example.com",
            ),
        )
        bot = TelegramBot(config, agent_name=agent_name)
        bot._bot.delete_webhook = AsyncMock()
        bot._dp.start_polling = AsyncMock()

        async def replay() -> None:
            pass

        bot._replay_update_journal = replay
        bots.append(bot)
        return bot

    yield make
    for bot in bots:
        await bot._webchat_stop()
        await bot._bot.session.close()


def runtime():
    return SimpleNamespace(
        link=SimpleNamespace(report_failure=MagicMock(), record=MagicMock()), stop=AsyncMock()
    )


async def test_disabled_web_preserves_original_network_error(make_bot):
    bot = make_bot(enabled=False)
    original = TelegramNetworkError(MagicMock(), "offline")
    bot._bot.delete_webhook.side_effect = original
    bot._webchat_local_start = AsyncMock()
    with pytest.raises(TelegramNetworkError) as caught:
        await bot.run()
    assert caught.value is original
    bot._bot.delete_webhook.assert_awaited_once_with(drop_pending_updates=False)
    bot._webchat_local_start.assert_not_awaited()
    bot._dp.start_polling.assert_not_awaited()
    assert bot._orchestrator is None


@pytest.mark.parametrize(
    ("failures", "delays"), [(3, [5, 10, 20]), (7, [5, 10, 20, 40, 60, 60, 60])]
)
async def test_local_start_hooks_and_bridge_precede_retry_and_hooks_run_once(
    make_bot, failures, delays, caplog
):
    caplog.set_level(logging.INFO, logger="ductor_bot.messenger.telegram.app")
    bot = make_bot()
    recorded = []
    log_runtime = runtime()
    stop = AsyncMock()

    async def local(target):
        recorded.append("local")
        target._orchestrator = SimpleNamespace()
        return []

    async def hook():
        recorded.append("hook")

    async def start(target):
        recorded.append("runtime")
        target._chat_log_runtime = log_runtime
        return log_runtime

    async def bridge(target, active):
        assert target is bot
        assert active is log_runtime
        recorded.append("bridge")
        return stop

    attempts = 0

    async def delete(**kwargs):
        nonlocal attempts
        assert kwargs == {"drop_pending_updates": False}
        assert recorded[:4] == ["local", "hook", "runtime", "bridge"]
        attempts += 1
        if attempts <= failures:
            raise TelegramNetworkError(MagicMock(), "SENSITIVE_TOKEN SENSITIVE_MESSAGE")

    async def polling(*_args, **_kwargs):
        await bot._dp.emit_startup(bot=bot._bot)
        recorded.append("polling")

    bot.register_startup_hook(hook)
    bot._bot.delete_webhook.side_effect = delete
    bot._dp.start_polling.side_effect = polling
    bot._wait_webchat_retry = AsyncMock()
    with (
        patch(
            "ductor_bot.messenger.telegram.startup.run_local_startup", side_effect=local
        ) as initialize,
        patch.object(ChatLogRuntime, "start", side_effect=start) as start_runtime,
        patch("ductor_bot.webchat.bridge.start_bridge", side_effect=bridge) as start_listener,
        patch("ductor_bot.messenger.telegram.startup.run_startup", new_callable=AsyncMock),
    ):
        assert await bot.run() == 0
        await bot._webchat_local_start()
        await bot._dp.emit_startup(bot=bot._bot)
    assert [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.INFO and "local startup completed first" in record.getMessage()
    ] == ["Telegram unavailable; webchat local startup completed first: TelegramNetworkError"]
    assert "SENSITIVE_TOKEN" not in caplog.text
    assert "SENSITIVE_MESSAGE" not in caplog.text
    assert bot.config.telegram_token not in caplog.text
    assert recorded == ["local", "hook", "runtime", "bridge", "polling"]
    assert [call.args[0] for call in bot._wait_webchat_retry.await_args_list] == delays
    assert log_runtime.link.report_failure.call_count == failures
    log_runtime.link.record.assert_called_once_with(up=True)
    initialize.assert_awaited_once_with(bot)
    start_runtime.assert_awaited_once_with(bot)
    start_listener.assert_awaited_once_with(bot, log_runtime)
    await bot._webchat_stop()
    await bot._webchat_stop()
    stop.assert_awaited_once()


@pytest.mark.parametrize(
    "failure",
    [
        TelegramNetworkError(MagicMock(), "offline"),
        TelegramServerError(MagicMock(), "server failed"),
        TelegramRetryAfter(MagicMock(), "retry", 1),
        ConnectionError(),
        TimeoutError(),
    ],
)
async def test_transient_errors_retry(make_bot, failure):
    bot = make_bot()
    bot._webchat_local_start = AsyncMock()
    bot._wait_webchat_retry = AsyncMock()
    bot._bot.delete_webhook.side_effect = [failure, None]
    assert await bot.run() == 0
    bot._wait_webchat_retry.assert_awaited_once_with(5)
    bot._dp.start_polling.assert_awaited_once()


async def test_nontransient_errors_propagate(make_bot):
    bot = make_bot()
    bot._webchat_local_start = AsyncMock()
    bot._wait_webchat_retry = AsyncMock()
    original = TelegramBadRequest(MagicMock(), "invalid request")
    bot._bot.delete_webhook.side_effect = original
    with pytest.raises(TelegramBadRequest) as caught:
        await bot.run()
    assert caught.value is original
    bot._wait_webchat_retry.assert_not_awaited()
    bot._dp.start_polling.assert_not_awaited()


async def test_request_stop_interrupts_connection_backoff(make_bot):
    bot = make_bot()
    bot._webchat_local_start = AsyncMock()
    failed = asyncio.Event()

    async def delete(**_kwargs):
        failed.set()
        raise TimeoutError

    bot._bot.delete_webhook.side_effect = delete
    running = asyncio.create_task(bot.run())
    await asyncio.wait_for(failed.wait(), timeout=1)
    bot.request_stop()
    assert await asyncio.wait_for(running, timeout=1) == 0
    bot._dp.start_polling.assert_not_awaited()
    assert bot._bot.delete_webhook.await_count == 1


async def test_invalid_web_settings_keep_original_startup_path(make_bot, caplog):
    bot = make_bot(valid=False)
    assert bot._webchat_active is False
    original = TelegramNetworkError(MagicMock(), "offline")
    bot._bot.delete_webhook.side_effect = original
    with pytest.raises(TelegramNetworkError):
        await bot.run()
    assert caplog.text.count("Webchat disabled:") == 1
    assert "domain_invalid" in caplog.text
    assert bot._orchestrator is None


async def test_subagent_never_uses_web_startup(make_bot):
    bot = make_bot(agent_name="worker")
    assert bot._webchat_active is False
    bot._webchat_local_start = AsyncMock()
    await bot.run()
    bot._webchat_local_start.assert_not_awaited()


async def test_bridge_start_failure_leaves_runtime_available(make_bot, caplog):
    bot = make_bot()
    log_runtime = runtime()
    with (
        patch("ductor_bot.messenger.telegram.startup.run_local_startup", new_callable=AsyncMock),
        patch.object(ChatLogRuntime, "start", return_value=log_runtime),
        patch(
            "ductor_bot.webchat.bridge.start_bridge", side_effect=OSError("bind failed")
        ) as listener,
    ):
        await bot._webchat_local_start()
        await bot._webchat_local_start()
    assert bot._webchat_local_started is True
    assert bot._webchat_bridge_stop is None
    assert "Webchat bridge failed to start" in caplog.text
    listener.assert_awaited_once()
    log_runtime.stop.assert_not_awaited()


async def test_local_startup_tolerates_notices_without_telegram_calls(make_bot):
    bot = make_bot()
    orch = MagicMock()
    orch.paths = DuctorPaths(ductor_home=Path(bot.config.ductor_home))
    orch._sessions.list_all = AsyncMock(return_value=[])
    with (
        patch.object(Orchestrator, "create", new_callable=AsyncMock, return_value=orch) as create,
        patch(
            "ductor_bot.messenger.telegram.startup._handle_restart_sentinel",
            side_effect=TimeoutError(),
        ) as sentinel,
        patch(
            "ductor_bot.messenger.telegram.startup._handle_recovery", side_effect=TimeoutError()
        ) as recovery,
        patch.object(bot._bot, "get_me", new_callable=AsyncMock) as get_me,
        patch.object(bot, "_sync_commands", new_callable=AsyncMock) as commands,
        patch.object(bot, "audit_groups", new_callable=AsyncMock) as audit,
    ):
        actions = await run_local_startup(bot)
        assert await run_local_startup(bot) == []
        sentinel.assert_not_awaited()
        recovery.assert_not_awaited()
        await run_local_notices(bot, actions)
    create.assert_awaited_once()
    sentinel.assert_awaited_once()
    recovery.assert_awaited_once_with(bot, None, actions=actions)
    get_me.assert_not_awaited()
    commands.assert_not_awaited()
    audit.assert_not_awaited()
    assert bot._orchestrator is orch
    orch.wire_observers_to_bus.assert_called_once()


async def test_regular_primary_startup_keeps_notice_error_behavior(make_bot):
    bot = make_bot(enabled=False)
    orch = MagicMock()
    orch.paths = DuctorPaths(ductor_home=Path(bot.config.ductor_home))
    orch._sessions.list_all = AsyncMock(return_value=[])
    original = TimeoutError()
    with (
        patch.object(Orchestrator, "create", new_callable=AsyncMock, return_value=orch),
        patch(
            "ductor_bot.messenger.telegram.startup._handle_restart_sentinel", side_effect=original
        ),
        pytest.raises(TimeoutError) as caught,
    ):
        await _run_primary_startup(bot)
    assert caught.value is original


async def test_shutdown_bridge_before_runtime_and_orchestrator(make_bot):
    bot = make_bot()
    order = []

    async def record(name):
        order.append(name)

    bot._stop_polling_owned = AsyncMock(side_effect=lambda _budget: record("polling"))

    # AsyncMock returns its side effect's result; these callbacks must await their recorder.
    async def polling(_budget):
        await record("polling")

    async def bridge():
        await record("bridge")

    async def stop_runtime():
        await record("runtime")

    async def stop_orch(_budget):
        await record("orchestrator")

    bot._stop_polling_owned = AsyncMock(side_effect=polling)
    bot._webchat_bridge_stop = bridge
    bot._chat_log_runtime = SimpleNamespace(stop=stop_runtime)
    bot._orchestrator = SimpleNamespace(run_pre_shutdown_hooks=AsyncMock(), shutdown=stop_orch)
    bot._secret_input.close = AsyncMock()
    await bot.shutdown()
    assert order == ["polling", "bridge", "runtime", "orchestrator"]


async def test_startup_hook_wrapper_is_once_when_web_disabled(make_bot):
    bot = make_bot(enabled=False)
    hook = AsyncMock()
    bot.register_startup_hook(hook)
    await bot._extra_startup_hooks[0]()
    await bot._extra_startup_hooks[0]()
    hook.assert_awaited_once()


async def test_bridge_starts_while_telegram_startup_notice_is_blocked(make_bot):
    bot = make_bot()
    orch = MagicMock()
    orch.paths = DuctorPaths(ductor_home=Path(bot.config.ductor_home))
    orch._sessions.list_all = AsyncMock(return_value=[])
    orch.named_sessions.pop_recovered_running.return_value = []
    orch.inflight_tracker.list_all.return_value = []
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_send(**_kwargs):
        entered.set()
        await release.wait()
        raise TelegramNetworkError(MagicMock(), "Synthetic blackholed Telegram endpoint")

    bot._bot.send_message = AsyncMock(side_effect=slow_send)
    listener = AsyncMock(return_value=AsyncMock())
    with (
        patch.object(Orchestrator, "create", new_callable=AsyncMock, return_value=orch),
        patch.object(ChatLogRuntime, "start", new_callable=AsyncMock, return_value=runtime()),
        patch("ductor_bot.webchat.bridge.start_bridge", listener),
    ):
        starting = asyncio.create_task(bot._webchat_local_start())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            assert listener.await_count == 1
            assert not starting.done()
        finally:
            release.set()
            await asyncio.wait_for(starting, 5)


async def test_startup_recovery_keeps_new_web_turn_inflight(make_bot):
    """Only pre-bridge recovery records may be cleared and reported as interrupted."""
    bot = make_bot()
    orch = Orchestrator(bot.config, DuctorPaths(Path(bot.config.ductor_home)))

    def inflight(topic_id, preview):
        return InflightTurn(
            chat_id=123,
            topic_id=topic_id,
            provider="claude",
            model="test-model",
            session_id="synthetic",
            prompt_preview=preview,
            started_at=datetime.now(UTC).isoformat(),
            is_recovery=False,
            path="normal",
        )

    orch.inflight_tracker.begin(inflight(6, "OLD_BOOT_TURN"))
    notification_started, notification_release = asyncio.Event(), asyncio.Event()
    turn_started, turn_release = asyncio.Event(), asyncio.Event()
    messages = []

    async def send(**kwargs):
        messages.append(kwargs["text"])
        if len(messages) == 1:
            notification_started.set()
            await notification_release.wait()
        return SimpleNamespace(message_id=len(messages))

    async def active_turn(_dispatch):
        orch.inflight_tracker.begin(inflight(7, "NEW_WEB_TURN"))
        turn_started.set()
        await turn_release.wait()
        return OrchestratorResult(text="completed")

    orch._route_message = active_turn
    bot._bot.send_message = AsyncMock(side_effect=send)
    listener = AsyncMock(return_value=AsyncMock())
    starting = running = None
    try:
        with (
            patch.object(Orchestrator, "create", new_callable=AsyncMock, return_value=orch),
            patch.object(ChatLogRuntime, "start", new_callable=AsyncMock, return_value=runtime()),
            patch("ductor_bot.webchat.bridge.start_bridge", listener),
        ):
            starting = asyncio.create_task(bot._webchat_local_start())
            await asyncio.wait_for(notification_started.wait(), 2)
            listener.assert_awaited_once()
            running = asyncio.create_task(
                orch.handle_message_streaming(SessionKey.telegram(123, 7), "new web input")
            )
            await asyncio.wait_for(turn_started.wait(), 2)
            notification_release.set()
            await asyncio.wait_for(starting, 2)
            assert not running.done()
            assert orch.inflight_tracker.active_scopes() == [(123, 7)]
            interrupted = InflightTracker(orch.paths.inflight_turns_path).load_interrupted(
                max_age_seconds=3600
            )
            assert [turn.prompt_preview for turn in interrupted] == ["NEW_WEB_TURN"]
            assert any("OLD_BOOT_TURN" in message for message in messages)
            assert not any("NEW_WEB_TURN" in message for message in messages)
    finally:
        notification_release.set()
        turn_release.set()
        await asyncio.gather(
            *(task for task in (starting, running) if task), return_exceptions=True
        )


@pytest.mark.parametrize(
    ("options", "reason"),
    [
        ({"enabled": False}, "disabled"),
        ({"valid": False}, "domain_invalid"),
        ({"agent_name": "worker"}, "agent_not_main"),
    ],
)
async def test_webchat_skipped_info_once_without_secrets(make_bot, caplog, options, reason):
    caplog.set_level(logging.INFO, logger="ductor_bot.messenger.telegram.app")
    bot = make_bot(**options)
    assert [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.INFO
        and record.getMessage().startswith("Webchat startup skipped:")
    ] == [f"Webchat startup skipped: {reason}"]
    assert bot.config.telegram_token not in caplog.text
    assert bot.config.webchat.acme_email not in caplog.text
