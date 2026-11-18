"""Real browser relay + bridge vault contracts; all credentials are synthetic."""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from ductor_bot.chat_log.events import EventHub
from ductor_bot.config import AgentConfig
from ductor_bot.i18n import init
from ductor_bot.infra.secret_runtime import start_secret_runtime, stop_secret_runtime
from ductor_bot.infra.secret_store import set_secret
from ductor_bot.infra.secret_vault import VaultState
from ductor_bot.webchat.auth import hash_password
from ductor_bot.webchat.bridge import create_bridge_app
from ductor_bot.webchat.credentials import (
    CredentialStore,
    WebCredentials,
    derive_seed_key,
    encrypt_seed,
)
from ductor_bot.webchat.web.app import STATE, create_app
from ductor_bot.webchat.web.sessions import SESSION_COOKIE
from ductor_bot.workspace.paths import DuctorPaths
from tests.messenger.telegram.test_secret_input import Harness, message, settled_intercept, update

KEY = "synthetic-master-key-42"
NEW_KEY = "replacement-master-key-43"
VALUE = "synthetic-secret-value-44"
OTHER_VALUE = "other-secret-value-45"
TOKEN = "synthetic-bridge-token"
PASSWORD = "synthetic-web-password"
ORIGIN = "https://chat.example.com"
PREFIX = "/webchat/v1"


@pytest.fixture
async def h(tmp_path, monkeypatch):
    init("en")
    path = tmp_path / "secrets.env"
    monkeypatch.setenv("DUCTOR_SECRETS_FILE", str(path))
    paths = DuctorPaths(ductor_home=tmp_path)
    live = start_secret_runtime(tmp_path)
    clock = SimpleNamespace(now=1000.0)
    live.vault._clock = lambda: clock.now
    salt = b"0123456789abcdef"
    store = CredentialStore(paths.webchat_dir / "credentials.json")
    store.save(
        WebCredentials(
            hash_password(PASSWORD),
            encrypt_seed("JBSWY3DPEHPK3PXP", derive_seed_key(PASSWORD, salt), salt),
        )
    )
    runtime = SimpleNamespace(
        events=EventHub(),
        history=SimpleNamespace(append=AsyncMock()),
        recorder=MagicMock(),
        link=SimpleNamespace(state=lambda: {"state": "unknown"}),
        outbox=SimpleNamespace(pending=AsyncMock(return_value=[]), stop=AsyncMock()),
    )
    bot = SimpleNamespace(
        config=AgentConfig(allowed_user_ids=[101], language="en"),
        _orch=SimpleNamespace(paths=paths),
    )
    internal = TestClient(TestServer(create_bridge_app(bot, runtime, TOKEN)))
    await internal.start_server()
    public_app = create_app(
        bridge_url=str(internal.make_url("")), bridge_token=TOKEN, public_origin=ORIGIN
    )
    public = TestClient(TestServer(public_app))
    await public.start_server()
    state = public_app[STATE]
    response = await internal.get(PREFIX + "/auth/state", headers={"X-Dtw-Bridge-Token": TOKEN})
    generation = (await response.json())["credential_generation"]
    token = state.sessions.create(generation)
    session = state.sessions.get(token)
    headers = {
        "Cookie": f"{SESSION_COOKIE}={token}",
        "X-Dtw-Client": "web",
        "X-Dtw-Csrf": session.csrf,
    }
    try:
        yield SimpleNamespace(
            client=public,
            internal=internal,
            state=state,
            headers=headers,
            token=token,
            vault=live.vault,
            clock=clock,
            runtime=runtime,
            path=path,
            root=tmp_path,
        )
    finally:
        await public.close()
        await internal.close()
        await stop_secret_runtime(live)


async def post(h, path, body, expected=200):
    response = await h.client.post("/api" + path, json=body, headers=h.headers)
    assert response.status == expected, await response.text()
    assert response.headers["Cache-Control"] == "no-store"
    return await response.json()


async def setup(h):
    await post(h, "/masterkey/setup", {"master_key": KEY, "confirm_key": KEY})


