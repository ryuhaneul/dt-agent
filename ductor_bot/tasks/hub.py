"""TaskHub: central coordinator for background task delegation."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import TYPE_CHECKING

from ductor_bot.cli.claude_features import snapshot_refresh_active
from ductor_bot.cli.types import AgentRequest
from ductor_bot.infra.input_context import current_input_context, received_at
from ductor_bot.infra.shutdown_budget import CANCEL_FLOOR_SECONDS, ShutdownBudget, wait_capped
from ductor_bot.tasks.models import (
    TaskEntry,
    TaskInFlight,
    TaskResult,
    TaskSubmit,
    normalise_origin_transport,
    normalise_priority,
)
from ductor_bot.workspace.loader import build_appended_files_block

if TYPE_CHECKING:
    from pathlib import Path

    from ductor_bot.cli.process_registry import ProcessRegistry
    from ductor_bot.cli.service import CLIService
    from ductor_bot.config import TasksConfig
    from ductor_bot.tasks.registry import TaskRegistry
    from ductor_bot.workspace.paths import DuctorPaths

logger = logging.getLogger(__name__)

_FINISHED = frozenset({"done", "failed", "cancelled"})
_RESUMABLE = frozenset({"done", "failed", "cancelled", "waiting"})
_MAINTENANCE_INTERVAL = 5 * 3600  # 5 hours
_QUESTION_TIMEOUT = 270.0
_QUESTION_FORWARDED = (
    "Question forwarded to parent agent. "
    "Finish your current work — you will be resumed with the answer."
)

TaskResultCallback = Callable[[TaskResult], Awaitable[None]]
QuestionHandler = Callable[[str, str, str, int, int | None, str], Awaitable[None]]
TaskQuestionAnswerer = Callable[
    [str, str, str, int, int | None, str, Callable[[], str], Callable[[], bool], float],
    Awaitable[tuple[str | None, str]],
]
# A fixed zone name, or a getter so hot-reloaded settings reach receipts.
TimezoneSource = str | Callable[[], str]
# QuestionHandler(task_id, question, prompt_preview, chat_id, thread_id, transport) -> None

TASK_PROMPT_SUFFIX = """

---
TASK RULES (MANDATORY):
You are a background task agent. You have NO direct user access.

If you need information to complete this task, use:
```
python3 tools/task_tools/ask_parent.py "your question here"
```
Set the command timeout to at least 300 seconds (300000 ms). Wait up to about
five minutes for the result.
- If the output starts with "Answer from parent:", continue with that answer.
- If it starts with "Question forwarded", or the command fails or times out,
  finish what you can, include the unresolved question in your final result,
  and end this run. The parent will resume you with the answer after the run ends.
- Record each question and any answer in TASKMEMORY.md and your final result.

After finishing, update your task memory: {taskmemory_path}
"""

_RESUME_REMINDER = """

---
REMINDER: You are a background task agent with NO direct user access.
- Need information? Use: python3 tools/task_tools/ask_parent.py "your question"
- Set the command timeout to at least 300 seconds (300000 ms) and wait up to
  about five minutes.
- If the output starts with "Answer from parent:", continue with that answer.
- If it starts with "Question forwarded", or the command fails or times out,
  finish what you can, include the unresolved question in your final result,
  and end this run. The parent will resume you with the answer after the run ends.
