"""Background task observer: fire-and-forget CLI execution with notification."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from ductor_bot.background.models import BackgroundResult, BackgroundSubmit, BackgroundTask
from ductor_bot.i18n import t
from ductor_bot.infra.input_context import add_input_wait, current_input_context
from ductor_bot.infra.task_runner import TaskRunOptions, run_oneshot_task
from ductor_bot.multiagent.session_gate import GateCancelled
from ductor_bot.session.named import is_interagent_session_name
from ductor_bot.workspace.loader import build_appended_files_block

if TYPE_CHECKING:
    from ductor_bot.cli.param_resolver import TaskExecutionConfig
    from ductor_bot.cli.service import CLIService
    from ductor_bot.config import AgentConfig
    from ductor_bot.multiagent.session_gate import Reservation
    from ductor_bot.session.named import NamedSessionRegistry
    from ductor_bot.workspace.paths import DuctorPaths

logger = logging.getLogger(__name__)

BgResultCallback = Callable[[BackgroundResult], Awaitable[None]]

MAX_TASKS_PER_CHAT = 5


def _make_result(  # noqa: PLR0913  -- result fields have natural multi-arity
    bg_task: BackgroundTask,
    t0: float,
    *,
    result_text: str,
    status: str,
    session_name: str = "",
    session_id: str = "",
) -> BackgroundResult:
    """Build a :class:`BackgroundResult` carrying the task's routing fields."""
    return BackgroundResult(
        task_id=bg_task.task_id,
        chat_id=bg_task.chat_id,
        message_id=bg_task.message_id,
        thread_id=bg_task.thread_id,
        transport=bg_task.transport,
        prompt_preview=bg_task.prompt[:60],
        result_text=result_text,
        status=status,
        elapsed_seconds=time.monotonic() - t0,
        provider=bg_task.provider,
        model=bg_task.model,
        session_name=session_name,
        session_id=session_id,
    )


