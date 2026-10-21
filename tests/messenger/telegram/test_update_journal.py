"""Updates received but not started survive a restart; started ones never run again."""

# ruff: noqa: F811  (fixtures imported from the module that defines them are overridden here)

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import sys
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from aiogram import Bot, Dispatcher, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.types import Message, Update
from aiohttp import web
from pydantic_core import PydanticSerializationError

from ductor_bot.bus.lock_pool import LockPool
from ductor_bot.infra.input_context import current_input_context, current_input_timestamp
from ductor_bot.messenger.telegram import update_journal as journal_module
from ductor_bot.messenger.telegram.middleware import (
    AuthMiddleware,
    JournalMiddleware,
    SequentialMiddleware,
)
from ductor_bot.messenger.telegram.polling import PollingDispatcher
from ductor_bot.messenger.telegram.update_journal import UpdateJournal
from tests.messenger.telegram.test_polling_ownership import (  # noqa: F401
    FakeTelegramApi,
    _until,
    make_bot,
)

USER = 100
OTHER_USER = 999
CHAT = 100
SENT = 1_700_000_000.0


def message_update(
    update_id: int,
    text: str = "hello",
    *,
    user: int = USER,
    chat: int = CHAT,
    topic: int | None = None,
    date: float = SENT,
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "message_id": update_id + 1000,
        "date": int(date),
        "chat": {"id": chat, "type": "private"},
        "from": {"id": user, "is_bot": False, "first_name": "u"},
        "text": text,
    }
    if topic is not None:
        message.update(message_thread_id=topic, is_topic_message=True)
    return {"update_id": update_id, "message": message}


def callback_update(update_id: int) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": str(update_id),
            "from": {"id": USER, "is_bot": False, "first_name": "u"},
            "chat_instance": "x",
            "data": "button",
        },
    }


def write_state(
    path: Path,
    *,
    pending: dict[int, tuple[dict[str, Any], float]] | None = None,
    handled: dict[int, float] | None = None,
) -> None:
    data = {
        "version": 1,
        "pending": {
            str(uid): {
                "update": update,
                "received_at": at,
                "chat_id": update["message"]["chat"]["id"],
                "topic_id": update["message"].get("message_thread_id"),
            }
            for uid, (update, at) in (pending or {}).items()
        },
        "handled": {str(uid): at for uid, at in (handled or {}).items()},
    }
    path.write_text(json.dumps(data), encoding="utf-8")


class ScriptedApi(FakeTelegramApi):
    """Serves the scripted batches, one per getUpdates call, and records what it was asked."""

    def __init__(self, state_path: Path) -> None:
        super().__init__()
        self.batches: list[list[dict[str, Any]]] = []
        self.offsets: list[str | None] = []
        self.drop_pending: list[str | None] = []
        self.state_path = state_path
        self.journal_at_call: dict[int, tuple[set[int], set[int]]] = {}

    async def _handle(self, request: web.Request) -> web.Response:
        method = request.match_info["method"]
        if method == "getUpdates":
            form = await request.post()
            self.offsets.append(form.get("offset"))  # type: ignore[arg-type]
            call = len(self.offsets)
            seen = UpdateJournal(self.state_path)
            self.journal_at_call[call] = (
                {p.update_id for p in seen.pending()},
                {i for i in range(100) if seen.is_handled(i)},
            )
            if self.batches:
                return web.json_response({"ok": True, "result": self.batches.pop(0)})
            await asyncio.sleep(0.05)
            return web.json_response({"ok": True, "result": []})
        if method == "deleteWebhook":
            form = await request.post()
            self.drop_pending.append(form.get("drop_pending_updates"))  # type: ignore[arg-type]
        return await super()._handle(request)


@pytest.fixture
def path_in(tmp_path: Path) -> Path:
    return tmp_path / "state.json"


@pytest.fixture
async def scripted(tmp_path: Path) -> AsyncIterator[ScriptedApi]:
    server = ScriptedApi(tmp_path / "telegram_update_state.json")
    await server.start()
    yield server
    await server.stop()


@pytest.fixture
def api(scripted: ScriptedApi) -> ScriptedApi:
    """The server the bot of ``make_bot`` talks to."""
    return scripted


