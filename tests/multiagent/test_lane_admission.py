"""Admission through the bus: parallel lanes, FIFO, limits, cancellation and the time contract."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest  # noqa: TC002 -- annotations only
from aiohttp.test_utils import TestClient, TestServer

import ductor_bot.multiagent.bus as bus_module
from ductor_bot.cli.types import AgentResponse
from ductor_bot.config import AgentConfig
from ductor_bot.multiagent.bus import AsyncSendOptions, InterAgentBus
from ductor_bot.multiagent.delegation import DelegationContext
from ductor_bot.multiagent.internal_api import InternalAgentAPI
from ductor_bot.orchestrator import injection
from ductor_bot.orchestrator.core import Orchestrator
from ductor_bot.session.named import interagent_session_name
from ductor_bot.workspace.paths import DuctorPaths


class Recorder:
    """Synthetic CLI: records start/end per request and blocks until released."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.running = 0
        self.max_running = 0
        self.per_session: dict[str, int] = {}
        self.max_per_session: dict[str, int] = {}
        self.gates: dict[str, asyncio.Event] = {}
        self.delay = 0.0
        self.calls = 0

    def gate(self, text: str) -> asyncio.Event:
        return self.gates.setdefault(text, asyncio.Event())

    async def execute(self, request: object) -> AgentResponse:
        text = request.prompt.split("[INTER-AGENT MESSAGE", 1)[1].split("\n")[2]  # type: ignore[attr-defined]
        session = request.session_name  # type: ignore[attr-defined]
        self.calls += 1
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        self.per_session[session] = self.per_session.get(session, 0) + 1
        self.max_per_session[session] = max(
            self.max_per_session.get(session, 0), self.per_session[session]
        )
        self.events.append(f"start:{text}")
        try:
            if text in self.gates:
                await self.gates[text].wait()
            elif self.delay:
                await asyncio.sleep(self.delay)
        finally:
            self.running -= 1
            self.per_session[session] -= 1
            self.events.append(f"end:{text}")
        return AgentResponse(result=f"done {text}", session_id=f"sid-{text}")


def _make(tmp_path: Path, *, max_parallel: int = 4, queue_wait: float = 3600.0):
    bus = InterAgentBus(max_parallel_sessions=max_parallel, max_queue_wait_seconds=queue_wait)
    rec = Recorder()
    orchs = {}
    for name in ("main", "worker"):
        home = tmp_path / name
        home.mkdir()
        config = AgentConfig(
            allowed_user_ids=[12345],
            model="sonnet",
            interagent={
                "max_parallel_sessions": max_parallel,
                "max_queue_wait_seconds": queue_wait,
            },
        )
        orch = Orchestrator(config, DuctorPaths(home), agent_name=name)
        orch._cli_service.execute = rec.execute
        stack = MagicMock()
        stack.bot.orchestrator = orch
        stack.config = config
        stack.bot.notification_service.notify = AsyncMock()
        stack.bot.notification_service.notify_all = AsyncMock()
        bus.register(name, stack)
        orchs[name] = orch
    results: list = []

    async def handler(result):
        results.append(result)
        from ductor_bot.bus.envelope import DeliveryOutcome

        return DeliveryOutcome(ok=True, injected=True, delivered=True)

    bus.set_async_result_handler("main", handler)
    return bus, rec, orchs, results


def _send(bus: InterAgentBus, text: str, lane: str = "", chat: int = 0, topic: int | None = None):
    return bus.send_async(
        "main",
        "worker",
        f"\n{text}\n",
        opts=AsyncSendOptions(lane=lane, chat_id=chat, topic_id=topic, silent=True),
    )


async def _settle(times: int = 20) -> None:
    for _ in range(times):
        await asyncio.sleep(0)


