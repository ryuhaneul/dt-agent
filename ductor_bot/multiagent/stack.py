"""AgentStack: encapsulates a complete bot stack for one agent."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ductor_bot.config import AgentConfig
from ductor_bot.workspace.init import init_workspace
from ductor_bot.workspace.paths import DuctorPaths, resolve_paths

if TYPE_CHECKING:
    from ductor_bot.infra.shutdown_budget import ShutdownBudget
    from ductor_bot.messenger.protocol import BotProtocol

logger = logging.getLogger(__name__)


@dataclass
class AgentStack:
    """Container for one agent's entire bot stack.

    Each agent gets its own Bot → Orchestrator → CLIService pipeline,
    its own workspace, sessions, cron jobs, and webhooks.
    """

    name: str
    config: AgentConfig
    paths: DuctorPaths
    bot: BotProtocol
    is_main: bool = False

    @classmethod
    async def create(
        cls,
        name: str,
        config: AgentConfig,
        *,
        is_main: bool = False,
    ) -> AgentStack:
        """Factory: initialize workspace and create the transport-specific bot.

        The workspace is seeded (Zone 2 + 3) and the bot created,
        but the event loop is NOT started yet — call ``run()`` for that.
        """
        import asyncio

        paths = resolve_paths(ductor_home=config.ductor_home)
        await asyncio.to_thread(init_workspace, paths)

        from ductor_bot.messenger.registry import create_bot

        bot = create_bot(config, agent_name=name)

        logger.info(
            "AgentStack created: name=%s home=%s main=%s transport=%s",
            name,
            paths.ductor_home,
            is_main,
            config.transport,
        )
        return cls(name=name, config=config, paths=paths, bot=bot, is_main=is_main)

    async def run(self) -> int:
        """Start the bot (blocks until stop/crash).

        Returns exit code (0 = normal, 42 = restart requested).
        """
        return await self.bot.run()

    async def shutdown(self, budget: ShutdownBudget | None = None) -> None:
        """Gracefully shut down the bot and all observers (waits capped by *budget*)."""
        await self.bot.shutdown(budget)
        logger.info("AgentStack '%s' shut down", self.name)
