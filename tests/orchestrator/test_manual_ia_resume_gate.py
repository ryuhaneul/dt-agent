"""Manual ``@ia.…`` resumes: shared FIFO gate, latest session id, storage boundary, session end."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ductor_bot.background import BackgroundResult
from ductor_bot.cli.types import AgentResponse
from ductor_bot.config import AgentConfig
from ductor_bot.i18n import t
from ductor_bot.messenger.telegram.transport import TelegramTransport
from ductor_bot.multiagent.session_gate import GateCancelled, SessionGateRegistry
from ductor_bot.orchestrator.core import Orchestrator
from ductor_bot.orchestrator.flows import named_session_flow, named_session_streaming
from ductor_bot.orchestrator.injection import _get_or_create_interagent_session
from ductor_bot.session.key import SessionKey
from ductor_bot.session.named import (
    MAX_INTERAGENT_SESSIONS_PER_CHAT,
    NamedSession,
    interagent_session_name,
)
from ductor_bot.workspace.init import init_workspace
from ductor_bot.workspace.paths import DuctorPaths
from tests.orchestrator.conftest import setup_framework

CHAT = 12345


class Cli:
    """Synthetic CLI: records every call (prompt, resume id) and can block per prompt."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self.running = 0
        self.max_running = 0
        self.gates: dict[str, asyncio.Event] = {}

    def gate(self, prompt: str) -> asyncio.Event:
        return self.gates.setdefault(prompt, asyncio.Event())

    async def execute(self, request: object) -> AgentResponse:
        prompt = request.prompt.split("\n")[0]  # type: ignore[attr-defined]
        self.calls.append((prompt, request.resume_session))  # type: ignore[attr-defined]
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        try:
            if prompt in self.gates:
                await self.gates[prompt].wait()
        finally:
            self.running -= 1
        return AgentResponse(result=f"done {prompt}", session_id=f"sid-{prompt}")

    async def execute_streaming(self, request: object, **_callbacks: object) -> AgentResponse:
        return await self.execute(request)


@pytest.fixture
def orch(tmp_path: Path) -> Orchestrator:
    fw_root = tmp_path / "fw"
    setup_framework(fw_root)
    paths = DuctorPaths(
        ductor_home=tmp_path / "home", home_defaults=fw_root / "workspace", framework_root=fw_root
    )
    init_workspace(paths)
    config = AgentConfig()
    config.allowed_user_ids = [CHAT]
    o = Orchestrator(config, paths, agent_name="worker")
    cli = Cli()
    mock = MagicMock()
    mock._config = MagicMock()
    mock._config.agent_name = "worker"
    mock.execute = cli.execute
    mock.execute_streaming = cli.execute_streaming
    object.__setattr__(o, "_cli_service", mock)
    o.test_cli = cli  # type: ignore[attr-defined]
    o._observers.init_task_observers(
        cron_manager=MagicMock(),
        webhook_manager=MagicMock(),
        cli_service=mock,
        codex_cache=MagicMock(),
    )
    return o


def _gates(orch: Orchestrator) -> SessionGateRegistry:
    gates = SessionGateRegistry(4)
    orch.set_session_gates(gates)
    return gates


def _session(orch: Orchestrator, lane: str = "x", sid: str = "sid-0") -> NamedSession:
    ns, _new, _notice = _get_or_create_interagent_session(
        orch, "main", source_chat_id=7, source_topic_id=1, lane=lane
    )
    ns.session_id = sid
    ns.status = "idle"
    return ns


KEY = SessionKey.telegram(CHAT)


async def _settle(times: int = 30) -> None:
    for _ in range(times):
        await asyncio.sleep(0)


async def _until(predicate, timeout: float = 5.0) -> None:  # noqa: ASYNC109
    """Wait for a condition that needs real threads (workspace file reads) to complete."""
    for _ in range(int(timeout / 0.01)):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


def _clean(gates: SessionGateRegistry) -> bool:
    return gates.stats("worker") == (0, 0, 0) and not gates._queues


# -- foreground and streaming: the same FIFO as inter-agent requests ------------------------


@pytest.mark.parametrize("streaming", [False, True])
async def test_manual_resume_waits_behind_a_running_inter_agent_request(
    orch: Orchestrator, streaming: bool
) -> None:
    gates = _gates(orch)
    ns = _session(orch)
    running = gates.reserve("worker", CHAT, ns.name)  # an inter-agent request is executing
    await running.admit()
    flow = named_session_streaming if streaming else named_session_flow
    task = asyncio.create_task(flow(orch, KEY, ns.name, "manual"))
    await _settle()
    assert orch.test_cli.calls == []  # waiting, and not rejected as "still running"
    assert not task.done()
    running.release()
    result = await asyncio.wait_for(task, 2)
    assert "done manual" in result.text
    assert orch.test_cli.calls == [("manual", "sid-0")]
    assert _clean(gates)


