"""Shared one-shot CLI task execution for cron, webhook, and background observers."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from ductor_bot.config import resolve_user_timezone

if TYPE_CHECKING:
    from ductor_bot.cli.param_resolver import TaskExecutionConfig, TaskOverrides
    from ductor_bot.cron.execution import OneShotCommand, OneShotExecutionResult
    from ductor_bot.infra.base_task_observer import BaseTaskObserver

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TaskResult:
    """Normalized outcome of a one-shot task run."""

    status: str
    result_text: str
    execution: OneShotExecutionResult | None


@dataclass(frozen=True, slots=True)
class TaskRunOptions:
    """Execution options shared by cron, webhook, and background one-shot runs."""

    cwd: Path
    timeout_seconds: float
    timeout_label: str
    ductor_home: Path | None = None
    env_overrides: dict[str, str | None] = field(default_factory=dict)
    routing_env: dict[str, str] | None = None


_ROUTING_KEYS = (
    "DUCTOR_CHAT_ID",
    "DUCTOR_TOPIC_ID",
    "DUCTOR_TRANSPORT",
    "DUCTOR_RESULT_ORIGIN",
    "DUCTOR_BROADCAST_REF",
    "DUCTOR_IA_TURN",
    "DUCTOR_AGENT_NAME",
    "DUCTOR_AGENT_ROLE",
    "DUCTOR_INTERAGENT_HOST",
    "DUCTOR_INTERAGENT_PORT",
)


def _normalize_routing_env(one_shot: OneShotCommand, routing_env: dict[str, str]) -> None:
    """Apply this task's route after parent, command, and bot-home environments merge."""
    inherited = set(os.environ) | set(one_shot.env_overrides)
    keys = set(_ROUTING_KEYS)
    keys.update(key for key in inherited if key.startswith(("DUCTOR_ORIGIN_", "DUCTOR_IA_")))
    one_shot.env_overrides.update(dict.fromkeys(keys))
    one_shot.env_overrides.update(routing_env)


def _resolve_tz(tz_name: str) -> ZoneInfo:
    """Resolve configured, host and fallback timezones consistently."""
    return resolve_user_timezone(tz_name)


def _inject_dotenv_overrides(one_shot: OneShotCommand, ductor_home: Path) -> None:
    """Merge the home ``.env`` values into one_shot.env_overrides.

    Mirrors cli/executor.py:_build_env policy — keys already in os.environ
    or already set in env_overrides are never overridden (low priority).
    Re-reads on mtime change via load_env_secrets cache so .env edits take
    effect without a bot restart.
    """
    import os

    from ductor_bot.infra.env_secrets import load_env_secrets

    for k, v in load_env_secrets(ductor_home / ".env").items():
        if k not in os.environ and k not in one_shot.env_overrides:
            one_shot.env_overrides[k] = v


def _supply_secrets_store_path(one_shot: OneShotCommand, ductor_home: Path) -> None:
    """Default ``DUCTOR_SECRETS_FILE`` to the main home's store for this run.

    Only when the process environment, the home ``.env`` and the command's own overrides do not
    name a store already; a caller's override applied afterwards still wins.
    """
    from ductor_bot.infra.env_secrets import (
        SECRETS_FILE_NAME,
        SECRETS_FILE_VARIABLE,
        main_home_of,
    )

    if os.environ.get(SECRETS_FILE_VARIABLE) or SECRETS_FILE_VARIABLE in one_shot.env_overrides:
        return
    one_shot.env_overrides[SECRETS_FILE_VARIABLE] = str(
        main_home_of(ductor_home) / SECRETS_FILE_NAME
    )


async def _save_claude_usage(
    stdout: bytes, working_dir: str, slot: str = "claude", *, captured_at: int
) -> None:
    """Persist passive one-shot events without changing task or rotation outcomes."""
    from ductor_bot.cli.claude_usage import UsageStore, extract_usage_events, usage_file_path

    try:
        store = UsageStore(usage_file_path(Path(working_dir) / "workspace"))
        for event in extract_usage_events(stdout):
            await asyncio.to_thread(store.record, slot, event, captured_at=captured_at)
    except Exception as exc:
        logger.warning("Claude usage save failed: %s", type(exc).__name__)


