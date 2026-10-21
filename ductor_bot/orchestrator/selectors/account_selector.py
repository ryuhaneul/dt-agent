"""Interactive overview of configured Claude account priority and cooldowns."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from datetime import datetime
from pathlib import Path

from ductor_bot.cli import claude_slots
from ductor_bot.cli.claude_usage import UsageStore, usage_file_path
from ductor_bot.config import AgentConfig, resolve_user_timezone
from ductor_bot.i18n import t
from ductor_bot.orchestrator.selectors.models import Button, ButtonGrid, SelectorResponse
from ductor_bot.orchestrator.selectors.usage_view import render_account_usage

logger = logging.getLogger(__name__)

ACS_PREFIX = "acs:"
_CALLBACK_MAX_BYTES = 64


def is_account_selector_callback(data: str) -> bool:
    """Identify the account selector namespace."""
    return data.startswith(ACS_PREFIX)


async def account_selector_start(home: Path, config: AgentConfig) -> SelectorResponse:
    """Build a page from the current files without blocking the event loop."""
    try:
        return await asyncio.to_thread(_build_page, home, config)
    except Exception as exc:
        logger.warning("Account selector failed: %s", type(exc).__name__)
        return SelectorResponse(text=t("accounts.error"))


async def handle_account_callback(home: Path, config: AgentConfig, data: str) -> SelectorResponse:
    """Apply a named-slot action using the shared module, then reload the page."""
    try:
        note = ""
        if data != "acs:r":
            parts = data.split(":", 2)
            action = parts[1] if len(parts) == 3 else ""
            slot = parts[2] if len(parts) == 3 else ""
            if len(data.encode("utf-8")) > _CALLBACK_MAX_BYTES or action not in {
                "u",
                "d",
                "p",
                "c",
            }:
                note = t("accounts.stale")
            else:
                try:
                    if action == "p":
                        await asyncio.to_thread(claude_slots.prefer, home, slot)
                    elif action == "c":
                        await asyncio.to_thread(claude_slots.clear_cooldown, home, slot)
                    else:
                        await asyncio.to_thread(
                            claude_slots.move, home, slot, -1 if action == "u" else 1
                        )
                except ValueError:
                    note = t("accounts.stale")
        return await asyncio.to_thread(_build_page, home, config, note)
    except Exception as exc:
        logger.warning("Account selector failed: %s", type(exc).__name__)
        return SelectorResponse(text=t("accounts.error"))


def _slot_button(text: str, action: str, name: str) -> Button | None:
    data = f"acs:{action}:{name}"
    if len(data.encode("utf-8")) > _CALLBACK_MAX_BYTES:
        return None
    return Button(text=text, callback_data=data)


def _build_page(home: Path, config: AgentConfig, note: str = "") -> SelectorResponse:
    data = json.loads((home / "config" / "config.json").read_text(encoding="utf-8"))
    current = AgentConfig(claude_accounts=data.get("claude_accounts") or [])
    slots = claude_slots.overview(home, current)["slots"]
    usage = UsageStore(usage_file_path(home / "workspace")).load()
    lines = [t("accounts.title")]
    if note:
        lines.extend(["", note])
    rows: list[list[Button]] = []
    now = time.time()
    timezone = resolve_user_timezone(config.user_timezone)
    if not slots:
        lines.extend(["", t("accounts.empty")])
    for index, slot in enumerate(slots):
        name = slot["name"]
        auth = t("accounts.authenticated" if slot["authenticated"] else "accounts.unauthenticated")
        reset = slot["cooldown_until"]
        cooldown = t("accounts.no_cooldown")
        if reset is not None and reset > now:
            hours, minutes = divmod(math.ceil((reset - now) / 60), 60)
            cooldown = t(
                "accounts.cooldown",
                hours=hours,
                minutes=minutes,
                time=datetime.fromtimestamp(reset, timezone).strftime("%H:%M"),
            )
        lines.append(f"{index + 1}. {name}  {auth} · {cooldown}")
        try:
            usage_line = render_account_usage(usage.get(name), config, now=now)
        except Exception as exc:
            logger.warning("Account usage display failed: %s", type(exc).__name__)
            usage_line = t("usage.account_empty")
        lines.append("   " + usage_line)
        candidates: list[Button | None] = []
        if index > 0:
            candidates.extend(
                [
                    _slot_button(f"{name} ▲", "u", name),
                    _slot_button(t("accounts.btn_prefer", name=name), "p", name),
                ]
            )
        if index < len(slots) - 1:
            candidates.append(_slot_button(f"{name} ▼", "d", name))
        if reset is not None and reset > now:
            candidates.append(_slot_button(t("accounts.btn_clear", name=name), "c", name))
        buttons = [button for button in candidates if button is not None]
        if buttons:
            rows.append(buttons)
    lines.extend(["", t("accounts.subagent_note")])
    rows.append([Button(text=t("accounts.btn_refresh"), callback_data="acs:r")])
    return SelectorResponse(text="\n".join(lines), buttons=ButtonGrid(rows=rows))
