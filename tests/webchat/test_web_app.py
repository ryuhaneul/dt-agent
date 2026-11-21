"""Browser API contracts exercised against a real local fake bridge."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

import aiohttp
import pytest
from aiohttp import web

from ductor_bot.i18n.check import _compare_domain
from ductor_bot.i18n.loader import _load_toml, load_web_strings
from ductor_bot.infra.version import get_current_version
from ductor_bot.webchat.web.app import CSP, STARFLOW_CSP, STATE, create_app
from ductor_bot.webchat.web.sessions import ABSOLUTE_TTL, IDLE_TTL, SessionStore

I18N_ROOT = Path(__file__).resolve().parents[2] / "ductor_bot/i18n"
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
    delay_path: str | None = None
    claude_state: dict | None = None

    owner_headers: list = field(default_factory=list)
    owner_by_browser: dict = field(default_factory=dict)
    login_jobs: dict = field(default_factory=dict)
    login_cancelled: set = field(default_factory=set)

    async def handle(self, request):  # noqa: C901, PLR0912 - explicit fake API contract
        assert request.headers.get("X-Dtw-Bridge-Token") == "fake-bridge-token"
        assert "Cookie" not in request.headers
        assert "X-Dtw-Csrf" not in request.headers
        if self.unavailable:
            request.transport.close()
            return web.Response()
        path = request.path.removeprefix("/webchat/v1")
        self.owner_headers.append((path, request.headers.get("X-Dtw-Owner")))
        if "browser" in request.query:
            self.owner_by_browser[request.query["browser"]] = request.headers.get("X-Dtw-Owner")
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
        if self.delay and (self.delay_path is None or path == self.delay_path):
            await asyncio.sleep(self.delay)
        raw = await request.read()
        body = json.loads(raw) if raw else None
        self.calls.append((request.method, path, dict(request.query), body, raw))
        if path.startswith("/cli-login/"):
            owner = request.headers.get("X-Dtw-Owner")
            assert owner
            await asyncio.sleep(0.01)  # overlap concurrent requests from two browser sessions
            active = next(
                (
                    (job, account)
                    for job, account in self.login_jobs.items()
                    if job not in self.login_cancelled
                ),
                None,
            )
            if path == "/cli-login/cancel-owner":
                self.login_cancelled.update(
                    job for job, account in self.login_jobs.items() if account == owner
                )
                return web.json_response({"ok": True}, status=202)
            if path == "/cli-login/status":
                live = json.loads(request.headers.get("X-Dtw-Active-Owners", "null"))
                if active and live is not None and active[1] not in live:
                    self.login_jobs[active[0]] = owner
                    active = (active[0], owner)
                return web.json_response(
                    {
                        "providers": dict.fromkeys(("claude", "codex", "grok"), "installed"),
                        "claude_slots": [{"name": "claude", "priority": 1, "authenticated": False}],
                        "running": active is not None,
                        "provider": "codex" if active else None,
                        "job_id": active[0] if active and active[1] == owner else None,
                    }
                )
            if request.method == "POST" and path.startswith("/cli-login/slots/"):
                if path.endswith(("/logout", "/disconnect")):
                    return web.json_response({"ok": True})
            if path == "/cli-login/jobs":
                if body.get("provider") not in ("claude", "codex", "grok"):
                    return web.json_response({"error": "invalid_login"}, status=400)
                if active:
                    return web.json_response({"error": "login_running"}, status=409)
                job = "job-" + str(len(self.login_jobs) + 1)
                self.login_jobs[job] = owner
                return web.json_response({"job_id": job}, status=202)
            job = path.split("/")[3]
            if self.login_jobs.get(job) != owner:
                return web.json_response({"error": "login_not_found"}, status=404)
            if path.endswith("/input"):
                if body.get("kind") != "line" or not body.get("value"):
                    return web.json_response({"error": "invalid_input"}, status=400)
                return web.json_response({"ok": True})
            if path.endswith("/cancel"):
                self.login_cancelled.add(job)
                return web.json_response({"ok": True}, status=202)
            return web.json_response(
                {
                    "job_id": job,
                    "provider": "codex",
                    "slot": None,
                    "state": "cancelled" if job in self.login_cancelled else "running",
                    "reason": None,
                    "seq": 1,
                    "output": []
                    if request.query.get("after") == "1"
                    else [{"seq": 1, "text": "relay-output-canary"}],
                    "urls": ["https://fake.invalid/login"],
                    "code": "FAKE-CODE",
                    "needs_input": True,
                    "restart_required": False,
                }
            )
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
        if path == "/auth/password-change":
            if body["current_password"] != "good-password":
                return web.json_response({"error": "invalid"}, status=403)
            self.generation = "changed-generation"
            return web.json_response({"ok": True, "credential_generation": self.generation})
        if path == "/auth/logout":
            return web.json_response({"ok": True})
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
            data = {"telegram": {"state": "down"}, "outbox": {"pending": 2}}
            if self.claude_state is not None:
                data["claude"] = self.claude_state
            return web.json_response(data)
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
        "version": get_current_version(),
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
        ("PUT", "/api/topics/-123.4/seen", {"seq": -5}, 200),
        ("POST", "/api/topics/seen-all", None, 200),
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


@pytest.mark.parametrize("language", ["en", "de", "nl", "es", "fr", "id", "pt", "ru", "ko"])
async def test_i18n_explicit_web_language_is_public_and_does_not_change_bot(site, language):
    client, fake = site
    response = await client.get("/api/i18n", params={"lang": language})
    assert response.status == 200
    assert "WWW-Authenticate" not in response.headers
    assert await response.json() == {"lang": language, "strings": load_web_strings(language)}
    data = (await response.json())["strings"]
    expected = {
        "stream.label_thinking",
        "stream.label_compacting",
        "stream.label_recovering",
        "stream.label_timeout_warning",
        "stream.label_timeout_extended",
        "stream.done",
    }
    chat = _load_toml(I18N_ROOT / language / "chat.toml")
    web_keys = set(
        _load_toml(I18N_ROOT / language / "web.toml")
    )
    assert set(data) == web_keys | expected
    assert {key: data[key] for key in expected} == {key: chat[key] for key in expected}
    assert fake.language == "ko"
    assert (await (await client.get("/api/i18n")).json())["lang"] == "ko"


@pytest.mark.parametrize("language", ["", "invalid", "EN", "../en", "en-US", "ja"])
async def test_i18n_invalid_query_falls_back_to_bot_language(site, language):
    client, fake = site
    fake.language = "fr"
    response = await client.get("/api/i18n", params={"lang": language})
    assert await response.json() == {"lang": "fr", "strings": load_web_strings("fr")}


async def test_i18n_explicit_language_works_with_bridge_down(site):
    client, fake = site
    fake.unavailable = True
    response = await client.get("/api/i18n?lang=de")
    assert await response.json() == {"lang": "de", "strings": load_web_strings("de")}


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


async def test_version_is_in_authenticated_state_only(site, monkeypatch):
    client, bridge = site
    bridge.claude_state = {"next": "claude", "slots": [{"name": "claude", "kind": "oauth"}]}
    monkeypatch.setattr("ductor_bot.webchat.web.app.get_current_version", lambda: "9.8.7")
    response = await client.get("/api/state", headers=CLIENT)
    assert response.status == 403
    assert (await response.json()).keys().isdisjoint({"version", "claude"})
    for endpoint in ("/api/auth/state", "/api/i18n"):
        response = await client.get(endpoint, headers=CLIENT)
        assert response.status == 200
        assert "version" not in await response.json()

    headers, _ = await login(client)
    response = await client.get("/api/state", headers=headers)
    assert response.status == 200
    state = await response.json()
    assert state["version"] == "9.8.7"
    assert state["claude"] == bridge.claude_state


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


async def test_password_change_revokes_sessions_sockets_and_issues_new_csrf(site):
    client, fake = site
    first, _ = await login(client)
    second, _ = await login(client)
    sockets = [
        await client.ws_connect("/api/ws", headers={**headers, "Origin": ORIGIN})
        for headers in (first, second)
    ]
    for socket in sockets:
        await socket.receive_json(timeout=2)
    response = await client.post(
        "/api/auth/password-change",
        headers={**first, "X-Real-IP": "198.51.100.9"},
        json={
            "current_password": "good-password",
            "new_password": "changed-password-123",
            "code": "123456",
            "client_ip": "forged",
        },
    )
    assert response.status == 200
    data = await response.json()
    assert data["ok"] is True
    assert data["csrf"] != first["X-Dtw-Csrf"]
    assert_cookie(response.cookies["dtw_session"], "/")
    new_headers = {
        **CLIENT,
        "Cookie": cookie_header(response, "dtw_session"),
        "X-Dtw-Csrf": data["csrf"],
    }
    for socket in sockets:
        assert (await socket.receive(timeout=2)).type == aiohttp.WSMsgType.CLOSE
        await socket.close()
    state = client.app[STATE]
    assert state.sessions.count() == 1
    assert not state.validated
    assert not state.sockets
    for headers in (first, second):
        denied = await client.get("/api/topics", headers=headers)
        assert denied.status == 403
        assert "WWW-Authenticate" not in denied.headers
    created = await client.post("/api/topics", headers=new_headers, json={"name": "New topic"})
    assert created.status == 200
    assert await created.json() == {"path": "/topics", "body": {"name": "New topic"}}
    payload = next(call[3] for call in fake.calls if call[1] == "/auth/password-change")
    assert payload == {
        "current_password": "good-password",
        "new_password": "changed-password-123",
        "code": "123456",
        "client_ip": "198.51.100.9",
    }
    socket = await client.ws_connect("/api/ws", headers={**new_headers, "Origin": ORIGIN})
    assert await socket.receive_json(timeout=2) == {"type": "bridge", "up": True}
    await socket.close()


async def test_password_change_denials_preserve_session(site):
    client, fake = site
    headers, _ = await login(client)
    for request_headers in (
        CLIENT,
        {key: value for key, value in headers.items() if key != "X-Dtw-Csrf"},
        {key: value for key, value in headers.items() if key != "X-Dtw-Client"},
    ):
        response = await client.post("/api/auth/password-change", headers=request_headers, json={})
        assert response.status == 403
        assert "WWW-Authenticate" not in response.headers
    assert not any(call[1] == "/auth/password-change" for call in fake.calls)
    response = await client.post(
        "/api/auth/password-change", headers=headers, json={"current_password": "wrong"}
    )
    assert response.status == 403
    assert await response.json() == {"error": "invalid"}
    assert "WWW-Authenticate" not in response.headers
    assert client.app[STATE].sessions.count() == 1
    assert (await client.get("/api/topics", headers=headers)).status == 200


@pytest.mark.parametrize(
    ("path", "error"),
    [
        ("/api/topics", "history_busy"),
        ("/api/topics/-123.7/messages", "history_busy"),
        ("/api/state", "bot_unreachable"),
    ],
)
async def test_history_reads_preserve_slow_busy_errors_only(site, path, error):
    from aiohttp.test_utils import TestServer

    client, fake = site
    headers, _ = await login(client)

    async def busy(request):
        if request.path.removeprefix("/webchat/v1") == path.removeprefix("/api"):
            await asyncio.sleep(6)
            return web.json_response({"error": "history_busy"}, status=503)
        return await fake.handle(request)

    upstream = web.Application()
    upstream.router.add_get("/webchat/v1/{path:.*}", busy)
    async with TestServer(upstream) as server:
        client.app[STATE].bridge._url = str(server.make_url("/webchat/v1"))
        response = await client.get(path, headers=headers)
        assert response.status == 503
        assert await response.json() == {"error": error}


async def test_buttons_relay_requires_session_client_header_and_csrf(site):
    client, fake = site
    path = "/api/topics/1.7/buttons"
    for request_headers in [{}, CLIENT]:
        response = await client.post(
            path, json={"entry_id": "entry", "label": "Yes"}, headers=request_headers
        )
        assert response.status == 403
        assert "WWW-Authenticate" not in response.headers
    headers, _ = await login(client)
    for request_headers in [
        {key: value for key, value in headers.items() if key != "X-Dtw-Csrf"},
        {**headers, "X-Dtw-Csrf": "bad"},
        {key: value for key, value in headers.items() if key != "X-Dtw-Client"},
    ]:
        response = await client.post(
            path, json={"entry_id": "entry", "label": "Yes"}, headers=request_headers
        )
        assert response.status == 403
    response = await client.post(path, json={"entry_id": "entry", "label": "Yes"}, headers=headers)
    assert response.status == 200
    assert (await response.json())["body"] == {"entry_id": "entry", "label": "Yes"}
    assert any(call[1] == "/topics/1.7/buttons" for call in fake.calls)


async def test_selector_sync_relay_auth_csrf_and_exact_payload(site):
    client, fake = site
    path = "/api/topics/1.7/callbacks"
    body = {"entry_id": "e1", "data": "ms:p:claude", "revision": 0}
    response = await client.post(path, json=body)
    assert response.status == 403
    assert "WWW-Authenticate" not in response.headers
    authorized, _ = await login(client)
    for missing in ("X-Dtw-Client", "X-Dtw-Csrf"):
        response = await client.post(
            path, json=body, headers={k: v for k, v in authorized.items() if k != missing}
        )
        assert response.status == 403
        assert "WWW-Authenticate" not in response.headers
    response = await client.post(path, json=body, headers=authorized)
    assert response.status == 200
    assert fake.calls[-1][1] == "/topics/1.7/callbacks"
    assert fake.calls[-1][3] == body


@pytest.mark.parametrize(
    ("route", "body"),
    [
        ("buttons", {"entry_id": "e1", "label": "Yes"}),
        ("callbacks", {"entry_id": "e1", "data": "ms:p:claude", "revision": 0}),
    ],
)
async def test_button_relays_allow_six_second_bridge_response(site, route, body):
    client, fake = site
    authorized, _ = await login(client)
    fake.delay = 6
    fake.delay_path = f"/topics/1.7/{route}"
    response = await client.post(f"/api/topics/1.7/{route}", json=body, headers=authorized)
    assert response.status == 200
    assert await response.json() == {"path": f"/topics/1.7/{route}", "body": body}
    assert fake.calls[-1][1] == f"/topics/1.7/{route}"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/cli-login/status"),
        ("GET", "/api/cli-login/jobs/job-1"),
        ("POST", "/api/cli-login/jobs"),
        ("POST", "/api/cli-login/jobs/job-1/input"),
        ("POST", "/api/cli-login/jobs/job-1/cancel"),
    ],
)
async def test_cli_login_requires_browser_session(site, method, path):
    client, _ = site
    response = await client.request(method, path, headers=CLIENT, json={})
    assert response.status == 403
    assert "WWW-Authenticate" not in response.headers


@pytest.mark.parametrize(
    "path",
    ["/api/cli-login/jobs", "/api/cli-login/jobs/job-1/input", "/api/cli-login/jobs/job-1/cancel"],
)
@pytest.mark.parametrize("missing", ["X-Dtw-Client", "X-Dtw-Csrf"])
async def test_cli_login_mutations_keep_existing_csrf_and_client_checks(site, path, missing):
    client, fake = site
    authorized, _ = await login(client)
    forbidden = {key: value for key, value in authorized.items() if key != missing}
    response = await client.post(path, headers=forbidden, json={"provider": "codex"})
    assert response.status == 403
    assert "WWW-Authenticate" not in response.headers
    assert not any(call[1].startswith("/cli-login/") for call in fake.calls)


async def test_cli_login_relay_contract_and_owner_isolation(site, caplog):
    client, fake = site
    caplog.set_level(logging.DEBUG)
    first, first_response = await login(client)
    second, second_response = await login(client)
    original_headers = client.app[STATE].bridge._headers.copy()
    # GET follows the normal session-only contract; supplied owner is never trusted.
    only_cookie = {"Cookie": first["Cookie"], "X-Dtw-Owner": "forged-owner"}
    response = await client.get("/api/cli-login/status", headers=only_cookie)
    assert response.status == 200
    assert (await response.json())["claude_slots"] == [
        {"name": "claude", "priority": 1, "authenticated": False}
    ]
    created = await client.post("/api/cli-login/jobs", headers=first, json={"provider": "codex"})
    assert created.status == 202
    job = (await created.json())["job_id"]
    own = await client.get(f"/api/cli-login/jobs/{job}?after=1", headers=only_cookie)
    assert own.status == 200
    assert (await own.json())["output"] == []
    for method, suffix, body in (
        ("GET", "", None),
        ("POST", "/input", {"kind": "line", "value": "relay-input-canary"}),
        ("POST", "/cancel", None),
    ):
        denied = await client.request(
            method, f"/api/cli-login/jobs/{job}{suffix}", headers=second, json=body
        )
        assert denied.status == 404
        assert await denied.json() == {"error": "login_not_found"}
    for supplied, expected_job in ((first, job), (second, None)):
        response = await client.get("/api/cli-login/status", headers=supplied)
        assert (await response.json())["job_id"] == expected_job
    sent = await client.post(
        f"/api/cli-login/jobs/{job}/input",
        headers=first,
        json={"kind": "line", "value": "relay-input-canary"},
    )
    assert sent.status == 200
    conflict = await client.post("/api/cli-login/jobs", headers=second, json={"provider": "grok"})
    assert conflict.status == 409
    canceled = await client.post(f"/api/cli-login/jobs/{job}/cancel", headers=first)
    assert canceled.status == 202
    assert (await (await client.get(f"/api/cli-login/jobs/{job}", headers=first)).json())[
        "state"
    ] == "cancelled"
    invalid = await client.post("/api/cli-login/jobs", headers=first, json={"provider": "invalid"})
    assert invalid.status == 400
    assert "relay-input-canary" not in caplog.text
    assert "relay-output-canary" not in caplog.text
    assert client.app[STATE].bridge._headers == original_headers
    key = client.app[STATE].owner_key
    owners = [
        hmac.digest(key, response.cookies["dtw_session"].value.encode(), "sha256").hex()
        for response in (first_response, second_response)
    ]
    assert owners[0] != owners[1]
    assert {owner for path, owner in fake.owner_headers if path.startswith("/cli-login/")} == set(
        owners
    )
    assert all(
        owner is None for path, owner in fake.owner_headers if not path.startswith("/cli-login/")
    )


async def test_two_sessions_concurrently_keep_per_request_owner_and_legacy_headers(site):
    client, fake = site
    first, one = await login(client)
    second, two = await login(client)
    key = client.app[STATE].owner_key
    expected = [
        hmac.digest(key, response.cookies["dtw_session"].value.encode(), "sha256").hex()
        for response in (one, two)
    ]
    original = client.app[STATE].bridge._headers.copy()
    start = len(fake.owner_headers)
    responses = await asyncio.gather(
        client.get(
            "/api/cli-login/status?browser=one", headers={**first, "X-Dtw-Owner": expected[1]}
        ),
        client.get(
            "/api/cli-login/status?browser=two", headers={**second, "X-Dtw-Owner": expected[0]}
        ),
        client.get("/api/topics", headers=first),
    )
    assert all(response.status == 200 for response in responses)
    seen = [owner for path, owner in fake.owner_headers[start:] if path == "/cli-login/status"]
    assert sorted(seen) == sorted(expected)
    assert fake.owner_by_browser == {"one": expected[0], "two": expected[1]}
    assert client.app[STATE].bridge._headers == original
    assert all(
        owner is None for path, owner in fake.owner_headers[start:] if path != "/cli-login/status"
    )
    assert {call[2]["browser"] for call in fake.calls if call[1] == "/cli-login/status"} == {
        "one",
        "two",
    }


@pytest.mark.parametrize("path", ["/api/cli-login/status", "/api/cli-login/jobs/job-1"])
async def test_cli_login_requests_keep_five_second_upstream_timeout(site, path):
    client, fake = site
    authorized, _ = await login(client)
    original = fake.handle

    async def slow(request):
        if "/cli-login/" in request.path:
            await asyncio.sleep(6)
        return await original(request)

    bridge = client.app[STATE].bridge
    from aiohttp.test_utils import TestServer

    upstream = web.Application()
    upstream.router.add_route("*", "/webchat/v1/{path:.*}", slow)
    async with TestServer(upstream) as server:
        bridge._url = str(server.make_url("/webchat/v1"))
        response = await client.get(path, headers=authorized)
        assert response.status == 503
        assert await response.json() == {"error": "bot_unreachable"}
    assert bridge._client.timeout.total == 5


async def test_logout_cancels_only_its_own_cli_login(site):
    client, fake = site
    first, _ = await login(client)
    second, _ = await login(client)
    job = (
        await (
            await client.post("/api/cli-login/jobs", headers=first, json={"provider": "codex"})
        ).json()
    )["job_id"]
    assert (await client.post("/api/auth/logout", headers=second)).status == 200
    assert job not in fake.login_cancelled
    assert (await client.post("/api/auth/logout", headers=first)).status == 200
    assert job in fake.login_cancelled


async def test_password_change_cancels_jobs_for_all_revoked_sessions(site):
    client, fake = site
    first, _ = await login(client)
    second, _ = await login(client)
    job = (
        await (
            await client.post("/api/cli-login/jobs", headers=first, json={"provider": "codex"})
        ).json()
    )["job_id"]
    payload = {"current_password": "wrong", "new_password": "new-password-long", "code": "123456"}
    assert (
        await client.post("/api/auth/password-change", headers=second, json=payload)
    ).status == 403
    assert job not in fake.login_cancelled
    payload["current_password"] = "good-password"
    response = await client.post("/api/auth/password-change", headers=second, json=payload)
    assert response.status == 200
    assert job in fake.login_cancelled
    assert client.app[STATE].sessions.count() == 1


@pytest.mark.parametrize("action", ["logout", "password-change"])
async def test_session_revocation_survives_cli_cancel_bridge_failure(site, action):
    from ductor_bot.webchat.web.upstream import BridgeUnavailableError

    client, _ = site
    supplied, _ = await login(client)
    bridge = client.app[STATE].bridge
    original = bridge.request

    async def request(method, path, **kwargs):
        if path == "/cli-login/cancel-owner":
            raise BridgeUnavailableError
        return await original(method, path, **kwargs)

    with patch.object(bridge, "request", side_effect=request):
        response = await client.post(
            "/api/auth/" + action,
            headers=supplied,
            json={
                "current_password": "good-password",
                "new_password": "new-password-long",
                "code": "123456",
            },
        )
    assert response.status == 200
    assert (await client.get("/api/state", headers=supplied)).status == 403


async def test_expired_cli_owner_is_recoverable_and_browser_cannot_forge_live_owners(site):
    client, _ = site
    first, _ = await login(client)
    second, _ = await login(client)
    job = (
        await (
            await client.post("/api/cli-login/jobs", headers=first, json={"provider": "codex"})
        ).json()
    )["job_id"]
    forged = {**second, "X-Dtw-Owner": "fake", "X-Dtw-Active-Owners": "[]"}
    state = await (await client.get("/api/cli-login/status", headers=forged)).json()
    assert state["running"]
    assert state["job_id"] is None
    assert (await client.get(f"/api/cli-login/jobs/{job}", headers=second)).status == 404
    sessions = client.app[STATE].sessions
    token = first["Cookie"].split("=", 1)[1]
    sessions.get(token).created -= ABSOLUTE_TTL + 1
    state = await (await client.get("/api/cli-login/status", headers=second)).json()
    assert state["running"]
    assert state["job_id"] == job
    assert (await client.get(f"/api/cli-login/jobs/{job}", headers=second)).status == 200
    assert (await client.post(f"/api/cli-login/jobs/{job}/cancel", headers=second)).status == 202
    assert (await client.get(f"/api/cli-login/jobs/{job}", headers=first)).status == 403


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [("PUT", "/api/topics/-123.7/seen", {"seq": -5}), ("POST", "/api/topics/seen-all", {})],
)
async def test_read_state_relay_requires_session_client_header_and_csrf(site, method, path, body):
    client, fake = site
    for request_headers in [{}, CLIENT]:
        response = await client.request(method, path, json=body, headers=request_headers)
        assert response.status == 403
        assert "WWW-Authenticate" not in response.headers
    headers, _ = await login(client)
    before = len(fake.calls)
    for request_headers in [
        {key: value for key, value in headers.items() if key != "X-Dtw-Csrf"},
        {**headers, "X-Dtw-Csrf": "bad"},
        {key: value for key, value in headers.items() if key != "X-Dtw-Client"},
    ]:
        response = await client.request(method, path, json=body, headers=request_headers)
        assert response.status == 403
        assert "WWW-Authenticate" not in response.headers
    assert not any(call[1] == path.removeprefix("/api") for call in fake.calls[before:])


async def test_logout_releases_its_pending_authentication_context(site):
    client, fake = site
    headers, _ = await login(client)
    pre = await password(client)
    headers["Cookie"] += "; " + cookie_header(pre, "dtw_pre")
    response = await client.post("/api/auth/logout", headers=headers)
    assert response.status == 200
    calls = [call for call in fake.calls if call[1] == "/auth/logout"]
    assert len(calls) == 1
    assert calls[0][3] == {"preauth": "fake-preauth"}
    assert response.cookies["dtw_pre"]["max-age"] == "0"


async def test_preauth_only_logout_reaches_auth_and_discards_material(
    aiohttp_client, aiohttp_server, tmp_path
):
    from ductor_bot.webchat.auth import AuthError, BridgeAuth, hash_password, totp_code
    from ductor_bot.webchat.credentials import WebCredentials, derive_seed_key, encrypt_seed

    password_value = "synthetic-password-123"
    seed = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    auth = BridgeAuth(tmp_path / "secrets.env", "example.com", tmp_path / "auth_state.json")
    salt = b"0123456789abcdef"
    auth.credential_store.save(
        WebCredentials(
            hash_password(password_value),
            encrypt_seed(seed, derive_seed_key(password_value, salt), salt),
        )
    )
    fake = FakeBridge()

    async def forward(request):
        assert request.headers.get("X-Dtw-Bridge-Token") == "fake-bridge-token"
        try:
            return web.json_response(
                await auth.handle(request.match_info["action"], await request.json())
            )
        except AuthError as exc:
            return web.json_response(exc.payload, status=403)

    bridge = web.Application()
    bridge.router.add_post("/webchat/v1/auth/{action:password|totp|logout}", forward)
    bridge.router.add_route("*", "/webchat/v1/{path:.*}", fake.handle)
    server = await aiohttp_server(bridge)
    client = await aiohttp_client(
        create_app(
            bridge_url=str(server.make_url("")),
            bridge_token="fake-bridge-token",
            public_origin=ORIGIN,
            static_dir=tmp_path,
        )
    )
    try:
        response = await client.post(
            "/api/auth/password",
            json={"password": password_value},
            headers={**CLIENT, "X-Real-IP": "198.51.100.7"},
        )
        assert response.status == 200
        cookie = cookie_header(response, "dtw_pre")
        token = response.cookies["dtw_pre"].value
        pre = auth._preauth[token]
        assert pre.seed == seed
        assert pre.seed_key is not None
        assert pre.seed_salt is not None
        denied = await client.post("/api/auth/logout", json={}, headers={"Cookie": cookie})
        assert denied.status == 403
        assert token in auth._preauth
        response = await client.post(
            "/api/auth/logout", json={}, headers={**CLIENT, "Cookie": cookie}
        )
        assert response.status == 200
        assert response.cookies["dtw_pre"]["max-age"] == "0"
        assert response.cookies["dtw_session"]["max-age"] == "0"
        assert token not in auth._preauth
        assert token not in auth._preauth_timers
        assert pre.seed is pre.seed_key is pre.seed_salt is None
        response = await client.post(
            "/api/auth/totp",
            json={"code": totp_code(seed, int(time.time() // 30))},
            headers={**CLIENT, "Cookie": cookie, "X-Real-IP": "198.51.100.7"},
        )
        assert response.status == 403
    finally:
        await client.close()
        await auth.close()


async def test_turn_cancel_relay_requires_session_csrf_and_client(site):
    client, fake = site
    path = "/api/topics/-123.4/turns/turn-id/cancel"
    denied = await client.post(path, headers=CLIENT)
    assert denied.status == 403
    assert "WWW-Authenticate" not in denied.headers
    session_headers, _ = await login(client)
    for auth in [
        CLIENT,
        {"X-Dtw-Csrf": session_headers["X-Dtw-Csrf"]},
        {**session_headers, "X-Dtw-Csrf": "wrong"},
    ]:
        response = await client.post(path, headers=auth)
        assert response.status == 403
        assert "WWW-Authenticate" not in response.headers
    response = await client.post(path, headers=session_headers)
    assert response.status == 200
    assert fake.calls[-1][:2] == ("POST", "/topics/-123.4/turns/turn-id/cancel")


async def test_attachment_message_owner_is_derived_from_cookie(site):
    client, fake = site
    authorized, response = await login(client)
    expected = hmac.digest(
        client.app[STATE].owner_key, response.cookies["dtw_session"].value.encode(), "sha256"
    ).hex()
    posted = await client.post(
        "/api/topics/123/messages",
        json={"text": "message", "client_id": "request", "attachment_ids": []},
        headers={**authorized, "X-Dtw-Owner": "forged-owner"},
    )
    assert posted.status == 202
    assert [owner for path, owner in fake.owner_headers if path == "/topics/123/messages"] == [
        expected
    ]
    fetched = await client.get("/api/topics/123/messages/entry/attachments", headers=authorized)
    assert fetched.status == 200
    assert (await fetched.json())["path"] == "/topics/123/messages/entry/attachments"


async def test_slot_logout_returns_the_result_after_five_seconds(site):
    client, fake = site
    authorized, _ = await login(client)
    route = "/cli-login/slots/claude-work/logout"
    fake.delay_path = route
    fake.delay = 6

    response = await client.post(f"/api{route}", headers=authorized)

    assert response.status == 200
    assert await response.json() == {"ok": True}
    assert client.app[STATE].bridge._client.timeout.total == 5
