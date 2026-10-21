"""AgentSupervisor: manages main agent + dynamic sub-agents in a single process."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any

from ductor_bot.cli.process_registry import ProcessRegistry
from ductor_bot.config import AgentConfig, update_config_file_async
from ductor_bot.i18n import t
from ductor_bot.infra.agent_diagnostics import (
    Reason,
    create_error,
    item_label,
    name_format_error,
    safe_error,
    show_reason,
    shown_item_label,
    start_error,
    valid_sub_name,
)
from ductor_bot.infra.feature_deps import check_feature_deps, format_missing, missing_for
from ductor_bot.infra.file_watcher import FileWatcher
from ductor_bot.infra.restart import EXIT_RESTART
from ductor_bot.infra.shutdown_budget import CANCEL_FLOOR_SECONDS, ShutdownBudget, wait_capped
from ductor_bot.multiagent.health import AgentHealth
from ductor_bot.multiagent.models import SubAgentConfig, merge_sub_agent_config
from ductor_bot.multiagent.registry import AgentRegistry
from ductor_bot.multiagent.stack import AgentStack
from ductor_bot.text.response_format import show_crash_error
from ductor_bot.workspace.paths import resolve_paths

if TYPE_CHECKING:
    from ductor_bot.multiagent.bus import InterAgentBus
    from ductor_bot.multiagent.internal_api import InternalAgentAPI
    from ductor_bot.multiagent.shared_knowledge import SharedKnowledgeSync
    from ductor_bot.tasks.hub import TaskHub

logger = logging.getLogger(__name__)

REDELIVERY_SCAN_INTERVAL = 300  # seconds

_MAX_RESTART_RETRIES = 5
_RESTART_BACKOFF_BASE = 5  # seconds, doubles each retry
_MAX_BACKOFF = 60  # seconds; cap for the main agent's unbounded transient retries
_MAX_BACKOFF_SHIFT = 6  # clamp the doubling exponent so unbounded retries stay a small int


# How long a restart waits for request handlers of the old stack (its CLIs are stopped).
_GENERATION_DRAIN_SECONDS = 30.0
# A stopping sub-agent first gets this long to return from run() on its own, then to finish
# the cancellation that follows (its bot stops its own polling while it is cancelled).
_GRACEFUL_STOP_SECONDS = 5.0
_CANCEL_WAIT_SECONDS = 10.0


def _is_transient(exc: Exception) -> bool:
    """Classify an exception as a transient (retryable) network failure.

    Name-based for transport error types: the supervisor is transport-agnostic
    and must not import aiogram (or any transport SDK). Matches the transient
    branch of aiogram's error hierarchy (``TelegramNetworkError``,
    ``TelegramServerError``, both subclasses of ``TelegramAPIError``) plus the
    stdlib connection/timeout errors. Deliberately excludes fatal errors such as
    ``TelegramUnauthorizedError`` (bad token) and ``TelegramConflictError``
    (double polling), which must terminate the supervisor.
    """
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    return type(exc).__name__ in {"TelegramNetworkError", "TelegramServerError"}


def _capped_backoff(retry_count: int) -> int:
    """Exponential backoff in seconds, capped at ``_MAX_BACKOFF`` (5, 10, 20, 40, 60, 60, …).

    ``retry_count`` is 1-based and unbounded for the main agent, which retries
    transient network errors indefinitely; the exponent is clamped so the
    intermediate value never grows large.
    """
    shift = min(retry_count - 1, _MAX_BACKOFF_SHIFT)
    return min(_RESTART_BACKOFF_BASE * (1 << shift), _MAX_BACKOFF)


def _config_changed(new: AgentConfig, old: AgentConfig) -> bool:
    """Detect meaningful config changes that require agent restart."""
    if new.transport != old.transport:
        return True
    if new.transports != old.transports:
        return True
    return any(
        _TRANSPORT_IDENTITY_CHANGED.get(transport, _default_identity_check)(new, old)
        for transport in new.transports
    )


def _telegram_identity_check(new: AgentConfig, old: AgentConfig) -> bool:
    return new.telegram_token != old.telegram_token


def _matrix_identity_check(new: AgentConfig, old: AgentConfig) -> bool:
    return any(
        getattr(new.matrix, key) != getattr(old.matrix, key)
        for key in (
            "homeserver",
            "user_id",
            "password",
            "access_token",
            "device_id",
            "store_path",
        )
    )


def _slack_identity_check(new: AgentConfig, old: AgentConfig) -> bool:
    return new.slack.bot_token != old.slack.bot_token or new.slack.app_token != old.slack.app_token


def _default_identity_check(_new: AgentConfig, _old: AgentConfig) -> bool:
    return False


_IdentityCheck = Callable[[AgentConfig, AgentConfig], bool]

_TRANSPORT_IDENTITY_CHANGED: dict[str, _IdentityCheck] = {
    "telegram": _telegram_identity_check,
    "matrix": _matrix_identity_check,
    "slack": _slack_identity_check,
}


async def _wait_ended(task: asyncio.Task[Any], seconds: float) -> bool:
    """Wait up to *seconds* for *task* to end; its result or error is not raised.

    Returns whether the task has ended.
    """
    if not task.done():
        await asyncio.wait({task}, timeout=seconds)
    if not task.done():
        return False
    if not task.cancelled():
        task.exception()  # mark it retrieved
    return True


def _cancel_wait(budget: ShutdownBudget) -> float:
    """How long to wait for a cancelled task: the time left, but at least the cancel floor."""
    return min(_CANCEL_WAIT_SECONDS, max(budget.remaining(), CANCEL_FLOOR_SECONDS))


@dataclass(frozen=True, slots=True)
class StartResult:
    ok: bool
    reason: str = ""


class AgentSupervisor:
    """Manages the main agent and dynamically created sub-agents.

    Watches ``agents.json`` via FileWatcher and starts/stops sub-agents
    as entries are added or removed. Each agent runs as a supervised
    asyncio task with automatic crash recovery.
    """

    def __init__(
        self, main_config: AgentConfig, *, excluded_subs: dict[str, str] | None = None
    ) -> None:
        self._main_config = main_config
        self._excluded = dict(excluded_subs or {})
        # Sub-agents declared in agents.json: such a name that is not running is restarting.
        self._declared: set[str] = set()
        self._main_paths = resolve_paths(ductor_home=main_config.ductor_home)
        self._agents_path = self._main_paths.ductor_home / "agents.json"
        self._registry = AgentRegistry(self._agents_path)
        self._stacks: dict[str, AgentStack] = {}
        self._tasks: dict[str, asyncio.Task[int]] = {}
        self._health: dict[str, AgentHealth] = {}
        self._watcher = FileWatcher(self._agents_path, self._on_agents_changed)
        self._running = False
        self._main_done: asyncio.Event = asyncio.Event()
        self._main_ready: asyncio.Event = asyncio.Event()
        self._main_generation = 0
        self._main_ready_gen: int | None = None
        self._main_fatal = False
        # Serializes every sub-agent start, stop, restart and rebuild. Functions named
        # ``*_locked`` (and ``_start_sub_agent``) expect the caller to hold it and never call
        # a public lifecycle method; a task never waits for its own completion while holding it.
        self._agents_lock = asyncio.Lock()
        self._stopping = False
        # Sub-agents whose stop is in progress: their supervised task ends without taking the
        # lifecycle lock (the stopper holds it while waiting for that task).
        self._stopping_names: set[str] = set()
        self._redelivery_tasks: dict[str, asyncio.Task[None]] = {}
        self._redelivery_stopping = False
        self._redelivery_pending: dict[str, bool] = {}
        self._redelivery_scan_task: asyncio.Task[None] | None = None
        # Per agent: the task that tells its parent about tasks a restart cut off.
        self._notice_tasks: dict[str, asyncio.Task[int]] = {}

        # Bus, internal API, shared knowledge, task hub — created lazily in start()
        self._bus: InterAgentBus | None = None
        self._internal_api: InternalAgentAPI | None = None
        self._shared_knowledge: SharedKnowledgeSync | None = None
        self._task_hub: TaskHub | None = None

    @property
    def stacks(self) -> dict[str, AgentStack]:
        return self._stacks

    @property
    def health(self) -> dict[str, AgentHealth]:
        return self._health

    @property
    def bus(self) -> InterAgentBus | None:
        return self._bus

    def is_delivery_ready(self, handler_key: str) -> bool:
        """Whether the current agent stack can accept an async final result."""
        if handler_key == "main":
            return (
                not self._main_fatal
                and self._main_generation > 0
                and self._main_ready_gen == self._main_generation
            )
        stack = self._stacks.get(handler_key)
        health = self._health.get(handler_key)
        return (
            stack is not None
            and health is not None
            and health.status == "running"
            and stack.bot.orchestrator is not None
            and stack.config.transport != "headless"
        )

    async def start(self) -> int:
        """Start main agent + all sub-agents. Blocks until main agent exits."""
        self._running = True
        self._redelivery_stopping = False

        # Initialize inter-agent bus
        from ductor_bot.multiagent.bus import InterAgentBus
        from ductor_bot.multiagent.internal_api import InternalAgentAPI

        self._bus = InterAgentBus(
            self._main_paths.ductor_home,
            max_parallel_sessions=self._main_config.interagent.max_parallel_sessions,
            max_queue_wait_seconds=self._main_config.interagent.max_queue_wait_seconds,
        )
        self._wire_bus_checkers(self._bus)
        self._internal_api = InternalAgentAPI(
            self._bus,
            port=self._main_config.interagent_port,
        )
        self._internal_api.set_health_ref(self._health)
        started = await self._internal_api.start()
        if not started:
            msg = "Internal agent API failed to start"
            raise RuntimeError(msg)
        logger.info("InterAgentBus and internal API started")

        # Initialize task hub (background task delegation)
        if self._main_config.tasks.enabled:
            from ductor_bot.tasks.hub import TaskHub
            from ductor_bot.tasks.registry import TaskRegistry

            registry = TaskRegistry(
                registry_path=self._main_paths.tasks_registry_path,
                tasks_dir=self._main_paths.tasks_dir,
            )
            # #92: shared fallback ProcessRegistry. In practice every agent's
            # CLIService holds its OWN registry (see ``Orchestrator.__init__``),
            # so per-agent registries wired in ``_wire_task_hub`` take precedence.
            # The shared fallback is kept for test setups and single-agent deploys.
            self._task_process_registry = ProcessRegistry()
            self._task_hub = TaskHub(
                registry,
                self._main_paths,
                cli_service=None,  # Set per-agent in _post_startup
                config=self._main_config.tasks,
                user_timezone=lambda: self._main_config.user_timezone,
                append_system_prompt_files=self._main_config.append_system_prompt_files,
                claude_system_prompt_snapshot=self._main_config.claude_system_prompt_snapshot,
                process_registry=self._task_process_registry,
            )
            self._internal_api.set_task_hub(self._task_hub)
            logger.info(
                "TaskHub initialized (max_parallel=%d)", self._main_config.tasks.max_parallel
            )

        # 1. Start main agent
        main_stack = await AgentStack.create(
            "main",
            self._main_config,
            is_main=True,
        )
        self._stacks["main"] = main_stack
        self._main_generation += 1
        self._main_ready_gen = None
        self._health["main"] = AgentHealth(name="main")
        self._bus.register("main", main_stack)
        self._bus.set_async_result_handler("main", main_stack.bot.on_async_interagent_result)

        self._tasks["main"] = asyncio.create_task(
            self._supervised_run("main", main_stack),
            name="agent:main",
        )

        # 2. Wait for main agent startup before starting sub-agents.
        startup_timeout = 120
        try:
            await asyncio.wait_for(self._main_ready.wait(), timeout=startup_timeout)
        except TimeoutError:
            logger.warning(
                "Main agent startup timed out after %ds, starting sub-agents anyway",
                startup_timeout,
            )

        # 3. Load and start sub-agents from agents.json
        await self._sync_sub_agents()

        # 4. Start shared knowledge sync (SHAREDMEMORY.md → all agents)
        from ductor_bot.multiagent.shared_knowledge import SharedKnowledgeSync

        shared_path = self._main_paths.ductor_home / "SHAREDMEMORY.md"
        self._shared_knowledge = SharedKnowledgeSync(shared_path, self)
        await self._shared_knowledge.start()

        # 5. Start FileWatcher for agents.json
        await self._watcher.start()

        self._redelivery_scan_task = asyncio.create_task(
            self._scan_redeliveries(),
            name="ia-redelivery-scan",
        )

        # 6. Wait for main agent to finish — it determines the exit code
        await self._main_done.wait()
        main_task = self._tasks.get("main")
        exit_code = 0
        if main_task and main_task.done():
            try:
                exit_code = main_task.result()
            except (asyncio.CancelledError, Exception):
                exit_code = 1

        return exit_code

    def _finish_agent(self, name: str, health: AgentHealth) -> None:
        """Mark an agent as stopped and signal main-done if it is the main agent."""
        health.mark_stopped()
        if name == "main":
            self._main_ready_gen = None
            self._main_done.set()

    async def _handle_restart_exit(
        self, name: str, stack: AgentStack, health: AgentHealth
    ) -> tuple[AgentStack, bool]:
        """Handle EXIT_RESTART for an agent.

        Returns ``(stack, should_return)`` — when *should_return* is True the
        caller must ``return EXIT_RESTART``.
        """
        if name == "main":
            logger.info("Main agent requested full service restart")
            self.begin_shutdown()
            self._finish_agent(name, health)
            return stack, True

        # Sub-agent: in-process hot-reload (rebuild stack only)
        logger.info("Sub-agent '%s' requested restart (hot-reload)", name)
        # Waiting for the lock is cancellable: a stop or restart that gets it first cancels this
        # task instead of waiting for it, so the old stack is never run again.
        async with self._agents_lock:
            self.begin_shutdown(name)
            self._mark_stopping(name)
            health.mark_starting()
            new_stack = await self._replace_stack(name, stack)
        return new_stack, False

    async def _replace_stack(self, name: str, stack: AgentStack) -> AgentStack:
        """Shut a stack down, let its handlers finish and build the next one (lock held)."""
        budget = ShutdownBudget()
        try:
            await stack.shutdown(budget)
        except Exception as exc:
            logger.warning("Agent '%s': old stack shutdown failed (%s)", name, type(exc).__name__)
        await self._retire_stack(name, stack, budget)
        return await self._rebuild_stack(name, stack)

    async def _handle_crash(
        self,
        name: str,
        stack: AgentStack,
        health: AgentHealth,
        retry_count: int,
        exc: Exception,
    ) -> tuple[AgentStack, int, bool]:
        """Handle a crash in ``_supervised_run``.

        Returns ``(stack, retry_count, should_return)`` — when *should_return*
        is True the caller must ``return 1``.

        The main agent terminates the supervisor on fatal errors, but retries
        transient network failures (see ``_is_transient``) indefinitely with
        capped backoff — an early-startup network blip must not take down the
        whole service. Sub-agents retry any crash up to ``_MAX_RESTART_RETRIES``
        times.
        """
        error_msg = f"{type(exc).__name__}: {exc}"
        health.mark_crashed(error_msg)

        if name == "main":
            self._main_ready_gen = None
            if not _is_transient(exc):
                self._main_fatal = True
                logger.error(
                    "Main agent crashed (fatal), terminating supervisor: %s",
                    error_msg,
                    exc_info=sys.exc_info(),
                )
                self._main_ready.set()  # unblock sub-agent startup if still waiting
                self._main_done.set()
                return stack, retry_count, True

            logger.warning(
                "Main agent crashed (transient, attempt %d): %s — retrying",
                retry_count,
                error_msg,
            )
            stack = await self._backoff_and_rebuild(
                name, stack, health, _capped_backoff(retry_count)
            )
            return stack, retry_count, False

        logger.error(
            "Agent '%s' crashed (attempt %d/%d): %s",
            name,
            retry_count,
            _MAX_RESTART_RETRIES,
            error_msg,
            exc_info=sys.exc_info(),
        )

        if retry_count > _MAX_RESTART_RETRIES:
            logger.error(
                "Agent '%s' exceeded max retries (%d), giving up",
                name,
                _MAX_RESTART_RETRIES,
                exc_info=sys.exc_info(),
            )
            await self._notify_main_agent(
                t(
                    "supervisor.stopped_after_crashes",
                    name=name,
                    max=_MAX_RESTART_RETRIES,
                    error=show_crash_error(error_msg),
                )
            )
            return stack, retry_count, True

        wait = _RESTART_BACKOFF_BASE * (2 ** (retry_count - 1))
        stack = await self._backoff_and_rebuild(name, stack, health, wait)
        return stack, retry_count, False

    async def _backoff_and_rebuild(
        self, name: str, stack: AgentStack, health: AgentHealth, wait: int
    ) -> AgentStack:
        """Sleep for *wait* seconds, then shut down and rebuild the agent stack.

        Shared by the sub-agent recovery path and the main-agent transient-retry
        path. Rebuild failures are logged and swallowed so the supervised loop
        retries again on its next iteration.
        """
        logger.info("Agent '%s' restarting in %ds", name, wait)
        await asyncio.sleep(wait)

        try:
            if name == "main":
                self._mark_stopping(name)
                stack = await self._replace_stack(name, stack)
            else:
                async with self._agents_lock:
                    self._mark_stopping(name)
                    stack = await self._replace_stack(name, stack)
            health.mark_starting()
        except Exception:
            logger.exception("Failed to rebuild agent '%s'", name)

        return stack

    async def _supervised_run(self, name: str, stack: AgentStack) -> int:
        """Run an agent with automatic crash recovery.

        On crash:
          - Sub-agents: retry with exponential backoff (5s, 10s, 20s, 40s, 80s);
            give up after ``_MAX_RESTART_RETRIES`` consecutive failures.
          - Main agent: fatal errors terminate the supervisor; transient network
            errors (see ``_is_transient``) retry indefinitely with capped backoff.
        On clean exit: return the exit code.
        On restart request (exit code 42):
          - Main agent: propagate EXIT_RESTART to trigger full service restart.
          - Sub-agents: rebuild stack in-process (hot-reload).
        """
        from ductor_bot.log_context import set_log_context

        set_log_context(agent_name=name)
        health = self._health[name]
        health.mark_starting()
        retry_count = 0

        while self._running:
            try:
                self._inject_supervisor_hook(stack)
                health.mark_running()
                logger.info("Agent '%s' running", name)
                exit_code = await stack.run()

                if name in self._stopping_names:
                    logger.info("Agent '%s' ended while being stopped (code=%d)", name, exit_code)
                    health.mark_stopped()
                    return exit_code

                if exit_code == EXIT_RESTART:
                    stack, should_return = await self._handle_restart_exit(name, stack, health)
                    if should_return:
                        return EXIT_RESTART
                    retry_count = 0
                    continue

                # Clean exit
                logger.info("Agent '%s' exited cleanly (code=%d)", name, exit_code)
                self._finish_agent(name, health)

            except asyncio.CancelledError:
                logger.info("Agent '%s' cancelled", name)
                health.mark_stopped()
                raise

            except Exception as exc:
                if name in self._stopping_names:
                    logger.warning(
                        "Agent '%s' failed while being stopped (%s)", name, type(exc).__name__
                    )
                    health.mark_stopped()
                    return 1
                retry_count += 1
                stack, retry_count, should_return = await self._handle_crash(
                    name,
                    stack,
                    health,
                    retry_count,
                    exc,
                )
                if should_return:
                    return 1
                continue

            else:
                return exit_code

        self._finish_agent(name, health)
        return 0

    def _inject_supervisor_hook(self, stack: AgentStack) -> None:
        """Register a dispatcher startup hook to inject supervisor reference.

        The orchestrator is created during TelegramBot._on_startup(). We register
        an additional startup handler that fires AFTER _on_startup and sets the
        supervisor reference + registers multi-agent commands on the main agent.

        For the main agent this also signals ``_main_ready`` so the supervisor
        knows Docker and workspace init are complete before starting sub-agents.
        """
        supervisor = self

        async def _post_startup() -> None:
            orch = stack.bot.orchestrator
            if orch is None:
                return
            orch.supervisor = supervisor
            if supervisor._bus is not None:
                # Manual @ia.… resumes share the bus-owned session gates.
                orch.set_session_gates(supervisor._bus.gates)
            if stack.is_main and stack is supervisor._stacks.get("main"):
                orch.register_multiagent_commands()
                stack.bot.set_abort_all_callback(supervisor.abort_all_agents)
                message_bus = getattr(stack.bot, "_bus", None)
                if message_bus is not None and message_bus._injector is orch:
                    supervisor._main_ready_gen = supervisor._main_generation
                    supervisor._main_ready.set()
                else:
                    logger.error(
                        "Main agent injector not wired for generation=%s",
                        supervisor._main_generation,
                    )

            # Wire task hub: set CLI service and register handlers
            if supervisor._task_hub is not None:
                supervisor._wire_task_hub(stack)
                supervisor._report_interrupted_tasks(stack)  # after the result handler is wired

            if supervisor._bus is not None and supervisor.is_delivery_ready(stack.name):
                supervisor._start_redelivery(stack.name)

            logger.debug("Supervisor reference injected into agent '%s'", stack.name)

        # Startup handlers run in registration order;
        # TelegramBot registers _on_startup in __init__, so ours runs after.
        stack.bot.register_startup_hook(_post_startup)

    async def _scan_redeliveries(self) -> None:
        """Replay ready targets periodically, including records held only in memory."""
        while self._running:
            await asyncio.sleep(REDELIVERY_SCAN_INTERVAL)
            if not self._running:
                break  # type: ignore[unreachable]  # state can change across the await
            for target in {"main", *self._stacks}:
                if self.is_delivery_ready(target):
                    self._start_redelivery(target, force=False)

    def _start_redelivery(self, target: str, *, force: bool = True) -> None:
        """Schedule one replay per target; readiness events override periodic backoff."""
        if self._redelivery_stopping:
            return
        active = self._redelivery_tasks.get(target)
        if active is not None and not active.done():
            self._redelivery_pending[target] = self._redelivery_pending.get(target, False) or force
            return
        self._redelivery_pending[target] = force
        self._redelivery_tasks[target] = asyncio.create_task(
            self._redeliver_target(target),
            name=f"ia-redelivery:{target}",
        )

    async def _redeliver_target(self, target: str) -> None:
        try:
            while True:
                force = self._redelivery_pending.pop(target)
                if self._bus is not None:
                    newly_parked: set[str] = set()
                    # Scan active records first so parked delivery retries cannot
                    # delay restart reports. Each scan must survive the other's error.
                    try:
                        await self._bus.recover_inflight(
                            target,
                            newly_parked=newly_parked,
                            respect_backoff=not force,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.error(  # noqa: TRY400
                            "Inflight async recovery failed target=%s error=%s",
                            target,
                            type(exc).__name__,
                        )
                    try:
                        # Newly parked tasks already exhausted this cycle's retries.
                        await self._bus.redeliver_undelivered(
                            target,
                            exclude_task_ids=newly_parked,
                            respect_backoff=not force,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.error(  # noqa: TRY400
                            "Parked async redelivery failed target=%s error=%s",
                            target,
                            type(exc).__name__,
                        )
                if target not in self._redelivery_pending:
                    break
        finally:
            self._redelivery_pending.pop(target, None)
            if self._redelivery_tasks.get(target) is asyncio.current_task():
                self._redelivery_tasks.pop(target, None)

    async def _cancel_redeliveries(self, target: str | None = None) -> None:
        if target is None:
            tasks = list(self._redelivery_tasks.items())
        else:
            task = self._redelivery_tasks.get(target)
            tasks = [(target, task)] if task is not None else []
        for _, task in tasks:
            task.cancel()
        for name, task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
            # A task cancelled before its coroutine starts never runs its finally block.
            if self._redelivery_tasks.get(name) is task:
                self._redelivery_tasks.pop(name, None)
                self._redelivery_pending.pop(name, None)

    def _forget_notice(self, name: str, done: asyncio.Task[int]) -> None:
        if self._notice_tasks.get(name) is done:
            del self._notice_tasks[name]

    def _cancel_notice(self, name: str) -> None:
        task = self._notice_tasks.pop(name, None)
        if task is not None:
            task.cancel()

    def _report_interrupted_tasks(self, stack: AgentStack) -> None:
        """Start telling the agent's parent about tasks a restart cut off (not awaited).

        The notice runs a turn in the parent session and may wait for it, so it must not
        hold up the start.
        """
        hub = self._task_hub
        name = stack.name
        if hub is None or self._stopping or not hub.has_result_handler(name):
            return  # not wired yet: the notice needs the parent's result path
        if not hub.registry.pending_notices(name):
            return
        previous = self._notice_tasks.pop(name, None)
        if previous is not None:
            previous.cancel()
        task = asyncio.create_task(
            hub.report_interrupted(name, stack.bot.notify_task_interrupted),
            name=f"task-notice:{name}",
        )
        self._notice_tasks[name] = task
        task.add_done_callback(partial(self._forget_notice, name))

    def _wire_task_hub(self, stack: AgentStack) -> None:
        """Wire task hub handlers for an agent stack.

        Called after the orchestrator is initialized (in _post_startup).
        Registers the agent's CLI service and per-agent result/question handlers.
        """
        hub = self._task_hub
        if hub is None:
            return

        orch = stack.bot.orchestrator
        if orch is None:
            return

        name = stack.name
        orch.set_task_hub(hub)

        # Register this agent's CLI service and workspace paths for task execution
        hub.set_cli_service(name, orch.cli_service, user_timezone=lambda: orch.config.user_timezone)
        hub.set_agent_paths(name, stack.paths)
        hub.set_agent_snapshot_mode(name, stack.config.claude_system_prompt_snapshot)
        # #92: register this agent's ProcessRegistry so TaskHub.cancel routes
        # kill_for_task to the registry where task:<id> subprocesses actually
        # live (each orchestrator owns its own — see Orchestrator.__init__).
        hub.set_agent_process_registry(name, orch.process_registry)

        hub.set_result_handler(name, stack.bot.on_task_result)
        hub.set_question_handler(name, stack.bot.on_task_question)
        hub.set_task_question_answerer(name, orch.answer_task_question)

        async def mark_tasks_interrupted() -> None:
            hub.begin_shutdown(name)  # before the stop kills this agent's CLI processes

        orch.register_pre_shutdown_hook(mark_tasks_interrupted)

        # Register agent's primary chat_id for resolving CLI-submitted tasks
        if stack.config.allowed_user_ids:
            hub.set_agent_chat_id(name, stack.config.allowed_user_ids[0])

        logger.debug("Task hub wired for agent '%s'", name)

    async def _rebuild_stack(self, name: str, old_stack: AgentStack) -> AgentStack:
        """Rebuild an AgentStack from its config."""
        new_stack = await AgentStack.create(
            name,
            old_stack.config,
            is_main=old_stack.is_main,
        )
        self._stacks[name] = new_stack
        if name == "main":
            self._main_generation += 1
            self._main_ready_gen = None
        if self._bus:
            self._bus.register(name, new_stack)
            self._bus.set_async_result_handler(name, new_stack.bot.on_async_interagent_result)
        return new_stack

    # -- Sub-agent lifecycle ------------------------------------------------

    async def _sync_sub_agents(self) -> None:
        """Load agents.json and start any sub-agents not yet running."""
        async with self._agents_lock:
            sub_agents = self._registry.load()
            self._declared = {a.name for a in sub_agents}
            for name, reason in self._excluded.items():
                await self._report_excluded(name, reason)
            for sub_cfg in sub_agents:
                if sub_cfg.name not in self._stacks and sub_cfg.name not in self._excluded:
                    await self._start_sub_agent(sub_cfg)

    def _display_name(self, name: str, reason: str = "", *, shown: bool = False) -> str:
        """Name of a sub-agent in logs (English) or, with *shown*, in a message to a person."""
        label = shown_item_label if shown else item_label
        if isinstance(reason, Reason):
            known, index = reason.entry()
            if known:
                return label(index)
        if valid_sub_name(name):
            return name
        return label(self._registry.index_for(name))

    async def _report_excluded(self, name: str, reason: str) -> None:
        logger.error("Sub-agent '%s' excluded: %s", self._display_name(name, reason), reason)
        display = self._display_name(name, reason, shown=True)
        await self._notify_main_agent(
            t("supervisor.excluded", name=display, reason=show_reason(reason))
        )

    async def _exclude_sub(self, name: str, reason: str) -> StartResult:
        if self._excluded.get(name) != reason:
            self._excluded[name] = reason
            await self._report_excluded(name, reason)
        return StartResult(ok=False, reason=reason)

    async def _start_sub_agent(self, sub_cfg: SubAgentConfig) -> StartResult:
        """Create and start a sub-agent, reporting the actual outcome (lock held).

        A name that is already running is not started a second time, and nothing starts once
        ``stop_all()`` began.
        """
        if self._stopping:
            return StartResult(ok=False, reason="supervisor is shutting down")
        if sub_cfg.name in self._stacks:
            return StartResult(ok=False, reason="already running")
        return await self._create_sub_agent(sub_cfg)

    async def _create_sub_agent(self, sub_cfg: SubAgentConfig) -> StartResult:
        name = sub_cfg.name
        if not valid_sub_name(name):
            return await self._exclude_sub(name, name_format_error(self._registry.index_for(name)))

        agent_home = self._main_paths.ductor_home / "agents" / name
        try:
            config = merge_sub_agent_config(self._main_config, sub_cfg, agent_home)
        except Exception as exc:
            reason = safe_error(self._registry.index_for(name), exc)
            return await self._exclude_sub(name, reason)

        missing = missing_for(config)
        if missing:
            reason = format_missing(missing, sub=name)
            return await self._exclude_sub(name, reason)

        try:
            stack = await AgentStack.create(name, config)
        except Exception as exc:
            return await self._exclude_sub(name, create_error(exc))

        try:
            # Workspace init creates config.json from config.example (main defaults).
            # Overwrite model/provider/effort so the on-disk config matches agents.json.
            config_path = agent_home / "config" / "config.json"
            if config_path.exists():
                await update_config_file_async(
                    config_path,
                    provider=config.provider,
                    model=config.model,
                    reasoning_effort=config.reasoning_effort,
                    memory_lint=config.memory_lint.model_dump(mode="json"),
                    interagent=config.interagent.model_dump(mode="json"),
                    claude_system_prompt_snapshot=config.claude_system_prompt_snapshot,
                )

            self._stacks[name] = stack
            self._health[name] = AgentHealth(name=name)
            if self._bus:
                self._bus.register(name, stack)
                self._bus.set_async_result_handler(name, stack.bot.on_async_interagent_result)

            # A fresh context: the log context of whoever started the agent is not inherited.
            self._tasks[name] = asyncio.create_task(
                self._supervised_run(name, stack),
                name=f"agent:{name}",
                context=contextvars.Context(),
            )

            # Sync shared knowledge into the new agent's MAINMEMORY.md
            if self._shared_knowledge:
                await self._shared_knowledge.sync_agent(stack.paths.mainmemory_path)
        except Exception as exc:
            await self._clean_up_failed_start(name, stack)
            return await self._exclude_sub(name, start_error(exc))

        self._excluded.pop(name, None)
        logger.info("Sub-agent '%s' started (home=%s)", name, agent_home)
        return StartResult(ok=True)

    async def _clean_up_failed_start(self, name: str, stack: AgentStack) -> None:
        """Undo a start that failed after the stack was created (lock held)."""
        if self._stacks.get(name) is stack:
            await self._stop_agent_locked(name)
            return
        try:
            await stack.shutdown()
        except Exception as exc:
            logger.warning(
                "Agent '%s': shutdown after a failed start failed (%s)", name, type(exc).__name__
            )

    def _wire_bus_checkers(self, bus: InterAgentBus) -> None:
        bus.set_delivery_ready_checker(self.is_delivery_ready)
        # Lets waiting requests tell a restarting recipient from one that is gone for good.
        bus.set_agent_expected_checker(self.is_agent_expected)

    def begin_shutdown(self, name: str | None = None) -> None:
        """Mark the agents' orchestrators as shutting down (all when *name* is None).

        Call before any task cancel or CLI kill: a foreground turn cut off afterwards keeps
        its in-flight record for the next start's notice.
        """
        from ductor_bot.orchestrator.lifecycle import begin_shutdown

        for agent, stack in list(self._stacks.items()):
            orch = stack.bot.orchestrator
            if orch is not None and name in (None, agent):
                begin_shutdown(orch)

    async def _retire_stack(
        self, name: str, stack: AgentStack, budget: ShutdownBudget | None = None
    ) -> None:
        """Let a shut-down stack's request handlers finish, then close its Named registry.

        Nothing of the old generation may write the next one's state. The stack is already unavailable (``_mark_stopping``) and its CLIs are stopped, so the
        handlers end quickly; one that does not is left behind and can no longer write.
        """
        drain = _GENERATION_DRAIN_SECONDS
        if budget is not None:
            drain = budget.cap(drain)
        if self._bus is not None and not await self._bus.drain_agent(name, drain):
            logger.warning(
                "Agent '%s': request handlers of the old stack still running after %.0fs; "
                "their later session writes are dropped",
                name,
                drain,
            )
        orch = stack.bot.orchestrator
        if orch is not None:
            orch.named_sessions.close()

    def _mark_stopping(self, name: str) -> None:
        """Requests waiting for this agent must not pick the stack that is shutting down."""
        if self._bus:
            self._bus.mark_unavailable(name)

    def is_agent_expected(self, name: str) -> bool:
        """Whether a missing agent is coming back (restart in progress) rather than gone."""
        if name == "main" or name in self._stacks:
            return True
        return name in self._declared and name not in self._excluded

    async def stop_agent(self, name: str) -> None:
        """Stop a sub-agent gracefully."""
        if name == "main":
            logger.warning("Cannot stop main agent via stop_agent()")
            return
        async with self._agents_lock:
            await self._stop_agent_locked(name)

    async def _end_agent_task(
        self, name: str, task: asyncio.Task[int], stack: AgentStack | None, budget: ShutdownBudget
    ) -> None:
        """End a sub-agent's supervised task: ask it to return, then cancel it (all waits capped).

        A bot that can stop on request returns from ``run()`` by itself; the others, and one
        that does not return in time, are cancelled. A cancelled bot stops its own polling
        before the cancellation reaches this task, so the wait after the cancel is bounded too.
        """
        graceful = False
        if stack is not None:
            try:
                graceful = stack.bot.request_stop() is True
            except Exception as exc:
                logger.warning("Agent '%s': stop request failed (%s)", name, type(exc).__name__)
        if graceful:
            await _wait_ended(task, budget.cap(_GRACEFUL_STOP_SECONDS))
        if not task.done():
            task.cancel()
            if not await _wait_ended(task, _cancel_wait(budget)):
                logger.warning("Agent '%s': task did not end within the shutdown budget", name)

    async def _stop_agent_locked(self, name: str, budget: ShutdownBudget | None = None) -> None:
        """Stop a sub-agent: end its task, shut the stack down (lock held).

        *budget* is the shared shutdown budget when several agents are stopped one after
        another; a single stop gets its own.
        """
        budget = budget or ShutdownBudget()
        self.begin_shutdown(name)
        self._mark_stopping(name)
        await self._cancel_redeliveries(name)
        self._cancel_notice(name)
        self._stopping_names.add(name)
        try:
            task = self._tasks.pop(name, None)
            if task and not task.done():
                await self._end_agent_task(name, task, self._stacks.get(name), budget)
        finally:
            self._stopping_names.discard(name)

        stack = self._stacks.pop(name, None)
        if stack:
            try:
                await stack.shutdown(budget)
            except Exception as exc:
                logger.warning("Agent '%s': stack shutdown failed (%s)", name, type(exc).__name__)
            await self._retire_stack(name, stack, budget)

        if self._bus:
            self._bus.unregister(name)

        health = self._health.get(name)
        if health:
            health.mark_stopped()

        logger.info("Sub-agent '%s' stopped", name)

    async def _start_registered_agent(self, sub_cfg: SubAgentConfig) -> StartResult:
        """Apply the raw agents.json dependency check before a manual start."""
        reason = check_feature_deps(self._main_config, self._main_paths).get(sub_cfg.name)
        if reason:
            return await self._exclude_sub(sub_cfg.name, reason)
        return await self._start_sub_agent(sub_cfg)

    async def start_agent_by_name(self, name: str) -> str:
        """Start a sub-agent by name from the registry. Returns status message."""
        async with self._agents_lock:
            if name in self._stacks:
                return t("supervisor.already_running", name=name)

            agents = self._registry.load()
            match = next((a for a in agents if a.name == name), None)
            if match is None:
                if reason := self._excluded.get(name):
                    return t("supervisor.not_started", name=name, reason=show_reason(reason))
                return t("supervisor.not_found", name=name)

            result = await self._start_registered_agent(match)
        if result.ok:
            return t("supervisor.started", name=name)
        return t("supervisor.not_started", name=name, reason=show_reason(result.reason))

    async def restart_agent(self, name: str) -> str:
        """Restart a sub-agent (stop + start). Returns status message."""
        if name == "main":
            return t("supervisor.cannot_restart_main")

        async with self._agents_lock:
            agents = self._registry.load()
            match = next((a for a in agents if a.name == name), None)
            if match is None:
                if reason := self._excluded.get(name):
                    return t("supervisor.not_started", name=name, reason=show_reason(reason))
                return t("supervisor.not_found", name=name)

            if name in self._stacks:
                await self._stop_agent_locked(name)

            result = await self._start_registered_agent(match)
        if result.ok:
            return t("supervisor.restarted", name=name)
        return t("supervisor.not_started", name=name, reason=show_reason(result.reason))

    # -- FileWatcher callback -----------------------------------------------

    async def _on_agents_changed(self) -> None:
        """Called when agents.json mtime changes. Sync running agents."""
        async with self._agents_lock:
            desired = {a.name: a for a in self._registry.load()}
            current_sub = set(self._stacks.keys()) - {"main"}
            desired_names = set(desired.keys())
            self._declared = set(desired_names)
            prechecked = check_feature_deps(self._main_config, self._main_paths)
            for name, reason in prechecked.items():
                if name in current_sub:
                    await self._stop_agent_locked(name)
                    current_sub.remove(name)
                await self._exclude_sub(name, reason)
            for name in set(self._excluded) - desired_names - set(prechecked):
                self._excluded.pop(name, None)

            # Recheck stopped agents against the current declaration and imports.
            for name in desired_names - current_sub:
                if name in prechecked:
                    continue
                logger.info(
                    "agents.json: new agent '%s' detected, starting",
                    item_label(self._registry.index_for(name)),
                )
                await self._start_sub_agent(desired[name])

            # Stop removed agents
            for name in current_sub - desired_names:
                logger.info("agents.json: agent '%s' removed, stopping", name)
                await self._stop_agent_locked(name)

            # Check for config changes on existing agents
            for name in desired_names & current_sub:
                sub_cfg = desired[name]
                existing = self._stacks.get(name)
                if existing is None:
                    continue

                await self._restart_changed_agent(name, sub_cfg, existing)

    async def _restart_changed_agent(
        self, name: str, sub_cfg: SubAgentConfig, existing: AgentStack
    ) -> None:
        # Rebuild config and compare credentials
        agent_home = self._main_paths.ductor_home / "agents" / name
        try:
            new_config = merge_sub_agent_config(self._main_config, sub_cfg, agent_home)
        except Exception:
            await self._stop_agent_locked(name)
            await self._start_sub_agent(sub_cfg)
            return
        if _config_changed(new_config, existing.config):
            logger.info("agents.json: agent '%s' config changed, restarting", name)
            await self._stop_agent_locked(name)
            await self._start_sub_agent(sub_cfg)

    # -- Notifications ------------------------------------------------------

    async def _notify_main_agent(self, message: str) -> None:
        """Send a system notification to the main agent's users/rooms."""
        main = self._stacks.get("main")
        if main is None:
            return
        try:
            await main.bot.notification_service.notify_all(
                f"**[{t('supervisor.label')}]** {message}"
            )
        except Exception:
            logger.exception("Failed to notify main agent")

    # -- Abort all ----------------------------------------------------------

    async def abort_all_agents(self) -> int:
        """Kill active CLI processes on ALL agent stacks (without stopping agents).

        Called by the main bot's ``/stop_all`` handler via callback.
        Returns total number of killed processes across all agents.
        """
        killed = 0
        for name, stack in list(self._stacks.items()):
            orch = stack.bot.orchestrator
            if orch is None:
                continue
            try:
                k = await orch.process_registry.kill_all_active()
                if orch.bg_observer:
                    chat_ids = {t.chat_id for t in orch.bg_observer._tasks.values()}
                    for cid in chat_ids:
                        k += await orch.bg_observer.cancel_all(cid)
                if k:
                    logger.info("Abort-all killed %d process(es) on agent '%s'", k, name)
                killed += k
            except Exception:
                logger.exception("Error aborting processes on agent '%s'", name)

        # Cancel in-flight async inter-agent tasks on the bus
        if self._bus:
            cancelled = await self._bus.cancel_all_async()
            if cancelled:
                logger.info("Abort-all cancelled %d async inter-agent task(s)", cancelled)
            killed += cancelled

        # Cancel in-flight background tasks
        killed += await self._abort_all_tasks()

        return killed

    async def _abort_all_tasks(self) -> int:
        """Cancel all in-flight background tasks across all agents."""
        if self._task_hub is None:
            return 0
        total = 0
        for stack in list(self._stacks.values()):
            for cid in stack.config.allowed_user_ids:
                k = await self._task_hub.cancel_all(cid)
                if k:
                    logger.info("Abort-all cancelled %d task(s) for agent '%s'", k, stack.name)
                total += k
        return total

    # -- Shutdown -----------------------------------------------------------

    async def _stop_background_services(self, budget: ShutdownBudget) -> None:
        """Stop what feeds the agents (replay scan, file watcher, shared knowledge)."""
        for name in list(self._notice_tasks):
            self._cancel_notice(name)
        if self._redelivery_scan_task is not None:
            self._redelivery_scan_task.cancel()
            await _wait_ended(self._redelivery_scan_task, _cancel_wait(budget))
            self._redelivery_scan_task = None

        await wait_capped(self._watcher.stop(), budget.remaining(), label="agents watcher stop")
        if self._shared_knowledge:
            await wait_capped(
                self._shared_knowledge.stop(), budget.remaining(), label="shared knowledge stop"
            )

        # Replays may be sleeping between retries; cancellation restores their files.
        await wait_capped(self._cancel_redeliveries(), budget.remaining(), label="redelivery stop")

    async def _acquire_lock_capped(self, budget: ShutdownBudget) -> bool:
        """Take the lifecycle lock, waiting at most the time left. False: go on without it."""
        try:
            await asyncio.wait_for(self._agents_lock.acquire(), budget.remaining())
        except TimeoutError:
            logger.warning("Lifecycle lock still held at shutdown; stopping the agents without it")
            return False
        return True

    async def stop_all(self) -> None:  # noqa: C901
        """Shut down all agents and cleanup within one shutdown budget."""
        budget = ShutdownBudget()
        self.begin_shutdown()
        self._running = False
        self._stopping = True
        self._redelivery_stopping = True
        await self._stop_background_services(budget)

        # Cancel in-flight async tasks before tearing down agents
        if self._bus:
            cancelled = await self._bus.cancel_all_async()
            if cancelled:
                logger.warning("Cancelled %d in-flight async inter-agent task(s)", cancelled)

        # Stop sub-agents first, then main. The lock is taken once and the names are read under
        # it: a restart that is in progress finishes first, so no new stack outlives this.
        locked = await self._acquire_lock_capped(budget)
        try:
            for name in [n for n in list(self._stacks.keys()) if n != "main"]:
                await self._stop_agent_locked(name, budget)
        finally:
            if locked:
                self._agents_lock.release()

        # Stop main
        main_task = self._tasks.pop("main", None)
        if main_task and not main_task.done():
            main_task.cancel()
            if not await _wait_ended(main_task, _cancel_wait(budget)):
                logger.warning("Main task did not end within the shutdown budget")

        main_stack = self._stacks.pop("main", None)
        if main_stack:
            try:
                await main_stack.shutdown(budget)
            except Exception as exc:
                logger.warning("Main stack shutdown failed (%s)", type(exc).__name__)

        if self._bus:
            self._bus.unregister("main")

        # Stop task hub
        if self._task_hub:
            await self._task_hub.shutdown(budget)

        # Stop internal API
        if self._internal_api:
            await wait_capped(
                self._internal_api.stop(), budget.remaining(), label="internal api stop"
            )

        logger.info("AgentSupervisor stopped (all agents shut down)")
