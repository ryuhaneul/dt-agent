"""Interactive overview of configured Claude account priority and cooldowns."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import datetime
from pathlib import Path
from threading import Lock
from uuid import uuid4

from ductor_bot.cli import claude_slots
from ductor_bot.cli.claude_accounts import DEFAULT_SLOT, host_slot_config_dir, slot_is_api_key
from ductor_bot.cli.claude_usage import UsageStore, usage_file_path
from ductor_bot.config import AgentConfig, resolve_user_timezone
from ductor_bot.i18n import t
from ductor_bot.orchestrator.selectors.models import Button, ButtonGrid, SelectorResponse
from ductor_bot.orchestrator.selectors.usage_view import render_account_usage

logger = logging.getLogger(__name__)

ACS_PREFIX = "acs:"
_CALLBACK_MAX_BYTES = 64

_DELETE_REFERENCES_MAX = 1024
_delete_references: dict[str, tuple[Path, str, claude_slots.SlotIdentity | None]] = {}
_delete_reference_lock = Lock()


def _remember_delete_reference(
    home: Path, slot: str, identity: claude_slots.SlotIdentity | None = None
) -> str:
    reference = uuid4().hex
    with _delete_reference_lock:
        if len(_delete_references) >= _DELETE_REFERENCES_MAX:
            del _delete_references[next(iter(_delete_references))]
        _delete_references[reference] = (home, slot, identity)
    return reference


def _take_delete_reference(
    home: Path, reference: str, *, confirmation: bool
) -> tuple[str, claude_slots.SlotIdentity | None]:
    with _delete_reference_lock:
        recorded = _delete_references.get(reference)
        if recorded is None or recorded[0] != home or (recorded[2] is not None) != confirmation:
            raise claude_slots.SlotDeleteError("slot_stale")
        del _delete_references[reference]
    return recorded[1], recorded[2]


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
        if len(data.encode("utf-8")) <= _CALLBACK_MAX_BYTES and data.startswith(
            ("acs:x:", "acs:y:", "acs:n:")
        ):
            return await asyncio.to_thread(_handle_delete, home, config, data)
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


def _handle_delete(home: Path, config: AgentConfig, data: str) -> SelectorResponse:
    action, value = data.split(":", 2)[1:]
    try:
        if action == "x":
            name = value
            if value.startswith("@"):
                name, _ = _take_delete_reference(home, value[1:], confirmation=False)
            identity = claude_slots.slot_identity(home, name)
            reference = _remember_delete_reference(home, name, identity)
            return SelectorResponse(
                text=t("accounts.delete_confirm", name=name),
                buttons=ButtonGrid(
                    [
                        [
                            Button(t("accounts.btn_cancel_delete"), f"acs:n:{reference}"),
                            Button(t("accounts.btn_confirm_delete"), f"acs:y:{reference}"),
                        ]
                    ]
                ),
            )
        name, confirmed_identity = _take_delete_reference(home, value, confirmation=True)
        if action == "n":
            return _build_page(home, config)
        assert confirmed_identity is not None
        claude_slots.remove(home, name, expected=confirmed_identity)
        note = t("accounts.deleted", name=name)
    except claude_slots.SlotDirectoryError as exc:
        note = t("accounts.delete_failed", path=str(exc.path.relative_to(home)), reason=exc.reason)
    except claude_slots.SlotDeleteError as exc:
        note = t(
            {
                "slot_busy": "accounts.delete_busy",
                "slot_default": "accounts.delete_default",
            }.get(str(exc), "accounts.stale")
        )
    return _build_page(home, config, note)


def _slot_button(text: str, action: str, name: str) -> Button | None:
    data = f"acs:{action}:{name}"
    if len(data.encode("utf-8")) > _CALLBACK_MAX_BYTES:
        return None
    return Button(text=text, callback_data=data)


# API key slots skip the OAuth usage line, which adds one branch.
def _build_page(  # noqa: C901
    home: Path, config: AgentConfig, note: str = ""
) -> SelectorResponse:
    accounts = claude_slots.read_accounts(home)
    current = AgentConfig(
        claude_accounts=[*accounts, *claude_slots.unregistered_slots(home, accounts)]
    )
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
        api_key = slot_is_api_key(host_slot_config_dir(home, name))
        auth = t(
            "accounts.api_key"
            if api_key
            else "accounts.auth_expired"
            if slot["auth_expired"]
            else "accounts.authenticated"
            if slot["authenticated"]
            else "accounts.unauthenticated"
        )
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
        priority = index < len(accounts)
        unused = "" if priority else " · " + t("accounts.unused")
        lines.append(f"{index + 1}. {name}  {auth} · {cooldown}{unused}")
        if not api_key:
            try:
                usage_line = render_account_usage(usage.get(name), config, now=now)
            except Exception as exc:
                logger.warning("Account usage display failed: %s", type(exc).__name__)
                usage_line = t("usage.account_empty")
            lines.append("   " + usage_line)
        candidates: list[Button | None] = []
        if priority and index > 0:
            candidates.extend(
                [
                    _slot_button(f"{name} ▲", "u", name),
                    _slot_button(t("accounts.btn_prefer", name=name), "p", name),
                ]
            )
        if index < len(accounts) - 1:
            candidates.append(_slot_button(f"{name} ▼", "d", name))
        if priority and reset is not None and reset > now:
            candidates.append(_slot_button(t("accounts.btn_clear", name=name), "c", name))
        if name != DEFAULT_SLOT and claude_slots.is_slot_name(name):
            text = t("accounts.btn_delete", name=name)
            delete_button = _slot_button(text, "x", name)
            if delete_button is None:
                reference = _remember_delete_reference(home, name)
                delete_button = _slot_button(text, "x", "@" + reference)
            candidates.append(delete_button)
        buttons = [button for button in candidates if button is not None]
        if buttons:
            rows.append(buttons)
    lines.extend(["", t("accounts.subagent_note")])
    rows.append([Button(text=t("accounts.btn_refresh"), callback_data="acs:r")])
    return SelectorResponse(text="\n".join(lines), buttons=ButtonGrid(rows=rows))