@pytest.mark.parametrize("path", ["/masterkey/status", "/secrets/keys"])
async def test_read_authentication_and_no_store(h, path):
    response = await h.client.get("/api" + path)
    assert response.status == 403
    assert await response.json() == {"error": "auth_required"}
    assert response.headers["Cache-Control"] == "no-store"
    response = await h.internal.get(PREFIX + path)
    assert response.status == 403
    assert response.headers["Cache-Control"] == "no-store"
    assert "WWW-Authenticate" not in response.headers


@pytest.mark.parametrize("missing", ["cookie", "client", "csrf", "wrong-csrf"])
async def test_mutations_require_browser_session_client_and_csrf(h, missing):
    headers = dict(h.headers)
    if missing == "wrong-csrf":
        headers["X-Dtw-Csrf"] = "wrong"
    else:
        headers.pop({"cookie": "Cookie", "client": "X-Dtw-Client", "csrf": "X-Dtw-Csrf"}[missing])
    response = await h.client.post(
        "/api/masterkey/setup", json={"master_key": KEY, "confirm_key": KEY}, headers=headers
    )
    assert response.status == 403
    assert not h.vault.encrypted_path.exists()
    response = await h.internal.post(
        PREFIX + "/masterkey/setup",
        json={"master_key": KEY, "confirm_key": KEY},
        headers={"Authorization": "Bearer ignored"},
    )
    assert response.status == 403
    assert "WWW-Authenticate" not in response.headers


async def test_complete_actions_and_single_value_reveal(h):
    response = await h.client.get("/api/masterkey/status", headers=h.headers)
    assert await response.json() == {
        "state": "unconfigured",
        "ttl_seconds": 0,
        "remaining_seconds": None,
    }
    # Legacy plaintext writes remain available until setup.
    await post(h, "/secrets/set", {"key": "TOOL_TOKEN", "value": VALUE})
    await post(h, "/secrets/set", {"key": "OTHER_TOKEN", "value": OTHER_VALUE})
    await setup(h)
    assert not h.path.exists()
    response = await h.client.get("/api/secrets/keys", headers=h.headers)
    assert await response.json() == {"keys": ["OTHER_TOKEN", "TOOL_TOKEN"]}
    assert VALUE not in await response.text()
    assert await post(h, "/secrets/reveal", {"key": "TOOL_TOKEN", "master_key": KEY}) == {
        "value": VALUE
    }
    assert await post(h, "/secrets/reveal", {"key": "MISSING", "master_key": KEY}, 400) == {
        "error": "SECRET_NOT_SET"
    }
    response = await h.internal.post(
        PREFIX + "/secrets/reveal",
        json={"key": "TOOL_TOKEN", "master_key": KEY},
        headers={"X-Dtw-Bridge-Token": TOKEN},
    )
    assert response.headers["Cache-Control"] == "no-store"
    assert await response.json() == {"value": VALUE}
    await post(h, "/secrets/set", {"key": "TOOL_TOKEN", "value": OTHER_VALUE})
    assert await post(h, "/secrets/delete", {"key": "OTHER_TOKEN"}) == {"ok": True, "deleted": True}
    assert await post(h, "/secrets/delete", {"key": "OTHER_TOKEN"}) == {
        "ok": True,
        "deleted": False,
    }
    await post(h, "/masterkey/ttl", {"ttl_seconds": 600})
    response = await h.client.get("/api/masterkey/status", headers=h.headers)
    assert await response.json() == {
        "state": "unlocked",
        "ttl_seconds": 600,
        "remaining_seconds": 600.0,
    }
    await post(
        h, "/masterkey/change", {"current_key": KEY, "new_key": NEW_KEY, "confirm_key": NEW_KEY}
    )
    await post(h, "/masterkey/lock", {})
    await post(h, "/masterkey/unlock", {"master_key": NEW_KEY})
    assert await post(h, "/secrets/reveal", {"key": "TOOL_TOKEN", "master_key": NEW_KEY}) == {
        "value": OTHER_VALUE
    }
    assert (await h.vault.status()).ttl_seconds == 600


