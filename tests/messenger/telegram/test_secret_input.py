"""Secret messages are consumed before persistence and ordinary Telegram processing."""

from __future__ import annotations

import asyncio
import json
import logging
import stat
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Dispatcher, Router
from aiogram.types import CallbackQuery, ForceReply, Message, Update

from ductor_bot.i18n import init, t
from ductor_bot.infra.secret_store import list_keys, set_secret
from ductor_bot.messenger.telegram import secret_input
from ductor_bot.messenger.telegram.polling import PollingDispatcher
from ductor_bot.messenger.telegram.secret_input import PROMPT_MARKER, SecretInput
from ductor_bot.messenger.telegram.update_journal import UpdateJournal

REAL_SLEEP = asyncio.sleep

VALUE = "synthetic-secret-value-42"
USER = 101
OTHER = 202
BOT = 303


def message(
    text=None, *, mid=1, user=USER, chat=None, kind="private", reply=None, quote=None, thread=None
):
    data = {
        "message_id": mid,
        "date": 1000,
        "chat": {"id": user if chat is None else chat, "type": kind},
        "from": {"id": user, "is_bot": user == BOT, "first_name": "test"},
    }
    if thread is not None:
        data["is_topic_message"] = True
        data["message_thread_id"] = thread
    if text is not None:
        data["text"] = text
    else:
        data["document"] = {"file_id": "file", "file_unique_id": "unique"}
    if reply is not None:
        data["reply_to_message"] = reply.model_dump(mode="json", by_alias=True)
    if quote is not None:
        data["quote"] = {"text": quote, "position": 0, "is_manual": True}
    return Message.model_validate(data)


def update(msg, uid=None):
    return Update(update_id=msg.message_id if uid is None else uid, message=msg)


def callback(data, *, user=USER, chat=None, kind="private", msg=True, thread=None):
    fields = {
        "id": "synthetic-callback",
        "from": {"id": user, "is_bot": False, "first_name": "test"},
        "chat_instance": "synthetic-chat",
        "data": data,
    }
    if msg:
        fields["message"] = message(
            "button", user=BOT, chat=user if chat is None else chat, kind=kind, thread=thread
        ).model_dump(mode="json", by_alias=True)
    return CallbackQuery.model_validate(fields)


@dataclass
class Clock:
    now: float = 1000.0

    def __call__(self):
        return self.now


class FakeBot:
    id = BOT

    def __init__(self):
        self.sent = []
        self.deleted = []
        self.edited = []
        self.answered = []
        self.delete_failures = 0
        self.send_failures = 0

    async def send_message(self, chat_id, text, **kwargs):
        if self.send_failures:
            self.send_failures -= 1
            raise RuntimeError(VALUE)
        result = message(
            text,
            mid=100 + len(self.sent),
            user=BOT,
            chat=chat_id,
            thread=kwargs.get("message_thread_id"),
        )
        self.sent.append((result, kwargs))
        return result

    async def edit_message_text(self, text, **kwargs):
        self.edited.append((text, kwargs))
        return True

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))
        if self.delete_failures:
            self.delete_failures -= 1
            raise RuntimeError(VALUE)
        return True

    async def answer_callback_query(self, query_id):
        self.answered.append(query_id)
        return True


class Harness:
    def __init__(self, home, *, is_main=True, allowed=None):
        self.home = home
        self.path = home / "secrets.env"
        self.state = home / "secret_input_state.json"
        self.clock = Clock()
        self.bot = FakeBot()
        self.allowed = {USER, OTHER} if allowed is None else allowed
        self.input = SecretInput(
            self.bot,
            is_main=is_main,
            allowed_users=self.allowed,
            ductor_home=home,
            store_path=self.path,
            clock=self.clock,
        )

    def restart(self):
        for task in self.input._delete_tasks:
            task.cancel()
        self.input = SecretInput(
            self.bot,
            is_main=True,
            allowed_users=self.allowed,
            ductor_home=self.home,
            store_path=self.path,
            clock=self.clock,
        )

    async def command(self, text="/secret set TEST_KEY", **kwargs):
        assert await settled_intercept(self.input, update(message(text, **kwargs)))

    def prompt(self):
        return next(msg for msg, _ in reversed(self.bot.sent) if msg.text.startswith(PROMPT_MARKER))

    def texts(self):
        return [msg.text for msg, _ in self.bot.sent]


def value_deletions(bot):
    prompts = {(msg.chat.id, msg.message_id) for msg, _ in bot.sent
               if msg.text.startswith(PROMPT_MARKER)}
    return [row for row in bot.deleted if row not in prompts]


async def drain_deletions(instance):
    while tasks := [task for task in instance._delete_tasks
                    if task.get_name() != "secret-prompt-expiry"]:
        await asyncio.gather(*tasks)
        # Completed gathers may not yield; let deletion done callbacks run.
        await REAL_SLEEP(0)


async def settled_intercept(instance, incoming):
    consumed = await instance.intercept(incoming)
    await drain_deletions(instance)
    return consumed


@pytest.fixture
def h(tmp_path, monkeypatch):
    init("en")
    async def sleep(delay):
        if delay > 4:
            await asyncio.Future()
    monkeypatch.setattr(secret_input.asyncio, "sleep", AsyncMock(side_effect=sleep))
    return Harness(tmp_path)


@pytest.fixture(autouse=True)
async def clean_prompt_tasks():
    yield
    tasks = [task for task in asyncio.all_tasks()
             if task.get_name() in {"secret-prompt-expiry", "secret-prompt-delete"}]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def listen(dp, h, updates, monkeypatch, *, process=False):
    async def source(_bot, **_kwargs):
        for item in updates:
            yield item
            await drain_deletions(h.input)

    monkeypatch.setattr(Dispatcher, "_listen_updates", staticmethod(source))
    yielded = []
    async for item in dp._listen_updates(h.bot):
        yielded.append(item)
        if process:
            await dp.feed_update(h.bot, item)
    return yielded


@pytest.mark.parametrize("level", [logging.INFO, logging.DEBUG])
@pytest.mark.parametrize(
    "case",
    [
        "command",
        "marked_restart",
        "marked_expired",
        "marked_missing",
        "pending",
        "reply_guarded",
        "quote_guarded",
    ],
)
@pytest.mark.parametrize("with_journal", [True, False])
async def test_polling_quarantines_all_cases(h, monkeypatch, caplog, case, level, with_journal):  # noqa: PLR0917
    caplog.set_level(level)
    if case == "command":
        target = message(f"/secret set TEST_KEY {VALUE}", mid=9)
    elif case.startswith("marked"):
        await h.command()
        prompt = h.prompt()
        if case.endswith("restart"):
            h.restart()
        elif case.endswith("expired"):
            h.clock.now += 120
        else:
            await h.command("/secret cancel")
        target = message(VALUE, mid=9, reply=prompt)
    elif case == "pending":
        await h.command()
        target = message(VALUE, mid=9)
    else:
        await h.command()
        original = message(VALUE, mid=8, reply=h.prompt())
        h.bot.delete_failures = 3
        assert await settled_intercept(h.input, update(original))
        # A fresh instance must still quarantine quotes after the failed deletion.
        h.restart()
        target = message(
            "quoted message",
            mid=9,
            reply=original,
            quote=VALUE if case.startswith("quote") else None,
        )
    journal = UpdateJournal(h.home / "journal.json", clock=h.clock)
    observed = MagicMock(wraps=journal) if with_journal else None
    dp = PollingDispatcher(journal=observed, secret_input=h.input)
    middleware = AsyncMock()
    orchestrator = AsyncMock()
    router = Router()
    router.message.outer_middleware(middleware)

    async def handle(event, **data):
        await orchestrator(event, **data)

    router.message.register(handle)
    dp.include_router(router)
    result = await listen(dp, h, [update(target)], monkeypatch, process=True)
    assert result == []
    assert journal.pending() == []
    if observed:
        observed.record_handled.assert_called_once_with(9)
        observed.record_pending.assert_not_called()
        observed.claim.assert_not_called()
        assert journal.is_handled(9)
        assert VALUE not in (h.home / "journal.json").read_text()
    middleware.assert_not_awaited()
    orchestrator.assert_not_awaited()
    assert VALUE not in caplog.text
    assert all(VALUE not in str(record.args) for record in caplog.records)


async def test_same_batch_command_then_value(h, monkeypatch):
    journal = UpdateJournal(h.home / "journal.json", clock=h.clock)
    dp = PollingDispatcher(journal=journal, secret_input=h.input)
    items = [
        update(message("/secret@synthetic_bot set TEST_KEY", mid=1)),
        update(message(VALUE, mid=2)),
    ]
    assert await listen(dp, h, items, monkeypatch) == []
    assert h.path.read_text() == f"TEST_KEY={VALUE}\n"
    assert journal.pending() == []
    assert all(journal.is_handled(item.update_id) for item in items)
    assert h.input._pending == {}


