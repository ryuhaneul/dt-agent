"""Tests for AuthMiddleware and SequentialMiddleware."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import Message


def _make_message(
    chat_id: int = 1,
    user_id: int = 100,
    text: str = "hello",
    *,
    topic_thread_id: int | None = None,
    chat_type: str = "private",
) -> MagicMock:
    """Create a mock aiogram Message."""
    msg = MagicMock(spec=Message)
    msg.chat = MagicMock()
    msg.chat.id = chat_id
    msg.chat.type = chat_type
    msg.from_user = MagicMock()
    msg.from_user.id = user_id
    msg.text = text
    msg.message_id = 1
    msg.is_topic_message = topic_thread_id is not None
    msg.message_thread_id = topic_thread_id
    return msg


class TestAuthMiddleware:
    """Test user ID filtering middleware."""

    async def test_allowed_user_passes(self) -> None:
        from ductor_bot.messenger.telegram.middleware import AuthMiddleware

        mw = AuthMiddleware(allowed_user_ids={100, 200})
        handler = AsyncMock(return_value="ok")
        msg = _make_message(user_id=100)

        result = await mw(handler, msg, {})
        handler.assert_called_once()
        assert result == "ok"

    async def test_blocked_user_dropped(self) -> None:
        from ductor_bot.messenger.telegram.middleware import AuthMiddleware

        mw = AuthMiddleware(allowed_user_ids={100})
        handler = AsyncMock()
        msg = _make_message(user_id=999)

        result = await mw(handler, msg, {})
        handler.assert_not_called()
        assert result is None

    async def test_no_from_user_dropped(self) -> None:
        from ductor_bot.messenger.telegram.middleware import AuthMiddleware

        mw = AuthMiddleware(allowed_user_ids={100})
        handler = AsyncMock()
        msg = _make_message()
        msg.from_user = None

        result = await mw(handler, msg, {})
        handler.assert_not_called()
        assert result is None

    async def test_non_message_event_passes(self) -> None:
        from ductor_bot.messenger.telegram.middleware import AuthMiddleware

        mw = AuthMiddleware(allowed_user_ids={100})
        handler = AsyncMock(return_value="pass")
        event = MagicMock()  # Not a Message or CallbackQuery
        event.__class__ = type("Update", (), {})

        result = await mw(handler, event, {})
        handler.assert_called_once()
        assert result == "pass"

    async def test_group_allowed_group_and_user_passes(self) -> None:
        """Message passes when both group and user are allowlisted."""
        from ductor_bot.messenger.telegram.middleware import AuthMiddleware

        mw = AuthMiddleware(allowed_user_ids={100}, allowed_group_ids={-1001})
        handler = AsyncMock(return_value="ok")
        msg = _make_message(user_id=100, chat_type="group", chat_id=-1001)

        result = await mw(handler, msg, {})
        handler.assert_called_once()
        assert result == "ok"

    async def test_group_blocked_group(self) -> None:
        """Message dropped when group is not in allowed_group_ids."""
        from ductor_bot.messenger.telegram.middleware import AuthMiddleware

        mw = AuthMiddleware(allowed_user_ids={100}, allowed_group_ids={-1002})
        handler = AsyncMock()
        msg = _make_message(user_id=100, chat_type="group", chat_id=-1001)

        result = await mw(handler, msg, {})
        handler.assert_not_called()
        assert result is None

    async def test_group_blocked_user_in_allowed_group(self) -> None:
        """Message dropped when user is not allowed, even if group is."""
        from ductor_bot.messenger.telegram.middleware import AuthMiddleware

        mw = AuthMiddleware(allowed_user_ids={100}, allowed_group_ids={-1001})
        handler = AsyncMock()
        msg = _make_message(user_id=999, chat_type="group", chat_id=-1001)

        result = await mw(handler, msg, {})
        handler.assert_not_called()
        assert result is None

    async def test_group_empty_group_ids_blocks_all(self) -> None:
        """Empty allowed_group_ids means no groups are allowed (fail-closed)."""
        from ductor_bot.messenger.telegram.middleware import AuthMiddleware

        mw = AuthMiddleware(allowed_user_ids={100})
        handler = AsyncMock()
        msg = _make_message(user_id=100, chat_type="group", chat_id=-1001)

        result = await mw(handler, msg, {})
        handler.assert_not_called()
        assert result is None

    async def test_group_allowlist_hot_reload_via_shared_set_identity(self) -> None:
        """In-place allowed-group updates reach the authorization middleware."""
        from ductor_bot.messenger.telegram.middleware import AuthMiddleware

        shared_groups: set[int] = set()
        mw = AuthMiddleware(allowed_user_ids={100}, allowed_group_ids=shared_groups)
        handler = AsyncMock(return_value="ok")
        msg = _make_message(user_id=100, chat_type="group", chat_id=-1001)

        assert await mw(handler, msg, {}) is None
        handler.assert_not_called()

        shared_groups.update({-1001})

        result = await mw(handler, msg, {})
        handler.assert_called_once()
        assert result == "ok"

    async def test_supergroup_uses_group_check(self) -> None:
        """Supergroups also go through group allowlist check."""
        from ductor_bot.messenger.telegram.middleware import AuthMiddleware

        mw = AuthMiddleware(allowed_user_ids={100}, allowed_group_ids={-1001})
        handler = AsyncMock(return_value="ok")
        msg = _make_message(user_id=100, chat_type="supergroup", chat_id=-1001)

        result = await mw(handler, msg, {})
        handler.assert_called_once()
        assert result == "ok"

    async def test_private_message_ignores_group_ids(self) -> None:
        """Private messages only check allowed_user_ids, not group IDs."""
        from ductor_bot.messenger.telegram.middleware import AuthMiddleware

        mw = AuthMiddleware(allowed_user_ids={100}, allowed_group_ids=set())
        handler = AsyncMock(return_value="ok")
        msg = _make_message(user_id=100, chat_type="private")

        result = await mw(handler, msg, {})
        handler.assert_called_once()
        assert result == "ok"

    async def test_callback_query_in_group_checks_both(self) -> None:
        """CallbackQuery from a group enforces both group and user checks."""
        from aiogram.types import CallbackQuery

        from ductor_bot.messenger.telegram.middleware import AuthMiddleware

        mw = AuthMiddleware(allowed_user_ids={100}, allowed_group_ids={-1001})
        handler = AsyncMock(return_value="ok")

        cb = MagicMock(spec=CallbackQuery)
        cb.from_user = MagicMock()
        cb.from_user.id = 100
        cb.message = MagicMock()
        cb.message.chat = MagicMock()
        cb.message.chat.type = "group"
        cb.message.chat.id = -1001

        result = await mw(handler, cb, {})
        handler.assert_called_once()
        assert result == "ok"

    async def test_callback_query_blocked_group(self) -> None:
        """CallbackQuery from an unauthorized group is dropped."""
        from aiogram.types import CallbackQuery

        from ductor_bot.messenger.telegram.middleware import AuthMiddleware

        mw = AuthMiddleware(allowed_user_ids={100}, allowed_group_ids={-1002})
        handler = AsyncMock()

        cb = MagicMock(spec=CallbackQuery)
        cb.from_user = MagicMock()
        cb.from_user.id = 100
        cb.message = MagicMock()
        cb.message.chat = MagicMock()
        cb.message.chat.type = "group"
        cb.message.chat.id = -1001

        result = await mw(handler, cb, {})
        handler.assert_not_called()
        assert result is None

    async def test_on_rejected_fires_for_blocked_group(self) -> None:
        """on_rejected callback fires when a group message is rejected."""
        from ductor_bot.messenger.telegram.middleware import AuthMiddleware

        calls: list[tuple[int, str, str]] = []
        mw = AuthMiddleware(
            allowed_user_ids={100},
            allowed_group_ids={-1002},
            on_rejected=lambda cid, ct, t: calls.append((cid, ct, t)),
        )
        handler = AsyncMock()
        msg = _make_message(user_id=100, chat_type="group", chat_id=-1001)
        msg.chat.title = "Bad Group"

        await mw(handler, msg, {})
        assert calls == [(-1001, "group", "Bad Group")]
        handler.assert_not_called()

    async def test_on_rejected_not_fired_for_allowed_group(self) -> None:
        """on_rejected does NOT fire when the group is allowed."""
        from ductor_bot.messenger.telegram.middleware import AuthMiddleware

        calls: list[tuple[int, str, str]] = []
        mw = AuthMiddleware(
            allowed_user_ids={100},
            allowed_group_ids={-1001},
            on_rejected=lambda cid, ct, t: calls.append((cid, ct, t)),
        )
        handler = AsyncMock(return_value="ok")
        msg = _make_message(user_id=100, chat_type="group", chat_id=-1001)

        await mw(handler, msg, {})
        assert calls == []
        handler.assert_called_once()

    async def test_on_rejected_not_fired_for_private_chat(self) -> None:
        """on_rejected does NOT fire for rejected private messages."""
        from ductor_bot.messenger.telegram.middleware import AuthMiddleware

        calls: list[tuple[int, str, str]] = []
        mw = AuthMiddleware(
            allowed_user_ids={100},
            on_rejected=lambda cid, ct, t: calls.append((cid, ct, t)),
        )
        handler = AsyncMock()
        msg = _make_message(user_id=999, chat_type="private")

        await mw(handler, msg, {})
        assert calls == []


class TestSequentialMiddleware:
    """Test dedup + per-chat sequential lock."""

    async def test_unaddressed_group_message_dropped_before_queue(self) -> None:
        """In mention_only mode, an unaddressed group message must be dropped
        before the queue indicator is sent — otherwise users see a misleading
        '[Message in queue...]' for a bot that will never reply.
        """
        from ductor_bot.messenger.telegram.middleware import SequentialMiddleware

        mw = SequentialMiddleware(group_mention_only=True)
        mw.set_bot_username("mybot")
        mw.set_bot_id(1)
        bot = AsyncMock()
        bot.send_message = AsyncMock(return_value=MagicMock(message_id=999))
        mw.set_bot(bot)

        # Pre-occupy the lock so an indicator would otherwise be emitted.
        chat_id = 100
        held = mw.get_lock((chat_id, None))
        await held.acquire()
        try:
            handler = AsyncMock()
            msg = _make_message(chat_id=chat_id, text="hi", chat_type="supergroup")
            msg.entities = []
            msg.caption = None
            msg.caption_entities = []
            msg.reply_to_message = None

            result = await mw(handler, msg, {})

            handler.assert_not_called()
            bot.send_message.assert_not_called()
            assert not mw.has_pending(chat_id)
            assert result is None
        finally:
            held.release()

    async def test_addressed_group_message_passes(self) -> None:
        """A group message with the bot's @mention reaches the handler."""
        from ductor_bot.messenger.telegram.middleware import SequentialMiddleware

        mw = SequentialMiddleware(group_mention_only=True)
        mw.set_bot_username("mybot")
        mw.set_bot_id(1)

        handler = AsyncMock(return_value="ok")
        msg = _make_message(chat_id=100, text="@mybot hi", chat_type="supergroup")
        mention = MagicMock()
        mention.type = "mention"
        mention.offset = 0
        mention.length = len("@mybot")
        msg.entities = [mention]
        msg.caption = None
        msg.caption_entities = []
        msg.reply_to_message = None

        result = await mw(handler, msg, {})
        handler.assert_called_once()
        assert result == "ok"

    async def test_sequential_processing(self) -> None:
        from ductor_bot.messenger.telegram.middleware import SequentialMiddleware

        mw = SequentialMiddleware()
        call_order: list[int] = []

        async def handler(_event: object, _data: dict[str, object]) -> None:
            call_order.append(1)
            await asyncio.sleep(0.01)
            call_order.append(2)

        msg = _make_message(chat_id=1)
        await mw(handler, msg, {})
        assert call_order == [1, 2]

    async def test_duplicate_message_dropped(self) -> None:
        from ductor_bot.messenger.telegram.middleware import SequentialMiddleware

        mw = SequentialMiddleware()
        handler = AsyncMock()
        msg = _make_message(chat_id=1)
        msg.message_id = 42

        # First call goes through
        await mw(handler, msg, {})
        assert handler.call_count == 1

        # Same message_id is deduped
        await mw(handler, msg, {})
        assert handler.call_count == 1

    async def test_abort_trigger_bypasses_lock(self) -> None:
        from ductor_bot.messenger.telegram.middleware import SequentialMiddleware

        mw = SequentialMiddleware()
        abort_handler = AsyncMock(return_value=True)
        mw.set_abort_handler(abort_handler)

        handler = AsyncMock()
        msg = _make_message(chat_id=1, text="/stop")

        result = await mw(handler, msg, {})
        abort_handler.assert_called_once()
        handler.assert_not_called()
        assert result is None

    async def test_non_abort_reaches_handler(self) -> None:
        from ductor_bot.messenger.telegram.middleware import SequentialMiddleware

        mw = SequentialMiddleware()
        abort_handler = AsyncMock(return_value=False)
        mw.set_abort_handler(abort_handler)

        handler = AsyncMock(return_value="handled")
        msg = _make_message(chat_id=1, text="hello there")

        await mw(handler, msg, {})
        handler.assert_called_once()

    async def test_abort_all_trigger_bypasses_lock(self) -> None:
        from ductor_bot.messenger.telegram.middleware import SequentialMiddleware

        mw = SequentialMiddleware()
        abort_all_handler = AsyncMock(return_value=True)
        mw.set_abort_all_handler(abort_all_handler)

        handler = AsyncMock()
        msg = _make_message(chat_id=1, text="/stop_all")

        result = await mw(handler, msg, {})
        abort_all_handler.assert_called_once()
        handler.assert_not_called()
        assert result is None

    async def test_abort_all_checked_before_abort(self) -> None:
        """'/stop_all' should trigger abort_all, NOT single abort."""
        from ductor_bot.messenger.telegram.middleware import SequentialMiddleware

        mw = SequentialMiddleware()
        abort_handler = AsyncMock(return_value=True)
        abort_all_handler = AsyncMock(return_value=True)
        mw.set_abort_handler(abort_handler)
        mw.set_abort_all_handler(abort_all_handler)

        handler = AsyncMock()
        msg = _make_message(chat_id=1, text="/stop_all")

        await mw(handler, msg, {})
        abort_all_handler.assert_called_once()
        abort_handler.assert_not_called()

    async def test_single_stop_uses_abort_not_abort_all(self) -> None:
        """'/stop' should trigger abort, NOT abort_all."""
        from ductor_bot.messenger.telegram.middleware import SequentialMiddleware

        mw = SequentialMiddleware()
        abort_handler = AsyncMock(return_value=True)
        abort_all_handler = AsyncMock(return_value=True)
        mw.set_abort_handler(abort_handler)
        mw.set_abort_all_handler(abort_all_handler)

        handler = AsyncMock()
        msg = _make_message(chat_id=1, text="/stop")

        await mw(handler, msg, {})
        abort_handler.assert_called_once()
        abort_all_handler.assert_not_called()

    @pytest.mark.parametrize(
        ("text", "which"),
        [
            ("/stop", "abort"),
            ("/stop@somebot", "abort"),
            ("/stop_all", "abort_all"),
            ("/interrupt", "interrupt"),
            ("/interrupt@somebot", "interrupt"),
            ("!interrupt", "interrupt"),
        ],
    )
    async def test_stop_commands_reach_their_handler(self, text: str, which: str) -> None:
        from ductor_bot.messenger.telegram.middleware import SequentialMiddleware

        mw = SequentialMiddleware()
        handlers = {
            name: AsyncMock(return_value=True) for name in ("abort", "abort_all", "interrupt")
        }
        mw.set_abort_handler(handlers["abort"])
        mw.set_abort_all_handler(handlers["abort_all"])
        mw.set_interrupt_handler(handlers["interrupt"])

        handler = AsyncMock()
        await mw(handler, _make_message(chat_id=1, text=text), {})

        for name, h in handlers.items():
            assert h.call_count == (1 if name == which else 0)
        handler.assert_not_called()

    @pytest.mark.parametrize(
        "text", ["stop", "wait", "cancel", "esc", "stop all", "interrupt", "abort", "skip"]
    )
    async def test_bare_word_is_an_ordinary_message(self, text: str) -> None:
        from ductor_bot.messenger.telegram.middleware import SequentialMiddleware

        mw = SequentialMiddleware()
        abort_handler = AsyncMock(return_value=True)
        abort_all_handler = AsyncMock(return_value=True)
        interrupt_handler = AsyncMock(return_value=True)
        mw.set_abort_handler(abort_handler)
        mw.set_abort_all_handler(abort_all_handler)
        mw.set_interrupt_handler(interrupt_handler)

        handler = AsyncMock(return_value="handled")
        await mw(handler, _make_message(chat_id=1, text=text), {})

        handler.assert_called_once()
        abort_handler.assert_not_called()
        abort_all_handler.assert_not_called()
        interrupt_handler.assert_not_called()

    async def test_bare_word_leaves_the_waiting_queue_alone(self) -> None:
        from ductor_bot.messenger.telegram.middleware import SequentialMiddleware

        mw = SequentialMiddleware()
        mw.set_abort_handler(AsyncMock(return_value=True))
        ran: list[str] = []
        release = asyncio.Event()
        acquired = asyncio.Event()

        async def slow(*_a: object) -> None:
            acquired.set()
            await release.wait()
            ran.append("slow")

        async def queued(*_a: object) -> None:
            ran.append("queued")

        async def word(*_a: object) -> None:
            ran.append("word")

        msgs = [_make_message(chat_id=1, text=t) for t in ("first", "second", "stop")]
        for number, msg in enumerate(msgs, start=1):
            msg.message_id = number
        first = asyncio.create_task(mw(slow, msgs[0], {}))
        await acquired.wait()
        second = asyncio.create_task(mw(queued, msgs[1], {}))
        third = asyncio.create_task(mw(word, msgs[2], {}))
        await asyncio.sleep(0.02)
        release.set()
        await asyncio.wait_for(asyncio.gather(first, second, third), 5)

        assert ran == ["slow", "queued", "word"]

    async def test_quick_command_bypasses_lock(self) -> None:
        from ductor_bot.messenger.telegram.middleware import SequentialMiddleware

        mw = SequentialMiddleware()
        quick_handler = AsyncMock(return_value=True)
        mw.set_quick_command_handler(quick_handler)

        handler = AsyncMock()
        msg = _make_message(chat_id=1, text="/status")

        result = await mw(handler, msg, {})
        quick_handler.assert_called_once_with(1, msg)
        handler.assert_not_called()
        assert result is None

    async def test_quick_command_while_lock_held(self) -> None:
        """Quick command responds immediately even while a CLI call holds the lock."""
        from ductor_bot.messenger.telegram.middleware import SequentialMiddleware

        mw = SequentialMiddleware()
        results: list[str] = []

        async def quick_handler(_chat_id: int, _message: object) -> bool:
            results.append("quick")
            return True

        mw.set_quick_command_handler(quick_handler)

        lock_acquired = asyncio.Event()
        release = asyncio.Event()

        async def slow_handler(_event: object, _data: dict[str, object]) -> None:
            lock_acquired.set()
            await release.wait()
            results.append("slow")

        slow_msg = _make_message(chat_id=1, text="do something long")
        task = asyncio.create_task(mw(slow_handler, slow_msg, {}))
        await lock_acquired.wait()

        quick_msg = _make_message(chat_id=1, text="/cron")
        quick_msg.message_id = 2
        await mw(AsyncMock(), quick_msg, {})

        assert results == ["quick"]

        release.set()
        await task
        assert results == ["quick", "slow"]

    async def test_non_quick_command_blocks_normally(self) -> None:
        from ductor_bot.messenger.telegram.middleware import SequentialMiddleware

        mw = SequentialMiddleware()
        quick_handler = AsyncMock(return_value=True)
        mw.set_quick_command_handler(quick_handler)

        handler = AsyncMock(return_value="handled")
        msg = _make_message(chat_id=1, text="/new")

        await mw(handler, msg, {})
        quick_handler.assert_not_called()
        handler.assert_called_once()


