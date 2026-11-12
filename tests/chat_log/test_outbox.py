# ruff: noqa: INP001
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import ANY, patch

import pytest

from ductor_bot.chat_log.events import EventHub
from ductor_bot.chat_log.outbox import Outbox


async def test_restart_order_and_clock_regression(tmp_path: Path) -> None:
    outbox = Outbox(tmp_path)
    with patch("ductor_bot.chat_log.outbox.time.time_ns", return_value=123):
        items = [
            await outbox.enqueue(
                kind="bus",
                chat_id=-123,
                topic_id=7,
                text=str(index),
                origin="background",
                title="Example",
                history_id="history",
                buttons_session="named-example",
            )
            for index in range(3)
        ]
        restarted = Outbox(tmp_path)
        last = await restarted.enqueue(kind="reply_mirror", chat_id=1, text="last")
    assert await restarted.pending() == [*items, last]
    assert await restarted.has_pending()
    names = sorted((tmp_path / "pending").glob("*.json"))
    assert [path.name.split("-", 1)[0] for path in names] == [
        f"{value:020d}" for value in range(123, 127)
    ]
    assert items[0].buttons_session == "named-example"
    assert items[0].tid == "-123.7"
    assert json.loads(names[0].read_text())["history_id"] == "history"


async def test_progress_and_terminal_states(tmp_path: Path) -> None:
    outbox = Outbox(tmp_path)
    first = await outbox.enqueue(kind="bus", chat_id=1, text="three chunks", history_id="h")
    second = await outbox.enqueue(kind="user_mirror", chat_id=1, text="next", history_id="h")
    await outbox.mark_progress(first.id, sent_chunks=1, attempts=1, last_error="TimeoutError")
    restarted = Outbox(tmp_path)
    items = await restarted.pending()
    assert [item.id for item in items] == [first.id, second.id]
    assert (items[0].sent_chunks, items[0].attempts, items[0].last_error) == (1, 1, "TimeoutError")
    with pytest.raises(ValueError, match="backwards"):
        await restarted.mark_progress(first.id, sent_chunks=0, attempts=2)
    assert await restarted.mirror_state("h") == "pending"
    await restarted.mark_sent(first.id)
    assert await restarted.mirror_state("h") == "pending"
    await restarted.mark_failed(second.id)
    assert await restarted.mirror_state("h") == "failed"
    assert not await restarted.has_pending()
    assert len(list((tmp_path / "done").glob("*.json"))) == 2
    assert (await restarted.views())[0]["last_error"] == "send_failed"
    assert await restarted.mirror_state("missing") is None


async def test_drop_topic_and_mirror_aggregation(tmp_path: Path) -> None:
    outbox = Outbox(tmp_path)
    first = await outbox.enqueue(kind="bus", chat_id=1, topic_id=2, text="one", history_id="h")
    await outbox.enqueue(kind="reply_mirror", chat_id=1, topic_id=3, text="two", history_id="other")
    await outbox.mark_sent(first.id)
    assert await outbox.mirror_state("h") == "sent"
    await outbox.enqueue(kind="bus", chat_id=1, topic_id=2, text="three", history_id="h")
    assert await outbox.mirror_state("h") == "pending"
    assert await outbox.drop_tid("1.2") == 1
    assert await outbox.drop_tid("1.2") == 0
    assert await outbox.mirror_state("h") == "failed"
    assert [item.tid for item in await outbox.pending()] == ["1.3"]


async def test_prune_and_recent_failed_views(tmp_path: Path) -> None:
    outbox = Outbox(tmp_path)
    for state in ("failed", "sent", "dropped"):
        item = await outbox.enqueue(kind="bus", chat_id=1, text="body")
        if state == "failed":
            await outbox.mark_failed(item.id)
        elif state == "sent":
            await outbox.mark_sent(item.id)
        else:
            await outbox.drop_tid("1")
    assert len(await outbox.views()) == 1
    for path in (tmp_path / "done").glob("*.json"):
        data = json.loads(path.read_text())
        data["updated"] = "2000-01-01T00:00:00Z"
        path.write_text(json.dumps(data))
    pending = await outbox.enqueue(kind="bus", chat_id=1, text="keep pending")
    assert [view["id"] for view in await outbox.views()] == [pending.id]
    assert await outbox.prune() == 3
    assert [item.id for item in await outbox.pending()] == [pending.id]
    assert await outbox.prune() == 0


async def test_events_and_read_only_initialization(tmp_path: Path) -> None:
    hub = EventHub()
    queue = hub.subscribe()
    root = tmp_path / "absent"
    outbox = Outbox(root, hub)
    assert not await outbox.has_pending()
    assert await outbox.views() == []
    assert await outbox.prune() == 0
    assert not root.exists()
    item = await outbox.enqueue(kind="bus", chat_id=1, text="x" * 100, origin="cron", title="job")
    event = await queue.get()
    assert event["pending"] == 1
    assert event["item"]["preview"] == "x" * 80
    await outbox.mark_sent(item.id)
    assert (await queue.get())["pending"] == 0


