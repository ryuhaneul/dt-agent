"""Web command catalog and dispatch without Telegram message handlers."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING, Any, NotRequired, TypedDict

from ductor_bot.chat_log.ids import from_tid, to_tid
from ductor_bot.errors import ProductError
from ductor_bot.i18n import t
from ductor_bot.i18n.loader import TranslationStore
from ductor_bot.infra.secret_store import store_path
from ductor_bot.messenger.telegram.app import (
    _build_help_text,
    build_agent_commands_text,
    build_info_response,
    leave_chat_response,
    prepare_restart_response,
)
from ductor_bot.messenger.telegram.file_browser import (
    file_browser_start,
    handle_file_browser_callback,
)
from ductor_bot.messenger.telegram.handlers import (
    abort_all_response,
    interrupt_response,
    new_session_response,
)
from ductor_bot.messenger.telegram.middleware import QUICK_COMMANDS
from ductor_bot.messenger.telegram.welcome import build_welcome_keyboard, build_welcome_text
from ductor_bot.orchestrator.selectors.models import ButtonGrid
from ductor_bot.session.key import SessionKey
from ductor_bot.text.response_format import stop_text
from ductor_bot.webchat.secret import WebSecrets

if TYPE_CHECKING:
    from aiogram.types import InlineKeyboardMarkup

    from ductor_bot.messenger.telegram.app import TelegramBot
    from ductor_bot.webchat.turns import WebTurns

logger = logging.getLogger(__name__)

# One catalog for execution and autocomplete. Values are commands.toml keys.
WEB_COMMANDS: dict[str, str] = {
    "new": "bot.new",
    "reset": "bot.reset",
    "stop": "bot.stop",
    "interrupt": "bot.interrupt",
    "model": "bot.model",
    "effort": "bot.effort",
    "autocompact": "bot.autocompact",
    "status": "bot.status",
    "usage": "bot.usage",
    "memory": "bot.memory",
    "session": "bot.session",
    "tasks": "bot.tasks",
    "cron": "bot.cron",
    "agent_commands": "bot.agent_commands",
    "showfiles": "bot.showfiles",
    "info": "bot.info",
    "help": "bot.help",
    "diagnose": "bot.diagnose",
    "restart": "bot.restart",
    "secret": "bot.secret",
    "accounts": "bot.accounts",
    "sessions": "bot.sessions",
    "agents": "multiagent.agents",
    "agent_start": "multiagent.agent_start",
    "agent_stop": "multiagent.agent_stop",
    "agent_restart": "multiagent.agent_restart",
    "stop_all": "multiagent.stop_all",
    "start": "bot.start",
    "where": "bot.where",
    "leave": "bot.leave",
}

# These commands have Telegram transport handlers outside the registry path.
_DIRECT_COMMANDS = {
    "new",
    "stop",
    "interrupt",
    "stop_all",
    "restart",
    "help",
    "info",
    "agent_commands",
    "where",
    "leave",
    "showfiles",
    "start",
    "usage",
    "accounts",
    "session",
    "secret",
}

_IMMEDIATE_COMMANDS = {value.removeprefix("/") for value in QUICK_COMMANDS} | {
    "stop",
    "interrupt",
    "stop_all",
    "secret",
    "restart",
}


class WebButton(TypedDict):
    text: str
    callback_data: NotRequired[str]
    url: NotRequired[str]


class CommandResponse(TypedDict):
    text: str
    buttons: list[list[WebButton]] | None
    secret: NotRequired[dict[str, Any]]


class WebCommandBusyError(Exception):
    """A state-changing command cannot run while its session is busy."""


def command_list(language: str) -> list[dict[str, str]]:
    """Describe commands in a request-local language without changing the bot locale."""
    store = TranslationStore(language)
    return [{"name": name, "description": store.cmd(key)} for name, key in WEB_COMMANDS.items()]


def parse_command(text: str) -> tuple[str, str] | None:
    """Recognize a known command, normalizing only its name and bot suffix."""
    parts = text.strip().split(None, 1)
    if not parts or not parts[0].startswith("/"):
        return None
    name = parts[0][1:].split("@", 1)[0].lower()
    if name not in WEB_COMMANDS:
        return None
    normalized = f"/{name}"
    if len(parts) > 1:
        normalized += " " + parts[1]
    return name, normalized


def serialize_buttons(
    buttons: ButtonGrid | InlineKeyboardMarkup | None,
) -> list[list[WebButton]] | None:
    """Preserve callback data, URLs and row order from both existing keyboard types."""
    if buttons is None:
        return None
    if isinstance(buttons, ButtonGrid):
        return [
            [{"text": button.text, "callback_data": button.callback_data} for button in row]
            for row in buttons.rows
        ]
    rows: list[list[WebButton]] = []
    for row in buttons.inline_keyboard:
        rendered: list[WebButton] = []
        for button in row:
            item: WebButton = {"text": button.text}
            if button.callback_data is not None:
                item["callback_data"] = button.callback_data
            elif button.url is not None:
                item["url"] = button.url
            rendered.append(item)
        rows.append(rendered)
    return rows


def _response(
    text: str, buttons: ButtonGrid | InlineKeyboardMarkup | None = None
) -> CommandResponse:
    return {"text": text, "buttons": serialize_buttons(buttons)}


class WebCommandDispatcher:
    def __init__(
        self, bot: TelegramBot, turns: WebTurns, *, secret: WebSecrets | None = None
    ) -> None:
        self.bot = bot
        self.turns = turns
        self.secret = secret or WebSecrets(
            bot._orch.paths.ductor_home,
            store_path(bot._orch.paths.ductor_home),
            bot.config.language,
        )

    async def dispatch(
        self,
        tid: str,
        text: str,
        *,
        on_start: Callable[[], Awaitable[None]] | None = None,
        on_finish: Callable[[CommandResponse], Awaitable[None]] | None = None,
    ) -> CommandResponse | None:
        parsed = parse_command(text)
        if parsed is None:
            return None
        name, normalized = parsed
        # Never pass secret arguments to a turn, recorder, outbox or orchestrator.
        if name == "secret":
            result = await self.secret.command(tid, normalized)
            return CommandResponse(text=result["text"], buttons=None, secret=result["secret"])
        if (
            name not in _DIRECT_COMMANDS
            and self.bot._orch._command_registry.resolve(normalized.strip().lower()) is None
        ):
            return None
        key = from_tid(tid)
        if name in _IMMEDIATE_COMMANDS:
            return await self._run_command(name, key, normalized, on_start, on_finish)

        targets = [key]
        parts = normalized.split(None, 1)
        if name == "new" and len(parts) > 1 and parts[1].startswith("@"):
            topic_id = self.bot._topic_names.find_by_name(key.chat_id, parts[1][1:])
            if topic_id is not None and topic_id != key.topic_id:
                targets.append(SessionKey(chat_id=key.chat_id, topic_id=topic_id))
        pool = self.bot._orch._lock_pool
        assert pool is not None
        locks = [
            pool.get(target.lock_key)
            for target in sorted(targets, key=lambda target: target.storage_key)
        ]
        if any(lock.locked() for lock in locks) or any(
            self.turns.busy(to_tid(target)) for target in targets
        ):
            raise WebCommandBusyError
        async with AsyncExitStack() as stack:
            for lock in locks:
                await stack.enter_async_context(lock)
            return await self._run_command(name, key, normalized, on_start, on_finish)

    async def _run_command(
        self,
        name: str,
        key: SessionKey,
        text: str,
        on_start: Callable[[], Awaitable[None]] | None,
        on_finish: Callable[[CommandResponse], Awaitable[None]] | None,
    ) -> CommandResponse:
        if on_start is not None:
            await on_start()
        succeeded = True
        try:
            result = await self._execute(name, key, text)
        except WebCommandBusyError:
            raise
        except Exception as exc:
            logger.error(  # noqa: TRY400 -- exception messages may contain secrets
                "Web command failed: command=%s reason=%s", name, type(exc).__name__, exc_info=False
            )
            result = _response(t("error.internal"))
            succeeded = False
        if on_finish is not None:
            await on_finish(result)
        if name == "restart" and succeeded:
            from ductor_bot.infra.restart import write_restart_marker

            await asyncio.to_thread(
                write_restart_marker,
                marker_path=self.bot._orch.paths.ductor_home / "restart-requested",
            )
        return result

    async def _execute(  # noqa: C901, PLR0911, PLR0912 -- the supported direct command set
        self, name: str, key: SessionKey, text: str
    ) -> CommandResponse:
        orch = self.bot._orch
        if name == "new":
            return _response(
                await new_session_response(
                    orch.reset_active_provider_session, key, text, self.bot._topic_names
                )
            )
        if name == "stop":
            killed = await self.turns.abort(to_tid(key))
            return _response(stop_text(bool(killed), orch.active_provider_name))
        if name == "interrupt":
            return _response(interrupt_response(orch, key.chat_id))
        if name == "stop_all":
            await self.bot._sequential.drain_pending(key.chat_id)
            await self.turns.cancel_pending(key.chat_id)
            return _response(await abort_all_response(orch, self.bot._abort_all_callback))
        if name == "restart":
            return _response(await prepare_restart_response(key.chat_id, orch.paths))
        if name == "help":
            return _response(_build_help_text())
        if name == "info":
            return _response(*build_info_response())
        if name == "agent_commands":
            return _response(build_agent_commands_text())
        if name == "where":
            return _response(self.bot._format_where())
        if name == "leave":
            return _response(
                await leave_chat_response(
                    text, self.bot.bot_instance.leave_chat, self.bot._chat_tracker
                )
            )
        if name == "showfiles":
            return _response(*await file_browser_start(orch.paths))
        if name == "start":
            from ductor_bot.cli.auth import check_all_auth

            auth = await asyncio.to_thread(check_all_auth)
            return _response(
                build_welcome_text("", auth, self.bot.config), build_welcome_keyboard()
            )
        if name == "usage":
            return await self._usage_response()
        if name == "accounts":
            from ductor_bot.orchestrator.selectors.account_selector import account_selector_start

            response = await account_selector_start(orch.paths.ductor_home, self.bot.config)
            return _response(response.text, response.buttons)
        if name == "session":
            parts = text.split(None, 1)
            if len(parts) < 2:
                return _response(self.bot._build_session_help())
            parsed = self.bot._parse_session_prompt(key.chat_id, parts[1])
            try:
                # Zero means no Telegram source message; deliver without a reply quote.
                return _response(
                    self.bot._submit_session_response(key.chat_id, parsed, 0, key.topic_id)
                )
            except ValueError as exc:
                return _response(exc.shown() if isinstance(exc, ProductError) else str(exc))
        return await self._registry_response(name, key, text)

    async def _registry_response(self, name: str, key: SessionKey, text: str) -> CommandResponse:
        orch = self.bot._orch
        if orch._inflight_tracker.shutting_down:
            return _response(t("error.shutting_down"))
        orch._observers.heartbeat.notify_activity(key)
        orch._process_registry.clear_abort(key.chat_id)
        orch._process_registry.clear_topic_abort(key.chat_id, key.topic_id)
        handler = orch._command_registry.resolve(text.strip().lower())
        if handler is None:
            # Main's multi-agent registry is absent when no supervisor is configured.
            return _response(t("agents.not_active"))
        result = await handler(orch, key, text)
        assert result is not None
        return _response(result.text, result.buttons)

    async def _usage_response(self) -> CommandResponse:
        from ductor_bot.cli.codex_usage import query_codex_usage

        try:
            response = await self.bot._usage_response(await query_codex_usage())
        except Exception as exc:
            logger.warning("Web usage display failed: %s", type(exc).__name__)
            return _response(t("usage.error"))
        return _response(response.text, response.buttons)

    async def file_browser_response(self, tid: str, data: str) -> CommandResponse:
        """Build a file selector step; file requests continue as a normal topic turn."""
        text, keyboard, prompt = await handle_file_browser_callback(self.bot._orch.paths, data)
        if prompt is not None:
            if self.turns.busy(tid):
                raise WebCommandBusyError
            await self.turns.submit(tid, prompt)
        return _response(text, keyboard)
