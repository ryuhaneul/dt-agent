"""Synchronous-question server guards and tool contracts, using synthetic agents only."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from ductor_bot.bus.adapters import build_interagent_injection_prompt
from ductor_bot.bus.envelope import DeliveryOutcome
from ductor_bot.cli.types import AgentResponse
from ductor_bot.multiagent.bus import (
    SYNC_DELEGATION_QUESTION_REJECTED,
    AsyncInterAgentResult,
    AsyncInterAgentTask,
    AsyncSendOptions,
)
from ductor_bot.multiagent.delegation import DelegationContext
from ductor_bot.multiagent.internal_api import InternalAgentAPI
from ductor_bot.multiagent.models import SubAgentConfig
from ductor_bot.multiagent.persistence import AsyncRecordStore
from ductor_bot.workspace.init import _build_identity_notice, _sync_home_defaults
from ductor_bot.workspace.paths import DuctorPaths
from tests.multiagent.test_delegation_context import agents as agents
from tests.orchestrator.test_interagent import _load_tool, _run_tool

ROOT = Path(__file__).resolve().parents[2]
ASYNC_REJECTED = (
    "Async sends are not allowed during a delegated task. If you need to ask, "
    "use the synchronous ask_agent or include the question in your result report."
)


@pytest.mark.parametrize("tool_name", ["ask_agent.py", "ask_agent_async.py"])
async def test_sync_delegation_actual_tool_exits_one_with_exact_server_error(
    agents,
    tool_name,
    caplog,  # noqa: F811
):
    bus, main, worker = agents
    api = InternalAgentAPI(bus, port=0)
    caplog.set_level("INFO", logger="ductor_bot.multiagent.bus")
    async with TestClient(TestServer(api._app)) as client:

        async def execute(request):
            environment = {k: v for k, v in os.environ.items() if not k.startswith("DUCTOR_")}
            environment.update(
                DUCTOR_AGENT_NAME="worker",
                DUCTOR_IA_TURN="1",
                DUCTOR_DELEGATION_ID=request.delegation_id,
                DUCTOR_INTERAGENT_HOST="127.0.0.1",
                DUCTOR_INTERAGENT_PORT=str(client.server.port),
            )
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(ROOT / "ductor_bot/_home_defaults/workspace/tools/agent_tools" / tool_name),
                "main",
                "synthetic forced question",
                env=environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), 3)
            assert process.returncode == 1
            assert stdout == b""
            receipt, error_line = stderr.decode().splitlines()
            assert receipt.startswith("[Received: ") and receipt.endswith(" UTC]")
            assert error_line == "Error: " + SYNC_DELEGATION_QUESTION_REJECTED
            assert "try again" not in stderr.decode() and "another route" not in stderr.decode()
            return AgentResponse(
                result="Checks finished. Missing value: unset. Question in final report."
            )

        worker._cli_service.execute.side_effect = execute
        result = await asyncio.wait_for(bus.send("main", "worker", "synthetic task"), 5)
    assert result.success and "Missing value: unset" in result.text
    main._cli_service.execute.assert_not_awaited()
    assert main._named_sessions.list_active(12345) == []
    assert not bus._async_tasks and not bus._delegations
    if tool_name == "ask_agent.py":
        logs = [r.getMessage() for r in caplog.records if "worker -> main" in r.getMessage()]
        assert len(logs) == 1 and "rejected" in logs[0]
        assert "synthetic forced question" not in logs[0]


@pytest.mark.parametrize("mode", ["sync", "async", "unknown"])
@pytest.mark.parametrize("matching_sender", [True, False])
async def test_async_rejection_mode_uses_server_registry(agents, mode, matching_sender):  # noqa: F811
    bus, main, _ = agents
    bus._delegations["verified-id"] = DelegationContext("verified-id", mode, "main", "worker")
    before = dict(bus._delegations)
    async with TestClient(TestServer(InternalAgentAPI(bus, port=0)._app)) as client:
        response = await client.post(
            "/interagent/send_async",
            json={
                "from": "worker" if matching_sender else "other",
                "to": "main",
                "message": "synthetic",
                "ia_turn": True,
                "delegation_id": "verified-id",
            },
        )
        body = await response.json()
    expected = (
        SYNC_DELEGATION_QUESTION_REJECTED if mode == "sync" and matching_sender else ASYNC_REJECTED
    )
    stamp = body.pop("received_at")
    receipt = body.pop("received_context")
    assert isinstance(stamp, (int, float)) and stamp > 0
    assert receipt == (
        "[Received: " + datetime.fromtimestamp(stamp, UTC).strftime("%Y-%m-%d %H:%M:%S %Z") + "]"
    )
    assert response.status == 200 and body == {"success": False, "error": expected}
    assert not bus._async_tasks and bus._delegations == before
    main._cli_service.execute.assert_not_awaited()
    assert bus.delegation_mode("", "worker") == "unknown"
    assert bus.delegation_mode("expired", "worker") == "unknown"


def test_async_tool_payload_includes_only_dispatch_id(monkeypatch):
    monkeypatch.setenv("DUCTOR_IA_TURN", "1")
    monkeypatch.setenv("DUCTOR_DELEGATION_ID", "server-dispatch")
    body = _run_tool(
        _load_tool("ask_agent_async.py"), ["ask_agent_async.py", "main", "synthetic"], monkeypatch
    )
    assert body["delegation_id"] == "server-dispatch" and body["ia_turn"] is True
    assert "mode" not in body and "request_preview" not in body


@pytest.mark.parametrize("transport", ["headless", "telegram"])
async def test_async_result_hint_uses_registered_recipient_transport(agents, transport, tmp_path):  # noqa: F811
    bus, _, worker = agents
    bus._agents["worker"].config = SubAgentConfig(name="worker", transport=transport)
    bus._record_store = AsyncRecordStore(tmp_path)
    delivered = []

    async def receive(result):
        assert result.recipient_headless == (transport == "headless")
        prompt = build_interagent_injection_prompt(
            result, agent_name="main", transport_label="Telegram chat"
        )
        assert f"The recipient processed this in session `{result.session_name}`." in prompt
        assert ("Telegram chat via" in prompt) == (transport == "telegram")
        assert ("The user can continue this session" in prompt) == (transport == "telegram")
        delivered.append(result)
        return DeliveryOutcome(ok=True, injected=True, delivered=True)

    bus.set_async_result_handler("main", receive)
    task_id = bus.send_async("main", "worker", "synthetic", opts=AsyncSendOptions(silent=True))
    await asyncio.wait_for(bus._async_tasks[task_id].asyncio_task, 1)
    assert len(delivered) == 1 and delivered[0].success
    assert not bus._delegations


def test_framework_tool_sync_updates_existing_agent_copy(tmp_path):
    defaults = ROOT / "ductor_bot/_home_defaults"
    paths = DuctorPaths(tmp_path / "home", home_defaults=defaults, framework_root=ROOT)
    tool = paths.workspace / "tools/agent_tools/ask_agent_async.py"
    tool.parent.mkdir(parents=True)
    tool.write_text("# synthetic old framework tool\n")
    _sync_home_defaults(paths)
    source = defaults / "workspace/tools/agent_tools/ask_agent_async.py"
    assert tool.read_bytes() == source.read_bytes()
    assert 'body["delegation_id"]' in tool.read_text()
    assert tool.with_suffix(".py.bak").read_text() == "# synthetic old framework tool\n"
    for transport in ("telegram", "headless"):
        identity = _build_identity_notice("worker", transport)
        assert "the server rejects those questions" in identity
        assert "do not invent missing values" in identity.replace("\n", " ")
    for relative in ("workspace/RULES.md", "workspace/tools/agent_tools/RULES.md"):
        assert "server rejects questions" in (defaults / relative).read_text().replace("\n", " ")


def test_legacy_record_without_headless_flag_remains_readable(tmp_path):
    store = AsyncRecordStore(tmp_path)
    task = AsyncInterAgentTask(
        task_id="0123456789ab", sender="main", recipient="worker", message="synthetic"
    )
    store.create(task)
    result = AsyncInterAgentResult(
        task_id=task.task_id,
        sender="main",
        recipient="worker",
        message_preview="synthetic",
        result_text="done",
    )
    store.update("main", task.task_id, result=result)
    path = store.path_for("main", task.task_id)
    record = json.loads(path.read_text())
    del record["result"]["recipient_headless"]
    path.write_text(json.dumps(record))
    assert store.result_from_record(store.load("main", task.task_id)).recipient_headless is False