@pytest.mark.parametrize("level", [logging.INFO, logging.DEBUG])
@pytest.mark.parametrize("prompt_message_id", [0, 100])
async def test_restart_resumes_unexpired_unmarked_input(
    h, monkeypatch, caplog, level, prompt_message_id
):
    caplog.set_level(level)
    await h.command()
    data = json.loads(h.state.read_text())
    data["pending"][0]["prompt_message_id"] = prompt_message_id
    h.state.write_text(json.dumps(data))
    previous = h.input
    h.clock.now += 119
    h.restart()
    assert h.input is not previous
    journal = UpdateJournal(h.home / "journal.json", clock=h.clock)
    dp = PollingDispatcher(journal=journal, secret_input=h.input)
    target = update(message(VALUE, mid=9))
    assert await listen(dp, h, [target], monkeypatch) == []
    assert h.path.read_text() == f"TEST_KEY={VALUE}\n"
    assert journal.pending() == []
    assert journal.is_handled(9)
    assert h.input._pending == {}
    assert VALUE not in journal._path.read_text()
    assert VALUE not in h.state.read_text()
    assert VALUE not in caplog.text
    assert all(VALUE not in str(record.args) for record in caplog.records)


@pytest.mark.parametrize("elapsed", [120, 121])
async def test_restart_keeps_grace_pending_and_intercepts_marked_reply(
    h, monkeypatch, caplog, elapsed
):
    caplog.set_level(logging.DEBUG)
    await h.command()
    prompt = h.prompt()
    h.clock.now += elapsed
    h.restart()
    assert (USER, USER) in h.input._pending
    journal = UpdateJournal(h.home / "journal.json", clock=h.clock)
    dp = PollingDispatcher(journal=journal, secret_input=h.input)
    ordinary = update(message("ordinary", mid=8))
    assert await listen(dp, h, [ordinary], monkeypatch) == []
    assert journal.pending() == []
    marked = update(message(VALUE, mid=9, reply=prompt))
    assert await listen(dp, h, [marked], monkeypatch) == []
    assert journal.pending() == []
    assert journal.is_handled(9)
    assert h.texts()[-1] == t("secret.expired_value")
    assert not h.path.exists()
    assert VALUE not in journal._path.read_text()
    assert VALUE not in caplog.text


async def test_polling_still_yields_ordinary_updates(h, monkeypatch):
    journal = UpdateJournal(h.home / "journal.json", clock=h.clock)
    dp = PollingDispatcher(journal=journal, secret_input=h.input)
    ordinary = update(message("ordinary", mid=4))
    assert await listen(dp, h, [ordinary], monkeypatch) == [ordinary]
    assert [entry.update_id for entry in journal.pending()] == [4]


