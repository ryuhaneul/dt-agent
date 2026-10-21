"""Shared priority and cooldown operations for configured Claude slots."""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TypedDict

from ductor_bot.cli.claude_accounts import (
    CONTAINER_DUCTOR_MOUNT,
    SlotRegistry,
    SlotSnapshot,
    host_slot_config_dir,
    slot_is_authenticated,
    state_file_path,
)
from ductor_bot.config import AgentConfig, transform_config_file


class SlotOverview(TypedDict):
    """One configured slot, in priority order."""

    name: str
    authenticated: bool
    cooldown_until: float | None


class Overview(TypedDict):
    """Shared JSON representation for command and UI consumers."""

    slots: list[SlotOverview]


def _accounts(data: dict[str, object]) -> list[str]:
    accounts = data.get("claude_accounts") or []
    if not isinstance(accounts, list) or not all(isinstance(slot, str) for slot in accounts):
        raise ValueError("invalid_arguments")
    return list(accounts)


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
                "authenticated": slot_is_authenticated(host_slot_config_dir(home, name)),
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