async def test_atomic_failure_preserves_progress(tmp_path: Path) -> None:
    outbox = Outbox(tmp_path)
    item = await outbox.enqueue(kind="bus", chat_id=1, text="body")
    with (
        patch("ductor_bot.chat_log.outbox.Path.replace", side_effect=OSError("replace failed")),
        pytest.raises(OSError, match="replace failed"),
    ):
        await outbox.mark_progress(item.id, sent_chunks=1, attempts=1)
    assert (await Outbox(tmp_path).pending())[0].sent_chunks == 0
    assert not list((tmp_path / "pending").glob("*.tmp"))


async def test_interrupted_terminal_transition_is_recovered(tmp_path: Path) -> None:
    outbox = Outbox(tmp_path)
    item = await outbox.enqueue(kind="bus", chat_id=1, text="body")
    with (
        patch.object(outbox, "_move_done", side_effect=OSError("interrupted")),
        pytest.raises(OSError, match="interrupted"),
    ):
        await outbox.mark_sent(item.id)
    restarted = Outbox(tmp_path)
    assert not await restarted.has_pending()
    assert len(list((tmp_path / "done").glob("*.json"))) == 1


async def test_concurrent_enqueue_and_identifier_validation(tmp_path: Path) -> None:
    outbox = Outbox(tmp_path)
    items = await asyncio.gather(
        *(outbox.enqueue(kind="bus", chat_id=1, text=str(index)) for index in range(30))
    )
    assert await outbox.pending() == items
    with pytest.raises(ValueError, match="identifier"):
        await outbox.mark_sent("../bad")
    with pytest.raises(KeyError):
        await outbox.mark_sent("123456789abc")
    with pytest.raises(ValueError, match="nonnegative"):
        await outbox.enqueue(kind="bus", chat_id=1, text="body", sent_chunks=-1)


# Resumable delivery tests use the same disk format as the storage tests above.


def delivery(outbox, responses):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from ductor_bot.chat_log.link import OutboxDrainer, TelegramLinkMonitor

    ops = SimpleNamespace(
        send_text=AsyncMock(side_effect=responses), probe=AsyncMock(return_value=True)
    )
    link = TelegramLinkMonitor(ops, EventHub())
    return OutboxDrainer(outbox, ops, link), ops, link


def progress(*, delivered=False, chunks=0, failure="transient", error="TimeoutError"):
    from ductor_bot.messenger.telegram.sender import SendProgress

    return SendProgress(
        delivered, chunks, None if delivered else failure, "" if delivered else error, None
    )


async def test_transient_stops_before_next_item_and_restart_resumes_progress(tmp_path):
    outbox = Outbox(tmp_path)
    first = await outbox.enqueue(kind="bus", chat_id=1, text="first")
    await outbox.enqueue(kind="bus", chat_id=1, text="second")
    drainer, ops, link = delivery(outbox, [progress(chunks=1)])
    ops.probe.return_value = False
    await drainer.drain()
    ops.send_text.assert_awaited_once_with(1, None, "first", 0, None, on_chunk=ANY)
    assert link.state()["state"] == "down"
    restarted = Outbox(tmp_path)
    assert (await restarted.pending())[0].sent_chunks == 1
    assert (await restarted.pending())[0].attempts == 1
    drainer, ops, _ = delivery(
        restarted, [progress(delivered=True, chunks=3), progress(delivered=True, chunks=1)]
    )
    await drainer.drain()
    assert [call.args[3] for call in ops.send_text.await_args_list] == [1, 0]
    assert not await restarted.has_pending()
    saved = json.loads(next((tmp_path / "done").glob(f"*-{first.id}.json")).read_text())
    assert (saved["sent_chunks"], saved["attempts"], saved["state"]) == (3, 1, "sent")


async def test_permanent_failure_advances_without_changing_link_state(tmp_path):
    outbox = Outbox(tmp_path)
    first = await outbox.enqueue(kind="bus", chat_id=1, text="first")
    await outbox.enqueue(kind="bus", chat_id=1, text="second")
    drainer, ops, link = delivery(
        outbox,
        [
            progress(failure="permanent", error="TelegramForbiddenError"),
            progress(delivered=True, chunks=1),
        ],
    )
    link.record(up=True, probe=True)
    queue = link._events.subscribe()
    await drainer.drain()
    assert ops.send_text.await_count == 2
    assert queue.qsize() == 1
    assert link.state()["state"] == "up"
    saved = json.loads(next((tmp_path / "done").glob(f"*-{first.id}.json")).read_text())
    assert (saved["state"], saved["last_error"]) == ("failed", "TelegramForbiddenError")


