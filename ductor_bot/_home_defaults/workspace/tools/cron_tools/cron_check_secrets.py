#!/usr/bin/env python3
"""Check a cron task's static secret-use evidence, never secret values.

Usage: python3 tools/cron_tools/cron_check_secrets.py JOB_OR_FOLDER [--key KEY_NAME ...]
Static evidence can miss dynamic names, imports and external commands or produce false positives.
An empty result never guarantees that a task is independent of secrets.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _secrets import SecretsError, secret_keys

_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,55}\Z")
_SECRET_NAME = re.compile(
    r"(?:^|_)(?:KEY|TOKEN|PASSWORD|PASS|SECRET|SEED|CREDENTIALS?)\Z"
)
_SKIP = {".git", ".venv", "venv", "node_modules", "__pycache__", ".cache"}
_EXTENSIONS = {".py", ".sh", ".bash", ".js", ".mjs", ".cjs", ".ts", ".ps1"}
_LIMITATION = (
    "Static evidence only: dynamic names, imports, outside files and external commands may be "
    "missed; comments or unused code may cause false positives. Review the assignment even "
    "when no evidence is found. Unknown names may be provided by the environment instead of "
    "the tool store. No secret values are requested or returned."
)
_WARNING = (
    "If this job needs secrets, report that restart, manual lock or TTL expiry locks a "
    "configured store and secret-dependent runs fail with SECRETS_LOCKED until the owner "
    "uses /masterkey unlock or webchat unlock. Never put values or master keys in task files."
)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        print(json.dumps({"error": "INVALID_ARGUMENTS"}))
        raise SystemExit(1)


def _candidate(name: object, *, explicit: bool = False) -> str | None:
    if (
        isinstance(name, str)
        and _NAME.fullmatch(name)
        and (explicit or _SECRET_NAME.search(name))
    ):
        return name
    return None


def _python(source: str) -> set[str]:
    names: set[str] = set()
    for part in ast.walk(ast.parse(source)):
        if isinstance(part, ast.Name) and (name := _candidate(part.id)):
            names.add(name)
        if isinstance(part, ast.Call):
            function = part.func
            called = (
                function.id
                if isinstance(function, ast.Name)
                else function.attr
                if isinstance(function, ast.Attribute)
                else ""
            )
            if called == "load_secrets" or (
                called == "get"
                and isinstance(function, ast.Attribute)
                and isinstance(function.value, ast.Attribute)
                and function.value.attr == "environ"
            ):
                for argument in (
                    part.args if called == "load_secrets" else part.args[:1]
                ):
                    if isinstance(argument, ast.Constant) and (
                        name := _candidate(argument.value, explicit=True)
                    ):
                        names.add(name)
        if (
            isinstance(part, ast.Subscript)
            and isinstance(part.value, ast.Attribute)
            and part.value.attr == "environ"
            and isinstance(part.slice, ast.Constant)
        ):
            if name := _candidate(part.slice.value, explicit=True):
                names.add(name)
    return names


def _commands(source: str) -> set[str]:
    names: set[str] = set()
    for line in source.splitlines():
        try:
            words = shlex.split(line, comments=True)
        except ValueError:
            continue
        initial = (
            words[1:]
            if words and words[0] in {"export", "readonly", "local", "declare"}
            else words
        )
        for word in initial:
            if "=" not in word:
                break
            if name := _candidate(word.split("=", 1)[0]):
                names.add(name)
        command = Path(words[0]).name if words else ""
        tool = words[1] if command.startswith("python") and len(words) > 1 else command
        if Path(tool).name in {"fill_secret.py", "fill_secret"}:
            if name := _candidate(words[-1], explicit=True):
                names.add(name)
            for index, word in enumerate(words):
                if word in {"--env", "--key"} and index + 1 < len(words):
                    if name := _candidate(words[index + 1], explicit=True):
                        names.add(name)
                elif word.startswith(("--env=", "--key=")):
                    if name := _candidate(word.split("=", 1)[1], explicit=True):
                        names.add(name)
        # Single-quoted literals do not expand shell environment references.
        expandable = re.sub(r"'[^']*'", "", line)
        for match in re.finditer(r"\$(?:\{)?([A-Z][A-Z0-9_]{0,55})\b", expandable):
            if name := _candidate(match[1]):
                names.add(name)
    return names


def scan(
    folder: Path, declared: set[str], inventory: set[str] | None
) -> dict[str, object]:
    names: set[str] = set()
    indicators: set[str] = set()
    skipped = 0
    files = [folder / "TASK_DESCRIPTION.md"]
    for directory, dirs, children in os.walk(folder / "scripts", followlinks=False):
        skipped += sum(
            name not in _SKIP and (Path(directory) / name).is_symlink() for name in dirs
        )
        dirs[:] = [
            name
            for name in dirs
            if name not in _SKIP and not (Path(directory) / name).is_symlink()
        ]
        files.extend(
            Path(directory) / name
            for name in children
            if Path(name).suffix in _EXTENSIONS
        )
    for file in files:
        if file.is_symlink() or not file.resolve().is_relative_to(folder):
            skipped += 1
            continue
        try:
            source = file.read_text(encoding="utf-8")
            indicators.update(
                marker for marker in ("load_secrets", "fill_secret") if marker in source
            )
            names.update(_python(source) if file.suffix == ".py" else _commands(source))
            if file.name == "TASK_DESCRIPTION.md":
                for match in re.finditer(r"\bload_secrets\s*\([^\n)]*\)", source):
                    try:
                        names.update(_python(match[0]))
                    except SyntaxError:
                        continue
            # Only match already declared/inventoried names in free prose, never arbitrary literals.
            for name in declared | (inventory or set()):
                if re.search(
                    r"(?<![A-Z0-9_])" + re.escape(name) + r"(?![A-Z0-9_])", source
                ):
                    names.add(name)
        except (OSError, UnicodeError, SyntaxError):
            skipped += 1
    result: dict[str, object] = {
        "requires_secrets": bool(names or indicators),
        "keys": sorted(names),
        "indicators": sorted(indicators),
        "inventory_available": inventory is not None,
        "skipped_files": skipped,
        "warning": _WARNING if names or indicators else None,
        "limitations": [_LIMITATION],
    }
    if inventory is not None:
        result.update(
            known=sorted(names & inventory), unknown=sorted(names - inventory)
        )
    return result


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