class TestGetLock:
    """Tests for SequentialMiddleware.get_lock()."""

    def test_same_chat_returns_same_lock(self) -> None:
        from ductor_bot.messenger.telegram.middleware import SequentialMiddleware

        mw = SequentialMiddleware()
        lock_a = mw.get_lock(1)
        lock_b = mw.get_lock(1)
        assert lock_a is lock_b

    def test_different_chats_return_different_locks(self) -> None:
        from ductor_bot.messenger.telegram.middleware import SequentialMiddleware

        mw = SequentialMiddleware()
        lock_a = mw.get_lock(1)
        lock_b = mw.get_lock(2)
        assert lock_a is not lock_b

    def test_returns_asyncio_lock(self) -> None:
        from ductor_bot.messenger.telegram.middleware import SequentialMiddleware

        mw = SequentialMiddleware()
        lock = mw.get_lock(42)
        assert isinstance(lock, asyncio.Lock)

    async def test_lock_shared_with_middleware(self) -> None:
        """Lock returned by get_lock is the same one used by __call__."""
        from ductor_bot.messenger.telegram.middleware import SequentialMiddleware

        mw = SequentialMiddleware()
        lock = mw.get_lock(1)
        acquired = asyncio.Event()
        release = asyncio.Event()

        async def slow_handler(_event: object, _data: dict[str, object]) -> None:
            acquired.set()
            await release.wait()

        msg = _make_message(chat_id=1)
        task = asyncio.create_task(mw(slow_handler, msg, {}))
        await acquired.wait()

        # Lock should be held by the middleware call
        assert lock.locked()

        release.set()
        await task
        assert not lock.locked()


