"""Autocompact controls shared by Telegram and web inline selectors."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import nullcontext
from typing import TYPE_CHECKING, Any

from ductor_bot.cli.autocompact import resolve_autocompact, validate_autocompact
from ductor_bot.config import update_config_file_async
from ductor_bot.i18n import t
from ductor_bot.multiagent.registry import update_agent_fields
from ductor_bot.orchestrator.selectors.autocompact_automation import (
    automation_state,
    automation_target,
    automation_token,
    save_automation,
)
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
    rows.append([Button(t("autocompact.automation"), "ac:auto:list:0")])
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
        t("autocompact.input_prompt", scope=_input_scope(orch, scope)),
        ButtonGrid([[Button(t("autocompact.cancel"), "ac:cancel")]]),
    )


async def handle_autocompact_callback(  # noqa: PLR0911 -- selector action dispatch
    orch: Orchestrator, key: SessionKey, data: str, *, user_id: int | None = None
) -> SelectorResponse:
    if data == "ac:cancel":
        _clear_input(orch, key, user_id)
        return SelectorResponse(t("autocompact.cancelled"))
    if data.startswith("ac:auto:"):
        _clear_input(orch, key, user_id)
        return await _automation_callback(orch, key, data, user_id=user_id)
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
    if scope.startswith("auto:"):
        _, kind, token = scope.split(":")
        result = await _save_automation_selection(orch, kind, token, value)
    else:
        result = await save_autocompact(orch, key, scope, value)
    # On a write error retain the pending input so the owner can retry or cancel.
    if result.buttons is not None:
        orch._autocompact_inputs.pop(identity, None)
    return result


_AUTOMATION_PAGE_SIZE = 5


def _input_scope(orch: Orchestrator, scope: str) -> str:
    if not scope.startswith("auto:"):
        return t(f"autocompact.scope_{scope}")
    _, kind, token = scope.split(":")
    item = automation_target(orch, kind, token)
    return str(item["name"])[:120] if item else t("autocompact.automation")


def _automation_value(item: dict[str, Any]) -> str:
    value = format_autocompact(item["value"])
    if item["value"] is None:
        value += " → " + format_autocompact(item["effective_value"])
    return value


def automation_selector_start(
    orch: Orchestrator, page: int = 0, *, note: str = ""
) -> SelectorResponse:
    items = automation_state(orch)["items"]
    pages = max(1, (len(items) + _AUTOMATION_PAGE_SIZE - 1) // _AUTOMATION_PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    lines = [t("autocompact.automation")]
    if note:
        lines.append(note)
    rows: list[list[Button]] = []
    start = page * _AUTOMATION_PAGE_SIZE
    for index, item in enumerate(items[start : start + _AUTOMATION_PAGE_SIZE], start + 1):
        state = t("autocompact.enabled" if item["enabled"] else "autocompact.disabled")
        lines.append(
            t(
                "autocompact.automation_item",
                number=index,
                name=item["name"][:120],
                kind=t("autocompact." + item["kind"]),
                state=state,
                schedule=item["schedule"][:120],
                value=_automation_value(item),
            )
        )
        rows.append(
            [
                Button(
                    t("autocompact.change_item", number=index),
                    f"ac:auto:{item['kind']}:{automation_token(item['id'])}:open",
                )
            ]
        )
    if not items:
        lines.append(t("autocompact.automation_empty"))
    lines.append(t("autocompact.task_hint"))
    lines.append(t("autocompact.page", current=page + 1, total=pages))
    nav = []
    if page:
        nav.append(Button(t("autocompact.previous"), f"ac:auto:list:{page - 1}"))
    if page + 1 < pages:
        nav.append(Button(t("autocompact.next"), f"ac:auto:list:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([Button(t("autocompact.change_topic"), "ac:scope:topic")])
    rows.append([Button(t("autocompact.cancel"), "ac:cancel")])
    return SelectorResponse("\n\n".join(lines), ButtonGrid(rows))


def _automation_detail(orch: Orchestrator, kind: str, token: str) -> SelectorResponse:
    item = automation_target(orch, kind, token)
    if item is None:
        return automation_selector_start(orch, note=t("autocompact.list_changed"))
    prefix = f"ac:auto:{kind}:{token}:"
    modes = ["inherit", "default", "off"] if kind == "webhook" else ["default", "off"]
    rows = [
        [
            Button(
                ("✓ " if item["value"] == (None if mode == "inherit" else mode) else "")
                + t("autocompact." + mode),
                prefix + mode,
            )
            for mode in modes
        ]
    ]
    rows += [
        [Button(t("autocompact.input"), prefix + "input")],
        [Button(t("autocompact.automation"), "ac:auto:list:0")],
        [Button(t("autocompact.cancel"), "ac:cancel")],
    ]
    return SelectorResponse(
        item["name"][:120]
        + "\n\n"
        + _automation_value(item)
        + "\n\n"
        + t("autocompact.memory_warning"),
        ButtonGrid(rows),
    )


async def _save_automation_selection(
    orch: Orchestrator, kind: str, token: str, value: AutocompactValue | None
) -> SelectorResponse:
    item = automation_target(orch, kind, token)
    if item is None:
        return automation_selector_start(orch, note=t("autocompact.list_changed"))
    try:
        if not save_automation(orch, kind, item["id"], value):
            return automation_selector_start(orch, note=t("autocompact.list_changed"))
    except (OSError, ValueError):
        logger.warning("Automation autocompact save failed")
        return SelectorResponse(t("autocompact.save_error"))
    result = _automation_detail(orch, kind, token)
    return SelectorResponse(
        t("autocompact.saved", scope=item["name"][:120], value=format_autocompact(value))
        + "\n\n"
        + result.text,
        result.buttons,
    )


async def _automation_callback(  # noqa: PLR0911 -- selector action dispatch
    orch: Orchestrator, key: SessionKey, data: str, *, user_id: int | None
) -> SelectorResponse:
    parts = data.split(":")
    if len(parts) == 4 and parts[2] == "list":
        try:
            page = int(parts[3])
        except ValueError:
            return SelectorResponse(t("autocompact.invalid_action"))
        return automation_selector_start(orch, page)
    if len(parts) != 5 or parts[2] not in {"cron", "webhook"}:
        return SelectorResponse(t("autocompact.invalid_action"))
    _, _, kind, token, action = parts
    if action not in {"open", "input", "default", "off", "inherit"} or (
        action == "inherit" and kind != "webhook"
    ):
        return SelectorResponse(t("autocompact.invalid_action"))
    if automation_target(orch, kind, token) is None:
        return automation_selector_start(orch, note=t("autocompact.list_changed"))
    if action == "open":
        return _automation_detail(orch, kind, token)
    if action == "input":
        return _begin_input(orch, key, f"auto:{kind}:{token}", user_id)
    value = None if action == "inherit" else validate_autocompact(action)
    return await _save_automation_selection(orch, kind, token, value)