class Rig:
    """The bot's real pipeline: journal, dispatcher, middleware chain and a recording handler."""

    def __init__(self, server: ScriptedApi, *, allowed: frozenset[int] = frozenset({USER})) -> None:
        self.server = server
        self.journal = UpdateJournal(server.state_path)
        self.dp = PollingDispatcher(journal=self.journal)
        self.seen: list[dict[str, Any]] = []
        self.blocked: set[int] = set()
        self.gate = asyncio.Event()
        self.recovery_calls = 0
        self.hub_calls = 0
        self.replayed = 0
        session = AiohttpSession(api=TelegramAPIServer.from_base(server.base_url))
        self.bot = Bot(token="123:fake-token", session=session)
        router = Router()
        router.message.outer_middleware(JournalMiddleware(self.journal.mark_started))
        router.message.outer_middleware(AuthMiddleware(set(allowed)))
        self.sequential = SequentialMiddleware(lock_pool=LockPool())
        router.message.outer_middleware(self.sequential)
        router.message()(self._on_message)
        self.dp.include_router(router)
        self.dp.startup.register(self._recovery)  # what the bot's own startup does
        self.dp.startup.register(self._hub)  # what the supervisor hooks in
        self.dp.startup.register(self._replay)  # the last startup handler
        self.task: asyncio.Task[None] | None = None

    async def _recovery(self) -> None:
        self.recovery_calls += 1

    async def _hub(self) -> None:
        self.hub_calls += 1

    async def _replay(self, **kwargs: Any) -> None:
        workflow = {key: value for key, value in kwargs.items() if key != "bot"}
        self.replayed = self.dp.replay_journal(self.bot, **workflow)

    async def _on_message(self, message: Message) -> None:
        self.seen.append(
            {
                "id": message.message_id - 1000,
                "text": message.text,
                "received": current_input_timestamp(),
                "context": current_input_context("UTC"),
                "ready": (self.recovery_calls, self.hub_calls),
            }
        )
        if message.message_id - 1000 in self.blocked:
            await self.gate.wait()

    @property
    def ids(self) -> list[int]:
        return [item["id"] for item in self.seen]

    def start(self) -> None:
        self.task = asyncio.create_task(
            self.dp.start_polling(
                self.bot,
                allowed_updates=["message", "callback_query"],
                close_bot_session=False,
                handle_signals=False,
            )
        )

    async def crash(self) -> None:
        """The process ends: polling stops without confirming, running handlers are gone."""
        assert self.task is not None
        self.dp.signal_stop()
        await asyncio.wait_for(self.task, 5)
        for handler in list(self.dp._handle_update_tasks):
            handler.cancel()
        await asyncio.gather(*self.dp._handle_update_tasks, return_exceptions=True)
        await self.bot.session.close()


@pytest.fixture
def rigs() -> list[Rig]:
    return []


async def settle(rig: Rig, ticks: int = 3) -> None:
    for _ in range(ticks):
        await asyncio.sleep(0.05)
    assert rig.task is not None
    assert not rig.task.done()


# -- journal file: states, invariants, damage ------------------------------------------------------