class TestIsQuickCommand:
    """Unit tests for is_quick_command()."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("/status", True),
            ("/memory", True),
            ("/cron", True),
            ("/diagnose", True),
            ("/STATUS", True),
            ("  /status  ", True),
            ("/model", True),
            ("/model sonnet", True),
            ("/status@my_bot", True),
            ("/model@my_bot gpt-5.3-codex", True),
            ("/where", True),
            ("/leave", True),
            ("/leave -1001234567890", True),
            ("/new", False),
            ("/stop", False),
            ("/restart", False),
            ("hello", False),
            ("", False),
        ],
    )
    def test_is_quick_command(self, text: str, expected: bool) -> None:
        from ductor_bot.messenger.telegram.middleware import is_quick_command

        assert is_quick_command(text) == expected


class TestQueueManagement:
    """Tests for queue entry tracking and cancellation."""

    async def test_is_busy_when_lock_held(self) -> None:
        from ductor_bot.messenger.telegram.middleware import SequentialMiddleware

        mw = SequentialMiddleware()
        lock = mw.get_lock(1)

        assert not mw.is_busy(1)

        acquired = asyncio.Event()
        release = asyncio.Event()

        async def hold_lock() -> None:
            async with lock:
                acquired.set()
                await release.wait()

        task = asyncio.create_task(hold_lock())
        await acquired.wait()
        assert mw.is_busy(1)

        release.set()
        await task
        assert not mw.is_busy(1)

    async def test_has_pending_empty(self) -> None:
        from ductor_bot.messenger.telegram.middleware import SequentialMiddleware

        mw = SequentialMiddleware()
        assert not mw.has_pending(1)

    async def test_cancel_entry_marks_cancelled(self) -> None:
        from ductor_bot.messenger.telegram.middleware import SequentialMiddleware, _QueueEntry

        mw = SequentialMiddleware()
        entry = _QueueEntry(entry_id=1, chat_id=10, message_id=100, text_preview="test")
        mw._pending.setdefault(10, []).append(entry)

        assert not entry.cancelled
        result = await mw.cancel_entry(10, 1)
        assert result is True
        assert entry.cancelled

    async def test_cancel_entry_unknown_returns_false(self) -> None:
        from ductor_bot.messenger.telegram.middleware import SequentialMiddleware

        mw = SequentialMiddleware()
        result = await mw.cancel_entry(10, 999)
        assert result is False

    async def test_drain_pending_cancels_all(self) -> None:
        from ductor_bot.messenger.telegram.middleware import SequentialMiddleware, _QueueEntry

        mw = SequentialMiddleware()
        entries = [
            _QueueEntry(entry_id=i, chat_id=10, message_id=100 + i, text_preview=f"msg{i}")
            for i in range(3)
        ]
        mw._pending[10] = list(entries)

        count = await mw.drain_pending(10)
        assert count == 3
        assert all(e.cancelled for e in entries)

    async def test_drain_pending_skips_already_cancelled(self) -> None:
        from ductor_bot.messenger.telegram.middleware import SequentialMiddleware, _QueueEntry

        mw = SequentialMiddleware()
        e1 = _QueueEntry(entry_id=1, chat_id=10, message_id=101, text_preview="a")
        e2 = _QueueEntry(entry_id=2, chat_id=10, message_id=102, text_preview="b", cancelled=True)
        mw._pending[10] = [e1, e2]

        count = await mw.drain_pending(10)
        assert count == 1

    async def test_cancelled_entry_skips_handler(self) -> None:
        """When a queued message is cancelled, the handler is not invoked."""
        from ductor_bot.messenger.telegram.middleware import SequentialMiddleware

        mw = SequentialMiddleware()
        bot = AsyncMock()
        bot.send_message = AsyncMock(return_value=MagicMock(message_id=999))
        bot.delete_message = AsyncMock()
        mw.set_bot(bot)

        acquired = asyncio.Event()
        release = asyncio.Event()
        handler_calls: list[str] = []

        async def slow_handler(_event: object, _data: dict[str, object]) -> None:
            acquired.set()
            await release.wait()
            handler_calls.append("slow")

        async def normal_handler(_event: object, _data: dict[str, object]) -> None:
            handler_calls.append("normal")

        msg1 = _make_message(chat_id=1, text="first")
        msg1.message_id = 1
        task1 = asyncio.create_task(mw(slow_handler, msg1, {}))
        await acquired.wait()

        msg2 = _make_message(chat_id=1, text="second")
        msg2.message_id = 2
        task2 = asyncio.create_task(mw(normal_handler, msg2, {}))
        await asyncio.sleep(0.01)

        assert mw.has_pending(1)
        entries = mw._pending.get(1, [])
        assert len(entries) == 1
        await mw.cancel_entry(1, entries[0].entry_id)

        release.set()
        await task1
        await task2

        assert handler_calls == ["slow"]

    async def test_abort_drains_pending(self) -> None:
        """Abort trigger drains the pending queue."""
        from ductor_bot.messenger.telegram.middleware import SequentialMiddleware

        mw = SequentialMiddleware()
        bot = AsyncMock()
        bot.send_message = AsyncMock(return_value=MagicMock(message_id=999))
        bot.edit_message_text = AsyncMock()
        bot.delete_message = AsyncMock()
        mw.set_bot(bot)

        async def discard_queue(chat_id: int, _msg: Message) -> bool:
            await mw.drain_topic(chat_id, None)  # what the app's abort handler does first
            return True

        abort_handler = AsyncMock(side_effect=discard_queue)
        mw.set_abort_handler(abort_handler)

        acquired = asyncio.Event()
        release = asyncio.Event()
        handler_calls: list[str] = []

        async def slow_handler(_event: object, _data: dict[str, object]) -> None:
            acquired.set()
            await release.wait()
            handler_calls.append("slow")

        async def queued_handler(_event: object, _data: dict[str, object]) -> None:
            handler_calls.append("queued")

        msg1 = _make_message(chat_id=1, text="first")
        msg1.message_id = 1
        task1 = asyncio.create_task(mw(slow_handler, msg1, {}))
        await acquired.wait()

        msg2 = _make_message(chat_id=1, text="second")
        msg2.message_id = 2
        task2 = asyncio.create_task(mw(queued_handler, msg2, {}))
        await asyncio.sleep(0.01)

        stop_msg = _make_message(chat_id=1, text="/stop")
        stop_msg.message_id = 3
        await mw(AsyncMock(), stop_msg, {})

        release.set()
        await task1
        await task2

        assert handler_calls == ["slow"]
        abort_handler.assert_called_once()

    async def test_stop_kills_active_cli_process(self) -> None:
        """End-to-end style: /stop kills the active CLI process via process tree."""
        from ductor_bot.cli.process_registry import ProcessRegistry
        from ductor_bot.messenger.telegram.middleware import SequentialMiddleware

        mw = SequentialMiddleware()
        registry = ProcessRegistry()

        process = MagicMock(spec=asyncio.subprocess.Process)
        process.pid = 4242
        process.returncode = None
        process.wait = AsyncMock(return_value=0)
        process.terminate = MagicMock()
        process.kill = MagicMock()
        process.stdin = MagicMock()
        process.stdin.close = MagicMock()

        acquired = asyncio.Event()
        release = asyncio.Event()

        async def slow_handler(_event: object, _data: dict[str, object]) -> None:
            registry.register(chat_id=1, process=process, label="main")
            acquired.set()
            await release.wait()

        async def abort_handler(_chat_id: int, _msg: Message) -> bool:
            await registry.kill_all(1)
            return True

        mw.set_abort_handler(abort_handler)

        first_msg = _make_message(chat_id=1, text="long running")
        first_msg.message_id = 1
        slow_task = asyncio.create_task(mw(slow_handler, first_msg, {}))
        await acquired.wait()
        assert registry.has_active(1)

        stop_msg = _make_message(chat_id=1, text="/stop")
        stop_msg.message_id = 2

        with patch("ductor_bot.cli.process_registry.asyncio.sleep", new_callable=AsyncMock):
            await mw(AsyncMock(), stop_msg, {})

        assert process.stdin.close.called
        assert registry.was_aborted(1) is True
        assert registry.has_active(1) is False

        release.set()
        await slow_task


class TestForumTopicIndicator:
    """Tests for queue indicator message_thread_id propagation."""

    async def test_indicator_includes_thread_id_for_topic_message(self) -> None:
        from ductor_bot.messenger.telegram.middleware import SequentialMiddleware

        mw = SequentialMiddleware()
        bot = AsyncMock()
        sent_msg = MagicMock(message_id=999)
        bot.send_message = AsyncMock(return_value=sent_msg)
        bot.delete_message = AsyncMock()
        mw.set_bot(bot)

        acquired = asyncio.Event()
        release = asyncio.Event()

        async def slow_handler(_event: object, _data: dict[str, object]) -> None:
            acquired.set()
            await release.wait()

        # Both messages must share the same topic so they get the same lock key
        msg1 = _make_message(chat_id=1, text="first", topic_thread_id=42)
        msg1.message_id = 1
        task1 = asyncio.create_task(mw(slow_handler, msg1, {}))
        await acquired.wait()

        msg2 = _make_message(chat_id=1, text="second", topic_thread_id=42)
        msg2.message_id = 2
        task2 = asyncio.create_task(mw(AsyncMock(), msg2, {}))
        await asyncio.sleep(0.01)

        # Verify send_message was called with message_thread_id=42
        bot.send_message.assert_called_once()
        call_kwargs = bot.send_message.call_args.kwargs
        assert call_kwargs.get("message_thread_id") == 42

        release.set()
        await task1
        await task2

    async def test_indicator_none_thread_id_for_normal_message(self) -> None:
        from ductor_bot.messenger.telegram.middleware import SequentialMiddleware

        mw = SequentialMiddleware()
        bot = AsyncMock()
        sent_msg = MagicMock(message_id=999)
        bot.send_message = AsyncMock(return_value=sent_msg)
        bot.delete_message = AsyncMock()
        mw.set_bot(bot)

        acquired = asyncio.Event()
        release = asyncio.Event()

        async def slow_handler(_event: object, _data: dict[str, object]) -> None:
            acquired.set()
            await release.wait()

        msg1 = _make_message(chat_id=1, text="first")
        msg1.message_id = 1
        task1 = asyncio.create_task(mw(slow_handler, msg1, {}))
        await acquired.wait()

        msg2 = _make_message(chat_id=1, text="second")
        msg2.message_id = 2
        task2 = asyncio.create_task(mw(AsyncMock(), msg2, {}))
        await asyncio.sleep(0.01)

        bot.send_message.assert_called_once()
        call_kwargs = bot.send_message.call_args.kwargs
        assert call_kwargs.get("message_thread_id") is None

        release.set()
        await task1
        await task2


class TestStopDiscardsTheTopicsQueue:
    """``/stop`` and the stop words drop the waiting messages of their topic, nobody else's."""

    @staticmethod
    async def _scene(stop_text: str) -> tuple[list[str], list[str], dict[str, asyncio.Event]]:
        """Topic 5 and topic 6 each run one turn and have one message waiting; then stop."""
        from functools import partial

        from ductor_bot.messenger.telegram.handlers import handle_abort, handle_abort_all
        from ductor_bot.messenger.telegram.middleware import SequentialMiddleware

        mw = SequentialMiddleware()
        bot = AsyncMock()
        bot.send_message = AsyncMock(return_value=MagicMock(message_id=999))
        mw.set_bot(bot)
        gates = {"a1": asyncio.Event(), "b1": asyncio.Event()}
        ran: list[str] = []
        replies: list[str] = []

        async def turn(event: object, _data: dict[str, object]) -> None:
            name = event.text  # type: ignore[attr-defined]
            ran.append(name)
            if name in gates:
                await gates[name].wait()

        orchestrator = MagicMock()
        orchestrator.active_provider_name = "Claude"

        async def abort(_chat: int, topic_id: int | None = None) -> int:
            gates["a1" if topic_id == 5 else "b1"].set()  # the kill ends the running turn
            await asyncio.sleep(0.05)  # the real kill takes time; the freed lock is up for grabs
            return 1

        orchestrator.abort = abort

        async def abort_handler(chat_id: int, message: Message) -> bool:
            return await handle_abort(
                orchestrator,
                bot,
                chat_id=chat_id,
                message=message,
                discard_queue=partial(mw.drain_topic, chat_id, message.message_thread_id),
            )

        async def abort_all_handler(chat_id: int, message: Message) -> bool:
            orchestrator.abort_all = AsyncMock(return_value=2)
            for gate in gates.values():
                gate.set()
            await asyncio.sleep(0.05)
            return await handle_abort_all(orchestrator, bot, chat_id=chat_id, message=message)

        mw.set_abort_handler(abort_handler)
        mw.set_abort_all_handler(abort_all_handler)

        async def send(text: str, topic: int, number: int) -> asyncio.Task[object]:
            msg = _make_message(chat_id=1, text=text, topic_thread_id=topic)
            msg.message_id = number
            task = asyncio.create_task(mw(turn, msg, {}))
            await asyncio.sleep(0.02)
            return task

        tasks = [
            await send("a1", 5, 1),
            await send("a2", 5, 2),
            await send("b1", 6, 3),
            await send("b2", 6, 4),
        ]
        with patch(
            "ductor_bot.messenger.telegram.handlers.send_rich", new_callable=AsyncMock
        ) as send_rich:
            stop = _make_message(chat_id=1, text=stop_text, topic_thread_id=5)
            stop.message_id = 9
            await mw(AsyncMock(), stop, {})
            replies.extend(call.args[2] for call in send_rich.await_args_list)
        gates["b1"].set()  # whatever is still running in topic 6 may end now
        await asyncio.wait_for(asyncio.gather(*tasks), 5)
        return ran, replies, gates

    @pytest.mark.parametrize("stop_text", ["/stop", "/stop@somebot"])
    async def test_stop_drops_the_same_topics_queue_only(self, stop_text: str) -> None:
        ran, replies, _gates = await self._scene(stop_text)

        assert "a2" not in ran  # not started when the kill freed the lock
        assert "b2" in ran  # the other topic's waiting message was kept
        assert len(replies) == 1
        assert "1 queued message(s) in this topic discarded." in replies[0]

    async def test_stop_all_drops_every_topics_queue(self) -> None:
        ran, _replies, _gates = await self._scene("/stop_all")

        assert "a2" not in ran
        assert "b2" not in ran

    async def test_drained_entries_are_marked_as_dealt_with(self) -> None:
        from ductor_bot.messenger.telegram.middleware import MARK_STARTED_KEY, SequentialMiddleware

        mw = SequentialMiddleware()
        mw.set_bot(AsyncMock(send_message=AsyncMock(return_value=MagicMock(message_id=1))))
        release = asyncio.Event()
        marked: list[int] = []

        async def running(_event: object, _data: dict[str, object]) -> None:
            await release.wait()

        first = _make_message(chat_id=1, text="running", topic_thread_id=5)
        first.message_id = 1
        t1 = asyncio.create_task(mw(running, first, {}))
        await asyncio.sleep(0.02)
        other_topic = _make_message(chat_id=1, text="waiting elsewhere", topic_thread_id=6)
        other_topic.message_id = 3
        waiting = _make_message(chat_id=1, text="waiting", topic_thread_id=5)
        waiting.message_id = 2
        t2 = asyncio.create_task(
            mw(AsyncMock(), waiting, {MARK_STARTED_KEY: lambda: marked.append(2)})
        )
        t3 = asyncio.create_task(
            mw(running, other_topic, {MARK_STARTED_KEY: lambda: marked.append(3)})
        )
        await asyncio.sleep(0.02)

        assert await mw.drain_topic(1, 5) == 1  # topic 6 is not waiting (its lock is free)

        release.set()
        await asyncio.gather(t1, t2, t3)
        assert 2 in marked  # the journal moves it to handled: no replay after a restart

    async def test_no_topic_scope_matches_only_untopiced_messages(self) -> None:
        from ductor_bot.messenger.telegram.middleware import SequentialMiddleware, _QueueEntry

        mw = SequentialMiddleware()
        plain = _QueueEntry(entry_id=1, chat_id=10, message_id=1, text_preview="a", topic_id=None)
        topiced = _QueueEntry(entry_id=2, chat_id=10, message_id=2, text_preview="b", topic_id=7)
        mw._pending[10] = [plain, topiced]

        assert await mw.drain_topic(10, None) == 1
        assert plain.cancelled
        assert not topiced.cancelled


