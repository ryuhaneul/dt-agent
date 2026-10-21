"""Messenger-free bot for sub-agents that only receive delegated work."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

from ductor_bot.bus.bus import MessageBus
from ductor_bot.files.allowed_roots import resolve_allowed_roots
from ductor_bot.messenger.notifications import NotificationService
from ductor_bot.orchestrator.core import Orchestrator

if TYPE_CHECKING:
    from ductor_bot.bus.envelope import DeliveryOutcome
    from ductor_bot.bus.lock_pool import LockPool
    from ductor_bot.config import AgentConfig
    from ductor_bot.infra.shutdown_budget import ShutdownBudget
    from ductor_bot.multiagent.bus import AsyncInterAgentResult
    from ductor_bot.tasks.models import TaskResult
    from ductor_bot.workspace.paths import DuctorPaths

logger = logging.getLogger(__name__)


class _SilentNotificationService:
    transport_name = "headless"

    async def notify_origin(
        self, transport: str, chat_id: int, text: str, *, topic_id: int | None = None
    ) -> bool:
        """Headless agents have no chat destination."""
        return False

    async def notify(self, chat_id: int, text: str) -> None:
        """Headless agents have no chat destination."""

    async def notify_all(self, text: str) -> None:
        """Headless agents have no chat destination."""


class HeadlessBot:
    """Run an orchestrator and inter-agent bus without a messenger."""

    def __init__(
        self,
        config: AgentConfig,
        *,
        agent_name: str = "main",
        bus: MessageBus | None = None,
        lock_pool: LockPool | None = None,
    ) -> None:
        self._config = config
        self._agent_name = agent_name
        self._bus = bus if bus is not None else MessageBus(lock_pool=lock_pool)
        self._orchestrator: Orchestrator | None = None
        self._notification_service: NotificationService = _SilentNotificationService()
        self._startup_hooks: list[Callable[[], Awaitable[None]]] = []
        self._abort_all_callback: Callable[[], Awaitable[int]] | None = None
        self._stopped = asyncio.Event()

    @property
    def orchestrator(self) -> Orchestrator | None:
        return self._orchestrator

    @property
    def config(self) -> AgentConfig:
        return self._config

    @property
    def notification_service(self) -> NotificationService:
        return self._notification_service

    async def run(self) -> int:
        self._orchestrator = await Orchestrator.create(
            self._config, agent_name=self._agent_name, lock_pool=self._bus.lock_pool
        )
        self._orchestrator.wire_observers_to_bus(self._bus)
        for hook in self._startup_hooks:
            await hook()
        await self._stopped.wait()
        return 0

    def request_stop(self) -> bool:
        self._stopped.set()
        return True

    async def shutdown(self, budget: ShutdownBudget | None = None) -> None:
        self._stopped.set()
        if self._orchestrator is not None:
            await self._orchestrator.shutdown(budget)

    def register_startup_hook(self, hook: Callable[[], Awaitable[None]]) -> None:
        self._startup_hooks.append(hook)

    def set_abort_all_callback(self, callback: Callable[[], Awaitable[int]]) -> None:
        self._abort_all_callback = callback

    async def on_async_interagent_result(self, result: AsyncInterAgentResult) -> DeliveryOutcome:
        from ductor_bot.bus.envelope import DeliveryOutcome

        logger.error("Headless agent cannot receive async result task=%s", result.task_id)
        return DeliveryOutcome(
            ok=False, injected=False, delivered=False, error="recipient is headless"
        )

    async def on_task_result(self, result: TaskResult) -> None:
        logger.warning("Headless agent received task result (task=%s)", result.task_id)

    async def notify_task_interrupted(self, result: TaskResult) -> bool:
        logger.warning("Headless agent cannot report interrupted task %s", result.task_id)
        return False

    async def on_task_question(  # noqa: PLR0913, PLR0917
        self,
        task_id: str,
        question: str,
        prompt_preview: str,
        chat_id: int,
        thread_id: int | None = None,
        transport: str = "",
    ) -> None:
        logger.warning("Headless agent received task question (task=%s)", task_id)

    def file_roots(self, paths: DuctorPaths) -> list[Path] | None:
        return resolve_allowed_roots(self._config.file_access, paths.workspace)
