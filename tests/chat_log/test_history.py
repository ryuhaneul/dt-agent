# ruff: noqa: INP001
from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

from ductor_bot.chat_log.events import EventHub
from ductor_bot.chat_log.history import Entry, HistoryStore
from ductor_bot.chat_log.ids import filename_tid, from_tid, history_filename, new_id, to_tid
from ductor_bot.session.key import SessionKey


def entry(text: str = "hello", **kwargs) -> Entry:
    return Entry(role="user", source="telegram", text=text, **kwargs)


async def test_pagination_restart_and_import_order(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path)
    key = SessionKey.telegram(-123, 7)
    for index in range(100):
        await store.append(key, entry(str(index)))
    latest = await store.read("-123.7", limit=30)
    assert [item.seq for item in latest] == list(range(71, 101))
    previous = await store.read("-123.7", before=latest[0].seq, limit=30)
    assert [item.seq for item in previous] == list(range(41, 71))
    imported = Entry(role="bot", source="import", text="old", imported=True, seq=-999999)
    await store.append(key, imported)
    assert (await store.read("-123.7", limit=200))[0] == imported
    restarted = HistoryStore(tmp_path)
    assert (await restarted.append(key, entry())).seq == 101
    assert await restarted.count("-123.7") == 102
    assert (await restarted.last("-123.7")).seq == 101
    assert await restarted.delete("-123.7") == 102
    assert await restarted.count("-123.7") == 0
    assert (await store.append(key, entry())).seq == 1


async def test_corrupt_and_incomplete_lines(tmp_path: Path, caplog) -> None:
    store = HistoryStore(tmp_path)
    await store.append("42", entry("first"))
    path = tmp_path / "tg_42.jsonl"
    with path.open("a") as stream:
        stream.write("{invalid}\n{interrupted")
    assert (await HistoryStore(tmp_path).append("42", entry("second"))).seq == 2
    assert [item.text for item in await store.read("42")] == ["first", "second"]
    assert "Skipping corrupt history line" in caplog.text
    assert "interrupted" not in caplog.text


async def test_concurrent_stores_share_file_lock(tmp_path: Path) -> None:
    stores = [HistoryStore(tmp_path), HistoryStore(tmp_path)]
    await asyncio.gather(*(stores[index % 2].append("1", entry(str(index))) for index in range(50)))
    assert [item.seq for item in await stores[0].read("1")] == list(range(1, 51))


async def test_import_ref_search_and_undo(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path)
    ref = {"provider": "codex", "session_id": "example", "line": 1}
    await store.append("1", entry("Live Search", ts="2026-01-02T00:00:00Z"))
    await store.append(
        "1",
        Entry(
            role="bot",
            source="import",
            text="older SEARCH",
            seq=-1,
            imported=True,
            import_ref=ref,
            ts=None,
        ),
    )
    await store.append(
        "unknown",
        Entry(role="user", source="import", text="unknown", seq=-1, imported=True, ts=None),
    )
    assert await store.has_import_ref(ref)
    assert not await store.has_import_ref({**ref, "line": 2})
    hits = await store.search("search")
    assert [(hit["tid"], hit["seq"]) for hit in hits] == [("1", 1), ("1", -1)]
    assert hits[1]["ts"] is None
    assert await store.remove_imported() == 2
    assert not (tmp_path / "unknown.jsonl").exists()
    assert not await store.has_import_ref(ref)
    assert [item.text for item in await store.read("1")] == ["Live Search"]
    assert (await store.append("1", entry())).seq == 2


async def test_positive_maximum_not_line_count(tmp_path: Path) -> None:
    path = tmp_path / "tg_1.jsonl"
    rows = [entry(seq=19), entry(seq=3), entry(seq=-10, imported=True)]
    path.write_text("".join(json.dumps(asdict(row)) + "\n" for row in rows))
    assert (await HistoryStore(tmp_path).append("1", entry())).seq == 20