async def run_oneshot_task(
    exec_config: TaskExecutionConfig,
    prompt: str,
    options: TaskRunOptions,
) -> TaskResult:
    """Build the CLI command and execute it, returning a normalized result.

    Returns a ``cli_not_found`` result instead of raising when the provider
    binary is missing.  All other execution details (timeout, stderr, status
    mapping) are delegated to ``execute_one_shot``.

    When the resolved provider is ``claude`` and ``claude_accounts`` is
    non-empty, the call is routed through the shared slot-rotation driver
    so cron/webhook/background tasks share the same cooldown state and
    fallback behaviour as the interactive chat provider.
    """
    from ductor_bot.cron.execution import build_cmd, execute_one_shot

    if exec_config.provider == "claude" and exec_config.claude_accounts:
        return await _run_claude_with_rotation(exec_config, prompt, options)

    one_shot = build_cmd(exec_config, prompt)
    if one_shot is None:
        return TaskResult(
            status=f"error:cli_not_found_{exec_config.provider}",
            result_text=f"[{exec_config.provider} CLI not found]",
            execution=None,
        )

    if exec_config.provider == "claude":
        from ductor_bot.cli.slot_rotation import slot_extra_env

        one_shot.env_overrides.update(
            slot_extra_env("claude", working_dir=str(Path(exec_config.working_dir) / "workspace"))
        )
    if options.ductor_home is not None:
        one_shot.env_overrides["DUCTOR_HOME"] = str(options.ductor_home)
        _inject_dotenv_overrides(one_shot, options.ductor_home)
        _supply_secrets_store_path(one_shot, options.ductor_home)
    one_shot.env_overrides.update(options.env_overrides)
    if options.routing_env is not None:
        _normalize_routing_env(one_shot, options.routing_env)
    captured_at = int(time.time())
    execution = await execute_one_shot(
        one_shot,
        cwd=options.cwd,
        provider=exec_config.provider,
        timeout_seconds=options.timeout_seconds,
        timeout_label=options.timeout_label,
    )

    if exec_config.provider == "claude":
        await _save_claude_usage(execution.stdout, exec_config.working_dir, captured_at=captured_at)

    return TaskResult(
        status=execution.status,
        result_text=execution.result_text,
        execution=execution,
    )


