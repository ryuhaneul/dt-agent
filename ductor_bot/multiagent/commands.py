"""Telegram command handlers for multi-agent management.

Registered only on the main agent's Orchestrator when a supervisor is present.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ductor_bot.i18n import t
from ductor_bot.orchestrator.registry import OrchestratorResult
from ductor_bot.text.response_format import (
    SEP,
    fmt,
    format_uptime,
    show_agent_status,
    show_crash_error,
)

if TYPE_CHECKING:
    from ductor_bot.orchestrator.core import Orchestrator
    from ductor_bot.session.key import SessionKey

logger = logging.getLogger(__name__)

_STATUS_EMOJI = {
    "running": "●",
    "starting": "◐",
    "crashed": "✖",
    "stopped": "○",
}


async def cmd_agents(orch: Orchestrator, _key: SessionKey, _text: str) -> OrchestratorResult:
    """Handle /agents: list all agents with status."""
    supervisor = orch.supervisor
    if supervisor is None:
        return OrchestratorResult(text=t("agents.not_active"))

    lines: list[str] = []
    for name in sorted(supervisor.health.keys()):
        health = supervisor.health[name]
        stack = supervisor.stacks.get(name)
        emoji = _STATUS_EMOJI.get(health.status, "?")
        role = t("agent_role.main") if (stack and stack.is_main) else t("agent_role.sub")

        info = f"  {emoji} **{name}** [{role}] — {show_agent_status(health.status)}"
        if health.status == "running" and health.uptime_human:
            info += f" ({format_uptime(health.uptime_seconds)})"
        if stack:
            model_label = stack.config.model
            effort = stack.config.reasoning_effort
            if effort:
                model_label += f" ({effort})"
            info += f" | {model_label}"
        if health.restart_count > 0:
            info += t("agents.restarts", count=health.restart_count)
        if health.status == "crashed" and health.last_crash_error:
            error = show_crash_error(health.last_crash_error)[:100]
            info += f"\n      {t('agents.error_line', error=error)}"
        lines.append(info)

    if not lines:
        return OrchestratorResult(text=fmt(t("agents.header"), SEP, t("agents.empty")))

    return OrchestratorResult(text=fmt(t("agents.header"), SEP, "\n".join(lines)))


async def cmd_agent_stop(orch: Orchestrator, _key: SessionKey, text: str) -> OrchestratorResult:
    """Handle /agent_stop <name>: stop a sub-agent."""
    supervisor = orch.supervisor
    if supervisor is None:
        return OrchestratorResult(text=t("agents.not_active"))

    parts = text.split(None, 1)
    if len(parts) < 2:
        return OrchestratorResult(text=t("agents.usage_stop"))

    name = parts[1].strip().lower()
    if name == "main":
        return OrchestratorResult(text=t("agents.cannot_stop_main"))

    if name not in supervisor.stacks:
        return OrchestratorResult(text=t("agents.not_running", name=name))

    await supervisor.stop_agent(name)
    return OrchestratorResult(text=t("agents.stopped", name=name))


async def cmd_agent_start(orch: Orchestrator, _key: SessionKey, text: str) -> OrchestratorResult:
    """Handle /agent_start <name>: start a sub-agent from the registry."""
    supervisor = orch.supervisor
    if supervisor is None:
        return OrchestratorResult(text=t("agents.not_active"))

    parts = text.split(None, 1)
    if len(parts) < 2:
        return OrchestratorResult(text=t("agents.usage_start"))

    name = parts[1].strip().lower()
    result = await supervisor.start_agent_by_name(name)
    return OrchestratorResult(text=result)


async def cmd_agent_restart(orch: Orchestrator, _key: SessionKey, text: str) -> OrchestratorResult:
    """Handle /agent_restart <name>: restart a sub-agent."""
    supervisor = orch.supervisor
    if supervisor is None:
        return OrchestratorResult(text=t("agents.not_active"))

    parts = text.split(None, 1)
    if len(parts) < 2:
        return OrchestratorResult(text=t("agents.usage_restart"))

    name = parts[1].strip().lower()
    result = await supervisor.restart_agent(name)
    return OrchestratorResult(text=result)