async def test_five_failures_with_successful_probe_fail_item_then_continue(tmp_path):
    outbox = Outbox(tmp_path)
    first = await outbox.enqueue(kind="bus", chat_id=1, text="blocked")
    await outbox.enqueue(kind="bus", chat_id=1, text="next")
    drainer, ops, link = delivery(
        outbox, [progress() for _ in range(5)] + [progress(delivered=True, chunks=1)]
    )
    for index in range(5):
        await link.probe_once()
        await drainer.drain()
        if index < 4:
            assert (await outbox.pending())[0].id == first.id
    assert ops.send_text.await_count == 6
    assert not await outbox.has_pending()
    saved = json.loads(next((tmp_path / "done").glob(f"*-{first.id}.json")).read_text())
    assert (saved["attempts"], saved["last_error"], saved["state"]) == (5, "send_failed", "failed")


async def test_four_failures_then_success_are_sent(tmp_path):
    outbox = Outbox(tmp_path)
    first = await outbox.enqueue(kind="bus", chat_id=1, text="recovering")
    drainer, _, link = delivery(
        outbox, [progress() for _ in range(4)] + [progress(delivered=True, chunks=1)]
    )
    for _ in range(5):
        await link.probe_once()
        await drainer.drain()
    saved = json.loads(next((tmp_path / "done").glob(f"*-{first.id}.json")).read_text())
    assert (saved["attempts"], saved["state"]) == (4, "sent")


async def test_probe_failure_resets_consecutive_count(tmp_path):
    outbox = Outbox(tmp_path)
    first = await outbox.enqueue(kind="bus", chat_id=1, text="recovering")
    drainer, ops, link = delivery(outbox, [progress() for _ in range(8)])
    for _ in range(2):
        await link.probe_once()
        await drainer.drain()
    ops.probe.return_value = False
    await link.probe_once()
    await drainer.drain()
    ops.probe.return_value = True
    for _ in range(4):
        await link.probe_once()
        await drainer.drain()
    assert (await outbox.pending())[0].attempts == 6
    await link.probe_once()
    await drainer.drain()
    saved = json.loads(next((tmp_path / "done").glob(f"*-{first.id}.json")).read_text())
    assert (saved["attempts"], saved["state"]) == (7, "failed")


