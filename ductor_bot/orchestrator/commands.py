"""Command handlers for all slash commands."""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

from ductor_bot.cli.auth import check_all_auth
from ductor_bot.i18n import t
from ductor_bot.infra.version import get_current_version
from ductor_bot.orchestrator.registry import OrchestratorResult
from ductor_bot.orchestrator.selectors.cron_selector import cron_selector_start
from ductor_bot.orchestrator.selectors.model_selector import (
    effort_selector_start,
    model_selector_start,
    switch_model,
)
from ductor_bot.orchestrator.selectors.session_selector import session_selector_start
from ductor_bot.orchestrator.selectors.task_selector import task_selector_start
from ductor_bot.text.response_format import (
    SEP,
    fmt,
    format_uptime,
    new_session_text,
    show_agent_role,
    show_agent_status,
    show_auth_status,
    show_crash_error,
)
from ductor_bot.workspace.loader import read_mainmemory

if TYPE_CHECKING:
    from ductor_bot.orchestrator.core import Orchestrator
    from ductor_bot.session.key import SessionKey

logger = logging.getLogger(__name__)


# -- Command wrappers (registered by Orchestrator._register_commands) --


async def cmd_reset(orch: Orchestrator, key: SessionKey, _text: str) -> OrchestratorResult:
    """Handle /new: kill processes and reset only active provider session."""
    logger.info("Reset requested")
    await orch._process_registry.kill_by_chat_topic(key.chat_id, key.topic_id)
    provider = await orch.reset_active_provider_session(key)
    return OrchestratorResult(text=new_session_text(provider))


async def cmd_reset_current(orch: Orchestrator, key: SessionKey, _text: str) -> OrchestratorResult:
    """Handle /reset: kill processes and reset the *current* provider session."""
    logger.info("Reset (current) requested")
    await orch._process_registry.kill_by_chat_topic(key.chat_id, key.topic_id)
    provider = await orch.reset_current_provider_session(key)
    return OrchestratorResult(text=new_session_text(provider))


async def cmd_status(orch: Orchestrator, key: SessionKey, _text: str) -> OrchestratorResult:
    """Handle /status."""
    logger.info("Status requested")
    return OrchestratorResult(text=await _build_status(orch, key))


async def cmd_model(orch: Orchestrator, key: SessionKey, text: str) -> OrchestratorResult:
    """Handle /model [name]."""
    logger.info("Model requested")
    parts = text.split(None, 1)
    if len(parts) < 2:
        resp = await model_selector_start(orch, key)
        return OrchestratorResult(text=resp.text, buttons=resp.buttons)
    name = parts[1].strip()
    result_text = await switch_model(orch, key, name)
    return OrchestratorResult(text=result_text)


async def cmd_effort(orch: Orchestrator, key: SessionKey, _text: str) -> OrchestratorResult:
    """Handle /effort: show reasoning-effort buttons for the active provider."""
    logger.info("Effort requested")
    resp = await effort_selector_start(orch, key)
    return OrchestratorResult(text=resp.text, buttons=resp.buttons)


async def cmd_memory(orch: Orchestrator, _key: SessionKey, _text: str) -> OrchestratorResult:
    """Handle /memory."""
    logger.info("Memory requested")
    content = await asyncio.to_thread(read_mainmemory, orch.paths)
    if not content.strip():
        return OrchestratorResult(
            text=fmt(
                t("memory.header"),
                SEP,
                t("memory.empty"),
                SEP,
                t("memory.empty_tip"),
            ),
        )
    return OrchestratorResult(
        text=fmt(
            t("memory.header"),
            SEP,
            content,
            SEP,
            t("memory.filled_tip"),
        ),
    )


async def cmd_sessions(orch: Orchestrator, key: SessionKey, _text: str) -> OrchestratorResult:
    """Handle /sessions."""
    logger.info("Sessions requested")
    resp = await session_selector_start(orch, key.chat_id)
    return OrchestratorResult(text=resp.text, buttons=resp.buttons)


async def cmd_tasks(orch: Orchestrator, key: SessionKey, _text: str) -> OrchestratorResult:
    """Handle /tasks."""
    logger.info("Tasks requested")
    hub = orch.task_hub
    if hub is None:
        return OrchestratorResult(
            text=fmt(t("tasks.header"), SEP, t("tasks.disabled")),
        )
    resp = task_selector_start(hub, key.chat_id)
    return OrchestratorResult(text=resp.text, buttons=resp.buttons)


async def cmd_cron(orch: Orchestrator, _key: SessionKey, _text: str) -> OrchestratorResult:
    """Handle /cron."""
    logger.info("Cron requested")
    resp = await cron_selector_start(orch)
    return OrchestratorResult(text=resp.text, buttons=resp.buttons)


