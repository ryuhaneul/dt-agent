"""Interactive overview of configured Claude account priority and cooldowns."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Lock
from uuid import uuid4

from ductor_bot.cli import claude_slots
from ductor_bot.cli.claude_accounts import (
    DEFAULT_SLOT,
    SlotKind,
    host_slot_config_dir,
    slot_is_authenticated,
    slot_kind,
)
from ductor_bot.cli.claude_usage import UsageStore, usage_file_path
from ductor_bot.config import AgentConfig, resolve_user_timezone
from ductor_bot.i18n import t
from ductor_bot.orchestrator.selectors.models import Button, ButtonGrid, SelectorResponse
from ductor_bot.orchestrator.selectors.usage_view import render_account_usage

logger = logging.getLogger(__name__)

ACS_PREFIX = "acs:"
_CALLBACK_MAX_BYTES = 64

_SLOT_ACTIONS = {"x": "delete", "l": "logout", "k": "disconnect"}
_SLOT_REFERENCES_MAX = 1024


@dataclass(frozen=True, slots=True)
class _SlotReference:
    home: Path
    slot: str
    action: str
    identity: claude_slots.SlotIdentity | None


_slot_references: dict[str, _SlotReference] = {}
_slot_reference_lock = Lock()


def _remember_slot_reference(
    home: Path, slot: str, action: str, identity: claude_slots.SlotIdentity | None = None
) -> str:
    reference = uuid4().hex
    with _slot_reference_lock:
        if len(_slot_references) >= _SLOT_REFERENCES_MAX:
            del _slot_references[next(iter(_slot_references))]
        _slot_references[reference] = _SlotReference(home, slot, action, identity)
    return reference


def _take_slot_reference(
    home: Path, reference: str, *, confirmation: bool, action: str | None = None
) -> _SlotReference:
    with _slot_reference_lock:
        recorded = _slot_references.get(reference)
        if (
            recorded is None
            or recorded.home != home
            or (recorded.identity is not None) != confirmation
            or (action is not None and recorded.action != action)
        ):
            raise claude_slots.SlotDeleteError("slot_stale")
        del _slot_references[reference]
    return recorded


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
        parts = data.split(":", 2)
        action = parts[1] if len(parts) == 3 and parts[0] == "acs" else ""
        if len(data.encode("utf-8")) <= _CALLBACK_MAX_BYTES and (
            action in _SLOT_ACTIONS or action in {"y", "n"}
        ):
            return await asyncio.to_thread(_handle_slot_change, home, config, action, parts[2])
        note = ""
        if data != "acs:r":
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


def _auth_change_identity(home: Path, name: str, action: str) -> claude_slots.SlotIdentity:
    if not claude_slots.is_slot_name(name):
        raise claude_slots.SlotDeleteError("invalid_slot")
    directory = host_slot_config_dir(home, name)
    if not directory.is_dir():
        raise claude_slots.SlotDeleteError("slot_not_found")
    kind = slot_kind(directory)
    if action == "disconnect":
        if kind != "api_key":
            raise claude_slots.SlotDeleteError("not_api_key_slot")
    elif kind != "oauth":
        raise claude_slots.SlotDeleteError("api_key_slot")
    elif not slot_is_authenticated(directory):
        raise claude_slots.SlotDeleteError("slot_stale")
    try:
        status = directory.lstat()
    except FileNotFoundError as exc:
        raise claude_slots.SlotDeleteError("slot_not_found") from exc
    return status.st_dev, status.st_ino, status.st_ctime_ns


def _apply_slot_change(
    home: Path, name: str, action: str, identity: claude_slots.SlotIdentity
) -> None:
    if action == "delete":
        claude_slots.remove(home, name, expected=identity)
    elif action == "logout":
        claude_slots.logout(home, name, expected=identity)
    else:
        claude_slots.disconnect_api_key(home, name, expected=identity)


def _handle_slot_change(
    home: Path, config: AgentConfig, callback: str, value: str
) -> SelectorResponse:
    action = _SLOT_ACTIONS.get(callback, "")
    try:
        if action:
            name = value
            if value.startswith("@"):
                name = _take_slot_reference(home, value[1:], confirmation=False, action=action).slot
            identity = (
                claude_slots.slot_identity(home, name)
                if action == "delete"
                else _auth_change_identity(home, name, action)
            )
            reference = _remember_slot_reference(home, name, action, identity)
            return SelectorResponse(
                text=t(f"accounts.{action}_confirm", name=name),
                buttons=ButtonGrid(
                    [
                        [
                            Button(t("accounts.btn_cancel_delete"), f"acs:n:{reference}"),
                            Button(t(f"accounts.btn_confirm_{action}"), f"acs:y:{reference}"),
                        ]
                    ]
                ),
            )
        recorded = _take_slot_reference(home, value, confirmation=True)
        if callback == "n":
            return _build_page(home, config)
        action = recorded.action
        assert recorded.identity is not None
        _apply_slot_change(home, recorded.slot, action, recorded.identity)
        key = "deleted" if action == "delete" else f"{action}_done"
        note = t(f"accounts.{key}", name=recorded.slot)
    except claude_slots.SlotDirectoryError as exc:
        note = t("accounts.delete_failed", path=str(exc.path.relative_to(home)), reason=exc.reason)
    except claude_slots.SlotDeleteError as exc:
        note = t(
            {
                "slot_busy": f"accounts.{action}_busy",
                "slot_default": "accounts.delete_default",
                "api_key_slot": "accounts.logout_api_key_slot",
                "not_api_key_slot": "accounts.disconnect_not_api_key_slot",
                "logout_failed": "accounts.logout_failed",
                "disconnect_failed": "accounts.disconnect_failed",
            }.get(str(exc), "accounts.stale")
        )
    return _build_page(home, config, note)


def _slot_button(text: str, action: str, name: str) -> Button | None:
    data = f"acs:{action}:{name}"
    if len(data.encode("utf-8")) > _CALLBACK_MAX_BYTES:
        return None
    return Button(text=text, callback_data=data)


def _slot_change_button(home: Path, text: str, callback: str, name: str) -> Button | None:
    direct = _slot_button(text, callback, name)
    if direct is not None:
        return direct
    reference = _remember_slot_reference(home, name, _SLOT_ACTIONS[callback])
    return _slot_button(text, callback, "@" + reference)


def _slot_change_buttons(
    home: Path, name: str, kind: SlotKind, has_oauth_credentials: bool
) -> list[Button | None]:
    if not claude_slots.is_slot_name(name):
        return []
    buttons = []
    if kind == "api_key":
        buttons.append(
            _slot_change_button(home, t("accounts.btn_disconnect", name=name), "k", name)
        )
    elif has_oauth_credentials:
        buttons.append(_slot_change_button(home, t("accounts.btn_logout", name=name), "l", name))
    if name != DEFAULT_SLOT:
        buttons.append(_slot_change_button(home, t("accounts.btn_delete", name=name), "x", name))
    return buttons


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
        directory = host_slot_config_dir(home, name)
        kind = slot_kind(directory)
        has_oauth_credentials = kind == "oauth" and slot_is_authenticated(directory)
        auth = t(
            "accounts.api_key"
            if kind == "api_key"
            else "accounts.api_key_detached"
            if kind == "api_key_detached"
            else "accounts.auth_expired"
            if slot["auth_expired"] and has_oauth_credentials
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
        if kind == "oauth":
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
        candidates.extend(_slot_change_buttons(home, name, kind, has_oauth_credentials))
        buttons = [button for button in candidates if button is not None]
        if buttons:
            rows.append(buttons)
    lines.extend(["", t("accounts.subagent_note")])
    rows.append([Button(text=t("accounts.btn_refresh"), callback_data="acs:r")])
    return SelectorResponse(text="\n".join(lines), buttons=ButtonGrid(rows=rows))