async def test_three_real_sender_chunks_resume_without_resending_first(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from aiogram.exceptions import TelegramNetworkError

    from ductor_bot.chat_log.link import OutboxDrainer, TelegramLinkMonitor
    from ductor_bot.messenger.telegram.webchat_ops import TelegramWebchatOps

    raw = MagicMock()
    raw.send_message = AsyncMock(
        side_effect=[SimpleNamespace(message_id=1), TelegramNetworkError(MagicMock(), "offline")]
    )
    carrier = SimpleNamespace(
        bot_instance=raw, _orch=SimpleNamespace(paths=tmp_path), file_roots=lambda _: None
    )
    ops = TelegramWebchatOps(carrier)
    monkeypatch.setattr(
        "ductor_bot.messenger.telegram.sender._ia_text_chunks",
        lambda _: ["first", "second", "third"],
    )
    outbox = Outbox(tmp_path / "outbox")
    await outbox.enqueue(kind="reply_mirror", chat_id=1, text="three chunks")
    await OutboxDrainer(outbox, ops, TelegramLinkMonitor(ops, EventHub())).drain()
    assert (await outbox.pending())[0].sent_chunks == 1
    raw.send_message.reset_mock(side_effect=True)
    raw.send_message.side_effect = [SimpleNamespace(message_id=2), SimpleNamespace(message_id=3)]
    restarted = Outbox(tmp_path / "outbox")
    await OutboxDrainer(restarted, ops, TelegramLinkMonitor(ops, EventHub())).drain()
    assert [call.kwargs["text"] for call in raw.send_message.await_args_list] == ["second", "third"]
    assert not await restarted.has_pending()


async def test_named_session_buttons_are_rebuilt_during_drain(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from ductor_bot.chat_log.link import OutboxDrainer, TelegramLinkMonitor
    from ductor_bot.messenger.telegram.webchat_ops import TelegramWebchatOps

    raw = MagicMock()
    raw.send_message = AsyncMock(return_value=SimpleNamespace(message_id=1))
    carrier = SimpleNamespace(
        bot_instance=raw, _orch=SimpleNamespace(paths=tmp_path), file_roots=lambda _: None
    )
    ops = TelegramWebchatOps(carrier)
    outbox = Outbox(tmp_path / "outbox")
    await outbox.enqueue(
        kind="bus",
        chat_id=1,
        topic_id=7,
        text="Report\n[button:Next]",
        buttons_session="named-example",
    )
    await OutboxDrainer(outbox, ops, TelegramLinkMonitor(ops, EventHub())).drain()
    call = raw.send_message.call_args.kwargs
    assert "[button:" not in call["text"]
    assert call["message_thread_id"] == 7
    assert call["reply_markup"].inline_keyboard[0][0].callback_data == "ns:named-example:Next"
    assert not await outbox.has_pending()


async def test_restart_resets_streak_but_keeps_total_attempts(tmp_path):
    outbox = Outbox(tmp_path)
    first = await outbox.enqueue(kind="bus", chat_id=1, text="recovering")
    drainer, _, link = delivery(outbox, [progress() for _ in range(4)])
    for _ in range(4):
        await link.probe_once()
        await drainer.drain()
    restarted = Outbox(tmp_path)
    drainer, _, link = delivery(restarted, [progress() for _ in range(5)])
    for _ in range(4):
        await link.probe_once()
        await drainer.drain()
    assert (await restarted.pending())[0].attempts == 8
    await link.probe_once()
    await drainer.drain()
    saved = json.loads(next((tmp_path / "done").glob(f"*-{first.id}.json")).read_text())
    assert (saved["attempts"], saved["state"]) == (9, "failed")


async def test_recovery_wake_before_send_failure_does_not_bypass_retry_delay(tmp_path):
    outbox = Outbox(tmp_path)
    await outbox.enqueue(kind="bus", chat_id=1, text="recovering")
    drainer, ops, link = delivery(outbox, [])
    link.on_up = drainer.wake

    async def send(*_args, **_kwargs):
        link.record(up=True, probe=True)
        return progress()

    ops.send_text.side_effect = send
    await drainer.drain()
    assert not drainer._wake.is_set()
    link.record(up=False, probe=True)
    link.record(up=True, probe=True)
    assert drainer._wake.is_set()


async def test_stale_probe_keeps_outage_pending_after_repeated_wakes(tmp_path):
    """A healthy probe before an outage cannot exhaust the retry budget."""
    outbox = Outbox(tmp_path / "outbox")
    item = await outbox.enqueue(kind="reply_mirror", chat_id=42, text="Important answer")
    drainer, ops, link = delivery(outbox, [progress() for _ in range(6)])
    await link.probe_once()
    checked = asyncio.Event()

    async def outage_probe():
        checked.set()
        raise TimeoutError("Synthetic full outage")

    ops.probe.side_effect = outage_probe
    drainer.start()
    try:
        await asyncio.wait_for(checked.wait(), 2)
        for _ in range(6):
            drainer.wake()
            pending = await outbox.pending()
            assert pending, "An outage must remain pending despite repeated wakes"
            assert pending[0].id == item.id
            await asyncio.sleep(0)
        assert ops.send_text.await_count == 1
        assert ops.probe.await_count == 2
    finally:
        await drainer.stop()
    assert drainer._up_failures == 0
    assert link.state()["state"] == "down"


async def test_down_link_does_not_send_after_repeated_wakes(tmp_path):
    outbox = Outbox(tmp_path / "outbox")
    await outbox.enqueue(kind="reply_mirror", chat_id=42, text="Wait for recovery")
    drainer, ops, link = delivery(outbox, [])
    link.record(up=False, probe=True)
    drainer.start()
    try:
        for _ in range(6):
            drainer.wake()
            assert await outbox.has_pending()
            await asyncio.sleep(0)
        ops.send_text.assert_not_awaited()
        ops.probe.assert_not_awaited()
    finally:
        await drainer.stop()


async def test_cancelled_drain_persists_each_acknowledged_chunk(tmp_path):
    """Stopping on C must retain acknowledged A and B across a disk reopen."""
    from types import SimpleNamespace

    from ductor_bot.chat_log.link import OutboxDrainer, TelegramLinkMonitor
    from ductor_bot.messenger.telegram.webchat_ops import TelegramWebchatOps

    blocked = asyncio.Event()
    restarted = False
    confirmed = []

    async def send(**kwargs):
        label = kwargs["text"][0]
        if label == "C" and not restarted:
            blocked.set()
            await asyncio.Event().wait()
        confirmed.append(label)
        return SimpleNamespace(message_id=len(confirmed))

    bot = SimpleNamespace(
        bot_instance=SimpleNamespace(send_message=send),
        _orch=SimpleNamespace(paths=tmp_path),
        file_roots=lambda _: None,
    )
    ops = TelegramWebchatOps(bot)
    outbox = Outbox(tmp_path / "outbox")
    await outbox.enqueue(kind="reply_mirror", chat_id=42, text="A" * 4096 + "B" * 4096 + "C" * 1808)
    drainer = OutboxDrainer(outbox, ops, TelegramLinkMonitor(ops, EventHub()))
    drainer.start()
    try:
        await asyncio.wait_for(blocked.wait(), 2)
    finally:
        await drainer.stop()
    [pending] = await outbox.pending()
    assert confirmed == ["A", "B"]
    assert pending.sent_chunks == 2
    restarted = True
    reopened = Outbox(tmp_path / "outbox")
    await OutboxDrainer(reopened, ops, TelegramLinkMonitor(ops, EventHub())).drain()
    assert confirmed == ["A", "B", "C"]
    assert not await reopened.has_pending()


async def test_cancelled_progress_waiting_for_outbox_lock_is_saved(tmp_path):
    """Cancellation while waiting for storage cannot discard a confirmed chunk."""
    from types import SimpleNamespace

    from ductor_bot.chat_log.link import OutboxDrainer, TelegramLinkMonitor
    from ductor_bot.messenger.telegram.webchat_ops import TelegramWebchatOps

    send_entered, return_ack, progress_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    confirmed = []

    async def send(**kwargs):
        if not confirmed:
            send_entered.set()
            await return_ack.wait()
        confirmed.append(kwargs["text"][0])
        return SimpleNamespace(message_id=len(confirmed))

    bot = SimpleNamespace(
        bot_instance=SimpleNamespace(send_message=send),
        _orch=SimpleNamespace(paths=tmp_path),
        file_roots=lambda _: None,
    )
    ops = TelegramWebchatOps(bot)
    outbox = Outbox(tmp_path / "outbox")
    item = await outbox.enqueue(kind="reply_mirror", chat_id=42, text="A" * 4096 + "C" * 10)
    original = outbox.mark_progress

    async def progress(*args, **kwargs):
        progress_entered.set()
        return await original(*args, **kwargs)

    outbox.mark_progress = progress
    drainer = OutboxDrainer(outbox, ops, TelegramLinkMonitor(ops, EventHub()))
    drainer.start()
    stopping = None
    try:
        await asyncio.wait_for(send_entered.wait(), 2)
        await outbox._lock.acquire()
        try:
            return_ack.set()
            await asyncio.wait_for(progress_entered.wait(), 2)
            stopping = asyncio.create_task(drainer.stop())
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        finally:
            outbox._lock.release()
        await asyncio.wait_for(stopping, 2)
    finally:
        return_ack.set()
        await drainer.stop()
    saved = next(value for value in await outbox.pending() if value.id == item.id)
    assert confirmed == ["A"]
    assert saved.sent_chunks == 1
    reopened = Outbox(outbox.directory)
    await OutboxDrainer(reopened, ops, TelegramLinkMonitor(ops, EventHub())).drain()
    assert confirmed == ["A", "C"]


@pytest.mark.parametrize("invalid", ["unknown_key", "unknown_version", "[1]", "{bad"])
async def test_invalid_pending_item_does_not_block_other_delivery(tmp_path, invalid, caplog):
    outbox = Outbox(tmp_path)
    first = await outbox.enqueue(kind="bus", chat_id=1, text="broken")
    path = next((tmp_path / "pending").glob(f"*-{first.id}.json"))
    if invalid in {"unknown_key", "unknown_version"}:
        data = json.loads(path.read_text())
        data["unknown" if invalid == "unknown_key" else "v"] = 99
        invalid = json.dumps(data)
    path.write_text(invalid)
    second = await outbox.enqueue(kind="bus", chat_id=1, text="valid")
    assert await outbox.has_pending()
    assert await outbox.pending() == [second]
    assert "Skipping unreadable outbox item" in caplog.text
    assert len(caplog.records) == 1
    assert not path.exists()
    assert (tmp_path / "corrupt" / path.name).read_text() == invalid
    drainer, _, _ = delivery(outbox, [progress(delivered=True, chunks=1)])
    await drainer.drain()
    assert not await outbox.has_pending()


async def test_progress_storage_failure_does_not_resend_acknowledged_chunk(tmp_path):
    outbox = Outbox(tmp_path)
    first = await outbox.enqueue(kind="bus", chat_id=1, text="first")
    await outbox.enqueue(kind="bus", chat_id=1, text="second")
    drainer, ops, _ = delivery(outbox, [])
    starts = []

    async def send(_chat, _topic, text, start, _buttons, *, on_chunk):
        starts.append((text, start))
        if start == 0:
            await on_chunk(1)
        return progress(delivered=True, chunks=1)

    ops.send_text.side_effect = send
    save = outbox.mark_progress

    async def broken_save(item_id, **kwargs):
        if item_id == first.id:
            raise OSError("full")
        return await save(item_id, **kwargs)

    with patch.object(outbox, "mark_progress", side_effect=broken_save):
        for _ in range(5):
            await drainer.drain()
    assert starts == [("first", 0)]
    assert drainer._active.attempts == drainer._up_failures == 0
    assert [item.text for item in await outbox.pending()] == ["first", "second"]
    await drainer.drain()
    assert starts == [("first", 0), ("first", 1), ("second", 0)]
    assert not await outbox.has_pending()
    saved = json.loads(next((tmp_path / "done").glob(f"*-{first.id}.json")).read_text())
    assert (saved["state"], saved["attempts"], saved["sent_chunks"]) == ("sent", 0, 1)


async def test_unexpected_send_exception_uses_five_failure_budget(tmp_path):
    outbox = Outbox(tmp_path)
    first = await outbox.enqueue(kind="bus", chat_id=1, text="first")
    await outbox.enqueue(kind="bus", chat_id=1, text="second")
    drainer, ops, _ = delivery(
        outbox, [RuntimeError("synthetic") for _ in range(5)] + [progress(delivered=True, chunks=1)]
    )
    for _ in range(5):
        await drainer.drain()
    assert ops.send_text.await_count == 6
    saved = json.loads(next((tmp_path / "done").glob(f"*-{first.id}.json")).read_text())
    assert (saved["state"], saved["attempts"]) == ("failed", 5)


@pytest.mark.parametrize("raised", [False, True])
async def test_retry_after_defers_without_exhausting_failure_budget(tmp_path, monkeypatch, raised):
    from unittest.mock import MagicMock

    from aiogram.exceptions import TelegramRetryAfter

    from ductor_bot.messenger.telegram.sender import SendProgress

    outbox = Outbox(tmp_path)
    await outbox.enqueue(kind="bus", chat_id=1, text="first")
    retry = TelegramRetryAfter(MagicMock(), "busy", retry_after=70)
    response = SendProgress(False, 0, "transient", "TelegramRetryAfter", retry)
    drainer, ops, _ = delivery(
        outbox, [retry if raised else response] * 6 + [progress(delivered=True, chunks=1)]
    )
    now = 100.0
    monkeypatch.setattr("ductor_bot.chat_log.link.time.monotonic", lambda: now)
    for index in range(6):
        await drainer.drain()
        assert ops.send_text.await_count == index + 1
        now += 69
        await drainer.drain()
        assert ops.send_text.await_count == index + 1
        assert (await outbox.pending())[0].attempts == 0
        now += 1
    await drainer.drain()
    assert not await outbox.has_pending()


async def test_pending_read_exception_preserves_active_failure_budget(tmp_path):
    outbox = Outbox(tmp_path)
    first = await outbox.enqueue(kind="bus", chat_id=1, text="first")
    await outbox.enqueue(kind="bus", chat_id=1, text="second")
    drainer, ops, _ = delivery(outbox, [progress()] + [progress(delivered=True, chunks=1)] * 2)
    await drainer.drain()
    with patch.object(outbox, "pending", side_effect=OSError("read failed")):
        for _ in range(6):
            await drainer.drain()
    await drainer.drain()
    assert ops.send_text.await_count == 3
    saved = json.loads(next((tmp_path / "done").glob(f"*-{first.id}.json")).read_text())
    assert (saved["state"], saved["attempts"]) == ("sent", 1)


@pytest.mark.parametrize("after_write", [False, True])
async def test_one_progress_write_failure_resumes_remaining_real_chunks(
    tmp_path, monkeypatch, after_write
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from ductor_bot.chat_log.link import OutboxDrainer, TelegramLinkMonitor
    from ductor_bot.messenger.telegram.webchat_ops import TelegramWebchatOps

    monkeypatch.setattr(
        "ductor_bot.messenger.telegram.sender._ia_text_chunks", lambda _: ["first", "second"]
    )
    raw = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)),
        get_me=AsyncMock(),
    )
    carrier = SimpleNamespace(
        bot_instance=raw, _orch=SimpleNamespace(paths=tmp_path), file_roots=lambda _: None
    )
    ops = TelegramWebchatOps(carrier)
    outbox = Outbox(tmp_path / "outbox")
    await outbox.enqueue(kind="bus", chat_id=1, text="report")
    drainer = OutboxDrainer(outbox, ops, TelegramLinkMonitor(ops, EventHub()))
    save = outbox.mark_progress
    writes = 0

    async def interrupted_save(*args, **kwargs):
        nonlocal writes
        writes += 1
        if writes == 1:
            if after_write:
                with patch.object(outbox, "_pending", side_effect=OSError("read failed")):
                    return await save(*args, **kwargs)
            raise OSError("full")
        return await save(*args, **kwargs)

    with patch.object(outbox, "mark_progress", side_effect=interrupted_save):
        await drainer.drain()
        [item] = await outbox.pending()
        assert (item.sent_chunks, item.attempts) == (int(after_write), 0)
        assert (drainer._active.sent_chunks, drainer._active.attempts) == (1, 0)
        await drainer.drain()
    assert [call.kwargs["text"] for call in raw.send_message.await_args_list] == ["first", "second"]
    assert not await outbox.has_pending()