class TestDrainCancelsEveryTargetBeforeAnyIndicator:
    """The running turn may end while the indicators are being edited; nothing waiting may start."""

    @staticmethod
    async def _scene(stop_text: str) -> dict[str, object]:
        from functools import partial

        from ductor_bot.messenger.telegram.handlers import handle_abort, handle_abort_all
        from ductor_bot.messenger.telegram.middleware import MARK_STARTED_KEY, SequentialMiddleware

        mw = SequentialMiddleware()
        events: list[str] = []
        edit_gate = asyncio.Event()
        turn_gate = asyncio.Event()
        ran: list[str] = []
        replies: list[str] = []

        async def slow_edit(*_a: object, **_k: object) -> None:
            events.append("edit")
            await edit_gate.wait()  # the first indicator edit takes a long time

        bot = AsyncMock()
        bot.send_message = AsyncMock(return_value=MagicMock(message_id=999))
        bot.edit_message_text = AsyncMock(side_effect=slow_edit)
        mw.set_bot(bot)

        async def turn(event: object, _data: dict[str, object]) -> None:
            name = event.text  # type: ignore[attr-defined]
            ran.append(name)
            if name == "running":
                await turn_gate.wait()

        orchestrator = MagicMock()
        orchestrator.active_provider_name = "Claude"
        orchestrator.abort = AsyncMock(return_value=0)  # nothing to kill: the turn ends by itself
        orchestrator.abort_all = AsyncMock(return_value=0)

        async def abort_handler(chat_id: int, message: Message) -> bool:
            with patch(
                "ductor_bot.messenger.telegram.handlers.send_rich", new_callable=AsyncMock
            ) as send_rich:
                handled = await handle_abort(
                    orchestrator,
                    bot,
                    chat_id=chat_id,
                    message=message,
                    discard_queue=partial(mw.drain_topic, chat_id, message.message_thread_id),
                )
            replies.extend(call.args[2] for call in send_rich.await_args_list)
            return handled

        async def abort_all_handler(chat_id: int, message: Message) -> bool:
            with patch("ductor_bot.messenger.telegram.handlers.send_rich", new_callable=AsyncMock):
                return await handle_abort_all(orchestrator, bot, chat_id=chat_id, message=message)

        mw.set_abort_handler(abort_handler)
        mw.set_abort_all_handler(abort_all_handler)

        def make(text: str, number: int, *, chat: int = 1, topic: int | None = 5) -> MagicMock:
            msg = _make_message(chat_id=chat, text=text, topic_thread_id=topic)
            msg.message_id = number
            return msg

        tasks: list[asyncio.Task[object]] = []

        async def send(msg: MagicMock, *, marked_as: str | None = None) -> None:
            data: dict[str, object] = {}
            if marked_as is not None:
                data[MARK_STARTED_KEY] = lambda: events.append(f"mark {marked_as}")
            tasks.append(asyncio.create_task(mw(turn, msg, data)))
            await asyncio.sleep(0.02)

        await send(make("running", 1))
        await send(make("second", 2), marked_as="second")
        await send(make("third", 3), marked_as="third")
        # waiting elsewhere: another topic of the chat, and another chat
        await send(make("running", 4, topic=6))
        await send(make("other-topic", 5, topic=6), marked_as="other-topic")
        await send(make("running", 6, chat=2))
        await send(make("other-chat", 7, chat=2), marked_as="other-chat")
        ran.clear()

        stop_task = asyncio.create_task(mw(AsyncMock(), make(stop_text, 9), {}))
        await asyncio.sleep(0.05)  # the stop is now in its first indicator edit
        turn_gate.set()  # the running turn ends normally and frees the lock
        await asyncio.sleep(0.05)
        started_during_edit = list(ran)
        edit_gate.set()
        await asyncio.wait_for(stop_task, 5)
        await asyncio.sleep(0.05)
        return {"during": started_during_edit, "events": events, "replies": replies, "ran": ran}

    @pytest.mark.parametrize("stop_text", ["/stop", "/stop@somebot"])
    async def test_stop_starts_nothing_that_was_waiting_and_counts_what_it_cancelled(
        self, stop_text: str
    ) -> None:
        result = await self._scene(stop_text)

        assert not {"second", "third"} & set(result["during"])  # type: ignore[arg-type]
        events = result["events"]
        first_edit = events.index("edit")  # type: ignore[attr-defined]
        assert {"mark second", "mark third"} <= set(events[:first_edit])  # type: ignore[index]
        assert len(result["replies"]) == 1  # type: ignore[arg-type]
        assert "2 queued message(s) in this topic discarded." in result["replies"][0]  # type: ignore[index]
        assert "mark other-topic" not in events[: first_edit + 1]  # type: ignore[index]
        assert "mark other-chat" not in events[: first_edit + 1]  # type: ignore[index]
        assert "other-topic" in result["ran"]  # kept, and it ran when its turn came  # type: ignore[operator]
        assert "other-chat" in result["ran"]  # type: ignore[operator]

    @pytest.mark.parametrize("stop_text", ["/stop_all", "/stop_all@somebot"])
    async def test_stop_all_starts_nothing_that_was_waiting_in_the_chat(
        self, stop_text: str
    ) -> None:
        result = await self._scene(stop_text)

        assert not {"second", "third", "other-topic"} & set(result["during"])  # type: ignore[arg-type]
        assert "second" not in result["ran"]  # type: ignore[operator]
        assert "third" not in result["ran"]  # type: ignore[operator]
        events = result["events"]
        first_edit = events.index("edit")  # type: ignore[attr-defined]
        assert {"mark second", "mark third", "mark other-topic"} <= set(events[:first_edit])  # type: ignore[index]
        assert "other-topic" not in result["ran"]  # type: ignore[operator]
        assert "other-chat" in result["ran"]  # another chat is not the stopped chat  # type: ignore[operator]

    async def test_a_message_that_already_started_is_not_counted(self) -> None:
        from ductor_bot.messenger.telegram.middleware import SequentialMiddleware, _QueueEntry

        mw = SequentialMiddleware()
        mw.set_bot(AsyncMock())
        waiting = _QueueEntry(entry_id=1, chat_id=10, message_id=1, text_preview="a", topic_id=5)
        mw._pending[10] = [waiting]
        mw._remove_entry(10, waiting)  # what a message does the moment it takes the lock

        assert await mw.drain_topic(10, 5) == 0
        assert not waiting.cancelled


@pytest.mark.parametrize("command", ["/accounts", "/effort"])
def test_account_and_effort_commands_bypass_turn_lock(command):
    from ductor_bot.messenger.telegram.middleware import is_quick_command

    assert is_quick_command(command)