@pytest.mark.parametrize(
    ("operation", "body"),
    [
        ("set", {"key": "TOOL_TOKEN", "value": VALUE}),
        ("delete", {"key": "TOOL_TOKEN"}),
        ("reveal", {"key": "TOOL_TOKEN", "master_key": KEY}),
    ],
)
async def test_locked_secrets_refused(h, operation, body):
    await setup(h)
    await post(h, "/masterkey/lock", {})
    assert await post(h, "/secrets/" + operation, body, 409) == {"error": "SECRETS_LOCKED"}
    response = await h.client.get("/api/secrets/keys", headers=h.headers)
    assert response.status == 409
    assert await response.json() == {"error": "SECRETS_LOCKED"}
    response = await h.client.get("/api/masterkey/status", headers=h.headers)
    assert (await response.json())["state"] == "locked"


async def test_wrong_master_key_rate_limit_and_telegram_share_one_vault(h):
    await setup(h)
    await h.vault.set("TOOL_TOKEN", VALUE)
    assert await post(
        h, "/secrets/reveal", {"key": "TOOL_TOKEN", "master_key": "incorrect-key"}, 422
    ) == {"error": "MASTER_KEY_INVALID"}
    telegram = Harness(h.root)
    try:
        await telegram.command("/masterkey lock")
        await telegram.command("/masterkey unlock")
        await settled_intercept(
            telegram.input, update(message(KEY, mid=10, reply=telegram.prompt()))
        )
        await drain(telegram)
        assert "SECRETS_RATE_LIMITED" in telegram.texts()[-1]
        assert (await h.vault.status()).state is VaultState.LOCKED
        h.clock.now += 2
        await telegram.command("/masterkey unlock")
        await settled_intercept(
            telegram.input, update(message(KEY, mid=11, reply=telegram.prompt()))
        )
        assert (await h.vault.status()).state is VaultState.UNLOCKED
        assert await post(h, "/secrets/reveal", {"key": "TOOL_TOKEN", "master_key": KEY}) == {
            "value": VALUE
        }
    finally:
        await telegram.input.close()


async def drain(telegram):
    from tests.messenger.telegram.test_secret_input import drain_deletions

    await drain_deletions(telegram.input)


async def test_invalid_key_does_not_revoke_web_session(h):
    await setup(h)
    await post(h, "/masterkey/lock", {})
    assert await post(h, "/masterkey/unlock", {"master_key": "incorrect-key"}, 422) == {
        "error": "MASTER_KEY_INVALID"
    }
    assert h.state.sessions.get(h.token) is not None
    result = await post(h, "/masterkey/unlock", {"master_key": KEY}, 429)
    assert result == {"error": "SECRETS_RATE_LIMITED", "retry_after": 1}
    h.clock.now += 2
    await post(h, "/masterkey/unlock", {"master_key": KEY})


async def test_input_codes_and_web_auth_migration_guard(h):
    assert await post(h, "/masterkey/setup", {"master_key": KEY, "confirm_key": NEW_KEY}, 400) == {
        "error": "MASTER_KEY_MISMATCH"
    }
    assert await post(
        h, "/masterkey/setup", {"master_key": "short", "confirm_key": "short"}, 400
    ) == {"error": "MASTER_KEY_TOO_SHORT"}
    set_secret(h.path, "DT_WEBCHAT_PASSWORD_HASH", "synthetic-old-hash")
    assert await post(h, "/masterkey/setup", {"master_key": KEY, "confirm_key": KEY}, 409) == {
        "error": "WEB_AUTH_MIGRATION_REQUIRED"
    }
    assert h.path.exists()


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/masterkey/setup", {"master_key": KEY}),
        ("/masterkey/setup", {"master_key": KEY, "confirm_key": KEY, "extra": VALUE}),
        ("/masterkey/ttl", {"ttl_seconds": True}),
        ("/masterkey/ttl", {"ttl_seconds": "60"}),
        ("/secrets/set", {"key": "TOOL_TOKEN", "value": 7}),
    ],
)
async def test_bad_body_types_and_fields(h, path, body):
    assert await post(h, path, body, 400) == {"error": "bad_request"}