@pytest.mark.parametrize("code", [5, 24])
async def test_transient_head_read_error_stops_delivery_in_order(tmp_path, code):
    outbox = Outbox(tmp_path)
    first = await outbox.enqueue(kind="bus", chat_id=1, text="first")
    second = await outbox.enqueue(kind="bus", chat_id=1, text="second")
    drainer, ops, _ = delivery(outbox, [progress(delivered=True, chunks=1)] * 2)
    load = outbox._load

    def unreadable_head(path):
        if path.name.endswith(f"-{first.id}.json"):
            raise OSError(code, "temporary read failure")
        return load(path)

    with patch.object(outbox, "_load", side_effect=unreadable_head):
        await drainer.drain()
    ops.send_text.assert_not_awaited()
    assert not (tmp_path / "corrupt").exists()
    assert await outbox.pending() == [first, second]
    await drainer.drain()
    assert [call.args[2] for call in ops.send_text.await_args_list] == ["first", "second"]


async def test_repeated_terminal_save_errors_do_not_fail_delivered_item(tmp_path):
    outbox = Outbox(tmp_path)
    first = await outbox.enqueue(kind="bus", chat_id=1, text="first")
    await outbox.enqueue(kind="bus", chat_id=1, text="second")
    drainer, ops, link = delivery(outbox, [progress(delivered=True, chunks=1)] * 2)
    with patch.object(outbox, "mark_sent", side_effect=OSError("disk unavailable")):
        for _ in range(6):
            await drainer.drain()
    assert drainer._active.state == "sent"
    assert drainer._active.attempts == drainer._up_failures == 0
    assert (await outbox.pending())[0].id == first.id
    ops.probe.assert_not_awaited()
    await drainer.drain()
    assert [call.args[2] for call in ops.send_text.await_args_list] == ["first", "second"]
    assert not await outbox.has_pending()
    assert link.state()["state"] == "up"