class BackgroundObserver:
    """Manages fire-and-forget background CLI tasks."""

    def __init__(
        self,
        paths: DuctorPaths,
        *,
        timeout_seconds: float,
        cli_service: CLIService | None = None,
        config: AgentConfig,
    ) -> None:
        self._paths = paths
        self._timeout_seconds = timeout_seconds
        self._cli_service = cli_service
        self._config = config
        self._on_result: BgResultCallback | None = None
        self._tasks: dict[str, BackgroundTask] = {}
        # Inter-agent sessions resumed by hand queue in the same FIFO gate as inter-agent requests.
        self._reserve: Callable[[int, str], Reservation | None] | None = None
        self._sessions: NamedSessionRegistry | None = None
        self._reservations: dict[str, Reservation] = {}

    def set_result_handler(self, handler: BgResultCallback) -> None:
        self._on_result = handler

    def set_session_gate(
        self,
        reserve: Callable[[int, str], Reservation | None],
        sessions: NamedSessionRegistry,
    ) -> None:
        """Connect the inter-agent session gate and the registry that stores their responses."""
        self._reserve = reserve
        self._sessions = sessions

    def submit(
        self,
        sub: BackgroundSubmit,
        exec_config: TaskExecutionConfig,
    ) -> str:
        """Submit a background task. Returns task_id."""
        active = sum(
            1
            for t in self._tasks.values()
            if t.chat_id == sub.chat_id and t.asyncio_task and not t.asyncio_task.done()
        )
        if active >= MAX_TASKS_PER_CHAT:
            msg = t("tasks.too_many", max=MAX_TASKS_PER_CHAT)
            raise ValueError(msg)

        task_id = secrets.token_hex(4)
        has_session_override = bool(sub.provider_override)
        bg_task = BackgroundTask(
            task_id=task_id,
            chat_id=sub.chat_id,
            prompt=sub.prompt,
            message_id=sub.message_id,
            thread_id=sub.thread_id,
            transport=sub.transport,
            provider=sub.provider_override if has_session_override else exec_config.provider,
            model=sub.model_override if has_session_override else exec_config.model,
            reasoning_effort=(
                sub.reasoning_effort_override
                if has_session_override
                else exec_config.reasoning_effort
            ),
            submitted_at=time.monotonic(),
            session_name=sub.session_name,
            resume_session_id=sub.resume_session_id,
            message_count=sub.message_count,
        )
        # Take the place in the session queue now (acceptance order), before the task exists.
        reservation = (
            self._reserve(sub.chat_id, sub.session_name)
            if self._reserve is not None and sub.session_name
            else None
        )
        if reservation is not None:
            self._reservations[task_id] = reservation
        atask = asyncio.create_task(self._run(bg_task, exec_config))
        bg_task.asyncio_task = atask
        atask.add_done_callback(lambda _t: self._tasks.pop(task_id, None))
        if reservation is not None:
            # Also covers a task cancelled before its first step.
            atask.add_done_callback(lambda _t: self._release_gate(task_id))
        self._tasks[task_id] = bg_task
        logger.info(
            "Background task submitted id=%s chat=%d provider=%s session=%s",
            task_id,
            sub.chat_id,
            bg_task.provider,
            sub.session_name or "<stateless>",
        )
        return task_id

    def active_tasks(self, chat_id: int | None = None) -> list[BackgroundTask]:
        tasks = [t for t in self._tasks.values() if t.asyncio_task and not t.asyncio_task.done()]
        if chat_id is not None:
            tasks = [t for t in tasks if t.chat_id == chat_id]
        return tasks

    async def cancel_all(self, chat_id: int) -> int:
        count = 0
        cancelled: list[asyncio.Task[None]] = []
        for task in list(self._tasks.values()):
            if task.chat_id == chat_id and task.asyncio_task and not task.asyncio_task.done():
                task.asyncio_task.cancel()
                cancelled.append(task.asyncio_task)
                count += 1
        if cancelled:
            await asyncio.gather(*cancelled, return_exceptions=True)
        return count

    async def shutdown(self) -> None:
        cancelled: list[asyncio.Task[None]] = []
        for task in list(self._tasks.values()):
            if task.asyncio_task and not task.asyncio_task.done():
                task.asyncio_task.cancel()
                cancelled.append(task.asyncio_task)
        if cancelled:
            await asyncio.gather(*cancelled, return_exceptions=True)
        self._tasks.clear()

    async def _run(self, bg_task: BackgroundTask, exec_config: TaskExecutionConfig) -> None:
        if bg_task.session_name and self._cli_service:
            await self._run_with_session(bg_task)
        else:
            await self._run_oneshot(bg_task, exec_config)

    async def _run_oneshot(self, bg_task: BackgroundTask, exec_config: TaskExecutionConfig) -> None:
        """Legacy stateless execution via run_oneshot_task."""
        t0 = time.monotonic()
        try:
            result = await run_oneshot_task(
                exec_config,
                bg_task.prompt,
                TaskRunOptions(
                    cwd=self._paths.workspace,
                    timeout_seconds=self._timeout_seconds,
                    timeout_label="Background task",
                    ductor_home=self._paths.ductor_home,
                ),
            )

            await self._deliver(
                _make_result(
                    bg_task,
                    t0,
                    result_text=result.result_text,
                    status="error:cli_not_found" if result.execution is None else result.status,
                )
            )
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await self._deliver(_make_result(bg_task, t0, result_text="", status="aborted"))
            raise
        except Exception:
            logger.exception("Background task failed id=%s", bg_task.task_id)
            with contextlib.suppress(Exception):
                await self._deliver(
                    _make_result(
                        bg_task,
                        t0,
                        result_text=t("tasks.internal_error"),
                        status="error:internal",
                    )
                )

    def _release_gate(self, task_id: str) -> None:
        reservation = self._reservations.pop(task_id, None)
        if reservation is not None:
            reservation.release()

    async def _finish_session_task(self, bg_task: BackgroundTask, result: BackgroundResult) -> None:
        """Store an inter-agent session's response, give the place back, then send the result.

        The response is stored while the session is still protected; the place is returned
        before the (possibly slow) delivery to the transport.
        """
        if self._sessions is not None and is_interagent_session_name(bg_task.session_name):
            self._sessions.update_after_response(
                bg_task.chat_id, bg_task.session_name, result.session_id
            )
        self._release_gate(bg_task.task_id)
        await self._deliver(result)

    async def _admit_session_task(self, bg_task: BackgroundTask) -> bool:
        """Wait for the place in the session queue and refresh the session state afterwards.

        Returns ``False`` when the session ended while the task waited (it must not run).
        """
        reservation = self._reservations.get(bg_task.task_id)
        if reservation is None:
            return True
        waited_from = time.monotonic()
        try:
            await reservation.admit()
        except GateCancelled:
            return False
        add_input_wait(time.monotonic() - waited_from)
        if self._sessions is None:
            return True
        ns = self._sessions.get(bg_task.chat_id, bg_task.session_name)
        if ns is None or ns.status == "ended":
            return False
        # The state read at submission is stale after a wait: take the latest session id.
        bg_task.resume_session_id = ns.session_id
        bg_task.message_count = ns.message_count
        if (ns.provider, ns.model) != (bg_task.provider, bg_task.model):
            # Recreated with another provider while waiting: the id belongs to this target.
            bg_task.provider = ns.provider
            bg_task.model = ns.model
            bg_task.reasoning_effort = ns.reasoning_effort
        return True

    async def _run_with_session(self, bg_task: BackgroundTask) -> None:
        """Named session execution via CLIService with resume support."""
        from ductor_bot.cli.types import AgentRequest

        assert self._cli_service is not None

        t0 = time.monotonic()
        process_label = f"ns:{bg_task.session_name}"
        try:
            if not await self._admit_session_task(bg_task):
                await self._finish_session_task(
                    bg_task,
                    _make_result(
                        bg_task,
                        t0,
                        result_text="",
                        status="aborted",
                        session_name=bg_task.session_name,
                    ),
                )
                return
            files_block = await build_appended_files_block(
                self._paths, self._config.append_system_prompt_files
            )
            from ductor_bot.orchestrator.hooks import persona_setup_suffix

            setup_hint = await persona_setup_suffix(
                self._config,
                self._paths,
                message_count=bg_task.message_count,
                is_new_session=not bg_task.resume_session_id,
                chat_id=bg_task.chat_id,
            )
            request = AgentRequest(
                prompt=f"{bg_task.prompt}\n\n{setup_hint}" if setup_hint else bg_task.prompt,
                received_context=current_input_context(self._config.user_timezone),
                append_system_prompt=files_block,
                model_override=bg_task.model or None,
                provider_override=bg_task.provider or None,
                effort_override=bg_task.reasoning_effort or None,
                chat_id=bg_task.chat_id,
                topic_id=bg_task.thread_id,
                transport=bg_task.transport,
                process_label=process_label,
                session_kind="named",
                session_name=bg_task.session_name,
                resume_session=bg_task.resume_session_id or None,
                timeout_seconds=self._timeout_seconds,
            )
            response = await self._cli_service.execute(request)

            status = "ok"
            if response.is_error:
                status = "error:cli"
                if response.timed_out:
                    status = "error:timeout"

            await self._finish_session_task(
                bg_task,
                _make_result(
                    bg_task,
                    t0,
                    result_text=response.result or "",
                    status=status,
                    session_name=bg_task.session_name,
                    session_id=response.session_id or "",
                ),
            )
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await self._finish_session_task(
                    bg_task,
                    _make_result(
                        bg_task,
                        t0,
                        result_text="",
                        status="aborted",
                        session_name=bg_task.session_name,
                    ),
                )
            raise
        except Exception:
            logger.exception(
                "Named session task failed id=%s name=%s", bg_task.task_id, bg_task.session_name
            )
            with contextlib.suppress(Exception):
                await self._finish_session_task(
                    bg_task,
                    _make_result(
                        bg_task,
                        t0,
                        result_text=t("tasks.internal_error"),
                        status="error:internal",
                        session_name=bg_task.session_name,
                    ),
                )

    async def _deliver(self, result: BackgroundResult) -> None:
        if self._on_result is None:
            logger.warning("No result handler set for background task %s", result.task_id)
            return
        try:
            await self._on_result(result)
        except Exception:
            logger.exception("Error delivering background result id=%s", result.task_id)
