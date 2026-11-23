"""Callback helpers for inline keyboard handling in the Telegram bot.

Extracts reusable patterns from the TelegramBot callback routing so the
four selector handlers (model, cron, session, task) share a single
implementation.
"""

from __future__ import annotations

import html as html_mod
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest

from ductor_bot.i18n import t
from ductor_bot.messenger.telegram.formatting import markdown_to_telegram_html
from ductor_bot.orchestrator.selectors.models import Button, ButtonGrid, SelectorResponse

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.types import InlineKeyboardMarkup, Message

    from ductor_bot.chat_log.history import HistoryStore
    from ductor_bot.messenger.telegram.app import TelegramBot
    from ductor_bot.session.key import SessionKey


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ButtonGrid -> InlineKeyboardMarkup conversion
# ---------------------------------------------------------------------------


def button_grid_to_markup(grid: ButtonGrid | None) -> InlineKeyboardMarkup | None:
    """Convert abstract ``ButtonGrid`` to aiogram ``InlineKeyboardMarkup``."""
    if grid is None:
        return None
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=btn.text, callback_data=btn.callback_data) for btn in row]
            for row in grid.rows
        ]
    )


# ---------------------------------------------------------------------------
# Selector result editing (shared by model / cron / session / task wizards)
# ---------------------------------------------------------------------------


async def edit_selector_response(
    bot: Bot,
    chat_id: int,
    message_id: int,
    resp: SelectorResponse,
) -> bool:
    """Return whether Telegram accepted the selector edit; suppress bad requests."""
    try:
        await bot.edit_message_text(
            text=markdown_to_telegram_html(resp.text),
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=button_grid_to_markup(resp.buttons),
            parse_mode=ParseMode.HTML,
        )

    # ---------------------------------------------------------------------------
    # Button choice annotation
    # ---------------------------------------------------------------------------

    except TelegramBadRequest:
        return False
    return True


async def mark_button_choice(bot: Bot, chat_id: int, msg: Message, label: str) -> None:
    """Edit the bot message to append ``[USER ANSWER] label`` and remove the keyboard.

    Falls back to keyboard-only removal when the message is a caption
    (photo/video) or the updated text would exceed Telegram limits.
    """
    await edit_button_choice(
        bot,
        chat_id,
        msg.message_id,
        label,
        html_text=(msg.html_text or msg.text) if msg.text is not None else None,
    )


async def edit_button_choice(
    bot: Bot,
    chat_id: int,
    message_id: int | None,
    label: str,
    *,
    html_text: str | None = None,
) -> bool:
    """Return whether the choice was shown in the Telegram message body."""
    if message_id is None:
        return False
    if html_text is not None:
        updated = f"{html_text}\n\n{t('notice.user_answer', answer=html_mod.escape(label))}"
        try:
            await bot.edit_message_text(
                text=updated,
                chat_id=chat_id,
                message_id=message_id,
                parse_mode=ParseMode.HTML,
                reply_markup=None,
            )
        except Exception as exc:
            logger.warning("Failed to annotate button choice: %s", type(exc).__name__)
        else:
            return True
    try:
        await bot.edit_message_reply_markup(
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=None,
        )
    except Exception as exc:
        logger.warning("Failed to remove button keyboard: %s", type(exc).__name__)
    return False


class ButtonAlreadyAnsweredError(Exception):
    """A competing transport already accepted this keyboard."""


async def process_button_choice(
    data: str,
    claim: Callable[[], Awaitable[bool]],
    annotate: Callable[[], Awaitable[None]],
    submit: Callable[[str, str | None, Callable[[], Awaitable[None]]], Awaitable[None]],
) -> bool:
    """Share claim/edit/input order while each transport keeps its ingress rules."""
    target = parse_ns_callback(data) if data.startswith("ns:") else None
    if data.startswith("ns:") and target is None:
        return False
    accepted = False

    async def accept() -> None:
        nonlocal accepted
        if accepted:
            return
        if not await claim():
            raise ButtonAlreadyAnsweredError
        accepted = True
        await annotate()

    try:
        await submit(target[1] if target else data, target[0] if target else None, accept)
        if not accepted:
            await accept()
    except ButtonAlreadyAnsweredError:
        return False
    return True