def _build_codex_cache_block(orch: Orchestrator) -> str:
    """Build the Codex model cache section for /diagnose."""
    if not orch._observers.codex_cache_obs:
        return "\n🔄 " + t("diagnose.codex_cache_not_init")
    cache = orch._observers.codex_cache_obs.get_cache()
    if not cache or not cache.models:
        return "\n🔄 " + t("diagnose.codex_cache_not_loaded")
    default_model = next((m.id for m in cache.models if m.is_default), "N/A")
    return "\n🔄 " + t(
        "diagnose.codex_cache_info",
        updated=cache.last_updated,
        count=len(cache.models),
        default=default_model,
    )


def _build_diagnose_health_block(orch: Orchestrator) -> str:
    """Build the multi-agent health section for /diagnose."""
    supervisor = orch._supervisor
    if supervisor is None:
        return ""
    status_icon = {"running": "●", "starting": "◐", "crashed": "✖", "stopped": "○"}
    agent_lines = ["\n" + t("diagnose.health_header")]
    for name in sorted(supervisor.health.keys()):
        h = supervisor.health[name]
        icon = status_icon.get(h.status, "?")
        role = show_agent_role(name)
        line = f"  {icon} `{name}` [{role}] — {show_agent_status(h.status)}"
        if h.status == "running" and h.uptime_human:
            line += f" ({format_uptime(h.uptime_seconds)})"
        if h.restart_count > 0:
            line += t("diagnose.restarts", count=h.restart_count)
        if h.status == "crashed" and h.last_crash_error:
            line += f"\n      `{show_crash_error(h.last_crash_error)[:100]}`"
        agent_lines.append(line)
    return "\n".join(agent_lines)


def _resolve_log_path(orch: Orchestrator) -> Path:
    """Return the best available log file path.

    Sub-agents don't have their own log files — fall back to the central
    log in the main ductor home (parent of ``agents/<name>``).
    """
    log_path = orch.paths.logs_dir / "agent.log"
    if not log_path.exists():
        main_logs = orch.paths.ductor_home.parent.parent / "logs" / "agent.log"
        if main_logs.exists():
            return main_logs
    return log_path


async def cmd_diagnose(orch: Orchestrator, _key: SessionKey, _text: str) -> OrchestratorResult:
    """Handle /diagnose."""
    logger.info("Diagnose requested")
    version = get_current_version()
    effective_model, effective_provider = orch.resolve_runtime_target(orch._config.model)
    info_block = (
        f"{t('diagnose.version_line', version=version)}\n"
        f"{t('diagnose.configured_line', provider=orch._config.provider, model=orch._config.model)}\n"
        f"{t('diagnose.effective_line', provider=effective_provider, model=effective_model)}"
    )

    cache_block = _build_codex_cache_block(orch)
    agent_block = _build_diagnose_health_block(orch)

    log_tail = await _read_log_tail(_resolve_log_path(orch))
    log_block = (
        f"{t('diagnose.log_header')}\n```\n{log_tail}\n```" if log_tail else t("diagnose.no_log")
    )

    return OrchestratorResult(
        text=fmt(t("diagnose.header"), SEP, info_block, cache_block, agent_block, SEP, log_block),
    )


# -- Helpers ------------------------------------------------------------------


def _build_agent_health_block(orch: Orchestrator) -> str:
    """Build the multi-agent health section for /status (main agent only)."""
    supervisor = orch._supervisor
    if supervisor is None or len(supervisor.health) <= 1:
        return ""

    status_icon = {
        "running": "●",
        "starting": "◐",
        "crashed": "✖",
        "stopped": "○",
    }
    agent_lines = [t("status.agents_header")]
    for name in sorted(supervisor.health.keys()):
        if name == "main":
            continue
        h = supervisor.health[name]
        icon = status_icon.get(h.status, "?")
        line = f"  {icon} {name} — {show_agent_status(h.status)}"
        if h.status == "running" and h.uptime_human:
            line += f" ({format_uptime(h.uptime_seconds)})"
        if h.restart_count > 0:
            line += f" ⟳{h.restart_count}"
        if h.status == "crashed" and h.last_crash_error:
            line += f"\n      {show_crash_error(h.last_crash_error)[:80]}"
        agent_lines.append(line)
    return "\n".join(agent_lines)


def _status_effort_suffix(orch: Orchestrator, model_name: str, effort: str) -> str:
    """Return the ``/status`` reasoning-effort line for effort-using providers.

    *effort* is the effective effort (the session's value in a topic, else the
    global default) so /status reflects what the next turn actually uses.
    """
    provider = orch.models.provider_for(model_name)
    if provider in ("codex", "claude", "grok") and effort and effort != "default":
        return f"\n{t('status.effort_line', effort=effort)}"
    return ""


