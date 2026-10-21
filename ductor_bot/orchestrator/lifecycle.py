"""Orchestrator lifecycle: async factory, startup, shutdown, infra management."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
from functools import partial
from typing import TYPE_CHECKING

from ductor_bot.files.allowed_roots import resolve_allowed_roots
from ductor_bot.infra.restart import register_restart_hook, unregister_restart_hook
from ductor_bot.infra.shutdown_budget import ShutdownBudget, wait_capped
from ductor_bot.workspace.init import inject_runtime_environment
from ductor_bot.workspace.paths import DuctorPaths, resolve_paths
from ductor_bot.workspace.skill_sync import cleanup_ductor_links

if TYPE_CHECKING:
    from ductor_bot.bus.lock_pool import LockPool
    from ductor_bot.config import AgentConfig
    from ductor_bot.orchestrator.core import Orchestrator

logger = logging.getLogger(__name__)


async def create_orchestrator(
    config: AgentConfig,
    *,
    agent_name: str = "main",
    lock_pool: LockPool | None = None,
) -> Orchestrator:
    """Async factory: build an Orchestrator.

    Workspace must already be initialized by the caller (``__main__.load_config``).
    """
    from ductor_bot.orchestrator.core import Orchestrator

    paths = resolve_paths(ductor_home=config.ductor_home)

    # Only set the process-wide env var for the main agent to avoid
    # race conditions in multi-agent mode (sub-agents use per-subprocess env).
    if agent_name == "main":
        os.environ["DUCTOR_HOME"] = str(paths.ductor_home)

    await asyncio.to_thread(
        inject_runtime_environment,
        paths,
        agent_name=agent_name,
        transport=config.transport,
    )

    orch = Orchestrator(
        config,
        paths,
        agent_name=agent_name,
        interagent_port=config.interagent_port,
        lock_pool=lock_pool,
    )
    register_restart_hook(paths.ductor_home, partial(begin_shutdown, orch))

    from ductor_bot.cli.auth import AuthStatus, check_all_auth

    auth_results = await asyncio.to_thread(check_all_auth)
    orch._providers.apply_auth_results(
        auth_results,
        auth_status_enum=AuthStatus,
        cli_service=orch._cli_service,
    )

    if not orch._providers.available_providers:
        logger.error("No authenticated providers found! CLI calls will fail.")
    else:
        logger.info(
            "Available providers: %s",
            ", ".join(sorted(orch._providers.available_providers)),
        )

    await asyncio.to_thread(orch._providers.init_gemini_state, paths.workspace)

    installed_providers = frozenset(
        name for name, result in auth_results.items() if result.status is not AuthStatus.NOT_FOUND
    )
    codex_cache = await orch._observers.init_model_caches(
        installed_providers=installed_providers,
        on_gemini_refresh=orch._providers.on_gemini_models_refresh,
        on_grok_refresh=orch._providers.on_grok_models_refresh,
    )
    orch._observers.init_task_observers(
        cron_manager=orch._cron_manager,
        webhook_manager=orch._webhook_manager,
        cli_service=orch._cli_service,
        codex_cache=codex_cache,
    )
    orch._providers._codex_cache_fn = lambda: orch._observers.codex_cache
    await orch._observers.start_all()

    # Direct API server (WebSocket, designed for Tailscale)
    if config.api.enabled:
        await start_api_server(orch, config, paths)

    await orch._observers.start_config_reloader(
        on_hot_reload=orch._on_config_hot_reload,
        on_restart_needed=lambda fields: logger.warning(
            "Config changed but requires restart: %s", ", ".join(fields)
        ),
    )

    return orch


async def start_api_server(
    orch: Orchestrator,
    config: AgentConfig,
    paths: DuctorPaths,
) -> None:
    """Initialize and start the direct WebSocket API server."""
    try:
        from ductor_bot.api.server import ApiServer
    except ImportError:
        logger.warning(
            "feature api is on but module nacl.public is missing. Add api to DUCTOR_EXTRAS in the root "
            ".env, then run docker compose build"
        )
        return

    if not config.api.token:
        from ductor_bot.config import update_config_file_async

        token = secrets.token_urlsafe(32)
        config.api.token = token
        await update_config_file_async(
            paths.config_path,
            api={**config.api.model_dump(), "token": token},
        )
        logger.info("Generated API auth token (persisted to config)")

    default_chat_id = config.api.chat_id or (
        config.allowed_user_ids[0] if config.allowed_user_ids else 1
    )
    server = ApiServer(config.api, default_chat_id=default_chat_id, lock_pool=orch._lock_pool)
    # The API keeps the legacy session key while its CLI turn carries API origin.
    server.set_message_handler(partial(orch.handle_message_streaming, request_transport="api"))
    server.set_abort_handler(orch.abort)
    server.set_file_context(
        allowed_roots=resolve_allowed_roots(config.file_access, paths.workspace),
        upload_dir=paths.api_files_dir,
        workspace=paths.workspace,
    )
    server.set_provider_info(orch._providers.build_provider_info(orch._observers.codex_cache_obs))
    server.set_active_state_getter(
        lambda: orch._providers.resolve_runtime_target(orch._config.model)
    )

    try:
        await server.start()
    except OSError:
        logger.exception(
            "Failed to start API server on %s:%d",
            config.api.host,
            config.api.port,
        )
        return

    orch._api_stop = server.stop


def begin_shutdown(orch: Orchestrator) -> None:
    """Mark the shutdown start; must run before any task cancel or CLI kill.

    A foreground turn cut off by the shutdown keeps its in-flight record (so the next start
    can tell the user) — unless the user had stopped it themselves before this point.
    """
    tracker = orch._inflight_tracker
    registry = orch._process_registry
    stopped_chats = registry.stopped_chats()
    stopped_topics = registry.stopped_topics()
    tracker.mark_shutting_down(
        (chat, topic)
        for chat, topic in tracker.active_scopes()
        if chat in stopped_chats or (chat, topic) in stopped_topics
    )


async def run_pre_shutdown_hooks(orch: Orchestrator, budget: ShutdownBudget | None = None) -> None:
    """Run the hooks that must see the CLI processes still alive; only the first call runs them."""
    if orch._pre_shutdown_ran:
        return
    orch._pre_shutdown_ran = True
    budget = budget or ShutdownBudget()
    for hook in list(orch._pre_shutdown_hooks):
        await wait_capped(hook(), budget.remaining(), label="pre-shutdown hook")


async def shutdown(orch: Orchestrator, budget: ShutdownBudget | None = None) -> None:
    """Cleanup on bot shutdown. Each wait is capped by the shared *budget*."""
    budget = budget or ShutdownBudget()
    begin_shutdown(orch)
    unregister_restart_hook(orch.paths.ductor_home)
    await run_pre_shutdown_hooks(orch, budget)
    killed = await orch._process_registry.kill_all_active(budget)
    if killed:
        logger.info("Shutdown terminated %d active CLI process(es)", killed)
    if orch._api_stop is not None:
        await wait_capped(orch._api_stop(), budget.remaining(), label="api stop")
    await asyncio.to_thread(cleanup_ductor_links, orch._paths)
    await wait_capped(orch._observers.stop_all(), budget.remaining(), label="observers stop")
    logger.info("Orchestrator shutdown")
