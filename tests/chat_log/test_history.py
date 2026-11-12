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
