"""Conversation history CLI commands."""

from __future__ import annotations

import asyncio
import json
import sys

from rich.console import Console

from ductor_bot.chat_log.history import HistoryStore
from ductor_bot.chat_log.ids import history_filename
from ductor_bot.chat_log.importer import TranscriptImporter
from ductor_bot.chat_log.outbox import Outbox, OutboxItem
from ductor_bot.chat_log.prune import RetentionPruner
from ductor_bot.chat_log.settings import chat_log_active, validate_chat_log
from ductor_bot.config import AgentConfig
from ductor_bot.i18n import init, t_rich
from ductor_bot.infra.atomic_io import atomic_text_save
from ductor_bot.infra.file_lock import exclusive_file_lock
from ductor_bot.workspace.paths import DuctorPaths, resolve_paths

_console = Console()


def _print_chat_log_status(config: AgentConfig, paths: DuctorPaths) -> None:
    history = HistoryStore(paths.chat_log_dir / "history")
    tids = history.tids()
    entries = 0
    for tid in tids:
        try:
            entries += len(history._read(history.directory / history_filename(tid)))
        except FileNotFoundError:
            continue
    outbox = Outbox(paths.chat_log_dir / "outbox")
    items: dict[str, OutboxItem] = {}
    unreadable = 0
    for state in ("pending", "done"):
        for path in (outbox.directory / state).glob("*.json"):
            try:
                item = outbox._load(path)
            except (OSError, ValueError, TypeError):
                unreadable += 1
                continue
            items[item.id] = item
    errors = validate_chat_log(config)
    _console.print(
        t_rich(
            "chat_log.status",
            active=t_rich(
                "chat_log.active" if chat_log_active(config, "main") else "chat_log.inactive"
            ),
            errors=", ".join(errors) if errors else t_rich("chat_log.no_errors"),
            files=len(tids),
            entries=entries,
            pending=sum(item.state == "pending" for item in items.values()),
            failed=sum(item.state == "failed" for item in items.values()),
            unreadable=unreadable,
            corrupt=sum(1 for _ in (outbox.directory / "corrupt").glob("*.json")),
        )
    )


def _prune_arguments(args: list[str]) -> tuple[int, bool, bool] | None:
    if not args or args[0] != "prune":
        return None
    flags = args[1:]
    if flags.count("--older-than") != 1:
        return None
    index = flags.index("--older-than")
    try:
        days = int(flags[index + 1])
    except (ValueError, IndexError):
        return None
    options = flags[:index] + flags[index + 2 :]
    if days <= 0 or any(option not in {"--backups", "--dry-run"} for option in options):
        return None
    if len(options) != len(set(options)):
        return None
    return days, "--backups" in options, "--dry-run" in options


def _prune_chat_log(paths: DuctorPaths, options: tuple[int, bool, bool]) -> None:
    days, backups, dry_run = options
    pruner = RetentionPruner(
        HistoryStore(paths.chat_log_dir / "history"),
        Outbox(paths.chat_log_dir / "outbox"),
        paths.webchat_dir / "backups",
    )
    try:
        result = asyncio.run(
            pruner.prune(
                history_days=days,
                done_days=days,
                backup_days=days if backups else None,
                dry_run=dry_run,
            )
        )
    except OverflowError:
        _console.print(t_rich("chat_log.usage"))
        raise SystemExit(2) from None
    _console.print(
        t_rich(
            "chat_log.prune_preview" if dry_run else "chat_log.prune_summary",
            entries=result.entries,
            files=result.history_files,
            outbox=result.outbox_items,
            backups=result.backups,
            bytes=result.bytes,
            failures=result.failures,
        )
    )


def _undo_import(paths: DuctorPaths, history: HistoryStore) -> int:
    state_path = paths.chat_log_dir / "import_refs.json"
    with exclusive_file_lock(state_path):
        removed = asyncio.run(history.remove_imported())
        atomic_text_save(state_path, "[]\n")
    return removed


def cmd_chat_log(args: list[str]) -> None:
    if "chat-log" in args:
        args = args[args.index("chat-log") + 1 :]
    status = args == ["status"]
    valid_import = (
        bool(args)
        and args[0] == "import"
        and all(arg in {"--dry-run", "--undo"} for arg in args[1:])
        and len(args[1:]) == len(set(args[1:]))
    )
    prune = _prune_arguments(args)
    if not status and not valid_import and prune is None:
        _console.print(t_rich("chat_log.usage"))
        raise SystemExit(2)
    paths = resolve_paths()
    raw = (
        json.loads(paths.config_path.read_text(encoding="utf-8"))
        if paths.config_path.is_file()
        else {}
    )
    config = AgentConfig.model_validate(raw)
    init(config.language)
    if prune is not None:
        _prune_chat_log(paths, prune)
        return
    if status:
        _print_chat_log_status(config, paths)
        return
    dry_run = "--dry-run" in args
    undo = "--undo" in args
    if not dry_run and not chat_log_active(config, "main"):
        _console.print(t_rich("chat_log.disabled"))
        raise SystemExit(1)
    history = HistoryStore(paths.chat_log_dir / "history")
    if undo:
        count = (
            sum(
                sum(
                    entry.imported or tid == "unknown"
                    for entry in history._read(history.directory / filename)
                )
                for tid, filename in ((tid, history_filename(tid)) for tid in history.tids())
            )
            if dry_run
            else _undo_import(paths, history)
        )
        _console.print(t_rich("chat_log.import_undone", entries=count))
        return
    files = TranscriptImporter(paths, config).run(dry_run=dry_run)
    for file in files:
        _console.print(
            f"{file.path} -> {file.tid}: {len(file.entries)}", markup=False, highlight=False
        )
    _console.print(
        t_rich(
            "chat_log.import_summary",
            files=len(files),
            entries=sum(len(file.entries) for file in files),
            unknown=sum(len(file.entries) for file in files if file.tid == "unknown"),
        )
    )


if __name__ == "__main__":
    cmd_chat_log(sys.argv[1:])
