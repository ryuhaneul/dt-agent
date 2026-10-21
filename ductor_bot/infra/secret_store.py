"""Validation and atomic updates for the shared tool secrets store."""

from __future__ import annotations

import os
import re
from pathlib import Path

from ductor_bot.infra.atomic_io import atomic_text_save
from ductor_bot.infra.env_secrets import (
    SECRETS_FILE_NAME,
    SECRETS_FILE_VARIABLE,
    load_env_secrets,
    main_home_of,
)
from ductor_bot.infra.file_lock import exclusive_file_lock

_KEY_PATTERN = re.compile(r"[A-Z][A-Z0-9_]{0,55}")
_LINE_BOUNDARIES = "\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029"


def store_path(ductor_home: Path) -> Path:
    """Choose the store using the same precedence as the CLI environment."""
    home = ductor_home.expanduser()
    value = os.environ.get(SECRETS_FILE_VARIABLE)
    if value is None:
        value = load_env_secrets(home / ".env").get(SECRETS_FILE_VARIABLE)
    return Path(value).expanduser() if value else main_home_of(home) / SECRETS_FILE_NAME


def validate_key(key: str) -> str | None:
    return None if _KEY_PATTERN.fullmatch(key) else "key_invalid"


def validate_value(value: str) -> str | None:  # noqa: PLR0911
    if not value:
        return "value_empty"
    if len(value) > 4096:
        return "value_too_long"
    if "\0" in value:
        return "value_nul"
    if any(char in value for char in _LINE_BOUNDARIES):
        return "value_multiline"
    if value[0].isspace() or value[-1].isspace():
        return "value_edge_space"
    if value[0] in {"'", '"'} or value[-1] in {"'", '"'}:
        return "value_edge_quote"
    if value.startswith("$"):
        return "value_dollar"
    return None


def _read_lines(path: Path) -> list[str]:
    try:
        with path.open(encoding="utf-8", newline="") as stream:
            return stream.read().splitlines(keepends=True)
    except FileNotFoundError:
        return []


def _line_key(line: str) -> str | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    key, separator, _ = stripped.partition("=")
    return key.strip() or None if separator else None


def list_keys(path: Path) -> list[str]:
    """Return sorted names without returning any stored values."""
    with exclusive_file_lock(path):
        return sorted({key for line in _read_lines(path) if (key := _line_key(line))})


def set_secret(path: Path, key: str, value: str) -> None:
    """Replace the first matching entry and remove later duplicates."""
    reason = validate_key(key) or validate_value(value)
    if reason:
        raise ValueError(reason)
    with exclusive_file_lock(path):
        lines = _read_lines(path)
        updated: list[str] = []
        replaced = False
        for line in lines:
            if _line_key(line) != key:
                updated.append(line)
            elif not replaced:
                ending = line[len(line.rstrip(_LINE_BOUNDARIES)) :]
                updated.append(f"{key}={value}{ending}")
                replaced = True
        if not replaced:
            if updated and not updated[-1].endswith(tuple(_LINE_BOUNDARIES)):
                updated[-1] += "\n"
            updated.append(f"{key}={value}\n")
        target = path.resolve() if path.is_symlink() else path
        atomic_text_save(target, "".join(updated))
        target.chmod(0o600)


def delete_secret(path: Path, key: str) -> bool:
    """Remove all entries for a key, reporting whether any existed."""
    reason = validate_key(key)
    if reason:
        raise ValueError(reason)
    with exclusive_file_lock(path):
        lines = _read_lines(path)
        updated = [line for line in lines if _line_key(line) != key]
        if len(updated) == len(lines):
            return False
        target = path.resolve() if path.is_symlink() else path
        atomic_text_save(target, "".join(updated))
        target.chmod(0o600)
        return True
