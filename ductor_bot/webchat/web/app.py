"""Browser-facing authentication, static assets and bridge relay."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

from ductor_bot.i18n import LANGUAGES
from ductor_bot.i18n.loader import load_web_strings
from ductor_bot.webchat.web.qr import qr_svg
from ductor_bot.webchat.web.sessions import (
    PREAUTH_COOKIE,
    SESSION_COOKIE,
    Session,
    SessionStore,
    clear_preauth,
    clear_session,
    set_preauth,
    set_session,
)
from ductor_bot.webchat.web.upstream import BridgeClient, BridgeUnavailableError

CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
    "connect-src 'self'; frame-src 'self'; frame-ancestors 'none'; "
    "base-uri 'none'; form-action 'self'"
)
STARFLOW_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "frame-ancestors 'self'"
)
_MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_PUBLIC = frozenset(
    {
        "/api/health",
        "/api/i18n",
        "/api/auth/state",
        "/api/auth/password",
        "/api/auth/totp",
        "/api/auth/setup/begin",
        "/api/auth/setup/confirm",
    }
)


@dataclass(slots=True)
class WebState:
    bridge_url: str
    bridge_token: str = field(repr=False)
    public_origin: str
    static_dir: Path
    sessions: SessionStore = field(default_factory=SessionStore)
    sockets: dict[web.WebSocketResponse, str] = field(default_factory=dict)
    bridge: BridgeClient = field(init=False)
    validated: dict[str, float] = field(default_factory=dict)

    def checked_session(self, token: str, data: dict[str, Any]) -> Session | None:
        session = self.sessions.get(token, touch=False)
        if session is not None and (
            not data.get("password_configured")
            or not data.get("totp_configured")
            or not session.credential_generation
            or session.credential_generation != data.get("credential_generation")
        ):
            self.sessions.delete(token)
            self.validated.pop(token, None)
            return None
        return session

    async def socket_session(self, token: str, *, force: bool = False) -> Session | None:
        session = self.sessions.get(token, touch=False)
        if session is None:
            self.validated.pop(token, None)
            return None
        if not force and time.monotonic() - self.validated.get(token, float("-inf")) < 10:
            return session
        status, data = await self.bridge.request("GET", "/auth/state")
        if status != 200:
            raise BridgeUnavailableError
        session = self.checked_session(token, data)
        if session is not None:
            self.validated[token] = time.monotonic()
        return session

    async def publish(self, frame: str) -> None:
        offline = frame == '{"type":"bridge","up":false}'
        if offline or frame == '{"type":"bridge","up":true}':
            # Recovery must validate even a recently checked, now revoked cookie.
            self.validated.clear()
        for socket, token in list(self.sockets.items()):
            try:
                session = (
                    self.sessions.get(token, touch=False)
                    if offline
                    else await self.socket_session(token)
                )
                if session is None:
                    await socket.close()
                else:
                    await socket.send_str(frame)
            except (BridgeUnavailableError, aiohttp.ClientError, ConnectionError, RuntimeError):
                self.validated.pop(token, None)
                await socket.close()


STATE = web.AppKey("webchat_state", WebState)


def _error(code: str, status: int = 403) -> web.Response:
    return web.json_response({"error": code}, status=status)


@web.middleware
async def security(
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> web.StreamResponse:
    try:
        if request.can_read_body:
            await request.read()
        return await handler(request)
    except BridgeUnavailableError:
        return _error("bot_unreachable", 503)
    except web.HTTPException as exc:
        return _error("too_large" if exc.status == 413 else "bad_request", exc.status)


async def response_headers(request: web.Request, response: web.StreamResponse) -> None:
    response.headers["Content-Security-Policy"] = (
        STARFLOW_CSP if request.path == "/static/starflow/base-stars.html" else CSP
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"


@web.middleware
async def authenticate(
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> web.StreamResponse:
    if not request.path.startswith("/api/"):
        return await handler(request)
    if request.method in _MUTATING and request.headers.get("X-Dtw-Client") != "web":
        return _error("forbidden")
    state = request.app[STATE]
    token = request.cookies.get(SESSION_COOKIE, "")
    session = state.sessions.get(token)
    if (
        request.path not in _PUBLIC
        and request.path not in {"/api/auth/logout", "/api/ws"}
        and session is not None
    ):
        status, data = await state.bridge.request("GET", "/auth/state")
        if status != 200:
            raise BridgeUnavailableError
        session = state.checked_session(token, data)
    if request.path not in _PUBLIC and session is None:
        return _error("auth_required")
    if request.method in _MUTATING and session is not None:
        csrf = request.headers.get("X-Dtw-Csrf", "")
        if not hmac.compare_digest(csrf.encode(), session.csrf.encode()):
            return _error("forbidden")
    return await handler(request)


async def _json_body(request: web.Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except (ValueError, UnicodeError, LookupError):
        raise web.HTTPBadRequest from None
    if not isinstance(body, dict):
        raise web.HTTPBadRequest
    return body


async def health(_request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def i18n(request: web.Request) -> web.Response:
    language = "en"
    try:
        status, meta = await request.app[STATE].bridge.request("GET", "/meta")
        if status == 200 and meta.get("language") in LANGUAGES:
            language = meta["language"]
    except BridgeUnavailableError:
        pass
    return web.json_response({"lang": language, "strings": load_web_strings(language)})


async def auth_state(request: web.Request) -> web.Response:
    state = request.app[STATE]
    session = state.sessions.get(request.cookies.get(SESSION_COOKIE, ""))
    configured = False
    bridge_up = False
    try:
        status, data = await state.bridge.request("GET", "/auth/state")
        bridge_up = status == 200
        configured = bool(data.get("totp_configured")) if bridge_up else False
        if bridge_up and session is not None:
            session = state.checked_session(request.cookies.get(SESSION_COOKIE, ""), data)
    except BridgeUnavailableError:
        pass
    return web.json_response(
        {
            "authenticated": session is not None,
            "totp_configured": configured,
            "csrf": session.csrf if session else None,
            "bridge_up": bridge_up,
        }
    )


async def auth(request: web.Request) -> web.Response:
    state = request.app[STATE]
    step = request.match_info["step"]
    body = await _json_body(request)
    client_ip = request.headers.get("X-Real-IP") or request.remote
    if step == "password":
        payload = {"password": body.get("password"), "client_ip": client_ip}
    else:
        preauth = request.cookies.get(PREAUTH_COOKIE)
        if not preauth:
            return _error("invalid")
        payload = {"preauth": preauth}
        if step == "setup/begin":
            payload["mode"] = body.get("mode")
            if "secret" in body:
                payload["secret"] = body["secret"]
        else:
            payload.update({"code": body.get("code"), "client_ip": client_ip})
    status, data = await state.bridge.request(
        "POST",
        "/auth/" + step,
        body=json.dumps(payload).encode(),
    )
    if status != 200:
        return web.json_response(data, status=status)
    if step == "password":
        response = web.json_response({"next": data["next"]})
        set_preauth(response, data["preauth"])
    elif step == "totp":
        response = web.json_response({"ok": True})
        set_session(response, state.sessions.create(data["credential_generation"]))
        clear_preauth(response)
    elif step == "setup/begin":
        secret = data["secret"]
        result = {"secret_display": " ".join(secret[i : i + 4] for i in range(0, len(secret), 4))}
        if body.get("mode") == "new":
            result["qr_svg"] = qr_svg(data["otpauth_uri"])
        response = web.json_response(result)
    else:
        response = web.json_response({"ok": True})
        clear_preauth(response)
    return response


async def logout(request: web.Request) -> web.Response:
    state = request.app[STATE]
    token = request.cookies.get(SESSION_COOKIE, "")
    state.sessions.delete(token)
    state.validated.pop(token, None)
    for socket, owner in list(state.sockets.items()):
        if owner == token:
            await socket.close()
    response = web.json_response({"ok": True})
    clear_session(response)
    clear_preauth(response)
    return response


async def relay(request: web.Request) -> web.Response:
    state = request.app[STATE]
    body = await request.read()
    path = request.rel_url.raw_path.removeprefix("/api")
    if request.query_string:
        path += "?" + request.rel_url.raw_query_string
    status, data = await state.bridge.request(request.method, path, body=body)
    if request.path == "/api/state" and status == 200:
        data.update({"bot": {"up": True}, "web_sessions": state.sessions.count()})
    return web.json_response(data, status=status)


async def websocket(request: web.Request) -> web.StreamResponse:
    state = request.app[STATE]
    if request.headers.get("Origin") != state.public_origin:
        return _error("forbidden")
    token = request.cookies.get(SESSION_COOKIE, "")
    bridge_up = state.bridge.up
    try:
        if await state.socket_session(token, force=True) is None:
            return _error("auth_required")
    except BridgeUnavailableError:
        state.validated.pop(token, None)
        bridge_up = False
    socket = web.WebSocketResponse(heartbeat=30)
    await socket.prepare(request)
    state.sockets[socket] = token
    try:
        await socket.send_json({"type": "bridge", "up": bridge_up})
        while (session := state.sessions.get(token, touch=False)) is not None:
            try:
                message = await socket.receive(timeout=session.remaining())
            except TimeoutError:
                continue
            if message.type in {
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.ERROR,
            }:
                break
            # Browser frames are deliberately ignored; mutations use REST only.
    finally:
        state.sockets.pop(socket, None)
        if token not in state.sockets.values():
            state.validated.pop(token, None)
        await socket.close()
    return socket


async def static(request: web.Request) -> web.StreamResponse:
    root = request.app[STATE].static_dir.resolve()
    name = request.match_info.get("asset", "index.html")
    try:
        path = (root / name).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise web.HTTPNotFound
    except (ValueError, OSError):
        raise web.HTTPNotFound from None
    return web.FileResponse(path)


async def shutdown(app: web.Application) -> None:
    for socket in list(app[STATE].sockets):
        await socket.close(code=1001)


async def lifecycle(app: web.Application) -> AsyncIterator[None]:
    state = app[STATE]
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as client:
        state.bridge = BridgeClient(state.bridge_url, state.bridge_token, client)
        task = asyncio.create_task(state.bridge.events(state.publish))
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


def create_app(
    *,
    bridge_url: str,
    bridge_token: str,
    public_origin: str,
    static_dir: Path | None = None,
) -> web.Application:
    app = web.Application(middlewares=[security, authenticate], client_max_size=512 * 1024)
    app[STATE] = WebState(
        bridge_url=bridge_url,
        bridge_token=bridge_token,
        public_origin=public_origin,
        static_dir=static_dir or Path(__file__).parent / "static",
    )
    app.cleanup_ctx.append(lifecycle)
    app.on_shutdown.append(shutdown)
    app.on_response_prepare.append(response_headers)
    app.router.add_get("/", static)
    app.router.add_get("/static/{asset:.*}", static)
    app.router.add_get("/api/health", health)
    app.router.add_get("/api/i18n", i18n)
    app.router.add_get("/api/auth/state", auth_state)
    app.router.add_post("/api/auth/{step:password|totp|setup/begin|setup/confirm}", auth)
    app.router.add_post("/api/auth/logout", logout)
    app.router.add_get("/api/ws", websocket)
    app.router.add_get("/api/state", relay)
    for path, methods in (
        ("/topics", ("GET", "POST")),
        ("/topics/{tid}", ("PATCH", "DELETE")),
        ("/topics/{tid}/messages", ("GET", "POST")),
        ("/topics/{tid}/abort", ("POST",)),
        ("/search", ("GET",)),
        ("/outbox", ("GET",)),
        ("/files", ("GET",)),
        ("/files/{name}", ("GET", "PUT")),
    ):
        for method in methods:
            app.router.add_route(method, "/api" + path, relay)
    return app
