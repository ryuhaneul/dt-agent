"""Shared priority and cooldown operations for configured Claude slots."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import TypedDict
from uuid import uuid4

from ductor_bot.cli.claude_accounts import (
    CONTAINER_DUCTOR_MOUNT,
    DEFAULT_SLOT,
    DETACHED_API_KEY_HELPER,
    AuthFailureDetails,
    SlotRegistry,
    SlotSnapshot,
    claude_slot_env,
    container_slot_config_dir,
    host_cli_auth_dir,
    host_slot_config_dir,
    slot_is_authenticated,
    slot_kind,
    state_file_path,
)
from ductor_bot.cli.claude_usage import UsageStore, usage_file_path
from ductor_bot.cli.slot_rotation import slot_use_lock
from ductor_bot.config import AgentConfig, transform_config_file
from ductor_bot.infra.atomic_io import atomic_bytes_save, atomic_text_save
from ductor_bot.infra.file_lock import exclusive_file_lock
from ductor_bot.infra.json_store import atomic_json_save

logger = logging.getLogger(__name__)


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


def _slot_name_for_log(name: str) -> str:
    return name if is_slot_name(name) else "<invalid slot name>"


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


SlotIdentity = str


class SlotDeleteError(ValueError):
    """A slot removal, logout, or key disconnection was refused or failed."""


class SlotDirectoryError(SlotDeleteError):
    """The login directory may still need removal."""

    def __init__(self, path: Path, cause: OSError) -> None:
        super().__init__("slot_delete_failed")
        self.path = path
        self.reason = cause.strerror or str(cause)


def _identity_directory(home: Path, slot: str, *, include_default: bool) -> Path:
    if slot == DEFAULT_SLOT and not include_default:
        raise SlotDeleteError("slot_default")
    if not is_slot_name(slot):
        raise SlotDeleteError("invalid_slot")
    return host_slot_config_dir(home, slot)


def _lstat_if_present(directory: Path) -> os.stat_result | None:
    try:
        return directory.lstat()
    except FileNotFoundError:
        return None


def _slot_directory_stat(directory: Path) -> os.stat_result | None:
    status = _lstat_if_present(directory)
    if status is None:
        return None
    if stat.S_ISDIR(status.st_mode) or (stat.S_ISLNK(status.st_mode) and directory.is_dir()):
        return status
    return None


def _config_identity(home: Path, slot: str) -> SlotIdentity:
    if slot == DEFAULT_SLOT or slot not in read_accounts(home):
        raise SlotDeleteError("slot_not_found")
    status = (home / "config" / "config.json").stat()
    return f"cfg:{status.st_dev}:{status.st_ino}:{status.st_ctime_ns}"


def _identity_path(directory: Path) -> Path:
    key = hashlib.sha256(directory.name.encode()).hexdigest()
    return directory.with_name(f".slot-{key}.id")


def _read_identity_file(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _matching_identity(content: bytes | None, status: os.stat_result) -> SlotIdentity | None:
    if content is None:
        return None
    match = re.fullmatch(rb"([0-9a-f]{32}) ([0-9]+) ([0-9]+)\n?", content)
    if match is not None and match.groups()[1:] == (
        str(status.st_dev).encode(),
        str(status.st_ino).encode(),
    ):
        return f"id:{match[1].decode()}"
    return None


def slot_identity(home: Path, slot: str, *, include_default: bool = False) -> SlotIdentity:
    """Read or create an identity for the slot's current directory."""
    directory = _identity_directory(home, slot, include_default=include_default)
    status = _slot_directory_stat(directory)
    if status is None:
        return _config_identity(home, slot)
    path = _identity_path(directory)
    identity = _matching_identity(_read_identity_file(path), status)
    if identity is not None:
        return identity
    with exclusive_file_lock(path):
        status = _slot_directory_stat(directory)
        if status is not None:
            identity = _matching_identity(_read_identity_file(path), status)
            if identity is None:
                value = uuid4().hex
                atomic_bytes_save(path, f"{value} {status.st_dev} {status.st_ino}\n".encode())
                identity = f"id:{value}"
            return identity
    return _config_identity(home, slot)


def current_slot_identity(
    home: Path, slot: str, *, include_default: bool = False
) -> SlotIdentity | None:
    """Read an identity for comparison without creating or locking the ID file."""
    directory = _identity_directory(home, slot, include_default=include_default)
    status = _slot_directory_stat(directory)
    if status is None:
        return _config_identity(home, slot)
    return _matching_identity(_read_identity_file(_identity_path(directory)), status)


def invalidate_slot_identity(directory: Path) -> None:
    """Invalidate confirmations issued before a slot's login completed."""
    path = _identity_path(directory)
    with exclusive_file_lock(path):
        path.unlink(missing_ok=True)


