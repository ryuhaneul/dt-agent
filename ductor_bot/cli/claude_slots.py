"""Shared priority and cooldown operations for configured Claude slots."""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TypedDict

from ductor_bot.cli.claude_accounts import (
    CONTAINER_DUCTOR_MOUNT,
    DEFAULT_SLOT,
    SlotRegistry,
    SlotSnapshot,
    host_cli_auth_dir,
    host_slot_config_dir,
    slot_is_authenticated,
    state_file_path,
)
from ductor_bot.cli.slot_rotation import slot_use_lock
from ductor_bot.config import AgentConfig, transform_config_file


class SlotOverview(TypedDict):
    """One configured slot, in priority order."""

    name: str
    authenticated: bool
    auth_expired: bool
    cooldown_until: float | None


class Overview(TypedDict):
    """Shared JSON representation for command and UI consumers."""

    slots: list[SlotOverview]


def _accounts(data: dict[str, object]) -> list[str]:
    accounts = data.get("claude_accounts") or []
    if not isinstance(accounts, list) or not all(isinstance(slot, str) for slot in accounts):
        raise ValueError("invalid_arguments")
    return list(accounts)


def is_slot_name(name: str) -> bool:
    """Match canonical login directory names, including the API key slot."""
    return (
        name != "claude-slots"
        and re.fullmatch(r"claude(?:-[a-z0-9][a-z0-9_-]*)?", name) is not None
    )


def read_accounts(home: Path) -> list[str]:
    """Read the current slot order from disk so deletion is visible immediately."""
    config = home / "config" / "config.json"
    return _accounts(json.loads(config.read_text(encoding="utf-8")))


def unregistered_slots(home: Path, accounts: Sequence[str]) -> list[str]:
    """List discovered login directories that have no configured priority."""
    root = host_cli_auth_dir(home)
    if not root.is_dir():
        return []
    return [
        entry.name
        for entry in sorted(root.iterdir())
        if is_slot_name(entry.name) and entry.name not in accounts and entry.is_dir()
    ]


SlotIdentity = tuple[int, int, int]


class SlotDeleteError(ValueError):
    """A deletion was refused or failed after updating the slot list."""


class SlotDirectoryError(SlotDeleteError):
    """The login directory may still need removal."""

    def __init__(self, path: Path, cause: OSError) -> None:
        super().__init__("slot_delete_failed")
        self.path = path
        self.reason = cause.strerror or str(cause)


def slot_identity(home: Path, slot: str) -> SlotIdentity:
    """Identify the folder, or the config revision for a slot with no folder."""
    if slot == DEFAULT_SLOT:
        raise SlotDeleteError("slot_default")
    if not is_slot_name(slot):
        raise SlotDeleteError("invalid_slot")
    directory = host_slot_config_dir(home, slot)
    if directory.is_dir():
        status = directory.lstat()
    elif slot in read_accounts(home):
        status = (home / "config" / "config.json").stat()
    else:
        raise SlotDeleteError("slot_not_found")
    return status.st_dev, status.st_ino, status.st_ctime_ns


def remove(home: Path, slot: str, *, expected: SlotIdentity | None = None) -> None:
    """Remove a named slot while excluding turn execution and web login."""
    if slot == DEFAULT_SLOT:
        raise SlotDeleteError("slot_default")
    if not is_slot_name(slot):
        raise SlotDeleteError("invalid_slot")

    with slot_use_lock(host_cli_auth_dir(home), slot, deleting=True) as reserved:
        if not reserved:
            raise SlotDeleteError("slot_busy")
        if expected is not None and slot_identity(home, slot) != expected:
            raise SlotDeleteError("slot_stale")
        root = host_cli_auth_dir(home)
        candidate = os.path.normpath(os.path.join(root, slot))  # noqa: PTH118 -- explicit boundary
        if not candidate.startswith(os.fspath(root) + os.sep):
            raise SlotDeleteError("invalid_slot")
        directory = Path(candidate)

        def unregister(data: dict[str, object]) -> None:
            accounts = _accounts(data)
            if slot not in accounts and not directory.is_dir():
                raise SlotDeleteError("slot_not_found")

            # Keep the config lock through cleanup so the membership check and
            # unregister use the same snapshot, in the existing lock order.
            _clear_slot_state(home, slot)
            if slot in accounts:
                data["claude_accounts"] = [name for name in accounts if name != slot]

        transform_config_file(home / "config" / "config.json", unregister)
        _remove_directory(directory)


