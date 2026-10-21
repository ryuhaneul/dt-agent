"""MultiBotAdapter: wraps multiple transport bots behind a single BotProtocol."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

from ductor_bot.bus.bus import MessageBus
from ductor_bot.bus.lock_pool import LockPool
from ductor_bot.infra.shutdown_budget import ShutdownBudget
from ductor_bot.messenger.notifications import CompositeNotificationService

if TYPE_CHECKING:
    from ductor_bot.bus.envelope import DeliveryOutcome
    from ductor_bot.config import AgentConfig
    from ductor_bot.messenger.notifications import NotificationService
    from ductor_bot.messenger.protocol import BotProtocol
    from ductor_bot.multiagent.bus import AsyncInterAgentResult
    from ductor_bot.orchestrator.core import Orchestrator
    from ductor_bot.tasks.models import TaskResult
    from ductor_bot.workspace.paths import DuctorPaths

logger = logging.getLogger(__name__)


class MultiBotAdapter:
    """Wraps multiple transport bots into a single BotProtocol facade.

    The **primary** bot (first transport) creates the orchestrator during
    startup.  Secondary bots receive the orchestrator before their ``run()``
    is called, so their startup skips orchestrator creation.

    All bots share a single ``MessageBus`` and ``LockPool``.
    """

    def __init__(
        self,
        config: AgentConfig,
        *,
        agent_name: str = "main",
    ) -> None:
        from ductor_bot.messenger.registry import _create_single_bot

        self._config = config
        self._lock_pool = LockPool()
        self._bus = MessageBus(lock_pool=self._lock_pool)

        transports = config.transports
        if not transports:
            msg = "MultiBotAdapter requires at least one transport"
            raise ValueError(msg)

        bots: list[BotProtocol] = []
        for transport_name in transports:
            bot = _create_single_bot(
                transport_name,
                config,
                agent_name=agent_name,
                bus=self._bus,
                lock_pool=self._lock_pool,
            )
            bots.append(bot)

        self._primary: BotProtocol = bots[0]
        self._secondaries: list[BotProtocol] = bots[1:]
        self._all: list[BotProtocol] = bots
        self._transports = transports

        self._notification_service = CompositeNotificationService()
        for bot in self._all:
            self._notification_service.add(bot.notification_service)

    # -- BotProtocol: properties delegated to primary --------------------------

    @property
    def orchestrator(self) -> Orchestrator | None:
        return self._primary.orchestrator

    @property
    def config(self) -> AgentConfig:
        return self._config

    @property
    def notification_service(self) -> NotificationService:
        return self._notification_service

    # -- BotProtocol: methods delegated to primary -----------------------------

    def register_startup_hook(self, hook: Callable[[], Awaitable[None]]) -> None:
        self._primary.register_startup_hook(hook)

    def set_abort_all_callback(self, callback: Callable[[], Awaitable[int]]) -> None:
        for bot in self._all:
            bot.set_abort_all_callback(callback)

    # -- BotProtocol: methods that fan out to all bots -------------------------

    async def on_async_interagent_result(self, result: AsyncInterAgentResult) -> DeliveryOutcome:
        from ductor_bot.bus.envelope import DeliveryOutcome

        if result.result_origin == "cron-broadcast" and not result.chat_id:
            return await self._primary.on_async_interagent_result(result)

        transport_name = {"tg": "telegram", "mx": "matrix", "sl": "slack"}.get(
            result.transport, self._transports[0] if not result.transport else ""
        )
        for name, bot in zip(self._transports, self._all, strict=True):
            if name == transport_name:
                return await bot.on_async_interagent_result(result)
        return DeliveryOutcome(
            ok=False, injected=False, delivered=False, error="transport unavailable"
        )

    async def on_task_result(self, result: TaskResult) -> None:
        for bot in self._all:
            await bot.on_task_result(result)

    async def notify_task_interrupted(self, result: TaskResult) -> bool:
        """Only the primary bot's transport carries the notice (all bots share the bus)."""
        return await self._primary.notify_task_interrupted(result)

    async def on_task_question(  # noqa: PLR0913, PLR0917
        self,
        task_id: str,
        question: str,
        prompt_preview: str,
        chat_id: int,
        thread_id: int | None = None,
        transport: str = "",
    ) -> None:
        transport_name = {"tg": "telegram", "mx": "matrix", "sl": "slack"}.get(transport)
        for name, bot in zip(self._transports, self._all, strict=True):
            if name == transport_name:
                await bot.on_task_question(
                    task_id, question, prompt_preview, chat_id, thread_id, transport
                )
                return
        for bot in self._all:
            await bot.on_task_question(
                task_id, question, prompt_preview, chat_id, thread_id, transport
            )

    def file_roots(self, paths: DuctorPaths) -> list[Path] | None:
        return self._primary.file_roots(paths)

    # -- BotProtocol: run / shutdown -------------------------------------------

    def request_stop(self) -> bool:
        """Ask every bot to end on its own. True only if each of them can."""
        results = [bot.request_stop() for bot in self._all]  # every bot gets asked
        return all(results)

    async def run(self) -> int:
        """Start all bots: primary first, then secondaries after orchestrator is ready.

        Returns exit code from the first bot that finishes (e.g. 42 for restart). When this
        coroutine is cancelled, every bot task is cancelled and awaited (each stops its own
        polling) before the cancellation goes on, also while waiting for the primary startup.
        """
        orch_ready = asyncio.Event()

        async def _signal_ready() -> None:
            orch_ready.set()

        self._primary.register_startup_hook(_signal_ready)

        started: list[asyncio.Task[int]] = []
        try:
            primary_task = asyncio.create_task(self._primary.run(), name="multi:primary")
            started.append(primary_task)

            await orch_ready.wait()

            # Inject the primary's orchestrator into secondary bots.
            for bot in self._secondaries:
                bot._orchestrator = self._primary.orchestrator  # type: ignore[attr-defined]

            for i, bot in enumerate(self._secondaries):
                started.append(asyncio.create_task(bot.run(), name=f"multi:secondary:{i}"))

            done, pending = await asyncio.wait(started, return_when=asyncio.FIRST_COMPLETED)

            for task in pending:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        except asyncio.CancelledError:
            for task in started:
                task.cancel()
            await asyncio.gather(*started, return_exceptions=True)
            raise

        for task in done:
            return task.result()
        return 0

    async def shutdown(self, budget: ShutdownBudget | None = None) -> None:
        """Shut down all bots, one after the other, within the shared *budget*."""
        budget = budget or ShutdownBudget()
        for bot in self._all:
            await bot.shutdown(budget)