async def _until(predicate, timeout: float = 5.0, *, strict: bool = True) -> None:  # noqa: ASYNC109
    """Poll until *predicate* holds (a real condition, not a number of event-loop yields)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            assert not strict, "timed out waiting for the condition"
            return
        await asyncio.sleep(0.01)


def _clean(bus: InterAgentBus) -> bool:
    return bus._gates.stats("worker") == (0, 0, 0) and not bus._gates._queues


async def _all_done(bus: InterAgentBus) -> None:
    tasks = [t.asyncio_task for t in bus._async_tasks.values() if t.asyncio_task]
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)
    await _until(lambda: _clean(bus), timeout=2, strict=False)


# -- parallel lanes, FIFO and limits (blocking conditions) ---------------------------------


async def test_two_lanes_run_in_parallel(tmp_path: Path) -> None:
    bus, rec, _, results = _make(tmp_path)
    for text in ("a", "b"):
        rec.gate(text)
    _send(bus, "a", lane="one")
    _send(bus, "b", lane="two")
    await _until(lambda: len(rec.events) >= 2)
    assert sorted(rec.events) == ["start:a", "start:b"]  # both started before either finished
    rec.gate("a").set()
    rec.gate("b").set()
    await _all_done(bus)
    assert sorted(r.result_text for r in results) == ["done a", "done b"]
    assert _clean(bus)


async def test_same_lane_runs_in_acceptance_order_without_overlap(tmp_path: Path) -> None:
    bus, rec, _, results = _make(tmp_path)
    for text in ("1", "2", "3"):
        rec.gate(text)
    ids = [_send(bus, text, lane="x") for text in ("1", "2", "3")]  # one tick, three requests
    await _until(lambda: rec.events == ["start:1"])
    await _settle()  # nothing else starts
    assert rec.events == ["start:1"]
    assert [bus.admission_info(i) for i in ids] == [(False, 0), (True, 1), (True, 2)]
    for text in ("1", "2", "3"):
        rec.gate(text).set()
        await _until(lambda text=text: f"end:{text}" in rec.events)
    await _all_done(bus)
    assert rec.events == ["start:1", "end:1", "start:2", "end:2", "start:3", "end:3"]
    assert max(rec.max_per_session.values()) == 1
    assert [r.result_text for r in results] == ["done 1", "done 2", "done 3"]
    assert _clean(bus)


async def test_unlaned_overlapping_requests_of_one_sender_are_serialized(tmp_path: Path) -> None:
    bus, rec, _, _ = _make(tmp_path)
    for text in ("1", "2"):
        rec.gate(text)
    _send(bus, "1", chat=77, topic=3)
    _send(bus, "2", chat=77, topic=3)
    await _until(lambda: rec.events == ["start:1"])
    await _settle()  # nothing else starts
    assert rec.events == ["start:1"]
    rec.gate("1").set()
    rec.gate("2").set()
    await _all_done(bus)
    assert max(rec.max_per_session.values()) == 1


async def test_other_topics_still_run_in_parallel(tmp_path: Path) -> None:
    bus, rec, _, _ = _make(tmp_path)
    for text in ("1", "2"):
        rec.gate(text)
    _send(bus, "1", chat=77, topic=3)
    _send(bus, "2", chat=77, topic=4)
    await _until(lambda: len(rec.events) >= 2)  # both CLIs started (not a fixed number of yields)
    assert sorted(rec.events) == ["start:1", "start:2"]
    rec.gate("1").set()
    rec.gate("2").set()
    await _all_done(bus)


async def test_slow_receipt_notice_does_not_reorder_a_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bus, rec, _, results = _make(tmp_path)
    original = bus._notify_recipient

    async def notify(task):
        await asyncio.sleep(0.12 if task.message.strip() == "1" else 0)
        await original(task)

    monkeypatch.setattr(bus, "_notify_recipient", notify)
    _send(bus, "1", lane="x")  # accepted first, its preparation finishes last
    _send(bus, "2", lane="x")
    await _all_done(bus)
    assert [r.result_text for r in results] == ["done 1", "done 2"]
    assert rec.events == ["start:1", "end:1", "start:2", "end:2"]


async def test_never_more_sessions_than_the_limit_run(tmp_path: Path) -> None:
    bus, rec, _, results = _make(tmp_path, max_parallel=2)
    rec.delay = 0.03
    for i in range(6):
        _send(bus, str(i), lane=f"l{i}")
    await _all_done(bus)
    assert rec.max_running == 2  # parallel, and never above the limit
    assert len(results) == 6
    assert _clean(bus)


async def test_acceptance_does_not_wait_for_a_free_slot(tmp_path: Path) -> None:
    bus, rec, _, _ = _make(tmp_path, max_parallel=1)
    rec.gate("busy")
    _send(bus, "busy", lane="a")
    await _until(lambda: "start:busy" in rec.events)
    api = InternalAgentAPI(bus, port=0)
    client = TestClient(TestServer(api._app))
    await client.start_server()
    try:
        started = time.monotonic()
        resp = await asyncio.wait_for(
            client.post(
                "/interagent/send_async",
                json={
                    "from": "main",
                    "to": "worker",
                    "message": "\nlate\n",
                    "lane": "b",
                    "silent": True,
                },
            ),
            2,
        )
        body = await resp.json()
        assert time.monotonic() - started < 1
        assert body["success"] is True
        assert (body["queued"], body["ahead"]) == (True, 1)  # waits for a slot, answered at once
        rec.gate("busy").set()
        await _all_done(bus)
    finally:
        await client.close()


async def test_an_unqueued_acceptance_reports_not_queued(tmp_path: Path) -> None:
    bus, _rec, _, _ = _make(tmp_path)
    task_id = _send(bus, "x", lane="solo")
    assert bus.admission_info(task_id) == (False, 0)
    assert bus.admission_info("unknown") == (False, 0)
    await _all_done(bus)


async def test_waiting_requests_hold_no_slot(tmp_path: Path) -> None:
    bus, rec, _, _ = _make(tmp_path, max_parallel=2)
    for text in ("1", "2", "3"):
        rec.gate(text)
    _send(bus, "1", lane="x")
    _send(bus, "2", lane="x")  # waits for the session turn
    _send(bus, "3", lane="y")
    await _until(lambda: len(rec.events) >= 2)
    await _settle()  # the third does not start
    assert sorted(rec.events) == ["start:1", "start:3"]
    assert bus._gates.stats("worker")[0] == 2  # exactly the two running ones
    for text in ("1", "2", "3"):
        rec.gate(text).set()
    await _all_done(bus)


# -- cancellation at every stage -------------------------------------------------------------


async def test_cancel_while_waiting_for_the_session_turn(tmp_path: Path) -> None:
    bus, rec, _, _results = _make(tmp_path)
    for text in ("1", "2", "3"):
        rec.gate(text)
    ids = [_send(bus, text, lane="x") for text in ("1", "2", "3")]
    await _until(lambda: rec.events == ["start:1"])
    bus._async_tasks[ids[1]].asyncio_task.cancel()
    await _until(
        lambda: (
            bus._gates.pending("worker", 12345, interagent_session_name("main", 0, None, "x")) == 2
        )
    )
    assert bus._gates.pending("worker", 12345, interagent_session_name("main", 0, None, "x")) == 2
    for text in ("1", "2", "3"):
        rec.gate(text).set()
    await _all_done(bus)
    assert [e for e in rec.events if e.startswith("start")] == ["start:1", "start:3"]
    assert _clean(bus)


async def test_cancel_while_waiting_for_a_slot(tmp_path: Path) -> None:
    bus, rec, _, _ = _make(tmp_path, max_parallel=1)
    rec.gate("1")
    first = _send(bus, "1", lane="a")
    second = _send(bus, "2", lane="b")
    await _until(lambda: bus._gates.stats("worker")[:2] == (1, 1))
    assert bus._gates.stats("worker")[:2] == (1, 1)
    bus._async_tasks[second].asyncio_task.cancel()
    await _until(lambda: bus._gates.stats("worker")[:2] == (1, 0))
    assert bus._gates.stats("worker")[:2] == (1, 0)
    rec.gate("1").set()
    await _all_done(bus)
    assert first
    assert _clean(bus)


async def test_cancel_while_the_cli_runs(tmp_path: Path) -> None:
    bus, rec, _, _ = _make(tmp_path, max_parallel=1)
    rec.gate("1")
    first = _send(bus, "1", lane="a")
    follower = _send(bus, "2", lane="a")
    await _until(lambda: "start:1" in rec.events)
    bus._async_tasks[first].asyncio_task.cancel()
    await _all_done(bus)
    assert "start:2" in rec.events  # the follower got the session and the slot
    assert follower
    assert _clean(bus)


async def test_cancel_during_the_stale_session_retry(tmp_path: Path) -> None:
    bus, rec, orchs, _ = _make(tmp_path)
    rec.gate("1")
    first = _send(bus, "1", lane="x")
    await _until(lambda: "start:1" in rec.events)
    rec.gate("1").set()
    await _all_done(bus)  # the session now has a session id
    calls = {"n": 0}
    blocker = asyncio.Event()

    async def execute(_request):
        calls["n"] += 1
        if calls["n"] == 1:
            return AgentResponse(result="No conversation found with session ID: x", is_error=True)
        await blocker.wait()  # the retry hangs
        return AgentResponse(result="never")

    orchs["worker"]._cli_service.execute = execute
    retry_id = _send(bus, "retry", lane="x")
    follower = _send(bus, "after", lane="x")
    await _until(lambda: calls["n"] == 2)
    assert calls["n"] == 2  # stale error, then the fresh retry is running
    bus._async_tasks[retry_id].asyncio_task.cancel()
    await _until(lambda: calls["n"] == 3)
    assert calls["n"] == 3  # the follower now runs (its stale/fresh turn)
    blocker.set()
    await _all_done(bus)
    assert first
    assert follower
    assert _clean(bus)


async def test_a_task_cancelled_before_its_first_step_still_frees_its_place(
    tmp_path: Path,
) -> None:
    bus, _rec, _, _ = _make(tmp_path)
    task_id = _send(bus, "x", lane="solo")
    bus._async_tasks[task_id].asyncio_task.cancel()  # cancelled before the coroutine ever ran
    await _until(lambda: _clean(bus))
    assert _clean(bus)


# -- time contract -----------------------------------------------------------------------------


async def test_execution_limit_starts_at_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bus_module, "_ASYNC_TIMEOUT", 0.25)
    bus, rec, _, results = _make(tmp_path)
    rec.delay = 0.15
    _send(bus, "1", lane="x")
    _send(bus, "2", lane="x")  # waits 0.15 s, then runs 0.15 s: 0.3 s in total > 0.25 s
    await _all_done(bus)
    assert [r.success for r in results] == [True, True]


async def test_queue_wait_limit_gives_a_distinct_result_with_the_session_name(
    tmp_path: Path,
) -> None:
    bus, rec, _, results = _make(tmp_path, queue_wait=0.08)
    rec.gate("long")
    _send(bus, "long", lane="x")
    _send(bus, "second", lane="x")
    await asyncio.sleep(0.2)
    rec.gate("long").set()
    await _all_done(bus)
    failed = [r for r in results if not r.success]
    assert len(failed) == 1
    assert bus_module.QUEUE_WAIT_TIMEOUT in failed[0].error
    assert "Timeout after" not in failed[0].error
    assert failed[0].session_name == interagent_session_name("main", 0, None, "x")
    assert "start:second" not in rec.events  # never started
    assert _clean(bus)


async def test_slow_preparation_counts_toward_the_wait_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bus, rec, _, results = _make(tmp_path, queue_wait=0.05)

    async def slow_notice(_task):
        await asyncio.sleep(1)

    monkeypatch.setattr(bus, "_notify_recipient", slow_notice)
    _send(bus, "1", lane="x")
    await _all_done(bus)
    assert bus_module.QUEUE_WAIT_TIMEOUT in results[0].error
    assert rec.calls == 0
    assert _clean(bus)


async def test_execution_timeout_stays_distinct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bus_module, "_ASYNC_TIMEOUT", 0.05)
    bus, rec, _, results = _make(tmp_path)
    rec.delay = 0.5
    _send(bus, "1", lane="x")
    await _all_done(bus)
    assert "Timeout after" in results[0].error
    assert bus_module.QUEUE_WAIT_TIMEOUT not in results[0].error
    assert results[0].session_name
    assert _clean(bus)


async def test_waited_includes_the_queue_wait_only(tmp_path: Path) -> None:
    import re

    bus, rec, orchs, _ = _make(tmp_path)
    prompts: list[str | None] = []
    original = rec.execute

    async def execute(request):
        prompts.append(request.received_context)
        return await original(request)

    orchs["worker"]._cli_service.execute = execute
    rec.gate("1")
    _send(bus, "1", lane="x")
    _send(bus, "2", lane="x")
    await _until(lambda: "start:1" in rec.events)
    await _until(
        lambda: (
            bus._gates.pending("worker", 12345, interagent_session_name("main", 0, None, "x")) == 2
        )
    )
    await asyncio.sleep(1.2)
    rec.gate("1").set()
    await _all_done(bus)
    assert "Waited" not in (prompts[0] or "")
    assert re.search(r"\[Waited: ([1-9]\d*)s\]", prompts[1] or "")
    assert (prompts[1] or "").startswith("[Received:")  # receipt time itself is unchanged


# -- synchronous sends -------------------------------------------------------------------------


async def test_sync_send_waits_in_the_same_queue_within_its_budget(tmp_path: Path) -> None:
    bus, rec, _, _ = _make(tmp_path)
    rec.gate("busy")
    _send(bus, "busy", lane="x")
    await _until(lambda: "start:busy" in rec.events)
    waiting = asyncio.create_task(bus.send("main", "worker", "\nsync\n", lane="x"))
    await _until(
        lambda: (
            bus._gates.pending("worker", 12345, interagent_session_name("main", 0, None, "x")) == 2
        )
    )
    assert "start:sync" not in rec.events
    rec.gate("busy").set()
    reply = await asyncio.wait_for(waiting, 2)
    assert reply.success
    assert rec.events.index("end:busy") < rec.events.index("start:sync")
    await _all_done(bus)
    assert _clean(bus)


async def test_sync_wait_and_execution_share_one_budget(tmp_path: Path) -> None:
    bus, rec, _, _ = _make(tmp_path)
    rec.gate("busy")
    _send(bus, "busy", lane="x")
    await _until(lambda: "start:busy" in rec.events)
    waiting = asyncio.create_task(
        bus.send("main", "worker", "\nsync\n", lane="x", send_timeout=0.2)
    )
    await asyncio.sleep(0.12)
    rec.gate("busy").set()
    rec.delay = 0.15  # 0.12 s of waiting + 0.15 s of running > 0.2 s
    reply = await asyncio.wait_for(waiting, 2)
    assert not reply.success
    assert "Timeout after" in (reply.error or "")
    await _all_done(bus)
    assert _clean(bus)


async def test_sync_send_that_never_gets_in_gives_up_and_frees_its_place(tmp_path: Path) -> None:
    bus, rec, _, _ = _make(tmp_path)
    rec.gate("busy")
    _send(bus, "busy", lane="x")
    await _until(lambda: "start:busy" in rec.events)
    reply = await bus.send("main", "worker", "\nsync\n", lane="x", send_timeout=0.1)
    assert not reply.success
    assert "start:sync" not in rec.events
    assert bus._gates.pending("worker", 12345, interagent_session_name("main", 0, None, "x")) == 1
    rec.gate("busy").set()
    await _all_done(bus)
    assert _clean(bus)


async def test_sync_reply_reports_that_it_queued(tmp_path: Path) -> None:
    bus, rec, _, _ = _make(tmp_path)
    rec.gate("busy")
    _send(bus, "busy", lane="x")
    await _until(lambda: "start:busy" in rec.events)
    waiting = asyncio.create_task(bus.send("main", "worker", "\nsync\n", lane="x"))
    await _until(
        lambda: (
            bus._gates.pending("worker", 12345, interagent_session_name("main", 0, None, "x")) == 2
        )
    )
    rec.gate("busy").set()
    reply = await asyncio.wait_for(waiting, 2)
    assert (reply.queued, reply.ahead) == (True, 1)
    solo = await bus.send("main", "worker", "\nsolo\n", lane="y")
    assert (solo.queued, solo.ahead) == (False, 0)
    await _all_done(bus)


# -- questions: reserve only for the Named-session fallback -----------------------------------


def _question(sender: str = "worker") -> DelegationContext:
    return DelegationContext(
        request_id="r1", mode="async", sender="main", recipient=sender, chat_id=-777, topic_id=5
    )


async def test_question_answered_in_the_original_session_reserves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bus, rec, orchs, _ = _make(tmp_path)
    monkeypatch.setattr(
        injection, "_answer_async_delegation_question", AsyncMock(return_value="ans")
    )
    entered: list[tuple[int, str]] = []
    out = await orchs["main"].handle_interagent_message(
        "worker",
        "q?",
        question_context=_question(),
        source_chat_id=-777,
        source_topic_id=5,
        admission=lambda chat, name: entered.append((chat, name)),  # type: ignore[arg-type, return-value]
    )
    assert out[0] == "ans"
    assert entered == []  # no reservation was made for an answered question
    assert rec.calls == 0


async def test_question_fallback_reserves_the_actual_named_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bus, _rec, orchs, _ = _make(tmp_path)
    monkeypatch.setattr(
        injection, "_answer_async_delegation_question", AsyncMock(return_value=None)
    )
    text, name, _ = await orchs["main"].handle_interagent_message(
        "worker",
        "\nq1\n",
        question_context=_question(),
        source_chat_id=-777,
        source_topic_id=5,
        lane="ask1",
        admission=lambda chat, name: bus._gates.reserve("main", chat, name),
    )
    assert name == interagent_session_name("worker", -777, 5, "ask1")
    assert text == "done q1"
    assert bus._gates.stats("main") == (0, 0, 0)  # entered and left


async def test_two_lane_questions_run_in_parallel_and_each_gets_its_own_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bus, rec, orchs, _ = _make(tmp_path)
    monkeypatch.setattr(
        injection, "_answer_async_delegation_question", AsyncMock(return_value=None)
    )
    for text in ("q1", "q2"):
        rec.gate(text)
    tasks = [
        asyncio.create_task(
            orchs["main"].handle_interagent_message(
                "worker",
                f"\n{text}\n",
                question_context=_question(),
                source_chat_id=-777,
                source_topic_id=5,
                lane=lane,
                admission=lambda chat, name: bus._gates.reserve("main", chat, name),
            )
        )
        for text, lane in (("q1", "one"), ("q2", "two"))
    ]
    await _until(lambda: len(rec.events) >= 2)
    assert sorted(rec.events) == ["start:q1", "start:q2"]  # parallel
    rec.gate("q1").set()
    rec.gate("q2").set()
    answers = await asyncio.gather(*tasks)
    assert [a[0] for a in answers] == ["done q1", "done q2"]
    assert bus._gates.stats("main") == (0, 0, 0)


async def test_bus_hands_a_question_the_admission_factory_explicitly(tmp_path: Path) -> None:
    """The bus passes the factory as an argument of the recipient call (no hidden state)."""
    bus, _rec, orchs, _ = _make(tmp_path)
    seen: dict[str, object] = {}

    async def fake(_sender, _message, **kwargs):
        seen.update(kwargs)
        return "ans", "", ""

    orchs["main"].handle_interagent_message = fake  # type: ignore[method-assign]
    await bus.send("worker", "main", "q?", ia_turn=True, chat_id=-777, topic_id=5)
    assert callable(seen["admission"])
    seen.clear()
    await bus.send("main", "worker", "\nx\n")  # a plain request reserves in the bus itself
    assert "admission" not in seen