- Record each question and any answer in TASKMEMORY.md and your final result.
- When done, write your final results to: {taskmemory_path}
"""


class TaskHub:
    """Central coordinator for background task delegation.

    Combines ``BackgroundObserver`` execution pattern with ``InterAgentBus``
    result-delivery pattern. Manages the full lifecycle: submit → execute →
    question handling → result delivery.
    """

    def __init__(  # noqa: PLR0913
        self,
        registry: TaskRegistry,
        paths: DuctorPaths,
        *,
        cli_service: CLIService | None = None,
        config: TasksConfig,
        user_timezone: TimezoneSource = "UTC",
        append_system_prompt_files: list[str] | None = None,
        claude_system_prompt_snapshot: str = "off",
        process_registry: ProcessRegistry | None = None,
    ) -> None:
        self._registry = registry
        self._paths = paths
        self._cli_service = cli_service
        self._cli_services: dict[str, CLIService] = {}
        self._agent_tasks_dirs: dict[str, Path] = {}
        self._agent_paths: dict[str, DuctorPaths] = {}
        self._append_system_prompt_files = append_system_prompt_files or []
        self._snapshot_mode = claude_system_prompt_snapshot
        self._agent_snapshot_modes: dict[str, str] = {}
        self._config = config
        self._user_timezone = user_timezone
        self._agent_timezones: dict[str, TimezoneSource] = {}
        self._in_flight: dict[str, TaskInFlight] = {}
        self._pending_resumes: dict[str, tuple[TaskEntry, str, str, str]] = {}
        self._result_handlers: dict[str, TaskResultCallback] = {}
        self._question_handlers: dict[str, QuestionHandler] = {}
        self._question_answerers: dict[str, TaskQuestionAnswerer] = {}
        self._question_locks: dict[str, asyncio.Lock] = {}
        self._run_sequences: dict[str, int] = {}
        self._agent_chat_ids: dict[str, int] = {}
        self._maintenance_task: asyncio.Task[None] | None = None
        # #92: registry used to kill task subprocess trees on cancel. A single
        # shared registry works when all task subprocesses register into it
        # (supervisor wires this — see ``AgentSupervisor._wire_task_hub``).
        # For multi-agent setups where each agent owns its own ProcessRegistry,
        # per-agent lookups take precedence via ``_agent_process_registries``.
        self._process_registry = process_registry
        self._agent_process_registries: dict[str, ProcessRegistry] = {}
        self._pending_deliveries: set[asyncio.Task[None]] = set()
        # Agents (or all, with ``_shutting_down_all``) whose stop has begun: a task that the
        # stop cuts off is recorded for a notice after the next start, not delivered now.
        self._shutting_down: set[str] = set()
        self._shutting_down_all = False

    def begin_shutdown(self, agent_name: str | None = None) -> None:
        """Mark a stop as begun, before any CLI process of the agent is killed."""
        if agent_name is None:
            self._shutting_down_all = True
        else:
            self._shutting_down.add(agent_name)

    def _is_shutting_down(self, agent_name: str) -> bool:
        return self._shutting_down_all or agent_name in self._shutting_down

    def has_result_handler(self, agent_name: str) -> bool:
        return agent_name in self._result_handlers

    async def report_interrupted(
        self, agent_name: str, notify: Callable[[TaskResult], Awaitable[bool]]
    ) -> int:
        """Tell the parent about each task a restart cut off (one try each per start).

        *notify* returns whether the notice reached the user. The flag is cleared only then;
        a failed try counts, and the third failed try clears it with a warning. Returns how
        many notices arrived.
        """
        told = 0
        for entry in self._registry.pending_notices(agent_name):
            try:
                delivered = await notify(self._interrupted_result(entry))
            except Exception:
                logger.exception("Notice about interrupted task %s failed", entry.task_id)
                delivered = False
            attempts = entry.notify_attempts if delivered else entry.notify_attempts + 1
            give_up = not delivered and attempts >= MAX_NOTICE_ATTEMPTS
            if give_up:
                logger.warning(
                    "Giving up telling the parent about interrupted task %s after %d tries",
                    entry.task_id,
                    attempts,
                )
            self._registry.update_status(
                entry.task_id,
                entry.status,
                notify_pending=not (delivered or give_up),
                notify_attempts=attempts,
            )
            told += delivered
        return told

    @staticmethod
    def _interrupted_result(entry: TaskEntry) -> TaskResult:
        """The failed result that tells a parent a restart cut its task off."""
        error = INTERRUPTED_BY_RESTART
        if entry.session_id and entry.provider:
            error += (
                f"\n\nTo continue this task's conversation, use:\n"
                f'python3 tools/task_tools/resume_task.py {entry.task_id} "your follow-up"'
            )
        return TaskResult(
            task_id=entry.task_id,
            chat_id=entry.chat_id,
            parent_agent=entry.parent_agent,
            name=entry.name,
            prompt_preview=entry.prompt_preview,
            result_text="",
            status="failed",
            elapsed_seconds=entry.elapsed_seconds,
            provider=entry.provider,
            model=entry.model,
            session_id=entry.session_id,
            error=error,
            original_prompt=entry.original_prompt,
            thread_id=entry.thread_id,
        )

    def start_maintenance(self) -> None:
        """Start periodic orphan cleanup (call once after bot startup)."""
        if self._maintenance_task is None:
            self._maintenance_task = asyncio.create_task(
                self._maintenance_loop(), name="task-maintenance"
            )

    @property
    def registry(self) -> TaskRegistry:
        return self._registry

    def set_result_handler(self, agent_name: str, handler: TaskResultCallback) -> None:
        """Register callback for delivering results to a parent agent."""
        self._result_handlers[agent_name] = handler
        self._shutting_down.discard(agent_name)  # the agent was started (again)

    def set_question_handler(self, agent_name: str, handler: QuestionHandler) -> None:
        """Register handler for task-agent questions (ask_parent)."""
        self._question_handlers[agent_name] = handler

    def set_task_question_answerer(self, agent_name: str, answerer: TaskQuestionAnswerer) -> None:
        """Register private answers in the source conversation of a parent agent."""
        self._question_answerers[agent_name] = answerer

    def set_cli_service(
        self, agent_name: str, cli: CLIService, *, user_timezone: TimezoneSource = "UTC"
    ) -> None:
        """Register a per-agent CLI service and its configured receipt timezone."""
        self._cli_services[agent_name] = cli
        self._agent_timezones[agent_name] = user_timezone

    def set_agent_process_registry(
        self, agent_name: str, process_registry: ProcessRegistry
    ) -> None:
        """Register a per-agent ProcessRegistry for subprocess-aware cancel.

        Each agent's orchestrator owns its own ``ProcessRegistry`` (see
        ``Orchestrator.__init__``), and task subprocesses register under the
        label ``task:<id>`` in THAT registry. When cancel() fires for a task
        we therefore need the registry tied to the task's parent agent; this
        map provides that lookup. Falls back to the shared ``_process_registry``
        when no per-agent entry exists.
        """
        self._agent_process_registries[agent_name] = process_registry

    def _resolve_process_registry(self, parent_agent: str | None) -> ProcessRegistry | None:
        """Pick the ProcessRegistry for *parent_agent*, or the shared default."""
        if parent_agent and parent_agent in self._agent_process_registries:
            return self._agent_process_registries[parent_agent]
        return self._process_registry

    def set_agent_paths(self, agent_name: str, paths: DuctorPaths) -> None:
        """Register per-agent paths for task folder isolation."""
        self._agent_tasks_dirs[agent_name] = paths.tasks_dir
        self._agent_paths[agent_name] = paths

    def set_agent_snapshot_mode(self, agent_name: str, mode: str) -> None:
        """Register the agent's ``claude_system_prompt_snapshot`` for its tasks."""
        self._agent_snapshot_modes[agent_name] = mode

    def set_agent_chat_id(self, agent_name: str, chat_id: int) -> None:
        """Register the primary chat_id for an agent (for resolving CLI-submitted tasks)."""
        self._agent_chat_ids[agent_name] = chat_id

    def bind_first_turn_origin(  # noqa: PLR0913, PLR0917
        self,
        agent_name: str,
        chat_id: int,
        topic_id: int | None,
        transport: str,
        session_id: str,
        turn_started_at: float,
    ) -> None:
        """Bind unbound foreground tasks accepted during this successful turn."""
        if not session_id:
            return
        transport = normalise_origin_transport(transport)
        for entry in self._registry.list_all():
            accepted_at = max(entry.created_at, entry.instruction_received_at)
            if (
                entry.parent_agent == agent_name
                and entry.chat_id == chat_id
                and entry.thread_id == topic_id
                and normalise_origin_transport(entry.origin_transport) == transport
                and entry.origin_session_kind == "foreground"
                and not entry.origin_session_id
                and turn_started_at <= accepted_at
            ):
                self._registry.update_status(
                    entry.task_id, entry.status, origin_session_id=session_id
                )

    def _check_enabled(self) -> None:
        if not self._config.enabled:
            msg = "Task system is disabled"
            raise ValueError(msg)
        if self._cli_service is None and not self._cli_services:
            msg = "CLIService not available"
            raise ValueError(msg)

    def submit(self, submit: TaskSubmit) -> str:
        """Create a task, spawn CLI subprocess. Returns task_id."""
        timestamp = time.time()
        self._check_enabled()

        # Resolve chat_id: CLI subprocess doesn't know it, look up from agent name
        if not submit.chat_id:
            resolved = self._agent_chat_ids.get(submit.parent_agent, 0)
            if resolved:
                submit.chat_id = resolved

        # #79: interactive tasks bypass the per-chat concurrency cap so
        # direct user follow-ups stay responsive under heavy batch load.
        # Active count excludes already-running interactive tasks for the
        # same reason — they never "fill up" the cap for background work.
        priority = normalise_priority(submit.priority)
        if priority != "interactive":
            active = sum(
                1
                for t in self._in_flight.values()
                if t.entry.chat_id == submit.chat_id
                and t.asyncio_task
                and not t.asyncio_task.done()
                and t.entry.priority != "interactive"
            )
            if active >= self._config.max_parallel:
                msg = f"Too many background tasks ({self._config.max_parallel} max)"
                raise ValueError(msg)

        provider = submit.provider_override or ""
        model = submit.model_override or ""
        thinking = submit.thinking_override or ""
        reasoning_effort = submit.reasoning_effort_override or ""

        # Resolve per-agent tasks_dir for folder isolation
        agent_tasks_dir = self._agent_tasks_dirs.get(submit.parent_agent)
        entry = self._registry.create(
            submit,
            provider,
            model,
            thinking=thinking,
            reasoning_effort=reasoning_effort,
            tasks_dir=agent_tasks_dir,
            priority=priority,
        )

        self._registry.update_status(entry.task_id, entry.status, instruction_received_at=timestamp)

        # Build prompt with mandatory suffix
        taskmemory = self._registry.taskmemory_path(entry.task_id)
        full_prompt = submit.prompt + TASK_PROMPT_SUFFIX.format(taskmemory_path=taskmemory)

        self._spawn(entry, full_prompt, thinking)

        logger.info(
            "Task submitted id=%s name='%s' parent=%s provider=%s",
            entry.task_id,
            entry.name,
            submit.parent_agent,
            entry.provider or "(parent default)",
        )
        return entry.task_id

    def resume(  # noqa: PLR0913
        self,
        task_id: str,
        follow_up: str,
        *,
        parent_agent: str = "",
        chat_id: int | None = None,
        thread_id: int | None = None,
        origin_transport: str = "",
        origin_session_kind: str = "",
        origin_session_id: str = "",
    ) -> str:
        """Resume a completed task's CLI session with a follow-up. Returns task_id.

        With *chat_id* the entry's routing moves to the requesting chat/topic
        (*thread_id* ``None`` = no topic), so this run's result, ``ask_parent``
        questions and the parent's answers go there and survive a restart. Without
        it the stored routing is unchanged.
        """
        timestamp = time.time()
        self._check_enabled()

        entry = self._registry.get(task_id)
        if entry is None:
            msg = f"Task '{task_id}' not found"
            raise ValueError(msg)
        if task_id in self._pending_resumes:
            msg = f"Task '{task_id}' already has a pending resume"
            raise ValueError(msg)
        if entry.status not in _RESUMABLE:
            msg = f"Task '{task_id}' is still {entry.status}"
            raise ValueError(msg)
        if not entry.session_id:
            msg = f"Task '{task_id}' has no resumable session"
            raise ValueError(msg)
        if not entry.provider:
            msg = f"Task '{task_id}' has no provider recorded"
            raise ValueError(msg)

        # #158: the persisted status can lag behind the actual execution
        # (e.g. a stale "waiting"/"done" while the subprocess still runs).
        # Never spawn a second run for the same task_id — _spawn() would
        # overwrite the in-flight handle and the two runs would race on
        # status updates and result delivery.
        inflight = self._in_flight.get(task_id)
        deferred = bool(inflight and inflight.asyncio_task and not inflight.asyncio_task.done())
        if deferred and inflight is not None and not inflight.status_final:
            msg = f"Task '{task_id}' is already running"
            raise ValueError(msg)

        routing: dict[str, object] = {"chat_id": chat_id, "thread_id": thread_id} if chat_id else {}

        # Reset to running — same entry, same folder, same task_id
        self._registry.update_status(
            task_id,
            "running",
            completed_at=0.0,
            error="",
            result_preview="",
            last_question="",
            instruction_received_at=timestamp,
            instruction_waited_seconds=None,
            origin_transport=origin_transport,
            origin_session_kind=origin_session_kind,
            origin_session_id=origin_session_id,
            notify_pending=False,
            notify_attempts=0,
            **routing,
        )

        # Append a short system reminder so the task agent remembers how to
        # communicate (ask_parent, TASKMEMORY, no direct user access).
        taskmemory = self._registry.taskmemory_path(entry.task_id)
        full_prompt = follow_up + _RESUME_REMINDER.format(taskmemory_path=taskmemory)
        if deferred:
            self._pending_resumes[task_id] = (entry, full_prompt, entry.thinking, entry.session_id)
            logger.info("Task resume queued id=%s until the previous run ends", task_id)
            return task_id
        self._spawn(entry, full_prompt, entry.thinking, resume_session=entry.session_id)

        logger.info(
            "Task resumed id=%s name='%s' provider=%s",
            task_id,
            entry.name,
            entry.provider,
        )
        return task_id

    def _spawn(
        self,
        entry: TaskEntry,
        prompt: str,
        thinking: str,
        *,
        resume_session: str | None = None,
    ) -> None:
        """Create the asyncio task and register it in-flight."""
        run_seq = self._run_sequences.get(entry.task_id, 0) + 1
        self._run_sequences[entry.task_id] = run_seq
        inflight = TaskInFlight(entry=entry, run_seq=run_seq)
        atask = asyncio.create_task(
            self._run(
                entry, prompt, thinking, resume_session=resume_session, queued_at=time.monotonic()
            ),
            name=f"task:{entry.task_id}",
        )
        inflight.asyncio_task = atask

        def completed(_task: asyncio.Task[None]) -> None:
            if self._in_flight.get(entry.task_id) is not inflight:
                return
            self._in_flight.pop(entry.task_id)
            pending = self._pending_resumes.pop(entry.task_id, None)
            if pending is not None:
                resumed_entry, resumed_prompt, resumed_thinking, resumed_session = pending
                if not self._interrupted_by_shutdown(resumed_entry, resumed_entry.elapsed_seconds):
                    self._spawn(
                        resumed_entry,
                        resumed_prompt,
                        resumed_thinking,
                        resume_session=resumed_session,
                    )

        atask.add_done_callback(completed)
        self._in_flight[entry.task_id] = inflight

    async def ask_parent(  # noqa: C901, PLR0912
        self, task_id: str, question: str, *, connection_alive: Callable[[], bool]
    ) -> str:
        """Answer a running task privately, or hand off its unanswered question."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _QUESTION_TIMEOUT
        timestamp = time.time()
        result = "fallback"
        reason = "no_task"
        handoff = "none"
        try:
            entry = self._registry.get(task_id)
            if entry is None:
                return "Error: Task not found"
            snapshot = replace(entry)
            inflight = self._in_flight.get(task_id)
            run_seq = self._run_sequences.get(task_id, 0)
            self._registry.update_status(
                task_id,
                entry.status,
                question_count=entry.question_count + 1,
                last_question=question[:200],
                last_question_received_at=timestamp,
            )
            if snapshot.parent_agent not in self._question_handlers:
                reason = "no_handler"
                return f"Error: No question handler for agent '{snapshot.parent_agent}'"
            answerer = self._question_answerers.get(snapshot.parent_agent)
            if (
                snapshot.origin_session_kind != "foreground"
                or not snapshot.origin_transport
                or not snapshot.chat_id
            ):
                reason = "ineligible"
            elif answerer is None:
                reason = "no_answerer"
            elif inflight is None or inflight.status_final:
                reason = "not_running"
            elif not connection_alive():
                reason = "client_gone"
            else:

                def resolve_origin_session_id() -> str:
                    current = (
                        self._registry.get(task_id)
                        if not snapshot.origin_session_id
                        and self._run_sequences.get(task_id) == run_seq
                        else snapshot
                    )
                    return current.origin_session_id if current is not None else ""

                lock = self._question_locks.setdefault(task_id, asyncio.Lock())
                try:
                    async with asyncio.timeout_at(deadline), lock:
                        if self._in_flight.get(task_id) is not inflight or inflight.status_final:
                            reason = "not_running"
                        else:
                            answer, reason = await answerer(
                                task_id,
                                snapshot.name,
                                question,
                                snapshot.chat_id,
                                snapshot.thread_id,
                                snapshot.origin_transport,
                                resolve_origin_session_id,
                                connection_alive,
                                deadline,
                            )
                            if answer is not None:
                                if connection_alive():
                                    result = "answered"
                                    reason = ""
                                    return f"Answer from parent:\n{answer}"
                                result = "late"
                                reason = "client_gone"
                except TimeoutError:
                    reason = "deadline"
                except Exception:
                    reason = "error"
            handoff = self._handoff_task_question(snapshot, question, inflight, run_seq, timestamp)
            return _QUESTION_FORWARDED
        finally:
            logger.info(
                "Task question task=%s result=%s reason=%s handoff=%s",
                task_id,
                result,
                reason,
                handoff,
            )

    def _handoff_task_question(
        self,
        snapshot: TaskEntry,
        question: str,
        inflight: TaskInFlight | None,
        run_seq: int,
        timestamp: float,
    ) -> str:
        """Apply the unanswered-question transition without touching a newer run."""
        task_id = snapshot.task_id
        current = self._registry.get(task_id)
        if current is None:
            return "skipped_deleted"
        same_run = self._run_sequences.get(task_id, 0) == run_seq
        if same_run:
            if (
                self._in_flight.get(task_id) is inflight
                and inflight is not None
                and not inflight.status_final
            ):
                inflight.has_pending_question = True
            elif (
                current.status == "cancelled" and inflight is not None and inflight.cancel_requested
            ):
                return "skipped_cancelled"
            elif current.status == "done":
                self._registry.update_status(task_id, "waiting")
        handler = self._question_handlers.get(snapshot.parent_agent)
        if handler is None:
            return "none"
        delivery = asyncio.create_task(
            self._deliver_question(
                handler,
                snapshot,
                question,
                received_timestamp=timestamp,
                earlier_run=not same_run,
            ),
            name=f"task-question:{task_id}",
        )
        delivery.add_done_callback(lambda _: None)
        return "posted"

    async def forward_question(self, task_id: str, question: str) -> str:
        """Forward a task agent's question to the parent. Returns immediately.

        The question is delivered asynchronously to the parent agent's Telegram
        chat. The parent answers by resuming the task with ``resume_task.py``.
        """
        timestamp = time.time()
        entry = self._registry.get(task_id)
        if entry is None:
            return "Error: Task not found"

        handler = self._question_handlers.get(entry.parent_agent)
        if handler is None:
            return f"Error: No question handler for agent '{entry.parent_agent}'"

        logger.info(
            "Task question forwarding task=%s parent=%s",
            task_id,
            entry.parent_agent,
        )

        self._registry.update_status(
            task_id,
            entry.status,
            question_count=entry.question_count + 1,
            last_question=question[:200],
            last_question_received_at=timestamp,
        )

        # Mark in-flight task so _run() uses "waiting" instead of "done"
        inflight = self._in_flight.get(task_id)
        if inflight and not inflight.status_final:
            inflight.has_pending_question = True

        # Fire-and-forget: deliver to parent's Telegram chat
        task = asyncio.create_task(
            self._deliver_question(handler, entry, question, received_timestamp=timestamp),
            name=f"task-question:{task_id}",
        )
        task.add_done_callback(lambda _: None)  # prevent GC of fire-and-forget task

        return (
            "Question forwarded to parent agent. "
            "Finish your current work — you will be resumed with the answer."
        )

    async def _deliver_question(
        self,
        handler: QuestionHandler,
        entry: TaskEntry,
        question: str,
        *,
        received_timestamp: float | None = None,
        earlier_run: bool = False,
    ) -> None:
        """Deliver question to parent agent (background coroutine)."""
        timestamp = received_timestamp
        if timestamp is None:
            timestamp = entry.last_question_received_at
            if not timestamp:
                # Exact receipt is unknown for old task files. Freeze the first
                # recovery receipt once so later delivery does not keep retiming it.
                timestamp = time.time()
                self._registry.update_status(
                    entry.task_id, entry.status, last_question_received_at=timestamp
                )
        try:
            with received_at(timestamp):
                await handler(
                    entry.task_id,
                    _question_prompt(entry, question, earlier_run=earlier_run),
                    entry.prompt_preview,
                    entry.chat_id,
                    entry.thread_id,
                    entry.origin_transport,
                )
        except Exception:
            logger.exception("Question delivery failed for task %s", entry.task_id)

    async def cancel(self, task_id: str) -> bool:
        """Cancel a running task. Returns True if cancelled.

        Kill order (per issue #92 / Pitfall 2): subprocess tree first so the
        CLI's streaming ``await`` unblocks, THEN asyncio task. Inverting this
        order hangs — ``cli.execute`` is blocked on the subprocess pipe, and
        a pending ``CancelledError`` cannot propagate until the pipe closes.
        """
        if self._pending_resumes.pop(task_id, None) is not None:
            self._registry.update_status(task_id, "cancelled", completed_at=time.time())
        inflight = self._in_flight.get(task_id)
        if inflight is None or inflight.asyncio_task is None or inflight.asyncio_task.done():
            logger.info("Cancel requested for non-running task id=%s", task_id)
            return False
        inflight.cancel_requested = True
        registry = self._resolve_process_registry(inflight.entry.parent_agent)
        if registry is not None:
            await registry.kill_for_task(task_id)
        inflight.asyncio_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await inflight.asyncio_task
        logger.info("Task cancel accepted id=%s name='%s'", task_id, inflight.entry.name)
        return True

    async def cancel_all(self, chat_id: int) -> int:
        """Cancel all running tasks for a chat.

        Kill order mirrors :meth:`cancel`: every task's subprocess tree is
        killed first (one ``kill_for_task`` per task) before any asyncio
        ``Task.cancel`` fires. Sequential ``await`` keeps each SIGTERM→SIGKILL
        ladder independent.
        """
        for task_id, pending in list(self._pending_resumes.items()):
            if pending[0].chat_id == chat_id:
                self._pending_resumes.pop(task_id)
                self._registry.update_status(task_id, "cancelled", completed_at=time.time())
        targets: list[tuple[str, str | None, asyncio.Task[None]]] = [
            (inflight.entry.task_id, inflight.entry.parent_agent, inflight.asyncio_task)
            for inflight in list(self._in_flight.values())
            if (
                inflight.entry.chat_id == chat_id
                and inflight.asyncio_task
                and not inflight.asyncio_task.done()
            )
        ]
        for task_id, _, _ in targets:
            self._in_flight[task_id].cancel_requested = True
        if not targets:
            return 0
        for task_id, parent_agent, _ in targets:
            registry = self._resolve_process_registry(parent_agent)
            if registry is not None:
                await registry.kill_for_task(task_id)
        cancelled: list[asyncio.Task[None]] = [atask for _, _, atask in targets]
        for atask in cancelled:
            atask.cancel()
        await asyncio.gather(*cancelled, return_exceptions=True)
        logger.info("Task cancel accepted for %d task(s) chat=%s", len(cancelled), chat_id)
        return len(cancelled)

    async def shutdown(self, budget: ShutdownBudget | None = None) -> None:
        """Cancel all in-flight tasks and clean up; the waits are capped by *budget*."""
        budget = budget or ShutdownBudget()
        self.begin_shutdown()  # what the cancellation below cuts off is recorded, not delivered
        if self._maintenance_task and not self._maintenance_task.done():
            self._maintenance_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._maintenance_task
            self._maintenance_task = None

        cancelled: list[asyncio.Task[None]] = []
        for inflight in list(self._in_flight.values()):
            if inflight.asyncio_task and not inflight.asyncio_task.done():
                inflight.asyncio_task.cancel()
                cancelled.append(inflight.asyncio_task)
        if cancelled:
            await wait_capped(
                asyncio.gather(*cancelled, return_exceptions=True),
                max(budget.remaining(), CANCEL_FLOOR_SECONDS),
                label="task cancel",
            )
        while self._pending_resumes:
            entry = self._pending_resumes.popitem()[1][0]
            self._interrupted_by_shutdown(entry, entry.elapsed_seconds)
        self._in_flight.clear()
        if self._pending_deliveries:
            await wait_capped(
                asyncio.gather(*self._pending_deliveries, return_exceptions=True),
                budget.remaining(),
                label="task result delivery",
            )

    async def _maintenance_loop(self) -> None:
        """Periodically clean orphaned task entries/folders and old finished tasks."""
        try:
            while True:
                try:
                    self._run_maintenance_once()
                except Exception:
                    logger.exception("Task maintenance failed (continuing)")
                await asyncio.sleep(_MAINTENANCE_INTERVAL)
        except asyncio.CancelledError:
            pass

    def _run_maintenance_once(self) -> None:
        """Run one task registry maintenance pass."""
        removed = self._registry.cleanup_orphans()
        if removed:
            logger.info("Task maintenance: removed %d orphan(s)", removed)
        pruned = self._registry.cleanup_finished_retention(
            max_age_hours=self._config.finished_retention_hours,
            keep_last=self._config.finished_keep_last,
        )
        if pruned:
            logger.info("Task maintenance: pruned %d finished task(s)", pruned)

    def _start_final_delivery(self, result: TaskResult) -> asyncio.Task[None]:
        delivery_task = asyncio.create_task(self._deliver(result))
        self._pending_deliveries.add(delivery_task)
        delivery_task.add_done_callback(self._pending_deliveries.discard)
        return delivery_task

    async def _prepare_request(
        self,
        entry: TaskEntry,
        prompt: str,
        cli: CLIService,
        resume_session: str | None,
    ) -> AgentRequest:
        """Build the CLI request for a task run and pre-resolve provider/model."""
        agent_paths = self._agent_paths.get(entry.parent_agent, self._paths)
        files_block = await build_appended_files_block(
            agent_paths, self._append_system_prompt_files
        )

        request = AgentRequest(
            prompt=prompt,
            received_context=self._instruction_context(entry),
            append_system_prompt=files_block,
            model_override=entry.model or None,
            provider_override=entry.provider or None,
            effort_override=entry.reasoning_effort or None,
            chat_id=entry.chat_id,
            topic_id=entry.thread_id,
            process_label=f"task:{entry.task_id}",
            timeout_seconds=self._config.timeout_seconds,
            resume_session=resume_session,
        )

        # Pre-resolve effective provider/model so the entry is never empty
        eff_provider, eff_model = cli.resolve_provider(request)
        if eff_provider and not entry.provider:
            self._registry.update_status(
                entry.task_id, "running", provider=eff_provider, model=eff_model
            )
            entry.provider = eff_provider
            entry.model = eff_model
        mode = self._agent_snapshot_modes.get(entry.parent_agent, self._snapshot_mode)
        if await snapshot_refresh_active(mode, eff_provider):
            request = replace(request, claude_snapshot_off=True)
        return request

    def _instruction_context(self, entry: TaskEntry) -> str | None:
        """Format durable receipt metadata without changing instruction text."""
        timestamp = entry.instruction_received_at
        if not timestamp:
            # Exact instruction intake is unknown in older registries, including
            # tasks already resumed before this field existed. Freeze the first
            # recovery receipt instead of claiming creation was follow-up intake.
            timestamp = time.time()
            self._registry.update_status(
                entry.task_id, entry.status, instruction_received_at=timestamp
            )
        source = self._agent_timezones.get(entry.parent_agent, self._user_timezone)
        timezone: object = source() if callable(source) else source
        if not isinstance(timezone, str):
            timezone = "UTC"
        with received_at(timestamp, waited_seconds=entry.instruction_waited_seconds):
            return current_input_context(timezone)

    async def _run(  # noqa: C901, PLR0915
        self,
        entry: TaskEntry,
        prompt: str,
        thinking: str,
        *,
        resume_session: str | None = None,
        queued_at: float | None = None,
    ) -> None:
        """Execute task as CLI subprocess."""
        cli = self._cli_services.get(entry.parent_agent) or self._cli_service
        assert cli is not None

        t0 = time.monotonic()
        inflight = self._in_flight.get(entry.task_id)
        final_delivery_started = False
        try:
            if queued_at is not None:
                waited = max(0.0, t0 - queued_at)
                self._registry.update_status(
                    entry.task_id,
                    entry.status,
                    instruction_waited_seconds=waited if waited >= 0.001 else None,
                )
            timeout = self._config.timeout_seconds
            request = await self._prepare_request(entry, prompt, cli, resume_session)
            response = await cli.execute(request)

            elapsed = time.monotonic() - t0
            has_pending = bool(inflight and inflight.has_pending_question)
            if inflight is not None:
                inflight.status_final = True
            status, error = _classify_task_response(response, timeout, has_pending, elapsed)
            if status == "cancelled" and self._interrupted_by_shutdown(entry, elapsed, response):
                return

            # Accumulate turns (resume adds to previous count)
            total_turns = entry.num_turns + response.num_turns

            self._registry.update_status(
                entry.task_id,
                status,
                session_id=response.session_id or "",
                completed_at=time.time(),
                elapsed_seconds=elapsed,
                error=error,
                result_preview=(response.result or "")[:_RESULT_PREVIEW_LEN],
                num_turns=total_turns,
            )

            result_text = response.result or ""
            session_id = response.session_id or ""

            # Append TASKMEMORY.md content so the parent gets the full picture.
            # Also include it on cancelled tasks (MED #1): partial work a
            # sub-agent wrote before SIGTERM/SIGKILL must not be silently lost.
            if status in {"done", "cancelled"}:
                taskmemory = self._registry.taskmemory_path(entry.task_id)
                result_text = _append_taskmemory(result_text, taskmemory)

            # Append resume hint so the parent agent knows it can follow up
            if status == "done" and session_id:
                result_text += (
                    f"\n\n---\nTo continue this task's conversation, use:\n"
                    f'python3 tools/task_tools/resume_task.py {entry.task_id} "your follow-up"'
                )

            final_delivery_started = True
            await asyncio.shield(
                self._start_final_delivery(
                    TaskResult(
                        task_id=entry.task_id,
                        chat_id=entry.chat_id,
                        parent_agent=entry.parent_agent,
                        name=entry.name,
                        prompt_preview=entry.prompt_preview,
                        result_text=result_text,
                        status=status,
                        elapsed_seconds=elapsed,
                        provider=entry.provider,
                        model=entry.model,
                        session_id=session_id,
                        error=error,
                        task_folder=str(self._registry.task_folder(entry.task_id)),
                        original_prompt=entry.original_prompt,
                        thread_id=entry.thread_id,
                    )
                )
            )

        except asyncio.CancelledError:
            if inflight is not None:
                inflight.status_final = True
            if final_delivery_started:
                logger.info(
                    "Task %s cancelled after completion — finished result preserved",
                    entry.task_id,
                )
                raise
            elapsed = time.monotonic() - t0
            if self._interrupted_by_shutdown(entry, elapsed):
                raise
            self._registry.update_status(
                entry.task_id,
                "cancelled",
                completed_at=time.time(),
                elapsed_seconds=elapsed,
            )
            # MED #1: include any partial TASKMEMORY.md the sub-agent wrote
            # before cancellation so the parent sees the progress, not silence.
            taskmemory = self._registry.taskmemory_path(entry.task_id)
            partial_text = _append_taskmemory("", taskmemory)
            with contextlib.suppress(Exception):
                await self._deliver(
                    TaskResult(
                        task_id=entry.task_id,
                        chat_id=entry.chat_id,
                        parent_agent=entry.parent_agent,
                        name=entry.name,
                        prompt_preview=entry.prompt_preview,
                        result_text=partial_text,
                        status="cancelled",
                        elapsed_seconds=elapsed,
                        provider=entry.provider,
                        model=entry.model,
                        original_prompt=entry.original_prompt,
                        thread_id=entry.thread_id,
                    )
                )
            raise

        except Exception:
            if inflight is not None:
                inflight.status_final = True
            logger.exception("Task failed id=%s name='%s'", entry.task_id, entry.name)
            elapsed = time.monotonic() - t0
            error_msg = "Internal error (check logs)"
            self._registry.update_status(
                entry.task_id,
                "failed",
                completed_at=time.time(),
                elapsed_seconds=elapsed,
                error=error_msg,
            )
            with contextlib.suppress(Exception):
                await self._deliver(
                    TaskResult(
                        task_id=entry.task_id,
                        chat_id=entry.chat_id,
                        parent_agent=entry.parent_agent,
                        name=entry.name,
                        prompt_preview=entry.prompt_preview,
                        result_text="",
                        status="failed",
                        elapsed_seconds=elapsed,
                        provider=entry.provider,
                        model=entry.model,
                        error=error_msg,
                        original_prompt=entry.original_prompt,
                        thread_id=entry.thread_id,
                    )
                )

    def _interrupted_by_shutdown(
        self, entry: TaskEntry, elapsed: float, response: object | None = None
    ) -> bool:
        """If a shutdown cut the task off, keep it as failed to tell its parent after the start.

        Returns whether it did. Nothing is delivered now: the transport is going away.
        """
        if not self._is_shutting_down(entry.parent_agent):
            return False
        extra: dict[str, object] = {}
        if response is not None:
            extra["session_id"] = getattr(response, "session_id", None) or entry.session_id
            extra["num_turns"] = entry.num_turns + (getattr(response, "num_turns", 0) or 0)
        self._registry.update_status(
            entry.task_id,
            "failed",
            completed_at=time.time(),
            elapsed_seconds=elapsed,
            error=INTERRUPTED_BY_RESTART,
            notify_pending=True,
            **extra,
        )
        logger.info("Task %s was cut off by a shutdown; its parent is told later", entry.task_id)
        return True

    async def _deliver(self, result: TaskResult) -> None:
        """Deliver result to the parent agent's registered callback."""
        handler = self._result_handlers.get(result.parent_agent)
        if handler is None:
            logger.warning(
                "No result handler for parent '%s' task=%s — result lost",
                result.parent_agent,
                result.task_id,
            )
            return
        try:
            await handler(result)
        except Exception:
            logger.exception(
                "Error delivering task result id=%s to '%s'",
                result.task_id,
                result.parent_agent,
            )


INTERRUPTED_BY_RESTART = "interrupted by restart"
MAX_NOTICE_ATTEMPTS = 3
_RESULT_PREVIEW_LEN = 200
_TASKMEMORY_MAX_LEN = 4000
# Exit codes that map to user-initiated cancel (SIGTERM=15, SIGKILL=9).
# Subprocess module reports these as 128+signal (143/137) or negative signal (-15/-9).
_CANCEL_RETURNCODES = frozenset({143, 137, -15, -9})


def _classify_task_response(
    response: object, timeout: float, has_pending_question: bool, elapsed: float = 0.0
) -> tuple[str, str]:
    """Map a CLIResponse to (status, error_message) for the task registry.

    Exit 143/137 (= 128 + SIGTERM/SIGKILL) means kill_for_task terminated the
    subprocess — surface as ``cancelled``, not ``failed``. The same exit code
    after the stale cleanup killed the process (``stale_killed``) is a failure the
    parent is told about.
    """
    if getattr(response, "timed_out", False):
        return "failed", f"Timeout after {timeout:.0f}s"
    if getattr(response, "stale_killed", False) is True:
        return "failed", f"stale process killed after {elapsed:.0f}s"
    if getattr(response, "is_error", False):
        if getattr(response, "returncode", None) in _CANCEL_RETURNCODES:
            return "cancelled", ""
        return "failed", getattr(response, "result", None) or "CLI error"
    if has_pending_question:
        return "waiting", ""
    return "done", ""


def _question_prompt(entry: TaskEntry, question: str, *, earlier_run: bool = False) -> str:
    """The question as the parent session gets it: where it comes from and how to answer.

    The question itself is not changed.
    """
    prefix = (
        "This question was asked during an earlier run; the task has since been resumed.\n\n"
        if earlier_run
        else ""
    )
    return (
        prefix
        + f"Background task '{entry.name}' ({entry.task_id}) asks a question while running:\n"
        f"{question}\n\n"
        f"The task is waiting for its answer. Answer it with: "
        f'python3 tools/task_tools/resume_task.py {entry.task_id} "your answer". '
        "Resume it after this run has finished (a running task cannot be resumed). "
        f"Only what you send with resume_task.py reaches the task; "
        f"your normal reply to this message goes to the user chat."
    )


def _append_taskmemory(result_text: str, taskmemory_path: Path) -> str:
    """Append TASKMEMORY.md content to the result so the parent gets the full context."""
    try:
        if not taskmemory_path.is_file():
            return result_text
        content = taskmemory_path.read_text(encoding="utf-8").strip()
        if not content:
            return result_text
    except OSError:
        logger.debug("Could not read TASKMEMORY.md at %s", taskmemory_path)
        return result_text

    if len(content) > _TASKMEMORY_MAX_LEN:
        # #91: make truncation visible -- silent truncation hid detailed
        # research findings from the parent agent. Log a WARNING (for operators)
        # AND emit a suffix that tells the parent agent the original length
        # and the full file path so it can read the complete content on demand.
        original_len = len(content)
        logger.warning(
            "TASKMEMORY truncated at %s: %d chars -> %d chars "
            "(parent agent sees 'full content at' hint)",
            taskmemory_path,
            original_len,
            _TASKMEMORY_MAX_LEN,
        )
        content = (
            content[:_TASKMEMORY_MAX_LEN]
            + f"\n[... truncated -- original was {original_len} chars. "
            + f"Full content at: {taskmemory_path}]"
        )

    return f"{result_text}\n\n---\nCONTENT FROM TASKMEMORY.MD ({taskmemory_path}):\n\n{content}"
