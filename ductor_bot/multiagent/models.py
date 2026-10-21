"""Data models for multi-agent configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from ductor_bot.config import (
    AgentConfig,
    ApiConfig,
    CleanupConfig,
    CLIParametersConfig,
    HeartbeatConfig,
    InteragentConfig,
    MatrixConfig,
    MemoryLintConfig,
    StreamingConfig,
    WebhookConfig,
    matrix_credentials_complete,
    safe_matrix_store,
)
from ductor_bot.messenger.matrix.credentials import load_matching_credentials


class SubAgentConfig(BaseModel):
    """Minimal sub-agent definition from agents.json.

    Only ``name`` is strictly required. Telegram agents need ``telegram_token``
    and ``allowed_user_ids``; Matrix agents need ``matrix`` config.
    All other fields are optional and inherit from the main agent config.
    """

    name: str
    transport: str = "telegram"  # "telegram" | "matrix" | "headless"

    # Telegram credentials (required when transport=telegram)
    telegram_token: str = ""
    allowed_user_ids: list[int] | None = None
    allowed_group_ids: list[int] | None = None

    # Matrix credentials (required when transport=matrix)
    matrix: MatrixConfig | None = None

    # Group behaviour
    group_mention_only: bool | None = None

    # Optional overrides — inherit from main agent if None
    provider: str | None = None
    model: str | None = None
    log_level: str | None = None
    idle_timeout_minutes: int | None = None
    session_age_warning_hours: int | None = None
    daily_reset_hour: int | None = None
    daily_reset_enabled: bool | None = None
    max_budget_usd: float | None = None
    max_turns: int | None = None
    max_session_messages: int | None = None
    permission_mode: str | None = None
    cli_timeout: float | None = None
    reasoning_effort: str | None = None
    file_access: str | None = None
    streaming: StreamingConfig | None = None
    heartbeat: HeartbeatConfig | None = HeartbeatConfig(enabled=False)
    cleanup: CleanupConfig | None = None
    webhooks: WebhookConfig | None = None
    api: ApiConfig | None = None
    cli_parameters: CLIParametersConfig | None = None
    user_timezone: str | None = None
    interagent_notify_received: bool | None = None
    # Never inherited from main: framework defaults plus this (partial) override.
    memory_lint: MemoryLintConfig | None = None
    # Partial override of main's ``interagent`` limits (only the keys given here differ).
    interagent: InteragentConfig | None = None
    claude_system_prompt_snapshot: Literal["off", "on"] | None = None


def resolve_sub_transports(sub: SubAgentConfig, agent_home: Path) -> list[str]:
    """Choose transports from this sub-agent's own usable credentials."""
    if "transport" in sub.model_fields_set and sub.transport == "headless":
        return ["headless"]

    has_telegram = bool(sub.telegram_token.strip())
    has_matrix = False
    if sub.matrix is not None:
        matrix = sub.matrix
        store = safe_matrix_store(agent_home, matrix.store_path)
        has_matrix = matrix_credentials_complete(matrix)
        if not has_matrix and matrix.homeserver.strip() and matrix.user_id.strip():
            has_matrix = load_matching_credentials(store / "credentials.json", matrix) is not None

    if "transport" in sub.model_fields_set:
        if sub.transport == "telegram" and has_telegram:
            return ["telegram"]
        if sub.transport == "matrix" and has_matrix:
            return ["matrix"]
        raise ValueError(
            f"Sub-agent '{sub.name}' has no usable credentials for transport '{sub.transport}'"
        )

    chosen = []
    if has_telegram:
        chosen.append("telegram")
    if has_matrix:
        chosen.append("matrix")
    return chosen or ["headless"]


def merge_sub_agent_config(
    main: AgentConfig,
    sub: SubAgentConfig,
    agent_home: Path,
) -> AgentConfig:
    """Create a full AgentConfig by merging main config with sub-agent overrides.

    Merge: main agent defaults → ``agents.json`` explicit overrides (non-None). The one
    exception is ``memory_lint``, which starts from the framework defaults, not from main.

    ``switch_model()`` keeps ``agents.json`` up-to-date when the user changes
    model/provider/reasoning_effort in a sub-agent chat, so no extra config
    layer is needed.
    """
    base = main.model_dump()

    # agents.json explicit overrides (non-None fields win)
    overrides = sub.model_dump(exclude_none=True, exclude={"name"})
    base.update(overrides)
    # ``interagent`` is a partial override over main's value, key by key.
    base["interagent"] = {
        **main.interagent.model_dump(),
        **(sub.interagent.model_dump(exclude_unset=True) if sub.interagent else {}),
    }
    # A sub never inherits main's memory_lint (a main in "full" must not make every sub run a
    # full lint turn): framework defaults, overlaid with the sub's own optional override.
    base["memory_lint"] = (sub.memory_lint or MemoryLintConfig()).model_dump()

    chosen = resolve_sub_transports(sub, agent_home)
    base["ductor_home"] = str(agent_home)
    base["transports"] = chosen
    base["transport"] = chosen[0]
    base["telegram_token"] = sub.telegram_token
    base["allowed_user_ids"] = sub.allowed_user_ids or []
    base["allowed_group_ids"] = sub.allowed_group_ids or []
    if sub.matrix is not None:
        base["matrix"] = sub.matrix.model_dump()
    # An inherited main Matrix block is inert when "matrix" is not chosen.

    # Sub-agents don't need the user-facing API server (they use InterAgentBus).
    # Disable it unless the sub-agent explicitly provides an api config.
    if sub.api is None:
        base.setdefault("api", {})["enabled"] = False

    return AgentConfig(**base)
