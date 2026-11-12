"""Browser API contracts exercised against a real local fake bridge."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from unittest.mock import patch

import aiohttp
import pytest
from aiohttp import web

from ductor_bot.i18n.check import _compare_domain
from ductor_bot.i18n.loader import load_web_strings
from ductor_bot.webchat.web.app import CSP, STARFLOW_CSP, STATE, create_app
from ductor_bot.webchat.web.sessions import ABSOLUTE_TTL, IDLE_TTL, SessionStore

ORIGIN = "https://chat.example.com"
CLIENT = {"X-Dtw-Client": "web"}


@dataclass
class FakeBridge:
    language: str = "ko"
    configured: bool = True
    generation: str = "fake-generation"
    unavailable: bool = False
    calls: list = field(default_factory=list)
    sockets: set = field(default_factory=set)
    connected: asyncio.Event = field(default_factory=asyncio.Event)
    connections: int = 0
    delay: float = 0

    async def handle(self, request):  # noqa: C901, PLR0912 - explicit fake API contract
        assert request.headers.get("X-Dtw-Bridge-Token") == "fake-bridge-token"
        assert "Cookie" not in request.headers
        assert "X-Dtw-Csrf" not in request.headers
        if self.unavailable:
            request.transport.close()
            return web.Response()
        path = request.path.removeprefix("/webchat/v1")
        if path == "/events":
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            self.sockets.add(socket)
            self.connections += 1
            self.connected.set()
            try:
                async for _message in socket:
                    pytest.fail("Browser frames must not reach the bridge")
            finally:
                self.sockets.discard(socket)
            return socket
        if self.delay:
            await asyncio.sleep(self.delay)
        raw = await request.read()
        body = json.loads(raw) if raw else None
        self.calls.append((request.method, path, dict(request.query), body, raw))
        if path == "/meta":
            return web.json_response({"language": self.language})
        if path == "/auth/state":
            return web.json_response(
                {
                    "totp_configured": self.configured,
                    "password_configured": True,
                    "credential_generation": self.generation,
                }
            )
        if path == "/auth/password":
            if body["password"] == "locked":
                return web.json_response({"error": "locked", "retry_after": 12}, status=403)
            if body["password"] != "good-password":
                return web.json_response({"error": "invalid"}, status=403)
            return web.json_response(
                {"preauth": "fake-preauth", "next": "totp" if self.configured else "setup"}
            )
        if path.startswith("/auth/"):
            if body["preauth"] != "fake-preauth":
                return web.json_response({"error": "invalid"}, status=403)
            if path == "/auth/setup/begin":
                if self.configured:
                    return web.json_response({"error": "totp_configured"}, status=403)
                return web.json_response(
                    {
                        "secret": "JBSWY3DPEHPK3PXP",
                        "otpauth_uri": "otpauth://totp/example?secret=JBSWY3DPEHPK3PXP",
                    }
                )
            if body["code"] != "123456":
                return web.json_response({"error": "invalid"}, status=403)
            if path == "/auth/setup/confirm":
                self.configured = True
            return web.json_response({"ok": True, "credential_generation": self.generation})
        if path == "/state":
            return web.json_response({"telegram": {"state": "down"}, "outbox": {"pending": 2}})
        if path.startswith("/files/") and request.method == "PUT":
            return web.json_response({"error": "conflict"}, status=409)
        return web.json_response(
            {"path": path, "body": body}, status=202 if path.endswith("/messages") and body else 200
        )

    async def send(self, frame):
        for socket in list(self.sockets):
            await socket.send_str(frame)


@pytest.fixture
async def site(aiohttp_client, aiohttp_server, tmp_path):
    fake = FakeBridge()
    bridge = web.Application(client_max_size=1024 * 1024)
    bridge.router.add_route("*", "/webchat/v1/{path:.*}", fake.handle)
    server = await aiohttp_server(bridge)
    (tmp_path / "index.html").write_text("<html>SPA</html>")
    (tmp_path / "starflow").mkdir()
    (tmp_path / "starflow/base-stars.html").write_text("<html>Stars</html>")
    app = create_app(
        bridge_url=str(server.make_url("")),
        bridge_token="fake-bridge-token",
        public_origin=ORIGIN,
        static_dir=tmp_path,
    )
    client = await aiohttp_client(app)
    await asyncio.wait_for(fake.connected.wait(), 2)
    try:
        yield client, fake
    finally:
        await client.close()
        await server.close()


def cookie_header(response, name):
    return f"{name}={response.cookies[name].value}"


async def password(client):
    response = await client.post(
        "/api/auth/password",
        json={"password": "good-password"},
        headers={**CLIENT, "X-Real-IP": "198.51.100.7"},
    )
    assert response.status == 200
    return response


async def login(client):
    pre = await password(client)
    response = await client.post(
        "/api/auth/totp",
        json={"code": "123456"},
        headers={**CLIENT, "Cookie": cookie_header(pre, "dtw_pre")},
    )
    assert response.status == 200
    headers = {**CLIENT, "Cookie": cookie_header(response, "dtw_session")}
    state = await (await client.get("/api/auth/state", headers=headers)).json()
    headers["X-Dtw-Csrf"] = state["csrf"]
    return headers, response


def assert_cookie(cookie, path):
    assert cookie["httponly"]
    assert cookie["secure"]
    assert cookie["samesite"] == "Strict"
    assert cookie["path"] == path


async def test_public_health_static_and_security_headers(site):
    client, _ = site
    for path in ["/", "/api/health", "/static/starflow/base-stars.html", "/missing"]:
        response = await client.get(path)
        assert response.headers["Content-Security-Policy"] == (
            STARFLOW_CSP if "base-stars" in path else CSP
        )
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Referrer-Policy"] == "no-referrer"
        assert "WWW-Authenticate" not in response.headers
    response = await client.get("/api/health")
    assert await response.json() == {"ok": True}
    assert response.headers["Cache-Control"] == "no-store"
    assert await (await client.get("/api/auth/state")).json() == {
        "authenticated": False,
        "csrf": None,
        "totp_configured": True,
        "bridge_up": True,
    }


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/topics"),
        ("GET", "/api/state"),
        ("GET", "/api/files"),
        ("GET", "/api/outbox"),
        ("GET", "/api/search?q=x"),
        ("GET", "/api/ws"),
        ("POST", "/api/topics"),
        ("PUT", "/api/files/PERSONA.md"),
    ],
)
async def test_unauthenticated_is_forbidden(site, method, path):
    client, _ = site
    response = await client.request(method, path, headers=CLIENT)
    assert response.status == 403
    assert await response.json() == {"error": "auth_required"}
    assert "WWW-Authenticate" not in response.headers


async def test_client_header_and_invalid_credentials(site):
    client, fake = site
    before = len(fake.calls)
    response = await client.post("/api/auth/password", json={"password": "good-password"})
    assert response.status == 403
    assert len(fake.calls) == before
    for value, expected in [
        ("bad", {"error": "invalid"}),
        ("locked", {"error": "locked", "retry_after": 12}),
    ]:
        response = await client.post("/api/auth/password", json={"password": value}, headers=CLIENT)
        assert response.status == 403
        assert await response.json() == expected
        assert not response.cookies
    for path in ["totp", "setup/begin", "setup/confirm"]:
        response = await client.post("/api/auth/" + path, json={}, headers=CLIENT)
        assert response.status == 403


async def test_password_totp_cookies_csrf_and_logout(site):
    client, fake = site
    pre = await password(client)
    assert await pre.json() == {"next": "totp"}
    assert "fake-preauth" not in await pre.text()
    assert_cookie(pre.cookies["dtw_pre"], "/api/auth")
    assert pre.cookies["dtw_pre"]["max-age"] == "300"
    assert fake.calls[-1][3]["client_ip"] == "198.51.100.7"
    headers, response = await login(client)
    assert_cookie(response.cookies["dtw_session"], "/")
    assert response.cookies["dtw_session"]["max-age"] == ""
    assert response.cookies["dtw_pre"]["max-age"] == "0"
    assert_cookie(response.cookies["dtw_pre"], "/api/auth")
    assert len(response.cookies["dtw_session"].value) >= 43
    for csrf in [None, "wrong", "non-ascii-✓"]:
        bad = {k: v for k, v in headers.items() if k != "X-Dtw-Csrf"}
        if csrf:
            bad["X-Dtw-Csrf"] = csrf
        denied = await client.post("/api/topics", json={"name": "test"}, headers=bad)
        assert denied.status == 403
    state = await (await client.get("/api/state", headers=headers)).json()
    assert state == {
        "telegram": {"state": "down"},
        "outbox": {"pending": 2},
        "bot": {"up": True},
        "web_sessions": 1,
    }
    response = await client.post("/api/auth/logout", headers=headers)
    assert response.status == 200
    assert response.cookies["dtw_session"]["max-age"] == "0"
    assert_cookie(response.cookies["dtw_session"], "/")
    assert (await client.get("/api/topics", headers=headers)).status == 403


async def test_setup_new_paste_confirm_returns_to_password(site):
    client, fake = site
    fake.configured = False
    pre = await password(client)
    assert await pre.json() == {"next": "setup"}
    headers = {**CLIENT, "Cookie": cookie_header(pre, "dtw_pre")}
    new = await client.post("/api/auth/setup/begin", json={"mode": "new"}, headers=headers)
    data = await new.json()
    assert set(data) == {"secret_display", "qr_svg"}
    assert data["secret_display"] == "JBSW Y3DP EHPK 3PXP"
    assert "<svg" in data["qr_svg"]
    assert "<script" not in data["qr_svg"]
    assert fake.calls[-1][3] == {"preauth": "fake-preauth", "mode": "new"}
    paste = await client.post(
        "/api/auth/setup/begin", json={"mode": "paste", "secret": "existing"}, headers=headers
    )
    assert await paste.json() == {"secret_display": data["secret_display"]}
    assert fake.calls[-1][3] == {"preauth": "fake-preauth", "mode": "paste", "secret": "existing"}
    invalid = await client.post("/api/auth/setup/confirm", json={"code": "bad"}, headers=headers)
    assert invalid.status == 403
    confirmed = await client.post(
        "/api/auth/setup/confirm", json={"code": "123456"}, headers=headers
    )
    assert await confirmed.json() == {"ok": True}
    assert confirmed.cookies["dtw_pre"]["max-age"] == "0"
    assert "dtw_session" not in confirmed.cookies
    configured = await client.post("/api/auth/setup/begin", json={"mode": "new"}, headers=headers)
    assert configured.status == 403
    assert await configured.json() == {"error": "totp_configured"}


@pytest.mark.parametrize(
    ("method", "path", "body", "status"),
    [
        ("GET", "/api/topics", None, 200),
        ("POST", "/api/topics", {"name": "new"}, 200),
        ("PATCH", "/api/topics/-123.4", {"name": "renamed"}, 200),
        ("DELETE", "/api/topics/-123.4", {"confirm_name": "renamed"}, 200),
        ("GET", "/api/topics/-123.4/messages?before=9&limit=20", None, 200),
        ("POST", "/api/topics/-123.4/messages", {"text": "hello", "client_id": "c1"}, 202),
        ("POST", "/api/topics/-123.4/abort", None, 200),
        ("GET", "/api/search?q=a%26b%20%23c", None, 200),
        ("GET", "/api/outbox", None, 200),
        ("GET", "/api/files", None, 200),
        ("GET", "/api/files/MAINMEMORY.md", None, 200),
        ("PUT", "/api/files/PERSONA.md", {"content": "edited", "base_sha256": "base"}, 409),
    ],
)
async def test_relay_preserves_request_and_status(site, method, path, body, status):
    client, fake = site
    headers, _ = await login(client)
    raw = json.dumps(body, indent=2).encode() if body else None
    response = await client.request(method, path, data=raw, headers=headers)
    assert response.status == status
    call = fake.calls[-1]
    assert call[0] == method
    assert call[1] == path.split("?")[0].removeprefix("/api")
    assert call[3] == body
    assert call[4] == (raw or b"")
    if "/search" in path:
        assert call[2] == {"q": "a&b #c"}
    if status == 409:
        assert await response.json() == {"error": "conflict"}


async def test_bridge_down_health_auth_state_and_i18n_fallback(site):
    client, fake = site
    headers, _ = await login(client)
    fake.unavailable = True
    response = await client.get("/api/topics", headers=headers)
    assert response.status == 503
    assert await response.json() == {"error": "bot_unreachable"}
    assert (await client.get("/api/health")).status == 200
    state = await (await client.get("/api/auth/state", headers=headers)).json()
    assert state["bridge_up"] is False
    assert state["authenticated"] is True
    data = await (await client.get("/api/i18n")).json()
    assert data == {"lang": "en", "strings": load_web_strings("en")}


@pytest.mark.parametrize("language", ["ko", "en", "unsupported"])
async def test_i18n_language_and_key_parity(site, language):
    client, fake = site
    fake.language = language
    data = await (await client.get("/api/i18n")).json()
    assert data["lang"] == (language if language != "unsupported" else "en")
    assert data["strings"] == load_web_strings(data["lang"])
    assert _compare_domain(load_web_strings("en"), load_web_strings("ko")).clean


async def test_body_limit_and_malformed_auth_json(site):
    client, fake = site
    headers, _ = await login(client)
    before = len(fake.calls)
    for path in [
        "/api/auth/password",
        "/api/topics/-123.4/messages",
        "/api/auth/logout",
        "/api/health",
    ]:
        response = await client.request(
            "GET" if path.endswith("health") else "POST",
            path,
            data=b"x" * (512 * 1024 + 1),
            headers=headers,
        )
        assert response.status == 413
        assert await response.json() == {"error": "too_large"}
    assert len(fake.calls) == before
    for body in [b"not-json", b"[]", b"null"]:
        response = await client.post("/api/auth/password", data=body, headers=CLIENT)
        assert response.status == 400


@pytest.mark.parametrize("expiry", ["idle", "absolute"])
async def test_expired_session_rejected(site, expiry):
    client, _ = site
    headers, response = await login(client)
    session = client.app[STATE].sessions.get(response.cookies["dtw_session"].value)
    if expiry == "idle":
        session.last_seen -= IDLE_TTL
    else:
        session.created -= ABSOLUTE_TTL
    response = await client.get("/api/topics", headers=headers)
    assert response.status == 403
    state = await (await client.get("/api/auth/state", headers=headers)).json()
    assert state["authenticated"] is False
    assert state["csrf"] is None


async def test_ws_origin_session_fanout_frames_and_logout(site):
    client, fake = site
    headers, _ = await login(client)
    for origin in [None, "http://chat.example.com"]:
        ws_headers = {**headers}
        if origin:
            ws_headers["Origin"] = origin
        with pytest.raises(aiohttp.WSServerHandshakeError) as exc:
            await client.ws_connect("/api/ws", headers=ws_headers)
        assert exc.value.status == 403
    one = await client.ws_connect("/api/ws", headers={**headers, "Origin": ORIGIN})
    two = await client.ws_connect("/api/ws", headers={**headers, "Origin": ORIGIN})
    assert await one.receive_json(timeout=2) == {"type": "bridge", "up": True}
    assert await two.receive_json(timeout=2) == {"type": "bridge", "up": True}
    assert fake.connections == 1
    await one.send_json({"type": "ignored"})
    frame = '{"type":"delta","tid":"-123.4","text":"hello"}'
    await fake.send(frame)
    assert (await one.receive(timeout=2)).data == frame
    assert (await two.receive(timeout=2)).data == frame
    response = await client.post("/api/auth/logout", headers=headers)
    assert response.status == 200
    assert (await one.receive(timeout=2)).type == aiohttp.WSMsgType.CLOSE
    assert (await two.receive(timeout=2)).type == aiohttp.WSMsgType.CLOSE


async def test_ws_direct_nonstandard_https_port_requires_exact_origin(site):
    from ductor_bot.config import WebchatConfig
    from scripts.webchat import compose_settings

    client, _ = site
    settings = compose_settings(
        WebchatConfig(domain="chat.example.com", exposure="direct", https_port=8443).model_dump()
    )
    client.app[STATE].public_origin = settings.get("DTW_PUBLIC_ORIGIN", ORIGIN)
    headers, _ = await login(client)
    socket = await client.ws_connect(
        "/api/ws", headers={**headers, "Origin": "https://chat.example.com:8443"}
    )
    assert await socket.receive_json(timeout=2) == {"type": "bridge", "up": True}
    await socket.close()
    for origin in (ORIGIN, "https://chat.example.com:9443"):
        with pytest.raises(aiohttp.WSServerHandshakeError) as exc:
            await client.ws_connect("/api/ws", headers={**headers, "Origin": origin})
        assert exc.value.status == 403


async def test_ws_bridge_disconnect_and_reconnect(site):
    client, fake = site
    headers, _ = await login(client)
    socket = await client.ws_connect("/api/ws", headers={**headers, "Origin": ORIGIN})
    await socket.receive_json(timeout=2)
    for bridge_socket in list(fake.sockets):
        await bridge_socket.close()
    assert await socket.receive_json(timeout=2) == {"type": "bridge", "up": False}
    response = await client.get("/api/state?refresh=1", headers=headers)
    assert (await response.json())["bot"] == {"up": True}
    other = await client.ws_connect("/api/ws", headers={**headers, "Origin": ORIGIN})
    assert await other.receive_json(timeout=2) == {"type": "bridge", "up": False}
    assert await socket.receive_json(timeout=4) == {"type": "bridge", "up": True}
    assert await other.receive_json(timeout=2) == {"type": "bridge", "up": True}
    await other.close()
    assert fake.connections == 2
    await socket.close()


def test_session_activity_never_extends_absolute_lifetime():
    clock = 100.0
    with patch("ductor_bot.webchat.web.sessions.time.monotonic", side_effect=lambda: clock):
        store = SessionStore()
        token = store.create()
        for _ in range(12):
            clock += 3600
            session = store.get(token)
        assert session is None
        assert store.count() == 0


async def test_bridge_http_timeout_is_five_seconds(site):
    client, fake = site
    headers, _ = await login(client)
    assert client.app[STATE].bridge._client.timeout.total == 5
    fake.delay = 6
    response = await client.get("/api/topics", headers=headers)
    assert response.status == 503
    assert await response.json() == {"error": "bot_unreachable"}


async def test_session_idle_expiry_closes_websocket(site):
    client, fake = site
    headers, response = await login(client)
    token = response.cookies["dtw_session"].value
    session = client.app[STATE].sessions.get(token)
    socket = await client.ws_connect("/api/ws", headers={**headers, "Origin": ORIGIN})
    await socket.receive_json(timeout=2)
    session.last_seen -= IDLE_TTL
    await fake.send('{"type":"topics"}')
    assert (await socket.receive(timeout=2)).type == aiohttp.WSMsgType.CLOSE


def test_web_strings_fallback_and_locale_path_safety():
    english = load_web_strings("en")
    assert load_web_strings("unsupported") == english
    assert load_web_strings("../ko") == english
    assert set(load_web_strings("ko")) == set(english)


async def test_chunked_body_limit(site):
    client, _ = site
    headers, _ = await login(client)

    async def chunks():
        for size in [256 * 1024, 256 * 1024, 1]:
            yield b"x" * size

    response = await client.post("/api/auth/logout", data=chunks(), headers=headers)
    assert response.status == 413


@pytest.mark.parametrize("path", ["/static/%00", "/static/" + "x" * 300])
async def test_review_bad_static_paths_are_4xx(site, path):
    client, _ = site
    response = await client.get(path)
    assert response.status in {400, 404}
    assert "Traceback" not in await response.text()


async def test_review_unknown_charset_is_400(site):
    client, _ = site
    response = await client.post(
        "/api/auth/password",
        data=b"{}",
        headers={**CLIENT, "Content-Type": "application/json; charset=unknown-encoding"},
    )
    assert response.status == 400
    assert "Traceback" not in await response.text()


@pytest.mark.parametrize(
    ("method", "path", "status", "error"),
    [
        ("POST", "/api/topics", 503, "telegram_unavailable"),
        ("PATCH", "/api/topics/-123.4", 503, "telegram_unavailable"),
        ("DELETE", "/api/topics/-123.4", 409, "topic_busy"),
    ],
)
async def test_review_topic_mutations_preserve_slow_bridge_errors(
    site, method, path, status, error
):
    client, fake = site
    headers, _ = await login(client)
    original = fake.handle

    async def slow(request):
        if request.path.removeprefix("/webchat/v1") == path.removeprefix("/api"):
            await asyncio.sleep(6)
            return web.json_response({"error": error}, status=status)
        return await original(request)

    bridge = client.app[STATE].bridge
    from aiohttp.test_utils import TestServer

    upstream = web.Application()
    upstream.router.add_route("*", "/webchat/v1/{path:.*}", slow)
    async with TestServer(upstream) as server:
        bridge._url = str(server.make_url("/webchat/v1"))
        response = await client.request(method, path, json={"name": "test"}, headers=headers)
        assert response.status == status
        assert await response.json() == {"error": error}


async def test_review_logout_works_without_bridge(site):
    client, fake = site
    headers, response = await login(client)
    token = response.cookies["dtw_session"].value
    fake.unavailable = True
    response = await client.post("/api/auth/logout", headers=headers)
    assert response.status == 200
    assert response.cookies["dtw_session"]["max-age"] == "0"
    assert response.cookies["dtw_pre"]["max-age"] == "0"
    assert client.app[STATE].sessions.get(token) is None


async def test_review_ws_validation_cached_per_session_and_expires(site):
    client, fake = site
    headers, _ = await login(client)
    with patch("ductor_bot.webchat.web.app.time", create=True) as clock:
        clock.monotonic.return_value = 100
        one = await client.ws_connect("/api/ws", headers={**headers, "Origin": ORIGIN})
        two = await client.ws_connect("/api/ws", headers={**headers, "Origin": ORIGIN})
        await one.receive_json(timeout=2)
        await two.receive_json(timeout=2)
        before = len([call for call in fake.calls if call[1] == "/auth/state"])
        clock.monotonic.return_value = 109.99
        for _ in range(50):
            await fake.send('{"type":"delta","text":"x"}')
            assert (await one.receive_json(timeout=2))["type"] == "delta"
            assert (await two.receive_json(timeout=2))["type"] == "delta"
        assert len([call for call in fake.calls if call[1] == "/auth/state"]) == before
        clock.monotonic.return_value = 110
        await fake.send('{"type":"topics"}')
        await one.receive_json(timeout=2)
        await two.receive_json(timeout=2)
        assert len([call for call in fake.calls if call[1] == "/auth/state"]) == before + 1
        fake.generation = "revoked-generation"
        clock.monotonic.return_value = 120
        await fake.send('{"type":"topics"}')
        assert (await one.receive(timeout=2)).type == aiohttp.WSMsgType.CLOSE
        assert (await two.receive(timeout=2)).type == aiohttp.WSMsgType.CLOSE
    await one.close()
    await two.close()


async def test_review_ws_validation_unavailable_closes_instead_of_losing_frame(site):
    client, fake = site
    headers, _ = await login(client)
    socket = await client.ws_connect("/api/ws", headers={**headers, "Origin": ORIGIN})
    await socket.receive_json(timeout=2)
    fake.unavailable = True
    with patch("ductor_bot.webchat.web.app.time", create=True) as clock:
        clock.monotonic.return_value = float("inf")
        await fake.send('{"type":"delta","text":"must resync"}')
        assert (await socket.receive(timeout=2)).type == aiohttp.WSMsgType.CLOSE
    await socket.close()


async def test_review_ws_offline_connect_then_recovery_rejects_revoked_cookie(site):
    client, fake = site
    headers, _ = await login(client)
    fake.unavailable = True
    socket = await client.ws_connect("/api/ws", headers={**headers, "Origin": ORIGIN})
    assert await socket.receive_json(timeout=2) == {"type": "bridge", "up": False}
    fake.unavailable = False
    fake.generation = "revoked-generation"
    await client.app[STATE].publish('{"type":"bridge","up":true}')
    assert (await socket.receive(timeout=2)).type == aiohttp.WSMsgType.CLOSE
    assert client.app[STATE].sessions.count() == 0
    await socket.close()


@pytest.mark.parametrize("endpoint", ["abort", "messages"])
async def test_review_abort_waits_for_slow_delivery_lock(site, endpoint):
    client, fake = site
    headers, _ = await login(client)
    original_handle = fake.handle

    async def slow(request):
        if request.path.endswith(f"/{endpoint}"):
            if endpoint == "messages":
                assert await request.json() == {"text": " /StOp ", "client_id": "stop-request"}
            await asyncio.sleep(7)
            return web.json_response({"killed": 1}, status=202 if endpoint == "messages" else 200)
        return await original_handle(request)

    from aiohttp.test_utils import TestServer

    upstream = web.Application()
    upstream.router.add_route("*", "/webchat/v1/{path:.*}", slow)
    async with TestServer(upstream) as server:
        client.app[STATE].bridge._url = str(server.make_url("/webchat/v1"))
        response = await client.post(
            f"/api/topics/123.4/{endpoint}",
            headers=headers,
            json={"text": " /StOp ", "client_id": "stop-request"}
            if endpoint == "messages"
            else None,
        )
        assert response.status == (202 if endpoint == "messages" else 200)
        assert await response.json() == {"killed": 1}