def response_button_data(buttons: object, label: str) -> str | None:
    """Return only ordinary reply data, keeping selectors and URL chips separate."""
    from ductor_bot.messenger.telegram.buttons import _truncate_callback_data

    if not isinstance(buttons, list):
        return None
    for row in buttons:
        if not isinstance(row, list):
            continue
        for button in row:
            if not isinstance(button, dict) or button.get("text") != label or button.get("url"):
                continue
            data = button.get("callback_data")
            if not isinstance(data, str):
                continue
            target = parse_ns_callback(data) if data.startswith("ns:") else None
            if data.startswith("ns:") and target is None:
                continue
            expected = _truncate_callback_data(label)
            if target:
                expected = _truncate_callback_data(f"ns:{target[0]}:{expected}")
            if data == expected and (target is not None or not _selector_data(data)):
                return data
    return None


def _selector_data(data: str) -> bool:
    from ductor_bot.messenger.telegram.file_browser import is_file_browser_callback
    from ductor_bot.messenger.telegram.middleware import MQ_PREFIX
    from ductor_bot.orchestrator.selectors.account_selector import is_account_selector_callback
    from ductor_bot.orchestrator.selectors.autocompact_selector import is_autocompact_callback
    from ductor_bot.orchestrator.selectors.cron_selector import is_cron_selector_callback
    from ductor_bot.orchestrator.selectors.model_selector import is_model_selector_callback
    from ductor_bot.orchestrator.selectors.session_selector import is_session_selector_callback
    from ductor_bot.orchestrator.selectors.task_selector import is_task_selector_callback

    return data.startswith(("sec:", "w:", "usg:", MQ_PREFIX)) or any(
        predicate(data)
        for predicate in (
            is_file_browser_callback,
            is_account_selector_callback,
            is_autocompact_callback,
            is_cron_selector_callback,
            is_model_selector_callback,
            is_session_selector_callback,
            is_task_selector_callback,
        )
    )


# ---------------------------------------------------------------------------
# Named-session callback helpers
# ---------------------------------------------------------------------------


def parse_ns_callback(data: str) -> tuple[str, str] | None:
    """Parse ``ns:<session_name>:<label>`` callback data.

    Returns ``(session_name, label)`` or ``None`` if the format is invalid.
    """
    rest = data[3:]  # strip "ns:"
    colon = rest.find(":")
    if colon < 0:
        return None
    session_name = rest[:colon]
    label = rest[colon + 1 :]
    if not session_name or not label:
        return None
    return session_name, label


@dataclass(frozen=True, slots=True)
class SelectorStep:
    response: SelectorResponse | None = None
    prompt: str | None = None


def selector_button_data(data: object) -> bool:
    """Namespaces supported by the shared selector dispatcher, excluding private controls."""
    from ductor_bot.messenger.telegram.welcome import resolve_welcome_callback

    if not isinstance(data, str):
        return False
    return data.startswith(
        ("ms:", "ac:", "crn:", "nsc:", "tsc:", "acs:", "sf:", "sf!", "usg:")
    ) or (data.startswith("w:") and resolve_welcome_callback(data) is not None)


def button_kinds(buttons: object) -> list[list[str]]:
    """Project kinds for the web without changing stored keyboard rows."""
    if not isinstance(buttons, list):
        return []
    return [
        [
            "link"
            if button.get("url")
            else "reply"
            if response_button_data([[button]], button.get("text", "")) is not None
            else "selector"
            if selector_button_data(button.get("callback_data", ""))
            else "disabled"
            for button in row
        ]
        for row in buttons
    ]