async def _build_status(orch: Orchestrator, key: SessionKey) -> str:
    """Build the /status response text."""
    runtime_model, _runtime_provider = orch.resolve_runtime_target(orch._config.model)
    configured_model = orch._config.model

    def _model_line(model_name: str) -> str:
        if model_name == configured_model:
            return t("status.model_line", model=model_name)
        return t("status.model_line_configured", model=model_name, configured=configured_model)

    session = await orch._sessions.get_active(key)
    if session:
        topic_line = (
            f"{t('status.topic_line', topic=session.topic_name)}\n" if session.topic_name else ""
        )
        session_block = (
            f"{topic_line}"
            f"{t('status.session_line', sid=session.session_id[:8] + '...')}\n"
            f"{t('status.messages_line', count=session.message_count)}\n"
            f"{_model_line(session.model)}"
            f"{_status_effort_suffix(orch, session.model, session.reasoning_effort or orch._config.reasoning_effort)}"
        )
    else:
        session_block = (
            f"{t('status.no_session')}\n"
            f"{_model_line(runtime_model)}{_status_effort_suffix(orch, runtime_model, orch._config.reasoning_effort)}"
        )

    bg_tasks = orch.active_background_tasks(key.chat_id)
    bg_block = ""
    if bg_tasks:
        import time

        bg_lines = [t("status.bg_header", count=len(bg_tasks))]
        for bg_t in bg_tasks:
            age = time.monotonic() - bg_t.submitted_at
            bg_lines.append(
                f"  `{bg_t.task_id}` {bg_t.prompt[:40]}... ({t('age.s', n=f'{age:.0f}')})"
            )
        bg_block = "\n".join(bg_lines)

    auth = await asyncio.to_thread(check_all_auth)
    auth_lines: list[str] = []
    for provider, result in auth.items():
        age_label = f" ({result.age_human})" if result.age_human else ""
        auth_lines.append(f"  [{provider}] {show_auth_status(result.status.value)}{age_label}")
    auth_block = t("status.auth_header") + "\n" + "\n".join(auth_lines)

    streaming_cfg = orch._config.streaming

    def _on_off(flag: bool) -> str:
        return t("status.on") if flag else t("status.off")

    streaming_block = "\n".join(
        [
            t("status.streaming_header"),
            t("status.reasoning_stream", value=_on_off(streaming_cfg.show_reasoning_stream)),
            t("status.tool_progress", value=_on_off(streaming_cfg.show_tool_progress)),
            t("status.thinking_indicator", value=_on_off(streaming_cfg.show_thinking_indicator)),
        ]
    )

    agent_block = _build_agent_health_block(orch)

    blocks = [t("status.header"), SEP, session_block]
    if bg_block:
        blocks += [SEP, bg_block]
    blocks += [SEP, auth_block, SEP, streaming_block]
    if agent_block:
        blocks += [SEP, agent_block]
    return fmt(*blocks)


# A line of the log file (see ``logging_config.FILE_FMT``): time, level, logger, source position,
# the optional ``[op:chat:sid]`` context, then the message.
_LOG_LINE = re.compile(
    r"^(?P<head>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \[[A-Z]+\] (?P<logger>[\w.]+):[^:\s]+:\d+: "
    r"(?:\[[^\]\n]*\] )?)(?P<message>.*)$"
)
_COST_FIELD = r" cost=\$\d+(?:\.\d+)?"
_TOKENS_FIELD = r" tokens=\d+"
# (logger, start of the completion message, figures to drop). Only these exact pairs were ever
# written with figures, so nothing else in the log is touched.
_OLD_FIGURE_LINES = (
    ("ductor_bot.cli.claude_provider", r"CLI done ", _COST_FIELD + "|" + _TOKENS_FIELD),
    ("ductor_bot.cli.grok_provider", r"Grok done ", _COST_FIELD + "|" + _TOKENS_FIELD),
    ("ductor_bot.cli.codex_provider", r"Codex done ", _TOKENS_FIELD),
    ("ductor_bot.cli.service", r"CLI \S+ \[(?:ok|error)\] ", _COST_FIELD + "|" + _TOKENS_FIELD),
)
_OLD_FIGURE_RULES = tuple(
    (logger, re.compile(prefix), re.compile(fields)) for logger, prefix, fields in _OLD_FIGURE_LINES
)


def _hide_old_figures(line: str) -> str:
    """Drop the token and cost figures from a completion line written by an older version.

    The previous token/cost tracking was wrong: it added the CLI's cumulative session cost on
    every turn and left cache tokens out of the token count. An accurate rework was judged not
    worth it, so the tracking was removed. Log files written before that still hold the figures;
    the file is left as it is and only this display hides them.
    """
    found = _LOG_LINE.match(line)
    if found is None:
        return line
    for logger_name, prefix, fields in _OLD_FIGURE_RULES:
        if found["logger"] == logger_name and prefix.match(found["message"]):
            return found["head"] + fields.sub("", found["message"])
    return line


async def _read_log_tail(log_path: Path, lines: int = 50) -> str:
    """Read the last *lines* of a log file without blocking the event loop."""

    def _read() -> str:
        if not log_path.is_file():
            return ""
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
            tail = text.strip().splitlines()[-lines:]
            return "\n".join(_hide_old_figures(line) for line in tail)
        except OSError:
            return t("diagnose.log_unreadable")

    return await asyncio.to_thread(_read)
