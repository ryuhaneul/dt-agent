"""Bus delivery storage through the real detailed Telegram sender."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)

from ductor_bot.bus.bus import MessageBus
from ductor_bot.bus.envelope import Envelope, Origin
from ductor_bot.chat_log.events import EventHub
from ductor_bot.chat_log.link import OutboxDrainer, TelegramLinkMonitor
from ductor_bot.chat_log.outbox import Outbox
from ductor_bot.messenger.telegram import sender
from ductor_bot.messenger.telegram.transport import TelegramTransport
from ductor_bot.messenger.telegram.webchat_ops import TelegramWebchatOps


def make_transport(tmp_path):
    bot = MagicMock()
    bot.bot_instance.send_message = AsyncMock(return_value=SimpleNamespace(message_id=1))
    bot.file_roots.return_value = [tmp_path]
    bot._config.allowed_user_ids = [42, 43]
    bot.broadcast = AsyncMock()
    transport = TelegramTransport(bot)
    outbox = Outbox(tmp_path / "outbox")
    failure = MagicMock()
    transport.set_outbox(outbox, on_failure=failure)
    return transport, bot, outbox, failure


def task_result():
    return Envelope(
        origin=Origin.TASK_RESULT,
        chat_id=42,
        topic_id=7,
        result_text="agent response",
        prompt="INTERNAL_INJECTION",
        status="done",
        needs_injection=True,
        metadata={"name": "Task"},
    )


@pytest.mark.parametrize("first_fails", [False, True])
async def test_task_parts_preserve_order_and_history_id(tmp_path, first_fails):
    transport, bot, outbox, failure = make_transport(tmp_path)
    error = TelegramNetworkError(MagicMock(), "offline")
    bot.bot_instance.send_message.side_effect = (
        [error] if first_fails else [SimpleNamespace(message_id=1), error]
    )
    env = task_result()
    await transport.deliver(env)
    items = await outbox.pending()
    assert len(items) == (2 if first_fails else 1)
    assert items[-1].text == "agent response"
    assert all(i.history_id == env.metadata["chat_log_entry_id"] for i in items)
    assert all(i.topic_id == 7 and i.origin == "task_result" and i.title == "Task" for i in items)
    assert all("INTERNAL_INJECTION" not in i.text for i in items)
    if first_fails:
        assert "Task" in items[0].text
    assert env.delivered
    assert env.delivery_error == ""
    failure.assert_called_once_with()


async def test_pending_prevents_overtaking_and_disable_restores_legacy(tmp_path):
    transport, bot, outbox, failure = make_transport(tmp_path)
    await outbox.enqueue(kind="bus", chat_id=42, text="older")
    env = Envelope(origin=Origin.WEBHOOK_WAKE, chat_id=42, result_text="new")
    await transport.deliver(env)
    assert [i.text for i in await outbox.pending()] == ["older", "new"]
    bot.bot_instance.send_message.assert_not_awaited()
    failure.assert_not_called()
    transport.set_outbox(None)
    with patch("ductor_bot.messenger.telegram.transport.send_rich", new_callable=AsyncMock) as send:
        await transport.deliver(env)
        send.assert_awaited_once()
    assert len(await outbox.pending()) == 2


@pytest.mark.parametrize(
    "error",
    [
        TelegramNetworkError(MagicMock(), "offline"),
        TelegramServerError(MagicMock(), "server"),
        TelegramRetryAfter(MagicMock(), "busy", retry_after=1),
        TimeoutError(),
    ],
)
async def test_real_sender_partial_transient_is_stored(tmp_path, monkeypatch, error):
    transport, bot, outbox, failure = make_transport(tmp_path)
    monkeypatch.setattr(sender, "_ia_text_chunks", lambda _: ["first", "second", "third"])
    bot.bot_instance.send_message.side_effect = [SimpleNamespace(message_id=1), error]
    env = Envelope(origin=Origin.WEBHOOK_WAKE, chat_id=42, result_text="report")
    await transport.deliver(env)
    [item] = await outbox.pending()
    assert item.sent_chunks == 1
    assert item.text == "report"
    assert env.delivered
    failure.assert_called_once_with()


@pytest.mark.parametrize("error_class", [TelegramBadRequest, TelegramForbiddenError])
async def test_permanent_raises_original_and_bus_marks_failure(tmp_path, error_class):
    transport, bot, outbox, failure = make_transport(tmp_path)
    error = error_class(MagicMock(), "target missing")
    bot.bot_instance.send_message.side_effect = error
    env = Envelope(origin=Origin.WEBHOOK_WAKE, chat_id=42, result_text="report")
    with pytest.raises(error_class) as caught:
        await transport.deliver(env)
    assert caught.value is error
    bus = MessageBus()
    bus.register_transport(transport)
    await bus._deliver(env)
    assert not env.delivered
    assert await outbox.pending() == []
    failure.assert_not_called()


@pytest.mark.parametrize("origin", [Origin.CRON, Origin.HEARTBEAT])
@pytest.mark.parametrize("permanent", [False, True])
async def test_fallback_only_on_permanent_failure(tmp_path, origin, permanent):
    transport, bot, outbox, failure = make_transport(tmp_path)
    error_class = TelegramForbiddenError if permanent else TelegramNetworkError
    error = error_class(MagicMock(), "offline")
    bot.bot_instance.send_message.side_effect = error
    env = Envelope(origin=origin, chat_id=99, topic_id=7, result_text="report", status="success")
    with patch(
        "ductor_bot.messenger.telegram.transport.send_rich", new_callable=AsyncMock
    ) as fallback:
        await transport.deliver(env)
        assert fallback.await_count == int(permanent)
        if permanent:
            assert fallback.await_args.args[1] == 42
    assert len(await outbox.pending()) == int(not permanent)
    assert failure.call_count == int(not permanent)
    if not permanent:
        assert env.delivered
        assert env.delivery_error == ""


async def test_real_html_retry_succeeds_without_queue(tmp_path):
    transport, bot, outbox, _ = make_transport(tmp_path)
    bot.bot_instance.send_message.side_effect = [
        TelegramBadRequest(MagicMock(), "bad HTML"),
        SimpleNamespace(message_id=1),
    ]
    await transport.deliver(
        Envelope(origin=Origin.WEBHOOK_WAKE, chat_id=42, result_text="**report**")
    )
    assert await outbox.pending() == []
    assert bot.bot_instance.send_message.await_count == 2


async def test_background_stores_original_buttons_and_session(tmp_path):
    transport, bot, outbox, _ = make_transport(tmp_path)
    bot.bot_instance.send_message.side_effect = TelegramNetworkError(MagicMock(), "offline")
    env = Envelope(
        origin=Origin.BACKGROUND,
        chat_id=42,
        session_name="analysis",
        result_text="done [button:Next]",
    )
    await transport.deliver(env)
    [item] = await outbox.pending()
    assert item.buttons_session == "analysis"
    assert "[button:Next]" in item.text
    assert bot.bot_instance.send_message.await_args.kwargs["reply_markup"] is not None


@pytest.mark.parametrize("origin", [Origin.CRON, Origin.WEBHOOK_CRON])
async def test_broadcast_stores_each_recipient(tmp_path, origin):
    transport, bot, outbox, failure = make_transport(tmp_path)
    bot.bot_instance.send_message.side_effect = TelegramNetworkError(MagicMock(), "offline")
    env = Envelope(origin=origin, chat_id=0, result_text="report", status="success")
    await transport.deliver_broadcast(env)
    items = await outbox.pending()
    assert [i.chat_id for i in items] == [42, 43]
    assert items[0].text == items[1].text
    assert items[0].history_id == items[1].history_id
    assert env.delivered
    bot.broadcast.assert_not_awaited()
    failure.assert_called_once_with()


async def test_exclusions_and_unavailable_file(tmp_path):
    transport, bot, outbox, _ = make_transport(tmp_path)
    bot.bot_instance.send_message.side_effect = TelegramNetworkError(MagicMock(), "offline")
    await transport.deliver(Envelope(origin=Origin.INTERAGENT, chat_id=42, result_text="report"))
    await transport.deliver(Envelope(origin=Origin.WEBHOOK_WAKE, chat_id=42, result_text=""))
    await transport.deliver(
        Envelope(
            origin=Origin.CRON,
            chat_id=42,
            status="success",
            result_text='Message sent successfully. "Hi" delivered to Telegram (id 5).',
        )
    )
    assert await outbox.pending() == []
    assert bot.bot_instance.send_message.await_count == 1
    await transport.deliver(
        Envelope(
            origin=Origin.WEBHOOK_WAKE, chat_id=42, result_text=f"<file:{tmp_path / 'missing.txt'}>"
        )
    )
    [item] = await outbox.pending()
    assert item.sent_chunks == 0
    assert item.text == f"<file:{tmp_path / 'missing.txt'}>"
    assert bot.bot_instance.send_message.await_count == 2


async def test_order_overtake_serializes_immediate_send_and_drain(tmp_path):
    """Concurrent B waits for all confirmed and queued chunks of A."""
    from ductor_bot.bus.envelope import LockMode
    from ductor_bot.chat_log.events import EventHub
    from ductor_bot.chat_log.link import OutboxDrainer, TelegramLinkMonitor
    from ductor_bot.messenger.telegram.webchat_ops import TelegramWebchatOps

    transport, bot, outbox, _ = make_transport(tmp_path)
    blocked, release = asyncio.Event(), asyncio.Event()
    successful = []
    failed = False

    async def send(**kwargs):
        nonlocal failed
        text = kwargs["text"]
        if text.startswith("A") and len(text) < 4096 and not failed:
            blocked.set()
            await release.wait()
            failed = True
            raise TelegramNetworkError(MagicMock(), "Synthetic outage")
        successful.append("B" if text == "B" else "A")
        return SimpleNamespace(message_id=len(successful))

    bot.bot_instance.send_message.side_effect = send
    bus = MessageBus()
    bus.register_transport(transport)
    first = asyncio.create_task(
        bus.submit(
            Envelope(
                origin=Origin.CRON,
                chat_id=42,
                topic_id=7,
                result_text="A" * 5000,
                status="success",
                lock_mode=LockMode.NONE,
            )
        )
    )
    await asyncio.wait_for(blocked.wait(), 2)
    second = asyncio.create_task(
        bus.submit(
            Envelope(
                origin=Origin.WEBHOOK_WAKE,
                chat_id=42,
                topic_id=7,
                result_text="B",
                lock_mode=LockMode.NONE,
            )
        )
    )
    # Let B enter delivery while A's final chunk is still in flight.
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, second)
    ops = TelegramWebchatOps(bot)
    drainer = OutboxDrainer(outbox, ops, TelegramLinkMonitor(ops, EventHub()))
    draining = asyncio.create_task(drainer.drain())
    third = asyncio.create_task(
        bus.submit(
            Envelope(
                origin=Origin.WEBHOOK_WAKE,
                chat_id=42,
                topic_id=7,
                result_text="B",
                lock_mode=LockMode.NONE,
            )
        )
    )
    await asyncio.gather(draining, third)
    await drainer.drain()
    assert successful == ["A", "A", "B", "B"]
    assert not await outbox.has_pending()


async def test_disabled_outbox_uses_legacy_sender_without_delivery_lock(tmp_path):
    transport, bot, outbox, _ = make_transport(tmp_path)
    transport.set_outbox(None)
    with patch(
        "ductor_bot.messenger.telegram.transport.send_rich", new_callable=AsyncMock
    ) as legacy:
        await transport.deliver(
            Envelope(
                origin=Origin.WEBHOOK_WAKE, chat_id=42, topic_id=7, result_text="Legacy result"
            )
        )
    legacy.assert_awaited_once()
    assert legacy.await_args.args[:3] == (bot.bot_instance, 42, "Legacy result")
    assert not await outbox.has_pending()


@pytest.mark.parametrize("blocked", [False, True])
async def test_unavailable_file_does_not_skip_later_attachment(tmp_path, blocked):
    """Match legacy missing-file notices and successful attachment delivery."""
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    good = allowed / "good.txt"
    good.write_text("Synthetic report")
    unavailable = tmp_path / "outside.txt" if blocked else allowed / "missing.txt"
    if blocked:
        unavailable.write_text("Blocked file")
    counts = []
    messages = []
    for enabled in (False, True):
        transport, bot, outbox, _ = make_transport(tmp_path)
        bot.file_roots.return_value = [allowed]
        bot.bot_instance.send_document = AsyncMock(return_value=SimpleNamespace(message_id=2))
        if not enabled:
            transport.set_outbox(None)
        await transport.deliver(
            Envelope(
                origin=Origin.WEBHOOK_WAKE,
                chat_id=42,
                result_text=f"Synthetic result <file:{unavailable}> <file:{good}>",
            )
        )
        counts.append(bot.bot_instance.send_document.await_count)
        messages.append(
            [call.kwargs["text"] for call in bot.bot_instance.send_message.await_args_list]
        )
        assert not await outbox.has_pending()
    assert counts == [1, 1]
    assert messages[0] == messages[1]


async def test_unavailable_file_notice_progress_survives_cancelled_drain(tmp_path):
    """Persist the skipped-file notice before the next attachment is attempted."""
    _, bot, outbox, _ = make_transport(tmp_path)
    good = tmp_path / "good.txt"
    good.write_text("Synthetic report")
    await outbox.enqueue(
        kind="bus", chat_id=42, text=f"<file:{tmp_path / 'missing.txt'}> <file:{good}>"
    )
    entered = asyncio.Event()

    async def blocked_upload(**_kwargs):
        entered.set()
        await asyncio.Event().wait()

    bot.bot_instance.send_document = AsyncMock(side_effect=blocked_upload)
    ops = TelegramWebchatOps(bot)
    drainer = OutboxDrainer(outbox, ops, TelegramLinkMonitor(ops, EventHub()))
    task = asyncio.create_task(drainer.drain())
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    [saved] = await outbox.pending()
    assert saved.sent_chunks == 1
    bot.bot_instance.send_message.assert_awaited_once()
    bot.bot_instance.send_document.reset_mock(side_effect=True)
    bot.bot_instance.send_document.return_value = SimpleNamespace(message_id=2)
    reopened = Outbox(outbox.directory)
    await OutboxDrainer(reopened, ops, TelegramLinkMonitor(ops, EventHub())).drain()
    assert not await reopened.has_pending()
    bot.bot_instance.send_message.assert_awaited_once()
    bot.bot_instance.send_document.assert_awaited_once()


@pytest.mark.parametrize("immediate", [False, True])
async def test_web_mirrors_follow_delivery_lock_order(tmp_path, immediate):
    """Both immediate delivery and draining finish older chunks before web mirrors."""
    from ductor_bot.bus.envelope import LockMode
    from ductor_bot.chat_log.history import HistoryStore
    from ductor_bot.config import AgentConfig
    from ductor_bot.orchestrator.registry import OrchestratorResult
    from ductor_bot.webchat.turns import WebTurns
    from ductor_bot.workspace.paths import DuctorPaths

    transport, bot, outbox, _ = make_transport(tmp_path)
    bot.config = AgentConfig(allowed_user_ids=[42])
    bot._orch.paths = DuctorPaths(tmp_path)
    bot._orch.handle_message_streaming = AsyncMock(return_value=OrchestratorResult(text="reply-B"))
    bot._orch._process_registry = SimpleNamespace(
        was_aborted=lambda *_: False, was_aborted_topic=lambda *_: False
    )
    events = EventHub()
    active = SimpleNamespace(
        outbox=outbox,
        history=HistoryStore(tmp_path / "history"),
        events=events,
        drainer=SimpleNamespace(wake=MagicMock()),
    )
    waiting, release, mirror_waiting = asyncio.Event(), asyncio.Event(), asyncio.Event()
    confirmed = []
    failed = False

    async def send(**kwargs):
        nonlocal failed
        text = kwargs["text"]
        if text.startswith("A") and len(text) < 4096 and not failed:
            waiting.set()
            await release.wait()
            failed = True
            raise TelegramNetworkError(MagicMock(), "Synthetic outage")
        confirmed.append(
            "A" if text.startswith("A") else "web-B" if text.endswith("\nB") else "web-reply"
        )
        return SimpleNamespace(message_id=len(confirmed))

    bot.bot_instance.send_message.side_effect = send
    bus = MessageBus()
    bus.register_transport(transport)
    bot._orch._lock_pool = bus.lock_pool
    ops = TelegramWebchatOps(bot)
    if immediate:
        older = asyncio.create_task(
            bus.submit(
                Envelope(
                    origin=Origin.WEBHOOK_WAKE,
                    chat_id=42,
                    topic_id=7,
                    result_text="A" * 5000,
                    lock_mode=LockMode.NONE,
                )
            )
        )
    else:
        await outbox.enqueue(kind="reply_mirror", chat_id=42, topic_id=7, text="A" * 5000)
        older = asyncio.create_task(
            OutboxDrainer(outbox, ops, TelegramLinkMonitor(ops, events)).drain()
        )
    await asyncio.wait_for(waiting.wait(), 2)
    acquire = outbox.delivery_lock.acquire

    async def observed_acquire():
        if asyncio.current_task().get_name() == "webchat-turn":
            mirror_waiting.set()
        return await acquire()

    outbox.delivery_lock.acquire = observed_acquire
    turns = WebTurns(bot, active)
    turns.submit("42.7", "B")
    newer = turns._tasks["42.7"]
    observed = asyncio.create_task(mirror_waiting.wait())
    try:
        await asyncio.wait_for(
            asyncio.wait({observed, newer}, return_when=asyncio.FIRST_COMPLETED), 2
        )
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(older, newer), 2)
        observed.cancel()
        await asyncio.gather(observed, return_exceptions=True)
        await turns.stop()
    queued = [item.kind for item in await outbox.pending()]
    assert queued == ["bus" if immediate else "reply_mirror", "user_mirror", "reply_mirror"]
    await OutboxDrainer(outbox, ops, TelegramLinkMonitor(ops, events)).drain()
    assert confirmed == ["A", "A", "web-B", "web-reply"]


@pytest.mark.parametrize("sent_chunks", [0, 1])
async def test_initial_retry_after_defers_queue_despite_healthy_probe(
    tmp_path, monkeypatch, sent_chunks
):
    transport, bot, outbox, _ = make_transport(tmp_path)
    ops = TelegramWebchatOps(bot)
    link = TelegramLinkMonitor(ops, EventHub())
    drainer = OutboxDrainer(outbox, ops, link)
    transport.set_outbox(
        outbox, on_failure=link.report_failure, on_retry_after=drainer.defer_retry
    )
    now = 100.0
    monkeypatch.setattr("ductor_bot.chat_log.link.time.monotonic", lambda: now)
    monkeypatch.setattr(sender, "_ia_text_chunks", lambda _: ["first", "second"])
    bot.bot_instance.get_me = AsyncMock(return_value=SimpleNamespace(id=1))
    bot.bot_instance.send_message.side_effect = [
        *[SimpleNamespace(message_id=1)] * sent_chunks,
        TelegramRetryAfter(MagicMock(), "busy", retry_after=70),
        SimpleNamespace(message_id=2),
        SimpleNamespace(message_id=3),
    ]
    await transport.deliver(
        Envelope(origin=Origin.WEBHOOK_WAKE, chat_id=42, result_text="report")
    )
    [item] = await outbox.pending()
    assert item.sent_chunks == sent_chunks
    assert item.attempts == 0
    assert link.state()["state"] == "down"
    for now in (101.0, 169.0):
        await link.probe_once()
        await drainer.drain()
        assert bot.bot_instance.send_message.await_count == sent_chunks + 1
        [item] = await outbox.pending()
        assert item.attempts == 0
    now = 170.0
    await drainer.drain()
    assert bot.bot_instance.send_message.await_count == 3
    assert not await outbox.has_pending()


async def test_unreadable_pending_head_queues_bus_delivery_without_overtaking(tmp_path):
    transport, bot, outbox, _ = make_transport(tmp_path)
    first = await outbox.enqueue(kind="bus", chat_id=42, text="first")
    load = outbox._load

    def unreadable(path):
        if path.name.endswith(f"-{first.id}.json"):
            raise PermissionError("read unavailable")
        return load(path)

    env = Envelope(origin=Origin.WEBHOOK_WAKE, chat_id=42, result_text="second")
    with patch.object(outbox, "_load", side_effect=unreadable):
        await transport.deliver(env)
    assert env.delivered
    assert env.delivery_error == ""
    bot.bot_instance.send_message.assert_not_awaited()
    assert [item.text for item in await outbox.pending()] == ["first", "second"]
