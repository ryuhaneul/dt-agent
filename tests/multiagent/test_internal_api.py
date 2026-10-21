"""Tests for multiagent/internal_api.py: InternalAgentAPI HTTP endpoints."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient

from ductor_bot.config import AgentConfig
from ductor_bot.multiagent.bus import InterAgentBus
from ductor_bot.multiagent.health import AgentHealth
from ductor_bot.multiagent.internal_api import InternalAgentAPI, _normalise_transport

_ASYNC_TOOL = (
    Path(__file__).resolve().parents[2]
    / "ductor_bot/_home_defaults/workspace/tools/agent_tools/ask_agent_async.py"
)


def _async_tool_body(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], target: str, *, silent: bool = False
) -> dict[str, object]:
    for key in (
        "DUCTOR_AGENT_NAME",
        "DUCTOR_CHAT_ID",
        "DUCTOR_TOPIC_ID",
        "DUCTOR_TRANSPORT",
        "DUCTOR_IA_TURN",
        "DUCTOR_ORIGIN_CHAT_ID",
        "DUCTOR_ORIGIN_TOPIC_ID",
        "DUCTOR_ORIGIN_TRANSPORT",
        "DUCTOR_RESULT_ORIGIN",
        "DUCTOR_BROADCAST_REF",
    ):
        if key in env:
            monkeypatch.setenv(key, env[key])
        else:
            monkeypatch.delenv(key, raising=False)

    spec = importlib.util.spec_from_file_location("ask_agent_async_test_tool", _ASYNC_TOOL)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(
        {"success": True, "task_id": "task-1"}
    ).encode()
    body: dict[str, object] = {}

    def urlopen(request: object, timeout: int) -> MagicMock:
        assert timeout == 10
        body.update(json.loads(request.data.decode()))
        return response

    monkeypatch.setattr(mod.urllib.request, "urlopen", urlopen)
    flags = ["--silent"] if silent else []
    monkeypatch.setattr(sys, "argv", ["ask_agent_async.py", *flags, target, "next task"])
    mod.main()
    return body


@pytest.fixture
def bus() -> InterAgentBus:
    return InterAgentBus()


@pytest.fixture
def api(bus: InterAgentBus) -> InternalAgentAPI:
    return InternalAgentAPI(bus, port=0)


@pytest.fixture
async def client(api: InternalAgentAPI) -> TestClient:
    """Create aiohttp test client for the internal API."""
    from aiohttp.test_utils import TestServer

    server = TestServer(api._app)
    c = TestClient(server)
    await c.start_server()
    yield c
    await c.close()


def test_async_tool_displays_headless_result_receiver_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    spec = importlib.util.spec_from_file_location("ask_agent_async_headless_test", _ASYNC_TOOL)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(
        {"success": False, "error": "headless agent 'worker' cannot receive async results"}
    ).encode()
    monkeypatch.setattr(mod.urllib.request, "urlopen", lambda *_args, **_kwargs: response)
    monkeypatch.setattr(
        sys, "argv", ["ask_agent_async.py", "--reply-to", "worker", "main", "do work"]
    )

    with pytest.raises(SystemExit) as exc:
        mod.main()

    assert exc.value.code == 1
    assert "Error: headless agent 'worker' cannot receive async results" in capsys.readouterr().err


def test_internal_api_binds_loopback(api: InternalAgentAPI) -> None:
    assert api._bind_host == "127.0.0.1"


class TestHandleSend:
    """Test POST /interagent/send."""

    async def test_send_success(self, client: TestClient, bus: InterAgentBus) -> None:
        stack = MagicMock()
        stack.bot.orchestrator = MagicMock()
        stack.bot.orchestrator.handle_interagent_message = AsyncMock(
            return_value=("OK", "ia-sender", "")
        )
        bus.register("target", stack)

        resp = await client.post(
            "/interagent/send",
            json={"from": "sender", "to": "target", "message": "Hello"},
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["success"] is True
        assert data["text"] == "OK"

    async def test_send_missing_fields(self, client: TestClient) -> None:
        resp = await client.post(
            "/interagent/send",
            json={"from": "sender"},
        )
        assert resp.status == 400
        data = await resp.json()
        assert data["success"] is False
        assert "Missing" in data["error"]

    async def test_send_invalid_json(self, client: TestClient) -> None:
        resp = await client.post(
            "/interagent/send",
            data=b"not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400

    async def test_send_unknown_recipient(self, client: TestClient) -> None:
        resp = await client.post(
            "/interagent/send",
            json={"from": "sender", "to": "nonexistent", "message": "Hello"},
        )
        data = await resp.json()
        assert data["success"] is False
        assert "not found" in data["error"]


class TestHandleSendAsync:
    """Test POST /interagent/send_async."""

    async def test_send_async_success(self, client: TestClient, bus: InterAgentBus) -> None:
        stack = MagicMock()
        stack.bot.orchestrator = MagicMock()
        stack.bot.orchestrator.handle_interagent_message = AsyncMock(
            return_value=("OK", "ia-sender", "")
        )
        bus.register("target", stack)
        bus.register("sender", MagicMock())
        bus.set_async_result_handler("sender", AsyncMock())

        resp = await client.post(
            "/interagent/send_async",
            json={"from": "sender", "to": "target", "message": "Hello"},
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["success"] is True
        assert "task_id" in data

    def test_normalise_transport_aliases(self) -> None:
        assert _normalise_transport("telegram") == "tg"
        assert _normalise_transport("matrix") == "mx"
        assert _normalise_transport("slack") == "sl"
        assert _normalise_transport("mx") == "mx"

    async def test_send_async_unknown_recipient(self, client: TestClient) -> None:
        resp = await client.post(
            "/interagent/send_async",
            json={"from": "sender", "to": "nonexistent", "message": "Hello"},
        )
        data = await resp.json()
        assert data["success"] is False
        assert "not found" in data["error"]

    async def test_send_async_missing_fields(self, client: TestClient) -> None:
        resp = await client.post(
            "/interagent/send_async",
            json={"from": "sender"},
        )
        assert resp.status == 400


class TestNewSessionFlag:
    """Test new_session flag in /interagent/send and /interagent/send_async."""

    async def test_send_passes_new_session_true(
        self, client: TestClient, bus: InterAgentBus
    ) -> None:
        stack = MagicMock()
        stack.bot.orchestrator = MagicMock()
        stack.bot.orchestrator.handle_interagent_message = AsyncMock(
            return_value=("OK", "ia-sender", "")
        )
        bus.register("target", stack)

        resp = await client.post(
            "/interagent/send",
            json={
                "from": "sender",
                "to": "target",
                "message": "Hello",
                "new_session": True,
                "chat_id": 777,
                "topic_id": 10,
                "transport": "mx",
            },
        )
        assert resp.status == 200
        stack.bot.orchestrator.handle_interagent_message.assert_awaited_once_with(
            "sender",
            "Hello",
            new_session=True,
            source_chat_id=777,
            source_topic_id=10,
            source_transport="mx",
        )

    async def test_send_defaults_new_session_false(
        self, client: TestClient, bus: InterAgentBus
    ) -> None:
        stack = MagicMock()
        stack.bot.orchestrator = MagicMock()
        stack.bot.orchestrator.handle_interagent_message = AsyncMock(
            return_value=("OK", "ia-sender", "")
        )
        bus.register("target", stack)

        resp = await client.post(
            "/interagent/send",
            json={"from": "sender", "to": "target", "message": "Hello"},
        )
        assert resp.status == 200
        stack.bot.orchestrator.handle_interagent_message.assert_awaited_once_with(
            "sender",
            "Hello",
            new_session=False,
        )

    async def test_send_async_passes_new_session(
        self, client: TestClient, bus: InterAgentBus
    ) -> None:
        stack = MagicMock()
        stack.bot.orchestrator = MagicMock()
        stack.bot.orchestrator.handle_interagent_message = AsyncMock(
            return_value=("OK", "ia-sender", "")
        )
        bus.register("target", stack)
        bus.register("sender", MagicMock())
        bus.set_async_result_handler("sender", AsyncMock())

        resp = await client.post(
            "/interagent/send_async",
            json={
                "from": "sender",
                "to": "target",
                "message": "Hello",
                "new_session": True,
            },
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["success"] is True


class TestHandleList:
    """Test GET /interagent/agents."""

    async def test_list_empty(self, client: TestClient) -> None:
        resp = await client.get("/interagent/agents")
        assert resp.status == 200
        data = await resp.json()
        assert data["agents"] == []

    async def test_list_with_agents(self, client: TestClient, bus: InterAgentBus) -> None:
        bus.register("main", MagicMock())
        bus.register("sub1", MagicMock())

        resp = await client.get("/interagent/agents")
        data = await resp.json()
        assert set(data["agents"]) == {"main", "sub1"}


class TestHandleHealth:
    """Test GET /interagent/health."""

    async def test_health_no_ref(self, client: TestClient) -> None:
        resp = await client.get("/interagent/health")
        data = await resp.json()
        assert data["agents"] == {}

    async def test_health_with_agents(self, client: TestClient, api: InternalAgentAPI) -> None:
        h = AgentHealth(name="main")
        h.mark_running()
        api.set_health_ref({"main": h})

        resp = await client.get("/interagent/health")
        data = await resp.json()
        assert "main" in data["agents"]
        assert data["agents"]["main"]["status"] == "running"
        assert data["agents"]["main"]["restart_count"] == 0

    async def test_health_crashed_agent(self, client: TestClient, api: InternalAgentAPI) -> None:
        h = AgentHealth(name="sub1")
        h.mark_crashed("OOM")
        api.set_health_ref({"sub1": h})

        resp = await client.get("/interagent/health")
        data = await resp.json()
        assert data["agents"]["sub1"]["status"] == "crashed"
        assert data["agents"]["sub1"]["last_crash_error"] == "OOM"
        assert data["agents"]["sub1"]["restart_count"] == 1


_TASK_TOOLS = (
    Path(__file__).resolve().parents[2] / "ductor_bot/_home_defaults/workspace/tools/task_tools"
)


class TestHandleTaskResume:
    @staticmethod
    def _hub(owner: str = "main") -> MagicMock:
        hub = MagicMock()
        hub.resume = MagicMock(return_value="task-1")
        hub.registry.get.return_value = MagicMock(parent_agent=owner)
        return hub

    async def test_forwards_requesting_chat_and_topic(
        self, client: TestClient, api: InternalAgentAPI
    ) -> None:
        hub = self._hub()
        api.set_task_hub(hub)
        resp = await client.post(
            "/tasks/resume",
            json={
                "task_id": "task-1",
                "prompt": "go",
                "from": "main",
                "chat_id": -1001,
                "topic_id": 7,
            },
        )
        assert resp.status == 200
        assert (await resp.json())["success"] is True
        hub.resume.assert_called_once_with(
            "task-1", "go", parent_agent="main", chat_id=-1001, thread_id=7,
            origin_transport="", origin_session_kind="", origin_session_id="",
        )

    async def test_chat_without_topic_means_no_topic(
        self, client: TestClient, api: InternalAgentAPI
    ) -> None:
        hub = self._hub()
        api.set_task_hub(hub)
        await client.post(
            "/tasks/resume",
            json={"task_id": "task-1", "prompt": "go", "from": "main", "chat_id": 42},
        )
        hub.resume.assert_called_once_with(
            "task-1", "go", parent_agent="main", chat_id=42, thread_id=None,
            origin_transport="", origin_session_kind="", origin_session_id="",
        )

    async def test_without_routing_info_keeps_stored_routing(
        self, client: TestClient, api: InternalAgentAPI
    ) -> None:
        hub = self._hub()
        api.set_task_hub(hub)
        await client.post(
            "/tasks/resume",
            json={"task_id": "task-1", "prompt": "go", "from": "main", "topic_id": 7},
        )
        hub.resume.assert_called_once_with(
            "task-1", "go", parent_agent="main", chat_id=None, thread_id=7,
            origin_transport="", origin_session_kind="", origin_session_id="",
        )

    async def test_invalid_routing_fields_rejected(
        self, client: TestClient, api: InternalAgentAPI
    ) -> None:
        hub = self._hub()
        api.set_task_hub(hub)
        resp = await client.post(
            "/tasks/resume",
            json={"task_id": "task-1", "prompt": "go", "from": "main", "chat_id": "abc"},
        )
        assert (await resp.json())["success"] is False
        hub.resume.assert_not_called()

    async def test_foreign_task_still_forbidden(
        self, client: TestClient, api: InternalAgentAPI
    ) -> None:
        hub = self._hub(owner="other")
        api.set_task_hub(hub)
        resp = await client.post(
            "/tasks/resume",
            json={"task_id": "task-1", "prompt": "go", "from": "main", "chat_id": 42},
        )
        assert resp.status == 403
        hub.resume.assert_not_called()


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"DUCTOR_CHAT_ID": "42", "DUCTOR_TOPIC_ID": "200"}, {"chat_id": 42, "topic_id": 200}),
        ({"DUCTOR_CHAT_ID": "-1001"}, {"chat_id": -1001}),
        ({"DUCTOR_TOPIC_ID": "200"}, {}),
        ({}, {}),
    ],
)
def test_resume_tool_sends_current_chat_and_topic(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], expected: dict[str, int]
) -> None:
    for key in ("DUCTOR_CHAT_ID", "DUCTOR_TOPIC_ID"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.syspath_prepend(str(_TASK_TOOLS))
    import _shared  # type: ignore[import-not-found]

    sent: dict[str, object] = {}

    def fake_post(_url: str, body: dict[str, object], **_kwargs: object) -> dict[str, object]:
        sent.update(body)
        return {"success": True}

    monkeypatch.setattr(_shared, "post_json", fake_post)
    monkeypatch.setattr(_shared, "get_api_url", lambda path: f"http://127.0.0.1{path}")
    monkeypatch.setattr(sys, "argv", ["resume_task.py", "task-1", "follow up"])
    spec = importlib.util.spec_from_file_location(
        "resume_task_under_test", _TASK_TOOLS / "resume_task.py"
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.main()
    assert {k: sent[k] for k in ("chat_id", "topic_id") if k in sent} == expected
    assert sent["task_id"] == "task-1"
    assert sent["prompt"] == "follow up"
class TestHandleTaskCancel:
    async def test_cancel_logs_sender(
        self,
        client: TestClient,
        api: InternalAgentAPI,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        hub = MagicMock()
        hub.cancel = AsyncMock(return_value=True)
        hub.registry.get.return_value = MagicMock(parent_agent="main")
        api.set_task_hub(hub)
        caplog.set_level("INFO", logger="ductor_bot.multiagent.internal_api")

        resp = await client.post(
            "/tasks/cancel",
            json={"task_id": "task-123", "from": "main"},
        )

        assert resp.status == 200
        data = await resp.json()
        assert data["success"] is True
        hub.cancel.assert_awaited_once_with("task-123")

        messages = "\n".join(record.getMessage() for record in caplog.records)
        assert "Task cancel via API id=task-123 from=main success=True" in messages


class TestLifecycle:
    """Test InternalAgentAPI lifecycle return values."""

    async def test_start_returns_false_on_bind_error(self, api: InternalAgentAPI) -> None:
        from unittest.mock import patch

        with patch(
            "aiohttp.web.TCPSite.start",
            new_callable=AsyncMock,
            side_effect=OSError("bind failed"),
        ):
            started = await api.start()

        assert started is False


def test_parse_origin_preserves_invalid_topic_for_notification_guard() -> None:
    from ductor_bot.multiagent.internal_api import _parse_origin

    assert _parse_origin({"chat_id": 0, "topic_id": 17185}) == (0, 17185)


async def test_ia_turn_async_rejected_before_task_creation(
    client: TestClient, bus: InterAgentBus
) -> None:
    with patch.object(bus, "send_async") as send_async:
        resp = await client.post(
            "/interagent/send_async",
            json={
                "from": "developer",
                "to": "main",
                "message": "Task",
                "ia_turn": True,
                "reply_to": "main",
            },
        )
    data = await resp.json()
    assert resp.status == 200
    assert data["success"] is False
    assert "Async sends are not allowed during a delegated task" in data["error"]
    send_async.assert_not_called()
    assert bus._async_tasks == {}


async def test_ia_turn_sync_remains_allowed(client: TestClient, bus: InterAgentBus) -> None:
    stack = MagicMock()
    stack.bot.orchestrator.handle_interagent_message = AsyncMock(return_value=("ok", "ia-dev", ""))
    bus.register("main", stack)
    resp = await client.post(
        "/interagent/send",
        json={"from": "developer", "to": "main", "message": "Question", "ia_turn": True},
    )
    assert (await resp.json())["success"] is True
    stack.bot.orchestrator.handle_interagent_message.assert_awaited_once()


async def test_topic_only_origin_is_logged_without_changing_sync_request(
    client: TestClient, bus: InterAgentBus, caplog: pytest.LogCaptureFixture
) -> None:
    stack = MagicMock()
    stack.bot.orchestrator.handle_interagent_message = AsyncMock(return_value=("ok", "ia-dev", ""))
    bus.register("main", stack)
    resp = await client.post(
        "/interagent/send",
        json={
            "from": "developer",
            "to": "main",
            "message": "Question",
            "chat_id": 0,
            "topic_id": 10,
        },
    )
    assert (await resp.json())["success"] is True
    assert "Invalid inter-agent origin" in caplog.text


async def test_topic_only_async_origin_is_logged_before_acceptance(
    client: TestClient, bus: InterAgentBus, caplog: pytest.LogCaptureFixture
) -> None:
    bus.register("developer", MagicMock())
    bus.register("main", MagicMock())
    bus.set_async_result_handler("developer", AsyncMock())
    with patch.object(bus, "send_async", return_value="task-1") as send_async:
        resp = await client.post(
            "/interagent/send_async",
            json={
                "from": "developer",
                "to": "main",
                "message": "Task",
                "chat_id": 0,
                "topic_id": 10,
            },
        )
    assert (await resp.json())["success"] is True
    assert "Invalid inter-agent origin" in caplog.text
    assert send_async.call_args.kwargs["opts"].topic_id == 10


async def test_interagent_result_injection_label_can_send_async(
    client: TestClient,
    bus: InterAgentBus,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An async result injection label is not an IA delegation turn."""
    from ductor_bot.cli.base import CLIConfig
    from ductor_bot.cli.executor import build_subprocess_env

    env = build_subprocess_env(
        CLIConfig(
            ductor_home=tmp_path,
            agent_name="main",
            chat_id=12345,
            transport="tg",
            process_label="interagent:envelope-1",
        )
    )
    assert "DUCTOR_IA_TURN" not in env
    body = _async_tool_body(monkeypatch, env, "developer")
    assert "ia_turn" not in body

    bus.register("main", MagicMock())
    bus.register("developer", MagicMock())
    bus.set_async_result_handler("main", AsyncMock())
    with patch.object(bus, "send_async", return_value="task-1") as send_async:
        resp = await client.post("/interagent/send_async", json=body)
    assert (await resp.json())["success"] is True
    send_async.assert_called_once()