async def test_journal_write_retries_without_pending_or_dispatch(h, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    journal = UpdateJournal(h.home / "journal.json", clock=h.clock)
    original = journal.record_handled
    calls = 0

    def save(uid):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise OSError(VALUE)
        original(uid)

    monkeypatch.setattr(journal, "record_handled", save)
    dp = PollingDispatcher(journal=journal, secret_input=h.input)
    assert (
        await listen(dp, h, [update(message(f"/secret set TEST_KEY {VALUE}", mid=5))], monkeypatch)
        == []
    )
    assert calls == 3
    assert journal.pending() == []
    assert journal.is_handled(5)
    assert VALUE not in caplog.text


@pytest.mark.parametrize(
    "text",
    ["/secret", "/secret@synthetic_bot", "/secret unknown", "/secret list extra"],
)
async def test_usage_and_command_prefix(h, text):
    await h.command(text)
    assert h.texts() == [t("secret.usage")]


async def test_list_cancel_and_buttons(h):
    await h.command("/secret list")
    assert h.texts()[-1] == t("secret.list_empty")
    await h.command()
    assert isinstance(h.bot.sent[-1][1]["reply_markup"], ForceReply)
    assert h.prompt().text.splitlines()[0] == PROMPT_MARKER
    assert h.bot.sent[-1][1]["parse_mode"] is None
    await h.command("/secret cancel")
    assert h.texts()[-1] == t("secret.cancelled")
    await h.command("/secret cancel")
    assert h.texts()[-1] == t("secret.nothing_pending")
    set_secret(h.path, "SECOND", "synthetic-second")
    set_secret(h.path, "FIRST", "synthetic-first")
    await h.command("/secret list")
    assert h.texts()[-1] == t("secret.list", keys="FIRST\nSECOND")
    assert "synthetic-first" not in str(h.texts())
    await h.command("/secret set FIRST")
    markup = h.bot.sent[-1][1]["reply_markup"]
    assert [button.callback_data for button in markup.inline_keyboard[0]] == [
        "sec:ow:FIRST",
        "sec:x",
    ]
    assert h.input._pending == {}
    assert await h.input.handle_callback(callback("sec:ow:FIRST"))
    assert h.input._pending[(USER, USER)].key == "FIRST"
    assert await h.input.handle_callback(callback("sec:x"))
    assert h.texts()[-1] == t("secret.cancelled")
    assert h.input._pending == {}
    await h.command("/secret delete FIRST")
    assert h.bot.sent[-1][1]["reply_markup"].inline_keyboard[0][0].callback_data == "sec:del:FIRST"
    assert await h.input.handle_callback(callback("sec:del:FIRST"))
    assert list_keys(h.path) == ["SECOND"]
    assert await h.input.handle_callback(callback("sec:del:FIRST"))
    assert h.texts()[-1] == t("secret.delete_missing").format(key="FIRST")


@pytest.mark.parametrize("operation", ["set", "delete"])
async def test_callback_data_fits_maximum_key(h, operation):
    key = "A" * 56
    set_secret(h.path, key, "synthetic-existing")
    await h.command(f"/secret {operation} {key}")
    buttons = h.bot.sent[-1][1]["reply_markup"].inline_keyboard[0]
    assert all(len(button.callback_data.encode()) <= 64 for button in buttons)
    assert len(buttons[0].callback_data.encode()) == (63 if operation == "set" else 64)


@pytest.mark.parametrize("key", ["", "a", "A" * 57, VALUE])
async def test_invalid_key_is_not_echoed(h, key):
    await h.command(f"/secret set {key}")
    assert h.texts()[-1] == t("secret.invalid_key")
    assert VALUE not in str(h.texts())
    assert h.input._pending == {}


@pytest.mark.parametrize(
    ("reason", "value"),
    [
        ("value_empty", ""),
        ("value_too_long", "X" * 4097),
        ("value_nul", "x\0x"),
        ("value_multiline", "x\nx"),
        ("value_edge_space", " x"),
        ("value_edge_quote", "'x"),
        ("value_dollar", "$x"),
    ],
)
async def test_each_value_rejection_keeps_pending(h, reason, value):
    await h.command()
    original = h.input._pending[(USER, USER)]
    assert await settled_intercept(h.input, update(message(value, mid=2, reply=h.prompt())))
    assert h.texts()[-1] == t("secret.rejected", reason=t(f"secret.reason_{reason}", max=4096))
    assert h.input._pending[(USER, USER)] is original
    assert not h.path.exists()
    assert h.bot.deleted[-1] == (USER, 2)
    assert await settled_intercept(h.input, update(message(VALUE, mid=3)))
    assert h.path.read_text() == f"TEST_KEY={VALUE}\n"


async def test_length_boundary_env_note_and_metadata(h):
    (h.home / ".env").write_text("export TEST_KEY=synthetic-provider\n")
    await h.command()
    data = json.loads(h.state.read_text())
    assert set(data) == {"pending", "guarded"}
    assert data["pending"] == [
        {
            "chat_id": USER,
            "user_id": USER,
            "key": "TEST_KEY",
            "prompt_message_id": h.prompt().message_id,
            "expires_at": 1120.0,
        }
    ]
    assert stat.S_IMODE(h.state.stat().st_mode) == 0o600
    assert VALUE not in h.state.read_text()
    value = "X" * 4096
    assert await settled_intercept(h.input, update(message(value, mid=3)))
    assert h.path.read_text() == f"TEST_KEY={value}\n"
    assert h.texts()[-1] == t("secret.saved").format(key="TEST_KEY", length=4096) + "\n" + t(
        "secret.saved_env_note"
    ).format(key="TEST_KEY")
    assert value not in h.state.read_text()
    assert json.loads(h.state.read_text())["pending"] == []
    assert stat.S_IMODE(h.state.stat().st_mode) == 0o600


async def test_non_text_reply_is_deleted_and_pending_kept(h):
    await h.command()
    assert await settled_intercept(h.input, update(message(None, mid=2, reply=h.prompt())))
    assert h.texts()[-1] == t("secret.not_text")
    assert (USER, USER) in h.input._pending
    assert h.bot.deleted[-1] == (USER, 2)
    assert not h.path.exists()


@pytest.mark.parametrize("operation", ["set", "delete"])
async def test_extra_arguments_deleted_never_saved(h, operation):
    await h.command(f"/secret {operation} TEST_KEY {VALUE}", mid=7)
    assert h.texts()[-1] == t("secret.extra_args")
    assert h.bot.deleted == [(USER, 7)]
    assert not h.path.exists()
    assert h.input._pending == {}
    assert VALUE not in str(h.texts())


@pytest.mark.parametrize(
    ("kind", "chat", "user", "expected"),
    [
        ("group", -404, USER, "private_only"),
        ("supergroup", -404, USER, "private_only"),
        ("private", OTHER, USER, "private_only"),
        ("private", 999, 999, None),
    ],
)
async def test_command_authorization(h, kind, chat, user, expected):
    await h.command(user=user, chat=chat, kind=kind)
    assert h.texts() == ([t(f"secret.{expected}")] if expected else [])
    assert h.input._pending == {}
    assert not h.path.exists()


@pytest.mark.parametrize(
    ("kind", "chat", "user"),
    [("group", -404, USER), ("private", OTHER, USER), ("private", 999, 999)],
)
async def test_marked_reply_authorization_deletes_without_response(h, kind, chat, user):
    await h.command()
    prompt = h.prompt()
    before = len(h.bot.sent)
    assert await settled_intercept(h.input,
        update(message(VALUE, mid=2, reply=prompt, user=user, chat=chat, kind=kind))
    )
    assert h.bot.deleted[-1] == (chat, 2)
    assert len(h.bot.sent) == before
    assert not h.path.exists()


async def test_pending_authorization_is_rechecked(h):
    await h.command()
    h.allowed.remove(USER)
    assert await settled_intercept(h.input, update(message(VALUE, mid=2)))
    assert h.bot.deleted[-1] == (USER, 2)
    assert not h.path.exists()


async def test_guarded_reply_authorization_is_rechecked(h):
    await h.command()
    original = message(VALUE, mid=8, reply=h.prompt())
    h.bot.delete_failures = 3
    assert await settled_intercept(h.input, update(original))
    h.allowed.remove(USER)
    count = len(h.bot.sent)
    assert await settled_intercept(h.input, update(message("quote", mid=9, reply=original)))
    assert h.bot.deleted[-1] == (USER, 9)
    assert len(h.bot.sent) == count


@pytest.mark.parametrize("extra", [False, True])
async def test_sub_bot_consumes_only_commands(tmp_path, monkeypatch, extra):
    init("en")
    monkeypatch.setattr(secret_input.asyncio, "sleep", AsyncMock())
    sub = Harness(tmp_path, is_main=False)
    text = "/secret set TEST_KEY" + (f" {VALUE}" if extra else "")
    await sub.command(text, mid=4)
    assert sub.texts() == [t("secret.main_only")]
    assert sub.bot.deleted == ([(USER, 4)] if extra else [])
    marked = message(f"{PROMPT_MARKER}\nrequest", user=BOT, chat=USER)
    assert not await settled_intercept(sub.input, update(message(VALUE, mid=2, reply=marked)))
    assert not await settled_intercept(sub.input, update(message(VALUE, mid=3)))
    assert not sub.state.exists()
    assert not sub.path.exists()


@pytest.mark.parametrize("data", ["sec:ow:TEST_KEY", "sec:del:TEST_KEY", "sec:x"])
@pytest.mark.parametrize("denial", ["removed", "group", "other_user", "sub", "missing_message"])
async def test_callback_authorization_rechecked(h, data, denial):
    set_secret(h.path, "TEST_KEY", "synthetic-existing")
    await h.command("/secret set TEST_KEY")
    fields = {}
    if denial == "removed":
        h.allowed.remove(USER)
    elif denial == "group":
        fields = {"chat": -404, "kind": "group"}
    elif denial == "other_user":
        fields = {"user": OTHER, "chat": USER}
    elif denial == "sub":
        h.input._is_main = False
    else:
        fields = {"msg": False}
    assert await h.input.handle_callback(callback(data, **fields))
    assert h.path.read_text() == "TEST_KEY=synthetic-existing\n"
    assert h.input._pending == {}
    assert h.bot.answered == ["synthetic-callback"]


async def test_unknown_callback_and_non_message_update(h):
    assert not await h.input.handle_callback(callback("other:button"))
    assert await h.input.handle_callback(callback("sec:unknown"))
    assert await h.input.handle_callback(callback("sec:ow:bad-key"))
    assert h.texts()[-1] == t("secret.invalid_key")
    assert not await settled_intercept(h.input, Update(update_id=99))


async def test_requests_replace_and_users_remain_separate(h):
    await h.command("/secret set FIRST")
    await h.command("/secret set SECOND", mid=2)
    await h.command("/secret set THIRD", mid=3, user=OTHER)
    assert {identity: row.key for identity, row in h.input._pending.items()} == {
        (USER, USER): "SECOND",
        (OTHER, OTHER): "THIRD",
    }
    await asyncio.gather(
        settled_intercept(h.input, update(message(VALUE, mid=4))),
        settled_intercept(h.input, update(message("synthetic-other-value", mid=5, user=OTHER))),
    )
    assert h.path.read_text() == f"SECOND={VALUE}\nTHIRD=synthetic-other-value\n"
    assert h.input._pending == {}


async def test_simultaneous_set_last_request_wins(h, monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()
    original = h.bot.send_message

    async def send(chat_id, text, **kwargs):
        if text.startswith(PROMPT_MARKER) and "FIRST" in text:
            entered.set()
            await release.wait()
        return await original(chat_id, text, **kwargs)

    monkeypatch.setattr(h.bot, "send_message", send)
    first = asyncio.create_task(settled_intercept(h.input, update(message("/secret set FIRST", mid=1))))
    await entered.wait()
    second = asyncio.create_task(settled_intercept(h.input, update(message("/secret set SECOND", mid=2))))
    scheduled = asyncio.Event()
    asyncio.get_running_loop().call_soon(scheduled.set)
    try:
        await scheduled.wait()
        # The second request cannot replace metadata while the first prompt is in flight.
        assert h.input._pending[(USER, USER)].key == "FIRST"
        assert h.bot.sent == []
    finally:
        release.set()
        results = await asyncio.gather(first, second)
    assert results == [True, True]
    assert h.input._pending[(USER, USER)].key == "SECOND"
    assert json.loads(h.state.read_text())["pending"][0]["key"] == "SECOND"


@pytest.mark.parametrize("failures", [2, 3])
async def test_deletion_retries_and_guarded_retention(h, failures, caplog):
    caplog.set_level(logging.DEBUG)
    await h.command()
    h.bot.delete_failures = failures
    original = message(VALUE, mid=8, reply=h.prompt())
    assert await settled_intercept(h.input, update(original))
    assert value_deletions(h.bot) == [(USER, 8)] * 3
    assert [call.args[0] for call in secret_input.asyncio.sleep.await_args_list if call.args[0] <= 4] == [1, 2]
    data = json.loads(h.state.read_text())
    assert [row for row in data["guarded"] if row["message_id"] != h.prompt().message_id] == [
        {"chat_id": USER, "message_id": 8, "at": 1000.0, "deleted": failures == 2}
    ]
    assert (t("secret.delete_failed") in h.texts()) == (failures == 3)
    assert VALUE not in caplog.text
    assert VALUE not in h.state.read_text()
    if failures == 3:
        assert await settled_intercept(h.input,
            update(message("follow-up", mid=9, reply=original, quote=VALUE))
        )
        assert h.bot.deleted[-2:] == [(USER, 8), (USER, 9)]
        assert h.texts()[-1] == t("secret.quoted_refused")
        assert [row for row in json.loads(h.state.read_text())["guarded"] if row["message_id"] != h.prompt().message_id] == [
            {"chat_id": USER, "message_id": 8, "at": 1000.0, "deleted": True},
            {"chat_id": USER, "message_id": 9, "at": 1000.0, "deleted": True},
        ]


async def test_store_failure_never_leaks_or_yields(h, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    await h.command()

    def fail(*_args):
        raise OSError(VALUE)

    monkeypatch.setattr(secret_input, "set_secret", fail)
    journal = UpdateJournal(h.home / "journal.json", clock=h.clock)
    dp = PollingDispatcher(journal=journal, secret_input=h.input)
    assert await listen(dp, h, [update(message(VALUE, mid=2))], monkeypatch) == []
    assert h.texts()[-1] == t("secret.save_failed")
    assert not h.path.exists()
    assert journal.pending() == []
    assert journal.is_handled(2)
    assert VALUE not in caplog.text


@pytest.mark.parametrize("stage", [1, 2])
async def test_pending_add_two_stage_failure(h, monkeypatch, caplog, stage):
    caplog.set_level(logging.DEBUG)
    original = secret_input.atomic_text_save
    calls = 0
    states = []

    def save(path, content):
        nonlocal calls
        calls += 1
        states.append(json.loads(content))
        if calls == stage:
            raise OSError(VALUE)
        original(path, content)

    monkeypatch.setattr(secret_input, "atomic_text_save", save)
    await h.command()
    assert states[0]["pending"][0]["prompt_message_id"] == 0
    assert h.input._pending == {}
    assert h.texts()[-1] == t("secret.save_failed")
    if stage == 1:
        assert h.texts() == [t("secret.save_failed")]
        assert h.bot.deleted == []
        assert not h.state.exists()
    else:
        assert states[1]["pending"][0]["prompt_message_id"] == h.prompt().message_id
        assert h.bot.deleted == [(USER, h.prompt().message_id)]
        assert json.loads(h.state.read_text())["pending"] == []
    assert VALUE not in caplog.text


@pytest.mark.parametrize("action", ["cancel", "saved", "expired"])
async def test_pending_remove_failure_warns_and_continues(h, monkeypatch, caplog, action):
    caplog.set_level(logging.WARNING)
    await h.command()
    prompt = h.prompt()

    original = secret_input.atomic_text_save

    def fail(path, content):
        if not json.loads(content)["pending"]:
            raise OSError(VALUE)
        original(path, content)

    monkeypatch.setattr(secret_input, "atomic_text_save", fail)
    if action == "cancel":
        await h.command("/secret cancel")
        assert h.texts()[-1] == t("secret.cancelled")
    else:
        if action == "expired":
            h.clock.now += 240
        assert await settled_intercept(h.input, update(message(VALUE, mid=2, reply=prompt)))
        assert h.path.exists() == (action == "saved")
    assert h.input._pending == {}
    assert sum("pending_remove_failed" in record.getMessage() for record in caplog.records) == 1
    assert "pending_remove_failed" in caplog.text
    assert VALUE not in caplog.text
    h.restart()
    assert bool(h.input._pending) is (action != "expired")


async def test_second_stage_failure_with_remove_failure(h, monkeypatch, caplog):
    original = secret_input.atomic_text_save
    calls = 0

    def save(path, content):
        nonlocal calls
        calls += 1
        if calls in {2, 3}:
            raise OSError(VALUE)
        original(path, content)

    monkeypatch.setattr(secret_input, "atomic_text_save", save)
    await h.command()
    assert h.input._pending == {}
    assert h.texts()[-1] == t("secret.save_failed")
    assert h.bot.deleted == [(USER, h.prompt().message_id)]
    assert "pending_remove_failed" in caplog.text
    h.restart()
    # Mandatory guard writes also persist the earlier in-memory pending removal.
    assert h.input._pending == {}


async def test_guarded_write_retries_block_next_update(h, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    await h.command()
    h.bot.delete_failures = 3
    original = secret_input.atomic_text_save
    writes = 0

    def save(path, content):
        nonlocal writes
        if json.loads(content)["guarded"]:
            writes += 1
            if writes < 4:
                raise OSError(VALUE)
        original(path, content)

    monkeypatch.setattr(secret_input, "atomic_text_save", save)
    original_value = message(VALUE, mid=8, reply=h.prompt())
    quote = message("quote", mid=9, reply=original_value, quote=VALUE)
    reached_second = []

    async def source(_bot, **_kwargs):
        yield update(original_value)
        reached_second.append(json.loads(h.state.read_text())["guarded"])
        await drain_deletions(h.input)
        yield update(quote)
        await drain_deletions(h.input)

    monkeypatch.setattr(Dispatcher, "_listen_updates", staticmethod(source))
    dp = PollingDispatcher(secret_input=h.input)
    assert [item async for item in dp._listen_updates(h.bot)] == []
    assert reached_second == [[{"chat_id": USER, "message_id": 8, "at": 1000.0, "deleted": False}]]
    assert writes == 11  # Includes prompt guard creation and its deletion transition.
    assert [call.args[0] for call in secret_input.asyncio.sleep.await_args_list if call.args[0] <= 4] == [
        0.5,
        1.0,
        2.0,
        1,
        2,
    ]
    assert [row for row in json.loads(h.state.read_text())["guarded"]
            if row["message_id"] != h.prompt().message_id] == [
        {"chat_id": USER, "message_id": 8, "at": 1000.0, "deleted": True},
        {"chat_id": USER, "message_id": 9, "at": 1000.0, "deleted": True},
    ]
    assert VALUE not in caplog.text


@pytest.mark.parametrize(
    "content",
    [
        b"\xff",
        b"not-json",
        b"{}",
        b'{"pending": {}, "guarded": []}',
        b'{"pending": [], "guarded": [{"chat_id": "bad", "message_id": 8, "at": 1000, "deleted": false}]}',
    ],
)
def test_corrupt_metadata_is_preserved_byte_for_byte(tmp_path, content, caplog):
    state = tmp_path / "secret_input_state.json"
    state.write_bytes(content)
    h = Harness(tmp_path)
    assert h.input._pending == {}
    assert h.input._guarded == {}
    preserved = list(tmp_path.glob("secret_input_state.json.corrupt-*"))
    assert len(preserved) == 1
    assert preserved[0].read_bytes() == content
    assert not state.exists()
    assert len(caplog.records) == 1
    assert "metadata_unreadable" in caplog.text


def test_unreadable_metadata_is_preserved(tmp_path, monkeypatch, caplog):
    state = tmp_path / "secret_input_state.json"
    content = b'{"pending": [], "guarded": []}'
    state.write_bytes(content)
    original = Path.read_text

    def read(path, *args, **kwargs):
        if path == state:
            raise PermissionError(VALUE)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    h = Harness(tmp_path)
    assert h.input._guarded == {}
    assert next(tmp_path.glob("secret_input_state.json.corrupt-*")).read_bytes() == content
    assert len(caplog.records) == 1
    assert VALUE not in caplog.text


async def test_foreign_marker_and_slash_commands_are_ordinary(h):
    foreign = message(f"{PROMPT_MARKER}\nrequest", user=OTHER, chat=USER)
    assert not await settled_intercept(h.input, update(message("/ordinary", mid=3, reply=foreign)))
    assert not await settled_intercept(h.input, update(message("/ordinary", mid=4)))
    await h.command()
    h.clock.now += 240
    assert not await settled_intercept(h.input, update(message(VALUE, mid=5)))


@pytest.mark.parametrize("extra", [False, True])
async def test_denied_user_never_gets_delete_failure_reply(h, caplog, extra):
    caplog.set_level(logging.DEBUG)
    await h.command()
    prompt = h.prompt()
    h.allowed.remove(USER)
    h.bot.delete_failures = 3
    before = len(h.bot.sent)
    text = f"/secret set TEST_KEY {VALUE}" if extra else VALUE
    target = message(text, mid=8, reply=None if extra else prompt)
    assert await settled_intercept(h.input, update(target))
    assert h.bot.deleted == [(USER, 8)] * 3
    assert len(h.bot.sent) == before
    assert not h.path.exists()
    assert json.loads(h.state.read_text())["guarded"] == [
        {"chat_id": USER, "message_id": 8, "at": 1000.0, "deleted": False}
    ]
    assert VALUE not in caplog.text


@pytest.fixture
def app_harness(tmp_path, monkeypatch):
    from ductor_bot.config import AgentConfig
    from ductor_bot.messenger.telegram import app

    init("en")
    monkeypatch.delenv("DUCTOR_SECRETS_FILE", raising=False)
    bots = []

    def create(agent_name="main"):
        home = tmp_path if agent_name == "main" else tmp_path / "agents" / agent_name
        home.mkdir(parents=True, exist_ok=True)
        fake = FakeBot()
        fake.get_my_commands = AsyncMock(return_value=[])
        fake.set_my_commands = AsyncMock()
        fake.delete_my_commands = AsyncMock()
        monkeypatch.setattr(app, "Bot", lambda **_kwargs: fake)
        config = AgentConfig(
            telegram_token="303:synthetic-token",
            allowed_user_ids=[USER],
            ductor_home=str(home),
        )
        bot = app.TelegramBot(config, agent_name=agent_name)
        bots.append(bot)
        return bot, fake

    return create


@pytest.mark.parametrize("agent_name", ["main", "helper"])
def test_app_connects_secret_input(app_harness, agent_name):
    bot, fake = app_harness(agent_name)
    handler = bot._dp._secret_input
    assert isinstance(handler, SecretInput)
    assert handler is bot._secret_input
    assert handler._is_main is (agent_name == "main")
    assert handler._bot is fake
    assert set(handler._allowed_users) == {USER}
    home = Path(bot._config.ductor_home)
    expected_home = home if agent_name == "main" else home.parent.parent
    assert handler._home == expected_home
    assert handler._store_path == expected_home / "secrets.env"
    assert bot._dp._journal is bot._journal


async def test_app_routes_secret_callback_once(app_harness, monkeypatch):
    bot, fake = app_harness()
    query = callback("sec:x")
    answer = AsyncMock()
    monkeypatch.setattr(CallbackQuery, "answer", answer)
    handler = AsyncMock(wraps=bot._secret_input.handle_callback)
    monkeypatch.setattr(bot._secret_input, "handle_callback", handler)
    normal = AsyncMock()
    monkeypatch.setattr(bot, "_route_special_callback", normal)

    await bot._on_callback_query(query)

    handler.assert_awaited_once_with(query)
    assert fake.answered == [query.id]
    answer.assert_not_awaited()
    normal.assert_not_awaited()


async def test_app_keeps_other_callback_route(app_harness, monkeypatch):
    from ductor_bot.messenger.telegram.topic import get_session_key

    bot, fake = app_harness()
    query = callback("mq:synthetic")
    answer = AsyncMock()
    monkeypatch.setattr(CallbackQuery, "answer", answer)
    handler = AsyncMock()
    monkeypatch.setattr(bot._secret_input, "handle_callback", handler)
    normal = AsyncMock(return_value=True)
    monkeypatch.setattr(bot, "_route_special_callback", normal)

    await bot._on_callback_query(query)

    answer.assert_awaited_once()
    handler.assert_not_awaited()
    assert fake.answered == []
    normal.assert_awaited_once_with(
        get_session_key(query.message), query.message.message_id, query.data, thread_id=None
    )


@pytest.mark.parametrize("agent_name", ["main", "helper"])
async def test_app_publishes_main_only_secret(app_harness, agent_name):
    from aiogram.types import BotCommand

    from ductor_bot.commands import get_main_only_commands
    from ductor_bot.messenger.telegram import app

    bot, fake = app_harness(agent_name)
    expected = app._BOT_COMMANDS + (
        [BotCommand(command=cmd, description=desc) for cmd, desc in get_main_only_commands()]
        if agent_name == "main"
        else []
    )
    await bot._sync_commands()
    fake.set_my_commands.assert_awaited_once_with(expected)
    commands = fake.set_my_commands.call_args.args[0]
    assert [item.command for item in commands[: len(app._BOT_COMMANDS)]] == [
        item.command for item in app._BOT_COMMANDS
    ]
    assert ("secret" in [item.command for item in commands]) is (agent_name == "main")
    assert "secret" not in app._CMD_DESC

    fake.set_my_commands.reset_mock()
    fake.get_my_commands.side_effect = [[], [], expected]
    await bot._sync_commands()
    fake.set_my_commands.assert_not_awaited()


@pytest.mark.parametrize("level", [logging.INFO, logging.DEBUG])
async def test_app_secret_flow_skips_journal_and_handlers(app_harness, monkeypatch, caplog, level):
    caplog.set_level(level)
    bot, fake = app_harness()
    middleware = AsyncMock()
    bot._router.message.outer_middleware(middleware)
    orchestrator = MagicMock()
    orchestrator.handle_message = AsyncMock()
    orchestrator.handle_message_streaming = AsyncMock()
    bot._orchestrator = orchestrator
    observed = MagicMock(wraps=bot._journal)
    bot._dp._journal = observed

    async def source(_bot, **_kwargs):
        yield update(message("/secret set TEST_KEY", mid=501))
        yield update(message(VALUE, mid=502))

    monkeypatch.setattr(Dispatcher, "_listen_updates", staticmethod(source))
    yielded = []
    async for item in bot._dp._listen_updates(fake):
        yielded.append(item)
        await bot._dp.feed_update(fake, item)

    assert yielded == []
    assert bot._journal.pending() == []
    assert observed.record_handled.call_args_list == [((501,),), ((502,),)]
    observed.record_pending.assert_not_called()
    observed.claim.assert_not_called()
    middleware.assert_not_awaited()
    assert orchestrator.mock_calls == []
    assert list_keys(Path(bot._config.ductor_home) / "secrets.env") == ["TEST_KEY"]
    await drain_deletions(bot._secret_input)
    assert value_deletions(fake) == [(USER, 502)]
    assert VALUE not in caplog.text
    for path in (bot._journal._path, bot._secret_input._state_path):
        assert VALUE not in path.read_text()



@pytest.mark.parametrize(
    "case", ["removed_command", "removed_value", "removed_callback", "added_user"]
)
async def test_app_secret_input_uses_reloaded_allowlist(app_harness, case):
    bot, fake = app_harness()
    handler = bot._dp._secret_input
    path = Path(bot._config.ductor_home) / "secrets.env"
    if case == "removed_value":
        assert await settled_intercept(handler, update(message("/secret set TEST_KEY")))
    elif case == "removed_callback":
        set_secret(path, "TEST_KEY", VALUE)
    fake.sent.clear()

    config = bot._config.model_copy(update={"allowed_user_ids": [OTHER]})
    bot._on_auth_hot_reload(config, {"allowed_user_ids": [OTHER]})

    if case == "removed_command":
        assert await settled_intercept(handler, update(message("/secret set TEST_KEY", mid=2)))
        assert fake.sent == []
        assert handler._pending == {}
        assert list_keys(path) == []
    elif case == "removed_value":
        assert await settled_intercept(handler, update(message(VALUE, mid=2)))
        assert list_keys(path) == []
        assert fake.sent == []
        assert value_deletions(fake) == [(USER, 2)]
    elif case == "removed_callback":
        query = callback("sec:del:TEST_KEY")
        await bot._on_callback_query(query)
        assert list_keys(path) == ["TEST_KEY"]
        assert fake.sent == []
        assert fake.answered == [query.id]
    else:
        assert await settled_intercept(handler,
            update(message("/secret set TEST_KEY", mid=2, user=OTHER))
        )
        assert len(fake.sent) == 1
        assert fake.sent[0][0].text.startswith(PROMPT_MARKER)
        assert await settled_intercept(handler, update(message(VALUE, mid=3, user=OTHER)))
        assert list_keys(path) == ["TEST_KEY"]
        assert value_deletions(fake) == [(OTHER, 3)]
        query = callback("sec:del:TEST_KEY", user=OTHER)
        await bot._on_callback_query(query)
        assert list_keys(path) == []
        assert fake.answered == [query.id]
    assert handler._allowed_users is bot._allowed_users


@pytest.mark.parametrize("level", [logging.INFO, logging.DEBUG])
@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("quoted", [False, True])
@pytest.mark.parametrize("initial_failures", [0, 3])
async def test_app_consumes_consecutive_guarded_quotes(  # noqa: PLR0917
    app_harness, monkeypatch, tmp_path, level, restart, quoted, initial_failures
):
    import io

    bot, fake = app_harness()
    monkeypatch.setattr(secret_input.asyncio, "sleep", AsyncMock())
    await settled_intercept(bot._secret_input, update(message("/secret set TEST_KEY")))
    original = message(VALUE, mid=601)
    fake.delete_failures = initial_failures
    if restart or initial_failures:
        assert await settled_intercept(bot._secret_input, update(original))
    if restart:
        bot, fake = app_harness()
    middleware = AsyncMock()
    bot._router.message.outer_middleware(middleware)
    orchestrator = MagicMock()
    orchestrator.handle_message = AsyncMock()
    orchestrator.handle_message_streaming = AsyncMock()
    bot._orchestrator = orchestrator
    observed = MagicMock(wraps=bot._journal)
    bot._dp._journal = observed
    quotes = [
        update(message("follow-up", mid=mid, reply=original, quote=VALUE if quoted else None))
        for mid in (602, 603)
    ]

    async def source(_bot, **_kwargs):
        if not restart and not initial_failures:
            yield update(original)
            await drain_deletions(bot._secret_input)
        for item in quotes:
            yield item
            await drain_deletions(bot._secret_input)

    monkeypatch.setattr(Dispatcher, "_listen_updates", staticmethod(source))
    console = io.StringIO()
    log_path = tmp_path / "capture.log"
    handlers = [logging.StreamHandler(console), logging.FileHandler(log_path)]
    root = logging.getLogger()
    previous_level = root.level
    root.setLevel(level)
    for handler in handlers:
        root.addHandler(handler)
    try:
        yielded = []
        async for item in bot._dp._listen_updates(fake):
            yielded.append(item)
            await bot._dp.feed_update(fake, item)
    finally:
        for handler in handlers:
            root.removeHandler(handler)
            handler.close()
        root.setLevel(previous_level)
    assert yielded == []
    assert bot._journal.pending() == []
    expected_ids = ([601] if not restart and not initial_failures else []) + [602, 603]
    assert observed.record_handled.call_args_list == [((mid,),) for mid in expected_ids]
    observed.record_pending.assert_not_called()
    middleware.assert_not_awaited()
    assert orchestrator.mock_calls == []
    assert VALUE not in bot._journal._path.read_text()
    assert VALUE not in console.getvalue()
    assert VALUE not in log_path.read_text()
    expected_calls = ([(USER, 601)] * (initial_failures + 1)) if not restart else (
        [(USER, 601)] if initial_failures else []
    )
    assert value_deletions(fake) == [*expected_calls, (USER, 602), (USER, 603)]


@pytest.mark.parametrize("tilde", [False, True])
async def test_app_home_survives_working_directory_change(tmp_path, monkeypatch, tilde):
    from ductor_bot.config import AgentConfig
    from ductor_bot.messenger.telegram import app

    init("en")
    monkeypatch.delenv("DUCTOR_SECRETS_FILE", raising=False)
    temporary_home = tmp_path / "user-home"
    temporary_home.mkdir()
    monkeypatch.setenv("HOME", str(temporary_home))
    expanded_home = temporary_home / "synthetic-home"
    configured_home = "~/synthetic-home" if tilde else str(expanded_home)
    config = AgentConfig(
        telegram_token="303:synthetic-token",
        allowed_user_ids=[USER],
        ductor_home=configured_home,
    )
    bots = []
    for name in ("first-cwd", "second-cwd"):
        cwd = tmp_path / name
        cwd.mkdir()
        monkeypatch.chdir(cwd)
        fake = FakeBot()
        monkeypatch.setattr(app, "Bot", lambda fake=fake, **_kwargs: fake)
        bot = app.TelegramBot(config)
        bots.append(bot)
        if len(bots) == 1:
            assert await settled_intercept(bot._secret_input, update(message("/secret set TEST_KEY")))
            assert (expanded_home / "secret_input_state.json").is_file()
        assert not (cwd / "~").exists()
    bot = bots[-1]
    assert bot._secret_input._home == expanded_home
    assert bot._secret_input._store_path == expanded_home / "secrets.env"
    assert bot._secret_input._pending[(USER, USER)].key == "TEST_KEY"

    async def source(_bot, **_kwargs):
        yield update(message(VALUE, mid=702))

    monkeypatch.setattr(Dispatcher, "_listen_updates", staticmethod(source))
    assert [item async for item in bot._dp._listen_updates(fake)] == []
    assert bot._journal.pending() == []
    assert bot._journal.is_handled(702)
    assert VALUE not in bot._journal._path.read_text()
    assert (expanded_home / "secrets.env").read_text() == f"TEST_KEY={VALUE}\n"


def test_secret_input_expands_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    instance = SecretInput(
        FakeBot(), is_main=True, allowed_users={USER},
        ductor_home=Path("~/synthetic-home"), store_path=tmp_path / "synthetic-store",
    )
    assert instance._home == tmp_path / "synthetic-home"


async def test_guarded_retention_prunes_on_read_and_write(h):
    await h.command()
    assert await settled_intercept(h.input, update(message(VALUE, mid=8)))
    h.clock.now += 48 * 60 * 60 - 1
    h.restart()
    assert h.input._guarded[(USER, 8)].deleted
    h.clock.now += 1
    h.restart()
    assert h.input._guarded == {}
    h.input._save()
    assert json.loads(h.state.read_text())["guarded"] == []
    await h.command("/secret set NEXT_KEY", mid=10)
    assert await settled_intercept(h.input, update(message(VALUE, mid=11)))
    h.clock.now += 48 * 60 * 60
    h.input._save()
    assert h.input._guarded == {}
    assert json.loads(h.state.read_text())["guarded"] == []


@pytest.mark.parametrize("transition", [False, True])
async def test_guarded_persistence_precedes_deletion_and_retries(h, monkeypatch, transition):
    await h.command()
    original_save = secret_input.atomic_text_save
    original_delete = h.bot.delete_message
    writes = 0
    snapshots = []

    def save(path, content):
        nonlocal writes
        row = json.loads(content)["guarded"][0]
        if row["deleted"] is transition:
            writes += 1
            if writes < 3:
                raise OSError(VALUE)
        original_save(path, content)

    async def delete(chat_id, message_id):
        if message_id == 8:
            snapshots.append([row for row in json.loads(h.state.read_text())["guarded"]
                              if row["message_id"] == 8])
        return await original_delete(chat_id, message_id)

    monkeypatch.setattr(secret_input, "atomic_text_save", save)
    monkeypatch.setattr(h.bot, "delete_message", delete)
    assert await settled_intercept(h.input, update(message(VALUE, mid=8)))
    assert snapshots == [[
        {"chat_id": USER, "message_id": 8, "at": 1000.0, "deleted": False}
    ]]
    assert [row for row in json.loads(h.state.read_text())["guarded"]
            if row["message_id"] == 8] == [
        {"chat_id": USER, "message_id": 8, "at": 1000.0, "deleted": True}
    ]
    assert writes >= 3
    assert [call.args[0] for call in secret_input.asyncio.sleep.await_args_list if call.args[0] <= 4] == [0.5, 1.0]

@pytest.mark.parametrize("thread", [None, 707])
@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("/secret", "usage"),
        ("/secret list", "list_empty"),
        ("/secret cancel", "nothing_pending"),
        (f"/secret set TEST_KEY {VALUE}", "extra_args"),
    ],
)
async def test_command_reply_thread(h, thread, command, expected):
    await h.command(command, thread=thread)
    assert h.texts()[-1] == t(f"secret.{expected}")
    assert h.bot.sent[-1][1]["message_thread_id"] == thread


async def test_dm_topic_request_and_result(h):
    await h.command("/secret set NEW_KEY", thread=707)
    assert isinstance(h.bot.sent[-1][1]["reply_markup"], ForceReply)
    assert await settled_intercept(h.input, update(message(VALUE, mid=2, thread=707, reply=h.prompt())))
    assert h.path.read_text() == f"NEW_KEY={VALUE}\n"
    assert h.texts()[-1] == t("secret.saved").format(key="NEW_KEY", length=len(VALUE))
    assert [kwargs["message_thread_id"] for _, kwargs in h.bot.sent] == [707, 707]


async def test_dm_topic_confirmation_callbacks(h):
    set_secret(h.path, "TEST_KEY", "synthetic-existing")
    await h.command(thread=707)
    assert (
        h.bot.sent[-1][1]["reply_markup"].inline_keyboard[0][0].callback_data == "sec:ow:TEST_KEY"
    )
    assert await h.input.handle_callback(callback("sec:ow:TEST_KEY", thread=707))
    assert isinstance(h.bot.sent[-1][1]["reply_markup"], ForceReply)
    await h.command("/secret cancel", thread=707)
    assert h.texts()[-1] == t("secret.cancelled")
    await h.command("/secret delete TEST_KEY", thread=707)
    assert (
        h.bot.sent[-1][1]["reply_markup"].inline_keyboard[0][0].callback_data == "sec:del:TEST_KEY"
    )
    assert await h.input.handle_callback(callback("sec:del:TEST_KEY", thread=707))
    assert h.texts()[-1] == t("secret.deleted").format(key="TEST_KEY")
    assert list_keys(h.path) == []
    assert all(kwargs["message_thread_id"] == 707 for _, kwargs in h.bot.sent)


async def test_dm_topic_quote_refusal(h):
    await h.command(thread=707)
    original = message(VALUE, mid=2, thread=707, reply=h.prompt())
    assert await settled_intercept(h.input, update(original))
    assert await settled_intercept(h.input,
        update(message("quoted message", mid=3, thread=707, reply=original, quote=VALUE))
    )
    assert h.texts()[-1] == t("secret.quoted_refused")
    assert h.bot.sent[-1][1]["message_thread_id"] == 707


async def test_group_topic_denial_stays_in_topic(h):
    await h.command("/secret", chat=-404, kind="supergroup", thread=707)
    assert h.texts() == [t("secret.private_only")]
    assert h.bot.sent[-1][1]["message_thread_id"] == 707
    assert h.input._pending == {}


@pytest.mark.parametrize("command", ["/Secret", "/SECRET", "/secret@synthetic_bot", "/SECRET@SYNTHETIC_BOT"])
async def test_exact_secret_command_variants(h, command):
    assert await h.input.intercept(update(message(f"{command} set TEST_KEY extra")))
    assert h.texts()[-1] == t("secret.extra_args")


@pytest.mark.parametrize(
    "text", ["/secrets x", "/secretary", "/secret@bot/extra", "/secret@bot@other", "/secret@"]
)
async def test_other_command_words_are_not_secret_commands(h, text):
    assert not await h.input.intercept(update(message(text)))


async def test_pending_accepts_slash_value(h):
    await h.command()
    assert await h.input.intercept(update(message("/x", mid=8)))
    assert h.path.read_text() == "TEST_KEY=/x\n"


@pytest.mark.parametrize("elapsed", [120, 239.999, 240])
async def test_expired_value_grace_boundary(h, elapsed):
    await h.command()
    h.clock.now += elapsed
    consumed = await h.input.intercept(update(message(VALUE, mid=8)))
    assert consumed is (elapsed < 240)
    assert not h.path.exists()
    if consumed:
        await drain_deletions(h.input)
        assert (USER, 8) in h.bot.deleted
        assert h.texts()[-1] == t("secret.expired_value")
        assert (USER, USER) in h.input._pending


async def test_guard_persistence_stops_after_five_attempts(h, monkeypatch, caplog):
    save = MagicMock(side_effect=OSError(VALUE))
    monkeypatch.setattr(h.input, "_save", save)
    await h.input._persist_guarded()
    assert save.call_count == 5
    assert [call.args[0] for call in secret_input.asyncio.sleep.await_args_list if call.args[0] <= 4] == [0.5, 1, 2, 4]
    assert "guarded_write_failed" in caplog.text
    assert VALUE not in caplog.text


async def test_blocked_delete_does_not_block_interception(h, monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()

    async def delete(*_args):
        started.set()
        await release.wait()

    monkeypatch.setattr(h.bot, "delete_message", delete)
    try:
        assert await h.input.intercept(update(message("/secret set TEST_KEY extra")))
        await asyncio.wait_for(started.wait(), 1)
        assert await asyncio.wait_for(h.input.intercept(update(message("/secret list", mid=2))), 1)
        assert not h.input._lock.locked()
    finally:
        release.set()
        await drain_deletions(h.input)
    assert not h.input._delete_tasks


async def test_secret_store_calls_use_worker_threads(h, monkeypatch):
    import threading

    event_thread = threading.get_ident()
    calls = []
    for name in ("list_keys", "set_secret", "delete_secret"):
        original = getattr(secret_input, name)

        def call(*args, _original=original, _name=name, **kwargs):
            calls.append((_name, threading.get_ident()))
            return _original(*args, **kwargs)

        monkeypatch.setattr(secret_input, name, call)
    await h.command()
    assert await h.input.intercept(update(message(VALUE, mid=8)))
    await h.command("/secret list")
    await h.command("/secret delete TEST_KEY")
    await h.input.handle_callback(callback("sec:del:TEST_KEY"))
    assert {name for name, _ in calls} == {"list_keys", "set_secret", "delete_secret"}
    assert all(thread != event_thread for _, thread in calls)


async def test_expired_value_never_enters_journal_or_handlers(h, monkeypatch):
    await h.command()
    h.clock.now += 120
    journal = UpdateJournal(h.home / "journal.json", clock=h.clock)
    dp = PollingDispatcher(journal=journal, secret_input=h.input)
    assert await listen(dp, h, [update(message(VALUE, mid=8))], monkeypatch) == []
    assert journal.pending() == []
    assert journal.is_handled(8)
    assert VALUE not in journal._path.read_text()
    assert (USER, 8) in h.bot.deleted


@pytest.mark.parametrize("reply_first", [False, True])
@pytest.mark.parametrize("after_expiry", [0, 119.999])
async def test_repeated_expired_values_remain_quarantined(h, monkeypatch, reply_first, after_expiry):
    await h.command()
    expiry = h.input._pending[(USER, USER)].expires_at
    h.clock.now = expiry + after_expiry
    journal = UpdateJournal(h.home / "journal.json", clock=h.clock)
    dp = PollingDispatcher(journal=journal, secret_input=h.input)
    incoming = [
        update(message(VALUE, mid=8, reply=h.prompt() if reply_first else None)),
        update(message(VALUE, mid=9)),
    ]
    assert await listen(dp, h, incoming, monkeypatch) == []
    assert journal.pending() == []
    assert all(journal.is_handled(mid) for mid in (8, 9))
    assert VALUE not in journal._path.read_text()
    assert not h.path.exists()
    assert value_deletions(h.bot) == [(USER, 8), (USER, 9)]
    expected = [t("secret.expired_value"), t("secret.expired_value")]
    assert h.texts()[-2:] == expected
    assert h.input._pending[(USER, USER)].expires_at == expiry

    h.clock.now = expiry + 120
    later = update(message(VALUE, mid=10))
    assert await listen(dp, h, [later], monkeypatch) == [later]
    assert (USER, USER) not in h.input._pending
    assert value_deletions(h.bot) == [(USER, 8), (USER, 9)]
    assert h.texts()[-2:] == expected
    assert not h.path.exists()


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("case", ["slash", "reply", "caption", "marked", "guarded"])
async def test_grace_messages_skip_normal_processing(h, monkeypatch, caplog, case, *, restart):
    await h.command()
    reply = None
    if case == "guarded":
        reply = message("earlier guarded input", mid=7)
        await h.input._delete(USER, 7)
        await drain_deletions(h.input)
    elif case == "reply":
        reply = message("unrelated message", mid=7)
    elif case == "marked":
        reply = h.prompt()
    target = message("/new" if case == "slash" else VALUE, mid=9, reply=reply)
    if case == "caption":
        target = message(None, mid=9).model_copy(update={"caption": VALUE})
    h.clock.now += 120
    if restart:
        h.restart()
    journal = UpdateJournal(h.home / "journal.json", clock=h.clock)
    dp = PollingDispatcher(journal=journal, secret_input=h.input)
    middleware = AsyncMock()
    handler = AsyncMock()
    router = Router()
    router.message.outer_middleware(middleware)

    async def handle(event, **data):
        await handler(event, **data)

    router.message.register(handle)
    dp.include_router(router)
    assert await listen(dp, h, [update(target)], monkeypatch, process=True) == []
    middleware.assert_not_awaited()
    handler.assert_not_awaited()
    assert journal.pending() == []
    assert journal.is_handled(9)
    assert (USER, 9) in h.bot.deleted
    expected = "quoted_refused" if case == "guarded" else "expired_value"
    assert h.texts()[-1] == t(f"secret.{expected}")
    assert not h.path.exists()
    assert VALUE not in h.state.read_text()
    assert VALUE not in journal._path.read_text()
    assert VALUE not in caplog.text


async def test_waiting_caption_uses_non_text_response(h, monkeypatch):
    await h.command()
    target = message(None, mid=9).model_copy(update={"caption": VALUE})
    journal = UpdateJournal(h.home / "journal.json", clock=h.clock)
    dp = PollingDispatcher(journal=journal, secret_input=h.input)
    assert await listen(dp, h, [update(target)], monkeypatch) == []
    assert h.texts()[-1] == t("secret.not_text")
    assert (USER, 9) in h.bot.deleted
    assert (USER, USER) in h.input._pending
    assert journal.pending() == []
    assert journal.is_handled(9)
    assert not h.path.exists()
    assert VALUE not in journal._path.read_text()


@pytest.mark.parametrize("command", ["/secret list", "/SECRET@SYNTHETIC_BOT list"])
async def test_grace_keeps_existing_secret_command_detection(h, command):
    await h.command()
    h.clock.now += 120
    await h.command(command, mid=9)
    assert h.texts()[-1] == t("secret.list_empty")
    assert (USER, 9) not in h.bot.deleted


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("case", ["slash", "reply", "caption"])
async def test_after_grace_messages_resume_normal_processing(h, monkeypatch, case, *, restart):
    await h.command()
    target = message(
        "/new" if case == "slash" else VALUE,
        mid=9,
        reply=message("unrelated message", mid=7) if case == "reply" else None,
    )
    if case == "caption":
        target = message(None, mid=9).model_copy(update={"caption": VALUE})
    h.clock.now += 240
    if restart:
        h.restart()
        assert h.input._pending == {}
    journal = UpdateJournal(h.home / "journal.json", clock=h.clock)
    dp = PollingDispatcher(journal=journal, secret_input=h.input)
    incoming = update(target)
    assert await listen(dp, h, [incoming], monkeypatch) == [incoming]
    assert [entry.update_id for entry in journal.pending()] == [9]
    assert (USER, 9) not in h.bot.deleted
    assert h.input._pending == {}


async def test_grace_guarded_reply_retries_original_deletion(h, monkeypatch):
    await h.command()
    original = message(VALUE, mid=7)
    h.bot.delete_failures = 3
    await h.input._delete(USER, 7)
    await drain_deletions(h.input)
    assert not h.input._guarded[(USER, 7)].deleted
    h.bot.deleted.clear()
    h.clock.now += 120
    target = update(message("reply to guarded input", mid=9, reply=original))
    journal = UpdateJournal(h.home / "journal.json", clock=h.clock)
    dp = PollingDispatcher(journal=journal, secret_input=h.input)
    handler = AsyncMock()
    router = Router()

    async def handle(event, **data):
        await handler(event, **data)

    router.message.register(handle)
    dp.include_router(router)
    assert await listen(dp, h, [target], monkeypatch, process=True) == []
    handler.assert_not_awaited()
    assert h.bot.deleted == [(USER, 7), (USER, 9)]
    assert h.texts()[-1] == t("secret.quoted_refused")
    assert journal.pending() == []
    assert journal.is_handled(9)


@pytest.mark.parametrize("finish", ["saved", "button", "cancel", "replace", "overwrite"])
async def test_prompt_deleted_when_request_finishes(h, finish):
    await h.command()
    prompt = h.prompt()
    if finish == "saved":
        assert await settled_intercept(h.input, update(message(VALUE, mid=2)))
    elif finish == "button":
        await h.input.handle_callback(callback("sec:x"))
    elif finish == "cancel":
        await h.command("/secret cancel")
    elif finish == "replace":
        await h.command("/secret set NEXT_KEY")
    else:
        set_secret(h.path, "EXISTING_KEY", "synthetic-existing")
        await h.command("/secret set EXISTING_KEY")
    await drain_deletions(h.input)
    assert h.bot.deleted.count((USER, prompt.message_id)) == 1
    if finish == "replace":
        assert (USER, h.prompt().message_id) not in h.bot.deleted


async def arm_expiry(h, monkeypatch, *, restart_after=None):
    registered, release = asyncio.Event(), asyncio.Event()
    delays = []

    async def sleep(delay):
        if delay > 4:
            delays.append(delay)
            registered.set()
            await release.wait()

    monkeypatch.setattr(secret_input.asyncio, "sleep", AsyncMock(side_effect=sleep))
    await h.command()
    prompt = h.prompt()
    await registered.wait()
    if restart_after is not None:
        registered.clear()
        h.clock.now += restart_after
        h.restart()
        await registered.wait()
    return prompt, release, delays


@pytest.mark.parametrize("restart_after", [None, 35])
async def test_prompt_expiry_edits_live_and_restarted_pending_prompt(h, monkeypatch, restart_after):
    prompt, release, delays = await arm_expiry(h, monkeypatch, restart_after=restart_after)
    assert delays == ([120] if restart_after is None else [120, 85])
    assert (USER, prompt.message_id) not in h.bot.deleted
    h.clock.now = 1120
    release.set()
    await asyncio.gather(*[task for task in h.input._delete_tasks
                           if task.get_name() == "secret-prompt-expiry"])
    await drain_deletions(h.input)
    assert h.bot.deleted.count((USER, prompt.message_id)) == 0
    assert h.bot.edited == [(f"{PROMPT_MARKER}\n{t('secret.expired')}",
                             {"chat_id": USER, "message_id": prompt.message_id,
                              "parse_mode": None})]
    assert (USER, USER) in h.input._pending
    assert await settled_intercept(h.input, update(message(VALUE, mid=2, reply=prompt)))
    assert h.texts()[-1] == t("secret.expired_value")
    assert not h.path.exists()
    assert h.bot.deleted.count((USER, prompt.message_id)) == 0


@pytest.mark.parametrize("elapsed", [120, 150, 241])
async def test_load_deletes_expired_and_old_prompts(h, elapsed):
    await h.command()
    prompt = h.prompt()
    h.clock.now += elapsed
    h.restart()
    expiry_tasks = [task for task in h.input._delete_tasks
                    if task.get_name() == "secret-prompt-expiry"]
    await asyncio.gather(*expiry_tasks)
    await drain_deletions(h.input)
    assert h.bot.deleted.count((USER, prompt.message_id)) == 1
    assert bool(h.input._pending) is (elapsed < 240)


@pytest.mark.parametrize("failure", ["invalid_value", "save"])
async def test_prompt_kept_when_input_can_be_retried(h, monkeypatch, failure):
    await h.command()
    prompt = h.prompt()
    if failure == "save":
        monkeypatch.setattr(secret_input, "set_secret", MagicMock(side_effect=OSError(VALUE)))
    value = " " if failure == "invalid_value" else VALUE
    assert await settled_intercept(h.input, update(message(value, mid=2)))
    assert (USER, prompt.message_id) not in h.bot.deleted
    assert h.input._pending[(USER, USER)].prompt_message_id == prompt.message_id


async def test_zero_prompt_id_does_not_schedule_deletion(h):
    h.input._pending[(USER, USER)] = secret_input._Pending(USER, USER, "TEST_KEY", 0, 1000)
    await h.command("/secret cancel")
    assert h.bot.deleted == []


async def test_prompt_deletion_failure_has_no_notice(h, caplog):
    await h.command()
    prompt = h.prompt()
    h.bot.delete_failures = 3
    await h.command("/secret cancel")
    assert h.bot.deleted == [(USER, prompt.message_id)] * 3
    assert h.texts()[-1] == t("secret.cancelled")
    assert t("secret.delete_failed") not in h.texts()
    assert "message_delete_failed" in caplog.text
    assert VALUE not in caplog.text


async def test_replaced_timer_does_not_delete_new_prompt(h, monkeypatch):
    prompt, release, _ = await arm_expiry(h, monkeypatch)
    async def wait_for_new_expiry(delay):
        if delay > 4:
            await asyncio.Future()
    monkeypatch.setattr(secret_input.asyncio, "sleep", AsyncMock(side_effect=wait_for_new_expiry))
    h.clock.now = 1030
    await h.command("/secret set NEXT_KEY")
    replacement = h.prompt()
    h.clock.now = 1120
    release.set()
    await REAL_SLEEP(0)
    await drain_deletions(h.input)
    assert h.bot.deleted.count((USER, prompt.message_id)) == 1
    assert (USER, replacement.message_id) not in h.bot.deleted


async def test_drain_deletions_yields_for_completed_tasks(monkeypatch):
    task = asyncio.create_task(REAL_SLEEP(0))
    await task
    instance = MagicMock(_delete_tasks={task})
    loop = asyncio.get_running_loop()
    loop.call_soon(instance._delete_tasks.discard, task)
    completed = loop.create_future()
    completed.set_result([None])
    calls = 0

    def gather_completed(*tasks):
        nonlocal calls
        calls += 1
        assert calls == 1, "Deletion callbacks must run before gathering completed tasks again"
        assert tasks == (task,)
        # Model the completed-future fast path used by newer asyncio versions.
        return completed

    with monkeypatch.context() as patch:
        patch.setattr(asyncio, "gather", gather_completed)
        await drain_deletions(instance)
    assert not instance._delete_tasks


async def test_close_cancels_expiry_before_session_close(app_harness, monkeypatch):
    from ductor_bot.infra.shutdown_budget import ShutdownBudget

    bot, fake = app_harness()
    registered, release = asyncio.Event(), asyncio.Event()
    after_close = []
    fake.session = MagicMock(close=AsyncMock())
    fake.delete_webhook = AsyncMock()

    async def sleep(delay):
        if delay > 4:
            registered.set()
            await release.wait()

    async def delete(chat_id, message_id):
        if fake.session.close.await_count:
            after_close.append((chat_id, message_id))
        fake.deleted.append((chat_id, message_id))

    monkeypatch.setattr(secret_input.asyncio, "sleep", AsyncMock(side_effect=sleep))
    monkeypatch.setattr(fake, "delete_message", delete)
    assert await bot._secret_input.intercept(update(message("/secret set TEST_KEY")))
    await registered.wait()
    await bot.shutdown(ShutdownBudget(total=1))
    assert not bot._secret_input._delete_tasks
    fake.session.close.assert_awaited_once()
    release.set()
    await REAL_SLEEP(0)
    assert not after_close
    assert not fake.deleted


@pytest.mark.parametrize("finish", [False, True])
async def test_close_reaps_inflight_prompt_deletion_and_restores(h, monkeypatch, finish):
    await h.command()
    prompt_id = h.prompt().message_id
    entered, release = asyncio.Event(), asyncio.Event()
    original = h.bot.delete_message

    async def delete(chat_id, message_id):
        entered.set()
        await release.wait()
        return await original(chat_id, message_id)

    monkeypatch.setattr(h.bot, "delete_message", delete)
    assert await h.input.intercept(update(message("/secret cancel", mid=2)))
    await entered.wait()
    if finish:
        closing = asyncio.create_task(h.input.close())
        await REAL_SLEEP(0)
        assert not closing.done()
        release.set()
        await closing
    else:
        await h.input.close(budget_seconds=0)
    assert not h.input._delete_tasks
    state = json.loads(h.state.read_text())
    assert state["pending"] == []
    assert state["guarded"][0]["deleted"] is finish
    restored_bot = FakeBot()
    restored = SecretInput(restored_bot, is_main=True, allowed_users={USER},
                           ductor_home=h.home, store_path=h.path, clock=h.clock)
    await drain_deletions(restored)
    assert restored_bot.deleted == ([] if finish else [(USER, prompt_id)])
    assert not restored._delete_tasks


@pytest.mark.parametrize("failure", [False, True])
async def test_load_retries_only_recent_incomplete_guarded_deletions(h, failure):
    h.state.write_text(json.dumps({
        "pending": [],
        "guarded": [
            {"chat_id": USER, "message_id": 8, "at": h.clock.now, "deleted": False},
            {"chat_id": USER, "message_id": 9, "at": h.clock.now, "deleted": True},
            {"chat_id": USER, "message_id": 10,
             "at": h.clock.now - secret_input.GUARDED_SECONDS, "deleted": False},
        ],
    }))
    if failure:
        h.bot.delete_failures = 3
    h.restart()
    await drain_deletions(h.input)
    assert h.bot.deleted == [(USER, 8)] * (3 if failure else 1)
    assert not h.texts()
    assert h.input._guarded[(USER, 8)].deleted is not failure
    assert (USER, 10) not in h.input._guarded
    await h.input.close()


async def test_load_does_not_duplicate_pending_and_guarded_prompt_deletion(h):
    h.state.write_text(json.dumps({
        "pending": [{"chat_id": USER, "user_id": USER, "key": "TEST_KEY",
                     "prompt_message_id": 100, "expires_at": 1120}],
        "guarded": [{"chat_id": USER, "message_id": 100, "at": 1000, "deleted": False}],
    }))
    h.restart()
    await drain_deletions(h.input)
    assert h.bot.deleted == [(USER, 100)]
    assert not h.input._delete_tasks
    await h.input.close()


async def test_expired_edited_prompt_catches_reply_after_five_minutes(h, monkeypatch):
    prompt, release, _ = await arm_expiry(h, monkeypatch)
    h.clock.now = 1120
    release.set()
    await asyncio.gather(*[task for task in h.input._delete_tasks
                           if task.get_name() == "secret-prompt-expiry"])
    text, _ = h.bot.edited[0]
    assert text.splitlines()[0] == PROMPT_MARKER
    reply = prompt.model_copy(update={"text": text})
    h.clock.now = 1300
    assert await settled_intercept(h.input, update(message(VALUE, mid=2, reply=reply)))
    assert (USER, 2) in h.bot.deleted
    assert h.texts()[-1] == t("secret.expired")
    assert not h.path.exists()


async def test_prompt_expiry_edit_failure_has_no_notice(h, monkeypatch, caplog):
    monkeypatch.setattr(h.bot, "edit_message_text", AsyncMock(side_effect=RuntimeError(VALUE)))
    prompt, release, _ = await arm_expiry(h, monkeypatch)
    before = h.texts()
    h.clock.now = 1120
    release.set()
    await asyncio.gather(*[task for task in h.input._delete_tasks
                           if task.get_name() == "secret-prompt-expiry"])
    assert h.bot.deleted == []
    assert h.texts() == before
    assert "prompt_edit_failed" in caplog.text
    assert VALUE not in caplog.text
    assert h.input._pending[(USER, USER)].prompt_message_id == prompt.message_id