def selector_fields(meta: dict[str, object]) -> dict[str, object]:
    return {
        "button_kinds": button_kinds(meta.get("buttons")),
        "selector_revision": meta.get("selector_revision", 0),
    }


def selector_rows(response: SelectorResponse) -> list[list[dict[str, str]]]:
    return (
        [
            [{"text": button.text, "callback_data": button.callback_data} for button in row]
            for row in response.buttons.rows
        ]
        if response.buttons is not None
        else []
    )


def selector_callback_lock_key(key: SessionKey, data: str) -> tuple[int, int | None] | int | None:
    """Share the selector's execution lock identity with web admission checks."""
    if data.startswith(("ms:", "ac:")):
        return key.lock_key
    if data.startswith(("crn:", "nsc:")):
        return key.chat_id
    return None


async def selector_callback_response(  # noqa: C901, PLR0911 -- one branch per supported selector namespace
    bot: TelegramBot, key: SessionKey, data: str, *, user_id: int | None = None
) -> SelectorStep:
    """Reuse selector functions and the Telegram handlers' existing lock boundaries."""
    from ductor_bot.infra.input_context import input_lock
    from ductor_bot.messenger.telegram.file_browser import handle_file_browser_callback
    from ductor_bot.messenger.telegram.welcome import resolve_welcome_callback
    from ductor_bot.orchestrator.selectors.account_selector import handle_account_callback
    from ductor_bot.orchestrator.selectors.cron_selector import handle_cron_callback
    from ductor_bot.orchestrator.selectors.model_selector import handle_model_callback
    from ductor_bot.orchestrator.selectors.session_selector import handle_session_callback
    from ductor_bot.orchestrator.selectors.task_selector import handle_task_callback

    if data.startswith("ac:"):
        from ductor_bot.orchestrator.selectors.autocompact_selector import (
            handle_autocompact_callback,
        )

        async with input_lock(bot._sequential.get_lock(key.lock_key)):
            return SelectorStep(
                await handle_autocompact_callback(bot._orch, key, data, user_id=user_id)
            )
    lock_key = selector_callback_lock_key(key, data)
    if data.startswith("w:"):
        return SelectorStep(prompt=resolve_welcome_callback(data))
    if data.startswith(("sf:", "sf!")):
        text, keyboard, prompt = await handle_file_browser_callback(bot._orch.paths, data)
        grid = (
            ButtonGrid(
                [
                    [Button(button.text, button.callback_data or "") for button in row]
                    for row in keyboard.inline_keyboard
                ]
            )
            if keyboard is not None
            else None
        )
        return SelectorStep(SelectorResponse(text, grid), prompt)
    if data.startswith("ms:"):
        assert lock_key is not None
        async with input_lock(bot._sequential.get_lock(lock_key)):
            return SelectorStep(await handle_model_callback(bot._orch, key, data))
    if data.startswith("crn:"):
        assert lock_key is not None
        async with input_lock(bot._sequential.get_lock(lock_key)):
            return SelectorStep(await handle_cron_callback(bot._orch, data))
    if data.startswith("nsc:"):
        assert lock_key is not None
        async with input_lock(bot._sequential.get_lock(lock_key)):
            return SelectorStep(await handle_session_callback(bot._orch, key.chat_id, data))
    if data.startswith("acs:"):
        response = (
            await handle_account_callback(bot._orch.paths.ductor_home, bot._config, data)
            if bot._agent_name == "main"
            else SelectorResponse(t("accounts.main_only"))
        )
        return SelectorStep(response)
    if data.startswith("tsc:"):
        hub = bot._orch.task_hub
        return (
            SelectorStep(await handle_task_callback(hub, key.chat_id, data))
            if hub is not None
            else SelectorStep()
        )
    if data.startswith("usg:"):
        from ductor_bot.cli.codex_usage import query_codex_usage

        try:
            return SelectorStep(await bot._usage_response(await query_codex_usage()))
        except Exception as exc:
            logger.warning("Usage display failed: %s", type(exc).__name__)
            return SelectorStep(SelectorResponse(t("usage.error")))
    return SelectorStep()


