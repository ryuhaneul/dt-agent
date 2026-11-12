"""Bridge authentication, metadata and listener lifetime."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import socket
import stat
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import DeleteForumTopic
from aiohttp.test_utils import TestClient, TestServer

from ductor_bot.bus.envelope import Envelope, Origin
from ductor_bot.bus.lock_pool import LockPool
from ductor_bot.chat_log.events import EventHub
from ductor_bot.chat_log.history import Entry, HistoryStore
from ductor_bot.chat_log.ids import history_filename
from ductor_bot.chat_log.outbox import Outbox
from ductor_bot.chat_log.recorder import HistoryRecorder, chat_log_source, chat_log_turn
from ductor_bot.cli.base import CLIConfig
from ductor_bot.cli.claude_provider import ClaudeCodeCLI
from ductor_bot.cli.stream_events import AssistantTextDelta, ResultEvent, ToolUseEvent
from ductor_bot.config import AgentConfig, ChatLogConfig, WebchatConfig
from ductor_bot.infra.input_context import current_input_timestamp
from ductor_bot.infra.secret_store import set_secret
from ductor_bot.messenger.telegram.topic import TopicNameCache
from ductor_bot.messenger.telegram.transport import TelegramTransport
from ductor_bot.multiagent.shared_knowledge import _sync_agent_io
from ductor_bot.orchestrator.registry import OrchestratorResult
from ductor_bot.session.key import SessionKey
from ductor_bot.session.manager import SessionData, SessionManager
from ductor_bot.webchat.auth import PASSWORD_KEY, hash_password, totp_code
from ductor_bot.webchat.bridge import create_bridge_app, start_bridge
from ductor_bot.webchat.files import WebFileError, WebFiles
from ductor_bot.webchat.turns import WebTurns
from ductor_bot.workspace.paths import DuctorPaths

_PREFIX = "/webchat/v1"
_TOKEN = "test-bridge-token"
_PASSWORD = "sample-password-123"


@pytest.fixture
async def bridge(tmp_path, monkeypatch):
    paths = DuctorPaths(ductor_home=tmp_path)
    monkeypatch.setattr(
        "ductor_bot.webchat.bridge.store_path", lambda _home: tmp_path / "secrets.env"
    )
    bot = SimpleNamespace(
        config=AgentConfig(
            allowed_user_ids=[123],
            user_timezone="Asia/Seoul",
            language="ko",
            chat_log=ChatLogConfig(enabled=True),
            webchat=WebchatConfig(
                enabled=True,
                domain="chat.example.com",
                allowed_ips=["203.0.113.0/24"],
                acme_email="admin@example.com",
            ),
        ),
        _orch=SimpleNamespace(paths=paths),
    )
    runtime = SimpleNamespace(
        link=SimpleNamespace(state=lambda: {"state": "unknown"}),
        outbox=SimpleNamespace(pending=AsyncMock(return_value=[object(), object()])),
    )
    client = TestClient(TestServer(create_bridge_app(bot, runtime, _TOKEN)))
    await client.start_server()
    try:
        yield client, bot, runtime, paths
    finally:
        await client.close()


def headers():
    return {"X-Dtw-Bridge-Token": _TOKEN}


@pytest.mark.parametrize(
    "path",
    ["health", "meta", "state", "auth/state", "not-found", "files", "files/PERSONA.md", "outbox"],
)
@pytest.mark.parametrize("token", [None, "wrong", "test-bridge-token-extra"])
async def test_every_path_requires_bridge_token(bridge, path, token):
    client, *_ = bridge
    response = await client.get(
        f"{_PREFIX}/{path}", headers={} if token is None else {"X-Dtw-Bridge-Token": token}
    )
    assert response.status == 403
    assert await response.json() == {"error": "forbidden"}
    assert not any(name.lower().startswith("access-control-") for name in response.headers)
    assert "WWW-Authenticate" not in response.headers


async def test_health_meta_state_and_auth_state(bridge):
    client, _, _, paths = bridge
    expected = {
        "health": {"ok": True, "agent": "main"},
        "meta": {
            "language": "ko",
            "timezone": "Asia/Seoul",
            "default_chat_id": 123,
            "domain": "chat.example.com",
        },
        "state": {"telegram": {"state": "unknown"}, "outbox": {"pending": 2}},
        "auth/state": {
            "password_configured": False,
            "totp_configured": False,
            "credential_generation": (
                await (await client.get(f"{_PREFIX}/auth/state", headers=headers())).json()
            )["credential_generation"],
        },
    }
    for path, body in expected.items():
        response = await client.get(f"{_PREFIX}/{path}", headers=headers())
        assert response.status == 200
        assert await response.json() == body
    assert (paths.webchat_dir / "auth_state.json").stat().st_mode & 0o777 == 0o600


async def test_password_setup_totp_end_to_end(bridge, caplog):
    client, _, _, paths = bridge
    set_secret(paths.ductor_home / "secrets.env", PASSWORD_KEY, hash_password(_PASSWORD))

    async def post(action, data):
        response = await client.post(f"{_PREFIX}/auth/{action}", json=data, headers=headers())
        return response, await response.json()

    response, _ = await post("password", {"password": "incorrect", "client_ip": "198.51.100.7"})
    assert response.status == 403
    response, pre = await post("password", {"password": _PASSWORD, "client_ip": "198.51.100.7"})
    assert response.status == 200
    assert pre["next"] == "setup"
    response, setup = await post("setup/begin", {"preauth": pre["preauth"], "mode": "new"})
    assert response.status == 200
    import time

    code = totp_code(setup["secret"], int(time.time() // 30))
    response, body = await post(
        "setup/confirm", {"preauth": pre["preauth"], "code": code, "client_ip": "198.51.100.7"}
    )
    assert response.status == 200
    assert body == {"ok": True}
    response, pre = await post("password", {"password": _PASSWORD, "client_ip": "198.51.100.7"})
    assert pre["next"] == "totp"
    response, body = await post(
        "totp", {"preauth": pre["preauth"], "code": code, "client_ip": "198.51.100.7"}
    )
    assert response.status == 403
    assert body == {"error": "code_reused"}
    assert _PASSWORD not in caplog.text
    assert setup["secret"] not in caplog.text
    assert pre["preauth"] not in caplog.text


@pytest.mark.parametrize(
    "data",
    [
        "{",
        "null",
        "[]",
        '"text"',
        '{"password":42,"client_ip":[]}',
        '{"password":"\\ud800","client_ip":"198.51.100.7"}',
    ],
)
async def test_malformed_auth_never_succeeds(bridge, data):
    client, *_ = bridge
    response = await client.post(f"{_PREFIX}/auth/password", data=data, headers=headers())
    assert response.status == 403
    assert await response.json() == {"error": "invalid"}


async def test_http_lockout_is_403_with_retry_after(bridge):
    client, *_ = bridge
    for _ in range(5):
        response = await client.post(
            f"{_PREFIX}/auth/password",
            json={"password": "wrong", "client_ip": "198.51.100.7"},
            headers=headers(),
        )
        assert response.status == 403
        assert await response.json() == {"error": "invalid"}
    response = await client.post(
        f"{_PREFIX}/auth/password",
        json={"password": _PASSWORD, "client_ip": "198.51.100.7"},
        headers=headers(),
    )
    assert response.status == 403
    body = await response.json()
    assert body["error"] == "locked"
    assert 0 < body["retry_after"] <= 900
    assert "WWW-Authenticate" not in response.headers


async def test_actual_listener_token_persistence_and_stop(bridge, caplog):
    caplog.set_level(logging.INFO, logger="ductor_bot.webchat.bridge")
    _, bot, runtime, paths = bridge
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    bot.config.webchat.bridge_port = port
    path = paths.webchat_dir / "share" / "bridge.token"
    stop = await start_bridge(bot, runtime)
    saved = path.read_bytes()
    assert [
        record.getMessage()
        for record in caplog.records
        if record.name == "ductor_bot.webchat.bridge" and record.levelno == logging.INFO
    ] == [f"Webchat bridge listening on 0.0.0.0:{port}"]
    assert saved.decode().strip() not in caplog.text
    assert _PASSWORD not in caplog.text
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert len(saved.strip()) >= 43
    url = f"http://127.0.0.1:{port}{_PREFIX}/health"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                assert response.status == 403
            async with session.get(
                url, headers={"X-Dtw-Bridge-Token": saved.decode().strip()}
            ) as response:
                assert response.status == 200
    finally:
        await stop()
        await stop()
    async with aiohttp.ClientSession() as session:
        with pytest.raises(aiohttp.ClientConnectorError):
            await session.get(url)
    stop = await start_bridge(bot, runtime)
    await stop()
    assert path.read_bytes() == saved


async def test_bind_failure_cleans_runner_and_can_retry(bridge, caplog):
    caplog.set_level(logging.INFO, logger="ductor_bot.webchat.bridge")
    _, bot, runtime, _ = bridge
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        bot.config.webchat.bridge_port = occupied.getsockname()[1]
        with pytest.raises(OSError, match="address already in use"):
            await start_bridge(bot, runtime)
        assert "Webchat bridge listening" not in caplog.text
    stop = await asyncio.wait_for(start_bridge(bot, runtime), timeout=2)
    await stop()


@pytest.fixture
async def conversation(tmp_path, monkeypatch):
    paths = DuctorPaths(ductor_home=tmp_path)
    monkeypatch.setattr(
        "ductor_bot.webchat.bridge.store_path", lambda _home: tmp_path / "secrets.env"
    )
    config = AgentConfig(allowed_user_ids=[-123], language="en")
    sessions = SessionManager(tmp_path / "sessions.json", config)
    key = SessionKey.telegram(-123, 7)
    await sessions.resolve_session(key)
    hub = EventHub()
    history = HistoryStore(paths.chat_log_dir / "history", hub)
    outbox = Outbox(paths.chat_log_dir / "outbox", hub)
    runtime = SimpleNamespace(
        events=hub,
        history=history,
        outbox=outbox,
        ops=SimpleNamespace(
            create_topic=AsyncMock(return_value=10),
            rename_topic=AsyncMock(),
            delete_topic=AsyncMock(return_value=True),
        ),
        drainer=SimpleNamespace(wake=MagicMock()),
        link=SimpleNamespace(state=lambda: {"state": "unknown"}),
    )
    recorder = HistoryRecorder(history, hub)
    runtime.recorder = recorder
    pool = LockPool()
    registry = SimpleNamespace(
        kill_by_chat_topic=AsyncMock(return_value=0),
        was_aborted=lambda _chat: False,
        was_aborted_topic=lambda _chat, _topic: False,
    )
    orch = SimpleNamespace(
        paths=paths,
        _sessions=sessions,
        _lock_pool=pool,
        _process_registry=registry,
        is_chat_busy=lambda chat, topic: pool.is_locked((chat, topic)),
        abort=AsyncMock(),
    )
    calls = []

    async def handle(session_key, text, **callbacks):
        calls.append(
            (
                session_key,
                text,
                callbacks,
                chat_log_source.get(),
                chat_log_turn.get(),
                current_input_timestamp(),
            )
        )
        assert pool.get(session_key.lock_key).locked()
        dispatch = SimpleNamespace(
            key=session_key, text=text, transport_override=None, counts_as_activity=True
        )
        await recorder.begin(dispatch)
        await callbacks["on_text_delta"]("answer")
        await callbacks["on_tool_activity"](ToolUseEvent(type="tool_use", tool_name="Read"))
        await callbacks["on_system_status"]("working")
        result = OrchestratorResult(text="answer")
        await recorder.finish(dispatch, result, status="ok")
        return result

    orch.handle_message_streaming = AsyncMock(side_effect=handle)
    bot = SimpleNamespace(config=config, _orch=orch, _topic_names=TopicNameCache())
    client = TestClient(TestServer(create_bridge_app(bot, runtime, _TOKEN)))
    await client.start_server()
    try:
        yield SimpleNamespace(
            client=client,
            bot=bot,
            runtime=runtime,
            paths=paths,
            key=key,
            pool=pool,
            calls=calls,
            recorder=recorder,
        )
    finally:
        await client.close()


async def receive_until(ws, predicate):
    events = []
    while True:
        event = await ws.receive_json(timeout=3)
        events.append(event)
        if predicate(event):
            return events


async def post_message(harness, tid="-123.7", text="hello", **extra):
    return await harness.client.post(
        f"{_PREFIX}/topics/{tid}/messages",
        json={"text": text, "client_id": "browser-request", **extra},
        headers=headers(),
    )


async def test_topics_merge_sources_names_groups_and_flags(conversation):
    h = conversation
    old = SessionData(
        -123, topic_id=7, topic_name="Session name", last_active="2020-01-01T00:00:00+00:00"
    )
    general = SessionData(-123, last_active="2020-01-01T00:00:00+00:00")
    other = SessionData(99, transport="mx")
    await h.bot._orch._sessions._save(
        {
            old.session_key.storage_key: old,
            general.session_key.storage_key: general,
            other.session_key.storage_key: other,
        }
    )
    await h.runtime.history.append(
        "-123.7",
        Entry(
            role="bot",
            source="import",
            text="old text",
            imported=True,
            seq=-1,
            ts="2020-01-01T00:00:00Z",
        ),
    )
    await h.runtime.history.append(
        "-123.8", Entry(role="user", source="telegram", text="x" * 150, ts="2025-01-01T00:00:00Z")
    )
    await h.runtime.history.append(
        "unknown",
        Entry(role="bot", source="import", text="unmapped", imported=True, seq=-1, ts=None),
    )
    h.bot._topic_names.set(-123, 8, "Cached name")
    registry = h.paths.webchat_dir / "topics.json"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(
        '[{"chat_id":-123,"topic_id":7,"name":"Registry name","created_at":"2020-01-01T00:00:00Z"},{"chat_id":-123,"topic_id":9,"name":"Saved name","created_at":null}]'
    )
    response = await h.client.get(f"{_PREFIX}/topics", headers=headers())
    assert response.status == 200
    topics = (await response.json())["topics"]
    by_id = {topic["tid"]: topic for topic in topics}
    assert set(by_id) == {"-123", "-123.7", "-123.8", "-123.9", "unknown"}
    assert by_id["-123.7"]["name"] == "Session name"
    assert by_id["-123.7"]["group"] == "imported"
    assert by_id["-123.7"]["preview"] == "1 imported messages"
    assert by_id["-123.7"]["renamable"]
    assert by_id["-123.7"]["deletable"]
    assert not by_id["-123.7"]["read_only"]
    assert by_id["-123.8"]["name"] == "Cached name"
    assert len(by_id["-123.8"]["preview"]) == 120
    assert by_id["-123.9"]["name"] == "Saved name"
    assert by_id["-123.9"]["created_at"] is None
    assert by_id["-123"]["name"] == "General"
    unknown = by_id["unknown"]
    assert (
        unknown["chat_id"],
        unknown["topic_id"],
        unknown["read_only"],
        unknown["renamable"],
        unknown["deletable"],
    ) == (0, None, True, False, False)
    assert topics[0]["tid"] == "-123.8"


async def test_topics_fallback_name_and_live_entry_leaves_imported_group(conversation):
    h = conversation
    session = await h.bot._orch._sessions.get_active(h.key)
    session.last_active = "2020-01-01T00:00:00Z"
    await h.bot._orch._sessions._save({h.key.storage_key: session})
    await h.runtime.history.append("-123.7", Entry(role="bot", source="telegram", text="live"))
    response = await h.client.get(f"{_PREFIX}/topics", headers=headers())
    topic = (await response.json())["topics"][0]
    assert topic["name"] == "Topic #7"
    assert topic["group"] == "telegram"


async def test_topic_create_rename_and_delete(conversation):
    h = conversation
    response = await h.client.post(
        f"{_PREFIX}/topics", json={"name": "  Created  "}, headers=headers()
    )
    assert response.status == 201
    created = (await response.json())["topic"]
    assert created["tid"] == "-123.10"
    h.runtime.ops.create_topic.assert_awaited_once_with(-123, "Created")
    assert h.bot._topic_names.get(-123, 10) == "Created"
    response = await h.client.patch(
        f"{_PREFIX}/topics/-123.7", json={"name": "Renamed"}, headers=headers()
    )
    assert response.status == 200
    assert (await response.json())["topic"]["name"] == "Renamed"
    assert (await h.bot._orch._sessions.get_active(h.key)).topic_name == "Renamed"
    row = await h.runtime.history.append("-123.7", Entry(role="user", source="web", text="saved"))
    await h.runtime.outbox.enqueue(
        kind="user_mirror", chat_id=-123, topic_id=7, text="mirror", history_id=row.id
    )
    response = await h.client.delete(
        f"{_PREFIX}/topics/-123.7", json={"confirm_name": "Renamed"}, headers=headers()
    )
    assert response.status == 200
    assert await response.json() == {
        "telegram_deleted": True,
        "session_removed": True,
        "history_entries": 1,
        "outbox_dropped": 1,
    }
    assert await h.bot._orch._sessions.get_active(h.key) is None
    assert not (h.runtime.history.directory / history_filename("-123.7")).exists()
    assert (
        h.runtime.outbox._load(next((h.runtime.outbox.directory / "done").glob("*.json"))).state
        == "dropped"
    )
    assert (await post_message(h)).status == 404
    h.bot._orch._process_registry.kill_by_chat_topic.assert_awaited_once_with(-123, 7)
    h.bot._orch.abort.assert_not_awaited()


@pytest.mark.parametrize("name", ["", " ", "x" * 129, 42, None])
async def test_bad_topic_name_is_400(conversation, name):
    h = conversation
    for method, path in (("post", "/topics"), ("patch", "/topics/-123.7")):
        response = await getattr(h.client, method)(
            f"{_PREFIX}{path}", json={"name": name}, headers=headers()
        )
        assert response.status == 400
        assert await response.json() == {"error": "bad_name"}
    h.runtime.ops.create_topic.assert_not_awaited()
    h.runtime.ops.rename_topic.assert_not_awaited()


async def test_unknown_and_general_are_not_manageable(conversation):
    h = conversation
    await h.bot._orch._sessions.resolve_session(SessionKey.telegram(-123))
    for tid in ("unknown", "-123"):
        for method in ("patch", "delete"):
            response = await getattr(h.client, method)(
                f"{_PREFIX}/topics/{tid}",
                json={"name": "x", "confirm_name": "General"},
                headers=headers(),
            )
            assert response.status == 409
            assert await response.json() == {"error": "not_a_topic"}
    for suffix in ("messages", "abort"):
        response = await h.client.post(
            f"{_PREFIX}/topics/unknown/{suffix}",
            json={"text": "hello", "client_id": "id"},
            headers=headers(),
        )
        assert response.status == 409
        assert await response.json() == {"error": "read_only"}
    assert len(await h.bot._orch._sessions.list_all()) == 2
    assert not (h.runtime.history.directory / "unknown.jsonl").exists()


async def test_api_failures_preserve_local_topic_and_unblock(conversation):
    h = conversation
    row = await h.runtime.history.append(
        "-123.7", Entry(role="user", source="telegram", text="saved")
    )
    await h.runtime.outbox.enqueue(
        kind="bus", chat_id=-123, topic_id=7, text="pending", history_id=row.id
    )
    path = h.runtime.history.directory / history_filename("-123.7")
    before = path.read_bytes()
    h.runtime.ops.delete_topic.side_effect = ConnectionError()
    response = await h.client.delete(
        f"{_PREFIX}/topics/-123.7", json={"confirm_name": "Topic #7"}, headers=headers()
    )
    assert response.status == 503
    assert path.read_bytes() == before
    assert await h.bot._orch._sessions.get_active(h.key) is not None
    assert len(await h.runtime.outbox.pending()) == 1
    assert (await post_message(h)).status == 202
    h.runtime.ops.create_topic.side_effect = ConnectionError()
    response = await h.client.post(f"{_PREFIX}/topics", json={"name": "new"}, headers=headers())
    assert response.status == 503


async def test_streaming_uses_same_lock_key_context_and_mirror_ids(conversation):
    h = conversation
    ws = await h.client.ws_connect(f"{_PREFIX}/events", headers=headers())
    response = await post_message(h)
    assert response.status == 202
    turn_id = (await response.json())["turn_id"]
    events = await receive_until(
        ws, lambda event: event.get("type") == "turn" and event.get("phase") == "done"
    )
    phases = [event["phase"] for event in events if event["type"] == "turn"]
    assert phases == ["queued", "started", "done"]
    assert any(event["type"] == "delta" and event["text"] == "answer" for event in events)
    assert any(event["type"] == "tool" and event["name"] == "Read" for event in events)
    streamed_entries = [event["entry"] for event in events if event["type"] == "entry"]
    assert streamed_entries[0]["mirror"] == {"state": "pending"}
    assert all("mirror" in entry for entry in streamed_entries)
    call = h.calls[0]
    assert call[0] == h.key
    assert call[0].chat_id == -123
    assert "request_transport" not in call[2]
    assert call[2]["include_partial_messages"] is True
    assert call[3] == "web"
    assert call[4]["turn_id"] == turn_id
    assert call[5] is not None
    entries = await h.runtime.history.read("-123.7")
    queued = await h.runtime.outbox.pending()
    assert [item.history_id for item in queued] == [entry.id for entry in entries]
    assert [item.kind for item in queued] == ["user_mirror", "reply_mirror"]
    assert all(entry.source == "web" and entry.turn_id == turn_id for entry in entries)
    await ws.close()


async def test_queued_turn_conflict_abort_and_stop_are_topic_scoped(conversation):
    h = conversation
    lock = h.pool.get(h.key.lock_key)
    await lock.acquire()
    ws = await h.client.ws_connect(f"{_PREFIX}/events", headers=headers())
    try:
        assert (await post_message(h)).status == 202
        await receive_until(ws, lambda event: event.get("phase") == "queued")
        response = await post_message(h)
        assert response.status == 409
        assert await response.json() == {"error": "web_turn_running"}
        response = await h.client.post(f"{_PREFIX}/topics/-123.7/abort", headers=headers())
        assert response.status == 200
        assert await response.json() == {"killed": 0}
        assert not h.calls
    finally:
        lock.release()
    response = await post_message(h, text=" /STOP ")
    assert response.status == 202
    assert await response.json() == {"killed": 0}
    assert not h.calls
    h.bot._orch.abort.assert_not_awaited()
    assert h.bot._orch._process_registry.kill_by_chat_topic.await_args_list[0].args == (-123, 7)
    await ws.close()


async def test_deleting_queued_turn_cannot_recreate_history(conversation):
    h = conversation
    lock = h.pool.get(h.key.lock_key)
    await lock.acquire()
    ws = await h.client.ws_connect(f"{_PREFIX}/events", headers=headers())
    assert (await post_message(h)).status == 202
    await receive_until(ws, lambda event: event.get("phase") == "queued")

    async def kill(_chat, _topic):
        lock.release()
        return 0

    h.bot._orch._process_registry.kill_by_chat_topic.side_effect = kill
    response = await h.client.delete(
        f"{_PREFIX}/topics/-123.7", json={"confirm_name": "Topic #7"}, headers=headers()
    )
    assert response.status == 200
    assert not h.calls
    assert not (h.runtime.history.directory / history_filename("-123.7")).exists()
    assert (await post_message(h)).status == 404
    assert await h.bot._orch._sessions.list_all() == []
    await ws.close()


async def test_history_pagination_mirror_join_and_search(conversation):
    h = conversation
    await h.runtime.history.append(
        "-123.7", Entry(role="bot", source="import", text="Find old", imported=True, seq=-1)
    )
    first = await h.runtime.history.append(
        "-123.7", Entry(role="user", source="web", text="find first")
    )
    second = await h.runtime.history.append(
        "-123.7", Entry(role="bot", source="web", text="FIND second")
    )
    await h.runtime.outbox.enqueue(
        kind="user_mirror", chat_id=-123, topic_id=7, text="first", history_id=first.id
    )
    item = await h.runtime.outbox.enqueue(
        kind="reply_mirror", chat_id=-123, topic_id=7, text="second", history_id=second.id
    )
    await h.runtime.outbox.mark_failed(item.id, "send_failed")
    response = await h.client.get(f"{_PREFIX}/topics/-123.7/messages?limit=2", headers=headers())
    body = await response.json()
    assert body["has_more"]
    assert [entry["seq"] for entry in body["entries"]] == [1, 2]
    assert [entry["mirror"] for entry in body["entries"]] == [
        {"state": "pending"},
        {"state": "failed"},
    ]
    response = await h.client.get(f"{_PREFIX}/topics/-123.7/messages?before=1", headers=headers())
    body = await response.json()
    assert not body["has_more"]
    assert [entry["seq"] for entry in body["entries"]] == [-1]
    response = await h.client.get(f"{_PREFIX}/search?q=FiNd", headers=headers())
    assert len((await response.json())["hits"]) == 3
    for query in ("limit=0", "limit=201", "before=x"):
        assert (
            await h.client.get(f"{_PREFIX}/topics/-123.7/messages?{query}", headers=headers())
        ).status == 400
    assert (await h.client.get(f"{_PREFIX}/search?q=", headers=headers())).status == 400


@pytest.mark.parametrize("status", ["ok", "error", "aborted", "raise", "missing"])
async def test_core_status_controls_mirroring_and_terminal_phase(conversation, status, caplog):
    h = conversation

    async def handle(key, _text, **callbacks):
        await callbacks["on_text_delta"]("partial")
        if status == "raise":
            raise RuntimeError("private provider failure text")
        turn = chat_log_turn.get()
        if status != "missing":
            await h.runtime.history.append(
                key,
                Entry(
                    role="bot",
                    source="web",
                    text="result",
                    id=turn["reply_entry_id"],
                    status=status,
                ),
            )
        return OrchestratorResult(text="result")

    h.bot._orch.handle_message_streaming.side_effect = handle
    ws = await h.client.ws_connect(f"{_PREFIX}/events", headers=headers())
    assert (await post_message(h)).status == 202
    expected = (
        "error" if status in {"error", "raise"} else "aborted" if status == "aborted" else "done"
    )
    await receive_until(
        ws, lambda event: event.get("type") == "turn" and event.get("phase") == expected
    )
    pending = await h.runtime.outbox.pending()
    assert len(pending) == (1 if expected == "error" else 2)
    if expected == "aborted":
        assert pending[-1].text.startswith("result\n\n")
    assert "private provider failure text" not in caplog.text
    if status == "missing":
        assert "reply_missing" in caplog.text
    await ws.close()


async def test_registry_abort_flags_override_ok_reply(conversation):
    h = conversation
    h.bot._orch._process_registry.was_aborted_topic = lambda chat, topic: (chat, topic) == (-123, 7)
    ws = await h.client.ws_connect(f"{_PREFIX}/events", headers=headers())
    assert (await post_message(h)).status == 202
    await receive_until(ws, lambda event: event.get("phase") == "aborted")
    assert (await h.runtime.outbox.pending())[-1].text.startswith("answer\n\n")
    await ws.close()


async def test_websocket_disconnect_does_not_cancel_turn(conversation):
    h = conversation
    started, release = asyncio.Event(), asyncio.Event()
    original = h.bot._orch.handle_message_streaming.side_effect

    async def handle(*args, **kwargs):
        started.set()
        await release.wait()
        return await original(*args, **kwargs)

    h.bot._orch.handle_message_streaming.side_effect = handle
    queue = h.runtime.events.subscribe()
    ws = await h.client.ws_connect(f"{_PREFIX}/events", headers=headers())
    assert (await post_message(h)).status == 202
    await asyncio.wait_for(started.wait(), 2)
    await ws.close()
    release.set()
    while True:
        event = await asyncio.wait_for(queue.get(), 2)
        if event.get("type") == "turn" and event.get("phase") == "done":
            break
    assert len(await h.runtime.history.read("-123.7")) == 2
    h.runtime.events.unsubscribe(queue)


async def test_events_ignore_client_frames_and_disconnect_on_overflow(conversation):
    h = conversation
    ws = await h.client.ws_connect(f"{_PREFIX}/events", headers=headers())
    await ws.send_json({"command": "must-be-ignored"})
    h.runtime.events.publish({"type": "topics"})
    assert await ws.receive_json(timeout=2) == {"type": "topics"}
    assert not h.calls
    for _ in range(1001):
        h.runtime.events.publish({"type": "topics"})
    message = await ws.receive(timeout=2)
    assert message.type == aiohttp.WSMsgType.CLOSE
    assert ws.close_code == 1013
    await h.client.close()
    assert not h.runtime.events._subscribers


async def test_delete_waits_for_telegram_finally_before_removing_history(conversation):
    h = conversation
    lock = h.pool.get(h.key.lock_key)
    await lock.acquire()
    release = asyncio.Event()

    async def telegram_turn():
        await release.wait()
        try:
            await h.runtime.history.append(
                "-123.7", Entry(role="bot", source="telegram", text="final reply")
            )
        finally:
            lock.release()

    task = asyncio.create_task(telegram_turn())

    async def kill(_chat, _topic):
        release.set()
        return 1

    h.bot._orch._process_registry.kill_by_chat_topic.side_effect = kill
    response = await h.client.delete(
        f"{_PREFIX}/topics/-123.7", json={"confirm_name": "Topic #7"}, headers=headers()
    )
    assert response.status == 200
    assert (await response.json())["history_entries"] == 1
    await task
    assert not (h.runtime.history.directory / history_filename("-123.7")).exists()


async def test_delete_lock_timeout_is_conflict_and_unblocks(conversation, monkeypatch):
    from ductor_bot.webchat import topics as module

    h = conversation
    original_timeout = asyncio.timeout

    def deadline(seconds):
        assert seconds == 30
        return original_timeout(0)

    monkeypatch.setattr(
        module,
        "asyncio",
        SimpleNamespace(to_thread=asyncio.to_thread, timeout=deadline, Lock=asyncio.Lock),
    )
    lock = h.pool.get(h.key.lock_key)
    await lock.acquire()
    try:
        response = await h.client.delete(
            f"{_PREFIX}/topics/-123.7", json={"confirm_name": "Topic #7"}, headers=headers()
        )
        assert response.status == 409
        assert await response.json() == {"error": "topic_busy"}
        h.runtime.ops.delete_topic.assert_not_awaited()
        assert await h.bot._orch._sessions.get_active(h.key) is not None
        assert (await post_message(h)).status == 202
    finally:
        lock.release()


async def test_other_topic_runs_while_delete_waits_for_lock(conversation):
    h = conversation
    await h.bot._orch._sessions.resolve_session(SessionKey.telegram(-123, 8))
    lock = h.pool.get(h.key.lock_key)
    await lock.acquire()
    entered = asyncio.Event()

    async def kill(_chat, _topic):
        entered.set()
        return 0

    h.bot._orch._process_registry.kill_by_chat_topic.side_effect = kill
    deleting = asyncio.create_task(
        h.client.delete(
            f"{_PREFIX}/topics/-123.7", json={"confirm_name": "Topic #7"}, headers=headers()
        )
    )
    await asyncio.wait_for(entered.wait(), 2)
    response = await asyncio.wait_for(post_message(h, tid="-123.8"), 2)
    assert response.status == 202
    lock.release()
    assert (await deleting).status == 200


async def test_delete_awaits_running_web_cleanup_and_suppresses_its_events(conversation):
    h = conversation
    started, finalized = asyncio.Event(), asyncio.Event()

    async def handle(key, text, **_callbacks):
        dispatch = SimpleNamespace(
            key=key, text=text, transport_override=None, counts_as_activity=True
        )
        await h.recorder.begin(dispatch)
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await h.recorder.finish(
                dispatch, OrchestratorResult(text="cleanup reply"), status="aborted"
            )
            finalized.set()

    h.bot._orch.handle_message_streaming.side_effect = handle
    ws = await h.client.ws_connect(f"{_PREFIX}/events", headers=headers())
    assert (await post_message(h)).status == 202
    await asyncio.wait_for(started.wait(), 2)
    response = await h.client.delete(
        f"{_PREFIX}/topics/-123.7", json={"confirm_name": "Topic #7"}, headers=headers()
    )
    assert response.status == 200
    assert finalized.is_set()
    assert (await response.json())["history_entries"] == 2
    assert not (h.runtime.history.directory / history_filename("-123.7")).exists()
    events = await receive_until(ws, lambda event: event["type"] == "topics")
    assert not any(event.get("entry", {}).get("text") == "cleanup reply" for event in events)
    assert not await h.runtime.outbox.pending()
    await ws.close()


async def test_false_delete_confirmation_preserves_local_data(conversation):
    h = conversation
    h.runtime.ops.delete_topic.return_value = False
    response = await h.client.delete(
        f"{_PREFIX}/topics/-123.7", json={"confirm_name": "Topic #7"}, headers=headers()
    )
    assert response.status == 503
    assert await h.bot._orch._sessions.get_active(h.key) is not None
    assert (await post_message(h)).status == 202


@pytest.mark.parametrize(
    "data", [{}, {"text": ""}, {"text": " "}, {"text": 42}, {"text": "x" * 32001}]
)
async def test_message_input_validation(conversation, data):
    response = await conversation.client.post(
        f"{_PREFIX}/topics/-123.7/messages", json={"client_id": "id", **data}, headers=headers()
    )
    assert response.status == 400
    assert not conversation.calls


async def test_missing_topics_and_confirmation_mismatch(conversation):
    h = conversation
    assert (await post_message(h, tid="no-such-topic")).status == 404
    response = await h.client.delete(
        f"{_PREFIX}/topics/-123.7", json={"confirm_name": "wrong"}, headers=headers()
    )
    assert response.status == 400
    assert await response.json() == {"error": "confirm_mismatch"}
    h.runtime.ops.delete_topic.assert_not_awaited()
    with pytest.raises(aiohttp.WSServerHandshakeError) as caught:
        await h.client.ws_connect(f"{_PREFIX}/events")
    assert caught.value.status == 403
    assert "WWW-Authenticate" not in caught.value.headers


async def test_telegram_topic_adapter_uses_forum_api_methods():
    from ductor_bot.messenger.telegram.webchat_ops import TelegramWebchatOps

    telegram = SimpleNamespace(
        create_forum_topic=AsyncMock(return_value=SimpleNamespace(message_thread_id=7)),
        edit_forum_topic=AsyncMock(),
        delete_forum_topic=AsyncMock(return_value=True),
    )
    ops = TelegramWebchatOps(SimpleNamespace(bot_instance=telegram))
    assert await ops.create_topic(-123, "Created") == 7
    await ops.rename_topic(-123, 7, "Renamed")
    assert await ops.delete_topic(-123, 7)
    telegram.create_forum_topic.assert_awaited_once_with(chat_id=-123, name="Created")
    telegram.edit_forum_topic.assert_awaited_once_with(
        chat_id=-123, message_thread_id=7, name="Renamed"
    )
    telegram.delete_forum_topic.assert_awaited_once_with(chat_id=-123, message_thread_id=7)


@pytest.mark.parametrize("error_type", [OSError, RuntimeError])
async def test_recording_failure_falls_back_to_ok_without_body_log(
    conversation, monkeypatch, caplog, error_type
):
    h = conversation
    turns = WebTurns(h.bot, h.runtime)

    def unreadable(_path):
        raise error_type("private content must not be logged")

    monkeypatch.setattr(h.runtime.history, "_read", unreadable)
    assert await turns._status("-123.7", "reply-id") == "ok"
    assert f"session=tg:-123:7 reason={error_type.__name__}" in caplog.text
    assert "private content" not in caplog.text


@pytest.mark.parametrize("storage", ["history", "session"])
async def test_cancelled_turn_storage_worker_finishes_before_topic_delete(
    conversation, monkeypatch, storage
):
    from ductor_bot.session import manager as session_module

    h = conversation
    entered, release, completed = threading.Event(), threading.Event(), threading.Event()
    killed = asyncio.Event()
    calls = 0
    original = (
        h.runtime.history._append if storage == "history" else session_module.atomic_json_save
    )

    def delayed(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            release.wait()
        result = original(*args, **kwargs)
        if calls == 1:
            completed.set()
        return result

    if storage == "history":
        monkeypatch.setattr(h.runtime.history, "_append", delayed)
    else:
        monkeypatch.setattr(session_module, "atomic_json_save", delayed)

    async def handle(key, text, **_callbacks):
        dispatch = SimpleNamespace(
            key=key, text=text, transport_override=None, counts_as_activity=True
        )
        try:
            await h.recorder.begin(dispatch)
            session = await h.bot._orch._sessions.get_active(key)
            await h.bot._orch._sessions.update_session(session)
        finally:
            await h.recorder.finish(dispatch, None, status="aborted")
        return OrchestratorResult(text="")

    async def kill(_chat, _topic):
        killed.set()
        return 0

    h.bot._orch.handle_message_streaming.side_effect = handle
    h.bot._orch._process_registry.kill_by_chat_topic.side_effect = kill
    deleting = None
    try:
        assert (await post_message(h)).status == 202
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait), 2)
        deleting = asyncio.create_task(
            h.client.delete(
                f"{_PREFIX}/topics/-123.7", json={"confirm_name": "Topic #7"}, headers=headers()
            )
        )
        await asyncio.wait_for(killed.wait(), 2)
        await asyncio.sleep(0)
        storage_lock = (
            h.runtime.history._lock("-123.7")
            if storage == "history"
            else h.bot._orch._sessions._lock
        )
        assert storage_lock.locked()
        assert not deleting.done()
    finally:
        release.set()
        if deleting is not None:
            response = await deleting
            assert response.status == 200
    assert completed.is_set()
    assert await h.bot._orch._sessions.get_active(h.key) is None
    assert not (h.runtime.history.directory / history_filename("-123.7")).exists()


async def test_cancelled_outbox_worker_finishes_before_drop(conversation, monkeypatch):
    h = conversation
    entered, release = threading.Event(), threading.Event()
    original = h.runtime.outbox._enqueue

    def delayed(item):
        entered.set()
        release.wait()
        original(item)

    monkeypatch.setattr(h.runtime.outbox, "_enqueue", delayed)
    writing = asyncio.create_task(
        h.runtime.outbox.enqueue(kind="user_mirror", chat_id=-123, topic_id=7, text="pending")
    )
    dropping = None
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait), 2)
        writing.cancel()
        await asyncio.sleep(0)
        assert not writing.done()
        assert h.runtime.outbox._lock.locked()
        # A second cancellation must not release the lock while the worker runs.
        writing.cancel()
        await asyncio.sleep(0)
        assert not writing.done()
        dropping = asyncio.create_task(h.runtime.outbox.drop_tid("-123.7"))
        await asyncio.sleep(0)
        assert not dropping.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await writing
        if dropping is not None:
            assert await dropping == 1
    assert not await h.runtime.outbox.pending()
    done = list((h.runtime.outbox.directory / "done").glob("*.json"))
    assert len(done) == 1
    assert h.runtime.outbox._load(done[0]).state == "dropped"


def seed_editor_files(harness, *, memory=b"personal memory\n"):
    paths = harness.paths
    files = {
        "PERSONA.md": paths.workspace / "PERSONA.md",
        "BEHAVIOR.md": paths.workspace / "BEHAVIOR.md",
        "MAINMEMORY.md": paths.mainmemory_path,
        "SHAREDMEMORY.md": paths.ductor_home / "SHAREDMEMORY.md",
    }
    for name, path in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(memory if name == "MAINMEMORY.md" else f"original {name}\r\n".encode())
    return files


async def put_file(harness, name, content, base):
    return await harness.client.put(
        f"{_PREFIX}/files/{name}",
        data=json.dumps({"content": content, "base_sha256": base}, ensure_ascii=False).encode(),
        headers={**headers(), "Content-Type": "application/json"},
    )


async def test_editor_listing_and_get_use_configured_paths(conversation):
    h = conversation
    paths = seed_editor_files(h)
    (h.paths.workspace / "OTHER.md").write_text("not editable")
    response = await h.client.get(f"{_PREFIX}/files", headers=headers())
    assert response.status == 200
    rows = (await response.json())["files"]
    assert {row["name"] for row in rows} == set(paths)
    for row in rows:
        original = paths[row["name"]].read_bytes()
        assert row["sha256"] == hashlib.sha256(original).hexdigest()
        assert row["size"] == len(original)
        assert row["mtime"].endswith("Z")
        assert datetime.fromisoformat(row["mtime"]).tzinfo == UTC
        response = await h.client.get(f"{_PREFIX}/files/{row['name']}", headers=headers())
        body = await response.json()
        assert body == {
            "name": row["name"],
            "content": original.decode(),
            "sha256": row["sha256"],
            "shared_block": False,
        }


@pytest.mark.parametrize("name", ["PERSONA.md", "BEHAVIOR.md", "MAINMEMORY.md", "SHAREDMEMORY.md"])
async def test_file_save_backups_original_bytes_and_returns_actual_hash(conversation, name):
    h = conversation
    paths = seed_editor_files(h)
    original = paths[name].read_bytes()
    content = "  new content\n\n"
    response = await put_file(h, name, content, hashlib.sha256(original).hexdigest())
    assert response.status == 200
    body = await response.json()
    actual = paths[name].read_bytes()
    expected = b"new content\n" if name == "SHAREDMEMORY.md" else content.encode()
    if name == "MAINMEMORY.md":
        expected = (
            b"  new content\n\n--- SHARED KNOWLEDGE START ---\n"
            b"original SHAREDMEMORY.md\n--- SHARED KNOWLEDGE END ---\n"
        )
    assert actual == expected
    assert body["sha256"] == hashlib.sha256(actual).hexdigest()
    assert body["backup"].startswith("backups/")
    assert (h.paths.webchat_dir / body["backup"]).read_bytes() == original
    assert str(h.paths.ductor_home) not in body["backup"]


@pytest.mark.parametrize(
    "markers",
    [
        ("--- SHARED KNOWLEDGE START ---", "--- SHARED KNOWLEDGE END ---"),
        ("<!-- SHARED:START -->", "<!-- SHARED:END -->"),
    ],
)
async def test_mainmemory_hides_and_preserves_shared_block(conversation, markers):
    h = conversation
    start, end = markers
    block = f"{start}\nshared content\n{end}"
    original = f"head\n\n{block}\n\ntail\n".encode()
    paths = seed_editor_files(h, memory=original)
    response = await h.client.get(f"{_PREFIX}/files/MAINMEMORY.md", headers=headers())
    data = await response.json()
    assert data["shared_block"]
    assert "shared content" not in data["content"]
    assert "head" in data["content"]
    assert "tail" in data["content"]
    assert data["sha256"] == hashlib.sha256(original).hexdigest()
    response = await put_file(h, "MAINMEMORY.md", "edited personal\n\n", data["sha256"])
    assert response.status == 200
    expected_block = (
        "--- SHARED KNOWLEDGE START ---\noriginal SHAREDMEMORY.md\n--- SHARED KNOWLEDGE END ---"
    )
    assert paths["MAINMEMORY.md"].read_text() == f"edited personal\n\n{expected_block}\n"
    assert (h.paths.webchat_dir / (await response.json())["backup"]).read_bytes() == original


async def test_all_complete_shared_blocks_are_preserved(conversation):
    h = conversation
    blocks = [
        "--- SHARED KNOWLEDGE START ---\nfirst\n--- SHARED KNOWLEDGE END ---",
        "<!-- SHARED:START -->\nsecond\n<!-- SHARED:END -->",
    ]
    original = ("before\n" + "\nmiddle\n".join(blocks) + "\nafter").encode()
    paths = seed_editor_files(h, memory=original)
    response = await h.client.get(f"{_PREFIX}/files/MAINMEMORY.md", headers=headers())
    body = await response.json()
    assert "first" not in body["content"]
    assert "second" not in body["content"]
    assert "middle" in body["content"]
    response = await put_file(h, "MAINMEMORY.md", "personal", body["sha256"])
    assert response.status == 200
    expected_block = (
        "--- SHARED KNOWLEDGE START ---\noriginal SHAREDMEMORY.md\n--- SHARED KNOWLEDGE END ---"
    )
    assert paths["MAINMEMORY.md"].read_text() == f"personal\n\n{expected_block}\n\n{blocks[1]}\n"


@pytest.mark.parametrize(
    "text",
    [
        "--- SHARED KNOWLEDGE START ---\ncut",
        "<!-- SHARED:END -->",
        "--- SHARED KNOWLEDGE START ---\n<!-- SHARED:END -->",
        "<!-- SHARED:START --><!-- SHARED:START --><!-- SHARED:END -->",
    ],
)
async def test_malformed_shared_blocks_are_not_edited(conversation, text):
    h = conversation
    paths = seed_editor_files(h, memory=text.encode())
    response = await h.client.get(f"{_PREFIX}/files/MAINMEMORY.md", headers=headers())
    assert response.status == 409
    assert await response.json() == {"error": "shared_block_invalid"}
    response = await put_file(
        h, "MAINMEMORY.md", "personal", hashlib.sha256(text.encode()).hexdigest()
    )
    assert response.status == 409
    assert paths["MAINMEMORY.md"].read_text() == text
    assert not (h.paths.webchat_dir / "backups").exists()


@pytest.mark.parametrize(
    "marker",
    [
        "--- SHARED KNOWLEDGE START ---",
        "--- SHARED KNOWLEDGE END ---",
        "<!-- SHARED:START -->",
        "<!-- SHARED:END -->",
    ],
)
@pytest.mark.parametrize("name", ["MAINMEMORY.md", "SHAREDMEMORY.md"])
async def test_memory_rejects_injected_shared_markers(conversation, marker, name):
    h = conversation
    paths = seed_editor_files(h)
    original = paths[name].read_bytes()
    response = await put_file(h, name, marker, hashlib.sha256(original).hexdigest())
    assert response.status == 400
    assert await response.json() == {"error": "bad_request"}
    assert paths[name].read_bytes() == original
    assert not (h.paths.webchat_dir / "backups").exists()


async def test_file_conflicts_are_read_only_and_concurrent_saves_serialize(conversation):
    h = conversation
    paths = seed_editor_files(h)
    original = paths["PERSONA.md"].read_bytes()
    response = await put_file(h, "PERSONA.md", "must not save", "wrong")
    assert response.status == 409
    assert await response.json() == {"error": "conflict"}
    assert paths["PERSONA.md"].read_bytes() == original
    assert not (h.paths.webchat_dir / "backups").exists()
    base = hashlib.sha256(original).hexdigest()
    results = await asyncio.gather(
        put_file(h, "PERSONA.md", "first", base), put_file(h, "PERSONA.md", "second", base)
    )
    assert sorted(response.status for response in results) == [200, 409]
    assert paths["PERSONA.md"].read_text() in {"first", "second"}
    assert len(list((h.paths.webchat_dir / "backups").rglob("PERSONA.md.*"))) == 1


@pytest.mark.parametrize(
    ("content", "status"), [("x" * 262144, 200), ("é" * 131072, 200), ("é" * 131073, 413)]
)
async def test_file_limit_counts_utf8_bytes(conversation, content, status):
    h = conversation
    paths = seed_editor_files(h)
    original = paths["PERSONA.md"].read_bytes()
    response = await put_file(h, "PERSONA.md", content, hashlib.sha256(original).hexdigest())
    assert response.status == status
    if status == 413:
        assert await response.json() == {"error": "too_large"}
        assert paths["PERSONA.md"].read_bytes() == original
        assert not (h.paths.webchat_dir / "backups").exists()


async def test_reassembled_shared_block_also_obeys_file_limit(conversation):
    h = conversation
    original = (
        b"--- SHARED KNOWLEDGE START ---\n" + b"s" * 262144 + b"\n--- SHARED KNOWLEDGE END ---"
    )
    paths = seed_editor_files(h, memory=original)
    paths["SHAREDMEMORY.md"].write_bytes(b"s" * 262144)
    response = await put_file(
        h, "MAINMEMORY.md", "small personal", hashlib.sha256(original).hexdigest()
    )
    assert response.status == 413
    assert await response.json() == {"error": "too_large"}
    assert paths["MAINMEMORY.md"].read_bytes() == original
    assert not (h.paths.webchat_dir / "backups").exists()


async def test_large_json_body_is_a_json_size_error(conversation):
    h = conversation
    paths = seed_editor_files(h)
    response = await put_file(
        h, "PERSONA.md", '"' * 262144, hashlib.sha256(paths["PERSONA.md"].read_bytes()).hexdigest()
    )
    assert response.status == 413
    assert await response.json() == {"error": "too_large"}


async def test_backup_names_use_kst_and_handle_same_second(conversation, monkeypatch):
    from ductor_bot.webchat import files as module

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 1, 1, 15, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr(module, "datetime", Frozen)
    h = conversation
    paths = seed_editor_files(h)
    originals, backups = [], []
    for content in ("one", "two", "three"):
        original = paths["PERSONA.md"].read_bytes()
        originals.append(original)
        response = await put_file(h, "PERSONA.md", content, hashlib.sha256(original).hexdigest())
        assert response.status == 200
        backups.append((await response.json())["backup"])
    assert backups == [
        "backups/2026-01-02/PERSONA.md.000000",
        "backups/2026-01-02/PERSONA.md.000000.1",
        "backups/2026-01-02/PERSONA.md.000000.2",
    ]
    assert [(h.paths.webchat_dir / backup).read_bytes() for backup in backups] == originals


@pytest.mark.parametrize("directory", ["workspace", "memory_system"])
async def test_internal_directory_symlinks_work(conversation, directory):
    h = conversation
    seed_editor_files(h)
    logical = h.paths.workspace if directory == "workspace" else h.paths.memory_system_dir
    target = h.paths.ductor_home / f"real-{directory}"
    logical.rename(target)
    logical.symlink_to(target, target_is_directory=True)
    name = "PERSONA.md" if directory == "workspace" else "MAINMEMORY.md"
    response = await h.client.get(f"{_PREFIX}/files/{name}", headers=headers())
    assert response.status == 200
    base = (await response.json())["sha256"]
    response = await put_file(h, name, "updated", base)
    assert response.status == 200
    expected = "updated"
    if name == "MAINMEMORY.md":
        expected += (
            "\n\n--- SHARED KNOWLEDGE START ---\noriginal SHAREDMEMORY.md\n"
            "--- SHARED KNOWLEDGE END ---\n"
        )
    assert (target / name).read_text() == expected
    assert logical.is_symlink()


@pytest.mark.parametrize("target_name", ["secrets.env", "MAINMEMORY.md", "other-persona.md"])
async def test_file_aliases_cannot_bypass_whitelist_or_shared_protection(conversation, target_name):
    h = conversation
    paths = seed_editor_files(h)
    target = (
        paths["MAINMEMORY.md"]
        if target_name == "MAINMEMORY.md"
        else h.paths.ductor_home / target_name
    )
    original = b"private sentinel content"
    target.write_bytes(original)
    paths["PERSONA.md"].unlink()
    paths["PERSONA.md"].symlink_to(target)
    response = await h.client.get(f"{_PREFIX}/files/PERSONA.md", headers=headers())
    assert response.status == 404
    assert "private sentinel" not in await response.text()
    response = await put_file(
        h, "PERSONA.md", "must not overwrite", hashlib.sha256(original).hexdigest()
    )
    assert response.status == 404
    assert target.read_bytes() == original
    response = await h.client.get(f"{_PREFIX}/files", headers=headers())
    assert "PERSONA.md" not in {row["name"] for row in (await response.json())["files"]}


async def test_directory_links_outside_home_are_not_editable(conversation):
    h = conversation
    seed_editor_files(h)
    target = h.paths.ductor_home.parent / f"{h.paths.ductor_home.name}-outside"
    h.paths.workspace.rename(target)
    h.paths.workspace.symlink_to(target, target_is_directory=True)
    for name in ("PERSONA.md", "BEHAVIOR.md", "MAINMEMORY.md"):
        assert (await h.client.get(f"{_PREFIX}/files/{name}", headers=headers())).status == 404
        assert (await put_file(h, name, "blocked", "base")).status == 404
    response = await h.client.get(f"{_PREFIX}/files", headers=headers())
    assert {row["name"] for row in (await response.json())["files"]} == {"SHAREDMEMORY.md"}


@pytest.mark.parametrize(
    "name",
    [
        "OTHER.md",
        "config.json",
        "secrets.env",
        "..%2fsecrets.env",
        "%2fetc%2fpasswd",
        "..%5csecrets.env",
    ],
)
async def test_only_four_exact_names_are_allowed(conversation, name):
    h = conversation
    seed_editor_files(h)
    assert (await h.client.get(f"{_PREFIX}/files/{name}", headers=headers())).status == 404
    assert (await put_file(h, name, "blocked", "base")).status == 404


async def test_missing_files_are_omitted_and_not_created(conversation):
    h = conversation
    paths = seed_editor_files(h)
    paths["PERSONA.md"].unlink()
    assert (await h.client.get(f"{_PREFIX}/files/PERSONA.md", headers=headers())).status == 404
    assert (await put_file(h, "PERSONA.md", "new", "base")).status == 404
    response = await h.client.get(f"{_PREFIX}/files", headers=headers())
    assert len((await response.json())["files"]) == 3
    assert not paths["PERSONA.md"].exists()


async def test_shared_file_save_matches_existing_sync_format_and_invalidates_hash(conversation):
    h = conversation
    original = (
        b"personal\n\n--- SHARED KNOWLEDGE START ---\nold shared\n--- SHARED KNOWLEDGE END ---\n"
    )
    paths = seed_editor_files(h, memory=original)
    old_shared = paths["SHAREDMEMORY.md"].read_bytes()
    response = await put_file(
        h, "SHAREDMEMORY.md", "  updated shared\n\n", hashlib.sha256(old_shared).hexdigest()
    )
    assert response.status == 200
    assert paths["SHAREDMEMORY.md"].read_bytes() == b"updated shared\n"
    assert _sync_agent_io(paths["SHAREDMEMORY.md"], paths["MAINMEMORY.md"])
    assert "updated shared" in paths["MAINMEMORY.md"].read_text()
    response = await h.client.get(f"{_PREFIX}/files/MAINMEMORY.md", headers=headers())
    body = await response.json()
    assert body["shared_block"]
    assert "updated shared" not in body["content"]
    assert body["sha256"] != hashlib.sha256(original).hexdigest()
    response = await put_file(
        h, "MAINMEMORY.md", "stale edit", hashlib.sha256(original).hexdigest()
    )
    assert response.status == 409
    assert "updated shared" in paths["MAINMEMORY.md"].read_text()


async def test_external_edit_during_backup_is_detected_before_replace(conversation, monkeypatch):
    from ductor_bot.webchat import files as module

    h = conversation
    paths = seed_editor_files(h)
    path = paths["PERSONA.md"]
    original = path.read_bytes()
    save = module.atomic_bytes_save

    def external_edit(backup, content):
        save(backup, content)
        if backup != path:
            path.write_bytes(b"external update")

    monkeypatch.setattr(module, "atomic_bytes_save", external_edit)
    response = await put_file(h, "PERSONA.md", "stale edit", hashlib.sha256(original).hexdigest())
    assert response.status == 409
    assert path.read_bytes() == b"external update"
    assert not list((h.paths.webchat_dir / "backups").rglob("PERSONA.md.*"))


async def test_cancelled_file_write_keeps_lock_until_committed(conversation, monkeypatch):
    h = conversation
    paths = seed_editor_files(h)
    store = WebFiles(h.paths)
    base = hashlib.sha256(paths["PERSONA.md"].read_bytes()).hexdigest()
    entered, release = threading.Event(), threading.Event()
    original = store._put

    def delayed(*args):
        entered.set()
        release.wait()
        return original(*args)

    monkeypatch.setattr(store, "_put", delayed)
    writing = asyncio.create_task(store.put("PERSONA.md", "committed", base))
    second = None
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait), 2)
        writing.cancel()
        await asyncio.sleep(0)
        assert not writing.done()
        assert store._locks["PERSONA.md"].locked()
        second = asyncio.create_task(store.put("PERSONA.md", "stale", base))
        await asyncio.sleep(0)
        assert not second.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await writing
        if second is not None:
            with pytest.raises(WebFileError) as caught:
                await second
            assert caught.value.status == 409
    assert paths["PERSONA.md"].read_text() == "committed"


async def test_outbox_view_includes_bus_metadata_without_internal_injection(conversation):
    h = conversation
    await h.runtime.outbox.enqueue(kind="user_mirror", chat_id=-123, topic_id=7, text="web input")
    await h.runtime.outbox.enqueue(kind="reply_mirror", chat_id=-123, topic_id=7, text="a" * 100)
    bot = MagicMock()
    bot.file_roots.return_value = [h.paths.workspace]
    bot._config.allowed_user_ids = [-123]
    bot.bot_instance.send_message = AsyncMock()
    transport = TelegramTransport(bot)
    transport.set_outbox(h.runtime.outbox)
    await transport.deliver(
        Envelope(
            origin=Origin.TASK_RESULT,
            chat_id=-123,
            topic_id=7,
            prompt="INTERNAL_PROMPT_DO_NOT_SHOW",
            result_text="public result",
            needs_injection=True,
            status="done",
            metadata={"name": "Visible task"},
        )
    )
    await transport.deliver(
        Envelope(
            origin=Origin.TASK_QUESTION,
            chat_id=-123,
            topic_id=7,
            prompt="PUBLIC_QUESTION",
            result_text="public answer",
            metadata={"task_id": "q"},
        )
    )
    response = await h.client.get(f"{_PREFIX}/outbox", headers=headers())
    assert response.status == 200
    data = await response.json()
    assert "INTERNAL_PROMPT_DO_NOT_SHOW" not in json.dumps(data)
    rows = data["items"]
    assert {row["kind"] for row in rows} == {"user_mirror", "reply_mirror", "bus"}
    assert any(row["origin"] == "task_result" and row["title"] == "Visible task" for row in rows)
    assert any("PUBLIC_QUESTION" in row["preview"] for row in rows)
    assert all(len(row["preview"]) <= 80 for row in rows)
    assert all(
        set(row)
        == {"id", "kind", "origin", "title", "tid", "preview", "created", "state", "last_error"}
        for row in rows
    )
    bot.bot_instance.send_message.assert_not_awaited()


async def test_outbox_view_only_pending_and_recent_failed(conversation):
    h = conversation
    pending = await h.runtime.outbox.enqueue(kind="user_mirror", chat_id=-123, text="pending")
    failed = await h.runtime.outbox.enqueue(
        kind="bus", chat_id=-123, text="failed", origin="cron", title="Cron"
    )
    await h.runtime.outbox.mark_failed(failed.id)
    old = await h.runtime.outbox.enqueue(kind="bus", chat_id=-123, text="old")
    await h.runtime.outbox.mark_failed(old.id)
    old_path = next((h.runtime.outbox.directory / "done").glob(f"*-{old.id}.json"))
    data = json.loads(old_path.read_text())
    data["updated"] = "2000-01-01T00:00:00+00:00"
    old_path.write_text(json.dumps(data))
    sent = await h.runtime.outbox.enqueue(kind="reply_mirror", chat_id=-123, text="sent")
    await h.runtime.outbox.mark_sent(sent.id)
    response = await h.client.get(f"{_PREFIX}/outbox", headers=headers())
    assert {row["id"] for row in (await response.json())["items"]} == {pending.id, failed.id}


@pytest.mark.parametrize("cancelled", [False, True])
async def test_abort_before_first_text_records_one_empty_reply(conversation, caplog, cancelled):
    h = conversation

    async def handle(key, text, **_callbacks):
        dispatch = SimpleNamespace(
            key=key, text=text, transport_override=None, counts_as_activity=True
        )
        await h.recorder.begin(dispatch)
        if cancelled:
            await h.recorder.finish(dispatch, None, status="aborted")
            raise asyncio.CancelledError
        h.bot._orch._process_registry.was_aborted_topic = lambda *_args: True
        result = OrchestratorResult(text="")
        await h.recorder.finish(dispatch, result, status="aborted")
        return result

    h.bot._orch.handle_message_streaming.side_effect = handle
    ws = await h.client.ws_connect(f"{_PREFIX}/events", headers=headers())
    response = await post_message(h)
    turn_id = (await response.json())["turn_id"]
    events = await receive_until(ws, lambda event: event.get("phase") == "aborted")
    replies = [entry for entry in await h.runtime.history.read("-123.7") if entry.role == "bot"]
    assert len(replies) == 1
    assert replies[0].status == "aborted"
    assert replies[0].text == ""
    assert replies[0].turn_id == turn_id
    assert len([event for event in events if event.get("entry", {}).get("role") == "bot"]) == 1
    assert [item.kind for item in await h.runtime.outbox.pending()] == ["user_mirror"]
    assert "reply_missing" not in caplog.text
    await ws.close()


async def test_claude_partial_deltas_reach_web_events_without_completed_text_duplicate(
    conversation, monkeypatch
):
    h = conversation
    fixture = Path(__file__).parents[1] / "cli" / "fixtures" / "claude_partial_messages.jsonl"
    lines = fixture.read_text().splitlines()
    monkeypatch.setattr("ductor_bot.cli.claude_provider.which", lambda _name: "/usr/bin/claude")

    async def subprocess_stream(*, spec, line_handler, **_kwargs):
        assert "--include-partial-messages" in spec.exec_cmd
        for line in lines:
            async for event in line_handler(line):
                yield event

    monkeypatch.setattr(
        "ductor_bot.cli.claude_provider.run_streaming_subprocess", subprocess_stream
    )

    async def handle(key, text, **callbacks):
        dispatch = SimpleNamespace(
            key=key, text=text, transport_override=None, counts_as_activity=True
        )
        await h.recorder.begin(dispatch)
        cli = ClaudeCodeCLI(
            CLIConfig(
                working_dir=h.paths.workspace,
                ductor_home=h.paths.ductor_home,
                include_partial_messages=callbacks["include_partial_messages"],
            )
        )
        final = ""
        async for event in cli.send_streaming(text):
            if isinstance(event, AssistantTextDelta):
                await callbacks["on_text_delta"](event.text)
            elif isinstance(event, ResultEvent):
                final = event.result
        result = OrchestratorResult(text=final)
        await h.recorder.finish(dispatch, result)
        return result

    h.bot._orch.handle_message_streaming.side_effect = handle
    ws = await h.client.ws_connect(f"{_PREFIX}/events", headers=headers())
    assert (await post_message(h)).status == 202
    events = await receive_until(ws, lambda event: event.get("phase") == "done")
    deltas = [event["text"] for event in events if event["type"] == "delta"]
    assert len(deltas) == 3
    replies = [entry for entry in await h.runtime.history.read("-123.7") if entry.role == "bot"]
    assert len(replies) == 1
    assert "".join(deltas) == replies[0].text
    mirrors = [item for item in await h.runtime.outbox.pending() if item.kind == "reply_mirror"]
    assert len(mirrors) == 1
    assert mirrors[0].text == replies[0].text
    await ws.close()


@pytest.mark.parametrize(
    "action",
    [
        "set-password",
        "reset-totp",
        "clear-credentials",
        "reenroll",
        "missing-state",
        "corrupt-state",
    ],
)
async def test_review_web_cookie_revoked_after_credential_change_and_restart(
    bridge, aiohttp_client, monkeypatch, action
):
    import time

    from ductor_bot.webchat import cli
    from ductor_bot.webchat.auth import TOTP_KEY
    from ductor_bot.webchat.web.app import STATE, create_app

    upstream, bot, runtime, paths = bridge
    runtime.events = EventHub()
    paths.workspace.mkdir(parents=True, exist_ok=True)
    (paths.workspace / "PERSONA.md").write_text("Test persona")
    store = paths.ductor_home / "secrets.env"
    seed = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    set_secret(store, PASSWORD_KEY, hash_password(_PASSWORD))
    set_secret(store, TOTP_KEY, seed)
    state_path = paths.webchat_dir / "auth_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "seed_fingerprint": hashlib.sha256(seed.encode()).hexdigest(),
                "last_step": int(time.time() // 30) - 2,
                "source": "login",
            }
        )
    )
    app = create_app(
        bridge_url=str(upstream.server.make_url("")),
        bridge_token=_TOKEN,
        public_origin="https://chat.example.com",
    )
    client = await aiohttp_client(app)
    pre = await client.post(
        "/api/auth/password", json={"password": _PASSWORD}, headers={"X-Dtw-Client": "web"}
    )
    response = await client.post(
        "/api/auth/totp",
        json={"code": totp_code(seed, int(time.time() // 30))},
        headers={"X-Dtw-Client": "web", "Cookie": "dtw_pre=" + pre.cookies["dtw_pre"].value},
    )
    assert response.status == 200
    cookie = {"Cookie": "dtw_session=" + response.cookies["dtw_session"].value}
    state = await (await client.get("/api/auth/state", headers=cookie)).json()
    assert state["authenticated"]
    assert "credential_generation" not in state
    assert "credential_generation" not in await response.text()
    assert (await client.get("/api/files/PERSONA.md", headers=cookie)).status == 200
    saved = app[STATE].sessions.get(response.cookies["dtw_session"].value)
    generation = saved.credential_generation
    assert generation not in await response.text()
    assert generation not in str(response.cookies)
    assert generation not in json.dumps(state)
    monkeypatch.setattr(cli, "resolve_paths", lambda: paths)
    monkeypatch.setattr(cli, "store_path", lambda _home: store)
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt: "changed-password-123")
    if action == "missing-state":
        state_path.unlink()
    elif action == "corrupt-state":
        state_path.write_text("{broken")
    else:
        cli.cmd_webchat(["reset-totp" if action == "reenroll" else action])
    if action == "reenroll":
        pre = await upstream.post(
            f"{_PREFIX}/auth/password",
            headers=headers(),
            json={"password": _PASSWORD, "client_ip": "test-client"},
        )
        preauth = (await pre.json())["preauth"]
        assert (
            await upstream.post(
                f"{_PREFIX}/auth/setup/begin",
                headers=headers(),
                json={"preauth": preauth, "mode": "paste", "secret": seed},
            )
        ).status == 200
        assert (
            await upstream.post(
                f"{_PREFIX}/auth/setup/confirm",
                headers=headers(),
                json={
                    "preauth": preauth,
                    "code": totp_code(seed, int(time.time() // 30)),
                    "client_ip": "test-client",
                },
            )
        ).status == 200
    # Recreate the bot-side bridge while retaining the web container and cookie.
    restarted = await aiohttp_client(create_bridge_app(bot, runtime, _TOKEN))
    app[STATE].bridge._url = str(restarted.server.make_url(_PREFIX))
    denied = await client.get("/api/files/PERSONA.md", headers=cookie)
    assert denied.status == 403
    assert await denied.json() == {"error": "auth_required"}
    assert app[STATE].sessions.count() == 0
    denied_write = await client.put(
        "/api/files/PERSONA.md",
        json={"content": "changed", "base_sha256": "unused"},
        headers={**cookie, "X-Dtw-Client": "web", "X-Dtw-Csrf": state["csrf"]},
    )
    assert denied_write.status == 403
    assert (paths.workspace / "PERSONA.md").read_text() == "Test persona"
    logged_out = await (await client.get("/api/auth/state", headers=cookie)).json()
    assert logged_out["authenticated"] is False
    assert logged_out["csrf"] is None


async def test_abort_during_preparation_prevents_provider_start(conversation):
    h = conversation
    preparing, release = asyncio.Event(), asyncio.Event()
    original = h.bot._orch.handle_message_streaming.side_effect

    async def prepare(*args, **kwargs):
        preparing.set()
        await release.wait()
        return await original(*args, **kwargs)

    h.bot._orch.handle_message_streaming.side_effect = prepare
    ws = await h.client.ws_connect(f"{_PREFIX}/events", headers=headers())
    assert (await post_message(h)).status == 202
    await preparing.wait()
    response = await h.client.post(f"{_PREFIX}/topics/-123.7/abort", headers=headers())
    assert response.status == 200
    assert await response.json() == {"killed": 0}
    release.set()
    events = await receive_until(ws, lambda event: event.get("phase") in {"done", "aborted"})
    assert events[-1]["phase"] == "aborted"
    assert not h.calls
    replies = [entry for entry in await h.runtime.history.read("-123.7") if entry.role == "bot"]
    assert len(replies) == 1
    assert replies[0].status == "aborted"
    await ws.close()


async def test_shutdown_preserves_completed_result_while_status_is_loading(conversation):
    h = conversation
    turns = WebTurns(h.bot, h.runtime)
    loading, release = asyncio.Event(), asyncio.Event()
    original = turns._status

    async def status(*args, **kwargs):
        if not kwargs.get("aborted"):
            loading.set()
            await release.wait()
        return await original(*args, **kwargs)

    turns._status = status
    queue = h.runtime.events.subscribe()
    turns.submit("-123.7", "hello")
    await loading.wait()
    stopping = asyncio.create_task(turns.stop())
    await asyncio.sleep(0)
    release.set()
    await stopping
    mirrors = [item for item in await h.runtime.outbox.pending() if item.kind == "reply_mirror"]
    assert [item.text for item in mirrors] == ["answer"]
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    assert [event["phase"] for event in events if event["type"] == "turn"][-1] == "done"
    h.runtime.events.unsubscribe(queue)


@pytest.mark.parametrize("started", [False, True])
async def test_topics_snapshot_exposes_active_web_turn(conversation, started):
    h = conversation
    lock = h.pool.get(h.key.lock_key)
    release = asyncio.Event()
    preparing = asyncio.Event()

    async def handle(*_args, **_kwargs):
        preparing.set()
        await release.wait()
        return OrchestratorResult(text="")

    if not started:
        await lock.acquire()
    h.bot._orch.handle_message_streaming.side_effect = handle
    try:
        response = await post_message(h)
        turn_id = (await response.json())["turn_id"]
        if started:
            await preparing.wait()
        response = await h.client.get(f"{_PREFIX}/topics", headers=headers())
        topic = next(item for item in (await response.json())["topics"] if item["tid"] == "-123.7")
        assert topic["busy"]
        assert topic["turn_id"] == turn_id
    finally:
        if not started:
            lock.release()
        release.set()


@pytest.mark.parametrize(
    "message", ["Bad Request: TOPIC_ID_INVALID", "Bad Request: message thread not found"]
)
async def test_already_deleted_telegram_topic_is_removed_locally(conversation, message):
    h = conversation
    row = await h.runtime.history.append("-123.7", Entry(role="user", source="web", text="saved"))
    await h.runtime.outbox.enqueue(
        kind="user_mirror", chat_id=-123, topic_id=7, text="pending", history_id=row.id
    )
    h.runtime.ops.delete_topic.side_effect = TelegramBadRequest(
        method=DeleteForumTopic(chat_id=-123, message_thread_id=7), message=message
    )
    response = await h.client.delete(
        f"{_PREFIX}/topics/-123.7", json={"confirm_name": "Topic #7"}, headers=headers()
    )
    assert response.status == 200
    assert (await response.json())["telegram_deleted"] is True
    assert await h.bot._orch._sessions.get_active(h.key) is None
    assert not (h.runtime.history.directory / history_filename("-123.7")).exists()
    assert not await h.runtime.outbox.pending()


@pytest.mark.parametrize(
    "message",
    [
        "Bad Request: chat not found",
        "Bad Request: not enough rights to delete topic",
        "Bad Request: TOPIC_CLOSED",
    ],
)
async def test_other_telegram_delete_errors_preserve_local_topic(conversation, message):
    h = conversation
    h.runtime.ops.delete_topic.side_effect = TelegramBadRequest(
        method=DeleteForumTopic(chat_id=-123, message_thread_id=7), message=message
    )
    response = await h.client.delete(
        f"{_PREFIX}/topics/-123.7", json={"confirm_name": "Topic #7"}, headers=headers()
    )
    assert response.status == 503
    assert await h.bot._orch._sessions.get_active(h.key) is not None


async def test_queue_endpoints_skip_unreadable_files(conversation, monkeypatch):
    h = conversation
    row = await h.runtime.history.append("-123.7", Entry("user", "web", "message"))
    first = await h.runtime.outbox.enqueue(
        kind="bus", chat_id=-123, topic_id=7, text="blocked", history_id=row.id
    )
    second = await h.runtime.outbox.enqueue(kind="bus", chat_id=-123, text="visible")
    failed = await h.runtime.outbox.enqueue(kind="bus", chat_id=-123, text="failed")
    await h.runtime.outbox.mark_failed(failed.id)
    load = h.runtime.outbox._load

    def unreadable(path):
        if path.name.endswith((f"-{first.id}.json", f"-{failed.id}.json")):
            raise PermissionError("read unavailable")
        return load(path)

    monkeypatch.setattr(h.runtime.outbox, "_load", unreadable)
    for endpoint in ("state", "outbox", "topics/-123.7/messages"):
        response = await h.client.get(f"{_PREFIX}/{endpoint}", headers=headers())
        assert response.status == 200
        data = await response.json()
        if endpoint == "outbox":
            assert [item["id"] for item in data["items"]] == [second.id]
        elif endpoint == "state":
            assert data["outbox"]["pending"] == 1
        else:
            assert data["entries"][0]["mirror"] is None