async def test_inter_agent_request_waits_behind_a_manual_resume(orch: Orchestrator) -> None:
    gates = _gates(orch)
    ns = _session(orch)
    orch.test_cli.gate("manual")
    first = asyncio.create_task(named_session_flow(orch, KEY, ns.name, "manual"))
    await _until(lambda: orch.test_cli.calls)
    later = gates.reserve("worker", CHAT, ns.name)  # an inter-agent request arrives afterwards
    waiter = asyncio.create_task(later.admit())
    await _settle()
    assert not waiter.done()
    assert orch.test_cli.max_running == 1
    orch.test_cli.gate("manual").set()
    await first
    await asyncio.wait_for(waiter, 2)
    later.release()
    assert _clean(gates)


async def test_two_manual_resumes_run_one_after_the_other(orch: Orchestrator) -> None:
    gates = _gates(orch)
    ns = _session(orch)
    orch.test_cli.gate("m1")
    t1 = asyncio.create_task(named_session_flow(orch, KEY, ns.name, "m1"))
    t2 = asyncio.create_task(named_session_flow(orch, KEY, ns.name, "m2"))
    await _until(lambda: [c[0] for c in orch.test_cli.calls] == ["m1"])
    orch.test_cli.gate("m1").set()
    await asyncio.gather(t1, t2)
    # the second one resumed with the session id the first one stored
    assert orch.test_cli.calls == [("m1", "sid-0"), ("m2", "sid-m1")]
    assert orch.test_cli.max_running == 1
    assert _clean(gates)


async def test_the_session_id_is_read_after_admission(orch: Orchestrator) -> None:
    gates = _gates(orch)
    ns = _session(orch, sid="old")
    running = gates.reserve("worker", CHAT, ns.name)
    await running.admit()
    task = asyncio.create_task(named_session_flow(orch, KEY, ns.name, "manual"))
    await _settle()
    assert orch.test_cli.calls == []
    orch._named_sessions.update_after_response(CHAT, ns.name, "new")  # the request stored its id
    running.release()
    await asyncio.wait_for(task, 2)
    assert orch.test_cli.calls == [("manual", "new")]  # not the "old" id read before the wait


async def test_user_named_sessions_are_still_rejected_while_running(orch: Orchestrator) -> None:
    gates = _gates(orch)
    ns = orch._named_sessions.create(CHAT, "claude", "opus", "prompt")  # a user session
    assert ns.status == "running"
    reply = await named_session_flow(orch, KEY, ns.name, "again")
    assert reply.text == t("session.still_running", name=ns.name)  # rejected, no CLI call
    assert orch.test_cli.calls == []
    ns.status = "idle"
    ok = await named_session_flow(orch, KEY, ns.name, "again")
    assert "done again" in ok.text
    assert gates.stats("worker") == (0, 0, 0)  # user sessions never touch the gates


async def test_without_gates_an_inter_agent_session_keeps_the_old_rejection(
    orch: Orchestrator,
) -> None:
    ns = _session(orch)
    ns.status = "running"
    reply = await named_session_flow(orch, KEY, ns.name, "manual")
    assert orch.test_cli.calls == []
    assert reply.text == t("session.still_running", name=ns.name)


# -- background resumes -------------------------------------------------------------------


def _bg(orch: Orchestrator) -> tuple[object, list[BackgroundResult], list[dict]]:
    gates_view: list[dict] = []
    results: list[BackgroundResult] = []
    bg = orch._observers.background
    assert bg is not None

    async def handler(result: BackgroundResult) -> None:
        gates = orch._session_gates
        sessions = orch._named_sessions
        ns = sessions.get(result.chat_id, result.session_name)
        gates_view.append(
            {
                "pending": gates.pending("worker", result.chat_id, result.session_name)
                if gates
                else -1,
                "sid": ns.session_id if ns else None,
                "status": ns.status if ns else None,
            }
        )
        results.append(result)

    bg.set_result_handler(handler)
    return bg, results, gates_view


async def _drain(bg) -> None:
    tasks = [t.asyncio_task for t in bg._tasks.values() if t.asyncio_task]
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 3)
    await _settle()


