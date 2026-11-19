"""Shared helpers for the workspace tool scripts.

Holds the argument parser every tool uses (standard library only, so a tool can load it by
file path) and the load/save/sanitize patterns of the cron and webhook tools.
"""

from __future__ import annotations

import json
import logging
import os
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


def parse_autocompact_option(
    value: str | None, *, allow_inherit: bool = False, use_env: bool = False
) -> str | int | None:
    """Parse CLI text without importing the bot's optional Python dependencies."""
    if value is None:
        value = os.environ.get("DUCTOR_AUTOCOMPACT", "default") if use_env else None
    if value is None or (allow_inherit and value == "inherit"):
        return None
    if value in ("default", "off"):
        return value
    match = re.fullmatch(
        r"([0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.([0-9]+))?([kKmM]?)", value.strip()
    )
    if match is not None:
        whole, fraction, suffix = match.groups()
        fraction = fraction or ""
        multiplier = {"": 1, "k": 1000, "m": 1000000}[suffix.lower()]
        numerator = int(whole.replace(",", "") + fraction) * multiplier
        tokens, remainder = divmod(numerator, 10 ** len(fraction))
        if not remainder and 100000 <= tokens <= 1000000:
            return tokens
    raise ValueError("autocompact must be default, off, or an integer from 100000 to 1000000" + (" (inherit is also allowed)" if allow_inherit else ""))


def normalize_autocompact_records(data: dict[str, Any], key: str) -> dict[str, Any]:
    """Repair invalid existing values on tool writes, preserving all other record fields."""
    inherit = key == "hooks"
    for record in data.get(key, []):
        if not isinstance(record, dict) or "autocompact" not in record:
            continue
        value = record["autocompact"]
        valid = value in ("default", "off") if isinstance(value, str) else type(value) is int and 100000 <= value <= 1000000
        if not valid and not (inherit and value is None):
            logging.getLogger(__name__).warning("Invalid stored autocompact for record %s; using fallback", record.get("id", "?"))
            record["autocompact"] = None if inherit else "default"
    return data
