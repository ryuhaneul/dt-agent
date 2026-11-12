"""Token-protected HTTP bridge hosted by the main Telegram bot."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from aiogram.exceptions import TelegramAPIError
from aiohttp import ClientError, web

from ductor_bot.config import resolve_user_timezone
from ductor_bot.infra.atomic_io import atomic_text_save
from ductor_bot.infra.secret_store import store_path
from ductor_bot.webchat.auth import AuthError, BridgeAuth
from ductor_bot.webchat.files import WebFileError, WebFiles
from ductor_bot.webchat.settings import resolve_default_chat_id
from ductor_bot.webchat.topics import TopicBusyError, TopicDeleteError, WebTopics
from ductor_bot.webchat.turns import WebTurns

if TYPE_CHECKING:
    from ductor_bot.chat_log.runtime import ChatLogRuntime
    from ductor_bot.messenger.telegram.app import TelegramBot

logger = logging.getLogger(__name__)

_PREFIX = "/webchat/v1"


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
        return await handler(request)

    app = web.Application(middlewares=[authenticate], client_max_size=512 * 1024)

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
        return web.json_response({**auth.state(), "credential_generation": credential_generation()})

    async def authenticate_user(request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return web.json_response({"error": "invalid"}, status=403)
        if not isinstance(data, dict):
            return web.json_response({"error": "invalid"}, status=403)
        try:
            result = await auth.handle(request.path.removeprefix(f"{_PREFIX}/auth/"), data)
        except AuthError as exc:
            return web.json_response(exc.payload, status=403)
        if request.path == f"{_PREFIX}/auth/totp":
            result["credential_generation"] = credential_generation()
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
    for action in ("password", "totp", "setup/begin", "setup/confirm"):
        app.router.add_post(f"{_PREFIX}/auth/{action}", authenticate_user)
    app.router.add_get(f"{_PREFIX}/state", state)
    _register_conversation_routes(app, bot, runtime)
    _register_file_routes(app, bot, runtime)
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


def _register_conversation_routes(  # noqa: C901, PLR0915
    app: web.Application, bot: TelegramBot, runtime: ChatLogRuntime
) -> None:
    turns = WebTurns(bot, runtime)
    topics = WebTopics(bot, runtime, turns)
    sockets: set[web.WebSocketResponse] = set()

    async def list_topics(_request: web.Request) -> web.Response:
        return web.json_response({"topics": await topics.list()})

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
        async with topics.gate(tid):
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
                return web.json_response(await topics.delete(tid))
            except TopicBusyError:
                return _error("topic_busy", 409)
            except (TelegramAPIError, ClientError, ConnectionError, TimeoutError, TopicDeleteError):
                return _error("telegram_unavailable", 503)

    async def messages(request: web.Request) -> web.Response:
        tid = request.match_info["tid"]
        if await topics.lookup(tid) is None:
            return _error("unknown_topic", 404)
        try:
            before = int(request.query["before"]) if "before" in request.query else None
            limit = int(request.query.get("limit", "100"))
            entries = await runtime.history.read(tid, before=before, limit=limit)
        except ValueError:
            return _error("bad_request", 400)
        rendered = []
        for entry in entries:
            mirror = await runtime.outbox.mirror_state(entry.id)
            rendered.append({**asdict(entry), "mirror": {"state": mirror} if mirror else None})
        more = bool(entries and await runtime.history.read(tid, before=entries[0].seq, limit=1))
        return web.json_response({"entries": rendered, "has_more": more})

    async def send_or_abort(request: web.Request) -> web.Response:  # noqa: PLR0911
        tid = request.match_info["tid"]
        if tid == "unknown":
            return _error("read_only", 409)
        async with topics.gate(tid):
            if tid in turns.blocked or await topics.lookup(tid) is None:
                return _error("unknown_topic", 404)
            if request.path.endswith("/abort"):
                return web.json_response({"killed": await turns.abort(tid)})
            data = await _body(request)
            text = data.get("text")
            client_id = data.get("client_id")
            if (
                not isinstance(text, str)
                or not text.strip()
                or len(text) > 32000
                or not isinstance(client_id, str)
                or not client_id
            ):
                return _error("bad_request", 400)
            if text.strip().lower() == "/stop":
                return web.json_response({"killed": await turns.abort(tid)}, status=202)
            if turns.busy(tid):
                return _error("web_turn_running", 409)
            return web.json_response({"turn_id": turns.submit(tid, text)}, status=202)

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
                            "mirror": {"state": mirror} if mirror else None,
                        },
                    }
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
    app.router.add_get(f"{_PREFIX}/topics", list_topics)
    app.router.add_post(f"{_PREFIX}/topics", create_topic)
    app.router.add_patch(f"{_PREFIX}/topics/{{tid}}", manage_topic)
    app.router.add_delete(f"{_PREFIX}/topics/{{tid}}", manage_topic)
    app.router.add_get(f"{_PREFIX}/topics/{{tid}}/messages", messages)
    app.router.add_post(f"{_PREFIX}/topics/{{tid}}/messages", send_or_abort)
    app.router.add_post(f"{_PREFIX}/topics/{{tid}}/abort", send_or_abort)
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