@pytest.mark.parametrize("code", [5, 13, 24])
async def test_unreadable_head_queries_and_enqueue_do_not_break_order(tmp_path, code):
    outbox = Outbox(tmp_path)
    first = await outbox.enqueue(kind="bus", chat_id=1, text="first", history_id="first")
    load = outbox._load

    def unreadable(path):
        if path.name.endswith(f"-{first.id}.json"):
            raise OSError(code, "read unavailable")
        return load(path)

    with patch.object(outbox, "_load", side_effect=unreadable):
        assert await outbox.has_pending()
        assert await outbox.views() == []
        assert await outbox.mirror_state("first") is None
        second = await outbox.enqueue(kind="bus", chat_id=1, text="second")
        assert [view["id"] for view in await outbox.views()] == [second.id]
        drainer, ops, _ = delivery(outbox, [])
        await drainer.drain()
        ops.send_text.assert_not_awaited()
    assert await outbox.pending() == [first, second]
    assert not (tmp_path / "corrupt").exists()


async def test_unreadable_done_is_skipped_in_queries_and_prune(tmp_path, caplog):
    outbox = Outbox(tmp_path)
    first = await outbox.enqueue(kind="bus", chat_id=1, text="unreadable")
    second = await outbox.enqueue(kind="bus", chat_id=1, text="expired")
    for item in (first, second):
        await outbox.mark_failed(item.id)
    path = next((tmp_path / "done").glob(f"*-{second.id}.json"))
    data = json.loads(path.read_text())
    data["updated"] = "2000-01-01T00:00:00Z"
    path.write_text(json.dumps(data))
    load = outbox._load

    def unreadable(path):
        if path.name.endswith(f"-{first.id}.json"):
            raise PermissionError("read unavailable")
        return load(path)

    with patch.object(outbox, "_load", side_effect=unreadable):
        assert await outbox.views() == []
        assert await outbox.mirror_state("missing") is None
        assert await outbox.prune() == 1
    assert "PermissionError" in caplog.text
    assert len(list((tmp_path / "done").glob("*.json"))) == 1