async def test_ia_environment_tool_request_is_rejected_before_enqueue(
    client: TestClient,
    bus: InterAgentBus,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Follow the standard tool path from IA CLI env to API admission."""
    from ductor_bot.cli.base import CLIConfig
    from ductor_bot.cli.executor import build_subprocess_env

    env = build_subprocess_env(
        CLIConfig(
            ductor_home=tmp_path,
            agent_name="developer",
            chat_id=12345,
            transport="tg",
            origin_chat_id=-100777,
            origin_topic_id=10,
            origin_transport="tg",
            interagent_turn=True,
        )
    )
    body = _async_tool_body(monkeypatch, env, "main")
    assert body["ia_turn"] is True
    assert body["chat_id"] == -100777
    assert body["topic_id"] == 10
    bus.register("developer", MagicMock())
    bus.register("main", MagicMock())
    bus.set_async_result_handler("developer", AsyncMock())
    with patch.object(bus, "send_async", return_value="task-1") as send_async:
        resp = await client.post("/interagent/send_async", json=body)
    data = await resp.json()
    assert data["success"] is False
    assert "Async sends are not allowed during a delegated task" in data["error"]
    send_async.assert_not_called()
    assert bus._async_tasks == {}


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        ({}, ("", "")),
        (
            {"result_origin": "cron-broadcast", "broadcast_ref": "cron-job-1"},
            ("cron-broadcast", "cron-job-1"),
        ),
    ],
)
async def test_async_routing_fields_survive_http_task_and_result(
    client: TestClient,
    bus: InterAgentBus,
    extra: dict[str, str],
    expected: tuple[str, str],
) -> None:
    gate = asyncio.Event()
    received = asyncio.Event()
    results = []

    async def run_target(*_args: object, **_kwargs: object) -> tuple[str, str, str]:
        await gate.wait()
        return "done", "ia-main", ""

    async def receive(result: object) -> None:
        results.append(result)
        received.set()

    main = MagicMock()
    target = MagicMock()
    target.bot.orchestrator.handle_interagent_message = run_target
    bus.register("main", main)
    bus.register("target", target)
    bus.set_async_result_handler("main", receive)

    response = await client.post(
        "/interagent/send_async",
        json={"from": "main", "to": "target", "message": "work", **extra},
    )
    data = await response.json()
    assert data["success"] is True
    task = bus._async_tasks[data["task_id"]]
    assert (task.result_origin, task.broadcast_ref) == expected

    gate.set()
    await asyncio.wait_for(received.wait(), timeout=1)
    assert (results[0].result_origin, results[0].broadcast_ref) == expected


def test_async_tool_forwards_routing_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _async_tool_body(
        monkeypatch,
        {
            "DUCTOR_AGENT_NAME": "main",
            "DUCTOR_RESULT_ORIGIN": "cron-broadcast",
            "DUCTOR_BROADCAST_REF": "cron-job-1",
        },
        "target",
    )
    assert body["result_origin"] == "cron-broadcast"
    assert body["broadcast_ref"] == "cron-job-1"



async def test_cron_broadcast_env_survives_tool_api_result_and_record(
    bus: InterAgentBus,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from ductor_bot.bus.envelope import DeliveryOutcome
    from ductor_bot.multiagent.persistence import AsyncRecordStore

    bus._record_store = AsyncRecordStore(tmp_path)
    received = asyncio.Event()
    saved: dict[str, object] = {}

    async def run_target(*_args: object, **_kwargs: object) -> tuple[str, str, str]:
        return "done", "ia-main", ""

    async def receive(result: object) -> DeliveryOutcome:
        record = bus._record_store.load("main", result.task_id)
        saved.update({
            "result_origin": record["result_origin"],
            "broadcast_ref": record["broadcast_ref"],
            "result": bus._record_store.result_from_record(record),
        })
        received.set()
        return DeliveryOutcome(ok=True)

    main = MagicMock()
    target = MagicMock()
    target.bot.orchestrator.handle_interagent_message = run_target
    bus.register("main", main)
    bus.register("target", target)
    bus.set_async_result_handler("main", receive)

    body = _async_tool_body(
        monkeypatch,
        {
            "DUCTOR_AGENT_NAME": "main",
            "DUCTOR_TRANSPORT": "tg",
            "DUCTOR_RESULT_ORIGIN": "cron-broadcast",
            "DUCTOR_BROADCAST_REF": "job-1",
        },
        "target",
    )
    response = await client.post("/interagent/send_async", json=body)
    assert (await response.json())["success"] is True
    await asyncio.wait_for(received.wait(), timeout=1)
    assert (saved["result_origin"], saved["broadcast_ref"]) == ("cron-broadcast", "job-1")
    result = saved["result"]
    assert (result.result_origin, result.broadcast_ref) == ("cron-broadcast", "job-1")


async def test_cron_clean_parent_env_has_registered_sender(
    bus: InterAgentBus,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from ductor_bot.cli.codex_cache import CodexModelCache
    from ductor_bot.config import AgentConfig
    from ductor_bot.cron.execution import OneShotCommand
    from ductor_bot.cron.manager import CronJob, CronManager
    from ductor_bot.cron.observer import CronObserver
    from ductor_bot.workspace.paths import DuctorPaths

    for key in tuple(os.environ):
        if key.startswith("DUCTOR_"):
            monkeypatch.delenv(key)
    paths = DuctorPaths(
        ductor_home=tmp_path / "home",
        home_defaults=tmp_path / "defaults",
        framework_root=tmp_path,
    )
    folder = paths.cron_tasks_dir / "job"
    folder.mkdir(parents=True)
    manager = CronManager(jobs_path=paths.cron_jobs_path)
    manager.add_job(CronJob(
        id="job", title="Job", description="", schedule="0 0 * * *",
        task_folder="job", agent_instruction="work", chat_id=0,
    ))
    observer = CronObserver(
        paths, manager, config=AgentConfig(interagent_port=9123),
        codex_cache=CodexModelCache("", []), agent_name="main",
    )
    observer.set_result_handler(AsyncMock(return_value=(True, "")))
    captured: dict[str, str] = {}
    proc = AsyncMock()
    proc.communicate.return_value = (b'{"result":"done"}', b"")
    proc.returncode = 0

    async def spawn(*_args: object, **kwargs: object) -> AsyncMock:
        captured.update(kwargs["env"])
        return proc

    with (
        patch("ductor_bot.cron.execution.build_cmd", return_value=OneShotCommand(cmd=["cli"])),
        patch("asyncio.create_subprocess_exec", side_effect=spawn),
    ):
        await observer._execute_job_inner("job", "work", "job")

    assert captured["DUCTOR_AGENT_NAME"] == "main"
    assert captured["DUCTOR_INTERAGENT_PORT"] == "9123"
    body = _async_tool_body(
        monkeypatch,
        {key: value for key, value in captured.items() if key.startswith("DUCTOR_")},
        "target",
    )
    assert body["from"] == "main"
    assert body["result_origin"] == "cron-broadcast"
    assert body["broadcast_ref"] == "job"
    target = MagicMock()
    target.bot.orchestrator.handle_interagent_message = AsyncMock(
        return_value=("done", "ia-main", "")
    )
    bus.register("main", MagicMock())
    bus.register("target", target)
    bus.set_async_result_handler("main", AsyncMock())
    response = await client.post("/interagent/send_async", json=body)
    assert (await response.json())["success"] is True


@pytest.mark.parametrize(
    ("routing", "expected"),
    [
        (
            {"DUCTOR_TRANSPORT": "tg", "DUCTOR_RESULT_ORIGIN": "cron-broadcast",
             "DUCTOR_BROADCAST_REF": "job"},
            {"DUCTOR_RESULT_ORIGIN": "cron-broadcast", "DUCTOR_BROADCAST_REF": "job"},
        ),
        (
            {"DUCTOR_TRANSPORT": "tg", "DUCTOR_CHAT_ID": "-100123"},
            {"DUCTOR_CHAT_ID": "-100123"},
        ),
    ],
)
@pytest.mark.parametrize("source", ["parent", "dotenv", "command"])
async def test_oneshot_routing_clears_inherited_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    routing: dict[str, str],
    expected: dict[str, str],
    source: str,
) -> None:
    from ductor_bot.cli.param_resolver import TaskExecutionConfig, TaskOverrides
    from ductor_bot.cron.execution import OneShotCommand
    from ductor_bot.infra.task_runner import execute_in_task_folder

    contaminated = {
        "DUCTOR_CHAT_ID": "-100999",
        "DUCTOR_TOPIC_ID": "77",
        "DUCTOR_TRANSPORT": "mx",
        "DUCTOR_RESULT_ORIGIN": "old",
        "DUCTOR_BROADCAST_REF": "old",
        "DUCTOR_ORIGIN_CHAT_ID": "-100888",
        "DUCTOR_ORIGIN_TOPIC_ID": "6",
        "DUCTOR_ORIGIN_TRANSPORT": "mx",
        "DUCTOR_ORIGIN_EXTRA": "old",
        "DUCTOR_IA_TURN": "1",
        "DUCTOR_IA_EXTRA": "old",
        "DUCTOR_AGENT_NAME": "other",
        "DUCTOR_INTERAGENT_PORT": "9999",
    }
    for key, value in contaminated.items():
        if source == "parent":
            monkeypatch.setenv(key, value)
        else:
            monkeypatch.delenv(key, raising=False)
    if source == "dotenv":
        (tmp_path / ".env").write_text(
            "".join(f"{key}={value}\n" for key, value in contaminated.items())
        )
    folder = tmp_path / "cron_tasks" / "job"
    folder.mkdir(parents=True)
    observer = MagicMock()
    observer._paths.ductor_home = tmp_path
    observer._config.interagent_port = 9123
    observer._agent_name = "worker"
    observer.resolve_execution_config.return_value = TaskExecutionConfig(
        provider="claude", model="opus", reasoning_effort="", cli_parameters=[],
        permission_mode="bypassPermissions", working_dir=str(tmp_path), file_access="all",
    )
    captured: dict[str, str] = {}
    proc = AsyncMock()
    proc.communicate.return_value = (b'{"result":"done"}', b"")
    proc.returncode = 0

    async def spawn(*_args: object, **kwargs: object) -> AsyncMock:
        captured.update(kwargs["env"])
        return proc

    with (
        patch(
            "ductor_bot.cron.execution.build_cmd",
            return_value=OneShotCommand(
                cmd=["cli"],
                env_overrides=contaminated.copy() if source == "command" else {},
            ),
        ),
        patch("asyncio.create_subprocess_exec", side_effect=spawn),
    ):
        await execute_in_task_folder(
            observer, cron_tasks_dir=tmp_path / "cron_tasks", task_folder="job",
            instruction="work", overrides=TaskOverrides(), dependency=None,
            task_id="job", task_label="Cron job", timeout_seconds=1,
            routing_env=routing,
        )
    assert captured["DUCTOR_AGENT_NAME"] == "worker"
    assert captured["DUCTOR_AGENT_ROLE"] == "sub"
    assert captured["DUCTOR_INTERAGENT_PORT"] == "9123"
    for key, value in expected.items():
        assert captured[key] == value
    for key in (
        "DUCTOR_ORIGIN_CHAT_ID", "DUCTOR_ORIGIN_TOPIC_ID", "DUCTOR_ORIGIN_TRANSPORT",
        "DUCTOR_ORIGIN_EXTRA", "DUCTOR_IA_TURN", "DUCTOR_IA_EXTRA",
    ):
        assert key not in captured
    for key in (
        "DUCTOR_CHAT_ID", "DUCTOR_TOPIC_ID", "DUCTOR_RESULT_ORIGIN",
        "DUCTOR_BROADCAST_REF",
    ):
        if key not in expected:
            assert key not in captured
    body = _async_tool_body(
        monkeypatch,
        {key: value for key, value in captured.items() if key.startswith("DUCTOR_")},
        "target",
    )
    assert "ia_turn" not in body
    assert body.get("chat_id") == (int(expected["DUCTOR_CHAT_ID"]) if "DUCTOR_CHAT_ID" in expected else None)
    assert "topic_id" not in body


@pytest.mark.parametrize("source", ["parent", "dotenv", "command"])
async def test_webhook_cron_task_tool_ignores_parent_route(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    source: str,
) -> None:
    from ductor_bot.cli.codex_cache import CodexModelCache
    from ductor_bot.cli.param_resolver import TaskOverrides
    from ductor_bot.config import AgentConfig
    from ductor_bot.cron.execution import OneShotCommand
    from ductor_bot.webhook.manager import WebhookManager
    from ductor_bot.webhook.observer import WebhookObserver
    from ductor_bot.workspace.paths import DuctorPaths

    contaminated = {
        "DUCTOR_CHAT_ID": "-100999", "DUCTOR_TOPIC_ID": "77",
        "DUCTOR_TRANSPORT": "mx", "DUCTOR_RESULT_ORIGIN": "old",
        "DUCTOR_BROADCAST_REF": "old", "DUCTOR_ORIGIN_CHAT_ID": "-100888",
        "DUCTOR_ORIGIN_TOPIC_ID": "6", "DUCTOR_ORIGIN_TRANSPORT": "mx",
        "DUCTOR_IA_TURN": "1", "DUCTOR_AGENT_NAME": "other",
        "DUCTOR_INTERAGENT_PORT": "9999",
    }
    for key, value in contaminated.items():
        if source == "parent":
            monkeypatch.setenv(key, value)
        else:
            monkeypatch.delenv(key, raising=False)
    paths = DuctorPaths(
        ductor_home=tmp_path / "home",
        home_defaults=tmp_path / "defaults",
        framework_root=tmp_path,
    )
    (paths.cron_tasks_dir / "job").mkdir(parents=True)
    if source == "dotenv":
        (paths.ductor_home / ".env").write_text(
            "".join(f"{key}={value}\n" for key, value in contaminated.items())
        )
    observer = WebhookObserver(
        paths, WebhookManager(hooks_path=paths.webhooks_path),
        config=AgentConfig(interagent_port=9123),
        codex_cache=CodexModelCache("", []), agent_name="main",
    )
    captured: dict[str, str] = {}
    proc = AsyncMock()
    proc.communicate.return_value = (b'{"result":"done"}', b"")
    proc.returncode = 0

    async def spawn(*_args: object, **kwargs: object) -> AsyncMock:
        captured.update(kwargs["env"])
        return proc

    with (
        patch(
            "ductor_bot.cron.execution.build_cmd",
            return_value=OneShotCommand(
                cmd=["cli"],
                env_overrides=contaminated.copy() if source == "command" else {},
            ),
        ),
        patch("asyncio.create_subprocess_exec", side_effect=spawn),
    ):
        result = await observer._dispatch_cron_task(
            "hook", "Hook", "job", "work", TaskOverrides()
        )
    assert result.status == "success"
    assert captured["DUCTOR_AGENT_NAME"] == "main"
    assert captured["DUCTOR_INTERAGENT_PORT"] == "9123"
    for key in (
        "DUCTOR_CHAT_ID", "DUCTOR_TOPIC_ID", "DUCTOR_ORIGIN_CHAT_ID",
        "DUCTOR_ORIGIN_TOPIC_ID", "DUCTOR_ORIGIN_TRANSPORT", "DUCTOR_IA_TURN",
    ):
        assert key not in captured
    assert captured["DUCTOR_TRANSPORT"] == "tg"
    body = _async_tool_body(
        monkeypatch,
        {key: value for key, value in captured.items() if key.startswith("DUCTOR_")},
        "target",
    )
    assert body["from"] == "main"
    assert body["result_origin"] == "cron-broadcast"
    assert body["broadcast_ref"] == "hook"
    assert "chat_id" not in body
    assert "topic_id" not in body
    assert "ia_turn" not in body



async def test_isolated_broadcast_redelegation_preserves_origin(
    bus: InterAgentBus,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from ductor_bot.bus.adapters import from_interagent_result
    from ductor_bot.bus.bus import MessageBus
    from ductor_bot.bus.envelope import DeliveryOutcome
    from ductor_bot.cli.executor import build_subprocess_env
    from ductor_bot.cli.process_registry import ProcessRegistry
    from ductor_bot.cli.service import CLIService, CLIServiceConfig
    from ductor_bot.cli.types import AgentResponse
    from ductor_bot.config import ModelRegistry
    from ductor_bot.multiagent.bus import AsyncInterAgentResult
    from ductor_bot.orchestrator.injection import _inject_prompt
    from ductor_bot.workspace.paths import DuctorPaths

    orch = MagicMock()
    orch.paths = DuctorPaths(ductor_home=tmp_path)
    orch._config.append_system_prompt_files = []
    orch._config.cli_timeout = 60
    orch._sessions.get_active = AsyncMock(
        return_value=MagicMock(session_id="dm-session-DM_MARKER_7f3")
    )

    async def execute(request: object) -> AgentResponse:
        text = "DM_MARKER_7f3" if request.resume_session else "public report"
        return AgentResponse(result=text, session_id="broadcast-session")

    orch._cli_service.execute = AsyncMock(side_effect=execute)
    message_bus = MessageBus()
    injector = MagicMock()

    async def inject(*args: object, **kwargs: object) -> str:
        return await _inject_prompt(orch, *args, **kwargs)

    injector.inject_prompt = AsyncMock(side_effect=inject)
    message_bus.set_injector(injector)
    from ductor_bot.messenger.telegram.transport import TelegramTransport

    bot = MagicMock()
    bot._agent_name = "main"
    bot._config.allowed_user_ids = [999, 1000]
    bot._bus = message_bus
    sent = AsyncMock(return_value=(True, 1))
    monkeypatch.setattr(
        "ductor_bot.messenger.telegram.transport.send_rich_checked_progress", sent
    )
    message_bus.register_transport(TelegramTransport(bot))

    first_result = AsyncInterAgentResult(
        task_id="first", sender="main", recipient="target",
        message_preview="work", result_text="done", chat_id=0, transport="tg",
        result_origin="cron-broadcast", broadcast_ref="job-1",
    )
    envelope = from_interagent_result(
        first_result, chat_id=999, injection_prompt="report result"
    )
    first_outcome = await message_bus.submit(envelope)
    assert first_outcome.injected
    assert first_outcome.delivered
    assert [call.args[1] for call in sent.await_args_list] == [999, 1000]
    assert envelope.result_text == "public report"
    orch._sessions.get_active.assert_not_awaited()
    request = orch._cli_service.execute.await_args.args[0]
    assert request.resume_session is None

    service = CLIService(
        config=CLIServiceConfig(
            working_dir=str(tmp_path), ductor_home=str(tmp_path),
            default_model="opus", provider="claude", max_turns=None,
            max_budget_usd=None, permission_mode="bypassPermissions",
        ),
        models=ModelRegistry(), available_providers=frozenset({"claude"}),
        process_registry=ProcessRegistry(),
    )
    with patch("ductor_bot.cli.service.create_cli") as create:
        service._make_cli(request)
    cli_config = create.call_args.args[0]
    env = build_subprocess_env(cli_config)
    assert env["DUCTOR_RESULT_ORIGIN"] == "cron-broadcast"
    assert env["DUCTOR_BROADCAST_REF"] == "job-1"
    assert "DUCTOR_CHAT_ID" not in env
    body = _async_tool_body(
        monkeypatch,
        {key: value for key, value in env.items() if key.startswith("DUCTOR_")},
        "target",
    )
    assert body["result_origin"] == "cron-broadcast"
    assert body["broadcast_ref"] == "job-1"
    assert "chat_id" not in body

    received = asyncio.Event()
    saved: list[AsyncInterAgentResult] = []

    async def receive(result: AsyncInterAgentResult) -> DeliveryOutcome:
        from ductor_bot.messenger.telegram.app import TelegramBot

        saved.append(result)
        outcome = await TelegramBot.on_async_interagent_result(bot, result)
        received.set()
        return outcome

    target = MagicMock()
    target.bot.orchestrator.handle_interagent_message = AsyncMock(
        return_value=("second done", "ia-main", "")
    )
    bus.register("main", MagicMock())
    bus.register("target", target)
    bus.set_async_result_handler("main", receive)
    response = await client.post("/interagent/send_async", json=body)
    assert (await response.json())["success"] is True
    await asyncio.wait_for(received.wait(), timeout=1)
    assert (saved[0].result_origin, saved[0].broadcast_ref) == (
        "cron-broadcast", "job-1"
    )
    assert [call.args[1] for call in sent.await_args_list] == [999, 1000, 999, 1000]
    assert [call.args[2] for call in sent.await_args_list] == ["public report"] * 4
    orch._sessions.get_active.assert_not_awaited()


@pytest.mark.parametrize(("source", "targeted"), [("cron", False), ("webhook", False), ("cron", True)])
async def test_runner_to_async_delivery_end_to_end(
    source: str,
    targeted: bool,
    bus: InterAgentBus,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from types import SimpleNamespace

    from ductor_bot.bus.bus import MessageBus
    from ductor_bot.bus.envelope import DeliveryMode, Origin
    from ductor_bot.cli.codex_cache import CodexModelCache
    from ductor_bot.cli.param_resolver import TaskOverrides
    from ductor_bot.config import AgentConfig
    from ductor_bot.cron.execution import OneShotCommand
    from ductor_bot.cron.manager import CronJob, CronManager
    from ductor_bot.cron.observer import CronObserver
    from ductor_bot.messenger.telegram.app import TelegramBot
    from ductor_bot.multiagent.persistence import AsyncRecordStore
    from ductor_bot.webhook.manager import WebhookManager
    from ductor_bot.webhook.observer import WebhookObserver
    from ductor_bot.workspace.paths import DuctorPaths

    for key in tuple(os.environ):
        if key.startswith("DUCTOR_"):
            monkeypatch.delenv(key)
    paths = DuctorPaths(
        ductor_home=tmp_path / "home",
        home_defaults=tmp_path / "defaults",
        framework_root=tmp_path,
    )
    (paths.cron_tasks_dir / "job").mkdir(parents=True)
    config = AgentConfig(interagent_port=client.server.port)
    captured: dict[str, str] = {}
    tool_runs: list[subprocess.CompletedProcess[bytes]] = []

    async def spawn(*_args: object, **kwargs: object) -> AsyncMock:
        child_env = dict(kwargs["env"])
        captured.update(child_env)
        proc = AsyncMock()
        proc.returncode = 0

        async def communicate(**_kwargs: object) -> tuple[bytes, bytes]:
            completed = await asyncio.to_thread(
                subprocess.run,
                [sys.executable, str(_ASYNC_TOOL), "target", "next task"],
                env=child_env,
                capture_output=True,
                timeout=10,
                check=False,
            )
            tool_runs.append(completed)
            assert completed.returncode == 0, completed.stderr.decode()
            return b'{"result":"runner done"}', b""

        proc.communicate.side_effect = communicate
        return proc

    if source == "cron":
        manager = CronManager(jobs_path=paths.cron_jobs_path)
        manager.add_job(CronJob(
            id="job", title="Job", description="", schedule="0 0 * * *",
            task_folder="job", agent_instruction="work",
            chat_id=-100123 if targeted else 0,
            topic_id=8 if targeted else None,
        ))
        observer = CronObserver(
            paths, manager, config=config,
            codex_cache=CodexModelCache("", []), agent_name="main",
        )
        observer.set_result_handler(AsyncMock(return_value=(True, "")))
    else:
        observer = WebhookObserver(
            paths, WebhookManager(hooks_path=paths.webhooks_path),
            config=config, codex_cache=CodexModelCache("", []),
            agent_name="main",
        )

    (tmp_path / "records").mkdir()
    bus._record_store = AsyncRecordStore(tmp_path / "records")
    message_bus = MessageBus()
    injector = AsyncMock()
    injector.inject_prompt.return_value = "Public final report"
    message_bus.set_injector(injector)
    transports = []
    for name in ("tg", "mx", "sl"):
        transport = AsyncMock()
        transport.transport_name = name
        message_bus.register_transport(transport)
        transports.append(transport)
    bot = MagicMock()
    bot._agent_name = "main"
    bot._config.allowed_user_ids = [999]
    bot._bus = message_bus
    bus.register("main", SimpleNamespace(config=SimpleNamespace(transport="telegram"), bot=bot))
    target = MagicMock()
    target.bot.orchestrator.handle_interagent_message = AsyncMock(
        return_value=("subtask done", "ia-main", ""),
    )
    bus.register("target", target)
    stored: dict[str, object] = {}
    received = asyncio.Event()
    task_ids: list[str] = []
    running_tasks: list[asyncio.Task[object]] = []

    async def receive(result: object) -> object:
        task_ids.append(result.task_id)
        running_tasks.append(bus._async_tasks[result.task_id].asyncio_task)
        stored.update(bus._record_store.load("main", result.task_id))
        outcome = await TelegramBot.on_async_interagent_result(bot, result)
        received.set()
        return outcome

    bus.set_async_result_handler("main", receive)
    with (
        patch("ductor_bot.cron.execution.build_cmd", return_value=OneShotCommand(cmd=["cli"])),
        patch("asyncio.create_subprocess_exec", side_effect=spawn),
    ):
        if source == "cron":
            await observer._execute_job_inner("job", "work", "job")
        else:
            run = await observer._dispatch_cron_task(
                "hook", "Hook", "job", "work", TaskOverrides(),
            )
            assert run.status == "success"

    assert len(tool_runs) == 1
    assert captured["DUCTOR_AGENT_NAME"] == "main"
    assert captured["DUCTOR_INTERAGENT_PORT"] == str(client.server.port)
    await asyncio.wait_for(received.wait(), timeout=2)
    assert len(task_ids) == 1
    task_id = task_ids[0]
    await asyncio.wait_for(running_tasks[0], timeout=2)

    if targeted:
        assert "DUCTOR_RESULT_ORIGIN" not in captured
        assert "DUCTOR_BROADCAST_REF" not in captured
        assert captured["DUCTOR_CHAT_ID"] == "-100123"
        assert captured["DUCTOR_TOPIC_ID"] == "8"
        assert stored["chat_id"] == -100123
        assert stored["topic_id"] == 8
        assert stored["transport"] == "tg"
    else:
        assert captured["DUCTOR_RESULT_ORIGIN"] == "cron-broadcast"
        assert captured["DUCTOR_BROADCAST_REF"] == ("job" if source == "cron" else "hook")
        assert "DUCTOR_CHAT_ID" not in captured
        assert stored["result_origin"] == "cron-broadcast"
        assert stored["broadcast_ref"] == ("job" if source == "cron" else "hook")
        assert stored["result"]["result_origin"] == "cron-broadcast"
    assert not bus._record_store.path_for("main", task_id).exists()
    injector.inject_prompt.assert_awaited_once()
    bot._default_chat_id.assert_not_called()
    if targeted:
        assert injector.inject_prompt.await_args.args[1] == -100123
        assert injector.inject_prompt.await_args.kwargs["topic_id"] == 8
        tg = transports[0]
        tg.deliver.assert_awaited_once()
        tg.deliver_broadcast.assert_not_awaited()
        envelope = tg.deliver.await_args.args[0]
        assert envelope.origin is Origin.INTERAGENT
        assert envelope.delivery is DeliveryMode.UNICAST
        assert (envelope.chat_id, envelope.topic_id) == (-100123, 8)
        assert envelope.result_text == "Public final report"
        for transport in transports[1:]:
            transport.deliver.assert_not_awaited()
            transport.deliver_broadcast.assert_not_awaited()
    else:
        assert injector.inject_prompt.await_args.kwargs["isolated"] is True
        assert injector.inject_prompt.await_args.args[1] == 0
        for transport in transports:
            transport.deliver.assert_not_awaited()
            transport.deliver_broadcast.assert_awaited_once()
            envelope = transport.deliver_broadcast.await_args.args[0]
            assert envelope.origin is Origin.INTERAGENT
            assert envelope.delivery is DeliveryMode.BROADCAST
            assert envelope.chat_id == 0
            assert envelope.result_text == "Public final report"
            assert envelope.metadata["broadcast_ref"] == stored["broadcast_ref"]


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("silent", [False, True])
async def test_receipt_switch_with_async_tool_and_result_delivery(
    client: TestClient, bus: InterAgentBus, monkeypatch: pytest.MonkeyPatch,
    enabled: bool, silent: bool,
) -> None:
    from ductor_bot.bus.envelope import DeliveryOutcome
    from ductor_bot.multiagent.bus import AsyncInterAgentResult

    target = MagicMock()
    # Omit the new key for the false case to exercise its real default.
    target.config = AgentConfig(interagent_notify_received=True) if enabled else AgentConfig()
    target.bot.notification_service = AsyncMock()
    target.bot.orchestrator.handle_interagent_message = AsyncMock(
        return_value=("actual result", "ia-main", "")
    )
    sender = MagicMock()
    sender.config = AgentConfig(interagent_notify_received=not enabled)
    sender.bot.notification_service = AsyncMock()
    bus.register("worker", target)
    bus.register("main", sender)
    completed = asyncio.get_running_loop().create_future()

    def delivered(result: AsyncInterAgentResult) -> DeliveryOutcome:
        completed.set_result(result)
        return DeliveryOutcome(ok=True, injected=True, delivered=True)

    bus.set_async_result_handler("main", AsyncMock(side_effect=delivered))
    body = _async_tool_body(monkeypatch, {
        "DUCTOR_AGENT_NAME": "main", "DUCTOR_CHAT_ID": "777",
        "DUCTOR_TOPIC_ID": "10", "DUCTOR_TRANSPORT": "tg",
    }, "worker", silent=silent)
    assert bool(body.get("silent", False)) is silent
    response = await client.post("/interagent/send_async", json=body)
    assert response.status == 200
    assert (await response.json())["success"] is True
    result = await asyncio.wait_for(completed, timeout=2)
    assert result.success is True
    assert result.result_text == "actual result"
    target.bot.orchestrator.handle_interagent_message.assert_awaited_once()
    notification = target.bot.notification_service
    if enabled and not silent:
        notification.notify_origin.assert_awaited_once()
        assert notification.notify_origin.call_args.args[:2] == ("tg", 777)
        assert notification.notify_origin.call_args.kwargs == {"topic_id": 10}
    else:
        notification.notify_origin.assert_not_awaited()
    notification.notify.assert_not_awaited()
    notification.notify_all.assert_not_awaited()
    sender.bot.notification_service.notify_origin.assert_not_awaited()
    sender.bot.notification_service.notify.assert_not_awaited()
    sender.bot.notification_service.notify_all.assert_not_awaited()