async def test_background_resume_queues_and_stores_before_the_place_is_returned(
    orch: Orchestrator,
) -> None:
    gates = _gates(orch)
    ns = _session(orch, sid="old")
    bg, results, view = _bg(orch)
    running = gates.reserve("worker", CHAT, ns.name)
    await running.admit()
    ns.status = "running"
    orch.submit_named_followup_bg(CHAT, ns.name, "bgmsg", 1, None)  # not rejected as "processing"
    await _settle()
    assert orch.test_cli.calls == []
    orch._named_sessions.update_after_response(CHAT, ns.name, "new")
    running.release()
    await _drain(bg)
    assert orch.test_cli.calls == [("bgmsg", "new")]  # the latest id, read after the wait
    assert results[0].status == "ok"
    # At delivery time the response was stored and the place was already returned.
    assert view == [{"pending": 0, "sid": "sid-bgmsg", "status": "idle"}]
    assert _clean(gates)


async def test_background_resume_takes_the_latest_provider_with_the_latest_session_id(
    orch: Orchestrator,
) -> None:
    gates = _gates(orch)
    ns = _session(orch, sid="claude-sid")
    seen: list[tuple[str | None, str | None, str | None, str | None]] = []
    inner = orch._cli_service.execute

    async def spy(request):
        seen.append(
            (
                request.provider_override,
                request.model_override,
                request.effort_override,
                request.resume_session,
            )
        )
        return await inner(request)

    orch._cli_service.execute = spy
    bg, _results, _view = _bg(orch)
    running = gates.reserve("worker", CHAT, ns.name)
    await running.admit()
    ns.status = "running"
    orch.submit_named_followup_bg(CHAT, ns.name, "bgmsg", 1, None)
    await _until(lambda: gates.pending("worker", CHAT, ns.name) == 2)
    # While it waited, the same session was recreated for another provider.
    ns.provider, ns.model, ns.reasoning_effort, ns.session_id = (
        "codex",
        "gpt-5.2-codex",
        "high",
        "codex-sid",
    )
    running.release()
    await _drain(bg)

    assert seen == [("codex", "gpt-5.2-codex", "high", "codex-sid")]


async def test_background_resume_keeps_the_submitted_target_when_nothing_changed(
    orch: Orchestrator,
) -> None:
    gates = _gates(orch)
    ns = _session(orch, sid="claude-sid")
    seen: list[tuple[str | None, str | None]] = []
    inner = orch._cli_service.execute

    async def spy(request):
        seen.append((request.provider_override, request.model_override))
        return await inner(request)

    orch._cli_service.execute = spy
    bg, _results, _view = _bg(orch)
    running = gates.reserve("worker", CHAT, ns.name)
    await running.admit()
    ns.status = "running"
    orch.submit_named_followup_bg(CHAT, ns.name, "bgmsg", 1, None)
    await _until(lambda: gates.pending("worker", CHAT, ns.name) == 2)
    running.release()
    await _drain(bg)

    assert seen == [(ns.provider, ns.model)]


async def test_background_resumes_of_one_session_never_overlap(orch: Orchestrator) -> None:
    gates = _gates(orch)
    ns = _session(orch)
    bg, results, _view = _bg(orch)
    orch.test_cli.gate("a")
    orch.submit_named_followup_bg(CHAT, ns.name, "a", 1, None)
    orch.submit_named_followup_bg(CHAT, ns.name, "b", 2, None)
    await _until(lambda: [c[0] for c in orch.test_cli.calls] == ["a"])
    await _settle()
    assert [c[0] for c in orch.test_cli.calls] == ["a"]  # "b" waits
    orch.test_cli.gate("a").set()
    await _drain(bg)
    assert orch.test_cli.calls == [("a", "sid-0"), ("b", "sid-a")]
    assert orch.test_cli.max_running == 1
    assert len(results) == 2
    assert _clean(gates)


async def test_background_user_session_is_still_rejected_while_running(orch: Orchestrator) -> None:
    _gates(orch)
    ns = orch._named_sessions.create(CHAT, "claude", "opus", "prompt")
    with pytest.raises(ValueError, match="still processing"):
        orch.submit_named_followup_bg(CHAT, ns.name, "again", 1, None)


async def test_cancelling_a_background_task_frees_its_place(orch: Orchestrator) -> None:
    gates = _gates(orch)
    ns = _session(orch)
    bg, _results, _view = _bg(orch)
    orch.test_cli.gate("a")
    orch.submit_named_followup_bg(CHAT, ns.name, "a", 1, None)
    orch.submit_named_followup_bg(CHAT, ns.name, "b", 2, None)
    await _until(lambda: len(orch.test_cli.calls) == 1)  # "a" runs, "b" waits behind it
    for task in list(bg._tasks.values()):
        task.asyncio_task.cancel()
    await _drain(bg)
    assert _clean(gates)