async def test_validation_and_no_constructor_side_effects(tmp_path: Path) -> None:
    root = tmp_path / "absent"
    store = HistoryStore(root)
    assert not root.exists()
    assert await store.read("1") == []
    assert await store.last("1") is None
    assert not root.exists()
    with pytest.raises(ValueError, match="negative sequence"):
        await store.append("1", entry(imported=True, seq=0))
    with pytest.raises(ValueError, match="limit"):
        await store.read("1", limit=0)


@pytest.mark.parametrize("read_only", [False, True])
async def test_missing_history_reads_do_not_create_locks(tmp_path: Path, read_only: bool) -> None:
    store = HistoryStore(tmp_path)
    if read_only:
        await asyncio.to_thread(tmp_path.chmod, 0o500)
    try:
        if read_only:
            with pytest.raises(PermissionError):
                (tmp_path / "write-probe").touch()
        for tid in ("555", "556"):
            assert await store.read(tid) == []
            assert await store.read_page(tid) == ([], False)
            summary = await store.summary(tid)
            assert summary.count == 0
            assert summary.imported_count == 0
            assert summary.last is None
        assert await asyncio.to_thread(lambda: list(tmp_path.glob("*.lock"))) == []
        assert await asyncio.to_thread(lambda: list(tmp_path.iterdir())) == []
    finally:
        await asyncio.to_thread(tmp_path.chmod, 0o700)


@pytest.mark.parametrize("tid", ["1", "-123.7", "0.0"])
def test_id_roundtrip(tid: str) -> None:
    assert to_tid(from_tid(tid)) == tid
    assert filename_tid(history_filename(tid)) == tid
    assert len(new_id()) == 12


@pytest.mark.parametrize("tid", ["../1", "1/2", "1.-2", "unknown", "1.2.3", ""])
def test_invalid_session_identifier(tid: str) -> None:
    with pytest.raises(ValueError, match="identifier"):
        from_tid(tid)


def test_unknown_filename_and_transport() -> None:
    assert history_filename("unknown") == "unknown.jsonl"
    assert filename_tid("unknown.jsonl") == "unknown"
    with pytest.raises(ValueError, match="filename"):
        filename_tid("../tg_1.jsonl")
    with pytest.raises(ValueError, match="Telegram"):
        to_tid(SessionKey.matrix(1))


async def test_event_hub_fanout_and_overflow(tmp_path: Path) -> None:
    hub = EventHub()
    slow = hub.subscribe()
    active = hub.subscribe()
    store = HistoryStore(tmp_path, hub)
    saved = await store.append("1", entry())
    assert (await active.get()) == {"type": "entry", "tid": "1", "entry": asdict(saved)}
    for index in range(1000):
        hub.publish({"type": "test", "index": index})
        assert (await active.get())["index"] == index
    assert await slow.get() is None
    hub.publish({"type": "remaining"})
    assert slow.empty()
    assert await active.get() == {"type": "remaining"}
    hub.unsubscribe(active)
    hub.publish({"type": "ignored"})
    assert active.empty()