@pytest.mark.parametrize("stage", ["read", "publish", "fsync", "cleanup"])
async def test_enqueue_post_replace_failure_returns_saved_item(tmp_path, stage):
    outbox = Outbox(tmp_path)
    target = {
        "read": "_pending",
        "publish": "_publish",
    }
    if stage in target:
        guard = patch.object(outbox, target[stage], side_effect=OSError("unavailable"))
    elif stage == "fsync":
        guard = patch("ductor_bot.chat_log.outbox._fsync_directory", side_effect=OSError("sync"))
    else:
        guard = patch("ductor_bot.chat_log.outbox.Path.unlink", side_effect=OSError("cleanup"))
    with guard:
        item = await outbox.enqueue(kind="bus", chat_id=1, text="saved")
    assert await Outbox(tmp_path).pending() == [item]
    assert not list((tmp_path / "pending").glob("*.tmp"))


@pytest.mark.parametrize("stage", ["replace", "write", "fsync"])
async def test_enqueue_pre_replace_failure_leaves_no_queued_item(tmp_path, stage):
    target = {"replace": "Path.replace", "write": "json.dump", "fsync": "os.fsync"}[stage]
    with (
        patch(f"ductor_bot.chat_log.outbox.{target}", side_effect=OSError("save unavailable")),
        pytest.raises(OSError, match="save unavailable"),
    ):
        await Outbox(tmp_path).enqueue(kind="bus", chat_id=1, text="absent")
    assert not list((tmp_path / "pending").iterdir())


