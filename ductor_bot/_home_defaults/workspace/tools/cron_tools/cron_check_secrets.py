#!/usr/bin/env python3
"""Check a cron task's static secret-use evidence, never secret values.

Usage: python3 tools/cron_tools/cron_check_secrets.py JOB_OR_FOLDER [--key KEY_NAME ...]
Static evidence can miss dynamic names, imports and external commands or produce false positives.
An empty result never guarantees that a task is independent of secrets.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _secrets import SecretsError, secret_keys

from ductor_bot.cron.definitions import scan_secrets as scan, secret_candidate as _candidate


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        print(json.dumps({"error": "INVALID_ARGUMENTS"}))
        raise SystemExit(1)


def main() -> int:
    parser = _Parser(description=__doc__)
    parser.add_argument(
        "task", help="Job name or a folder inside this home's workspace/cron_tasks"
    )
    parser.add_argument(
        "--key",
        action="append",
        default=[],
        help="A declared key name to look for in prose",
    )
    args = parser.parse_args()
    if any(_candidate(name, explicit=True) is None for name in args.key):
        print(json.dumps({"error": "INVALID_KEY_NAME"}))
        return 1
    root = (
        Path(os.environ.get("DUCTOR_HOME", "~/.ductor")).expanduser()
        / "workspace/cron_tasks"
    )
    supplied = Path(args.task).expanduser()
    folder = (
        supplied
        if supplied.is_absolute()
        else (supplied if supplied.is_dir() else root / supplied)
    )
    try:
        folder = folder.resolve(strict=True)
        if (
            folder == root.resolve()
            or not folder.is_relative_to(root.resolve())
            or not folder.is_dir()
        ):
            raise ValueError
    except (OSError, ValueError):
        print(json.dumps({"error": "INVALID_TASK_FOLDER"}))
        return 1
    inventory = None
    inventory_error = None
    try:
        inventory = set(secret_keys())
    except SecretsError as exc:
        inventory_error = exc.code
    result = scan(folder, set(args.key), inventory)
    if inventory_error:
        result["limitations"].append(
            f"Key inventory unavailable ({inventory_error}); candidates are not classified."
        )
    if result["skipped_files"]:
        result["limitations"].append(
            "Some files could not be read/analyzed or were linked outside the task; review them manually."
        )
        result["error"] = "SCAN_INCOMPLETE"
    print(json.dumps(result))
    return 1 if result["skipped_files"] else 0


if __name__ == "__main__":
    sys.exit(main())