async def test_separate_processes_share_sequence_lock(tmp_path: Path) -> None:
    script = """
import asyncio
import sys
from pathlib import Path
from ductor_bot.chat_log.history import Entry, HistoryStore

async def write():
    store = HistoryStore(Path(sys.argv[1]))
    for index in range(20):
        await store.append("1", Entry(role="user", source="telegram", text=str(index)))

asyncio.run(write())
"""
    processes = await asyncio.gather(
        *(
            asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                script,
                str(tmp_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            for _ in range(2)
        )
    )
    for process in processes:
        _, stderr = await process.communicate()
        assert process.returncode == 0, stderr.decode()
    assert [item.seq for item in await HistoryStore(tmp_path).read("1")] == list(range(1, 41))


async def test_truncated_utf8_line_preserves_history_and_sequence(tmp_path, caplog):
    store = HistoryStore(tmp_path)
    await store.append("1", entry("first", seq=9))
    await store.append("2", entry("other"))
    path = tmp_path / "tg_1.jsonl"
    with path.open("ab") as stream:
        stream.write(b'{"text":"\xe2\x82')
    reopened = HistoryStore(tmp_path)
    assert [item.text for item in await reopened.read("1")] == ["first"]
    assert len(caplog.records) == 1
    assert await reopened.count("1") == 1
    assert len(await reopened.search("")) == 2
    assert (await reopened.append("1", entry("second"))).seq == 2
    assert [item.text for item in await HistoryStore(tmp_path).read("1")] == ["first", "second"]


async def test_unreadable_sequence_checkpoint_falls_back_to_history(tmp_path, monkeypatch):
    store = HistoryStore(tmp_path)
    last = await store.append("1", entry("existing"))
    checkpoint = store._sequence_path("1")
    checkpoint.write_text("20")
    read_text = Path.read_text

    def unreadable(path, *args, **kwargs):
        if path == checkpoint:
            raise PermissionError("denied")
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    assert (await HistoryStore(tmp_path).append("1", entry("next"))).seq == last.seq + 1


async def test_index_reuses_parsing_and_updates_local_append(tmp_path, monkeypatch):
    from unittest.mock import Mock

    store = HistoryStore(tmp_path)
    for index in range(30):
        await store.append("1", entry(str(index)))
    assert len(await store.read("1", limit=3)) == 3
    parser = Mock(wraps=json.loads)
    monkeypatch.setattr(json, "loads", parser)
    assert [row.seq for row in await store.read("1", limit=3)] == [28, 29, 30]
    assert await store.count("1") == 30
    assert (await store.last("1")).seq == 30
    assert parser.call_count <= 4
    parser.reset_mock()
    await store.append("1", entry("new"))
    assert await store.count("1") == 31
    assert parser.call_count == 0


async def test_index_randomized_reference_equivalence(tmp_path):  # noqa: C901, PLR0912 - mutation matrix
    import random
    from datetime import UTC, datetime, timedelta

    from ductor_bot.chat_log.outbox import Outbox
    from ductor_bot.chat_log.prune import RetentionPruner
    from ductor_bot.infra.atomic_io import atomic_bytes_save
    from ductor_bot.infra.file_lock import exclusive_file_lock

    rng = random.Random(19406)  # noqa: S311 - reproducible test cases
    store = HistoryStore(tmp_path)
    other = HistoryStore(tmp_path)
    path = store._path("1")
    pruner = RetentionPruner(store, Outbox(tmp_path / "outbox"), tmp_path / "backups")
    operations = [
        "append",
        "import",
        "duplicate",
        "replace",
        "corrupt",
        "truncate",
        "delete",
        "other",
        "prune",
        "undo",
        "valid_tail",
    ]
    for step in range(150):
        operation = operations[step % len(operations)]
        if operation in {"append", "other"}:
            writer = store if operation == "append" else other
            await writer.append("1", entry(str(step)))
        elif operation == "import":
            await store.append(
                "1", entry(str(step), imported=True, seq=-rng.randint(1, 20), ts=None)
            )
        elif operation in {"duplicate", "replace", "corrupt", "truncate", "valid_tail"}:
            with exclusive_file_lock(path):
                if operation == "replace":
                    rows = [entry(str(step), seq=rng.randint(-10, 10), ts=None) for _ in range(5)]
                    atomic_bytes_save(
                        path, b"".join((json.dumps(asdict(row)) + "\n").encode() for row in rows)
                    )
                else:
                    with path.open("ab") as stream:
                        if operation == "corrupt":
                            stream.write(b'\n{invalid}\n{"unexpected":true}\n')
                        elif operation == "truncate":
                            stream.write(b'\n{"text":"\xe2\x82')
                        else:
                            row = entry(str(step), seq=7)
                            stream.write(
                                b"\n"
                                + json.dumps(asdict(row)).encode()
                                + (b"\n" if operation == "duplicate" else b"")
                            )
        elif operation == "delete":
            await other.delete("1")
        elif operation == "undo":
            await other.remove_imported()
        else:
            await store.append(
                "1", entry("expired", ts=(datetime.now(UTC) - timedelta(days=100)).isoformat())
            )
            await pruner.prune(history_days=30)
        expected = store._read(path)
        summary = await store.summary("1")
        assert summary.count == len(expected)
        assert summary.imported_count == sum(row.imported for row in expected)
        assert summary.last == (expected[-1] if expected else None)
        assert await store.count("1") == len(expected)
        assert await store.last("1") == (expected[-1] if expected else None)
        for before in (None, -5, 0, 7, 8, 100):
            limit = rng.randint(1, 10)
            selected = [row for row in expected if before is None or row.seq < before][-limit:]
            assert await store.read("1", before=before, limit=limit) == selected
            page, more = await store.read_page("1", before=before, limit=limit)
            assert page == selected
            assert more == bool(selected and any(row.seq < selected[0].seq for row in expected))


async def test_index_reads_during_concurrent_append(tmp_path):
    store, other = HistoryStore(tmp_path), HistoryStore(tmp_path)
    await store.append("1", entry("first"))
    await store.read("1")

    async def write():
        for index in range(50):
            await other.append("1", entry(str(index)))

    async def read():
        for _ in range(50):
            page, more = await store.read_page("1", limit=200)
            assert [row.seq for row in page] == list(range(1, len(page) + 1))
            assert not more

    await asyncio.gather(write(), read())
    assert await store.read("1", limit=200) == store._read(store._path("1"))
    assert await store.count("1") == 51


async def test_index_replacement_identity_and_stale_append_invalidation(tmp_path):
    import os

    from ductor_bot.infra.atomic_io import atomic_bytes_save
    from ductor_bot.infra.file_lock import exclusive_file_lock

    store = HistoryStore(tmp_path)
    first = await store.append("1", entry("first"))
    assert await store.last("1") == first
    path = store._path("1")
    previous = path.stat()
    replacement = entry("other", seq=first.seq, id=first.id, ts=first.ts)
    with exclusive_file_lock(path):
        atomic_bytes_save(
            path, (json.dumps(asdict(replacement), ensure_ascii=False) + "\n").encode()
        )
        os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    assert path.stat().st_size == previous.st_size
    assert await store.last("1") == replacement
    other = HistoryStore(tmp_path)
    await other.append("1", entry("external"))
    await store.append("1", entry("local after external"))
    assert "1" not in store._indexes
    assert await store.read("1") == store._read(path)
    assert [row.text for row in await store.read("1")] == [
        "other",
        "external",
        "local after external",
    ]
    await other.delete("1")
    assert await store.read("1") == []
    assert "1" not in store._indexes
    await other.append("1", entry("recreated"))
    assert [row.text for row in await store.read("1")] == ["recreated"]


async def test_index_accepts_rows_that_the_full_reader_accepts(tmp_path):
    # A row with a non-boolean "imported" value still parses as an Entry, so it must
    # not stop the indexed read or the count.
    history = HistoryStore(tmp_path)
    path = history.directory / history_filename("1")
    path.write_text(
        '{"role":"user","source":"web","text":"old","seq":1,"imported":null}\n'
        '{"role":"bot","source":"web","text":"latest","seq":2,"imported":false}\n',
        encoding="utf-8",
    )
    expected = [entry.text for entry in HistoryStore._read(path)][-1:]
    assert [entry.text for entry in await history.read("1", limit=1)] == expected == ["latest"]
    assert await history.count("1") == 2
    assert (await history.read("1", before=2, limit=1))[0].text == "old"