# -- storage boundary in the transports -------------------------------------------------


async def test_telegram_transport_leaves_storing_inter_agent_responses_to_the_observer() -> None:
    bot = MagicMock()
    named = bot._orch.named_sessions
    transport = TelegramTransport(bot)
    ia_env = SimpleNamespace(
        chat_id=5,
        session_name=interagent_session_name("main", 7, 1, "x"),
        session_id="s1",
        elapsed_seconds=1,
        status="ok",
        result_text="r",
        prompt_preview="p",
        topic_id=None,
        thread_id=None,
        reply_to_message_id=None,
        provider="claude",
        model="opus",
    )
    user_env = SimpleNamespace(**{**vars(ia_env), "session_name": "redowl", "session_id": "s2"})
    with (
        patch("ductor_bot.messenger.telegram.transport.send_rich", AsyncMock()),
        patch.object(TelegramTransport, "_format_named_session", return_value="text"),
        patch.object(TelegramTransport, "_opts", return_value=SimpleNamespace(reply_markup=None)),
    ):
        await transport._deliver_background(ia_env)  # type: ignore[arg-type]
        named.update_after_response.assert_not_called()
        await transport._deliver_background(user_env)  # type: ignore[arg-type]
    named.update_after_response.assert_called_once_with(5, "redowl", "s2")


# -- eviction protection and the end of a session ----------------------------------------


async def test_sessions_with_gate_places_are_not_evicted(orch: Orchestrator) -> None:
    gates = _gates(orch)
    protected = _session(orch, lane="keep")
    reservation = gates.reserve("worker", CHAT, protected.name)  # waiting or running
    protected.created_at = 0.0  # the oldest one: the first eviction candidate
    protected.status = "idle"
    for i in range(MAX_INTERAGENT_SESSIONS_PER_CHAT + 4):
        _session(orch, lane=f"l{i}")
    assert orch._named_sessions.get(CHAT, protected.name) is not None
    reservation.release()
    assert orch._named_sessions.is_protected is not None
    assert not orch._named_sessions.is_protected(CHAT, protected.name)


async def test_ending_a_session_cancels_waiting_requests_only_for_that_session(
    orch: Orchestrator,
) -> None:
    gates = _gates(orch)
    a, b = _session(orch, lane="a"), _session(orch, lane="b")
    running_a = gates.reserve("worker", CHAT, a.name)
    await running_a.admit()
    waiting_a = gates.reserve("worker", CHAT, a.name)
    wait_task = asyncio.create_task(waiting_a.admit())
    manual = asyncio.create_task(named_session_flow(orch, KEY, a.name, "manual-a"))
    other = asyncio.create_task(named_session_flow(orch, KEY, b.name, "manual-b"))
    await _until(lambda: gates.pending("worker", CHAT, a.name) == 3)  # running + both waiting
    assert await orch.end_named_session(CHAT, a.name) is True
    with pytest.raises(GateCancelled):
        await wait_task
    reply = await asyncio.wait_for(manual, 2)
    assert "done" not in reply.text  # the waiting manual resume gave up
    assert "done manual-b" in (await asyncio.wait_for(other, 2)).text  # the other lane ran
    assert [c[0] for c in orch.test_cli.calls] == ["manual-b"]
    running_a.release()
    assert _clean(gates)


async def test_a_late_response_does_not_revive_an_ended_session(orch: Orchestrator) -> None:
    _gates(orch)
    ns = _session(orch, sid="old")
    await orch.end_named_session(CHAT, ns.name)
    orch._named_sessions.update_after_response(CHAT, ns.name, "late", status="idle")
    assert ns.status == "ended"
    assert ns.session_id == "old"
    # a request that arrives after the end starts a fresh session (by design), not the old one
    fresh, is_new, _ = _get_or_create_interagent_session(
        orch, "main", source_chat_id=7, source_topic_id=1, lane="x"
    )
    assert is_new
    assert fresh.session_id == ""


async def test_ending_a_session_stops_both_process_labels(orch: Orchestrator) -> None:
    _gates(orch)
    ns = _session(orch)
    killed: list[str] = []

    async def kill(_chat: int, label: str) -> int:
        killed.append(label)
        return 0

    orch._process_registry.kill_by_label = kill  # type: ignore[method-assign]
    await orch.end_named_session(CHAT, ns.name)
    assert killed == [f"ns:{ns.name}", f"interagent:{ns.name}"]