@pytest.mark.parametrize("invalid", [b"", b"{bad", b"\xff"])
@pytest.mark.parametrize("stage", ["mkdir", "replace", "fsync"])
async def test_confirmed_corrupt_head_does_not_block_when_quarantine_fails(
    tmp_path, invalid, stage
):
    outbox = Outbox(tmp_path)
    first = await outbox.enqueue(kind="bus", chat_id=1, text="broken")
    second = await outbox.enqueue(kind="bus", chat_id=1, text="valid")
    path = next((tmp_path / "pending").glob(f"*-{first.id}.json"))
    path.write_bytes(invalid)
    if stage == "fsync":
        guard = patch("ductor_bot.chat_log.outbox._fsync_directory", side_effect=OSError("sync"))
    else:
        original = getattr(Path, stage)

        def unavailable(source, *args, **kwargs):
            if source == path or source == tmp_path / "corrupt":
                raise PermissionError("quarantine unavailable")
            return original(source, *args, **kwargs)

        guard = patch.object(Path, stage, unavailable)
    drainer, ops, _ = delivery(outbox, [progress(delivered=True, chunks=1)])
    with guard:
        await drainer.drain()
    assert [call.args[2] for call in ops.send_text.await_args_list] == ["valid"]
    assert list((tmp_path / "done").glob(f"*-{second.id}.json"))


async def test_worker_keeps_draining_and_advances_schedule_when_prune_fails(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from ductor_bot.chat_log.link import PRUNE_INTERVAL_SECONDS

    outbox = Outbox(tmp_path)
    await outbox.enqueue(kind="bus", chat_id=1, text="deliver")
    drainer, ops, _ = delivery(outbox, [progress(delivered=True, chunks=1)])
    tick = 100.0
    calls = 0
    drain = drainer.drain

    async def run_drain():
        nonlocal calls, tick
        await drain()
        calls += 1
        if calls == 1:
            drainer.wake()
        elif calls == 2:
            tick += PRUNE_INTERVAL_SECONDS
            drainer.wake()
        else:
            raise asyncio.CancelledError

    prune = AsyncMock(side_effect=OSError("cleanup unavailable"))
    with (
        patch.object(outbox, "prune", prune),
        patch.object(drainer, "drain", run_drain),
        patch("ductor_bot.chat_log.link.time", SimpleNamespace(monotonic=lambda: tick)),
        pytest.raises(asyncio.CancelledError),
    ):
        await asyncio.wait_for(drainer._run(), 2)
    assert prune.await_count == 2
    ops.send_text.assert_awaited_once()


async def test_unreadable_done_file_does_not_starve_worker_after_twelve_wakes(tmp_path):
    outbox = Outbox(tmp_path)
    old = await outbox.enqueue(kind="bus", chat_id=1, text="completed")
    await outbox.mark_sent(old.id)
    await outbox.enqueue(kind="bus", chat_id=1, text="deliver")
    drainer, ops, _ = delivery(outbox, [progress(delivered=True, chunks=1)])
    load = outbox._load
    reads = 0
    wakes = 0

    def unreadable(path):
        nonlocal reads
        if path.name.endswith(f"-{old.id}.json"):
            reads += 1
            raise OSError(12, "read unavailable")
        return load(path)

    async def wake():
        nonlocal wakes
        wakes += 1
        if wakes == 12:
            raise asyncio.CancelledError

    with (
        patch.object(outbox, "_load", side_effect=unreadable),
        patch.object(drainer._wake, "wait", side_effect=wake),
        pytest.raises(asyncio.CancelledError),
    ):
        await drainer._run()
    assert (reads, ops.send_text.await_count) == (1, 1)