def test_started_update_moves_in_one_save(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    journal = UpdateJournal(path)
    journal.record_pending(7, message_update(7), CHAT, None)
    saves: list[str] = []
    real = journal_module.atomic_text_save
    with patch.object(
        journal_module, "atomic_text_save", lambda p, c: (saves.append(c), real(p, c))[1]
    ):
        journal.mark_started(7)

    assert len(saves) == 1  # one rename moves the id
    state = json.loads(saves[0])
    assert "7" in state["handled"]
    assert "7" not in state["pending"]


def test_id_in_both_sets_counts_as_started(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    write_state(
        path,
        pending={7: (message_update(7), SENT), 8: (message_update(8), SENT)},
        handled={7: SENT},
    )

    journal = UpdateJournal(path)

    assert [p.update_id for p in journal.pending()] == [8]
    assert journal.is_handled(7)
    assert not journal.claim(7)


def test_started_ids_are_forgotten_after_25_hours(tmp_path: Path) -> None:
    now = [1_000_000.0]
    journal = UpdateJournal(tmp_path / "state.json", clock=lambda: now[0])
    journal.record_handled(1)
    now[0] += 24 * 3600
    journal.record_handled(2)
    assert journal.is_handled(1)
    now[0] += 2 * 3600

    journal.record_handled(3)

    assert not journal.is_handled(1)  # older than 25 h
    assert journal.is_handled(2)
    assert journal.is_handled(3)


def test_damaged_file_is_kept_and_the_journal_starts_empty(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "state.json"
    path.write_text("{ this is not json", encoding="utf-8")

    with caplog.at_level("WARNING"):
        journal = UpdateJournal(path)

    kept = list(tmp_path.glob("state.json.corrupt-*"))
    assert len(kept) == 1
    assert kept[0].read_text(encoding="utf-8") == "{ this is not json"
    assert journal.pending() == []
    assert sum("Telegram update journal is unreadable" in r.message for r in caplog.records) == 1
    journal.record_pending(1, message_update(1), CHAT, None)  # it works afterwards
    assert [p.update_id for p in UpdateJournal(path).pending()] == [1]


@pytest.mark.parametrize("fail_at", ["before the move", "after the move"])
def test_a_stop_at_each_save_boundary_loses_and_repeats_nothing(
    tmp_path: Path, fail_at: str
) -> None:
    path = tmp_path / "state.json"
    journal = UpdateJournal(path)
    journal.record_pending(7, message_update(7), CHAT, None)  # received, not yet yielded
    assert [p.update_id for p in UpdateJournal(path).pending()] == [7]  # a stop here: replayed

    if fail_at == "before the move":
        with (
            patch.object(journal_module, "atomic_text_save", side_effect=OSError("stop")),
            pytest.raises(OSError, match="stop"),
        ):
            journal.mark_started(7)  # the process ends while saving: the old file stays
        restarted = UpdateJournal(path)
        assert [p.update_id for p in restarted.pending()] == [7]  # still waiting: replayed once
        assert restarted.claim(7)
        assert not restarted.claim(7)
    else:
        journal.mark_started(7)
        restarted = UpdateJournal(path)
        assert restarted.pending() == []  # started: never replayed
        assert not restarted.claim(7)


# -- the order of record and confirmation ------------------------------------------------------------


async def test_updates_are_recorded_before_the_server_is_told_they_were_received(
    scripted: ScriptedApi,
) -> None:
    scripted.batches = [[message_update(1), message_update(2)]]
    rig = Rig(scripted)
    rig.blocked = {1}  # update 1 stays running, update 2 waits behind it

    rig.start()
    await _until(lambda: len(scripted.offsets) >= 2)
    await rig.crash()

    assert scripted.offsets[0] is None
    assert scripted.offsets[1] == "3"  # the second request confirms both updates ...
    pending, handled = scripted.journal_at_call[2]
    assert 2 in pending  # ... and by then the waiting one was on disk
    assert 1 in handled  # and the started one was recorded as started


async def test_nothing_is_dropped_or_flushed_at_start(
    scripted: ScriptedApi, make_bot: Callable[[], Any]
) -> None:
    bot = make_bot()
    bot._orchestrator = SimpleNamespace()  # type: ignore[assignment]
    run = asyncio.create_task(bot.run())
    await _until(lambda: len(scripted.offsets) >= 1)

    bot.request_stop()
    await asyncio.wait_for(run, 5)

    assert [value in (None, "false") for value in scripted.drop_pending] == [True]
    assert scripted.offsets[0] is None  # no offset=-1 flush
    assert bot._dp.startup.handlers[-1].callback == bot._replay_update_journal


# -- a restart ---------------------------------------------------------------------------------------------


async def test_started_update_is_not_run_again_after_a_restart(scripted: ScriptedApi) -> None:
    restart = message_update(1, "/restart")
    scripted.batches = [[restart]]
    first = Rig(scripted)
    first.start()
    await _until(lambda: first.ids == [1])
    await first.crash()  # the restart ends the process before the server heard about update 1

    scripted.batches = [[restart]]  # the server hands it out again
    second = Rig(scripted)
    second.start()
    await _until(lambda: len(scripted.offsets) >= 4)
    await second.crash()

    assert second.ids == []
    assert second.replayed == 0


async def test_waiting_update_is_replayed_exactly_once(scripted: ScriptedApi) -> None:
    running, waiting = message_update(1, "running"), message_update(2, "waiting")
    scripted.batches = [[running, waiting]]
    first = Rig(scripted)
    first.blocked = {1}
    first.start()
    await _until(lambda: first.ids == [1] and 2 in scripted.journal_at_call.get(2, (set(),))[0])
    await first.crash()
    assert first.ids == [1]  # 2 never started

    scripted.batches = [[running, waiting]]  # the server did not hear about either
    second = Rig(scripted)
    second.start()
    await _until(lambda: second.ids == [2])
    await _until(lambda: len(scripted.offsets) >= 5)
    await second.crash()

    assert second.ids == [2]  # replayed once; the server's copy of it did not run a second time
    assert second.replayed == 1
    assert UpdateJournal(scripted.state_path).pending() == []


async def test_started_ids_in_every_overlap_are_not_replayed(
    scripted: ScriptedApi,
) -> None:
    both = message_update(5)
    write_state(
        scripted.state_path,
        pending={5: (both, time.time()), 6: (message_update(6), time.time())},
        handled={5: time.time()},
    )
    scripted.batches = [[both]]  # the server also still has 5
    rig = Rig(scripted)

    rig.start()
    await _until(lambda: rig.ids == [6])
    await _until(lambda: len(scripted.offsets) >= 3)
    await rig.crash()

    assert rig.ids == [6]


async def test_small_id_waiting_message_is_replayed_after_a_larger_id_in_another_topic(
    scripted: ScriptedApi,
) -> None:
    write_state(
        scripted.state_path,
        pending={5: (message_update(5, "waits", topic=1), time.time())},
        handled={9: time.time()},  # a later update of another topic was started first
    )
    rig = Rig(scripted)

    rig.start()
    await _until(lambda: rig.ids == [5])
    await rig.crash()


async def test_new_message_with_a_small_id_is_processed(scripted: ScriptedApi) -> None:
    write_state(scripted.state_path, handled={900: time.time()})
    scripted.batches = [[message_update(3, "new")]]
    rig = Rig(scripted)

    rig.start()
    await _until(lambda: rig.ids == [3])
    await rig.crash()


# -- replay: readiness, rights, receipt --------------------------------------------------------------------


async def test_replay_runs_after_startup_and_the_startup_work_runs_once(
    scripted: ScriptedApi,
) -> None:
    write_state(scripted.state_path, pending={4: (message_update(4), time.time())})
    rig = Rig(scripted)

    rig.start()
    await _until(lambda: rig.ids == [4])
    await settle(rig)
    await rig.crash()

    assert rig.seen[0]["ready"] == (1, 1)  # recovery and the supervisor hook were done before
    assert (rig.recovery_calls, rig.hub_calls) == (1, 1)  # and the replay did not run them again


async def test_replayed_message_is_checked_against_the_allowlist_again(
    scripted: ScriptedApi,
) -> None:
    write_state(
        scripted.state_path,
        pending={
            4: (message_update(4, user=OTHER_USER), time.time()),
            5: (message_update(5), time.time()),
        },
    )
    rig = Rig(scripted)

    rig.start()
    await _until(lambda: rig.ids == [5])
    await settle(rig)
    await rig.crash()

    assert rig.ids == [5]  # the rejected one was not run ...
    assert UpdateJournal(scripted.state_path).pending() == []  # ... and is not kept either


async def test_replayed_message_keeps_its_original_arrival_time_once(
    scripted: ScriptedApi,
) -> None:
    received = SENT + 2000  # arrived long after it was sent
    write_state(
        scripted.state_path,
        pending={4: (message_update(4, "hello", date=SENT), received)},
    )
    rig = Rig(scripted)

    rig.start()
    await _until(lambda: rig.ids == [4])
    await rig.crash()

    item = rig.seen[0]
    assert item["received"] == received  # the original arrival, not the replay time
    assert item["text"] == "hello"  # nothing was added to the text
    assert item["context"].count("[Received:") == 1
    assert item["context"].count("[Sent:") == 1


async def test_button_press_is_not_replayed(scripted: ScriptedApi) -> None:
    scripted.batches = [[callback_update(3)]]
    first = Rig(scripted)
    first.start()
    await _until(lambda: len(scripted.offsets) >= 2)
    await first.crash()
    assert UpdateJournal(scripted.state_path).pending() == []
    assert UpdateJournal(scripted.state_path).is_handled(3)

    scripted.batches = [[callback_update(3)]]  # handed out again
    second = Rig(scripted)
    second.start()
    await _until(lambda: len(scripted.offsets) >= 4)
    await second.crash()

    assert second.replayed == 0
    assert second.ids == []


async def test_damaged_journal_does_not_stop_the_bot(scripted: ScriptedApi) -> None:
    scripted.state_path.write_text("not json at all", encoding="utf-8")
    scripted.batches = [[message_update(1)]]
    rig = Rig(scripted)

    rig.start()
    await _until(lambda: rig.ids == [1])
    await rig.crash()

    assert [p.name for p in scripted.state_path.parent.iterdir() if ".corrupt-" in p.name]


# -- cancelled by the user, or by the shutdown -----------------------------------------------------------


async def _two_messages_one_waiting(scripted: ScriptedApi) -> Rig:
    scripted.batches = [[message_update(1, "running"), message_update(2, "waiting")]]
    rig = Rig(scripted)
    rig.blocked = {1}
    rig.start()
    await _until(lambda: rig.ids == [1] and rig.sequential.has_pending(CHAT))
    return rig


@pytest.mark.parametrize("how", ["cancel one", "stop discards all"])
async def test_waiting_message_cancelled_by_the_user_is_not_replayed(
    scripted: ScriptedApi, how: str
) -> None:
    first = await _two_messages_one_waiting(scripted)
    if how == "cancel one":
        assert await first.sequential.cancel_entry(CHAT, 1)
    else:
        assert await first.sequential.drain_pending(CHAT) == 1
    saved = UpdateJournal(scripted.state_path)
    assert saved.pending() == []  # moved when it was cancelled, not later
    assert saved.is_handled(2)
    await first.crash()

    scripted.batches = [[message_update(1, "running"), message_update(2, "waiting")]]
    second = Rig(scripted)
    second.start()
    await _until(lambda: len(scripted.offsets) >= 5)
    await second.crash()

    assert second.ids == []
    assert second.replayed == 0


async def test_waiting_message_cut_off_by_the_shutdown_is_replayed(scripted: ScriptedApi) -> None:
    first = await _two_messages_one_waiting(scripted)
    await first.crash()  # the shutdown ends the waiting handler

    assert [p.update_id for p in UpdateJournal(scripted.state_path).pending()] == [2]
    second = Rig(scripted)
    second.start()
    await _until(lambda: second.ids == [2])
    await second.crash()


async def test_button_press_in_the_same_batch_leaves_the_waiting_message_alone(
    scripted: ScriptedApi,
) -> None:
    scripted.batches = [
        [message_update(1, "running"), message_update(2, "waiting"), callback_update(3)]
    ]
    first = Rig(scripted)
    first.blocked = {1}
    first.start()
    await _until(lambda: len(scripted.offsets) >= 2)
    await first.crash()
    saved = UpdateJournal(scripted.state_path)
    assert [p.update_id for p in saved.pending()] == [2]
    assert saved.is_handled(3)

    scripted.batches = [
        [message_update(1, "running"), message_update(2, "waiting"), callback_update(3)]
    ]
    second = Rig(scripted)
    second.start()
    await _until(lambda: second.ids == [2])
    await _until(lambda: len(scripted.offsets) >= 5)
    await second.crash()

    assert second.ids == [2]


# -- messages that waited too long ---------------------------------------------------------------------------

DAY = 24 * 3600.0


async def _bot_with_state(
    scripted: ScriptedApi, make_bot: Callable[[], Any], pending: dict[int, tuple[dict, float]]
) -> Any:
    write_state(scripted.state_path, pending=pending)
    bot = make_bot()
    bot._orchestrator = SimpleNamespace()
    bot.replayed_ids = []
    bot._dp.replay_update = lambda _bot, update, _at, **_kw: bot.replayed_ids.append(
        update.update_id
    )
    bot.notices = []

    async def notify(transport: str, chat_id: int, text: str, *, topic_id: int | None = None):
        bot.notices.append((transport, chat_id, topic_id, text))
        return True

    bot._notification_service.notify_origin = notify
    return bot


async def _run_until_started(scripted: ScriptedApi, bot: Any) -> None:
    run = asyncio.create_task(bot.run())
    await _until(lambda: len(scripted.offsets) >= 1)
    bot.request_stop()
    await asyncio.wait_for(run, 5)


async def test_message_older_than_24_hours_is_not_replayed_and_the_chat_is_told_once(
    scripted: ScriptedApi, make_bot: Callable[[], Any]
) -> None:
    now = time.time()
    old, fresh = now - DAY - 3600, now - DAY + 3600
    bot = await _bot_with_state(
        scripted,
        make_bot,
        {
            1: (message_update(1), old),
            2: (message_update(2), old),  # same chat and topic as 1: one notice for both
            3: (message_update(3, topic=7), old),  # another topic of the chat: its own notice
            4: (message_update(4), fresh),  # still within 24 hours
        },
    )

    await _run_until_started(scripted, bot)

    assert bot.replayed_ids == [4]
    assert {(chat, topic) for _t, chat, topic, _x in bot.notices} == {(CHAT, None), (CHAT, 7)}
    counts = {topic: text for _t, _c, topic, text in bot.notices}
    assert "2" in counts[None]
    assert "1" in counts[7]
    assert all(entry[0] == "tg" for entry in bot.notices)
    saved = UpdateJournal(scripted.state_path)
    assert [p.update_id for p in saved.pending()] == [4]  # only the fresh one still waits
    assert all(saved.is_handled(uid) for uid in (1, 2, 3))

    again = await _bot_with_state(scripted, make_bot, {})  # the next restart
    await _run_until_started(scripted, again)
    assert again.notices == []  # told once


async def test_messages_within_24_hours_are_replayed_without_a_notice(
    scripted: ScriptedApi, make_bot: Callable[[], Any]
) -> None:
    bot = await _bot_with_state(
        scripted, make_bot, {1: (message_update(1), time.time() - DAY + 600)}
    )

    await _run_until_started(scripted, bot)

    assert bot.replayed_ids == [1]
    assert bot.notices == []


async def test_a_failed_notice_is_logged_and_does_not_stop_the_start(
    scripted: ScriptedApi, make_bot: Callable[[], Any], caplog: pytest.LogCaptureFixture
) -> None:
    now = time.time()
    bot = await _bot_with_state(
        scripted,
        make_bot,
        {1: (message_update(1), now - DAY - 60), 2: (message_update(2), now)},
    )

    async def fail(*_args: object, **_kwargs: object) -> bool:
        bot.attempts = getattr(bot, "attempts", 0) + 1
        msg = "no access"
        raise RuntimeError(msg)

    bot._notification_service.notify_origin = fail

    with caplog.at_level("WARNING"):
        await _run_until_started(scripted, bot)

    assert bot.attempts == 1  # not retried
    assert bot.replayed_ids == [2]  # the start went on and replayed the fresh message
    assert any("skipped-messages notice" in r.message for r in caplog.records)


# -- a journal that cannot be saved -------------------------------------------------------------------------


class FailingSaves:
    """Makes the journal's file save raise ``OSError`` from the *n*-th save on, until healed."""

    def __init__(self) -> None:
        self.real = journal_module.atomic_text_save
        self.calls = 0
        self.fail_from: int | None = None

    def __call__(self, path: Path, content: str, **kwargs: Any) -> None:
        self.calls += 1
        if self.fail_from is not None and self.calls >= self.fail_from:
            raise OSError("disk full")
        self.real(path, content, **kwargs)

    def heal(self) -> None:
        self.fail_from = None


@pytest.fixture
def saves(monkeypatch: pytest.MonkeyPatch) -> FailingSaves:
    failing = FailingSaves()
    monkeypatch.setattr(journal_module, "atomic_text_save", failing)
    monkeypatch.setattr("ductor_bot.messenger.telegram.polling._SAVE_RETRY_FIRST_SECONDS", 0.05)
    monkeypatch.setattr("ductor_bot.messenger.telegram.polling._SAVE_RETRY_MAX_SECONDS", 0.1)
    return failing


def test_failed_save_leaves_memory_and_file_as_they_were(
    path_in: Path, saves: FailingSaves
) -> None:
    journal = UpdateJournal(path_in)
    journal.record_pending(1, message_update(1), CHAT, None)
    saves.fail_from = 1

    with pytest.raises(OSError, match="disk full"):
        journal.record_pending(2, message_update(2), CHAT, None)
    with pytest.raises(OSError, match="disk full"):
        journal.mark_started(1)
    with pytest.raises(OSError, match="disk full"):
        journal.record_handled(3)

    assert [p.update_id for p in journal.pending()] == [1]  # memory is still the saved state
    assert not journal.is_handled(1)
    assert not journal.is_handled(3)
    assert [p.update_id for p in UpdateJournal(path_in).pending()] == [1]  # and so is the file
    assert not journal.claim(1) or journal.claim(1) is False  # still in hand, not given out twice


async def test_a_failed_save_holds_the_update_and_the_server_confirmation_back(
    scripted: ScriptedApi, saves: FailingSaves, caplog: pytest.LogCaptureFixture
) -> None:
    scripted.batches = [[message_update(21)]]
    saves.fail_from = 1
    rig = Rig(scripted)

    with caplog.at_level(logging.WARNING, logger="ductor_bot.messenger.telegram.polling"):
        rig.start()
        await _until(lambda: len(scripted.offsets) >= 1)
        await asyncio.sleep(0.4)  # many retries later

        assert rig.ids == []  # not handed on
        assert len(scripted.offsets) == 1  # the next getUpdates (which confirms 21) did not go out
        assert sum("could not be saved" in r.message for r in caplog.records) >= 2
        assert not UpdateJournal(scripted.state_path).pending()  # nothing on disk either

        saves.heal()
        await _until(lambda: rig.ids == [21])
        await _until(lambda: len(scripted.offsets) >= 2)

    assert scripted.offsets[1] == "22"  # confirmed only after it was kept and handed on
    await rig.crash()


async def test_a_message_that_could_not_be_saved_is_not_lost_by_a_restart(
    scripted: ScriptedApi, saves: FailingSaves
) -> None:
    scripted.batches = [[message_update(21, "waiting")]]
    saves.fail_from = 1
    first = Rig(scripted)
    first.start()
    await _until(lambda: len(scripted.offsets) >= 1)
    await asyncio.sleep(0.2)
    await first.crash()  # the process ends while the save keeps failing
    assert first.ids == []

    saves.heal()
    scripted.batches = [[message_update(21, "waiting")]]  # the server never heard of it
    second = Rig(scripted)
    second.start()
    await _until(lambda: second.ids == [21])
    await _until(lambda: len(scripted.offsets) >= 4)
    await second.crash()

    assert second.ids == [21]  # once


async def test_restart_command_is_not_run_when_it_cannot_be_marked_started(
    scripted: ScriptedApi, saves: FailingSaves
) -> None:
    restart = message_update(12, "/restart")
    scripted.batches = [[restart]]
    saves.fail_from = 2  # the first save (received) works, the move to started fails
    first = Rig(scripted)
    first.start()
    await _until(lambda: saves.calls >= 2)
    await asyncio.sleep(0.3)
    await first.crash()
    assert first.ids == []  # its side effect did not start
    assert [p.update_id for p in UpdateJournal(scripted.state_path).pending()] == [12]

    saves.heal()
    scripted.batches = [[restart]]
    second = Rig(scripted)
    second.start()
    await _until(lambda: second.ids == [12])  # tried again at the next start
    asked = len(scripted.offsets)
    await _until(lambda: len(scripted.offsets) >= asked + 2)
    await second.crash()

    scripted.batches = [[restart]]  # the server hands it out once more
    third = Rig(scripted)
    asked = len(scripted.offsets)
    third.start()
    await _until(lambda: len(scripted.offsets) >= asked + 3)
    await third.crash()

    assert first.ids + second.ids + third.ids == [12]  # run exactly once in total


async def test_cancel_by_the_user_goes_on_when_the_save_fails(
    scripted: ScriptedApi, saves: FailingSaves
) -> None:
    first = await _two_messages_one_waiting(scripted)
    saves.fail_from = saves.calls + 1

    assert await first.sequential.cancel_entry(CHAT, 1)  # the cancel itself works

    assert [p.update_id for p in UpdateJournal(scripted.state_path).pending()] == [2]
    saves.heal()
    await first.crash()


# -- a journal file that cannot be read ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content", [b"\xff\xfe\x00 not utf-8", b"{ this is not json"], ids=["not-utf-8", "bad-json"]
)
def test_unreadable_file_is_kept_byte_for_byte_and_the_journal_starts_empty(
    path_in: Path, content: bytes, caplog: pytest.LogCaptureFixture
) -> None:
    path_in.write_bytes(content)

    with caplog.at_level(logging.WARNING):
        journal = UpdateJournal(path_in)

    kept = list(path_in.parent.glob(f"{path_in.name}.corrupt-*"))
    assert [p.read_bytes() for p in kept] == [content]
    assert journal.pending() == []
    assert sum("unreadable" in r.message for r in caplog.records) == 1


@pytest.mark.parametrize("content", [b"\xff\xfe\x00 not utf-8", b"{ this is not json"])
async def test_bot_is_created_with_an_unreadable_journal(
    scripted: ScriptedApi, make_bot: Callable[[], Any], content: bytes
) -> None:
    scripted.state_path.write_bytes(content)

    bot = make_bot()  # must not raise

    assert bot._journal.pending() == []
    assert list(scripted.state_path.parent.glob("telegram_update_state.json.corrupt-*"))


# -- a stop ends no turn and starts none --------------------------------------------------------------------------


class _StoppingOrchestrator:
    """What a stop does to the orchestrator: mark it, then kill the CLI processes for real."""

    def __init__(self, home: Path) -> None:
        from ductor_bot.cli.process_registry import ProcessRegistry
        from ductor_bot.infra.inflight import InflightTracker

        self._inflight_tracker = InflightTracker(home / "inflight_turns.json")
        self._process_registry = ProcessRegistry()
        self.paths = SimpleNamespace(ductor_home=home)

    @property
    def inflight_tracker(self) -> Any:
        return self._inflight_tracker

    async def run_pre_shutdown_hooks(self, _budget: object = None) -> None:
        return None

    async def shutdown(self, budget: object = None) -> None:
        from ductor_bot.orchestrator import lifecycle

        lifecycle.begin_shutdown(self)  # type: ignore[arg-type]
        await self._process_registry.kill_all_active(budget)  # type: ignore[arg-type]


def _use_real_cli_handler(
    bot: Any, orch: _StoppingOrchestrator, monkeypatch: pytest.MonkeyPatch, *, run_cli: bool
) -> tuple[list[int], list[Any], list[tuple[int, int | None]]]:
    """Replace the bot's message handler by one that runs a real (sleeping) CLI process."""
    from ductor_bot.cli import executor
    from ductor_bot.cli.base import CLIConfig
    from ductor_bot.cli.executor import SubprocessSpec, run_oneshot_subprocess
    from ductor_bot.cli.types import CLIResponse

    started: list[int] = []
    processes: list[Any] = []
    ended: list[tuple[int, int | None]] = []
    real_spawn = asyncio.create_subprocess_exec

    async def spawn(*args: Any, **kwargs: Any) -> Any:
        process = await real_spawn(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(executor.asyncio, "create_subprocess_exec", spawn)
    # the suite replaces the kill helpers by no-ops; this test owns the process it kills
    from ductor_bot.infra import process_tree

    for module in ("ductor_bot.cli.process_registry", "ductor_bot.cli.executor"):
        monkeypatch.setattr(
            f"{module}.force_kill_process_tree", process_tree.force_kill_process_tree
        )
    monkeypatch.setattr(
        "ductor_bot.cli.process_registry.terminate_process_tree",
        process_tree.terminate_process_tree,
    )

    async def turn(message: Message) -> None:
        number = message.message_id - 1000
        started.append(number)
        if not run_cli:
            return
        config = CLIConfig(
            process_registry=orch._process_registry, chat_id=message.chat.id, process_label="main"
        )
        spec = SubprocessSpec(
            exec_cmd=[sys.executable, "-c", "import time; time.sleep(120)"],
            use_cwd=None,
            prompt="",
            timeout_seconds=300,
        )
        response = await run_oneshot_subprocess(
            config,
            spec,
            lambda _out, _err, code: CLIResponse(result="", is_error=code != 0, returncode=code),
        )
        ended.append((number, response.returncode))

    bot._router.message.handlers.clear()
    bot._router.message.register(turn)
    return started, processes, ended


async def test_shutdown_starts_no_new_cli_and_keeps_the_waiting_message_for_the_next_start(
    scripted: ScriptedApi,
    make_bot: Callable[[], Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import os

    scripted.batches = [[message_update(1, "long turn"), message_update(2, "waits behind it")]]
    bot = make_bot()
    orch = _StoppingOrchestrator(tmp_path)
    bot._orchestrator = orch
    started, processes, ended = _use_real_cli_handler(bot, orch, monkeypatch, run_cli=True)
    run = asyncio.create_task(bot.run())
    await _until(lambda: started == [1] and bot._sequential.has_pending(CHAT))

    bot.request_stop()  # what the supervisor does first
    await asyncio.wait_for(run, 5)
    await asyncio.wait_for(bot.shutdown(), 15)  # kills the running turn's CLI, closes the session

    assert started == [1]  # the waiting message did not start a turn
    assert len(processes) == 1  # no CLI after the last kill
    assert ended == [(1, -15)]
    with pytest.raises(ProcessLookupError):
        os.kill(processes[0].pid, 0)  # nothing of it is left
    assert bot._dp.pending_handlers() == []  # no handler outlives the shutdown
    journal = UpdateJournal(bot._journal._path)
    assert [p.update_id for p in journal.pending()] == [2]  # not consumed, not cancelled
    assert journal.is_handled(1)

    # the next start replays it once
    again = make_bot()
    orch2 = _StoppingOrchestrator(tmp_path)
    again._orchestrator = orch2
    started2, processes2, _ended2 = _use_real_cli_handler(again, orch2, monkeypatch, run_cli=False)
    run2 = asyncio.create_task(again.run())
    await _until(lambda: started2 == [2])
    asked = len(scripted.offsets)
    await _until(lambda: len(scripted.offsets) >= asked + 2)
    again.request_stop()
    await asyncio.wait_for(run2, 5)
    await again.shutdown()

    assert started2 == [2]
    assert processes2 == []
    assert UpdateJournal(bot._journal._path).pending() == []


async def test_updates_that_do_not_take_the_lock_also_wait_once_the_stop_has_begun(
    make_bot: Callable[[], Any],
) -> None:
    from ductor_bot.messenger.telegram.middleware import KEEP_PENDING_KEY

    bot = make_bot()
    bot._orchestrator = SimpleNamespace()
    seen: list[str] = []

    async def handler(_event: object, _data: dict[str, Any]) -> str:
        seen.append("ran")
        return "ran"

    event = Message.model_validate(message_update(5, "hello")["message"])
    data: dict[str, Any] = {}
    assert await bot._sequential(handler, event, data) == "ran"  # before the stop: runs
    assert KEEP_PENDING_KEY not in data

    seen.clear()
    bot.request_stop()
    for text in ("hello", "/status", "stop"):
        event = Message.model_validate(message_update(5, text)["message"])
        data = {}
        assert await bot._sequential(handler, event, data) is None
        assert data[KEEP_PENDING_KEY] is True  # the journal keeps it pending
    assert seen == []


async def test_the_journal_keeps_an_update_the_stop_left_alone(
    scripted: ScriptedApi,
    make_bot: Callable[[], Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    scripted.batches = [[message_update(7, "arrives while stopping")]]
    bot = make_bot()
    orch = _StoppingOrchestrator(tmp_path)
    bot._orchestrator = orch
    started, _processes, _ended = _use_real_cli_handler(bot, orch, monkeypatch, run_cli=False)
    bot.request_stop()  # the stop has begun before the update is handled
    run = asyncio.create_task(bot.run())
    await asyncio.wait_for(run, 5)
    await bot.shutdown()

    assert started == []
    journal = UpdateJournal(bot._journal._path)
    assert journal.is_handled(7) is False


# -- the old waiting messages when the journal cannot be saved ---------------------------------------------------


async def test_start_goes_on_when_the_old_messages_cannot_be_set_aside(
    scripted: ScriptedApi,
    make_bot: Callable[[], Any],
    saves: FailingSaves,
    caplog: pytest.LogCaptureFixture,
) -> None:
    now = time.time()
    bot = await _bot_with_state(
        scripted,
        make_bot,
        {
            1: (message_update(1), now - DAY - 3600),  # too old
            2: (message_update(2), now - 60),
        },
    )
    saves.fail_from = 1

    with caplog.at_level(logging.WARNING):
        await _run_until_started(scripted, bot)

    assert bot.replayed_ids == [2]  # the start went on; the old one was not replayed
    assert bot.notices == []  # and nobody was told something that did not happen
    assert any("could not be set aside" in r.message for r in caplog.records)
    assert {p.update_id for p in UpdateJournal(scripted.state_path).pending()} == {1, 2}


# -- the aiogram internals this relies on -----------------------------------------------------------------


def test_aiogram_internals_the_journal_relies_on() -> None:
    listen = inspect.signature(Dispatcher._listen_updates)
    assert {"polling_timeout", "backoff_config", "allowed_updates"} <= set(listen.parameters)
    assert inspect.isasyncgenfunction(Dispatcher._listen_updates)
    process = inspect.signature(Dispatcher._process_update)
    assert {"bot", "update"} <= set(process.parameters)
    polling = inspect.getsource(Dispatcher._polling)
    assert "self._listen_updates(" in polling  # the reader we wrap is the one polling uses
    assert "self._process_update(" in polling  # replay processes updates the same way
    assert "_handle_update_tasks" in polling


def test_update_survives_the_journal_round_trip() -> None:
    bot = Bot(token="123:fake-token")
    original = Update.model_validate(message_update(5, topic=3), context={"bot": bot})

    dumped = original.model_dump(mode="json", by_alias=True, exclude_none=True)
    restored = Update.model_validate(json.loads(json.dumps(dumped)), context={"bot": bot})

    assert restored == original
    assert restored.message is not None
    assert restored.message.from_user is not None
    assert restored.message.from_user.id == USER


async def test_partial_link_preview_options_are_journaled(tmp_path: Path) -> None:
    payload = message_update(42)
    payload["message"]["link_preview_options"] = {"url": "https://example.com"}
    update = Update.model_validate(payload)
    path = tmp_path / "journal.json"
    journal = UpdateJournal(path)

    assert await PollingDispatcher._admit(update, journal)

    saved = json.loads(path.read_text(encoding="utf-8"))["pending"]["42"]["update"]
    assert saved == payload
    assert Update.model_validate(saved) == update
    assert UpdateJournal(path).pending()[0].update == saved


async def test_serialization_failure_yields_without_pending_or_content(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    content = "private message content"
    update = Update.model_validate(message_update(43, content))
    path = tmp_path / "journal.json"
    journal = UpdateJournal(path)
    dispatcher = PollingDispatcher(journal=journal)

    async def listen(*_args: Any, **_kwargs: Any) -> AsyncIterator[Update]:
        yield update

    bot = Bot(token="123:fake-token")
    try:
        with (
            patch.object(Dispatcher, "_listen_updates", listen),
            patch.object(Update, "model_dump", side_effect=PydanticSerializationError(content)),
            caplog.at_level(logging.ERROR),
        ):
            yielded = [item async for item in dispatcher._listen_updates(bot)]
    finally:
        await bot.session.close()

    assert yielded == [update]
    assert journal.pending() == []
    assert journal.is_handled(43)
    reloaded = UpdateJournal(path)
    assert reloaded.pending() == []
    assert reloaded.is_handled(43)
    errors = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert len(errors) == 1
    assert errors[0].getMessage() == (
        "Telegram update journal serialization failed (update 43, PydanticSerializationError)"
    )
    assert errors[0].exc_info is None
    assert content not in caplog.text


@pytest.mark.parametrize("reload_journal", [False, True], ids=["same-process", "restart"])
async def test_serialization_failure_is_processed_only_once(
    scripted: ScriptedApi, reload_journal: bool
) -> None:
    first = Rig(scripted)
    update = Update.model_validate(message_update(44), context={"bot": first.bot})
    second = first

    async def listen(*_args: Any, **_kwargs: Any) -> AsyncIterator[Update]:
        yield update

    try:
        with (
            patch.object(Dispatcher, "_listen_updates", listen),
            patch.object(Update, "model_dump", side_effect=PydanticSerializationError("failure")),
        ):
            yielded = [item async for item in first.dp._listen_updates(first.bot)]
            assert yielded == [update]
            for item in yielded:
                await first.dp._process_update(first.bot, item)
            assert first.ids == [44]
            assert first.journal.pending() == []
            assert first.journal.is_handled(44)

            if reload_journal:
                second = Rig(scripted)
            repeated = [item async for item in second.dp._listen_updates(second.bot)]
            for item in repeated:
                await second.dp._process_update(second.bot, item.as_(second.bot))
            assert repeated == []
            assert first.ids == [44]
            if reload_journal:
                assert second.ids == []
            assert second.journal.pending() == []
            assert second.journal.is_handled(44)
    finally:
        await first.bot.session.close()
        if second is not first:
            await second.bot.session.close()
