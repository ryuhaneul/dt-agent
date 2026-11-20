"""Tests for agent-facing rule docs that are consumed by Gemini memory imports."""

from __future__ import annotations

from pathlib import Path

import pytest

from ductor_bot.orchestrator import hooks


def test_agent_tools_rules_contains_transport_info() -> None:
    rules_path = (
        Path(__file__).resolve().parents[2]
        / "ductor_bot"
        / "_home_defaults"
        / "workspace"
        / "tools"
        / "agent_tools"
        / "RULES.md"
    )

    content = rules_path.read_text(encoding="utf-8")

    # Transport-neutral: covers both Telegram and Matrix agent creation.
    assert "Telegram" in content
    assert "Matrix" in content
    assert "create_agent.py" in content


def test_templates_and_hooks_have_no_combined_rules_filename() -> None:
    repo = Path(__file__).resolve().parents[2]
    defaults = repo / "ductor_bot" / "_home_defaults"
    combined = "CLAUDE/GEMINI/AGENTS"
    offenders = [
        str(path.relative_to(repo))
        for path in defaults.rglob("*")
        if path.is_file() and combined.encode() in path.read_bytes()
    ]
    offenders.extend(
        f"hook:{hook.name}"
        for hook in vars(hooks).values()
        if isinstance(hook, hooks.MessageHook) and combined in hook.suffix
    )

    assert not offenders


@pytest.mark.parametrize(
    "template",
    [
        "cron_tools/RULES-all-clis.md",
        "cron_tools/RULES-claude-only.md",
        "cron_tools/RULES-codex-only.md",
        "task_tools/RULES.md",
    ],
)
def test_cron_and_task_rules_describe_autocompact_inheritance(template: str) -> None:
    defaults = Path(__file__).resolve().parents[2] / "ductor_bot" / "_home_defaults"
    path = defaults / "workspace" / "tools" / template
    section = path.read_text(encoding="utf-8").split("## Automatic compaction\n")[1]
    assert "uses `inherit` when `DUCTOR_AUTOCOMPACT` is absent" in section
    assert all(f"`{value}`" in section for value in ("500k", "0.5m", "500,000"))
    assert "topic's own setting" in section
    assert "global value at run time" in section
