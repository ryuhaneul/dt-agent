"""Token-protected HTTP bridge hosted by the main Telegram bot."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import asdict
from typing import TYPE_CHECKING, Any, Literal

from aiogram.exceptions import TelegramAPIError
from aiohttp import ClientError, web

from ductor_bot.chat_log.history import InvalidSelectorButtonError, SelectorConflictError
from ductor_bot.chat_log.ids import from_tid
from ductor_bot.cli import claude_slots
from ductor_bot.cli.autocompact import parse_autocompact_input, resolve_autocompact
from ductor_bot.cli.claude_accounts import (
    SlotRegistry,
    host_slot_config_dir,
    slot_is_api_key,
    slot_is_authenticated,
    state_file_path,
)
from ductor_bot.config import resolve_user_timezone
from ductor_bot.i18n import LANGUAGES
from ductor_bot.i18n.loader import TranslationStore
from ductor_bot.infra.atomic_io import atomic_text_save
from ductor_bot.infra.file_lock import LockTimeoutError
from ductor_bot.infra.input_context import input_lock
from ductor_bot.infra.secret_store import store_path
from ductor_bot.messenger.telegram.buttons import extract_button_rows
from ductor_bot.messenger.telegram.callbacks import (
    edit_button_choice,
    edit_recorded_selector,
    process_button_choice,
    response_button_data,
    selector_button_data,
    selector_callback_lock_key,
    selector_callback_response,
    selector_fields,
    selector_rows,
)
from ductor_bot.orchestrator.selectors.autocompact_automation import (
    automation_state,
    save_automation,
)
from ductor_bot.orchestrator.selectors.autocompact_selector import save_autocompact
from ductor_bot.text.response_format import stop_text
from ductor_bot.webchat.attachments import MessageAttachments
from ductor_bot.webchat.auth import AuthError, BridgeAuth
from ductor_bot.webchat.cli_login import CliLoginError, CliLoginManager
from ductor_bot.webchat.cli_login_slots import ClaudeLoginSlots
from ductor_bot.webchat.commands import (
    CommandResponse,
    WebCommandBusyError,
    WebCommandDispatcher,
    command_list,
    parse_command,
)
from ductor_bot.webchat.cron_api import register_cron_routes
from ductor_bot.webchat.downloads import AttachmentDownloads, stream_attachment
from ductor_bot.webchat.files import WebFileError, WebFiles
from ductor_bot.webchat.read_state import ReadState
from ductor_bot.webchat.secret import WebSecrets
from ductor_bot.webchat.settings import resolve_default_chat_id
from ductor_bot.webchat.topics import TopicBusyError, TopicDeleteError, WebTopics
from ductor_bot.webchat.turns import WebTurns
from ductor_bot.webchat.uploads import (
    ATTACH_SETTINGS,
    UPLOADS,
    UploadError,
    register_upload_routes,
)
from ductor_bot.webchat.vault import register_vault_routes

if TYPE_CHECKING:
    from ductor_bot.chat_log.runtime import ChatLogRuntime
    from ductor_bot.messenger.telegram.app import TelegramBot
    from ductor_bot.orchestrator.core import Orchestrator
    from ductor_bot.session.key import SessionKey

logger = logging.getLogger(__name__)

_PREFIX = "/webchat/v1"
CLI_LOGIN = web.AppKey("cli_login_manager", CliLoginManager)


def create_bridge_app(  # noqa: C901
    bot: TelegramBot, runtime: ChatLogRuntime, token: str
) -> web.Application:
    """Build the internal API without opening a listener."""
    auth = BridgeAuth(
        store_path(bot._orch.paths.ductor_home),
        bot.config.webchat.domain,
        bot._orch.paths.webchat_dir / "auth_state.json",
    )

    @web.middleware
    async def authenticate(
        request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
    ) -> web.StreamResponse:
        supplied = request.headers.get("X-Dtw-Bridge-Token", "")
        if not supplied or not hmac.compare_digest(
            supplied.encode("utf-8", errors="replace"), token.encode()
        ):
            return web.json_response({"error": "forbidden"}, status=403)
        if request.can_read_body and "Content-Encoding" in request.headers:
            return web.json_response({"error": "unsupported_media_type"}, status=415)
        return await handler(request)

    app = web.Application(
        middlewares=[authenticate],
        client_max_size=512 * 1024,
        handler_args={"auto_decompress": False},
    )

    async def close_auth(_app: web.Application) -> None:
        await auth.close()

    app.on_cleanup.append(close_auth)

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "agent": "main"})

    async def meta(_request: web.Request) -> web.Response:
        return web.json_response(
            {
                "language": bot.config.language,
                "timezone": str(resolve_user_timezone(bot.config.user_timezone)),
                "default_chat_id": resolve_default_chat_id(bot.config),
                "domain": bot.config.webchat.domain,
            }
        )

    def credential_generation() -> str:
        return hmac.digest(token.encode(), auth.credentials(), "sha256").hex()

    async def auth_state(_request: web.Request) -> web.Response:
        try:
            return web.json_response(
                {**auth.state(), "credential_generation": credential_generation()}
            )
        except AuthError as exc:
            return web.json_response(exc.payload, status=403)

    async def authenticate_user(request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return web.json_response({"error": "invalid"}, status=403)
        if not isinstance(data, dict):
            return web.json_response({"error": "invalid"}, status=403)
        try:
            result = await auth.handle(request.path.removeprefix(f"{_PREFIX}/auth/"), data)
            if request.path in {f"{_PREFIX}/auth/totp", f"{_PREFIX}/auth/password-change"}:
                result["credential_generation"] = credential_generation()
        except AuthError as exc:
            return web.json_response(exc.payload, status=403)
        return web.json_response(result)

    async def state(_request: web.Request) -> web.Response:
        return web.json_response(
            {
                "telegram": runtime.link.state(),
                "outbox": {"pending": len(await runtime.outbox.pending(skip_unreadable=True))},
            }
        )

    app.router.add_get(f"{_PREFIX}/health", health)
    app.router.add_get(f"{_PREFIX}/meta", meta)
    app.router.add_get(f"{_PREFIX}/auth/state", auth_state)
    for action in ("password", "totp", "setup/begin", "setup/confirm", "password-change", "logout"):
        app.router.add_post(f"{_PREFIX}/auth/{action}", authenticate_user)
    app.router.add_get(f"{_PREFIX}/state", state)
    _register_conversation_routes(app, bot, runtime)
    _register_file_routes(app, bot, runtime)
    _register_cli_login_routes(app, bot)
    register_vault_routes(app, store_path(bot._orch.paths.ductor_home), runtime)
    return app


def _error(code: str, status: int) -> web.Response:
    return web.json_response({"error": code}, status=status)


async def _body(request: web.Request) -> dict[str, Any]:
    try:
        value = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise web.HTTPBadRequest(
            text='{"error":"bad_request"}', content_type="application/json"
        ) from None
    if not isinstance(value, dict):
        raise web.HTTPBadRequest(text='{"error":"bad_request"}', content_type="application/json")
    return value


def _name(data: dict[str, Any]) -> str | None:
    value = data.get("name")
    return value.strip() if isinstance(value, str) and 1 <= len(value.strip()) <= 128 else None


async def _autocompact_state(orch: Orchestrator, key: SessionKey) -> dict[str, Any]:
    session = await orch._sessions.get_active(key)
    topic_value = session.autocompact if session else None
    global_value = orch._config.autocompact
    effective = resolve_autocompact(topic_value, global_value)
    provider = session.provider if session else orch.resolve_runtime_target(orch._config.model)[1]
    supported = provider == "claude" or (provider == "codex" and effective != "off")
    reason = (
        None
        if supported
        else ("off_unsupported" if provider == "codex" else "provider_unsupported")
    )
    return {
        "topic_value": topic_value,
        "global_value": global_value,
        "effective_value": effective,
        "provider": provider,
        "supported": supported,
        "unsupported_reason": reason,
    }


async def _save_autocompact_values(
    orch: Orchestrator, key: SessionKey, data: dict[str, Any]
) -> bool:
    """Caller holds the global settings lock across both saves and any rollback."""
    previous_global = orch._config.autocompact
    for scope in ("global", "topic"):
        if scope not in data:
            continue
        result = await save_autocompact(orch, key, scope, data[scope], _global_lock_held=True)
        if result.buttons is None:
            if scope == "topic" and "global" in data:
                restored = await save_autocompact(
                    orch, key, "global", previous_global, _global_lock_held=True
                )
                if restored.buttons is None:
                    logger.warning("Autocompact global rollback failed")
            return False
    return True


def _register_conversation_routes(  # noqa: C901, PLR0915
    app: web.Application, bot: TelegramBot, runtime: ChatLogRuntime
) -> None:
    turns = WebTurns(bot, runtime)
    topics = WebTopics(bot, runtime, turns)
    register_cron_routes(app, bot, topics, turns)
    register_upload_routes(
        app,
        bot._orch.paths.workspace / "webchat_files",
        topics.lookup,
        config=bot.config.webchat,
        settings_path=bot._orch.paths.webchat_dir / "attach_settings.json",
    )
    attachments = MessageAttachments(
        app[UPLOADS],
        app[ATTACH_SETTINGS],
        bot._orch.paths.workspace,
        bot._orch.paths.webchat_dir / "attachments.jsonl",
        cleanup=bot.config.cleanup,
    )

    if hasattr(runtime, "history"):
        runtime.history.attachment_index_path = attachments.index.path

    downloads = AttachmentDownloads(
        bot._orch.paths.workspace, attachments.index, bot._orch.paths.webchat_dir / "thumbnails"
    )

    async def recover_attachments(_app: web.Application) -> AsyncIterator[None]:
        await attachments.recover()
        yield

    app.cleanup_ctx.append(recover_attachments)
    read_state = ReadState(bot._orch.paths.webchat_dir / "read_state.json")
    dispatcher = WebCommandDispatcher(
        bot,
        turns,
        secret=WebSecrets(
            bot._orch.paths.ductor_home,
            store_path(bot._orch.paths.ductor_home),
            bot.config.language,
        ),
    )
    strings = TranslationStore(bot.config.language)
    sockets: set[web.WebSocketResponse] = set()
    control_gates: dict[str, asyncio.Lock] = {}

    def control_gate(tid: str) -> asyncio.Lock:
        return control_gates.setdefault(tid, asyncio.Lock())

    async def commands(request: web.Request) -> web.Response:
        requested_language = request.query.get("lang", bot.config.language)
        for language in LANGUAGES:
            if language == requested_language:
                return web.json_response({"commands": command_list(language)})
        return _error("bad_request", 400)

    async def list_topics(_request: web.Request) -> web.Response:
        try:
            listed = await topics.list()
        except LockTimeoutError:
            return _error("history_busy", 503)
        try:
            cursors = await read_state.load()
        except OSError:
            cursors = {}
        return web.json_response(
            {"topics": [{**topic, "seen": cursors.get(topic["tid"])} for topic in listed]}
        )

    async def save_seen(request: web.Request) -> web.Response:
        tid = request.match_info["tid"]
        seq = (await _body(request)).get("seq")
        if type(seq) is not int:
            return _error("bad_request", 400)
        async with read_state.lock:
            if tid in turns.blocked or await topics.lookup(tid) is None:
                return _error("unknown_topic", 404)
            try:
                latest = await runtime.history.last(tid)
            except LockTimeoutError:
                return _error("history_busy", 503)
            updates = {tid: min(seq, latest.seq)} if latest is not None else {}
            try:
                cursors, changed = await read_state.merge(updates)
            except OSError:
                return _error("generic", 503)
            if changed:
                runtime.events.publish({"type": "seen", "seen": {tid: cursors[tid]}})
            return web.json_response({"seen": cursors.get(tid)})

    async def see_all(_request: web.Request) -> web.Response:
        async with read_state.lock:
            try:
                listed = await topics.list()
                updates = {}
                for topic in listed:
                    tid = topic["tid"]
                    if tid in turns.blocked:
                        continue
                    latest = await runtime.history.last(tid)
                    if latest is not None:
                        updates[tid] = latest.seq
            except LockTimeoutError:
                return _error("history_busy", 503)
            try:
                cursors, changed = await read_state.merge(updates)
            except OSError:
                return _error("generic", 503)
            if changed:
                runtime.events.publish({"type": "seen", "seen": cursors, "all": True})
            return web.json_response({"seen": cursors})

    async def create_topic(request: web.Request) -> web.Response:
        name = _name(await _body(request))
        if name is None:
            return _error("bad_name", 400)
        try:
            topic = await topics.create(name)
        except (TelegramAPIError, ClientError, ConnectionError, TimeoutError):
            return _error("telegram_unavailable", 503)
        return web.json_response({"topic": topic}, status=201)

    async def manage_topic(request: web.Request) -> web.Response:  # noqa: PLR0911
        tid = request.match_info["tid"]
        if tid == "unknown":
            return _error("not_a_topic", 409)
        data = await _body(request)
        async with topics.gate(tid), control_gate(tid):
            topic = await topics.lookup(tid)
            if topic is None:
                return _error("unknown_topic", 404)
            if not topic["deletable"]:
                return _error("not_a_topic", 409)
            try:
                if request.method == "PATCH":
                    name = _name(data)
                    if name is None:
                        return _error("bad_name", 400)
                    return web.json_response({"topic": await topics.rename(topic, name)})
                if data.get("confirm_name") != topic["name"]:
                    return _error("confirm_mismatch", 400)
                async with read_state.lock:
                    result = await topics.delete(tid)
                    try:
                        await read_state.remove(tid)
                    except OSError:
                        logger.warning("Unable to clean up webchat read state after topic deletion")
                return web.json_response(result)
            except TopicBusyError:
                return _error("topic_busy", 409)
            except (TelegramAPIError, ClientError, ConnectionError, TimeoutError, TopicDeleteError):
                return _error("telegram_unavailable", 503)

    async def autocompact(request: web.Request) -> web.Response:
        tid = request.match_info["tid"]
        if tid == "unknown":
            return _error("read_only", 409)
        data = await _body(request) if request.method == "PUT" else {}
        if request.method == "PUT":
            if not data or not data.keys() <= {"topic", "global"}:
                return _error("bad_request", 400)
            try:
                for scope, value in data.items():
                    if scope != "topic" or value is not None:
                        data[scope] = parse_autocompact_input(value)
            except ValueError:
                return _error("bad_request", 400)
        async with bot._orch._autocompact_global_lock:
            if await topics.lookup(tid) is None or tid in turns.blocked:
                return _error("unknown_topic", 404)
            key = from_tid(tid)
            saved = not data or await _save_autocompact_values(bot._orch, key, data)
            state = await _autocompact_state(bot._orch, key)
        if not saved:
            return web.json_response({"error": "generic", **state}, status=503)
        return web.json_response(state)

    async def automations(request: web.Request) -> web.Response:  # noqa: PLR0911
        tid = request.match_info["tid"]
        if tid == "unknown":
            return _error("read_only", 409)
        kind = request.match_info.get("kind", "")
        item_id = request.match_info.get("id", "")
        data = await _body(request) if request.method == "PUT" else {}
        if request.method == "PUT":
            if kind not in {"cron", "webhook"} or data.keys() != {"value"}:
                return _error("bad_request", 400)
            try:
                if data["value"] is not None:
                    data["value"] = parse_autocompact_input(data["value"])
            except ValueError:
                return _error("bad_request", 400)
        async with topics.gate(tid):
            if tid in turns.blocked or await topics.lookup(tid) is None:
                return _error("unknown_topic", 404)
            key = from_tid(tid)
            async with input_lock(bot._sequential.get_lock(key.lock_key)):
                try:
                    if request.method == "PUT" and not save_automation(
                        bot._orch, kind, item_id, data["value"]
                    ):
                        return _error("not_found", 404)
                    state = automation_state(bot._orch)
                except (OSError, ValueError, LockTimeoutError):
                    return _error("generic", 503)
        return web.json_response(state)

    async def messages(request: web.Request) -> web.Response:
        tid = request.match_info["tid"]
        if await topics.lookup(tid) is None:
            return _error("unknown_topic", 404)
        try:
            before = int(request.query["before"]) if "before" in request.query else None
            limit = int(request.query.get("limit", "100"))
            entries, more = await runtime.history.read_page(tid, before=before, limit=limit)
        except ValueError:
            return _error("bad_request", 400)
        except LockTimeoutError:
            return _error("history_busy", 503)
        mirrors = await runtime.outbox.mirror_states([entry.id for entry in entries])
        indexed = await attachments.index.get_many(tid, [entry.id for entry in entries])
        rendered = []
        metadata = await runtime.history.metadata(tid)
        for entry in entries:
            mirror = mirrors[entry.id]
            legacy: dict[str, Any] = {}
            if entry.role != "user":
                text, buttons = extract_button_rows(entry.text)
                legacy = {"text": text}
                if buttons is not None:
                    legacy["buttons"] = buttons
            rendered.append(
                {
                    **asdict(entry),
                    **legacy,
                    **metadata.get(entry.id, {}),
                    **selector_fields({**legacy, **metadata.get(entry.id, {})}),
                    "mirror": {"state": mirror} if mirror else None,
                    "has_attachments": bool(indexed[entry.id]),
                }
            )
        return web.json_response({"entries": rendered, "has_more": more})

    async def send_or_abort(  # noqa: PLR0911 -- validate ingress before attachment admission
        request: web.Request,
    ) -> web.Response:
        tid = request.match_info["tid"]
        if tid == "unknown":
            return _error("read_only", 409)
        abort = request.path.endswith("/abort")
        data = {} if abort else await _body(request)
        text = data.get("text")
        client_id = data.get("client_id")
        attachment_ids = data.get("attachment_ids", [])
        if not abort and not isinstance(attachment_ids, list):
            return _error("bad_attachments", 400)
        if not abort and (
            not isinstance(text, str)
            or (not text.strip() and not attachment_ids)
            or len(text) > 32000
            or not isinstance(client_id, str)
            or not client_id
        ):
            return _error("bad_request", 400)
        parsed = parse_command(text) if isinstance(text, str) else None
        if attachment_ids and parsed is not None:
            return _error("attachment_command", 400)
        control = abort or (parsed is not None and parsed[0] in {"stop", "interrupt", "stop_all"})
        # Control requests bypass slow commands. Management acquires the normal
        # gate first, then this control gate, so deletion remains exclusive.
        async with control_gate(tid) if control else topics.gate(tid):
            if tid in turns.blocked or await topics.lookup(tid) is None:
                return _error("unknown_topic", 404)
            if abort:
                return web.json_response({"killed": await turns.abort(tid)})
            assert isinstance(text, str)
            try:
                if attachment_ids:
                    return await dispatch_attachments(
                        tid, text, attachment_ids, request.headers.get("X-Dtw-Owner", "")
                    )
                return await dispatch_text(tid, text)
            except (WebCommandBusyError, OSError) as error:
                return (
                    _error("web_turn_running", 409)
                    if isinstance(error, WebCommandBusyError)
                    else _error("generic", 503)
                )

    async def cancel_turn(request: web.Request) -> web.Response:
        tid, turn_id = request.match_info["tid"], request.match_info["turn_id"]
        async with control_gate(tid):
            if tid in turns.blocked or await topics.lookup(tid) is None:
                return _error("unknown_topic", 404)
            try:
                result = await turns.cancel(tid, turn_id)
            except (OSError, LockTimeoutError):
                return _error("generic", 503)
            if result != "cancelled":
                return _error(result, 404 if result == "unknown_turn" else 409)
            return web.json_response({"turn_id": turn_id, "phase": "cancelled"})

    async def dispatch_attachments(
        tid: str, text: str, upload_ids: list[str], owner: str
    ) -> web.Response:
        # Keep attachment admission outside command dispatch and queue internals.
        try:
            prepared = await attachments.prepare(tid, owner, text, upload_ids)
        except UploadError as exc:
            return _error(exc.code, exc.status)
        except OSError:
            return _error("attachment_storage_unavailable", 503)
        try:
            turn_id = await turns.submit(
                tid, prepared.text, turn=prepared.turn, attachments=prepared
            )
        except (OSError, asyncio.CancelledError):
            try:
                await attachments.rollback(tid, owner, prepared)
            except (OSError, UploadError):
                logger.warning("Attachment admission rollback unavailable")
            raise
        return web.json_response({"turn_id": turn_id}, status=202)

    async def entry_attachments(request: web.Request) -> web.Response:
        tid, entry_id = request.match_info["tid"], request.match_info["entry_id"]
        if await topics.lookup(tid) is None:
            return _error("unknown_topic", 404)
        try:
            entry = await runtime.history.get_entry(tid, entry_id)
            if entry is None:
                return _error("unknown_entry", 404)
        except LockTimeoutError:
            return _error("history_busy", 503)
        items = await downloads.list(tid, entry)
        return web.json_response({"attachments": [item.metadata for item in items]})

    async def download_attachment(request: web.Request) -> web.StreamResponse:
        tid = request.match_info["tid"]
        if await topics.lookup(tid) is None:
            return _error("unknown_topic", 404)
        try:
            entry = await runtime.history.get_entry(tid, request.match_info["entry_id"])
        except LockTimeoutError:
            return _error("history_busy", 503)
        if entry is None:
            return _error("unknown_entry", 404)
        item = await downloads.find(tid, entry, request.match_info["attachment_id"])
        if item is None:
            return _error("unknown_attachment", 404)
        return await stream_attachment(request, item)

    app.router.add_get(
        f"{_PREFIX}/topics/{{tid}}/messages/{{entry_id}}/attachments", entry_attachments
    )

    for suffix in ("", "/preview"):
        app.router.add_get(
            f"{_PREFIX}/topics/{{tid}}/messages/{{entry_id}}/attachments/{{attachment_id}}"
            + suffix,
            download_attachment,
        )

    async def dispatch_text(  # noqa: C901 -- preserve existing command/message ingress
        tid: str,
        text: str,
        *,
        session_name: str | None = None,
        accept: Callable[[], Awaitable[None]] | None = None,
    ) -> web.Response:
        key = from_tid(tid)
        parsed = parse_command(text)
        if parsed is not None and parsed[0] == "secret":
            if accept is not None:
                await accept()
            safe = dispatcher.secret.safe_command(parsed[1])
            shown = strings.chat("chat_log.mirror_command", text=safe)
            entry = await runtime.recorder.command_input(key, safe)
            await mirror_secret(tid, shown, entry.id, kind="user_mirror")
            result = await dispatcher.secret.command(tid, parsed[1])
            await record_secret_result(tid, result)
            return web.json_response(result)
        if session_name is not None:
            if turns.busy(tid):
                raise WebCommandBusyError
            if accept is not None:
                await accept()
            return web.json_response(
                {"turn_id": await turns.submit(tid, text, session_name=session_name)}, status=202
            )

        async def record_input() -> None:
            if accept is not None:
                await accept()
            entry = await runtime.recorder.command_input(key, text)
            await runtime.outbox.enqueue(
                kind="user_mirror",
                chat_id=key.chat_id,
                topic_id=key.topic_id,
                text=strings.chat("chat_log.mirror_command", text=text),
                history_id=entry.id,
            )
            runtime.drainer.wake()

        async def record_result(result: CommandResponse) -> None:
            entry = await runtime.recorder.command_result(key, result["text"], result["buttons"])
            await runtime.outbox.enqueue(
                kind="reply_mirror",
                chat_id=key.chat_id,
                topic_id=key.topic_id,
                text=result["text"],
                history_id=entry.id,
            )
            runtime.drainer.wake()

        if text.strip().lower() == "/stop":
            await record_input()
            killed = await turns.abort(tid)
            await record_result(
                {
                    "text": stop_text(
                        bool(killed), bot._orch.active_provider_name if killed else ""
                    ),
                    "buttons": None,
                }
            )
            return web.json_response({"killed": killed}, status=202)
        command = await dispatcher.dispatch(
            tid, text, on_start=record_input, on_finish=record_result
        )
        if command is not None:
            if accept is not None:
                await accept()
            return web.json_response(command)
        if turns.busy(tid) and accept is not None:
            raise WebCommandBusyError
        if accept is not None:
            await accept()
        return web.json_response({"turn_id": await turns.submit(tid, text)}, status=202)

    async def mirror_secret(
        tid: str, text: str, entry_id: str, *, kind: Literal["user_mirror", "reply_mirror"]
    ) -> None:
        key = from_tid(tid)
        await runtime.outbox.enqueue(
            kind=kind, chat_id=key.chat_id, topic_id=key.topic_id, text=text, history_id=entry_id
        )
        runtime.drainer.wake()

    async def record_secret_result(tid: str, result: dict[str, Any]) -> None:
        # Capabilities and modal metadata must never enter history or the outbox.
        if result["text"]:
            entry = await runtime.recorder.command_result(from_tid(tid), result["text"], None)
            await mirror_secret(tid, result["text"], entry.id, kind="reply_mirror")

    async def secret_step(request: web.Request) -> web.Response:
        tid = request.match_info["tid"]
        if tid == "unknown":
            return _error("read_only", 409)
        payload = await _body(request)
        token = payload.get("token")
        action = request.match_info["action"]
        value = payload.get("value" if action == "value" else "confirm")
        if (
            not isinstance(token, str)
            or not token
            or (not isinstance(value, str) if action == "value" else type(value) is not bool)
        ):
            return _error("bad_request", 400)
        assert isinstance(value, (str, bool))
        async with topics.gate(tid):
            if tid in turns.blocked or await topics.lookup(tid) is None:
                return _error("unknown_topic", 404)
            status, result = await dispatcher.secret.complete(tid, token, action, value)
            if status != 403 and "text" in result:
                await record_secret_result(tid, result)
            return web.json_response(result, status=status)

    app.router.add_post(f"{_PREFIX}/topics/{{tid}}/secret/{{action:value|confirm}}", secret_step)

    async def button_context(
        tid: str,
        entry_id: str,
        label: str,
        *,
        register: bool = False,
    ) -> tuple[dict[str, Any], str]:
        if await topics.lookup(tid) is None:
            raise web.HTTPNotFound(
                text='{"error":"unknown_topic"}', content_type="application/json"
            )
        entry = await runtime.history.get_entry(tid, entry_id)
        if entry is None:
            raise web.HTTPNotFound(
                text='{"error":"unknown_entry"}', content_type="application/json"
            )
        meta = (await runtime.history.metadata(tid)).get(entry_id, {})
        rows = meta.get("buttons")
        if rows is None:
            _, rows = extract_button_rows(entry.text)
        data = response_button_data(rows, label) if entry.role != "user" else None
        if data is None:
            raise web.HTTPBadRequest(text='{"error":"bad_button"}', content_type="application/json")
        if register and not meta:
            assert rows is not None
            meta = await runtime.history.record_buttons(tid, entry_id, rows, only_if_missing=True)
            data = response_button_data(meta.get("buttons"), label)
            if data is None:
                raise web.HTTPBadRequest(
                    text='{"error":"bad_button"}', content_type="application/json"
                )
        return meta, data

    async def annotation_metadata(
        tid: str, entry_id: str, fallback: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            return (await runtime.history.metadata(tid))[entry_id]
        except Exception as exc:
            logger.warning("Accepted choice metadata failed: %s", type(exc).__name__)
            return fallback

    async def buttons(request: web.Request) -> web.Response:  # noqa: PLR0911 -- request status cases
        tid = request.match_info["tid"]
        if tid == "unknown":
            return _error("read_only", 409)
        data = await _body(request)
        entry_id, label = data.get("entry_id"), data.get("label")
        if not isinstance(entry_id, str) or not entry_id or not isinstance(label, str) or not label:
            return _error("bad_request", 400)
        _, preview = await button_context(tid, entry_id, label)
        command = parse_command(preview)
        control = command is not None and command[0] in {"stop", "interrupt", "stop_all"}
        async with control_gate(tid) if control else topics.gate(tid):
            if tid in turns.blocked or await topics.lookup(tid) is None:
                return _error("unknown_topic", 404)
            meta, callback_data = await button_context(tid, entry_id, label, register=True)
            if meta.get("answered") is not None:
                return _error("already_answered", 409)
            response: web.Response | None = None
            key = from_tid(tid)

            async def claim() -> bool:
                return await runtime.history.mark_answered(tid, entry_id, label)

            async def annotate() -> None:
                latest = await annotation_metadata(tid, entry_id, {**meta, "answered": label})
                await edit_button_choice(
                    bot.bot_instance,
                    key.chat_id,
                    latest.get("telegram_message_id"),
                    label,
                    html_text=latest.get("telegram_html_text"),
                )

            async def submit(
                text: str, session_name: str | None, accept: Callable[[], Awaitable[None]]
            ) -> None:
                nonlocal response
                response = await dispatch_text(tid, text, session_name=session_name, accept=accept)

            try:
                accepted = await process_button_choice(callback_data, claim, annotate, submit)
            except WebCommandBusyError:
                return _error("web_turn_running", 409)
            if not accepted:
                return _error("already_answered", 409)
            assert response is not None
            return web.json_response(
                {**json.loads(response.text or "{}"), "entry_id": entry_id, "answered": label},
                status=response.status,
            )

    async def callbacks(request: web.Request) -> web.Response:  # noqa: C901, PLR0911, PLR0912 -- explicit callback admission and HTTP statuses
        tid = request.match_info["tid"]
        if tid == "unknown":
            return _error("read_only", 409)
        payload = await _body(request)
        entry_id, data, revision = (
            payload.get("entry_id"),
            payload.get("data"),
            payload.get("revision"),
        )
        if (
            not isinstance(entry_id, str)
            or not entry_id
            or not isinstance(data, str)
            or not data
            or type(revision) is not int
            or revision < 0
        ):
            return _error("bad_request", 400)
        async with topics.gate(tid):
            if tid in turns.blocked or await topics.lookup(tid) is None:
                return _error("unknown_topic", 404)
            key = from_tid(tid)
            pool = bot._orch._lock_pool
            assert pool is not None
            selector_lock_key = selector_callback_lock_key(key, data)
            if selector_lock_key is not None and (
                turns.busy(tid) or pool.get(selector_lock_key).locked()
            ):
                return _error("web_turn_running", 409)
            entry = await runtime.history.get_entry(tid, entry_id)
            if entry is None:
                return _error("unknown_entry", 404)
            if entry.role == "user":
                return _error("bad_callback", 400)
            try:
                async with runtime.history.selector_step(tid, entry_id, data, revision) as meta:
                    if not selector_button_data(data):
                        return _error("bad_callback", 400)
                    if selector_lock_key is not None and (
                        turns.busy(tid) or pool.get(selector_lock_key).locked()
                    ):
                        return _error("web_turn_running", 409)
                    step = await selector_callback_response(bot, key, data)
                    if step.prompt is not None:
                        label = next(
                            button["text"]
                            for row in meta["buttons"]
                            for button in row
                            if button.get("callback_data") == data
                        )

                        accepted_meta = {**meta, "answered": label}

                        async def accept() -> None:
                            nonlocal accepted_meta
                            if not await runtime.history.mark_answered(tid, entry_id, label):
                                raise SelectorConflictError  # noqa: TRY301 -- acceptance runs inside dispatch ingress
                            accepted_meta = await annotation_metadata(tid, entry_id, accepted_meta)
                            await edit_button_choice(
                                bot.bot_instance,
                                key.chat_id,
                                accepted_meta.get("telegram_message_id"),
                                label,
                                html_text=accepted_meta.get("telegram_html_text"),
                            )

                        response = await dispatch_text(tid, step.prompt, accept=accept)
                        latest = accepted_meta
                        return web.json_response(
                            {
                                **json.loads(response.text or "{}"),
                                "entry_id": entry_id,
                                "text": latest.get("text", entry.text),
                                "buttons": latest["buttons"],
                                "answered": latest["answered"],
                                **selector_fields(latest),
                            },
                            status=response.status,
                        )
                    if step.response is None:
                        return _error("bad_callback", 400)
                    latest = await runtime.history.record_selector_response(
                        tid, entry_id, step.response.text, selector_rows(step.response)
                    )
                    await edit_recorded_selector(
                        bot.bot_instance,
                        key.chat_id,
                        latest.get("telegram_message_id"),
                        step.response,
                    )
                    return web.json_response(
                        {
                            "entry_id": entry_id,
                            "text": latest["text"],
                            "buttons": latest["buttons"],
                            **selector_fields(latest),
                        }
                    )
            except SelectorConflictError:
                return _error("stale_selector", 409)
            except InvalidSelectorButtonError:
                return _error("bad_callback", 400)
            except WebCommandBusyError:
                return _error("web_turn_running", 409)

    async def search(request: web.Request) -> web.Response:
        query = request.query.get("q", "")
        if not 1 <= len(query) <= 200:
            return _error("bad_request", 400)
        return web.json_response({"hits": await runtime.history.search(query)})

    async def events(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        queue = runtime.events.subscribe()
        pump: asyncio.Task[None] | None = None

        async def forward() -> None:
            while True:
                event = await queue.get()
                if event is None:
                    await ws.close(code=1013)
                    return
                tid = event.get("tid") or event.get("item", {}).get("tid")
                if event.get("type") == "entry":
                    mirror = await runtime.outbox.mirror_state(event["entry"]["id"])
                    event = {
                        **event,
                        "entry": {
                            **event["entry"],
                            **selector_fields(event["entry"]),
                            "has_attachments": bool(
                                await attachments.index.get(tid, event["entry"]["id"])
                            ),
                            "mirror": {"state": mirror} if mirror else None,
                        },
                    }
                if event.get("type") == "outbox" and "history_id" in event:
                    history_id = event["history_id"]
                    mirror = await runtime.outbox.mirror_state(history_id) if history_id else None
                    event = {**event, "mirror": mirror}
                if event.get("type") == "entry_meta":
                    event = {**event, "meta": {**event["meta"], **selector_fields(event["meta"])}}
                if tid not in turns.blocked:
                    await ws.send_json(event)

        try:
            await ws.prepare(request)
            sockets.add(ws)
            pump = asyncio.create_task(forward(), name="webchat-events")
            async for _message in ws:
                pass
        finally:
            runtime.events.unsubscribe(queue)
            sockets.discard(ws)
            if pump is not None:
                pump.cancel()
                await asyncio.gather(pump, return_exceptions=True)
            with contextlib.suppress(ConnectionError):
                await ws.close()
        return ws

    async def shutdown(_app: web.Application) -> None:
        await asyncio.gather(*(ws.close(code=1001) for ws in tuple(sockets)))
        await turns.stop()

    app.on_shutdown.append(shutdown)
    app.router.add_get(f"{_PREFIX}/commands", commands)
    app.router.add_get(f"{_PREFIX}/topics", list_topics)
    app.router.add_post(f"{_PREFIX}/topics", create_topic)
    app.router.add_post(f"{_PREFIX}/topics/seen-all", see_all)
    app.router.add_put(f"{_PREFIX}/topics/{{tid}}/seen", save_seen)
    app.router.add_patch(f"{_PREFIX}/topics/{{tid}}", manage_topic)
    app.router.add_delete(f"{_PREFIX}/topics/{{tid}}", manage_topic)
    app.router.add_get(f"{_PREFIX}/topics/{{tid}}/autocompact", autocompact)
    app.router.add_put(f"{_PREFIX}/topics/{{tid}}/autocompact", autocompact)
    app.router.add_get(f"{_PREFIX}/topics/{{tid}}/autocompact/automations", automations)
    app.router.add_put(
        f"{_PREFIX}/topics/{{tid}}/autocompact/automations/{{kind}}/{{id}}", automations
    )
    app.router.add_get(f"{_PREFIX}/topics/{{tid}}/messages", messages)
    app.router.add_post(f"{_PREFIX}/topics/{{tid}}/messages", send_or_abort)
    app.router.add_post(f"{_PREFIX}/topics/{{tid}}/abort", send_or_abort)
    app.router.add_post(f"{_PREFIX}/topics/{{tid}}/turns/{{turn_id}}/cancel", cancel_turn)
    app.router.add_post(f"{_PREFIX}/topics/{{tid}}/buttons", buttons)
    app.router.add_post(f"{_PREFIX}/topics/{{tid}}/callbacks", callbacks)
    app.router.add_get(f"{_PREFIX}/search", search)
    app.router.add_get(f"{_PREFIX}/events", events)


def _register_file_routes(app: web.Application, bot: TelegramBot, runtime: ChatLogRuntime) -> None:
    files = WebFiles(bot._orch.paths)

    async def outbox(_request: web.Request) -> web.Response:
        return web.json_response({"items": await runtime.outbox.views()})

    async def listing(_request: web.Request) -> web.Response:
        return web.json_response({"files": await files.list()})

    async def file(request: web.Request) -> web.Response:
        name = request.match_info["name"]
        if not files.allowed(name):
            return _error("not_found", 404)
        try:
            if request.method == "GET":
                return web.json_response(await files.get(name))
            data = await _body(request)
            content, base = data.get("content"), data.get("base_sha256")
            if not isinstance(content, str) or not isinstance(base, str):
                return _error("bad_request", 400)
            return web.json_response(await files.put(name, content, base))
        except web.HTTPRequestEntityTooLarge:
            return _error("too_large", 413)
        except WebFileError as exc:
            return _error(exc.code, exc.status)

    app.router.add_get(f"{_PREFIX}/outbox", outbox)
    app.router.add_get(f"{_PREFIX}/files", listing)
    app.router.add_get(f"{_PREFIX}/files/{{name}}", file)
    app.router.add_put(f"{_PREFIX}/files/{{name}}", file)


def _register_cli_login_routes(app: web.Application, bot: TelegramBot) -> None:  # noqa: C901, PLR0915
    paths = bot._orch.paths
    manager = CliLoginManager(
        startup_providers=getattr(bot._orch, "available_providers", frozenset()),
        slots=ClaudeLoginSlots(paths),
    )
    app[CLI_LOGIN] = manager

    async def close(_app: web.Application) -> None:
        await manager.close()

    app.on_cleanup.append(close)

    def list_slots() -> list[dict[str, object]]:
        configured = claude_slots.read_accounts(paths.ductor_home)
        priority = list(
            dict.fromkeys(name for name in configured if claude_slots.is_slot_name(name))
        )
        if not configured:
            priority = ["claude"]
        names = [*priority, *claude_slots.unregistered_slots(paths.ductor_home, priority)]
        expired_slots: set[str] = set()
        # Expiration metadata must not prevent listing existing login slots.
        with contextlib.suppress(Exception):
            expired_slots = set(
                SlotRegistry(state_file_path(paths.ductor_home)).load().auth_failures
            )
        return [
            {
                "name": name,
                "kind": "api_key"
                if slot_is_api_key(host_slot_config_dir(paths.ductor_home, name))
                else "oauth",
                "auth_expired": name in expired_slots,
                "priority": priority.index(name) + 1 if name in priority else None,
                "authenticated": slot_is_authenticated(
                    host_slot_config_dir(paths.ductor_home, name)
                ),
            }
            for name in names
        ]

    async def handle(request: web.Request) -> web.Response:  # noqa: C901, PLR0911, PLR0912
        owner = request.headers.get("X-Dtw-Owner", "")
        if not owner:
            return _error("forbidden", 403)
        resource = request.path.removeprefix(f"{_PREFIX}/cli-login")
        try:
            if resource == "/cancel-owner":
                manager.cancel_owner(owner)
                return web.json_response({"ok": True}, status=202)
            if resource == "/status":
                active_owners = None
                if "X-Dtw-Active-Owners" in request.headers:
                    try:
                        owners = json.loads(request.headers["X-Dtw-Active-Owners"])
                    except ValueError:
                        return _error("invalid_login", 400)
                    if not isinstance(owners, list) or not all(isinstance(o, str) for o in owners):
                        return _error("invalid_login", 400)
                    active_owners = frozenset(owners)
                return web.json_response(
                    {
                        **manager.status(owner, active_owners=active_owners),
                        "claude_slots": await asyncio.to_thread(list_slots),
                    }
                )
            if resource == "/jobs":
                data = await _body(request)
                provider, slot = data.get("provider"), data.get("slot")
                new_slot = data.get("new_slot", False)
                if (
                    not isinstance(provider, str)
                    or (slot is not None and not isinstance(slot, str))
                    or not isinstance(new_slot, bool)
                ):
                    return _error("invalid_login", 400)
                job = manager.start(provider, owner, slot=slot, new_slot=new_slot)
                return web.json_response({"job_id": job}, status=202)
            job = request.match_info["id"]
            if request.method in {"GET", "HEAD"}:
                try:
                    after = int(request.query.get("after", "0"))
                except ValueError:
                    after = -1
                # The manager checks ownership before reporting a bad sequence.
                return web.json_response(manager.get(job, owner, after=after))
            if request.match_info["action"] == "input":
                # Establish ownership before parsing or acting on submitted input.
                manager.get(job, owner)
                data = await _body(request)
                kind, value = data.get("kind"), data.get("value")
                if not isinstance(kind, str) or kind != "line" or not isinstance(value, str):
                    return _error("invalid_input", 400)
                manager.send_input(job, owner, kind, value)
                return web.json_response({"ok": True})
            manager.cancel(job, owner)
            return web.json_response({"ok": True}, status=202)
        except CliLoginError as exc:
            return _error(exc.code, exc.status)

    async def delete_slot(request: web.Request) -> web.Response:
        if not request.headers.get("X-Dtw-Owner", ""):
            return _error("forbidden", 403)
        try:
            await asyncio.to_thread(
                claude_slots.remove, paths.ductor_home, request.match_info["slot"]
            )
        except claude_slots.SlotDirectoryError as exc:
            return web.json_response(
                {
                    "error": str(exc),
                    "path": str(exc.path.relative_to(paths.ductor_home)),
                    "reason": exc.reason,
                },
                status=500,
            )
        except claude_slots.SlotDeleteError as exc:
            code = str(exc)
            return _error(code, {"slot_not_found": 404, "slot_busy": 409}.get(code, 400))
        except (OSError, ValueError):
            return _error("slot_delete_error", 500)
        return web.json_response({"ok": True})

    app.router.add_delete(f"{_PREFIX}/cli-login/slots/{{slot}}", delete_slot)
    app.router.add_post(f"{_PREFIX}/cli-login/cancel-owner", handle)
    app.router.add_get(f"{_PREFIX}/cli-login/status", handle)
    app.router.add_post(f"{_PREFIX}/cli-login/jobs", handle)
    app.router.add_get(f"{_PREFIX}/cli-login/jobs/{{id}}", handle)
    app.router.add_post(f"{_PREFIX}/cli-login/jobs/{{id}}/{{action:input|cancel}}", handle)


async def start_bridge(bot: TelegramBot, runtime: ChatLogRuntime) -> Callable[[], Awaitable[None]]:
    """Open the bridge listener and return its idempotent cleanup callback."""
    path = bot._orch.paths.webchat_dir / "share" / "bridge.token"
    if not path.exists():
        atomic_text_save(path, secrets.token_urlsafe(32) + "\n")
    path.chmod(0o600)
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        raise ValueError("bridge_token_empty")
    runner = web.AppRunner(create_bridge_app(bot, runtime, token), access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, "0.0.0.0", bot.config.webchat.bridge_port).start()  # noqa: S104
    except BaseException:
        await runner.cleanup()
        raise

    logger.info("Webchat bridge listening on %s:%d", "0.0.0.0", bot.config.webchat.bridge_port)  # noqa: S104

    async def stop() -> None:
        await runner.cleanup()

    return stop