@pytest.mark.parametrize("browser", [True, False])
@pytest.mark.parametrize("mode", ["oversize", "chunked", "malformed", "not-json", "array"])
async def test_body_bounds_and_json_only(h, browser, mode):
    client = h.client if browser else h.internal
    headers = h.headers if browser else {"X-Dtw-Bridge-Token": TOKEN}
    path = ("/api" if browser else PREFIX) + "/masterkey/setup"
    raw = (
        b"[1]"
        if mode == "array"
        else b"{broken"
        if mode == "malformed"
        else b"x" * (64 * 1024 + 1)
        if mode in {"oversize", "chunked"}
        else b"{}"
    )
    headers = {
        **headers,
        "Content-Type": "text/plain" if mode == "not-json" else "application/json",
    }

    async def chunks():
        yield raw[:32000]
        yield raw[32000:]

    response = await client.post(path, data=chunks() if mode == "chunked" else raw, headers=headers)
    assert response.status == (413 if mode in {"oversize", "chunked"} else 400)
    assert response.headers["Cache-Control"] == "no-store"
    assert not h.vault.encrypted_path.exists()


async def test_ws_events_from_browser_telegram_legacy_secret_and_idle_expiry(h):
    socket = await h.client.ws_connect("/api/ws", headers={**h.headers, "Origin": ORIGIN})

    # Web relay announces bridge connectivity; listen for the value-free invalidation frame.
    async def changed():
        while True:
            event = await socket.receive_json(timeout=3)
            if event.get("type") == "secrets_changed":
                assert event == {"type": "secrets_changed"}
                return

    telegram = Harness(h.root)
    try:
        await post(h, "/secrets/set", {"key": "TOOL_TOKEN", "value": VALUE})
        await changed()
        await telegram.command("/secret set OTHER_TOKEN")
        await settled_intercept(
            telegram.input, update(message(OTHER_VALUE, mid=12, reply=telegram.prompt()))
        )
        await changed()
        await setup(h)
        await changed()
        await telegram.command("/masterkey lock")
        await changed()
        await h.vault.unlock(KEY)
        await changed()
        await h.vault.set_ttl(60)
        await changed()
        h.clock.now += 61
        await h.vault.status()
        await changed()
        await h.vault.unlock(KEY)
        await changed()
        await h.vault.change_key(KEY, NEW_KEY)
        await changed()
        await h.vault.delete("OTHER_TOKEN")
        await changed()
    finally:
        await socket.close()
        await telegram.input.close()


async def test_debug_and_access_logs_history_and_outbox_never_receive_inputs(h, caplog):
    caplog.set_level(logging.DEBUG)
    await setup(h)
    await post(h, "/secrets/set", {"key": "TOOL_TOKEN", "value": VALUE})
    await post(h, "/secrets/set", {"key": "OTHER_TOKEN", "value": OTHER_VALUE})
    await post(h, "/secrets/reveal", {"key": "TOOL_TOKEN", "master_key": KEY})
    await post(
        h, "/masterkey/change", {"current_key": KEY, "new_key": NEW_KEY, "confirm_key": NEW_KEY}
    )
    for canary in (KEY, NEW_KEY, VALUE, OTHER_VALUE):
        assert canary not in caplog.text
        assert all(canary not in str(record.args) for record in caplog.records)
        assert all(
            canary.encode() not in path.read_bytes() for path in h.root.rglob("*") if path.is_file()
        )
    h.runtime.history.append.assert_not_awaited()
    assert not h.runtime.recorder.mock_calls
    assert not list((h.root / "chat_log").rglob("*"))


