"""Apply the configured provider/model/effort to the "Memory Lint" topic session."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ductor_bot.cli.param_resolver import TaskOverrides, resolve_cli_config
from ductor_bot.errors import DuctorError

if TYPE_CHECKING:
    from ductor_bot.config import MemoryLintConfig
    from ductor_bot.orchestrator.core import Orchestrator
    from ductor_bot.session.key import SessionKey

logger = logging.getLogger(__name__)


def has_target(cfg: MemoryLintConfig) -> bool:
    """True when any of provider/model/reasoning_effort is configured."""
    return bool(cfg.provider or cfg.model or cfg.reasoning_effort)


async def apply_lint_target(
    orch: Orchestrator, key: SessionKey, cfg: MemoryLintConfig
) -> str | None:
    """Point the session of *key* at the configured provider/model/effort.

    Unset parts keep the session's current value (a ``/model`` change in the topic survives for
    them). Values are validated like cron job overrides. Returns ``None`` when applied or nothing
    is configured, otherwise a message describing why the session was left unchanged.
    """
    if not has_target(cfg):
        return None
    default_model, default_provider = orch.resolve_runtime_target(None)
    session, _ = await orch._sessions.resolve_session(
        key,
        provider=default_provider,
        model=default_model,
        preserve_existing_target=True,
    )
    provider = cfg.provider or (
        orch.resolve_runtime_target(cfg.model)[1] if cfg.model else session.provider
    )
    if cfg.model:
        model = cfg.model
    elif provider != session.provider:
        model = orch._providers.default_model_for_provider(provider)
    else:
        model = session.model
    if not model:
        return f"no default model known for provider {provider}"
    try:
        resolved = resolve_cli_config(
            orch._config,
            orch._observers.codex_cache,
            task_overrides=TaskOverrides(
                provider=provider, model=model, reasoning_effort=cfg.reasoning_effort
            ),
        )
    except DuctorError as exc:
        return str(exc)
    await orch._sessions.sync_session_target(
        session,
        provider=resolved.provider,
        model=resolved.model,
        reasoning_effort=resolved.reasoning_effort if cfg.reasoning_effort else None,
    )
    return None
