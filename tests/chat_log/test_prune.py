# ruff: noqa: INP001
"""Retention boundaries, previews, terminal queues and concurrent writers."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
import time_machine

from ductor_bot.chat_log.cli import cmd_chat_log
from ductor_bot.chat_log.history import Entry, HistoryStore
from ductor_bot.chat_log.outbox import Outbox
from ductor_bot.infra.file_lock import LockTimeoutError, exclusive_file_lock
from ductor_bot.workspace.paths import DuctorPaths

NOW = datetime(2026, 11, 11, 5, tzinfo=UTC)


def pruner(paths, history=None, outbox=None):
    from ductor_bot.chat_log.prune import RetentionPruner

    return RetentionPruner(
        history or HistoryStore(paths.chat_log_dir / "history"),
        outbox or Outbox(paths.chat_log_dir / "outbox"),
        paths.webchat_dir / "backups",
    )


@pytest.fixture
def paths(tmp_path):
    return DuctorPaths(ductor_home=tmp_path)


@time_machine.travel(NOW, tick=False)
async def test_history_strict_boundary_preserves_unknown_and_corrupt_lines(paths):
    history = HistoryStore(paths.chat_log_dir / "history")
    old = await history.append("1", Entry("user", "telegram", "old", ts="2026-11-04T04:59:59Z"))
    boundary = await history.append(
        "1", Entry("bot", "telegram", "edge", ts="2026-11-04T05:00:00Z")
    )
    unknown = await history.append("1", Entry("bot", "telegram", "no date", ts=None))
    path = history._path("1")
    malformed = b'{"ts":"bad"}\nnot json\n'
    with path.open("ab") as stream:
        stream.write(malformed)
    original = path.read_bytes()
    result = await pruner(paths, history).prune(history_days=7)
    assert result.entries == 1
    assert result.bytes == len(original.splitlines(keepends=True)[0])
    assert result.history_files == 1
    assert path.read_bytes().endswith(malformed)
    assert [entry.id for entry in await history.read("1")] == [boundary.id, unknown.id]
    assert old.id.encode() not in path.read_bytes()
    appended = await history.append("1", Entry("user", "telegram", "new"))
    assert appended.seq == unknown.seq + 1


@time_machine.travel(NOW, tick=False)
async def test_empty_history_removed_but_lock_remains(paths):
    history = HistoryStore(paths.chat_log_dir / "history")
    await history.append(
        "unknown", Entry("user", "import", "old", imported=True, seq=-1, ts="2020-01-01T00:00:00Z")
    )
    result = await pruner(paths, history).prune(history_days=7)
    assert result.entries == 1
    assert not history._path("unknown").exists()
    assert history._path("unknown").with_suffix(".jsonl.lock").exists()


@time_machine.travel(NOW, tick=False)
async def test_completed_queue_only_and_strict_boundary(paths):
    outbox = Outbox(paths.chat_log_dir / "outbox")
    with time_machine.travel(NOW - timedelta(days=8), tick=False):
        pending = await outbox.enqueue(kind="bus", chat_id=2, text="pending", sent_chunks=2)
        for state in ("sent", "failed", "dropped"):
            item = await outbox.enqueue(kind="bus", chat_id=1, text=state)
            if state == "sent":
                await outbox.mark_sent(item.id)
            elif state == "failed":
                await outbox.mark_failed(item.id)
            else:
                await outbox.drop_tid("1")
                # Keep an in-progress delivery in pending storage.
                await outbox.enqueue(kind="bus", chat_id=2, text="in progress", sent_chunks=1)
    # Put a pending-state item in done to prove state is checked too.
    done = outbox.directory / "done" / "00000000000000000000-aaaaaaaaaaaa.json"
    done.write_text(json.dumps(asdict(pending)))
    with time_machine.travel(NOW - timedelta(days=7), tick=False):
        edge = await outbox.enqueue(kind="bus", chat_id=3, text="boundary")
        await outbox.mark_sent(edge.id)
    before = {path.name: path.read_bytes() for path in (outbox.directory / "pending").glob("*")}
    result = await pruner(paths, outbox=outbox).prune(done_days=7)
    assert result.outbox_items == 3
    assert done.exists()
    assert len(list((outbox.directory / "done").glob("*.json"))) == 2
    assert before == {
        path.name: path.read_bytes() for path in (outbox.directory / "pending").glob("*")
    }


@time_machine.travel(NOW, tick=False)
async def test_dry_run_changes_no_file_or_directory(paths):
    history = HistoryStore(paths.chat_log_dir / "history")
    path = history._path("1")
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(asdict(Entry("user", "telegram", "old", ts="2020-01-01T00:00:00Z"))) + "\n"
    )
    directory = paths.webchat_dir / "backups" / "2020-01-01"
    directory.mkdir(parents=True)
    (directory / "PERSONA.md.120000").write_bytes(b"backup")
    before = {str(p): p.read_bytes() for p in paths.ductor_home.rglob("*") if p.is_file()}
    preview = await pruner(paths).prune(history_days=7, backup_days=7, done_days=7, dry_run=True)
    assert preview.entries == preview.backups == 1
    assert preview.bytes == path.stat().st_size + 6
    assert before == {str(p): p.read_bytes() for p in paths.ductor_home.rglob("*") if p.is_file()}
    actual = await pruner(paths).prune(history_days=7, backup_days=7, done_days=7)
    assert actual == preview
    assert not directory.exists()


@time_machine.travel(NOW, tick=False)
async def test_backups_use_kst_creation_path_not_mtime(paths, caplog):
    directory = paths.webchat_dir / "backups"
    files = {
        "2026-11-11/PERSONA.md.140000": False,
        "2026-11-04/BEHAVIOR.md.140000": False,  # exact seven day boundary
        "2026-11-04/MAINMEMORY.md.135959.2": True,
        "2026-11-03/SHAREDMEMORY.md.235959": True,
        "2020-01-01/unrecognized": False,
        "2020-01-01/PERSONA.md.999999": False,
    }
    for relative in files:
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"backup")
        os.utime(path, (0, 0))
    unknown_directory = directory / "unrecognized"
    unknown_directory.mkdir()
    result = await pruner(paths).prune(backup_days=7)
    assert unknown_directory.is_dir()
    assert result.backups == 2
    for relative, removed in files.items():
        assert (directory / relative).exists() is not removed
    assert caplog.text.count("Skipping unrecognized backup paths") == 1


@time_machine.travel(NOW, tick=False)
async def test_all_disabled_does_not_create_or_remove(paths):
    result = await pruner(paths).prune(history_days=0, backup_days=None)
    assert result.bytes == result.entries == result.backups == result.outbox_items == 0
    assert list(paths.ductor_home.iterdir()) == []


@time_machine.travel(NOW, tick=False)
async def test_prune_waits_for_file_lock_and_keeps_concurrent_append(paths):
    history = HistoryStore(paths.chat_log_dir / "history")
    await history.append("1", Entry("user", "telegram", "old", ts="2020-01-01T00:00:00Z"))
    path = history._path("1")
    held, release = threading.Event(), threading.Event()

    def writer():
        with exclusive_file_lock(path):
            held.set()
            assert release.wait(3)
            with path.open("ab") as stream:
                stream.write(
                    (json.dumps(asdict(Entry("bot", "telegram", "new", seq=2))) + "\n").encode()
                )

    thread = asyncio.create_task(asyncio.to_thread(writer))
    assert await asyncio.to_thread(held.wait, 3)
    pruning = asyncio.create_task(pruner(paths, history).prune(history_days=7))
    try:
        await asyncio.sleep(0.05)
        assert not pruning.done()
    finally:
        release.set()
        await thread
    assert (await pruning).entries == 1
    assert [entry.text for entry in await history.read("1")] == ["new"]


@time_machine.travel(NOW, tick=False)
def test_cli_preview_and_manual_cleanup_with_recording_disabled(paths, capsys):
    path = paths.chat_log_dir / "history" / "tg_1.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(asdict(Entry("user", "telegram", "old", ts="2020-01-01T00:00:00Z"))) + "\n"
    )
    with patch("ductor_bot.chat_log.cli.resolve_paths", return_value=paths):
        cmd_chat_log(["prune", "--older-than", "7", "--backups", "--dry-run"])
        assert path.exists()
        assert "1" in capsys.readouterr().out
        cmd_chat_log(["chat-log", "prune", "--older-than", "7"])
    assert not path.exists()
    assert "1" in capsys.readouterr().out


@pytest.mark.parametrize(
    "args",
    [
        ["prune"],
        ["prune", "--older-than", "0"],
        ["prune", "--older-than", "-1"],
        ["prune", "--older-than", "1.5"],
        ["prune", "--older-than", "7", "--undo"],
        ["prune", "--older-than", "7", "--older-than", "8"],
    ],
)
def test_cli_invalid_prune_arguments(args):
    with pytest.raises(SystemExit) as exc:
        cmd_chat_log(args)
    assert exc.value.code == 2


@time_machine.travel(NOW, tick=False)
async def test_fresh_web_backup_of_old_source_is_preserved(paths):
    from ductor_bot.webchat.files import WebFiles

    source = paths.workspace / "PERSONA.md"
    source.parent.mkdir(parents=True)
    source.write_text("original")
    os.utime(source, (0, 0))
    files = WebFiles(paths)
    before = await files.get("PERSONA.md")
    saved = await files.put("PERSONA.md", "updated", before["sha256"])
    backup = paths.webchat_dir / saved["backup"]
    assert backup.read_bytes() == b"original"
    assert (await pruner(paths).prune(backup_days=7)).backups == 0
    assert backup.read_bytes() == b"original"


@pytest.mark.parametrize("keep_survivor", [False, True])
@time_machine.travel(NOW, tick=False)
async def test_pruned_sequence_continues_after_reopen(paths, keep_survivor):
    history = HistoryStore(paths.chat_log_dir / "history")
    await history.append(
        "1",
        Entry("user", "telegram", "first", ts=None if keep_survivor else "2020-01-01T00:00:00Z"),
    )
    last = await history.append("1", Entry("bot", "telegram", "last", ts="2020-01-01T00:00:00Z"))
    await pruner(paths, history).prune(history_days=7)
    reopened = HistoryStore(history.directory)
    saved = await reopened.append("1", Entry("user", "telegram", "new"))
    assert saved.seq == last.seq + 1
    assert [entry.text for entry in await reopened.read("1")][-1] == "new"


@pytest.mark.parametrize("error", [PermissionError("denied"), LockTimeoutError("lock timeout")])
@time_machine.travel(NOW, tick=False)
async def test_topic_failure_keeps_other_topics_and_backups_running(paths, error, caplog):
    history = HistoryStore(paths.chat_log_dir / "history")
    for tid in ("1", "2"):
        await history.append(tid, Entry("user", "telegram", "old", ts="2020-01-01T00:00:00Z"))
    directory = paths.webchat_dir / "backups" / "2020-01-01"
    directory.mkdir(parents=True)
    (directory / "PERSONA.md.120000").write_bytes(b"backup")
    cleaner = pruner(paths, history)
    original = cleaner._history_file

    def fail_first(tid, cutoff, dry_run):
        if tid == "1":
            raise error
        return original(tid, cutoff, dry_run)

    with patch.object(cleaner, "_history_file", side_effect=fail_first):
        result = await cleaner.prune(history_days=7, backup_days=7)
    assert result.failures == 1
    assert result.entries == result.backups == 1
    assert await history.count("1") == 1
    assert await history.count("2") == 0
    assert "History retention failed" in caplog.text


@time_machine.travel(NOW, tick=False)
async def test_sequence_checkpoint_is_not_history_and_delete_removes_it(paths):
    history = HistoryStore(paths.chat_log_dir / "history")
    await history.append("1", Entry("user", "telegram", "old", ts="2020-01-01T00:00:00Z"))
    await pruner(paths, history).prune(history_days=7)
    checkpoint = history._sequence_path("1")
    assert checkpoint.exists()
    assert history.tids() == []
    assert await history.read("1") == []
    assert await history.search("old") == []
    assert (await pruner(paths, history).prune(history_days=7)).entries == 0
    assert await history.delete("1") == 0
    assert not checkpoint.exists()
    assert (await history.append("1", Entry("user", "telegram", "new"))).seq == 1


@pytest.mark.parametrize("checkpoint", [None, b"not a number", b"\xff"])
async def test_invalid_or_missing_checkpoint_falls_back_to_history(paths, checkpoint):
    history = HistoryStore(paths.chat_log_dir / "history")
    last = await history.append("1", Entry("user", "telegram", "existing"))
    if checkpoint is not None:
        history._sequence_path("1").write_bytes(checkpoint)
    reopened = HistoryStore(history.directory)
    assert (await reopened.append("1", Entry("bot", "telegram", "next"))).seq == last.seq + 1


@time_machine.travel(NOW, tick=False)
async def test_stray_history_filename_does_not_stop_retention(paths, caplog):
    history = HistoryStore(paths.chat_log_dir / "history")
    await history.append("1", Entry("user", "telegram", "old", ts="2020-01-01T00:00:00Z"))
    stray = history.directory / "notes.jsonl"
    stray.write_bytes(b"keep this file")
    result = await pruner(paths, history).prune(history_days=7)
    assert result.entries == 1
    assert stray.read_bytes() == b"keep this file"
    assert history.tids() == []
    assert "Skipping unrecognized history filename" in caplog.text


@time_machine.travel(NOW, tick=False)
@pytest.mark.parametrize("manual", [False, True])
async def test_quarantine_retention_uses_mtime_and_preserves_boundary(paths, manual):
    outbox = Outbox(paths.chat_log_dir / "outbox")
    directory = outbox.directory / "corrupt"
    directory.mkdir(parents=True)
    old = directory / "old.json"
    edge = directory / "edge.json"
    recent = directory / "recent.json"
    for path, age in ((old, 8), (edge, 7), (recent, 1)):
        path.write_bytes(b"{invalid")
        modified = (NOW - timedelta(days=age)).timestamp()
        os.utime(path, (modified, modified))
    if manual:
        cleaner = pruner(paths, outbox=outbox)
        preview = await cleaner.prune(done_days=7, dry_run=True)
        assert preview.outbox_items == 1
        assert preview.bytes == old.stat().st_size
        assert old.exists()
        result = await cleaner.prune(done_days=7)
        assert result == preview
    else:
        assert await outbox.prune() == 1
    assert not old.exists()
    assert edge.exists()
    assert recent.exists()


@time_machine.travel(NOW, tick=False)
@pytest.mark.parametrize("manual", [False, True])
@pytest.mark.parametrize("failure", ["read", "unlink"])
async def test_queue_retention_skips_each_io_failure_and_continues(
    paths, monkeypatch, caplog, manual, failure
):
    outbox = Outbox(paths.chat_log_dir / "outbox")
    with time_machine.travel(NOW - timedelta(days=8), tick=False):
        for text in ("unreadable", "expired"):
            item = await outbox.enqueue(kind="bus", chat_id=1, text=text)
            await outbox.mark_sent(item.id)
    done = sorted((outbox.directory / "done").glob("*.json"))
    corrupt = outbox.directory / "corrupt"
    corrupt.mkdir()
    bad_stat = corrupt / "unreadable.json"
    old = corrupt / "expired.json"
    for path in (bad_stat, old):
        path.write_bytes(b"{bad")
        modified = (NOW - timedelta(days=8)).timestamp()
        os.utime(path, (modified, modified))
    operation = "open" if failure == "read" else "unlink"
    original = getattr(type(old), operation)
    stat = type(old).stat

    def unreadable(path, *args, **kwargs):
        if path == done[0]:
            raise PermissionError("file unavailable")
        return original(path, *args, **kwargs)

    def unavailable_stat(path, *args, **kwargs):
        if path == bad_stat:
            raise PermissionError("stat unavailable")
        return stat(path, *args, **kwargs)

    monkeypatch.setattr(type(old), operation, unreadable)
    monkeypatch.setattr(type(old), "stat", unavailable_stat)
    if manual:
        result = await pruner(paths, outbox=outbox).prune(done_days=7)
        assert result.outbox_items == 2
        assert result.failures == 2
    else:
        assert await outbox.prune() == 2
    assert done[0].exists()
    assert not done[1].exists()
    assert not old.exists()
    assert "PermissionError" in caplog.text