def test_api_422_does_not_emit_auth_required():
    source = Path("ductor_bot/webchat/web/static/js/api.js").read_text()
    script = """
import assert from 'node:assert/strict';
const source = JSON.parse(process.argv[1]);
globalThis.document = new EventTarget();
globalThis.CustomEvent = class extends Event {};
let required = 0;
document.addEventListener('auth-required', () => required++);
const api = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
api.setCsrf('synthetic-csrf');
globalThis.fetch = async () => ({ok:false,status:422,json:async()=>({error:'MASTER_KEY_INVALID'})});
await assert.rejects(api.api('/masterkey/unlock',{method:'POST',body:{master_key:'synthetic-key'}}), e=>e.status===422 && e.code==='MASTER_KEY_INVALID');
assert.equal(required,0);
globalThis.fetch = async () => ({ok:false,status:403,json:async()=>({error:'auth_required'})});
await assert.rejects(api.api('/masterkey/unlock',{method:'POST',body:{master_key:'synthetic-key'}}));
assert.equal(required,1);
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, json.dumps(source)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


async def test_unauthenticated_empty_mutation_still_returns_403(h):
    response = await h.client.post("/api/masterkey/unlock")
    assert response.status == 403
    response = await h.internal.post(PREFIX + "/masterkey/unlock")
    assert response.status == 403


async def test_listener_cleanup_preserves_telegram_expiry_callback(h):
    telegram = Harness(h.root)
    try:
        await telegram.input.start()
        security_callback = h.vault._expiry_callback
        assert security_callback is not None
        assert h.vault._change_listeners
        await h.internal.close()
        assert not h.vault._change_listeners
        assert h.vault._expiry_callback is security_callback
    finally:
        await telegram.input.close()


async def test_automatic_expiry_emits_ws_event_without_a_status_request(h):
    h.vault._clock = asyncio.get_running_loop().time
    await setup(h)
    socket = await h.client.ws_connect("/api/ws", headers={**h.headers, "Origin": ORIGIN})
    try:
        await h.vault.set_ttl(1)
        # One invalidation for the TTL edit, then another from the idle expiry timer.
        count = 0
        while count < 2:
            event = await socket.receive_json(timeout=3)
            if event.get("type") == "secrets_changed":
                assert event == {"type": "secrets_changed"}
                count += 1
        assert (await h.vault.status()).state is VaultState.LOCKED
    finally:
        await socket.close()


async def test_web_setup_reports_symlink_without_migrating_source(h):
    original = h.path.with_name("original.env")
    original.write_text("TOKEN=synthetic-symlink-value\n")
    h.path.unlink(missing_ok=True)
    h.path.symlink_to(original)
    before = original.read_bytes()
    assert await post(h, "/masterkey/setup", {"master_key": KEY, "confirm_key": KEY}, 400) == {
        "error": "SECRETS_FILE_SYMLINK"
    }
    assert original.read_bytes() == before
    assert h.path.is_symlink()
    assert not h.vault.encrypted_path.exists()
    assert (await h.vault.status()).state is VaultState.UNCONFIGURED


async def test_ttl_extension_requires_confirmation_and_shares_failure_limit(h):
    await post(h, "/masterkey/setup", {"master_key": KEY, "confirm_key": KEY})
    await post(h, "/masterkey/ttl", {"ttl_seconds": 60})
    for longer in (120, 0):
        assert await post(h, "/masterkey/ttl", {"ttl_seconds": longer}, 400) == {
            "error": "MASTER_KEY_REQUIRED"
        }
    assert h.vault._failures == 0
    assert await post(
        h, "/masterkey/ttl", {"ttl_seconds": 120, "master_key": "wrong-key"}, 422
    ) == {"error": "MASTER_KEY_INVALID"}
    assert (await post(h, "/masterkey/unlock", {"master_key": KEY}, 429))[
        "error"
    ] == "SECRETS_RATE_LIMITED"
    h.clock.now += 2
    await post(h, "/masterkey/ttl", {"ttl_seconds": 120, "master_key": KEY})
    await post(h, "/masterkey/ttl", {"ttl_seconds": 30})
    await post(h, "/masterkey/lock", {})
    await post(h, "/masterkey/ttl", {"ttl_seconds": 0, "master_key": KEY})
    assert (await h.vault.status()).state is VaultState.LOCKED
    assert (await h.vault.status()).ttl_seconds == 0


async def test_unconfigured_ttl_does_not_require_master_key(h):
    for duration in (60, 120, 0):
        await post(h, "/masterkey/ttl", {"ttl_seconds": duration})
    assert (await h.vault.status()).state is VaultState.UNCONFIGURED


async def test_invalid_config_status_reports_reason_and_blocks_unlock(h):
    await post(h, "/masterkey/setup", {"master_key": KEY, "confirm_key": KEY})
    await post(h, "/masterkey/lock", {})
    h.vault.configuration_error = "INVALID_VAULT_CONFIG"
    response = await h.client.get("/api/masterkey/status", headers=h.headers)
    assert response.status == 200
    status = await response.json()
    assert status["state"] == "locked"
    assert status["reason"] == "INVALID_VAULT_CONFIG"
    assert await post(h, "/masterkey/unlock", {"master_key": KEY}, 400) == {
        "error": "INVALID_VAULT_CONFIG"
    }
