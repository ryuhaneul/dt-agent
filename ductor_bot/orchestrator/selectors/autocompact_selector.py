"""Autocompact controls shared by Telegram and web inline selectors."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import nullcontext
from typing import TYPE_CHECKING

from ductor_bot.cli.autocompact import resolve_autocompact, validate_autocompact
from ductor_bot.config import update_config_file_async
from ductor_bot.i18n import t
from ductor_bot.multiagent.registry import update_agent_fields
from ductor_bot.orchestrator.selectors.models import Button, ButtonGrid, SelectorResponse

if TYPE_CHECKING:
    from ductor_bot.config import AutocompactValue
    from ductor_bot.orchestrator.core import Orchestrator
    from ductor_bot.session.key import SessionKey

logger = logging.getLogger(__name__)
AC_PREFIX = "ac:"
# Same two-minute deadline as Telegram SecretInput.PENDING_SECONDS.
INPUT_SECONDS = 120


def is_autocompact_callback(data: str) -> bool:
    return data.startswith(AC_PREFIX)


def format_autocompact(value: AutocompactValue | None) -> str:
    if value is None:
        return t("autocompact.inherit")
    if isinstance(value, int):
        return t("autocompact.tokens", count=f"{value:,}")
    return t(f"autocompact.{value}")


async def autocompact_selector_start(
    orch: Orchestrator, key: SessionKey, scope: str = "topic"
) -> SelectorResponse:
    session = await orch._sessions.get_active(key)
    topic_value = session.autocompact if session else None
    global_value = orch._config.autocompact
    effective = resolve_autocompact(topic_value, global_value)
    provider = session.provider if session else orch.resolve_runtime_target(orch._config.model)[1]
    text = t(
        "autocompact.status",
        agent=orch._agent_name,
        provider=provider,
        topic=format_autocompact(topic_value),
        global_value=format_autocompact(global_value),
        effective=format_autocompact(effective),
    )
    if provider in {"grok", "gemini"}:
        text += "\n\n" + t("autocompact.unsupported", provider=provider)
    elif provider == "codex" and "off" in (topic_value, global_value):
        text += "\n\n" + t("autocompact.off_unsupported", provider=provider)
    text += "\n\n" + t("autocompact.memory_warning")
    text += "\n\n" + t(f"autocompact.scope_{scope}")
    current = topic_value if scope == "topic" else global_value

    def option(mode: str) -> Button:
        value = None if mode == "inherit" else mode
        label = t(f"autocompact.{mode}")
        if current == value:
            label = f"✓ {label}"
        return Button(label, f"ac:{scope}:{mode}")

    rows = []
    if scope == "topic":
        rows.append([option("inherit"), option("default")])
        rows.append([option("off")])
    else:
        rows.append([option("default"), option("off")])
    rows += [
        [Button(t("autocompact.input"), f"ac:{scope}:input")],
        [Button(t("autocompact.cancel"), "ac:cancel")],
        [
            Button(
                t("autocompact.change_global" if scope == "topic" else "autocompact.change_topic"),
                "ac:scope:global" if scope == "topic" else "ac:scope:topic",
            )
        ],
    ]
    return SelectorResponse(text, ButtonGrid(rows))


async def save_autocompact(
    orch: Orchestrator,
    key: SessionKey,
    scope: str,
    value: AutocompactValue | None,
    *,
    _global_lock_held: bool = False,
) -> SelectorResponse:
    """Save one scope; a batched caller can retain the global lock across rollback."""
    try:
        if scope == "topic":
            await orch._sessions.set_autocompact(key, value)
        else:
            concrete = validate_autocompact(value)
            async with nullcontext() if _global_lock_held else orch._autocompact_global_lock:
                await update_config_file_async(orch.paths.config_path, autocompact=concrete)
                if orch.paths.ductor_home.parent.name == "agents":
                    await asyncio.to_thread(
                        update_agent_fields,
                        orch.paths.ductor_home.parent.parent / "agents.json",
                        orch._agent_name,
                        autocompact=concrete,
                    )
                orch._config.autocompact = concrete
                orch._on_config_hot_reload(orch._config, {"autocompact": concrete})
    except (OSError, ValueError) as exc:
        logger.warning("Autocompact save failed: %s", type(exc).__name__)
        return SelectorResponse(t("autocompact.save_error"))
    result = await autocompact_selector_start(orch, key, scope)
    return SelectorResponse(
        t(
            "autocompact.saved",
            scope=t(f"autocompact.scope_{scope}"),
            value=format_autocompact(value),
        )
        + "\n\n"
        + result.text,
        result.buttons,
    )


def _clear_input(orch: Orchestrator, key: SessionKey, user_id: int | None) -> None:
    if user_id is not None:
        orch._autocompact_inputs.pop((key, user_id), None)


def _begin_input(
    orch: Orchestrator, key: SessionKey, scope: str, user_id: int | None
) -> SelectorResponse:
    if user_id is None:
        return SelectorResponse(t("autocompact.web_input"))
    identity = (key, user_id)
    pending = (scope, time.monotonic() + INPUT_SECONDS)
    orch._autocompact_inputs[identity] = pending

    def expire() -> None:
        if orch._autocompact_inputs.get(identity) == pending:
            orch._autocompact_inputs.pop(identity, None)

    asyncio.get_running_loop().call_later(INPUT_SECONDS, expire)
    return SelectorResponse(
        t("autocompact.input_prompt", scope=t(f"autocompact.scope_{scope}")),
        ButtonGrid([[Button(t("autocompact.cancel"), "ac:cancel")]]),
    )


async def handle_autocompact_callback(
    orch: Orchestrator, key: SessionKey, data: str, *, user_id: int | None = None
) -> SelectorResponse:
    if data == "ac:cancel":
        _clear_input(orch, key, user_id)
        return SelectorResponse(t("autocompact.cancelled"))
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "ac":
        return SelectorResponse(t("autocompact.invalid_action"))
    _, scope, action = parts
    if scope == "scope" and action in {"topic", "global"}:
        _clear_input(orch, key, user_id)
        return await autocompact_selector_start(orch, key, action)
    valid = scope in {"topic", "global"} and (
        action in {"default", "off", "input"} or (scope == "topic" and action == "inherit")
    )
    if not valid:
        return SelectorResponse(t("autocompact.invalid_action"))
    if action == "input":
        return _begin_input(orch, key, scope, user_id)
    _clear_input(orch, key, user_id)
    value = None if action == "inherit" else validate_autocompact(action)
    return await save_autocompact(orch, key, scope, value)


async def consume_autocompact_input(
    orch: Orchestrator, key: SessionKey, text: str, *, user_id: int
) -> SelectorResponse | None:
    identity = (key, user_id)
    pending = orch._autocompact_inputs.get(identity)
    if pending is None:
        return None
    scope, expires = pending
    if expires <= time.monotonic():
        orch._autocompact_inputs.pop(identity, None)
        return SelectorResponse(t("autocompact.expired"))
    text = text.strip()
    if text.lower().split("@", 1)[0] == "/cancel":
        orch._autocompact_inputs.pop(identity, None)
        return SelectorResponse(t("autocompact.cancelled"))
    if text.startswith("/"):
        return None
    try:
        value = validate_autocompact(int(text))
    except ValueError:
        return SelectorResponse(
            t("autocompact.invalid_input"),
            ButtonGrid(
                [
                    [Button(t("autocompact.cancel"), "ac:cancel")],
                ]
            ),
        )
    result = await save_autocompact(orch, key, scope, value)
    # On a write error retain the pending input so the owner can retry or cancel.
    if result.buttons is not None:
        orch._autocompact_inputs.pop(identity, None)
    return result
