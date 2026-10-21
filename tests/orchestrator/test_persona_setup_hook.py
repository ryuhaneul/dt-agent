"""First-run persona setup hint: only while PERSONA.md carries the unset marker."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from ductor_bot.cli.types import AgentResponse
from ductor_bot.config import AgentConfig
from ductor_bot.orchestrator.core import Orchestrator
from ductor_bot.orchestrator.flows import normal
from ductor_bot.orchestrator.hooks import PERSONA_SETUP, HookContext
from ductor_bot.session.key import SessionKey
from ductor_bot.workspace.loader import PERSONA_UNSET_MARKER


def _ctx(*, count: int = 0, new: bool = False, due: bool = True) -> HookContext:
    return HookContext(
        chat_id=1,
        message_count=count,
        is_new_session=new,
        provider="claude",
        model="opus",
        persona_setup_due=due,
    )


class TestCondition:
    def test_fires_on_first_turn_of_new_session(self) -> None:
        assert PERSONA_SETUP.condition(_ctx(new=True)) is True

    def test_fires_every_sixth_message_while_unset(self) -> None:
        assert PERSONA_SETUP.condition(_ctx(count=5)) is True
        assert PERSONA_SETUP.condition(_ctx(count=1)) is False

    def test_never_fires_when_persona_is_set(self) -> None:
        assert PERSONA_SETUP.condition(_ctx(new=True, due=False)) is False
        assert PERSONA_SETUP.condition(_ctx(count=5, due=False)) is False

    def test_suffix_names_the_files_and_the_marker(self) -> None:
        text = PERSONA_SETUP.suffix
        assert "FIRST-RUN SETUP" in text
        assert PERSONA_UNSET_MARKER in text
        assert "PERSONA.md" in text
        assert "BEHAVIOR.md" in text
        assert "memory_system/" in text


@pytest.fixture
def execute(orch: Orchestrator) -> AsyncMock:
    mock = AsyncMock(return_value=AgentResponse(result="OK", session_id="s-1", is_error=False))
    object.__setattr__(orch._cli_service, "execute", mock)
    return mock


def _write_persona(orch: Orchestrator, body: str) -> None:
    (orch.paths.workspace / "PERSONA.md").write_text(body, encoding="utf-8")


async def test_first_turn_carries_hint_and_blank_template_is_not_injected(
    orch: Orchestrator, execute: AsyncMock
) -> None:
    _write_persona(orch, f"{PERSONA_UNSET_MARKER}\n# Persona\n- Name:\n")
    (orch.paths.workspace / "BEHAVIOR.md").write_text("- Be brief.\n", encoding="utf-8")
    await normal(orch, SessionKey(chat_id=1), "hello")
    request = execute.call_args[0][0]
    assert "FIRST-RUN SETUP" in request.prompt
    assert request.prompt.startswith("hello")
    assert "# Persona" not in (request.append_system_prompt or "")
    assert "Be brief." in (request.append_system_prompt or "")


async def test_hint_repeats_on_sixth_message_and_stops_when_marker_removed(
    orch: Orchestrator, execute: AsyncMock
) -> None:
    _write_persona(orch, f"{PERSONA_UNSET_MARKER}\n# Persona\n")
    key = SessionKey(chat_id=1)
    for i in range(5):
        await normal(orch, key, f"m{i}")
    flags = ["FIRST-RUN SETUP" in c[0][0].prompt for c in execute.call_args_list]
    assert flags == [True, False, False, False, False]

    await normal(orch, key, "sixth")
    assert "FIRST-RUN SETUP" in execute.call_args[0][0].prompt

    _write_persona(orch, "# Persona\nName: Ari\n")  # agent removed the marker
    for i in range(6):
        await normal(orch, key, f"after-{i}")
    assert all("FIRST-RUN SETUP" not in c[0][0].prompt for c in execute.call_args_list[6:])
    assert "Name: Ari" in (execute.call_args[0][0].append_system_prompt or "")


async def test_setting_off_disables_the_hint(orch: Orchestrator, execute: AsyncMock) -> None:
    _write_persona(orch, f"{PERSONA_UNSET_MARKER}\n# Persona\n")
    orch._config.persona_setup_prompt = False
    await normal(orch, SessionKey(chat_id=1), "hello")
    assert "FIRST-RUN SETUP" not in execute.call_args[0][0].prompt


async def test_missing_persona_file_means_no_hint(orch: Orchestrator, execute: AsyncMock) -> None:
    await normal(orch, SessionKey(chat_id=1), "hello")
    assert "FIRST-RUN SETUP" not in execute.call_args[0][0].prompt


def test_setting_defaults_on_and_is_hot_reloadable() -> None:
    from ductor_bot.config_reload import _HOT_RELOADABLE

    assert AgentConfig().persona_setup_prompt is True
    assert "persona_setup_prompt" in _HOT_RELOADABLE
