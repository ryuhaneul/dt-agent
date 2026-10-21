"""Lane identification and delivery plumbing: API, bus, records, results, controls."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from ductor_bot.bus.adapters import build_interagent_injection_prompt, from_interagent_result
from ductor_bot.bus.bus import MessageBus
from ductor_bot.bus.envelope import DeliveryOutcome
from ductor_bot.cli.process_registry import _PRESERVED_LABEL_PREFIXES, ProcessRegistry
from ductor_bot.cli.types import CLIResponse
from ductor_bot.config import AgentConfig
from ductor_bot.multiagent import bus as bus_module
from ductor_bot.multiagent.bus import (
    AsyncInterAgentResult,
    AsyncInterAgentTask,
    AsyncSendOptions,
    InterAgentBus,
)
from ductor_bot.multiagent.internal_api import InternalAgentAPI
from ductor_bot.multiagent.persistence import (
    _REQUIRED_FIELDS,
    _RESULT_REQUIRED_FIELDS,
    AsyncRecordStore,
)
from ductor_bot.orchestrator.core import Orchestrator
from ductor_bot.session.named import interagent_session_name
from ductor_bot.workspace.init import init_workspace
from ductor_bot.workspace.paths import DuctorPaths
from tests.orchestrator.conftest import setup_framework


def _name(lane: str = "", chat: int = 123, topic: int | None = 8, sender: str = "main") -> str:
    return interagent_session_name(sender, chat, topic, lane)


def _stack(result: tuple[str, str, str] = ("done", "ia-main", "")) -> SimpleNamespace:
    orch = SimpleNamespace(handle_interagent_message=AsyncMock(return_value=result))
    return SimpleNamespace(
        config=SimpleNamespace(interagent_notify_received=False, transport="telegram"),
        bot=SimpleNamespace(orchestrator=orch),
    )


def _ia(
    tmp_path: Path, stack: SimpleNamespace | None = None
) -> tuple[InterAgentBus, SimpleNamespace]:
    ia = InterAgentBus(tmp_path)
    stack = stack or _stack()
    ia.register(
        "main",
        SimpleNamespace(
            config=SimpleNamespace(transport="telegram"),
            bot=SimpleNamespace(orchestrator=SimpleNamespace()),
        ),
    )
    ia.register("worker", stack)
    delivered: list[AsyncInterAgentResult] = []

    async def deliver(result: AsyncInterAgentResult) -> DeliveryOutcome:
        delivered.append(result)
        return DeliveryOutcome(ok=True, injected=True, delivered=True)

    ia.set_async_result_handler("main", deliver)
    ia.delivered = delivered  # type: ignore[attr-defined]
    return ia, stack


async def _wait(items: list, timeout: float = 5.0) -> None:  # noqa: ASYNC109
    for _ in range(int(timeout / 0.02)):
        if items:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("no result delivered")


# -- send_async: lane reaches the recipient (both origin branches) and the record ------------


@pytest.mark.parametrize("origin", [True, False])
async def test_async_lane_reaches_the_recipient(tmp_path: Path, origin: bool) -> None:
    ia, stack = _ia(tmp_path)
    opts = AsyncSendOptions(
        lane="Build", chat_id=123 if origin else 0, topic_id=8 if origin else None
    )
    task_id = ia.send_async("main", "worker", "hi", opts=opts)
    assert task_id
    await _wait(ia.delivered)
    call = stack.bot.orchestrator.handle_interagent_message.await_args
    assert call.kwargs["lane"] == "build"  # normalized
    assert ("source_chat_id" in call.kwargs) is origin


async def test_unlaned_async_call_is_unchanged(tmp_path: Path) -> None:
    ia, stack = _ia(tmp_path)
    ia.send_async("main", "worker", "hi", opts=AsyncSendOptions(chat_id=123, topic_id=8))
    await _wait(ia.delivered)
    assert "lane" not in stack.bot.orchestrator.handle_interagent_message.await_args.kwargs


def test_invalid_lane_is_refused_before_any_record_exists(tmp_path: Path) -> None:
    ia, _ = _ia(tmp_path)
    with pytest.raises(ValueError, match="Invalid lane"):
        ia.send_async("main", "worker", "hi", opts=AsyncSendOptions(lane="a b"))
    assert not list(tmp_path.rglob("*.json"))


async def test_record_carries_optional_lane_keys_and_schema_is_unchanged(tmp_path: Path) -> None:
    _ia(tmp_path)
    store = AsyncRecordStore(tmp_path)
    task = AsyncInterAgentTask(
        task_id="abc000000001",
        sender="main",
        recipient="worker",
        message="m",
        chat_id=123,
        topic_id=8,
        transport="tg",
        lane="x1",
    )
    store.create(task)
    record = store.load("main", task.task_id)
    assert (record["lane"], record["session_name"]) == ("x1", _name("x1"))
    assert "lane" not in _REQUIRED_FIELDS
    assert "session_name" not in _REQUIRED_FIELDS
    assert _REQUIRED_FIELDS.issubset(record)  # an older product accepts the new record
    assert "lane" not in _RESULT_REQUIRED_FIELDS  # result schema untouched
    assert "session_name" in _RESULT_REQUIRED_FIELDS  # the existing field is the carrier
    await asyncio.sleep(0)


def test_old_records_without_the_keys_read_as_no_lane(tmp_path: Path) -> None:
    store = AsyncRecordStore(tmp_path)
    task = AsyncInterAgentTask(
        task_id="abc000000002",
        sender="main",
        recipient="worker",
        message="m",
        chat_id=123,
        topic_id=8,
        transport="tg",
        lane="x1",
    )
    store.create(task)
    path = store.path_for("main", task.task_id)
    raw = json.loads(path.read_text())
    del raw["lane"], raw["session_name"]
    path.write_text(json.dumps(raw))
    record = store.load("main", task.task_id)
    assert (record["lane"], record["session_name"]) == ("", "")


def test_malformed_lane_in_a_record_is_corrupt(tmp_path: Path) -> None:
    from ductor_bot.multiagent.persistence import CorruptAsyncRecordError

    store = AsyncRecordStore(tmp_path)
    task = AsyncInterAgentTask(
        task_id="abc000000003",
        sender="main",
        recipient="worker",
        message="m",
        chat_id=123,
        topic_id=8,
        transport="tg",
    )
    store.create(task)
    path = store.path_for("main", task.task_id)
    raw = json.loads(path.read_text())
    raw["lane"] = "BAD LANE"
    path.write_text(json.dumps(raw))
    with pytest.raises(CorruptAsyncRecordError):
        store.load("main", task.task_id)


# -- every result kind carries the real session name -----------------------------------------


async def test_success_result_uses_the_returned_name(tmp_path: Path) -> None:
    ia, _ = _ia(tmp_path, _stack(("ok", _name("x"), "")))
    ia.send_async("main", "worker", "hi", opts=AsyncSendOptions(lane="x", chat_id=123, topic_id=8))
    await _wait(ia.delivered)
    assert ia.delivered[0].session_name == _name("x")


async def test_failure_result_carries_the_session_name(tmp_path: Path) -> None:
    stack = _stack()
    stack.bot.orchestrator.handle_interagent_message = AsyncMock(side_effect=RuntimeError("boom"))
    ia, _ = _ia(tmp_path, stack)
    ia.send_async("main", "worker", "hi", opts=AsyncSendOptions(lane="x", chat_id=123, topic_id=8))
    await _wait(ia.delivered)
    result = ia.delivered[0]
    assert not result.success
    assert result.session_name == _name("x")


async def test_timeout_result_carries_the_session_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def slow(*_a: object, **_k: object) -> tuple[str, str, str]:
        await asyncio.sleep(5)
        return "", "", ""

    stack = _stack()
    stack.bot.orchestrator.handle_interagent_message = slow
    monkeypatch.setattr(bus_module, "_ASYNC_TIMEOUT", 0.05)
    ia, _ = _ia(tmp_path, stack)
    ia.send_async("main", "worker", "hi", opts=AsyncSendOptions(lane="x", chat_id=123, topic_id=8))
    await _wait(ia.delivered)
    assert "Timeout" in (ia.delivered[0].error or "")
    assert ia.delivered[0].session_name == _name("x")


async def test_unlaned_failure_result_gets_the_legacy_name(tmp_path: Path) -> None:
    ia, _ = _ia(
        tmp_path,
        SimpleNamespace(
            config=SimpleNamespace(interagent_notify_received=False, transport="telegram"),
            bot=SimpleNamespace(orchestrator=None),
        ),
    )
    ia.send_async("main", "worker", "hi")
    await _wait(ia.delivered)
    assert ia.delivered[0].session_name == "ia-main"


async def test_restart_interruption_keeps_session_and_lane(tmp_path: Path) -> None:
    store = AsyncRecordStore(tmp_path)
    store.create(
        AsyncInterAgentTask(
            task_id="abc000000004",
            sender="main",
            recipient="worker",
            message="Original request",
            chat_id=123,
            topic_id=8,
            transport="tg",
            lane="x1",
        )
    )
    ia, _ = _ia(tmp_path)
    ia._retry_delays = ()
    message_bus = MessageBus()
    injector = AsyncMock()
    injector.inject_prompt = AsyncMock(return_value="report")
    message_bus.set_injector(injector)
    transport = AsyncMock()
    transport.transport_name = "tg"
    message_bus.register_transport(transport)
    ia._agents["main"].bot._bus = message_bus
    seen: list[AsyncInterAgentResult] = []

    async def deliver(result: AsyncInterAgentResult):
        seen.append(result)
        prompt = build_interagent_injection_prompt(
            result, agent_name="main", transport_label="Telegram chat"
        )
        return await message_bus.submit(from_interagent_result(result, 0, injection_prompt=prompt))

    ia.set_async_result_handler("main", deliver)
    assert await ia.recover_inflight("main") == 1
    assert seen[0].error == "interrupted by restart"
    assert seen[0].session_name == _name("x1")
    prompt = injector.inject_prompt.await_args.args[0]
    assert _name("x1") in prompt
    assert "x1" in prompt


# -- display ---------------------------------------------------------------------------------


def _result(**kw: object) -> AsyncInterAgentResult:
    base: dict[str, object] = {
        "task_id": "t1",
        "sender": "main",
        "recipient": "worker",
        "message_preview": "m",
        "result_text": "done",
        "original_message": "do it",
    }
    base.update(kw)
    return AsyncInterAgentResult(**base)  # type: ignore[arg-type]


def test_failure_header_and_result_header_show_session_and_lane() -> None:
    name = _name("x1")
    fail = build_interagent_injection_prompt(
        _result(success=False, error="boom", session_name=name),
        agent_name="main",
        transport_label="chat",
    )
    assert f"`{name}`" in fail
    assert "x1" in fail
    lost = build_interagent_injection_prompt(
        _result(success=False, error="x", failure_kind="result_lost", session_name=name),
        agent_name="main",
        transport_label="chat",
    )
    assert f"`{name}`" in lost
    ok = build_interagent_injection_prompt(
        _result(session_name=name), agent_name="main", transport_label="chat"
    )
    assert f"`{name}`" in ok
    assert "Lane: `x1`" in ok


def test_unlaned_headers_have_no_lane_text() -> None:
    ok = build_interagent_injection_prompt(
        _result(session_name="ia-main"), agent_name="main", transport_label="chat"
    )
    assert "Lane" not in ok
    plain_fail = build_interagent_injection_prompt(
        _result(success=False, error="boom"), agent_name="main", transport_label="chat"
    )
    assert "Session:" not in plain_fail  # a result without a name keeps the old text


# -- internal API ----------------------------------------------------------------------------


@pytest.fixture
async def api_client(tmp_path: Path):
    ia, stack = _ia(tmp_path)
    api = InternalAgentAPI(ia, port=0)
    client = TestClient(TestServer(api._app))
    await client.start_server()
    yield client, ia, stack
    await client.close()


async def test_api_send_passes_a_normalized_lane(api_client) -> None:
    client, _ia_bus, stack = api_client
    resp = await client.post(
        "/interagent/send",
        json={
            "from": "main",
            "to": "worker",
            "message": "hi",
            "lane": "Build",
            "chat_id": 123,
        },
    )
    assert (await resp.json())["success"] is True
    assert stack.bot.orchestrator.handle_interagent_message.await_args.kwargs["lane"] == "build"


async def test_api_send_async_passes_the_lane_to_the_recipient(api_client) -> None:
    client, ia, stack = api_client
    resp = await client.post(
        "/interagent/send_async",
        json={
            "from": "main",
            "to": "worker",
            "message": "hi",
            "lane": "x-1",
            "chat_id": 123,
        },
    )
    assert (await resp.json())["success"] is True
    await _wait(ia.delivered)
    assert stack.bot.orchestrator.handle_interagent_message.await_args.kwargs["lane"] == "x-1"


@pytest.mark.parametrize("path", ["/interagent/send", "/interagent/send_async"])
@pytest.mark.parametrize("lane", ["a b", "-x", "a" * 13, 5, ["a"]])
async def test_api_rejects_a_bad_lane_before_doing_anything(api_client, path, lane) -> None:
    client, ia, stack = api_client
    resp = await client.post(
        path, json={"from": "main", "to": "worker", "message": "hi", "lane": lane}
    )
    body = await resp.json()
    assert resp.status == 400
    assert body["success"] is False
    assert "lane" in body["error"].lower()
    stack.bot.orchestrator.handle_interagent_message.assert_not_awaited()
    assert not ia.delivered


@pytest.mark.parametrize("path", ["/interagent/send", "/interagent/send_async"])
async def test_api_without_lane_is_unchanged(api_client, path) -> None:
    client, ia, stack = api_client
    resp = await client.post(path, json={"from": "main", "to": "worker", "message": "hi"})
    assert (await resp.json())["success"] is True
    if path.endswith("send_async"):
        await _wait(ia.delivered)
    assert "lane" not in stack.bot.orchestrator.handle_interagent_message.await_args.kwargs


async def test_direct_bus_send_validates_and_passes_the_lane(tmp_path: Path) -> None:
    ia, stack = _ia(tmp_path)
    ok = await ia.send("main", "worker", "hi", lane="Z9")
    assert ok.success
    assert stack.bot.orchestrator.handle_interagent_message.await_args.kwargs["lane"] == "z9"
    bad = await ia.send("main", "worker", "hi", lane="bad lane")
    assert not bad.success
    assert "lane" in (bad.error or "").lower()
    assert stack.bot.orchestrator.handle_interagent_message.await_count == 1


# -- orchestrator: session by lane, label, stale retry, end ---------------------------------


@pytest.fixture
def orch_ia(tmp_path: Path) -> Orchestrator:
    fw_root = tmp_path / "fw"
    setup_framework(fw_root)
    paths = DuctorPaths(
        ductor_home=tmp_path / "home", home_defaults=fw_root / "workspace", framework_root=fw_root
    )
    init_workspace(paths)
    config = AgentConfig()
    config.allowed_user_ids = [12345]
    o = Orchestrator(config, paths, agent_name="codex")
    cli = MagicMock()
    cli._config = MagicMock()
    cli._config.agent_name = "codex"
    cli._config.cli_timeout = 120
    cli.execute = AsyncMock(return_value=CLIResponse(session_id="sess-1", result="done"))
    object.__setattr__(o, "_cli_service", cli)
    return o


async def test_lanes_are_separate_sessions_and_unlaned_name_is_unchanged(
    orch_ia: Orchestrator,
) -> None:
    for lane in ("", "a", "b"):
        await orch_ia.handle_interagent_message(
            "main", "t", source_chat_id=777, source_topic_id=10, **({"lane": lane} if lane else {})
        )
    names = [c.args[0].session_name for c in orch_ia._cli_service.execute.call_args_list]
    assert names == [_name("", 777, 10), _name("a", 777, 10), _name("b", 777, 10)]
    assert names[0] == interagent_session_name("main", 777, 10)  # byte-identical
    assert all(orch_ia._named_sessions.get(12345, n) for n in names)
    labels = [c.args[0].process_label for c in orch_ia._cli_service.execute.call_args_list]
    assert labels == [f"interagent:{n}" for n in names]


async def test_return_value_reports_the_lane_session(orch_ia: Orchestrator) -> None:
    _text, name, _notice = await orch_ia.handle_interagent_message("main", "t", lane="x")
    assert name == _name("x", 0, None)  # no source: chat 0 / topic 0


async def test_stale_session_retry_and_new_session_keep_the_lane(orch_ia: Orchestrator) -> None:
    await orch_ia.handle_interagent_message(
        "main", "1", source_chat_id=7, source_topic_id=1, lane="x"
    )
    orch_ia._cli_service.execute = AsyncMock(
        side_effect=[
            CLIResponse(result="No conversation found with session ID: sess-1", is_error=True),
            CLIResponse(session_id="sess-2", result="done"),
        ]
    )
    _t, name, notice = await orch_ia.handle_interagent_message(
        "main", "2", source_chat_id=7, source_topic_id=1, lane="x"
    )
    assert name == _name("x", 7, 1)
    assert "stale" in notice
    first, retry = (c.args[0] for c in orch_ia._cli_service.execute.call_args_list)
    assert first.session_name == retry.session_name == name
    assert retry.resume_session is None
    assert retry.process_label == f"interagent:{name}"
    other = await orch_ia.handle_interagent_message(
        "main", "3", source_chat_id=7, source_topic_id=1, lane="x", new_session=True
    )
    assert other[1] == name
    assert orch_ia._named_sessions.get(12345, _name("", 7, 1)) is None  # no unlaned session born


async def test_ending_a_session_kills_only_its_own_process_labels(orch_ia: Orchestrator) -> None:
    await orch_ia.handle_interagent_message(
        "main", "1", source_chat_id=7, source_topic_id=1, lane="a"
    )
    await orch_ia.handle_interagent_message(
        "main", "1", source_chat_id=7, source_topic_id=1, lane="b"
    )
    registry = orch_ia._process_registry
    killed: list[str] = []
    cleared: list[str] = []

    async def kill(_chat_id: int, label: str) -> int:
        killed.append(label)
        return 0

    registry.kill_by_label = kill  # type: ignore[method-assign]
    registry.clear_label_abort = lambda _c, label: cleared.append(label)  # type: ignore[method-assign]
    a = _name("a", 7, 1)
    assert await orch_ia.end_named_session(12345, a) is True
    assert killed == [f"ns:{a}", f"interagent:{a}"]
    assert cleared == killed
    assert orch_ia._named_sessions.get(12345, _name("b", 7, 1)).status != "ended"


# -- process registry: /stop is unchanged, interagent labels are killable by name -----------


class _Proc:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.pid = 0

    def kill(self) -> None:
        self.returncode = -9

    def terminate(self) -> None:
        self.returncode = -15

    async def wait(self) -> int:
        return self.returncode or 0


def test_stop_preserved_prefixes_are_unchanged() -> None:
    assert _PRESERVED_LABEL_PREFIXES == ("task:", "task_result:", "ns:")


async def test_label_kill_targets_the_exact_interagent_label_and_clears_abort(monkeypatch) -> None:
    registry = ProcessRegistry()
    killed: list[str] = []

    async def fake_kill(entries: list) -> int:
        killed.extend(e.label for e in entries)
        return len(entries)

    monkeypatch.setattr("ductor_bot.cli.process_registry._kill_processes", fake_kill)
    a, b = _name("a", 7, 1), _name("b", 7, 1)
    for label in (f"interagent:{a}", f"interagent:{b}", f"ns:{a}"):
        registry.register(12345, _Proc(), label)  # type: ignore[arg-type]
    await registry.kill_by_label(12345, f"interagent:{a}")
    assert killed == [f"interagent:{a}"]
    registry.clear_label_abort(12345, f"interagent:{a}")
    assert (12345, f"interagent:{a}") not in registry._aborted_labels


async def test_topic_stop_still_kills_interagent_processes(monkeypatch) -> None:
    registry = ProcessRegistry()
    killed: list[str] = []

    async def fake_kill(entries: list) -> int:
        killed.extend(e.label for e in entries)
        return len(entries)

    monkeypatch.setattr("ductor_bot.cli.process_registry._kill_processes", fake_kill)
    registry.register(1, _Proc(), f"interagent:{_name('a')}", topic_id=None)  # type: ignore[arg-type]
    registry.register(1, _Proc(), f"ns:{_name('a')}", topic_id=None)  # type: ignore[arg-type]
    await registry.kill_by_chat_topic(1, None)
    assert killed == [f"interagent:{_name('a')}"]  # ns: stays preserved, interagent: dies