def remove(home: Path, slot: str, *, expected: SlotIdentity | None = None) -> None:
    """Remove a named slot while excluding turn execution and web login."""
    if slot == DEFAULT_SLOT:
        raise SlotDeleteError("slot_default")
    if not is_slot_name(slot):
        raise SlotDeleteError("invalid_slot")

    with slot_use_lock(host_cli_auth_dir(home), slot, deleting=True) as reserved:
        if not reserved:
            raise SlotDeleteError("slot_busy")
        if expected is not None and current_slot_identity(home, slot) != expected:
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
            if expected is not None and current_slot_identity(home, slot) != expected:
                raise SlotDeleteError("slot_stale")

            # Keep the config lock through cleanup so the membership check and
            # unregister use the same snapshot, in the existing lock order.
            _clear_slot_state(home, slot)
            if slot in accounts:
                data["claude_accounts"] = [name for name in accounts if name != slot]

        unregistered = transform_config_file(home / "config" / "config.json", unregister)
        _remove_directory(directory, unregistered=unregistered)
    logger.info("Claude slot deleted: %s", slot)


def _validate_auth_slot_path(directory: Path, expected_path: Path) -> None:
    try:
        if not directory.is_symlink() and directory.resolve() == expected_path:
            return
    except (OSError, RuntimeError) as exc:
        raise SlotDeleteError("invalid_slot") from exc
    raise SlotDeleteError("invalid_slot")


@contextmanager
def _reserve_auth_change(home: Path, slot: str, expected: SlotIdentity | None) -> Iterator[Path]:
    if not is_slot_name(slot):
        raise SlotDeleteError("invalid_slot")
    root = host_cli_auth_dir(home)
    directory = root / slot
    expected_path = root.resolve() / slot
    _validate_auth_slot_path(directory, expected_path)
    with slot_use_lock(root, slot, deleting=True) as reserved:
        if not reserved:
            raise SlotDeleteError("slot_busy")
        _validate_auth_slot_path(directory, expected_path)
        missing = "slot_not_found" if expected is None else "slot_stale"
        try:
            status = directory.lstat()
        except FileNotFoundError as exc:
            raise SlotDeleteError(missing) from exc
        if stat.S_ISLNK(status.st_mode):
            raise SlotDeleteError("invalid_slot")
        if not stat.S_ISDIR(status.st_mode):
            raise SlotDeleteError(missing)
        if (
            expected is not None
            and current_slot_identity(home, slot, include_default=True) != expected
        ):
            raise SlotDeleteError("slot_stale")
        yield directory


def logout(home: Path, slot: str, *, expected: SlotIdentity | None = None) -> None:
    """Log out an OAuth slot in the bot container, preserving its configuration."""
    with _reserve_auth_change(home, slot, expected) as directory:
        if slot_kind(directory) != "oauth":
            raise SlotDeleteError("api_key_slot")
        try:
            result = subprocess.run(
                ("claude", "auth", "logout"),
                env=claude_slot_env(os.environ, container_slot_config_dir(slot)),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
                check=False,
            )
            if result.returncode != 0 or slot_is_authenticated(directory):
                raise SlotDeleteError("logout_failed")
            _mark_slot_signed_out(home, slot)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SlotDeleteError("logout_failed") from exc
    logger.info("Claude slot logged out: %s", slot)


def disconnect_api_key(home: Path, slot: str, *, expected: SlotIdentity | None = None) -> None:
    """Remove an API key helper from settings and keep its command for recovery."""
    with _reserve_auth_change(home, slot, expected) as directory:
        if slot_kind(directory) != "api_key":
            raise SlotDeleteError("not_api_key_slot")

        settings = directory / "settings.json"
        try:
            original = settings.read_text(encoding="utf-8")
            data = json.loads(original)
        except (OSError, ValueError, RecursionError) as exc:
            raise SlotDeleteError("disconnect_failed") from exc

        helper = data.get("apiKeyHelper") if isinstance(data, dict) else None
        if not isinstance(helper, str) or not helper:
            raise SlotDeleteError("not_api_key_slot")
        try:
            atomic_text_save(directory / DETACHED_API_KEY_HELPER, helper + "\n")
            del data["apiKeyHelper"]
            atomic_json_save(settings, data)
        except (OSError, ValueError, RecursionError) as exc:
            raise SlotDeleteError("disconnect_failed") from exc

        try:
            _mark_slot_signed_out(home, slot)
        except OSError as exc:
            try:
                atomic_text_save(settings, original)
            except OSError as rollback_error:
                raise SlotDeleteError("disconnect_failed") from rollback_error
            raise SlotDeleteError("disconnect_failed") from exc
    logger.info("Claude slot API key disconnected: %s", slot)


def _mark_slot_signed_out(home: Path, slot: str) -> None:
    registry = SlotRegistry(state_file_path(home))
    fingerprint = registry.credential_mtimes(slot)

    def mark(snapshot: SlotSnapshot) -> None:
        snapshot.cooldowns.pop(slot, None)
        snapshot.auth_failures[slot] = fingerprint
        snapshot.auth_failure_details[slot] = AuthFailureDetails()

    registry.update(mark, strict=True)
    UsageStore(usage_file_path(home)).remove(slot)


