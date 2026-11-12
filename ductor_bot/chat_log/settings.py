"""Validation and activation for independent conversation recording."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ductor_bot.config import AgentConfig


def validate_chat_log(config: AgentConfig) -> list[str]:
    """Return operational errors without preventing the bot from starting."""
    if not config.chat_log.enabled:
        return []
    transports = config.transports or [config.transport]
    return [] if "telegram" in transports else ["telegram_required"]


def chat_log_active(config: AgentConfig, agent_name: str) -> bool:
    """Enable recording only for the main agent with valid settings."""
    return config.chat_log.enabled and agent_name == "main" and not validate_chat_log(config)