def _clear_slot_state(home: Path, slot: str) -> None:
    def clear(snapshot: SlotSnapshot) -> None:
        snapshot.cooldowns.pop(slot, None)
        snapshot.auth_failures.pop(slot, None)
        snapshot.auth_failure_details.pop(slot, None)
        if snapshot.last_used == slot:
            snapshot.last_used = None

    try:
        SlotRegistry(state_file_path(home)).update(clear, strict=True)
    except (ValueError, RecursionError) as exc:
        msg = "Could not update cooldown records"
        raise OSError(msg) from exc


def _remove_directory(directory: Path) -> None:
    try:
        if directory.is_symlink():
            directory.unlink()
        elif directory.is_dir():
            shutil.rmtree(directory)
    except OSError as exc:
        raise SlotDirectoryError(directory, exc) from exc


def _change_priority(home: Path, fn: Callable[[list[str]], list[str]]) -> None:
    def apply(data: dict[str, object]) -> None:
        data["claude_accounts"] = fn(_accounts(data))

    transform_config_file(home / "config" / "config.json", apply)


def _require_slot(accounts: list[str], slot: str) -> None:
    if slot not in accounts:
        raise ValueError("unknown_slot")


def prefer(home: Path, slot: str) -> None:
    """Move a configured slot to the front, preserving all other order."""

    def reorder(accounts: list[str]) -> list[str]:
        _require_slot(accounts, slot)
        return [slot, *[name for name in accounts if name != slot]]

    _change_priority(home, reorder)


def move(home: Path, slot: str, delta: int) -> None:
    """Move a configured slot one position, stopping at either end."""

    def reorder(accounts: list[str]) -> list[str]:
        _require_slot(accounts, slot)
        if delta not in (-1, 1):
            raise ValueError("invalid_delta")
        index = accounts.index(slot)
        target = index + delta
        if 0 <= target < len(accounts):
            accounts[index], accounts[target] = accounts[target], accounts[index]
        return accounts

    _change_priority(home, reorder)


def set_priority(home: Path, slots: Sequence[str]) -> None:
    """Replace the order while retaining exactly the configured slot set."""

    def reorder(accounts: list[str]) -> list[str]:
        if len(slots) != len(set(slots)) or set(slots) != set(accounts):
            raise ValueError("invalid_priority")
        return list(slots)

    _change_priority(home, reorder)


def clear_cooldown(home: Path, slot: str | None) -> None:
    """Remove cooldowns without overwriting concurrent cooldown records."""

    def validate_and_clear(data: dict[str, object]) -> None:
        if slot is not None:
            _require_slot(_accounts(data), slot)

        def clear(snapshot: SlotSnapshot) -> None:
            if slot is None:
                snapshot.cooldowns.clear()
            else:
                snapshot.cooldowns.pop(slot, None)

        SlotRegistry(state_file_path(home)).update(clear)

    # Keep validation current against priority edits and slot removal. The
    # config transform does not change config data, so it does not save it.
    transform_config_file(home / "config" / "config.json", validate_and_clear)


def overview(home: Path, config: AgentConfig) -> Overview:
    """Return priority order, authentication flags, and active reset epochs."""
    snapshot = SlotRegistry(state_file_path(home)).load()
    now = time.time()
    slots: list[SlotOverview] = []
    for name in config.claude_accounts:
        reset = snapshot.cooldowns.get(name)
        slots.append(
            {
                "name": name,
                "authenticated": name not in snapshot.auth_failures
                and slot_is_authenticated(host_slot_config_dir(home, name)),
                "auth_expired": name in snapshot.auth_failures,
                "cooldown_until": reset if reset is not None and reset > now else None,
            }
        )
    return {"slots": slots}


def main(argv: Sequence[str] | None = None) -> int:
    """Run the JSON-only command interface against DUCTOR_HOME."""
    args = list(sys.argv[1:] if argv is None else argv)
    home = Path(os.environ.get("DUCTOR_HOME", CONTAINER_DUCTOR_MOUNT)).expanduser()
    try:
        if args == ["show"]:
            pass
        elif len(args) == 2 and args[0] == "prefer":
            prefer(home, args[1])
        elif len(args) == 2 and args[0] == "priority":
            set_priority(home, args[1].split(",") if args[1] else [])
        elif len(args) == 2 and args[0] == "clear-cooldown":
            clear_cooldown(home, None if args[1] == "all" else args[1])
        else:
            print(json.dumps({"error": "invalid_arguments"}))
            return 2
        data = json.loads((home / "config" / "config.json").read_text(encoding="utf-8"))
        config = AgentConfig(claude_accounts=_accounts(data))
        print(json.dumps(overview(home, config)))
    except (ValueError, OSError) as exc:
        code = str(exc)
        if code not in {"unknown_slot", "invalid_priority", "invalid_delta", "invalid_arguments"}:
            code = "invalid_arguments"
        print(json.dumps({"error": code}))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