async def _run_claude_with_rotation(
    exec_config: TaskExecutionConfig,
    prompt: str,
    options: TaskRunOptions,
) -> TaskResult:
    """Execute the Claude one-shot path through the shared rotation driver."""
    from ductor_bot.cli.slot_rotation import (
        AttemptOutcome,
        build_fallback_prefix,
        build_unparsable_prefix,
        make_registry,
        run_with_rotation,
        slot_extra_env,
    )
    from ductor_bot.cron.execution import (
        build_cmd,
        execute_one_shot,
    )

    slot_workspace = str(Path(exec_config.working_dir) / "workspace")
    registry = make_registry(working_dir=slot_workspace)
    default_tz = _resolve_tz(exec_config.user_timezone)

    async def _attempt(slot: str | None) -> tuple[AttemptOutcome, OneShotExecutionResult | None]:
        one_shot = build_cmd(exec_config, prompt)
        if one_shot is None:
            outcome = AttemptOutcome(
                returncode=None,
                is_error=True,
                stderr="",
                result_text="",
            )
            return outcome, None

        if slot:
            for k, v in slot_extra_env(
                slot,
                working_dir=slot_workspace,
            ).items():
                one_shot.env_overrides[k] = v
        if options.ductor_home is not None:
            one_shot.env_overrides.setdefault("DUCTOR_HOME", str(options.ductor_home))
            _inject_dotenv_overrides(one_shot, options.ductor_home)
            _supply_secrets_store_path(one_shot, options.ductor_home)
        one_shot.env_overrides.update(options.env_overrides)
        if options.routing_env is not None:
            _normalize_routing_env(one_shot, options.routing_env)
        captured_at = int(time.time())
        execution = await execute_one_shot(
            one_shot,
            cwd=options.cwd,
            provider=exec_config.provider,
            timeout_seconds=options.timeout_seconds,
            timeout_label=options.timeout_label,
        )
        await _save_claude_usage(
            execution.stdout, exec_config.working_dir, slot or "claude", captured_at=captured_at
        )
        stderr_text = execution.stderr.decode(errors="replace") if execution.stderr else ""
        outcome = AttemptOutcome(
            returncode=execution.returncode,
            is_error=execution.returncode not in (0, None),
            stderr=stderr_text,
            result_text=execution.result_text,
        )
        return outcome, execution

    rotation = await run_with_rotation(
        accounts=list(exec_config.claude_accounts),
        registry=registry,
        default_tz=default_tz,
        attempt=_attempt,
    )

    execution = rotation.payload
    if execution is None:
        return TaskResult(
            status=f"error:cli_not_found_{exec_config.provider}",
            result_text=f"[{exec_config.provider} CLI not found]",
            execution=None,
        )

    if rotation.unparsable_stderr is not None:
        prefix = build_unparsable_prefix(rotation.unparsable_stderr)
        execution = replace(execution, result_text=prefix + (execution.result_text or ""))
    elif (
        rotation.first_rl_slot is not None
        and rotation.first_rl_reset is not None
        and rotation.slot is not None
        and not rotation.all_rate_limited
    ):
        prefix = build_fallback_prefix(
            rotation.first_rl_slot,
            rotation.slot,
            rotation.first_rl_reset,
            default_tz,
            reset_estimated=rotation.first_rl_reset_estimated,
        )
        execution = replace(execution, result_text=prefix + (execution.result_text or ""))

    return TaskResult(
        status=execution.status,
        result_text=execution.result_text,
        execution=execution,
    )


async def execute_in_task_folder(  # noqa: PLR0913
    observer: BaseTaskObserver,
    *,
    cron_tasks_dir: Path,
    task_folder: str,
    instruction: str,
    overrides: TaskOverrides,
    dependency: str | None,
    task_id: str,
    task_label: str,
    timeout_seconds: float,
    routing_env: dict[str, str] | None = None,
) -> TaskResult:
    """Execute a one-shot CLI task inside a ``cron_tasks`` subfolder.

    Shared core for :class:`CronObserver` and :class:`WebhookObserver`.
    Handles dependency locking, folder validation, config resolution,
    instruction enrichment, subprocess execution, and result logging.

    Caller-specific concerns (result delivery, status persistence,
    quiet-hour checks) remain with the caller.
    """
    from ductor_bot.cron.dependency_queue import get_dependency_queue
    from ductor_bot.cron.execution import enrich_instruction

    dep_queue = get_dependency_queue()

    async with dep_queue.acquire(task_id, task_label, dependency):
        folder = cron_tasks_dir / task_folder
        if not await asyncio.to_thread(folder.is_dir):
            return TaskResult(
                status="error:folder_missing",
                result_text="",
                execution=None,
            )

        exec_config = observer.resolve_execution_config(overrides)
        enriched = enrich_instruction(instruction, task_folder)

        logger.debug(
            "%s cwd=%s provider=%s model=%s timeout=%.0fs",
            task_label,
            folder,
            exec_config.provider,
            exec_config.model,
            timeout_seconds,
        )

        route = dict(routing_env or {})
        from ductor_bot.cli.executor import framework_identity_env

        route.update(framework_identity_env(observer._agent_name, observer._config.interagent_port))

        result = await run_oneshot_task(
            exec_config,
            enriched,
            TaskRunOptions(
                cwd=folder,
                timeout_seconds=timeout_seconds,
                timeout_label=task_label,
                ductor_home=observer._paths.ductor_home,
                routing_env=route,
            ),
        )

        if result.execution is not None:
            observer.log_execution_result(result, task_label, task_id)

        return result