async def edit_recorded_selector(
    bot: Bot, chat_id: int, message_id: int | None, response: SelectorResponse
) -> bool:
    """A completed state change survives unavailable or not-yet-delivered Telegram messages."""
    if message_id is None:
        return False
    try:
        edited = await edit_selector_response(bot, chat_id, message_id, response)
    except Exception as exc:
        logger.warning("Failed to edit selector message: %s", type(exc).__name__)
        return False
    if not edited:
        logger.warning("Failed to edit selector message: TelegramBadRequest")
    return edited


async def restore_recorded_selector(
    history: HistoryStore, bot: Bot, key: SessionKey, entry_id: str, message_id: int
) -> None:
    """Restore the latest screen when a Telegram press belongs to an obsolete keyboard."""
    from aiogram.types import InlineKeyboardMarkup

    from ductor_bot.chat_log.ids import to_tid

    tid = to_tid(key)
    try:
        async with history.selector_gate(tid, entry_id):
            meta = (await history.metadata(tid)).get(entry_id, {})
            if isinstance(answered := meta.get("answered"), str):
                await edit_button_choice(
                    bot, key.chat_id, message_id, answered, html_text=meta.get("telegram_html_text")
                )
                return
            text = meta.get("text")
            if text is None:
                entry = await history.get_entry(tid, entry_id)
                if entry is None:
                    return
                text = entry.text
            await bot.edit_message_text(
                chat_id=key.chat_id,
                message_id=message_id,
                text=markdown_to_telegram_html(text),
                reply_markup=InlineKeyboardMarkup.model_validate(
                    {"inline_keyboard": meta.get("buttons", [])}
                ),
                parse_mode=ParseMode.HTML,
            )
    except Exception as exc:
        logger.warning("Failed to restore selector message: %s", type(exc).__name__)


def recorded_keyboard_observer(
    history: HistoryStore, bot: Bot, key: SessionKey, entry_id: str, *, revision: int = 0
) -> Callable[[int, InlineKeyboardMarkup, str | None], Awaitable[None]]:
    """Bind a delivered keyboard without restoring an obsolete selector screen."""
    from ductor_bot.chat_log.ids import to_tid
    from ductor_bot.messenger.telegram.buttons import serialize_keyboard

    tid = to_tid(key)

    async def record(
        message_id: int, markup: InlineKeyboardMarkup, html_text: str | None = None
    ) -> None:
        from aiogram.types import InlineKeyboardMarkup

        # Delivery may hold the session lock; never wait for the entry gate here.
        meta = await history.bind_telegram_message(
            tid,
            entry_id,
            message_id,
            buttons=serialize_keyboard(markup),
            html_text=html_text,
            keyboard_revision=revision,
        )
        for _ in range(3):
            state = (meta.get("selector_revision", 0), meta.get("answered"))
            if isinstance(answered := meta.get("answered"), str):
                await edit_button_choice(
                    bot, key.chat_id, message_id, answered, html_text=meta.get("telegram_html_text")
                )
            elif meta.get("selector_revision", 0) != revision:
                try:
                    await bot.edit_message_text(
                        chat_id=key.chat_id,
                        message_id=message_id,
                        text=markdown_to_telegram_html(meta["text"]),
                        reply_markup=InlineKeyboardMarkup.model_validate(
                            {"inline_keyboard": meta["buttons"]}
                        ),
                        parse_mode=ParseMode.HTML,
                    )
                except Exception as exc:
                    logger.warning("Failed to reconcile selector keyboard: %s", type(exc).__name__)
                    return
            else:
                return
            meta = (await history.metadata(tid)).get(entry_id, {})
            if (meta.get("selector_revision", 0), meta.get("answered")) == state:
                return
        logger.warning("Stopped keyboard reconciliation after three state changes")

    return record