def _clear_slot_state(home: Path, slot: str) -> None:
    def clear(snapshot: SlotSnapshot) -> None:
        snapshot.cooldowns.pop(slot, None)
        snapshot.auth_failures.pop(slot, None)
        snapshot.auth_failure_details.pop(slot, None)
        if snapshot.last_used == slot:
            snapshot.last_used = None

    try:
        SlotRegistry(state_file_path(home)).update(clear, strict=True)
        UsageStore(usage_file_path(home)).remove(slot)
    except (ValueError, RecursionError) as exc:
        msg = "Could not update cooldown records"
        raise OSError(msg) from exc


def remove_slot_directory(directory: Path) -> None:
    """Remove the slot and its ID while excluding identity creation."""
    path = _identity_path(directory)
    with exclusive_file_lock(path):
        content = _read_identity_file(path)
        path.unlink(missing_ok=True)
        try:
            if directory.is_symlink():
                directory.unlink()
            elif directory.is_dir():
                shutil.rmtree(directory)
        except OSError:
            if content is not None:
                try:
                    status = _lstat_if_present(directory)
                    if status is not None and _matching_identity(content, status) is not None:
                        atomic_bytes_save(path, content)
                except OSError:
                    logger.warning("Could not restore the Claude slot ID: %s", directory.name)
            raise


def _remove_directory(directory: Path, *, unregistered: bool) -> None:
    try:
        remove_slot_directory(directory)
    except OSError as exc:
        if unregistered:
            logger.warning(
                "Claude slot unregistered but its directory could not be removed: %s",
                directory.name,
            )
        raise SlotDirectoryError(directory, exc) from exc


def _change_priority(home: Path, fn: Callable[[list[str]], list[str]]) -> bool:
    def apply(data: dict[str, object]) -> None:
        data["claude_accounts"] = fn(_accounts(data))

    return transform_config_file(home / "config" / "config.json", apply)


def _require_slot(accounts: list[str], slot: str) -> None:
    if slot not in accounts:
        raise ValueError("unknown_slot")


def prefer(home: Path, slot: str) -> None:
    """Move a configured slot to the front, preserving all other order."""

    def reorder(accounts: list[str]) -> list[str]:
        _require_slot(accounts, slot)
        return [slot, *[name for name in accounts if name != slot]]

    if _change_priority(home, reorder):
        logger.info("Claude slot preferred: %s", _slot_name_for_log(slot))


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

    if _change_priority(home, reorder):
        logger.info("Claude slot moved: %s (%+d)", _slot_name_for_log(slot), delta)


def swap(
    home: Path,
    first: str,
    second: str,
    *,
    validate: Callable[[list[str]], None] | None = None,
) -> None:
    """Exchange two slots, optionally validating the locked order before mutation."""

    def reorder(accounts: list[str]) -> list[str]:
        if validate is not None:
            validate(accounts)
        if first == second:
            raise ValueError("invalid_swap")
        _require_slot(accounts, first)
        _require_slot(accounts, second)
        left, right = accounts.index(first), accounts.index(second)
        accounts[left], accounts[right] = accounts[right], accounts[left]
        return accounts

    if _change_priority(home, reorder):
        logger.info(
            "Claude slot swapped: %s, %s", _slot_name_for_log(first), _slot_name_for_log(second)
        )


def set_priority(home: Path, slots: Sequence[str]) -> None:
    """Replace the order while retaining exactly the configured slot set."""

    def reorder(accounts: list[str]) -> list[str]:
        if len(slots) != len(set(slots)) or set(slots) != set(accounts):
            raise ValueError("invalid_priority")
        return list(slots)

    if _change_priority(home, reorder):
        logger.info("Claude slot priority set: %s", ", ".join(map(_slot_name_for_log, slots)))


def clear_cooldown(home: Path, slot: str | None) -> None:
    """Remove cooldowns without overwriting concurrent cooldown records."""
    cleared = False

    def validate_and_clear(data: dict[str, object]) -> None:
        if slot is not None:
            _require_slot(_accounts(data), slot)

        def clear(snapshot: SlotSnapshot) -> None:
            nonlocal cleared
            if slot is None:
                cleared = bool(snapshot.cooldowns)
                snapshot.cooldowns.clear()
            else:
                cleared = snapshot.cooldowns.pop(slot, None) is not None

        SlotRegistry(state_file_path(home)).update(clear)

    # Keep validation current against priority edits and slot removal. The
    # config transform does not change config data, so it does not save it.
    transform_config_file(home / "config" / "config.json", validate_and_clear)
    if not cleared:
        return
    if slot is None:
        logger.info("All Claude slot cooldowns cleared")
    else:
        logger.info("Claude slot cooldown cleared: %s", _slot_name_for_log(slot))


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
