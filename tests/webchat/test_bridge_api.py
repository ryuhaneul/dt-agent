"""Bridge authentication, metadata and listener lifetime."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import socket
import stat
import sys
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
from ductor_bot.webchat.topics import WebTopics
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
    assert by_id["-123.7"]["name_key"] is None
    assert by_id["-123.7"]["group"] == "imported"
    assert by_id["-123.7"]["preview"] == "1 imported messages"
    assert by_id["-123.7"]["renamable"]
    assert by_id["-123.7"]["deletable"]
    assert not by_id["-123.7"]["read_only"]
    assert by_id["-123.8"]["name"] == "Cached name"
    assert by_id["-123.8"]["name_key"] is None
    assert len(by_id["-123.8"]["preview"]) == 120
    assert by_id["-123.9"]["name"] == "Saved name"
    assert by_id["-123.9"]["name_key"] is None
    assert by_id["-123.9"]["created_at"] is None
    assert by_id["-123"]["name"] == "General"
    assert by_id["-123"]["name_key"] == "topic.general"
    unknown = by_id["unknown"]
    assert unknown["name_key"] == "topic.unknown_name"
    assert (
        unknown["chat_id"],
        unknown["topic_id"],
        unknown["read_only"],
        unknown["renamable"],
        unknown["deletable"],
    ) == (0, None, True, False, False)
    assert topics[0]["tid"] == "-123.8"


async def test_korean_system_name_metadata_does_not_mark_custom_names(conversation):
    h = conversation
    h.bot.config.language = "ko"
    general = SessionData(-123)
    custom = SessionData(-456, topic_name="일반")
    await h.bot._orch._sessions._save(
        {general.session_key.storage_key: general, custom.session_key.storage_key: custom}
    )
    await h.runtime.history.append(
        "unknown", Entry(role="bot", source="import", text="unmapped", imported=True, seq=-1)
    )
    turns = SimpleNamespace(active_turn_id=lambda _tid: None, busy=lambda _tid: False)
    topics = {item["tid"]: item for item in await WebTopics(h.bot, h.runtime, turns).list()}
    assert topics["-123"]["name"] == "일반"
    assert topics["-123"]["name_key"] == "topic.general"
    assert topics["unknown"]["name"] == "토픽 미상"
    assert topics["unknown"]["name_key"] == "topic.unknown_name"
    assert topics["-456"]["name"] == "일반"
    assert topics["-456"]["name_key"] is None
    assert h.bot.config.language == "ko"


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


async def test_password_change_route_rotates_generation(bridge, monkeypatch):
    from ductor_bot.webchat.auth import TOTP_KEY

    client, _, _, paths = bridge
    seed = "JBSWY3DPEHPK3PXP"
    step = 60_000_000
    monkeypatch.setattr("ductor_bot.webchat.auth.time.time", lambda: step * 30)
    set_secret(paths.ductor_home / "secrets.env", PASSWORD_KEY, hash_password(_PASSWORD))
    set_secret(paths.ductor_home / "secrets.env", TOTP_KEY, seed)
    before = await (await client.get(f"{_PREFIX}/auth/state", headers=headers())).json()
    state_path = paths.webchat_dir / "auth_state.json"
    state = json.loads(state_path.read_text())
    state.update(
        seed_fingerprint=hashlib.sha256(seed.encode()).hexdigest(),
        last_step=step - 2,
        source="login",
    )
    state_path.write_text(json.dumps(state))
    data = {
        "current_password": _PASSWORD,
        "new_password": "changed-password-123",
        "code": totp_code(seed, step),
        "client_ip": "198.51.100.7",
    }
    denied = await client.post(f"{_PREFIX}/auth/password-change", json=data)
    assert denied.status == 403
    response = await client.post(
        f"{_PREFIX}/auth/password-change",
        headers=headers(),
        json={**data, "current_password": "wrong"},
    )
    assert response.status == 403
    assert await response.json() == {"error": "invalid"}
    assert "WWW-Authenticate" not in response.headers
    response = await client.post(f"{_PREFIX}/auth/password-change", headers=headers(), json=data)
    assert response.status == 200
    result = await response.json()
    assert result["ok"] is True
    assert result["credential_generation"] != before["credential_generation"]
    after = await (await client.get(f"{_PREFIX}/auth/state", headers=headers())).json()
    assert result["credential_generation"] == after["credential_generation"]


async def test_history_queries_bound_file_reads_and_preserve_pagination(conversation, monkeypatch):
    from unittest.mock import Mock

    h = conversation
    for index in range(12):
        await h.runtime.history.append("-123.7", Entry(role="user", source="web", text=str(index)))
    await h.client.get(f"{_PREFIX}/topics", headers=headers())
    full_read = Mock(wraps=h.runtime.history._read)
    read = AsyncMock(wraps=h.runtime.history.read)
    pending = Mock(wraps=h.runtime.outbox._pending)
    done = Mock(wraps=h.runtime.outbox._recent_done)
    monkeypatch.setattr(h.runtime.history, "_read", full_read)
    monkeypatch.setattr(h.runtime.history, "read", read)
    monkeypatch.setattr(h.runtime.outbox, "_pending", pending)
    monkeypatch.setattr(h.runtime.outbox, "_recent_done", done)
    response = await h.client.get(f"{_PREFIX}/topics", headers=headers())
    topic = (await response.json())["topics"][0]
    assert (topic["count"], topic["preview"], topic["imported_count"]) == (12, "11", 0)
    response = await h.client.get(f"{_PREFIX}/topics/-123.7/messages?limit=5", headers=headers())
    body = await response.json()
    assert [row["seq"] for row in body["entries"]] == [8, 9, 10, 11, 12]
    assert all(row["mirror"] is None for row in body["entries"])
    assert body["has_more"] is True
    assert full_read.call_count == 0
    assert read.call_count <= 1
    assert pending.call_count == done.call_count == 1


async def test_duplicate_sequence_has_more_and_topic_summary_semantics(conversation):
    from dataclasses import asdict

    h = conversation
    rows = [
        Entry(role="user", source="import", text=str(i), seq=7, imported=True, ts=None)
        for i in range(3)
    ]
    path = h.runtime.history._path("-123.7")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(asdict(row)) + "\n" for row in rows))
    response = await h.client.get(f"{_PREFIX}/topics/-123.7/messages?limit=2", headers=headers())
    body = await response.json()
    assert [row["id"] for row in body["entries"]] == [row.id for row in rows[-2:]]
    assert body["has_more"] is False
    response = await h.client.get(f"{_PREFIX}/topics", headers=headers())
    topic = (await response.json())["topics"][0]
    assert (topic["last_ts"], topic["preview"], topic["count"], topic["imported_count"]) == (
        None,
        "2",
        3,
        3,
    )


async def test_topics_match_full_read_reference_including_empty_imported(conversation, monkeypatch):
    from ductor_bot.chat_log.history import HistorySummary

    h = conversation
    sessions = [
        SessionData(-123, topic_id=topic_id, last_active="2000-01-01T00:00:00Z")
        for topic_id in (7, 8)
    ]
    await h.bot._orch._sessions._save(
        {session.session_key.storage_key: session for session in sessions}
    )
    # Resolve another source through the registry rather than a session.
    h.paths.webchat_dir.mkdir(parents=True, exist_ok=True)
    (h.paths.webchat_dir / "topics.json").write_text(
        json.dumps(
            [
                {
                    "chat_id": -123,
                    "topic_id": 9,
                    "name": "registered",
                    "created_at": "2020-01-01T00:00:00Z",
                }
            ]
        )
    )
    await h.runtime.history.append(
        "-123.7", Entry(role="user", source="import", text="old", imported=True, seq=-3, ts=None)
    )
    await h.runtime.history.append(
        "unknown",
        Entry(role="bot", source="import", text="unknown", imported=True, seq=-1, ts=None),
    )
    await h.runtime.history.append("-123.9", Entry(role="user", source="web", text="x" * 200))
    response = await h.client.get(f"{_PREFIX}/topics", headers=headers())
    indexed = (await response.json())["topics"]

    async def reference(tid):
        rows = h.runtime.history._read(h.runtime.history._path(tid))
        return HistorySummary(
            len(rows), sum(row.imported for row in rows), rows[-1] if rows else None
        )

    monkeypatch.setattr(h.runtime.history, "summary", reference)
    response = await h.client.get(f"{_PREFIX}/topics", headers=headers())
    expected = (await response.json())["topics"]
    assert indexed == expected
    by_id = {row["tid"]: row for row in indexed}
    assert by_id["-123.8"]["group"] == "imported"
    assert by_id["-123.8"]["preview"] == "0 imported messages"
    assert by_id["-123.7"]["last_ts"] is None
    assert len(by_id["-123.9"]["preview"]) == 120


@pytest.mark.parametrize("bus_state", ["pending", "sent", "failed"])
async def test_null_history_id_does_not_match_bus_mirror(conversation, bus_state):
    h = conversation
    row = await h.runtime.history.append("-123.7", Entry(role="user", source="web", text="valid"))
    path = h.runtime.history._path("-123.7")
    with path.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps({"id": None, "seq": 2, "role": "user", "source": "web", "text": "legacy"})
            + "\n"
        )
    await h.runtime.outbox.enqueue(
        kind="user_mirror", chat_id=-123, topic_id=7, text="valid", history_id=row.id
    )
    bus = await h.runtime.outbox.enqueue(kind="bus", chat_id=-123, topic_id=7, text="delivery")
    if bus_state == "sent":
        await h.runtime.outbox.mark_sent(bus.id)
    elif bus_state == "failed":
        await h.runtime.outbox.mark_failed(bus.id)
    response = await h.client.get(f"{_PREFIX}/topics/-123.7/messages", headers=headers())
    assert response.status == 200
    entries = (await response.json())["entries"]
    assert [(entry["id"], entry["mirror"]) for entry in entries] == [
        (row.id, {"state": "pending"}),
        (None, None),
    ]


@pytest.mark.parametrize("path", ["topics", "topics/-123.7/messages"])
async def test_history_lock_timeout_is_retryable_and_recovers(conversation, path):
    h = conversation
    await h.runtime.history.append("-123.7", Entry(role="user", source="web", text="saved"))
    lock_path = h.runtime.history._path("-123.7").with_suffix(".jsonl.lock")
    holder = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import fcntl, sys; "
        "f = open(sys.argv[1], 'a'); fcntl.flock(f, fcntl.LOCK_EX); "
        "print('locked', flush=True); sys.stdin.read()",
        str(lock_path),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    try:
        assert await holder.stdout.readline() == b"locked\n"
        response = await h.client.get(f"{_PREFIX}/{path}", headers=headers())
        assert response.status == 503
        assert await response.json() == {"error": "history_busy"}
        assert "WWW-Authenticate" not in response.headers
        health = await h.client.get(f"{_PREFIX}/health", headers=headers())
        assert health.status == 200
    finally:
        holder.stdin.close()
        assert await holder.wait() == 0
    response = await h.client.get(f"{_PREFIX}/{path}", headers=headers())
    assert response.status == 200


async def test_legacy_response_markers_are_display_only_and_sidecar_rows_win(conversation):
    from dataclasses import asdict

    from ductor_bot.messenger.telegram.buttons import extract_button_rows

    h = conversation
    text = "Choose\n```\n[button:in code]\n```\n[button:Yes] [button:No]"
    entries = [
        Entry(role="bot", source="telegram", text=text, seq=1),
        Entry(role="user", source="telegram", text=text, seq=2),
        Entry(role="bot", source="web", text=text, seq=3),
    ]
    path = h.runtime.history.directory / history_filename("-123.7")
    path.parent.mkdir(parents=True, exist_ok=True)
    original = "".join(json.dumps(asdict(entry)) + "\n" for entry in entries).encode()
    path.write_bytes(original)
    selector = [[{"text": "Model", "callback_data": "ms:claude"}]]
    h.runtime.history._append_metadata("-123.7", entries[2].id, selector)
    cleaned, rows = extract_button_rows(text)
    for _ in range(2):
        response = await h.client.get(f"{_PREFIX}/topics/-123.7/messages", headers=headers())
        assert response.status == 200
        rendered = (await response.json())["entries"]
        assert rendered[0]["text"] == cleaned
        assert rendered[0]["buttons"] == rows
        assert "[button:in code]" in rendered[0]["text"]
        assert rendered[1]["text"] == text
        assert "buttons" not in rendered[1]
        assert rendered[2]["buttons"] == selector
        assert path.read_bytes() == original
    assert await h.runtime.history.metadata("-123.7") == {entries[2].id: {"buttons": selector}}


async def test_response_button_claim_dispatch_edit_and_duplicate(conversation, monkeypatch):
    h = conversation
    h.bot.bot_instance = SimpleNamespace(
        edit_message_text=AsyncMock(), edit_message_reply_markup=AsyncMock()
    )
    submit = MagicMock(return_value="button-turn")
    monkeypatch.setattr(WebTurns, "submit", submit)
    entry = await h.runtime.history.append(h.key, Entry("bot", "telegram", "Pick [button:Yes]"))
    await h.runtime.history.bind_telegram_message(
        "-123.7", entry.id, 42, html_text="<b>Actual chunk</b>"
    )
    queue = h.runtime.events.subscribe()
    url = f"{_PREFIX}/topics/-123.7/buttons"
    response = await h.client.post(
        url, json={"entry_id": entry.id, "label": "Yes"}, headers=headers()
    )
    assert response.status == 202
    assert await response.json() == {
        "turn_id": "button-turn",
        "entry_id": entry.id,
        "answered": "Yes",
    }
    submit.assert_called_once_with("-123.7", "Yes")
    edit = h.bot.bot_instance.edit_message_text.await_args.kwargs
    assert edit["chat_id"] == -123
    assert edit["message_id"] == 42
    assert edit["reply_markup"] is None
    assert edit["text"].startswith("<b>Actual chunk</b>\n\n")
    assert (await h.runtime.history.metadata("-123.7"))[entry.id]["answered"] == "Yes"
    assert any(queue.get_nowait()["type"] == "entry_meta" for _ in range(queue.qsize()))
    duplicate = await h.client.post(
        url, json={"entry_id": entry.id, "label": "Yes"}, headers=headers()
    )
    assert duplicate.status == 409
    assert await duplicate.json() == {"error": "already_answered"}
    assert submit.call_count == 1
    invalid = await h.client.post(
        url, json={"entry_id": entry.id, "label": "No"}, headers=headers()
    )
    assert invalid.status == 400


async def test_response_button_named_destination_and_selector_rejection(conversation, monkeypatch):
    h = conversation
    h.bot.bot_instance = SimpleNamespace(
        edit_message_text=AsyncMock(), edit_message_reply_markup=AsyncMock()
    )
    submit = MagicMock(return_value="named-turn")
    monkeypatch.setattr(WebTurns, "submit", submit)
    entry = await h.runtime.history.append(
        h.key,
        Entry("event", "system", "Pick"),
        buttons=[[{"text": "Yes", "callback_data": "ns:worker:Yes"}]],
    )
    response = await h.client.post(
        f"{_PREFIX}/topics/-123.7/buttons",
        json={"entry_id": entry.id, "label": "Yes"},
        headers=headers(),
    )
    assert response.status == 202
    submit.assert_called_once_with("-123.7", "Yes", session_name="worker")
    h.bot.bot_instance.edit_message_text.assert_not_awaited()
    selector = await h.runtime.history.append(
        h.key,
        Entry("bot", "web", "Model"),
        buttons=[[{"text": "Claude", "callback_data": "ms:p:claude"}]],
    )
    response = await h.client.post(
        f"{_PREFIX}/topics/-123.7/buttons",
        json={"entry_id": selector.id, "label": "Claude"},
        headers=headers(),
    )
    assert response.status == 400
    assert not await h.runtime.history.is_answered("-123.7", selector.id)


async def test_response_button_busy_does_not_claim_and_legacy_remains_immutable(
    conversation, monkeypatch
):
    h = conversation
    h.bot.bot_instance = SimpleNamespace(
        edit_message_text=AsyncMock(), edit_message_reply_markup=AsyncMock()
    )
    entry = await h.runtime.history.append(h.key, Entry("bot", "telegram", "Pick [button:Yes]"))
    monkeypatch.setattr(WebTurns, "busy", lambda _self, _tid: True)
    response = await h.client.post(
        f"{_PREFIX}/topics/-123.7/buttons",
        json={"entry_id": entry.id, "label": "Yes"},
        headers=headers(),
    )
    assert response.status == 409
    assert not await h.runtime.history.is_answered("-123.7", entry.id)
    monkeypatch.setattr(WebTurns, "busy", lambda _self, _tid: False)
    monkeypatch.setattr(WebTurns, "submit", MagicMock(return_value="legacy-turn"))
    path = h.runtime.history._path("-123.7")
    h.runtime.history._metadata_path("-123.7").unlink()
    # Old entries can still contain literal markers, without a sidecar.
    from dataclasses import asdict

    path.write_text(json.dumps(asdict(entry) | {"text": "Pick [button:Yes]"}) + "\n")
    original = path.read_bytes()
    response = await h.client.post(
        f"{_PREFIX}/topics/-123.7/buttons",
        json={"entry_id": entry.id, "label": "Yes"},
        headers=headers(),
    )
    assert response.status == 202
    assert path.read_bytes() == original
    assert await h.runtime.history.is_answered("-123.7", entry.id)
    h.bot.bot_instance.edit_message_reply_markup.assert_not_awaited()


async def test_response_button_named_turn_reaches_named_session_and_keeps_reply_buttons(
    conversation, monkeypatch
):
    from ductor_bot.orchestrator import flows

    h = conversation
    h.bot.bot_instance = SimpleNamespace(
        edit_message_text=AsyncMock(), edit_message_reply_markup=AsyncMock()
    )
    named = AsyncMock(return_value=OrchestratorResult(text="Named reply [button:Again]"))
    monkeypatch.setattr(flows, "named_session_streaming", named)
    entry = await h.runtime.history.append(
        h.key,
        Entry("bot", "telegram", "Named"),
        buttons=[[{"text": "Yes", "callback_data": "ns:worker:Yes"}]],
    )
    work = []
    original = WebTurns.submit

    def submit(self, tid, text, **kwargs):
        result = original(self, tid, text, **kwargs)
        work.append(self._tasks[tid])
        return result

    monkeypatch.setattr(WebTurns, "submit", submit)
    response = await h.client.post(
        f"{_PREFIX}/topics/-123.7/buttons",
        json={"entry_id": entry.id, "label": "Yes"},
        headers=headers(),
    )
    assert response.status == 202
    await asyncio.wait_for(work[0], 5)
    assert named.await_args.args == (h.bot._orch, h.key, "worker", "Yes")
    assert named.await_args.kwargs["transport_override"] == "api"
    h.bot._orch.handle_message_streaming.assert_not_awaited()
    reply = (await h.runtime.history.read("-123.7"))[-1]
    assert reply.source == "web"
    assert (await h.runtime.history.metadata("-123.7"))[reply.id]["buttons"][0][0][
        "callback_data"
    ] == "ns:worker:Again"
    assert any(item.buttons_session == "worker" for item in await h.runtime.outbox.pending())


async def test_telegram_choice_prevents_web_replay(conversation, monkeypatch):
    from tests.messenger.telegram.test_app import _make_callback_query
    from tests.messenger.telegram.test_callback_queue import setup_bot

    h = conversation
    bot, _, _ = setup_bot()
    bot._chat_log_runtime = SimpleNamespace(history=h.runtime.history)
    entry = await h.runtime.history.append(h.key, Entry("bot", "telegram", "Pick [button:Yes]"))
    await h.runtime.history.bind_telegram_message("-123.7", entry.id, 42, html_text="Actual")
    await bot._on_callback_query(
        _make_callback_query(data="Yes", chat_id=-123, message_id=42, topic_thread_id=7)
    )
    bot._handle_streaming.assert_awaited_once()
    submit = MagicMock(return_value="must-not-run")
    monkeypatch.setattr(WebTurns, "submit", submit)
    response = await h.client.post(
        f"{_PREFIX}/topics/-123.7/buttons",
        json={"entry_id": entry.id, "label": "Yes"},
        headers=headers(),
    )
    assert response.status == 409
    submit.assert_not_called()


async def test_response_button_rejects_busy_command_before_claim(conversation):
    h = conversation
    entry = await h.runtime.history.append(h.key, Entry("bot", "telegram", "Pick [button:/new]"))
    lock = h.pool.get(h.key.lock_key)
    async with lock:
        response = await h.client.post(
            f"{_PREFIX}/topics/-123.7/buttons",
            json={"entry_id": entry.id, "label": "/new"},
            headers=headers(),
        )
    assert response.status == 409
    assert not await h.runtime.history.is_answered("-123.7", entry.id)


async def test_response_button_bridge_token_forbidden(conversation):
    for request_headers in [{}, {"X-Dtw-Bridge-Token": "bad"}, {"Authorization": "Bearer test"}]:
        response = await conversation.client.post(
            f"{_PREFIX}/topics/-123.7/buttons",
            json={"entry_id": "entry", "label": "Yes"},
            headers=request_headers,
        )
        assert response.status == 403
        assert "WWW-Authenticate" not in response.headers


async def test_response_button_waits_for_telegram_busy_and_rejects_web_busy(
    conversation, monkeypatch
):
    h = conversation
    h.bot.bot_instance = SimpleNamespace(
        edit_message_text=AsyncMock(), edit_message_reply_markup=AsyncMock()
    )
    first = await h.runtime.history.append(h.key, Entry("bot", "telegram", "Pick [button:Yes]"))
    second = await h.runtime.history.append(h.key, Entry("bot", "telegram", "Other [button:Yes]"))
    tasks = []
    original = WebTurns.submit

    def submit(self, tid, text, **kwargs):
        turn_id = original(self, tid, text, **kwargs)
        tasks.append(self._tasks[tid])
        return turn_id

    monkeypatch.setattr(WebTurns, "submit", submit)
    lock = h.pool.get(h.key.lock_key)
    await lock.acquire()
    try:
        response = await h.client.post(
            f"{_PREFIX}/topics/-123.7/buttons",
            json={"entry_id": first.id, "label": "Yes"},
            headers=headers(),
        )
        assert response.status == 202
        assert not h.calls
        busy = await h.client.post(
            f"{_PREFIX}/topics/-123.7/buttons",
            json={"entry_id": second.id, "label": "Yes"},
            headers=headers(),
        )
        assert busy.status == 409
        assert not await h.runtime.history.is_answered("-123.7", second.id)
    finally:
        lock.release()
        await asyncio.wait_for(asyncio.gather(*tasks), 5)
    assert h.calls[0][1] == "Yes"


async def test_response_button_stop_bypasses_slow_command_gate(conversation, monkeypatch):
    from ductor_bot.webchat.commands import WebCommandDispatcher

    h = conversation
    h.bot.bot_instance = SimpleNamespace(
        edit_message_text=AsyncMock(), edit_message_reply_markup=AsyncMock()
    )
    entry = await h.runtime.history.append(h.key, Entry("bot", "telegram", "[button:/stop]"))
    entered, release = asyncio.Event(), asyncio.Event()
    original = WebCommandDispatcher.dispatch

    async def dispatch(self, tid, text, **kwargs):
        if text == "/help":
            entered.set()
            await release.wait()
            return {"text": "help", "buttons": None}
        return await original(self, tid, text, **kwargs)

    monkeypatch.setattr(WebCommandDispatcher, "dispatch", dispatch)
    command = asyncio.create_task(post_message(h, text="/help"))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        response = await asyncio.wait_for(
            h.client.post(
                f"{_PREFIX}/topics/-123.7/buttons",
                json={"entry_id": entry.id, "label": "/stop"},
                headers=headers(),
            ),
            3,
        )
        assert response.status == 202
        assert await h.runtime.history.is_answered("-123.7", entry.id)
    finally:
        release.set()
        await command


async def test_response_button_edit_failure_keeps_dispatch_and_answer(conversation, monkeypatch):
    from aiogram.exceptions import TelegramNetworkError

    h = conversation
    failure = TelegramNetworkError(MagicMock(), "unavailable")
    h.bot.bot_instance = SimpleNamespace(
        edit_message_text=AsyncMock(side_effect=failure),
        edit_message_reply_markup=AsyncMock(side_effect=failure),
    )
    submit = MagicMock(return_value="accepted")
    monkeypatch.setattr(WebTurns, "submit", submit)
    entry = await h.runtime.history.append(h.key, Entry("bot", "telegram", "[button:Yes]"))
    await h.runtime.history.bind_telegram_message("-123.7", entry.id, 42, html_text="Actual")
    response = await h.client.post(
        f"{_PREFIX}/topics/-123.7/buttons",
        json={"entry_id": entry.id, "label": "Yes"},
        headers=headers(),
    )
    assert response.status == 202
    assert await h.runtime.history.is_answered("-123.7", entry.id)
    submit.assert_called_once_with("-123.7", "Yes")


async def test_response_button_competing_transports_dispatch_once(conversation, monkeypatch):
    from tests.messenger.telegram.test_app import _make_callback_query
    from tests.messenger.telegram.test_callback_queue import setup_bot

    h = conversation
    tg, _, _ = setup_bot()
    tg._chat_log_runtime = SimpleNamespace(history=h.runtime.history)
    h.bot.bot_instance = SimpleNamespace(
        edit_message_text=AsyncMock(), edit_message_reply_markup=AsyncMock()
    )
    submit = MagicMock(return_value="web-turn")
    monkeypatch.setattr(WebTurns, "submit", submit)
    entry = await h.runtime.history.append(h.key, Entry("bot", "telegram", "[button:Yes]"))
    await h.runtime.history.bind_telegram_message("-123.7", entry.id, 42, html_text="Actual")
    response, _ = await asyncio.gather(
        h.client.post(
            f"{_PREFIX}/topics/-123.7/buttons",
            json={"entry_id": entry.id, "label": "Yes"},
            headers=headers(),
        ),
        tg._on_callback_query(
            _make_callback_query(data="Yes", chat_id=-123, message_id=42, topic_thread_id=7)
        ),
    )
    assert response.status in {202, 409}
    assert submit.call_count + tg._handle_streaming.await_count == 1
    assert await h.runtime.history.is_answered("-123.7", entry.id)


@pytest.mark.parametrize(
    ("delivery", "answered"),
    [("mirror", True), ("mirror", False), ("transport", True), ("transport", False)],
)
async def test_late_button_delivery_reconciles_answered_once(
    conversation, monkeypatch, delivery, answered
):
    from ductor_bot.chat_log.link import OutboxDrainer, TelegramLinkMonitor
    from ductor_bot.messenger.telegram.webchat_ops import TelegramWebchatOps

    h = conversation
    message = SimpleNamespace(message_id=42, text="Actual", html_text="<b>Final actual</b>")
    h.bot.bot_instance = SimpleNamespace(
        send_message=AsyncMock(return_value=message),
        edit_message_text=AsyncMock(),
        edit_message_reply_markup=AsyncMock(return_value=message),
    )
    h.bot.file_roots = lambda _paths: None
    entry = await h.runtime.history.append(h.key, Entry("bot", "telegram", "Pick [button:Yes]"))
    text = "Pick [button:Yes]"
    if delivery == "mirror":
        await h.runtime.outbox.enqueue(
            kind="reply_mirror", chat_id=-123, topic_id=7, text=text, history_id=entry.id
        )
    if answered:
        monkeypatch.setattr(WebTurns, "submit", MagicMock(return_value="accepted"))
        response = await h.client.post(
            f"{_PREFIX}/topics/-123.7/buttons",
            json={"entry_id": entry.id, "label": "Yes"},
            headers=headers(),
        )
        assert response.status == 202
    h.bot.bot_instance.edit_message_text.assert_not_awaited()
    if delivery == "mirror":
        ops = TelegramWebchatOps(h.bot)
        link = TelegramLinkMonitor(ops, h.runtime.events)
        link.record(up=True)
        await OutboxDrainer(h.runtime.outbox, ops, link, history=h.runtime.history).drain()
        assert not await h.runtime.outbox.pending()
    else:
        transport = TelegramTransport(h.bot)
        transport.set_outbox(h.runtime.outbox, history=h.runtime.history)
        await transport.deliver(
            Envelope(
                Origin.WEBHOOK_WAKE,
                -123,
                topic_id=7,
                result_text=text,
                metadata={"chat_log_entry_id": entry.id},
            )
        )
    assert h.bot.bot_instance.edit_message_text.await_count == int(answered)
    if answered:
        edit = h.bot.bot_instance.edit_message_text.await_args.kwargs
        assert edit["message_id"] == 42
        assert edit["reply_markup"] is None
        assert edit["text"].startswith("<b>Final actual</b>\n\n")
        assert await h.runtime.history.is_answered("-123.7", entry.id)


@pytest.mark.parametrize("answered", [True, False])
async def test_web_choice_before_telegram_binding_is_reconciled_once(
    conversation, monkeypatch, answered
):
    from ductor_bot.messenger.telegram.message_dispatch import (
        NonStreamingDispatch,
        run_non_streaming_message,
    )

    h = conversation
    entered, release = asyncio.Event(), asyncio.Event()
    message = SimpleNamespace(message_id=42, text="Actual", html_text="<b>Final actual</b>")

    async def send(**_kwargs):
        entered.set()
        await release.wait()
        return message

    h.bot.bot_instance = SimpleNamespace(
        send_message=AsyncMock(side_effect=send),
        send_chat_action=AsyncMock(),
        edit_message_text=AsyncMock(),
        edit_message_reply_markup=AsyncMock(return_value=message),
    )
    h.bot._orch._history_recorder = h.runtime.recorder

    async def handle(key, text):
        dispatch = SimpleNamespace(
            key=key, text=text, transport_override=None, counts_as_activity=True
        )
        await h.runtime.recorder.begin(dispatch)
        result = OrchestratorResult(text="Pick [button:Yes]")
        await h.runtime.recorder.finish(dispatch, result)
        return result

    h.bot._orch.handle_message = handle
    task = asyncio.create_task(
        run_non_streaming_message(
            NonStreamingDispatch(
                h.bot.bot_instance, h.bot._orch, h.key, "original", None, thread_id=7
            )
        )
    )
    monkeypatch.setattr(WebTurns, "submit", MagicMock(return_value="accepted"))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        entry = (await h.runtime.history.read("-123.7"))[-1]
        if answered:
            response = await h.client.post(
                f"{_PREFIX}/topics/-123.7/buttons",
                json={"entry_id": entry.id, "label": "Yes"},
                headers=headers(),
            )
            assert response.status == 202
        h.bot.bot_instance.edit_message_text.assert_not_awaited()
    finally:
        release.set()
        await asyncio.wait_for(task, 5)
    assert h.bot.bot_instance.edit_message_text.await_count == int(answered)
    if answered:
        edit = h.bot.bot_instance.edit_message_text.await_args.kwargs
        assert edit["message_id"] == 42
        assert edit["reply_markup"] is None
        assert edit["text"].startswith("<b>Final actual</b>\n\n")


async def test_selector_sync_transition_stale_invalid_end_and_restore(conversation, monkeypatch):
    from ductor_bot.orchestrator.selectors.models import Button, ButtonGrid, SelectorResponse

    h = conversation
    h.bot._sequential = SimpleNamespace(get_lock=h.bot._orch._lock_pool.get)
    h.bot.bot_instance = SimpleNamespace(edit_message_text=AsyncMock())
    rows = [[{"text": "Claude", "callback_data": "ms:p:claude"}]]
    entry = await h.runtime.history.append(h.key, Entry("bot", "web", "Provider"), buttons=rows)
    await h.runtime.history.bind_telegram_message("-123.7", entry.id, 44)
    lower = AsyncMock(
        side_effect=[
            SelectorResponse("Pick model", ButtonGrid([[Button("Opus", "ms:m:opus")]])),
            SelectorResponse("Selected Opus"),
        ]
    )
    monkeypatch.setattr(
        "ductor_bot.orchestrator.selectors.model_selector.handle_model_callback", lower
    )
    url = f"{_PREFIX}/topics/-123.7/callbacks"

    async def click(data, revision):
        return await h.client.post(
            url, json={"entry_id": entry.id, "data": data, "revision": revision}, headers=headers()
        )

    first = await click("ms:p:claude", 0)
    assert first.status == 200
    body = await first.json()
    assert body["text"] == "Pick model"
    assert body["selector_revision"] == 1
    assert body["button_kinds"] == [["selector"]]
    edited = h.bot.bot_instance.edit_message_text.call_args.kwargs
    assert edited["chat_id"] == -123
    assert edited["message_id"] == 44
    assert edited["reply_markup"].inline_keyboard[0][0].callback_data == "ms:m:opus"
    assert (await click("ms:p:claude", 0)).status == 409
    assert (await click("unsupported:data", 0)).status == 409
    assert (await click("ms:unknown", 1)).status == 400
    assert lower.await_count == 1
    final = await click("ms:m:opus", 1)
    assert final.status == 200
    assert (await final.json())["buttons"] == []
    assert (await click("ms:m:opus", 1)).status == 409
    restored = HistoryStore(h.paths.chat_log_dir / "history")
    assert (await restored.metadata("-123.7"))[entry.id]["text"] == "Selected Opus"
    assert (await restored.get_entry("-123.7", entry.id)).text == "Provider"
    messages = await (
        await h.client.get(f"{_PREFIX}/topics/-123.7/messages", headers=headers())
    ).json()
    found = next(item for item in messages["entries"] if item["id"] == entry.id)
    assert found["text"] == "Selected Opus"
    assert found["selector_revision"] == 2


async def test_selector_sync_missing_message_and_failed_edit_keep_revision(
    conversation, monkeypatch
):
    from ductor_bot.orchestrator.selectors.models import SelectorResponse

    h = conversation
    h.bot._sequential = SimpleNamespace(get_lock=h.bot._orch._lock_pool.get)
    h.bot.bot_instance = SimpleNamespace(
        edit_message_text=AsyncMock(side_effect=RuntimeError("offline"))
    )
    monkeypatch.setattr(
        "ductor_bot.orchestrator.selectors.model_selector.handle_model_callback",
        AsyncMock(return_value=SelectorResponse("Done")),
    )
    for mid in (None, 44):
        entry = await h.runtime.history.append(
            h.key,
            Entry("bot", "web", "Choose"),
            buttons=[[{"text": "Claude", "callback_data": "ms:p:claude"}]],
        )
        if mid:
            await h.runtime.history.bind_telegram_message("-123.7", entry.id, mid)
        response = await h.client.post(
            f"{_PREFIX}/topics/-123.7/callbacks",
            json={"entry_id": entry.id, "data": "ms:p:claude", "revision": 0},
            headers=headers(),
        )
        assert response.status == 200
        assert (await h.runtime.history.metadata("-123.7"))[entry.id]["selector_revision"] == 1
    assert h.bot.bot_instance.edit_message_text.await_count == 1


async def test_selector_sync_welcome_prompt_answered_and_callback_auth(conversation):
    from ductor_bot.messenger.telegram.welcome import resolve_welcome_callback

    h = conversation
    h.bot.bot_instance = SimpleNamespace(
        edit_message_text=AsyncMock(), edit_message_reply_markup=AsyncMock()
    )
    entry = await h.runtime.history.append(
        h.key, Entry("bot", "web", "Welcome"), buttons=[[{"text": "Know", "callback_data": "w:1"}]]
    )
    url = f"{_PREFIX}/topics/-123.7/callbacks"
    payload = {"entry_id": entry.id, "data": "w:1", "revision": 0}
    forbidden = await h.client.post(url, json=payload)
    assert forbidden.status == 403
    assert "WWW-Authenticate" not in forbidden.headers
    response = await h.client.post(url, json=payload, headers=headers())
    assert response.status == 202
    assert (await response.json())["answered"] == "Know"
    await asyncio.sleep(0)
    assert await h.runtime.history.is_answered("-123.7", entry.id)
    again = await h.client.post(url, json=payload, headers=headers())
    assert again.status == 409
    assert resolve_welcome_callback("w:1")


@pytest.mark.parametrize(
    ("namespace", "module", "function"),
    [
        ("crn:r:0", "cron_selector", "handle_cron_callback"),
        ("nsc:r", "session_selector", "handle_session_callback"),
        ("tsc:r", "task_selector", "handle_task_callback"),
        ("acs:r", "account_selector", "handle_account_callback"),
    ],
)
async def test_selector_sync_uses_existing_functions(
    conversation, monkeypatch, namespace, module, function
):
    from ductor_bot.orchestrator.selectors.models import SelectorResponse

    h = conversation
    h.bot._sequential = SimpleNamespace(get_lock=h.pool.get)
    h.bot.bot_instance = SimpleNamespace(edit_message_text=AsyncMock())
    h.bot._config = h.bot.config
    h.bot._agent_name = "main"
    h.bot._orch.task_hub = object()
    lower = AsyncMock(return_value=SelectorResponse("Unmodified lower response"))
    monkeypatch.setattr(f"ductor_bot.orchestrator.selectors.{module}.{function}", lower)
    entry = await h.runtime.history.append(
        h.key,
        Entry("bot", "web", "Choose"),
        buttons=[[{"text": "Next", "callback_data": namespace}]],
    )
    response = await h.client.post(
        f"{_PREFIX}/topics/-123.7/callbacks",
        json={"entry_id": entry.id, "data": namespace, "revision": 0},
        headers=headers(),
    )
    assert response.status == 200
    assert (await response.json())["text"] == "Unmodified lower response"
    assert lower.await_count == 1
    assert lower.call_args.args[-1] == namespace


async def test_selector_sync_real_task_delete_confirmation_cannot_be_skipped(conversation):
    h = conversation
    registry = SimpleNamespace(
        count_finished=MagicMock(return_value=1),
        list_all=MagicMock(return_value=[]),
        cleanup_finished=MagicMock(return_value=1),
    )
    h.bot._orch.task_hub = SimpleNamespace(registry=registry)
    h.bot.bot_instance = SimpleNamespace(edit_message_text=AsyncMock())
    entry = await h.runtime.history.append(
        h.key,
        Entry("bot", "web", "Tasks"),
        buttons=[[{"text": "Clean", "callback_data": "tsc:cleanup"}]],
    )

    async def click(data, revision):
        return await h.client.post(
            f"{_PREFIX}/topics/-123.7/callbacks",
            json={"entry_id": entry.id, "data": data, "revision": revision},
            headers=headers(),
        )

    assert (await click("tsc:cleanup_yes", 0)).status == 400
    registry.cleanup_finished.assert_not_called()
    confirmed = await click("tsc:cleanup", 0)
    assert confirmed.status == 200
    body = await confirmed.json()
    assert any(
        button["callback_data"] == "tsc:cleanup_yes" for row in body["buttons"] for button in row
    )
    registry.cleanup_finished.assert_not_called()
    assert (await click("tsc:cleanup_yes", 1)).status == 200
    registry.cleanup_finished.assert_called_once_with(-123)
    assert (await click("tsc:cleanup_yes", 1)).status == 409


async def test_selector_sync_usage_file_navigation_and_input(conversation, monkeypatch):
    from ductor_bot.orchestrator.selectors.models import Button, ButtonGrid, SelectorResponse

    h = conversation
    h.bot.bot_instance = SimpleNamespace(
        edit_message_text=AsyncMock(), edit_message_reply_markup=AsyncMock()
    )
    h.bot._usage_response = AsyncMock(
        return_value=SelectorResponse("Usage", ButtonGrid([[Button("Refresh", "usg:refresh")]]))
    )
    monkeypatch.setattr(
        "ductor_bot.cli.codex_usage.query_codex_usage", AsyncMock(return_value=None)
    )
    (h.paths.ductor_home / "folder").mkdir()
    for data in ("usg:refresh", "sf:", "sf!folder"):
        entry = await h.runtime.history.append(
            h.key,
            Entry("bot", "web", "Original"),
            buttons=[[{"text": "Go", "callback_data": data}]],
        )
        response = await h.client.post(
            f"{_PREFIX}/topics/-123.7/callbacks",
            json={"entry_id": entry.id, "data": data, "revision": 0},
            headers=headers(),
        )
        assert response.status == (202 if data.startswith("sf!") else 200)
        body = await response.json()
        if data.startswith("sf!"):
            assert body["answered"] == "Go"
            assert await h.runtime.history.is_answered("-123.7", entry.id)
        else:
            assert body["selector_revision"] == 1
            assert body["text"] != "Original"
    h.bot._usage_response.assert_awaited_once_with(None)


async def test_selector_sync_competing_web_and_telegram_observe_revision(conversation, monkeypatch):
    from ductor_bot.orchestrator.selectors.models import SelectorResponse
    from tests.messenger.telegram.test_app import _make_callback_query
    from tests.messenger.telegram.test_callback_queue import setup_bot

    h = conversation
    telegram, api, _ = setup_bot()
    telegram._orchestrator = h.bot._orch
    telegram._chat_log_runtime = h.runtime
    h.bot._sequential = telegram._sequential
    h.bot.bot_instance = api
    entry = await h.runtime.history.append(
        h.key,
        Entry("bot", "web", "Provider"),
        buttons=[[{"text": "Claude", "callback_data": "ms:p:claude"}]],
    )
    await h.runtime.history.bind_telegram_message("-123.7", entry.id, 42)
    entered, release = asyncio.Event(), asyncio.Event()

    async def action(*_args):
        entered.set()
        await release.wait()
        return SelectorResponse("Done")

    lower = AsyncMock(side_effect=action)
    monkeypatch.setattr(
        "ductor_bot.orchestrator.selectors.model_selector.handle_model_callback", lower
    )
    first = asyncio.create_task(
        telegram._on_callback_query(
            _make_callback_query(data="ms:p:claude", chat_id=-123, message_id=42, topic_thread_id=7)
        )
    )
    await asyncio.wait_for(entered.wait(), 2)
    second = asyncio.create_task(
        h.client.post(
            f"{_PREFIX}/topics/-123.7/callbacks",
            json={"entry_id": entry.id, "data": "ms:p:claude", "revision": 0},
            headers=headers(),
        )
    )
    try:
        release.set()
        await asyncio.wait_for(first, 2)
        assert (await asyncio.wait_for(second, 2)).status == 409
        assert lower.await_count == 1
    finally:
        release.set()
        await asyncio.gather(first, second, return_exceptions=True)


async def test_selector_sync_web_events_project_kinds_without_storing_them(conversation):
    h = conversation
    socket = await h.client.ws_connect(f"{_PREFIX}/events", headers=headers())
    try:
        rows = [
            [
                {"text": "Yes", "callback_data": "Yes"},
                {"text": "Model", "callback_data": "ms:p:claude"},
                {"text": "Site", "url": "https://example.com"},
                {"text": "Unsupported", "callback_data": "private:data"},
            ]
        ]
        entry = await h.runtime.history.append(h.key, Entry("bot", "web", "Choose"), buttons=rows)
        created = await receive_until(socket, lambda frame: frame.get("type") == "entry")
        assert created[-1]["entry"]["button_kinds"] == [["reply", "selector", "link", "disabled"]]
        await h.runtime.history.record_selector_response("-123.7", entry.id, "Done", [])
        updated = await receive_until(socket, lambda frame: frame.get("type") == "entry_meta")
        assert updated[-1]["meta"]["selector_revision"] == 1
        assert updated[-1]["meta"]["text"] == "Done"
        assert updated[-1]["meta"]["button_kinds"] == []
        assert "button_kinds" not in (await h.runtime.history.metadata("-123.7"))[entry.id]
    finally:
        await socket.close()


def test_selector_sync_button_kinds_cover_input_callbacks_without_mutation():
    from ductor_bot.messenger.telegram.callbacks import button_kinds

    rows = [
        [
            {"text": "Yes", "callback_data": "Yes"},
            {"text": "Again", "callback_data": "ns:worker:Again"},
            {"text": "Welcome", "callback_data": "w:1"},
            {"text": "Files", "callback_data": "sf!folder"},
            {"text": "Site", "url": "https://example.com"},
            {"text": "Unknown", "callback_data": "sec:private"},
        ]
    ]
    before = json.dumps(rows)
    assert button_kinds(rows) == [["reply", "reply", "selector", "selector", "link", "disabled"]]
    assert json.dumps(rows) == before


@pytest.mark.parametrize("route", ["buttons", "callbacks"])
async def test_web_choice_binding_between_context_and_claim_is_annotated(
    conversation, monkeypatch, route
):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    from ductor_bot.messenger.telegram.callbacks import recorded_keyboard_observer
    from ductor_bot.messenger.telegram.welcome import resolve_welcome_callback

    h = conversation
    h.bot.bot_instance = SimpleNamespace(
        edit_message_text=AsyncMock(), edit_message_reply_markup=AsyncMock()
    )
    submit = MagicMock(return_value="accepted")
    monkeypatch.setattr(WebTurns, "submit", submit)
    callback_data = "Yes" if route == "buttons" else "w:1"
    entry = await h.runtime.history.append(
        h.key,
        Entry("bot", "web", "Pick"),
        buttons=[[{"text": "Yes", "callback_data": callback_data}]],
    )
    observe = recorded_keyboard_observer(h.runtime.history, h.bot.bot_instance, h.key, entry.id)
    mark_answered = h.runtime.history.mark_answered

    async def bind_before_claim(tid, entry_id, label):
        # The web handler has read its unbound context; mirror delivery now finishes.
        await observe(
            42,
            InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Yes", callback_data=callback_data)]]
            ),
            "<b>Delivered snapshot</b>",
        )
        h.bot.bot_instance.edit_message_text.assert_not_awaited()
        h.bot.bot_instance.edit_message_reply_markup.assert_not_awaited()
        return await mark_answered(tid, entry_id, label)

    monkeypatch.setattr(h.runtime.history, "mark_answered", bind_before_claim)
    payload = {"entry_id": entry.id}
    payload.update(
        {"label": "Yes"} if route == "buttons" else {"data": callback_data, "revision": 0}
    )
    response = await h.client.post(
        f"{_PREFIX}/topics/-123.7/{route}", json=payload, headers=headers()
    )
    assert response.status == 202
    assert (await response.json())["answered"] == "Yes"
    submit.assert_called_once_with(
        "-123.7", "Yes" if route == "buttons" else resolve_welcome_callback(callback_data)
    )
    meta = (await h.runtime.history.metadata("-123.7"))[entry.id]
    assert meta["telegram_message_id"] == 42
    assert meta["answered"] == "Yes"
    h.bot.bot_instance.edit_message_text.assert_awaited_once()
    edit = h.bot.bot_instance.edit_message_text.await_args.kwargs
    assert edit["message_id"] == 42
    assert edit["chat_id"] == -123
    assert edit["reply_markup"] is None
    assert edit["text"].startswith("<b>Delivered snapshot</b>\n\n")
    h.bot.bot_instance.edit_message_reply_markup.assert_not_awaited()


async def test_failed_web_selector_edit_is_repaired_by_old_telegram_button(
    conversation, monkeypatch
):
    from aiogram.exceptions import TelegramRetryAfter

    from ductor_bot.orchestrator.selectors.models import Button, ButtonGrid, SelectorResponse
    from tests.messenger.telegram.test_app import _make_callback_query
    from tests.messenger.telegram.test_callback_queue import setup_bot

    h = conversation
    telegram, api, _ = setup_bot()
    telegram._orchestrator = h.bot._orch
    telegram._chat_log_runtime = h.runtime
    h.bot._sequential = telegram._sequential
    h.bot.bot_instance = api
    entry = await h.runtime.history.append(
        h.key,
        Entry("bot", "web", "Providers"),
        buttons=[[{"text": "Codex", "callback_data": "ms:p:codex"}]],
    )
    await h.runtime.history.bind_telegram_message("-123.7", entry.id, 42)
    lower = AsyncMock(
        return_value=SelectorResponse(
            "**Latest models**", ButtonGrid([[Button("Latest", "ms:m:latest")]])
        )
    )
    monkeypatch.setattr(
        "ductor_bot.orchestrator.selectors.model_selector.handle_model_callback", lower
    )
    api.edit_message_text.side_effect = TelegramRetryAfter(
        MagicMock(), "rate limited", retry_after=1
    )
    response = await h.client.post(
        f"{_PREFIX}/topics/-123.7/callbacks",
        json={"entry_id": entry.id, "data": "ms:p:codex", "revision": 0},
        headers=headers(),
    )
    assert response.status == 200
    assert (await response.json())["selector_revision"] == 1
    api.edit_message_text.assert_awaited_once()
    api.edit_message_text.reset_mock(side_effect=True)
    cb = _make_callback_query(data="ms:p:codex", chat_id=-123, message_id=42, topic_thread_id=7)
    await telegram._on_callback_query(cb)
    cb.answer.assert_awaited_once_with()
    lower.assert_awaited_once()
    api.edit_message_text.assert_awaited_once()
    edit = api.edit_message_text.await_args.kwargs
    assert edit["chat_id"] == -123
    assert edit["message_id"] == 42
    assert edit["text"] == "<b>Latest models</b>"
    assert edit["reply_markup"].inline_keyboard[0][0].callback_data == "ms:m:latest"
    assert (await h.runtime.history.metadata("-123.7"))[entry.id]["selector_revision"] == 1


async def test_usage_200_refreshes_bound_metadata_size(conversation, monkeypatch):
    from ductor_bot.orchestrator.selectors.models import Button, ButtonGrid, SelectorResponse

    h = conversation
    h.bot.bot_instance = SimpleNamespace(edit_message_text=AsyncMock())
    text = "Usage row: 01234567890123456789\n" * 100
    response = SelectorResponse(text, ButtonGrid([[Button("Refresh", "usg:refresh")]]))
    h.bot._usage_response = AsyncMock(return_value=response)
    monkeypatch.setattr(
        "ductor_bot.cli.codex_usage.query_codex_usage", AsyncMock(return_value=None)
    )
    entry = await h.runtime.history.append(
        h.key,
        Entry("bot", "web", "Usage"),
        buttons=[[{"text": "Refresh", "callback_data": "usg:refresh"}]],
    )
    await h.runtime.history.bind_telegram_message("-123.7", entry.id, 42)
    path = h.runtime.history._metadata_path("-123.7")
    max_size = 0
    for revision in range(200):
        result = await h.client.post(
            f"{_PREFIX}/topics/-123.7/callbacks",
            json={"entry_id": entry.id, "data": "usg:refresh", "revision": revision},
            headers=headers(),
        )
        assert result.status == 200
        assert (await result.json())["selector_revision"] == revision + 1
        max_size = max(max_size, path.stat().st_size)
    print(f"200 usage refreshes: max_bytes={max_size}, final_bytes={path.stat().st_size}")
    h.bot._usage_response.assert_awaited()
    assert h.bot._usage_response.await_count == 200
    assert h.bot.bot_instance.edit_message_text.await_count == 200
    latest = (await HistoryStore(h.paths.chat_log_dir / "history").metadata("-123.7"))[entry.id]
    assert latest["text"] == text
    assert latest["selector_revision"] == 200
    assert max_size <= 256 * 1024


@pytest.mark.parametrize("route", ["buttons", "callbacks"])
@pytest.mark.parametrize("persistent", [False, True])
async def test_post_claim_metadata_error_still_submits_once(
    conversation, monkeypatch, caplog, route, persistent
):
    h = conversation
    h.bot.bot_instance = SimpleNamespace(
        edit_message_text=AsyncMock(), edit_message_reply_markup=AsyncMock()
    )
    submit = MagicMock(return_value="accepted")
    monkeypatch.setattr(WebTurns, "submit", submit)
    data = "Yes" if route == "buttons" else "w:1"
    entry = await h.runtime.history.append(
        h.key, Entry("bot", "web", "Choose"), buttons=[[{"text": "Yes", "callback_data": data}]]
    )
    await h.runtime.history.bind_telegram_message(
        "-123.7", entry.id, 42, html_text="<b>Snapshot</b>"
    )
    mark = h.runtime.history.mark_answered
    metadata = h.runtime.history.metadata
    claimed, failures = False, 0

    async def claim(*args, **kwargs):
        nonlocal claimed
        claimed = await mark(*args, **kwargs)
        return claimed

    async def read(*args, **kwargs):
        nonlocal failures
        if claimed and (persistent or not failures):
            failures += 1
            raise PermissionError("private-post-claim-details")
        return await metadata(*args, **kwargs)

    monkeypatch.setattr(h.runtime.history, "mark_answered", claim)
    monkeypatch.setattr(h.runtime.history, "metadata", read)
    body = {
        "entry_id": entry.id,
        **({"label": "Yes"} if route == "buttons" else {"data": data, "revision": 0}),
    }
    response = await h.client.post(f"{_PREFIX}/topics/-123.7/{route}", json=body, headers=headers())
    assert response.status == 202
    result = await response.json()
    assert result["answered"] == "Yes"
    assert result["entry_id"] == entry.id
    assert result["turn_id"] == "accepted"
    submit.assert_called_once()
    assert failures == 1
    h.bot.bot_instance.edit_message_text.assert_awaited_once()
    assert h.bot.bot_instance.edit_message_text.await_args.kwargs["text"].startswith(
        "<b>Snapshot</b>\n\n"
    )
    if route == "callbacks":
        assert result["text"] == "Choose"
        assert result["buttons"] == [[{"text": "Yes", "callback_data": "w:1"}]]
        assert result["button_kinds"] == [["selector"]]
        assert result["selector_revision"] == 0
    monkeypatch.setattr(h.runtime.history, "metadata", metadata)
    assert (await metadata("-123.7"))[entry.id]["answered"] == "Yes"
    retry = await h.client.post(f"{_PREFIX}/topics/-123.7/{route}", json=body, headers=headers())
    assert retry.status == 409
    assert submit.call_count == 1
    assert "PermissionError" in caplog.text
    assert "private-post-claim-details" not in caplog.text


@pytest.mark.parametrize("session_busy", [False, True])
async def test_stale_restore_and_web_transition_share_entry_gate(
    conversation, monkeypatch, session_busy
):
    from ductor_bot.orchestrator.selectors.models import Button, ButtonGrid, SelectorResponse
    from tests.messenger.telegram.test_app import _make_callback_query
    from tests.messenger.telegram.test_callback_queue import setup_bot

    h = conversation
    telegram, api, _ = setup_bot()
    telegram._orchestrator = h.bot._orch
    telegram._chat_log_runtime = h.runtime
    h.bot._sequential = telegram._sequential
    h.bot.bot_instance = api
    entry = await h.runtime.history.append(
        h.key,
        Entry("bot", "web", "Original"),
        buttons=[[{"text": "Old", "callback_data": "ms:p:codex"}]],
    )
    await h.runtime.history.bind_telegram_message("-123.7", entry.id, 42)
    await h.runtime.history.record_selector_response(
        "-123.7", entry.id, "Latest screen 1", [[{"text": "Next", "callback_data": "ms:m:opus"}]]
    )
    entered, release = asyncio.Event(), asyncio.Event()
    phone, restore_gates = {}, []
    session_lock = telegram._sequential.get_lock(h.key.lock_key)
    if session_busy:
        await session_lock.acquire()

    async def edit(**kwargs):
        if kwargs["text"] == "Latest screen 1":
            restore_gates.append(h.runtime.history.selector_gate("-123.7", entry.id).locked())
            entered.set()
            await release.wait()
        phone["text"] = kwargs["text"]

    api.edit_message_text = AsyncMock(side_effect=edit)
    lower = AsyncMock(
        return_value=SelectorResponse(
            "Latest screen 2", ButtonGrid([[Button("Refresh", "ms:b:root")]])
        )
    )
    monkeypatch.setattr(
        "ductor_bot.orchestrator.selectors.model_selector.handle_model_callback", lower
    )
    cb = _make_callback_query(data="ms:p:codex", chat_id=-123, message_id=42, topic_thread_id=7)
    restoring = asyncio.create_task(telegram._on_callback_query(cb))
    web = None
    try:
        await asyncio.wait_for(entered.wait(), 3)
        if session_busy:
            assert session_lock.locked()
            session_lock.release()
        web = asyncio.create_task(
            h.client.post(
                f"{_PREFIX}/topics/-123.7/callbacks",
                json={"entry_id": entry.id, "data": "ms:m:opus", "revision": 1},
                headers=headers(),
            )
        )
        await asyncio.sleep(0.1)
    finally:
        if session_busy and session_lock.locked():
            session_lock.release()
        release.set()
        await asyncio.wait_for(restoring, 3)
        if web is not None:
            response = await asyncio.wait_for(web, 3)
    assert response.status == 200
    meta = (await h.runtime.history.metadata("-123.7"))[entry.id]
    assert meta["selector_revision"] == 2
    assert phone["text"] == meta["text"] == "Latest screen 2"
    assert restore_gates == [True]
    cb.answer.assert_awaited_once_with()
    lower.assert_awaited_once()


@pytest.mark.parametrize("source", ["telegram", "web"])
@pytest.mark.parametrize(
    ("data", "module", "handler", "lock_kind"),
    [
        ("ms:p:claude", "model_selector", "handle_model_callback", "topic"),
        ("crn:r:0", "cron_selector", "handle_cron_callback", "chat"),
        ("nsc:r", "session_selector", "handle_session_callback", "chat"),
    ],
)
async def test_busy_selector_returns_prompt_conflict_without_blocking_topic(
    conversation, monkeypatch, *, source, data, module, handler, lock_kind
):
    from ductor_bot.orchestrator.selectors.models import SelectorResponse

    h = conversation
    h.bot._sequential = SimpleNamespace(get_lock=h.pool.get)
    h.bot.bot_instance = SimpleNamespace(edit_message_text=AsyncMock())
    entry = await h.runtime.history.append(
        h.key,
        Entry("bot", "web", "Provider"),
        buttons=[[{"text": "Go", "callback_data": data}]],
    )
    await h.runtime.history.bind_telegram_message("-123.7", entry.id, 42)
    lower = AsyncMock(return_value=SelectorResponse("Selected"))
    monkeypatch.setattr(f"ductor_bot.orchestrator.selectors.{module}.{handler}", lower)
    lock_key = h.key.chat_id if source == "telegram" and lock_kind == "chat" else h.key.lock_key
    lock = h.pool.get(lock_key)
    entered, finish = asyncio.Event(), asyncio.Event()
    tasks = []
    original_submit = WebTurns.submit
    original_handle = h.bot._orch.handle_message_streaming.side_effect

    def submit(self, *args, **kwargs):
        turn_id = original_submit(self, *args, **kwargs)
        tasks.append(self._tasks["-123.7"])
        return turn_id

    async def handle(*args, **kwargs):
        entered.set()
        await finish.wait()
        return await original_handle(*args, **kwargs)

    monkeypatch.setattr(WebTurns, "submit", submit)
    if source == "telegram":
        await lock.acquire()
    else:
        h.bot._orch.handle_message_streaming.side_effect = handle
        assert (await post_message(h, text="Running turn")).status == 202
        await asyncio.wait_for(entered.wait(), 3)
    payload = {"entry_id": entry.id, "data": data, "revision": 0}
    url = f"{_PREFIX}/topics/-123.7/callbacks"
    before = await h.runtime.history.metadata("-123.7")
    original_bytes = h.runtime.history._metadata_path("-123.7").read_bytes()
    try:
        started = asyncio.get_running_loop().time()
        response = await asyncio.wait_for(h.client.post(url, json=payload, headers=headers()), 1)
        elapsed = asyncio.get_running_loop().time() - started
        assert response.status == 409
        assert await response.json() == {"error": "web_turn_running"}
        assert elapsed < 1
        assert lock.locked()
        lower.assert_not_awaited()
        h.bot.bot_instance.edit_message_text.assert_not_awaited()
        assert await h.runtime.history.metadata("-123.7") == before
        assert h.runtime.history._metadata_path("-123.7").read_bytes() == original_bytes
        followup = await asyncio.wait_for(post_message(h, text="Follow-up"), 1)
        assert followup.status == (202 if source == "telegram" else 409)
        if source == "web":
            assert await followup.json() == {"error": "web_turn_running"}
        assert lock.locked()
        print(f"{source} busy selector response: {elapsed:.6f}s; follow-up={followup.status}")
    finally:
        if source == "telegram" and lock.locked():
            lock.release()
        finish.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 5)
    # The original revision and data remain valid after the running turn finishes.
    response = await asyncio.wait_for(h.client.post(url, json=payload, headers=headers()), 1)
    assert response.status == 200
    assert (await response.json())["selector_revision"] == 1
    lower.assert_awaited_once()
    h.bot.bot_instance.edit_message_text.assert_awaited_once()


@pytest.mark.parametrize("web_busy", [False, True])
async def test_lock_free_task_selector_runs_during_a_turn(conversation, monkeypatch, web_busy):
    from ductor_bot.orchestrator.selectors.models import SelectorResponse

    h = conversation
    if web_busy:
        monkeypatch.setattr(WebTurns, "busy", lambda _self, _tid: True)
    h.bot._orch.task_hub = MagicMock()
    h.bot.bot_instance = SimpleNamespace(edit_message_text=AsyncMock())
    lower = AsyncMock(return_value=SelectorResponse("Tasks refreshed"))
    monkeypatch.setattr(
        "ductor_bot.orchestrator.selectors.task_selector.handle_task_callback", lower
    )
    entry = await h.runtime.history.append(
        h.key,
        Entry("bot", "web", "Tasks"),
        buttons=[[{"text": "Refresh", "callback_data": "tsc:r"}]],
    )
    await h.runtime.history.bind_telegram_message("-123.7", entry.id, 42)
    async with h.pool.get(h.key.lock_key):
        response = await asyncio.wait_for(
            h.client.post(
                f"{_PREFIX}/topics/-123.7/callbacks",
                json={"entry_id": entry.id, "data": "tsc:r", "revision": 0},
                headers=headers(),
            ),
            1,
        )
        assert response.status == 200
        result = await response.json()
        assert result["text"] == "Tasks refreshed"
        assert result["selector_revision"] == 1
        lower.assert_awaited_once_with(h.bot._orch.task_hub, h.key.chat_id, "tsc:r")
        h.bot.bot_instance.edit_message_text.assert_awaited_once()


async def test_cli_login_status_route_requires_owner(bridge):
    client, *_ = bridge
    response = await client.get(f"{_PREFIX}/cli-login/status", headers=headers())
    assert response.status == 403
    assert "WWW-Authenticate" not in response.headers


@pytest.fixture
async def cli_bridge(bridge, tmp_path, monkeypatch):
    from ductor_bot.cli.auth import AuthResult, AuthStatus
    from ductor_bot.webchat.bridge import CLI_LOGIN

    client, bot, runtime, paths = bridge
    manager = client.server.app[CLI_LOGIN]
    binary = tmp_path / "fake-cli-bin"
    binary.mkdir()
    for provider in ("claude", "codex", "grok"):
        executable = binary / provider
        executable.write_text(
            "#!" + sys.executable + "\n"
            "import os,time,tty,signal\n"
            "from pathlib import Path\n"
            "tty.setraw(0)\n"
            "if os.environ.get('FAKE_STUBBORN'): signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
            "print('https://fake.invalid/login?secret=terminal-output-canary',flush=True)\n"
            "print('Paste code here if prompted >',flush=True)\n"
            "data=os.read(0,4096)\n"
            "Path(os.environ['FAKE_INPUT']).write_bytes(data)\n"
            "while True: time.sleep(.05)\n"
        )
        executable.chmod(0o700)
    manager._env = {
        "PATH": str(binary),
        "HOME": str(tmp_path),
        "CODEX_HOME": str(tmp_path / "codex"),
        "GROK_HOME": str(tmp_path / "grok"),
        "FAKE_INPUT": str(tmp_path / "input.bin"),
    }
    manager._auth_checker = lambda provider: AuthResult(provider, AuthStatus.INSTALLED)
    monkeypatch.setattr(
        "ductor_bot.webchat.cli_login.container_slot_config_dir",
        lambda slot: str(paths.ductor_home / ".cli-auth" / slot),
    )
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.config_path.write_text('{"claude_accounts":["claude"]}')
    bot.config.claude_accounts = ["claude"]
    (paths.ductor_home / ".cli-auth/claude").mkdir(parents=True)
    return client, bot, runtime, paths, manager


def cli_headers(owner="owner-one"):
    return {**headers(), "X-Dtw-Owner": owner}


async def cli_poll(client, job, *, owner="owner-one", finished=False):
    for _ in range(300):
        response = await client.get(f"{_PREFIX}/cli-login/jobs/{job}", headers=cli_headers(owner))
        result = await response.json()
        if result.get("state") != "running" if finished else bool(result.get("output")):
            return result
        await asyncio.sleep(0.01)
    pytest.fail("fake CLI job did not reach expected state")


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "status"),
        ("POST", "jobs"),
        ("GET", "jobs/unknown"),
        ("POST", "jobs/unknown/input"),
        ("POST", "jobs/unknown/cancel"),
    ],
)
async def test_cli_bridge_all_routes_require_bridge_token_and_owner(cli_bridge, method, path):
    client, *_ = cli_bridge
    for supplied in ({}, headers(), {"X-Dtw-Owner": "owner-one"}):
        response = await client.request(
            method, f"{_PREFIX}/cli-login/{path}", headers=supplied, json={}
        )
        assert response.status == 403
        assert "WWW-Authenticate" not in response.headers


async def test_cli_status_returns_ordered_slots_and_cache_without_auth_wait(cli_bridge):
    client, bot, _, paths, manager = cli_bridge
    ready = threading.Event()
    release = threading.Event()

    def slow(provider):
        from ductor_bot.cli.auth import AuthResult, AuthStatus

        ready.set()
        assert release.wait(4)
        return AuthResult(provider, AuthStatus.AUTHENTICATED)

    manager._auth_checker = slow
    bot.config.claude_accounts = ["claude-work", "claude"]
    root = paths.ductor_home / ".cli-auth"
    (root / "claude-work").mkdir()
    (root / "claude-work/credentials.json").write_text("fake")
    (root / "claude-orphan").mkdir()
    (root / "claude-orphan/.credentials.json").write_text("fake")
    (root / "claude-slots").mkdir()
    try:
        response = await asyncio.wait_for(
            client.get(f"{_PREFIX}/cli-login/status", headers=cli_headers()), 0.5
        )
        result = await response.json()
        assert result == {
            "providers": dict.fromkeys(("claude", "codex", "grok")),
            "running": False,
            "provider": None,
            "job_id": None,
            "claude_slots": [
                {"name": "claude-work", "priority": 1, "authenticated": True},
                {"name": "claude", "priority": 2, "authenticated": False},
                {"name": "claude-orphan", "priority": None, "authenticated": True},
            ],
        }
    finally:
        release.set()
    await manager._refresh_task
    result = await (await client.get(f"{_PREFIX}/cli-login/status", headers=cli_headers())).json()
    assert set(result["providers"].values()) == {"authenticated"}


@pytest.mark.parametrize(
    "body",
    [
        [],
        {},
        {"provider": "shell"},
        {"provider": "gemini"},
        {"provider": 7},
        {"provider": "codex", "slot": 1},
        {"provider": "claude", "slot": "../escape"},
        {"provider": "claude", "slot": "work", "new_slot": "yes"},
    ],
)
async def test_cli_start_rejects_invalid_payloads(cli_bridge, body):
    client, *_ = cli_bridge
    response = await client.post(f"{_PREFIX}/cli-login/jobs", headers=cli_headers(), json=body)
    assert response.status == 400


async def test_cli_bridge_job_input_sequence_owner_conflict_cancel_and_no_recording(
    cli_bridge, caplog
):
    client, _, runtime, paths, manager = cli_bridge
    caplog.set_level(logging.DEBUG)
    created = await client.post(
        f"{_PREFIX}/cli-login/jobs", headers=cli_headers(), json={"provider": "codex"}
    )
    assert created.status == 202
    job = (await created.json())["job_id"]
    snapshot = await cli_poll(client, job)
    assert snapshot["urls"] == ["https://fake.invalid/login?secret=terminal-output-canary"]
    assert snapshot["needs_input"]
    after = await client.get(
        f"{_PREFIX}/cli-login/jobs/{job}?after={snapshot['seq']}", headers=cli_headers()
    )
    assert (await after.json())["output"] == []
    for query in ("-1", "not-an-integer"):
        assert (
            await client.get(f"{_PREFIX}/cli-login/jobs/{job}?after={query}", headers=cli_headers())
        ).status == 400
    conflict = await client.post(
        f"{_PREFIX}/cli-login/jobs", headers=cli_headers("owner-two"), json={"provider": "grok"}
    )
    assert conflict.status == 409
    assert await conflict.json() == {"error": "login_running"}
    for owner, expected in (("owner-one", job), ("owner-two", None)):
        response = await client.get(f"{_PREFIX}/cli-login/status", headers=cli_headers(owner))
        assert (await response.json())["job_id"] == expected
    for method, suffix, body in (
        ("GET", "", None),
        ("POST", "/input", {"kind": "line", "value": "other-owner-input"}),
        ("POST", "/cancel", None),
    ):
        response = await client.request(
            method,
            f"{_PREFIX}/cli-login/jobs/{job}{suffix}",
            headers=cli_headers("owner-two"),
            json=body,
        )
        assert response.status == 404
    invalid_other = await client.get(
        f"{_PREFIX}/cli-login/jobs/{job}?after=bad",
        headers=cli_headers("owner-two"),
    )
    assert invalid_other.status == 404
    sent = await client.post(
        f"{_PREFIX}/cli-login/jobs/{job}/input",
        headers=cli_headers(),
        json={"kind": "line", "value": "input-never-log-canary"},
    )
    assert sent.status == 200
    assert await sent.json() == {"ok": True}
    for _ in range(100):
        if (paths.ductor_home / "input.bin").exists():
            break
        await asyncio.sleep(0.01)
    assert (paths.ductor_home / "input.bin").read_bytes() == b"input-never-log-canary\r"
    canceled = await client.post(f"{_PREFIX}/cli-login/jobs/{job}/cancel", headers=cli_headers())
    assert canceled.status == 202
    assert (await cli_poll(client, job, finished=True))["state"] == "cancelled"
    assert "input-never-log-canary" not in caplog.text
    assert "terminal-output-canary" not in caplog.text
    runtime.outbox.pending.assert_not_awaited()
    assert not paths.chat_log_dir.exists()
    assert manager._jobs[job].process is None


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "line", "value": ""},
        {"kind": "key", "value": "1"},
        {"kind": "key", "value": "10"},
        {"kind": [], "value": "x"},
        {"kind": "key", "value": 1},
    ],
)
async def test_cli_input_invalid_contract(cli_bridge, body):
    client, *_ = cli_bridge
    created = await client.post(
        f"{_PREFIX}/cli-login/jobs", headers=cli_headers(), json={"provider": "codex"}
    )
    job = (await created.json())["job_id"]
    await cli_poll(client, job)
    response = await client.post(
        f"{_PREFIX}/cli-login/jobs/{job}/input", headers=cli_headers(), json=body
    )
    assert response.status == 400


async def test_cli_slot_request_reaches_preparation_and_shutdown_cleans_it(cli_bridge):
    client, _, _, paths, manager = cli_bridge
    response = await client.post(
        f"{_PREFIX}/cli-login/jobs",
        headers=cli_headers(),
        json={"provider": "claude", "slot": "web", "new_slot": True},
    )
    assert response.status == 202
    job = (await response.json())["job_id"]
    await cli_poll(client, job)
    directory = paths.ductor_home / ".cli-auth/claude-web"
    assert directory.is_dir()
    assert manager._slots is not None
    process = manager._jobs[job].process
    await client.close()
    assert process.returncode is not None
    assert not directory.exists()
    assert manager._closed


async def test_cli_status_empty_config_still_lists_default_slot(cli_bridge):
    client, bot, _, _, _ = cli_bridge
    bot.config.claude_accounts = []
    response = await client.get(f"{_PREFIX}/cli-login/status", headers=cli_headers())
    assert (await response.json())["claude_slots"] == [
        {"name": "claude", "priority": 1, "authenticated": False}
    ]


async def test_cli_cancel_acknowledges_before_five_second_child_cleanup(cli_bridge):
    client, _, _, _, manager = cli_bridge
    manager._env["FAKE_STUBBORN"] = "1"
    response = await client.post(
        f"{_PREFIX}/cli-login/jobs", headers=cli_headers(), json={"provider": "codex"}
    )
    job = (await response.json())["job_id"]
    await cli_poll(client, job)
    response = await asyncio.wait_for(
        client.post(f"{_PREFIX}/cli-login/jobs/{job}/cancel", headers=cli_headers()), 0.5
    )
    assert response.status == 202
    assert not manager._jobs[job].task.done()
    assert (
        await client.get(f"{_PREFIX}/cli-login/jobs/{job}", headers=cli_headers())
    ).status == 200
    await asyncio.wait_for(manager._jobs[job].task, 7)
    assert manager.get(job, "owner-one")["state"] == "cancelled"


@pytest.mark.parametrize(
    ("method", "suffix"), [("GET", ""), ("POST", "/input"), ("POST", "/cancel")]
)
async def test_cli_unknown_jobs_return_json_404(cli_bridge, method, suffix):
    client, *_ = cli_bridge
    response = await client.request(
        method,
        f"{_PREFIX}/cli-login/jobs/unknown{suffix}",
        headers=cli_headers(),
        json={"kind": "line", "value": "1"},
    )
    assert response.status == 404
    assert await response.json() == {"error": "login_not_found"}
