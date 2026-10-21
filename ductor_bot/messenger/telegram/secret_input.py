"""Consume secret input before Telegram updates reach the journal or handlers."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from collections.abc import Callable, Collection
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from aiogram.types import (
    CallbackQuery,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)

from ductor_bot.i18n import t
from ductor_bot.infra.atomic_io import atomic_text_save
from ductor_bot.infra.env_secrets import load_env_secrets, main_home_of
from ductor_bot.infra.secret_store import (
    delete_secret,
    list_keys,
    set_secret,
    validate_key,
    validate_value,
)
from ductor_bot.messenger.telegram.topic import get_thread_id

if TYPE_CHECKING:
    from aiogram import Bot

logger = logging.getLogger(__name__)

PROMPT_MARKER = "🔐 secret-input"
PENDING_SECONDS = 120
MAX_VALUE_LENGTH = 4096
GUARDED_SECONDS = 48 * 60 * 60


def _text(name: str, **kwargs: object) -> str:
    # The translation API uses "key" as its lookup argument. Format this placeholder here.
    return t(f"secret.{name}").format_map(kwargs)


@dataclass(slots=True)
class _Pending:
    chat_id: int
    user_id: int
    key: str
    prompt_message_id: int
    expires_at: float


@dataclass(slots=True)
class _Guarded:
    chat_id: int
    message_id: int
    at: float
    deleted: bool


class SecretInput:
    """A private, main-only input flow with metadata that never contains values."""

    def __init__(  # noqa: PLR0913
        self,
        bot: Bot,
        *,
        is_main: bool,
        allowed_users: Collection[int],
        ductor_home: Path,
        store_path: Path,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._bot = bot
        self._is_main = is_main
        self._allowed_users = allowed_users
        self._home = main_home_of(ductor_home.expanduser())
        self._store_path = store_path
        self._state_path = self._home / "secret_input_state.json"
        self._clock = clock
        self._pending: dict[tuple[int, int], _Pending] = {}
        self._guarded: dict[tuple[int, int], _Guarded] = {}
        self._lock = asyncio.Lock()
        self._delete_tasks: set[asyncio.Task[None]] = set()
        if is_main:
            self._load()

    async def close(self, budget_seconds: float = 5.0) -> None:
        """Cancel expiry waits and finish or reap deletions before closing the bot."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, min(budget_seconds, 5.0))
        expiry = [task for task in self._delete_tasks if task.get_name() == "secret-prompt-expiry"]
        for task in expiry:
            task.cancel()
        await asyncio.gather(*expiry, return_exceptions=True)
        while tasks := {task for task in self._delete_tasks if not task.done()}:
            done, _ = await asyncio.wait(
                tasks,
                timeout=max(0.0, deadline - loop.time()),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                break
        remaining = list(self._delete_tasks)
        for task in remaining:
            task.cancel()
        await asyncio.gather(*remaining, return_exceptions=True)
        self._delete_tasks.difference_update(remaining)

    async def intercept(self, update: Update) -> bool:
        """True means the update must never reach normal message processing."""
        message = update.message
        if message is None:
            return False
        async with self._lock:
            return await self._intercept_message(message)

    async def _intercept_message(self, message: Message) -> bool:
        parts = self._command_parts(message.text)
        command = bool(parts)
        reply = message.reply_to_message
        quoted = (
            self._is_main
            and reply is not None
            and (message.chat.id, reply.message_id) in self._guarded
        )
        marked = (
            self._is_main
            and reply is not None
            and reply.from_user is not None
            and reply.from_user.id == self._bot.id
            and (reply.text or "").startswith(PROMPT_MARKER)
        )
        quoted = quoted and not marked
        user_id = message.from_user.id if message.from_user else 0
        now = self._clock()
        pending = self._pending_for_input(message.chat.id, user_id, now)
        waiting = (
            pending is not None
            and pending.expires_at > now
            and (message.text is not None or message.caption is not None)
        )
        expired_value = (
            pending is not None
            and pending.expires_at <= now < pending.expires_at + 120
            and (message.text is not None or message.caption is not None)
            and not command
        )
        if not (command or (self._is_main and (quoted or marked or waiting or expired_value))):
            return False
        extra = len(parts) > 3 and parts[1] in {"set", "delete"}
        denial = self._denial(message, user_id)
        if extra:
            await self._delete(
                message.chat.id,
                message.message_id,
                notify=denial != "",
                thread_id=get_thread_id(message),
            )
        if denial is not None:
            if not command:
                await self._delete(
                    message.chat.id,
                    message.message_id,
                    notify=False,
                    thread_id=get_thread_id(message),
                )
            if command and denial:
                await self._send(
                    message.chat.id, _text(f"{denial}"), thread_id=get_thread_id(message)
                )
            return True
        if quoted and reply is not None:
            await self._delete(message.chat.id, reply.message_id, thread_id=get_thread_id(message))
            await self._delete(
                message.chat.id, message.message_id, thread_id=get_thread_id(message)
            )
            await self._send(
                message.chat.id, _text("quoted_refused"), thread_id=get_thread_id(message)
            )
        elif expired_value:
            await self._delete(
                message.chat.id, message.message_id, thread_id=get_thread_id(message)
            )
            await self._send(
                message.chat.id, _text("expired_value"), thread_id=get_thread_id(message)
            )
        elif command:
            if extra:
                await self._send(
                    message.chat.id, _text("extra_args"), thread_id=get_thread_id(message)
                )
            else:
                await self._command(message, parts, user_id)
        else:
            await self._value(message, pending)
        return True

    def _pending_for_input(self, chat_id: int, user_id: int, now: float) -> _Pending | None:
        pending = self._pending.get((chat_id, user_id))
        if pending is not None and now >= pending.expires_at + 120:
            self._remove_pending(chat_id, user_id)
            return None
        return pending

    @staticmethod
    def _command_parts(text: str | None) -> list[str]:
        parts = (text or "").split(maxsplit=3)
        if not parts or re.fullmatch(r"/secret(?:@[a-z0-9_]+)?", parts[0], re.IGNORECASE) is None:
            return []
        parts[0] = "/secret"
        return parts

    def _denial(self, message: Message, user_id: int) -> str | None:
        if user_id not in self._allowed_users:
            return ""
        if message.chat.type != "private" or message.chat.id != user_id:
            return "private_only"
        if not self._is_main:
            return "main_only"
        return None

    async def _command(self, message: Message, parts: list[str], user_id: int) -> None:
        chat_id = message.chat.id
        action = parts[1] if len(parts) > 1 else ""
        if action == "cancel" and len(parts) == 2:
            removed = self._remove_pending(chat_id, user_id)
            await self._send(
                chat_id,
                _text("cancelled" if removed else "nothing_pending"),
                thread_id=get_thread_id(message),
            )
        elif action == "list" and len(parts) == 2:
            try:
                keys = await asyncio.to_thread(list_keys, self._store_path)
            except Exception as exc:
                self._failure("list_failed", exc)
                await self._send(chat_id, _text("save_failed"), thread_id=get_thread_id(message))
                return
            await self._send(
                chat_id,
                _text("list", keys="\n".join(keys)) if keys else _text("list_empty"),
                thread_id=get_thread_id(message),
            )
        elif action in {"set", "delete"} and len(parts) in {2, 3}:
            key = parts[2] if len(parts) == 3 else ""
            if validate_key(key):
                await self._send(chat_id, _text("invalid_key"), thread_id=get_thread_id(message))
                return
            try:
                exists = key in await asyncio.to_thread(list_keys, self._store_path)
            except Exception as exc:
                self._failure("list_failed", exc)
                await self._send(chat_id, _text("save_failed"), thread_id=get_thread_id(message))
                return
            if action == "delete" or exists:
                if action == "set":
                    self._remove_pending(chat_id, user_id)
                operation = "del" if action == "delete" else "ow"
                await self._send(
                    chat_id,
                    _text(
                        "confirm_delete" if action == "delete" else "confirm_overwrite",
                        key=key,
                    ),
                    keyboard=self._keyboard(operation, key),
                    thread_id=get_thread_id(message),
                )
            else:
                await self._request(chat_id, user_id, key, thread_id=get_thread_id(message))
        else:
            await self._send(chat_id, _text("usage"), thread_id=get_thread_id(message))

    async def _request(
        self, chat_id: int, user_id: int, key: str, *, thread_id: int | None = None
    ) -> None:
        identity = (chat_id, user_id)
        pending = _Pending(chat_id, user_id, key, 0, self._clock() + PENDING_SECONDS)
        # Replace any earlier request before starting the new one.
        self._remove_pending(chat_id, user_id)
        self._pending[identity] = pending
        try:
            self._save()
        except OSError as exc:
            self._pending.pop(identity, None)
            self._failure("pending_add_failed", exc, key)
            await self._send(chat_id, _text("save_failed"), thread_id=thread_id)
            return
        prompt = await self._send(
            chat_id,
            f"{PROMPT_MARKER}\n{_text('prompt', key=key)}",
            force_reply=True,
            thread_id=thread_id,
        )
        if prompt is None:
            self._remove_pending(chat_id, user_id)
            await self._send(chat_id, _text("save_failed"), thread_id=thread_id)
            return
        pending.prompt_message_id = prompt.message_id
        try:
            self._save()
        except OSError as exc:
            self._failure("pending_id_failed", exc, key)
            self._remove_pending(chat_id, user_id)
            await self._send(chat_id, _text("save_failed"), thread_id=thread_id)
        else:
            self._schedule_prompt_delete(pending, at_expiry=True, edit_at_expiry=True)

    async def _value(self, message: Message, pending: _Pending | None) -> None:
        # Schedule deletion before responding; never make the value message a reply target.
        await self._delete(message.chat.id, message.message_id, thread_id=get_thread_id(message))
        if pending is None or pending.expires_at <= self._clock():
            if pending is not None and self._clock() >= pending.expires_at + 120:
                self._remove_pending(pending.chat_id, pending.user_id)
            await self._send(message.chat.id, _text("expired"), thread_id=get_thread_id(message))
            return
        if message.text is None:
            await self._send(message.chat.id, _text("not_text"), thread_id=get_thread_id(message))
            return
        reason = validate_value(message.text)
        if reason is not None:
            logger.info(
                "Secret input key=%s result=%s length=%d", pending.key, reason, len(message.text)
            )
            await self._send(
                message.chat.id,
                _text("rejected", reason=_text(f"reason_{reason}", max=MAX_VALUE_LENGTH)),
                thread_id=get_thread_id(message),
            )
            return
        try:
            await asyncio.to_thread(set_secret, self._store_path, pending.key, message.text)
        except Exception as exc:
            self._failure("save_failed", exc, pending.key)
            await self._send(
                message.chat.id, _text("save_failed"), thread_id=get_thread_id(message)
            )
            return
        self._remove_pending(pending.chat_id, pending.user_id)
        logger.info("Secret input key=%s result=saved length=%d", pending.key, len(message.text))
        text = _text("saved", key=pending.key, length=len(message.text))
        if pending.key in load_env_secrets(self._home / ".env"):
            text += "\n" + _text("saved_env_note", key=pending.key)
        await self._send(message.chat.id, text, thread_id=get_thread_id(message))

    async def handle_callback(self, callback_query: CallbackQuery) -> bool:
        """Handle secret buttons, checking the actor's authorization again."""
        data = callback_query.data or ""
        if not data.startswith("sec:"):
            return False
        async with self._lock:
            message = callback_query.message
            if isinstance(message, Message):
                denial = self._denial(message, callback_query.from_user.id)
                if denial is None:
                    await self._callback_action(
                        message.chat.id,
                        callback_query.from_user.id,
                        data,
                        thread_id=get_thread_id(message),
                    )
                elif denial:
                    await self._send(
                        message.chat.id, _text(f"{denial}"), thread_id=get_thread_id(message)
                    )
            try:
                await self._bot.answer_callback_query(callback_query.id)
            except Exception as exc:
                self._failure("callback_answer_failed", exc)
        return True

    async def _callback_action(
        self, chat_id: int, user_id: int, data: str, *, thread_id: int | None = None
    ) -> None:
        if data == "sec:x":
            self._remove_pending(chat_id, user_id)
            await self._send(chat_id, _text("cancelled"), thread_id=thread_id)
            return
        parts = data.split(":", 2)
        if len(parts) != 3 or parts[1] not in {"ow", "del"}:
            return
        key = parts[2]
        if validate_key(key):
            await self._send(chat_id, _text("invalid_key"), thread_id=thread_id)
        elif parts[1] == "ow":
            await self._request(chat_id, user_id, key, thread_id=thread_id)
        else:
            try:
                deleted = await asyncio.to_thread(delete_secret, self._store_path, key)
            except Exception as exc:
                self._failure("delete_failed", exc, key)
                await self._send(chat_id, _text("save_failed"), thread_id=thread_id)
                return
            await self._send(
                chat_id,
                _text("deleted" if deleted else "delete_missing", key=key),
                thread_id=thread_id,
            )

    @staticmethod
    def _keyboard(operation: str, key: str) -> InlineKeyboardMarkup:
        label = "btn_delete" if operation == "del" else "btn_overwrite"
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=_text(f"{label}"), callback_data=f"sec:{operation}:{key}"
                    ),
                    InlineKeyboardButton(text=_text("btn_cancel"), callback_data="sec:x"),
                ]
            ]
        )

    async def _send(
        self,
        chat_id: int,
        text: str,
        *,
        thread_id: int | None = None,
        force_reply: bool = False,
        keyboard: InlineKeyboardMarkup | None = None,
    ) -> Message | None:
        try:
            return await self._bot.send_message(
                chat_id,
                text,
                parse_mode=None,
                message_thread_id=thread_id,
                reply_markup=ForceReply(force_reply=True) if force_reply else keyboard,
            )
        except Exception as exc:
            self._failure("send_failed", exc)
            return None

    async def _delete(
        self, chat_id: int, message_id: int, *, notify: bool = True, thread_id: int | None = None
    ) -> None:
        identity = (chat_id, message_id)
        guarded = self._guarded.get(identity)
        if guarded is not None and guarded.deleted:
            return
        if guarded is None:
            guarded = _Guarded(chat_id, message_id, self._clock(), deleted=False)
            self._guarded[identity] = guarded
        if self._is_main:
            await self._persist_guarded()
        task_name = f"secret-message-delete:{chat_id}:{message_id}"
        if guarded.deleted or any(
            task.get_name() == task_name and not task.done() for task in self._delete_tasks
        ):
            return
        task = asyncio.create_task(
            self._delete_guarded(guarded, notify=notify, thread_id=thread_id), name=task_name
        )
        self._delete_tasks.add(task)
        task.add_done_callback(self._delete_done)

    def _delete_done(self, task: asyncio.Task[None]) -> None:
        self._delete_tasks.discard(task)
        if not task.cancelled() and (exc := task.exception()) is not None:
            self._failure("delete_task_failed", exc)

    async def _delete_guarded(
        self, guarded: _Guarded, *, notify: bool, thread_id: int | None
    ) -> None:
        chat_id, message_id = guarded.chat_id, guarded.message_id
        for attempt in range(3):
            try:
                await self._bot.delete_message(chat_id, message_id)
            except Exception as exc:
                self._failure("message_delete_failed", exc)
                if attempt < 2:
                    await asyncio.sleep(attempt + 1)
            else:
                guarded.deleted = True
                if self._is_main:
                    await self._persist_guarded()
                return
        if notify:
            await self._send(chat_id, _text("delete_failed"), thread_id=thread_id)

    async def _persist_guarded(self) -> None:
        pause = 0.5
        for attempt in range(5):
            try:
                self._save()
            except OSError as exc:
                self._failure("guarded_write_failed", exc)
                if attempt < 4:
                    await asyncio.sleep(pause)
                    pause *= 2
            else:
                return

    def _schedule_prompt_delete(
        self, pending: _Pending, *, at_expiry: bool = False, edit_at_expiry: bool = False
    ) -> None:
        if not pending.prompt_message_id:
            return
        task = asyncio.create_task(
            self._delete_pending_prompt(
                pending, at_expiry=at_expiry, edit_at_expiry=edit_at_expiry
            ),
            name="secret-prompt-expiry" if at_expiry else "secret-prompt-delete",
        )
        self._delete_tasks.add(task)
        task.add_done_callback(self._delete_done)

    async def _delete_pending_prompt(
        self, pending: _Pending, *, at_expiry: bool, edit_at_expiry: bool = False
    ) -> None:
        if at_expiry:
            await asyncio.sleep(max(0, pending.expires_at - self._clock()))
            async with self._lock:
                if self._pending.get((pending.chat_id, pending.user_id)) is pending:
                    if edit_at_expiry:
                        try:
                            await self._bot.edit_message_text(
                                f"{PROMPT_MARKER}\n{_text('expired')}",
                                chat_id=pending.chat_id,
                                message_id=pending.prompt_message_id,
                                parse_mode=None,
                            )
                        except Exception as exc:
                            self._failure("prompt_edit_failed", exc)
                    else:
                        await self._delete_prompt(pending)
        else:
            await self._delete_prompt(pending)

    async def _delete_prompt(self, pending: _Pending) -> None:
        message_id = pending.prompt_message_id
        if message_id:
            pending.prompt_message_id = 0
            await self._delete(pending.chat_id, message_id, notify=False)

    def _remove_pending(self, chat_id: int, user_id: int) -> bool:
        pending = self._pending.pop((chat_id, user_id), None)
        if pending is None:
            return False
        self._schedule_prompt_delete(pending)
        try:
            self._save()
        except OSError as exc:
            self._failure("pending_remove_failed", exc, pending.key)
        return True

    def _save(self) -> None:
        self._guarded = {
            identity: row
            for identity, row in self._guarded.items()
            if row.at > self._clock() - GUARDED_SECONDS
        }
        state = {
            "pending": [asdict(pending) for pending in self._pending.values()],
            "guarded": [asdict(message) for message in self._guarded.values()],
        }
        atomic_text_save(self._state_path, json.dumps(state))

    def _load(self) -> None:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            self._validate_state(data)
            self._guarded = {
                (row["chat_id"], row["message_id"]): _Guarded(**row)
                for row in data["guarded"]
                if row["at"] > self._clock() - GUARDED_SECONDS
            }
            for row in data["pending"]:
                pending = _Pending(**row)
                retained = pending.expires_at + 120 > self._clock()
                if retained:
                    self._pending[(pending.chat_id, pending.user_id)] = pending
                if (pending.chat_id, pending.prompt_message_id) not in self._guarded:
                    self._schedule_prompt_delete(
                        pending,
                        at_expiry=retained,
                        edit_at_expiry=pending.expires_at > self._clock(),
                    )
            for guarded in self._guarded.values():
                if not guarded.deleted:
                    task = asyncio.create_task(
                        self._delete_guarded(guarded, notify=False, thread_id=None),
                        name=f"secret-message-delete:{guarded.chat_id}:{guarded.message_id}",
                    )
                    self._delete_tasks.add(task)
                    task.add_done_callback(self._delete_done)
        except FileNotFoundError:
            return
        except (OSError, ValueError, TypeError, KeyError) as exc:
            stamp = datetime.fromtimestamp(self._clock(), UTC).strftime("%Y%m%dT%H%M%SZ")
            with contextlib.suppress(OSError):
                self._state_path.replace(
                    self._state_path.with_name(f"{self._state_path.name}.corrupt-{stamp}")
                )
            self._failure("metadata_unreadable", exc)

    @staticmethod
    def _validate_state(data: object) -> None:
        if not isinstance(data, dict) or set(data) != {"pending", "guarded"}:
            raise ValueError("metadata_shape")
        layouts: tuple[tuple[str, dict[str, type[object] | tuple[type[object], ...]]], ...] = (
            (
                "pending",
                {
                    "chat_id": int,
                    "user_id": int,
                    "key": str,
                    "prompt_message_id": int,
                    "expires_at": (int, float),
                },
            ),
            ("guarded", {"chat_id": int, "message_id": int, "at": (int, float), "deleted": bool}),
        )
        for field, types in layouts:
            rows = data[field]
            if not isinstance(rows, list):
                raise TypeError("metadata_shape")
            for row in rows:
                if not isinstance(row, dict) or set(row) != set(types):
                    raise ValueError("metadata_shape")
                if any(not isinstance(row[key], expected) for key, expected in types.items()):
                    raise ValueError("metadata_shape")
                if field == "pending" and validate_key(row["key"]):
                    raise ValueError("metadata_key")

    @staticmethod
    def _failure(result: str, exc: BaseException, key: str = "") -> None:
        logger.warning("Secret input key=%s result=%s error=%s", key, result, type(exc).__name__)
