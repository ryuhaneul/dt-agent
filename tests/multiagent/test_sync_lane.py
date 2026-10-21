"""Sync requests: own-session cycle rejection and the lane line of the delegated-work rules."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from ductor_bot.cli.types import AgentResponse
from ductor_bot.config import AgentConfig
from ductor_bot.multiagent.bus import SYNC_SELF_SESSION_REJECTED, InterAgentBus
from ductor_bot.orchestrator import injection
from ductor_bot.orchestrator.core import Orchestrator
from ductor_bot.session.named import interagent_session_name
from ductor_bot.workspace.paths import DuctorPaths


class Recorder:
    """Synthetic CLI: records every prompt it is given."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def execute(self, request: object) -> AgentResponse:
        self.prompts.append(request.prompt)  # type: ignore[attr-defined]
        return AgentResponse(result="done", session_id="sid")


def _make(tmp_path: Path):
    bus = InterAgentBus()
    rec = Recorder()
    for name in ("main", "worker"):
        home = tmp_path / name
        home.mkdir()
        config = AgentConfig(allowed_user_ids=[12345], model="sonnet")
        orch = Orchestrator(config, DuctorPaths(home), agent_name=name)
        orch._cli_service.execute = rec.execute
        stack = MagicMock()
        stack.bot.orchestrator = orch
        stack.config = config
        stack.bot.notification_service.notify = AsyncMock()
        stack.bot.notification_service.notify_all = AsyncMock()
        bus.register(name, stack)
    reserved: list[tuple[str, int, str]] = []
    real_reserve = bus._gates.reserve

    def counting_reserve(agent: str, chat_id: int, name: str):
        reserved.append((agent, chat_id, name))
        return real_reserve(agent, chat_id, name)

    bus._gates.reserve = counting_reserve  # type: ignore[method-assign]
    return bus, rec, reserved


# --- own-session sync cycle -----------------------------------------------------------


async def test_sync_to_own_current_session_is_rejected_at_once(tmp_path: Path) -> None:
    bus, rec, reserved = _make(tmp_path)
    own = interagent_session_name("worker", 0, None, "")

    reply = await bus.send("worker", "worker", "\nloop\n", source_session_name=own)

    assert reply.success is False
    assert reply.error == SYNC_SELF_SESSION_REJECTED
    assert reserved == []
    assert rec.prompts == []
    assert bus._gates._queues == {}


async def test_own_session_cycle_is_decided_per_lane(tmp_path: Path) -> None:
    bus, rec, reserved = _make(tmp_path)
    own = interagent_session_name("worker", 0, None, "a")

    rejected = await bus.send("worker", "worker", "\nloop\n", lane="a", source_session_name=own)
    assert rejected.error == SYNC_SELF_SESSION_REJECTED
    assert reserved == []

    # Another lane is another session: the parent lane is not inherited, so this is no cycle.
    other = await bus.send("worker", "worker", "\nwork\n", lane="b", source_session_name=own)
    assert other.success is True
    assert [r[2] for r in reserved] == [interagent_session_name("worker", 0, None, "b")]
    assert len(rec.prompts) == 1


async def test_sync_to_another_agent_is_not_a_self_cycle(tmp_path: Path) -> None:
    bus, rec, reserved = _make(tmp_path)
    own = interagent_session_name("worker", 0, None, "")

    reply = await bus.send("main", "worker", "\nwork\n", source_session_name=own)

    assert reply.success is True
    assert len(reserved) == 1
    assert len(rec.prompts) == 1


# --- lane line in the delegated-work rules --------------------------------------------


def _rules(prompt: str) -> str:
    start = prompt.index("[Delegated work rules]")
    return prompt[start : prompt.index("[/Delegated work rules]") + len("[/Delegated work rules]")]


async def test_laned_request_gets_the_lane_line(tmp_path: Path) -> None:
    bus, rec, _reserved = _make(tmp_path)

    await bus.send("main", "worker", "\nwork\n", lane="front")

    block = _rules(rec.prompts[0])
    assert block.startswith(injection.DELEGATED_WORK_RULES.removesuffix("[/Delegated work rules]"))
    lane_lines = [line for line in block.splitlines() if "lane 'front'" in line]
    assert len(lane_lines) == 1
    line = lane_lines[0]
    assert "Other lanes may run concurrently in the same workspace" in line
    assert "git worktree dedicated to this lane" in line
    assert "shared memory files" in line
    assert "report" in line
    assert block.endswith("[/Delegated work rules]")


async def test_unlaned_request_block_is_byte_identical(tmp_path: Path) -> None:
    bus, rec, _reserved = _make(tmp_path)

    await bus.send("main", "worker", "\nwork\n")

    assert _rules(rec.prompts[0]) == injection.DELEGATED_WORK_RULES
    assert "lane '" not in rec.prompts[0]


def test_delegation_notes_lane_line_only_for_work_requests() -> None:
    plain = injection._delegation_notes("worker", "main", None, None)
    laned = injection._delegation_notes("worker", "main", None, None, "front")
    assert plain.endswith("\n" + injection.DELEGATED_WORK_RULES)
    assert laned.startswith(plain.removesuffix(injection.DELEGATED_WORK_RULES))
    assert "lane 'front'" in laned
    assert "lane '" not in plain

    # A question (and an answer to it) gets no block at all — laned or not.
    question = MagicMock()
    assert injection._delegation_notes("worker", "main", None, question, "front") == ""
    assert injection._delegation_notes("main", "worker", None, None, "front") == ""
