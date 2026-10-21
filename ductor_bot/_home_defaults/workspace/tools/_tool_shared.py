"""Shared helpers for the workspace tool scripts.

Holds the argument parser every tool uses (standard library only, so a tool can load it by
file path) and the load/save/sanitize patterns of the cron and webhook tools.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, NoReturn


def usage_error(message: str | None, usage: str) -> NoReturn:
    """Print *message* and *usage* to stderr and exit with status 1."""
    if message:
        print(f"Error: {message}", file=sys.stderr)
    print(usage, file=sys.stderr)
    sys.exit(1)


def _is_option_like(token: str) -> bool:
    return token.startswith("-") and token != "-"


def parse_tool_args(
    argv: list[str],
    *,
    usage: str,
    help_text: str,
    flags: tuple[str, ...] = (),
    valued: tuple[str, ...] = (),
    min_positional: int = 0,
    max_positional: int = 0,
) -> tuple[dict[str, str | bool], list[str]]:
    """Split *argv* into options and positional arguments before anything else runs.

    - ``-h``/``--help`` anywhere before a ``--`` prints *help_text* and exits 0, also where a
      positional argument or an option value would stand.
    - The first ``--`` ends option handling and is dropped; everything after it is a
      positional argument as it is (``-x``, ``--help``, another ``--``).
    - Options are only read before the first positional argument. An unknown option there is
      a usage error, and so is a valued option whose next token is missing, is ``--`` or looks
      like an option.
    - A missing or surplus positional argument is a usage error (exit 1).
    """
    separator = argv.index("--") if "--" in argv else len(argv)
    if any(token in ("-h", "--help") for token in argv[:separator]):
        print(help_text)
        sys.exit(0)

    options: dict[str, str | bool] = {}
    positional: list[str] = []
    index = 0
    while index < separator:
        arg = argv[index]
        if positional:
            positional.append(arg)
            index += 1
        elif arg in flags:
            options[arg] = True
            index += 1
        elif arg in valued:
            if index + 1 >= separator or _is_option_like(argv[index + 1]):
                usage_error(f"{arg} requires a value", usage)
            options[arg] = argv[index + 1]
            index += 2
        elif _is_option_like(arg):
            usage_error(f"unknown option '{arg}'", usage)
        else:
            positional.append(arg)
            index += 1
    positional.extend(argv[separator + 1 :])
    if len(positional) < min_positional:
        usage_error(None, usage)
    if len(positional) > max_positional:
        usage_error(
            f"too many arguments ({len(positional)} given, expected at most {max_positional}). "
            "Wrap an argument that contains spaces (a prompt or message) in quotes.",
            usage,
        )
    return options, positional


def sanitize_name(raw: str) -> str:
    """Lowercase and normalize a name to ``[a-z0-9-]``."""
    slug = raw.lower()
    slug = re.sub(r"[^a-z0-9-]", "-", slug)
    slug = re.sub(r"-{2,}", "-", slug)
    return slug.strip("-")


def load_collection_or_default(path: Path, key: str) -> dict[str, Any]:
    """Load a JSON file or return ``{key: []}`` if missing/corrupt.

    *key* is the top-level list field (e.g. ``"jobs"`` or ``"hooks"``).
    """
    if not path.exists():
        return {key: []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {key: []}
    if not isinstance(data, dict):
        return {key: []}
    if not isinstance(data.get(key), list):
        return {key: []}
    return data


def load_collection_strict(path: Path, key: str) -> dict[str, Any]:
    """Load a JSON file and raise on malformed structure.

    *key* is the top-level list field (e.g. ``"jobs"`` or ``"hooks"``).
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get(key), list):
        msg = f"Corrupt {path.name} -- cannot parse"
        raise TypeError(msg)
    return data


def save_collection(path: Path, data: dict[str, Any]) -> None:
    """Persist a JSON collection with stable formatting (temp file + rename)."""
    from ductor_bot.infra.atomic_io import atomic_text_save

    atomic_text_save(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def available_ids(items: list[dict[str, Any]], id_field: str = "id") -> list[str]:
    """Return all IDs from a list of dicts for diagnostics."""
    return [str(item.get(id_field, "???")) for item in items]


def find_by_id(
    items: list[dict[str, Any]], item_id: str, id_field: str = "id"
) -> dict[str, Any] | None:
    """Find an item dict by its ID field."""
    return next((item for item in items if item.get(id_field) == item_id), None)
