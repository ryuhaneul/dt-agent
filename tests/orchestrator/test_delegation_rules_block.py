"""Delegation requests to a sub-agent carry the approved delegated-work rules block."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ductor_bot.cli.types import CLIResponse
from ductor_bot.config import AgentConfig
from ductor_bot.multiagent.delegation import DelegationContext
from ductor_bot.orchestrator.core import Orchestrator
from ductor_bot.orchestrator.injection import DELEGATED_WORK_RULES
from ductor_bot.workspace.paths import DuctorPaths

_APPROVED = (
    "[Delegated work rules]\n"
    "- Do this work inside your current turn and return the result in your reply. "
    "Do not hand it to a background task (create_task): its result would come back to "
    "your own chat, not to the delegating agent's original conversation.\n"
    "- This turn has a hard limit (1 hour for async delegation). If the request cannot "
    "finish in time, do not start: reply that it needs to be split and suggest how. "
    "That reply is expected, not a failure.\n"
    "[/Delegated work rules]"
)


def _make(workspace: tuple[DuctorPaths, AgentConfig], agent_name: str) -> Orchestrator:
    paths, config = workspace
    config.allowed_user_ids = [12345]
    orch = Orchestrator(config, paths, agent_name=agent_name)
    cli = MagicMock()
    cli._config = MagicMock()
    cli._config.agent_name = agent_name
    cli._config.cli_timeout = 120
    cli.execute = AsyncMock(return_value=CLIResponse(session_id="s1", result="done"))
    object.__setattr__(orch, "_cli_service", cli)
    return orch


def _delegation(mode: str) -> DelegationContext:
    return DelegationContext(
        request_id="req1", mode=mode, sender="main", recipient="sub", chat_id=7, topic_id=None
    )


def test_block_text_is_the_approved_text_verbatim() -> None:
    assert DELEGATED_WORK_RULES == _APPROVED


@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_sub_agent_delegation_requests_get_the_block(
    workspace: tuple[DuctorPaths, AgentConfig], mode: str
) -> None:
    orch = _make(workspace, "sub")
    await orch.handle_interagent_message("main", "do the thing", delegation=_delegation(mode))
    prompt = orch._cli_service.execute.call_args[0][0].prompt
    assert prompt.count(_APPROVED) == 1
    assert "do the thing" in prompt
    assert prompt.index("[END INTER-AGENT MESSAGE]") < prompt.index(_APPROVED)
    assert ("This is a synchronous delegation" in prompt) is (mode == "sync")


async def test_sub_to_sub_request_also_gets_the_block(
    workspace: tuple[DuctorPaths, AgentConfig],
) -> None:
    orch = _make(workspace, "sub")
    await orch.handle_interagent_message("other-sub", "please check")
    assert _APPROVED in orch._cli_service.execute.call_args[0][0].prompt


async def test_main_never_gets_the_block(workspace: tuple[DuctorPaths, AgentConfig]) -> None:
    orch = _make(workspace, "main")
    await orch.handle_interagent_message("sub", "FYI result")
    assert _APPROVED not in orch._cli_service.execute.call_args[0][0].prompt


async def test_question_messages_do_not_get_the_block(
    workspace: tuple[DuctorPaths, AgentConfig],
) -> None:
    orch = _make(workspace, "main")
    question = DelegationContext(
        request_id="q1", mode="unknown", sender="main", recipient="sub", chat_id=7, topic_id=None
    )
    await orch.handle_interagent_message("sub", "which target?", question_context=question)
    assert _APPROVED not in orch._cli_service.execute.call_args[0][0].prompt


def test_block_is_not_logged_or_displayed_by_default() -> None:
    # The block only lives in the CLI prompt string; nothing in the module writes it elsewhere.
    from ductor_bot.orchestrator import injection

    source = Path(injection.__file__).read_text(encoding="utf-8")
    assert source.count("DELEGATED_WORK_RULES") == 2  # definition + the single prompt append
