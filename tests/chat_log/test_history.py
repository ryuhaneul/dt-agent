# ruff: noqa: INP001
from __future__ import annotations

import asyncio
import json
import os
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
    if read_only and os.geteuid() == 0:
        # Root bypasses directory mode bits, so chmod cannot verify denied writes.
        pytest.skip("Root bypasses the read-only directory permission check")
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


async def test_count_after_distinguishes_rows_from_sequences_and_rebuilds_index(tmp_path):
    store = HistoryStore(tmp_path)
    await store.append("1", Entry("user", "import", "first", imported=True, seq=-2))
    await store.append("1", Entry("bot", "import", "second", imported=True, seq=-1))

    assert await store.count_after("1", -2) == 1
    await store.append(
        "1", Entry("event", "import", "duplicate", kind="log", imported=True, seq=-1)
    )
    assert await store.count_after("1", None) == (await store.summary("1")).count == 3
    assert await store.count_after("1", -2) == 1
    assert await store.count_after("1", -3) == 2

    other = HistoryStore(tmp_path)
    await other.append("1", Entry("bot", "import", "earlier", imported=True, seq=-4))
    assert await store.count_after("1", None) == 4
    assert await store.count_after("1", -3) == 2
    assert await store.count_after("1", -2) == 1
    assert await store.count_after("1", -1) == 0
    assert await store.count_after("2", None) == 0
    assert await store.count_after("2", -2) == 0


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


async def test_button_metadata_keeps_old_entry_reader_and_events_compatible(tmp_path):
    from ductor_bot.chat_log.events import EventHub

    hub = EventHub()
    queue = hub.subscribe()
    store = HistoryStore(tmp_path, hub)
    buttons = [[{"text": "Model", "callback_data": "ms:claude"}]]
    saved = await store.append("1.7", Entry("bot", "web", "Choose"), buttons=buttons)
    raw = json.loads(store._path("1.7").read_text())
    assert Entry(**raw) == saved
    assert "buttons" not in raw
    assert await store.metadata("1.7") == {saved.id: {"buttons": buttons}}
    assert (await queue.get())["entry"]["buttons"] == buttons
    assert store.tids() == ["1.7"]
    assert await HistoryStore(tmp_path).metadata("1.7") == {saved.id: {"buttons": buttons}}
    await store.delete("1.7")
    assert not (tmp_path / "1.7.meta.jsonl").exists()


async def test_activity_metadata_survives_button_update_and_compaction(tmp_path, monkeypatch):
    store = HistoryStore(tmp_path)
    tid = "1.7"
    saved = await store.append(tid, Entry("bot", "web", "Reply"))
    activities = [{"type": "tool", "name": "Read", "seq": 1, "offset": 0}]
    original = store._path(tid).read_bytes()
    await store.record_activity(tid, saved.id, activities)
    assert await store.metadata(tid) == {}
    assert await store.metadata(tid, include_activity=True) == {saved.id: {"activity": activities}}
    assert json.loads(store._metadata_path(tid).read_text()) == {
        "id": saved.id,
        "activity": activities,
    }

    buttons = [[{"text": "Next", "callback_data": "Next"}]]
    assert await store.record_buttons(tid, saved.id, buttons, only_if_missing=True) == {
        "buttons": buttons,
    }
    assert await store.metadata(tid) == {saved.id: {"buttons": buttons}}
    assert await store.metadata(tid, include_activity=True) == {
        saved.id: {"buttons": buttons, "activity": activities},
    }

    monkeypatch.setattr("ductor_bot.chat_log.history._METADATA_COMPACT_BYTES", 1)
    await store.record_mirror_revision(tid, saved.id, 2)
    expected = {"buttons": buttons, "activity": activities, "mirror_revision": 2}
    assert await HistoryStore(tmp_path).metadata(tid, include_activity=True) == {saved.id: expected}
    assert json.loads(store._metadata_path(tid).read_text()) == {"id": saved.id, **expected}
    assert store._path(tid).read_bytes() == original


@pytest.mark.parametrize("invalid", [None, "activity", {"type": "tool"}])
async def test_metadata_ignores_non_list_activity(tmp_path, invalid):
    store = HistoryStore(tmp_path)
    saved = await store.append("1", Entry("bot", "web", "Reply"))
    buttons = [[{"text": "Next", "callback_data": "Next"}]]
    store._metadata_path("1").write_text(
        json.dumps({"id": saved.id, "buttons": buttons, "activity": invalid}) + "\n"
    )
    assert await store.metadata("1", include_activity=True) == {saved.id: {"buttons": buttons}}


async def test_record_activity_skips_imported_entries(tmp_path):
    store = HistoryStore(tmp_path)
    imported = await store.append("1", Entry("bot", "import", "Reply", seq=-1, imported=True))
    await store.record_activity("1", imported.id, [{"type": "tool", "name": "Read"}])
    assert not store._metadata_path("1").exists()


async def test_code_display_metadata_preserves_v154_readers(tmp_path: Path) -> None:
    hub = EventHub()
    queue = hub.subscribe()
    store = HistoryStore(tmp_path, hub)
    tid = "1.7"
    saved = await store.append(tid, entry("echo hi"))
    history = tmp_path / history_filename(tid)
    raw = history.read_bytes()
    queue.get_nowait()

    await store.record_display_markdown(tid, saved.id, "```sh\necho hi\n```")

    expected_meta = {"buttons": [], "display_markdown": "```sh\necho hi\n```"}
    assert await HistoryStore(tmp_path).metadata(tid) == {saved.id: expected_meta}
    assert queue.get_nowait() == {
        "type": "entry_meta", "tid": tid, "entry_id": saved.id, "meta": expected_meta
    }
    assert history.read_bytes() == raw
    assert Entry(**json.loads(raw)) == saved

    item = json.loads((tmp_path / f"{tid}.meta.jsonl").read_bytes())
    assert isinstance(item, dict)
    assert isinstance(item.get("id"), str)
    assert isinstance(item.get("buttons"), list)
    legacy_keys = (
        "buttons", "telegram_message_id", "telegram_html_text", "answered", "text",
        "selector_revision", "mirror_revision",
    )
    assert {item["id"]: {key: item[key] for key in legacy_keys if key in item}} == {
        saved.id: {"buttons": []}
    }


@pytest.mark.parametrize("role", ["bot", "event"])
@pytest.mark.parametrize(
    "text",
    [
        "Choose\n[button: Yes ] [button:No]\n[button:Next]",
        "```text\n[button:preserve]\n```\n`[button:inline]`\n[button:Run]",
        "[button:" + "界" * 30 + "]\n[button:  ]",
        "[button: ]\nplain\n[button:unterminated",
    ],
)
async def test_response_markers_use_telegram_parser_and_sidecar(tmp_path, role, text):
    from ductor_bot.messenger.telegram.buttons import extract_buttons

    hub = EventHub()
    queue = hub.subscribe()
    store = HistoryStore(tmp_path, hub)
    cleaned, markup = extract_buttons(text)
    expected = (
        None
        if markup is None
        else [
            [{"text": button.text, "callback_data": button.callback_data} for button in row]
            for row in markup.inline_keyboard
        ]
    )
    saved = await store.append("1", Entry(role=role, source="telegram", text=text))
    assert saved.text == cleaned
    metadata = await HistoryStore(tmp_path).metadata("1")
    assert metadata == ({saved.id: {"buttons": expected}} if expected else {})
    assert (await store.read("1"))[0] == saved
    # Sidecar fields never enter the legacy Entry JSONL schema.
    assert Entry(**json.loads((tmp_path / history_filename("1")).read_text())) == saved
    event = queue.get_nowait()["entry"]
    assert event["text"] == cleaned
    assert event.get("buttons") == expected


async def test_user_markers_imports_and_explicit_selector_rows_are_preserved(tmp_path):
    store = HistoryStore(tmp_path)
    text = "Text\n[button:Label]"
    user = await store.append("1", entry(text))
    imported = Entry(role="bot", source="import", imported=True, seq=-1, text=text)
    assert await store.append("1", imported) == imported
    assert user.text == text
    buttons = [[{"text": "Model", "callback_data": "ms:claude"}]]
    saved = await store.append("1", Entry(role="bot", source="web", text=text), buttons=buttons)
    assert saved.text == "Text"
    assert await store.metadata("1") == {saved.id: {"buttons": buttons}}


async def test_keyboard_binding_answered_restart_events_and_legacy_reader(tmp_path):
    hub = EventHub()
    queue = hub.subscribe()
    store = HistoryStore(tmp_path, hub)
    saved = await store.append("1.7", Entry("bot", "telegram", "Pick [button:Yes]"))
    original = store._path("1.7").read_bytes()
    await store.bind_telegram_message("1.7", saved.id, 42)
    assert await store.entry_for_telegram_message("1.7", 42) == saved.id
    assert await store.entry_for_telegram_message("1.7", 43) is None
    assert not await store.is_answered("1.7", saved.id)
    assert await store.mark_answered("1.7", saved.id, "Yes")
    reopened = HistoryStore(tmp_path)
    assert await reopened.is_answered("1.7", saved.id)
    assert not await reopened.mark_answered("1.7", saved.id, "No")
    meta = (await reopened.metadata("1.7"))[saved.id]
    assert meta["telegram_message_id"] == 42
    assert meta["answered"] == "Yes"
    assert meta["buttons"] == [[{"text": "Yes", "callback_data": "Yes"}]]
    assert store._path("1.7").read_bytes() == original
    assert Entry(**json.loads(original)) == saved
    # The old sidecar reader only inspects id/buttons and can ignore new keys.
    for line in store._metadata_path("1.7").read_text().splitlines():
        item = json.loads(line)
        assert item["id"] == saved.id
        assert isinstance(item["buttons"], list)
    published = [queue.get_nowait() for _ in range(queue.qsize())]
    assert published[-1] == {"type": "entry_meta", "tid": "1.7", "entry_id": saved.id, "meta": meta}


async def test_answered_claim_is_atomic_across_stores_and_preserves_binding(tmp_path):
    first, second = HistoryStore(tmp_path), HistoryStore(tmp_path)
    entry = await first.append("1", Entry("bot", "web", "[button:Yes]"))
    results = await asyncio.gather(
        first.mark_answered("1", entry.id, "Yes"), second.mark_answered("1", entry.id, "Yes")
    )
    assert sorted(results) == [False, True]
    await second.bind_telegram_message("1", entry.id, 12)
    assert (await first.metadata("1"))[entry.id]["answered"] == "Yes"
    assert not await first.mark_answered("1", "unknown", "Yes")


async def test_keyboard_html_snapshot_survives_restart_and_clears_on_rebinding(tmp_path):
    store = HistoryStore(tmp_path)
    entry = await store.append("1", Entry("bot", "telegram", "**Full answer** [button:Yes]"))
    original = store._path("1").read_bytes()
    await store.bind_telegram_message("1", entry.id, 42, html_text="<b>Last chunk</b>")
    assert await store.mark_answered("1", entry.id, "Yes")
    metadata = (await HistoryStore(tmp_path).metadata("1"))[entry.id]
    assert metadata["telegram_html_text"] == "<b>Last chunk</b>"
    assert metadata["answered"] == "Yes"
    await store.bind_telegram_message("1", entry.id, 43)
    metadata = (await store.metadata("1"))[entry.id]
    assert metadata["telegram_html_text"] is None
    assert metadata["answered"] == "Yes"
    assert store._path("1").read_bytes() == original


async def test_annotation_error_keeps_answered_claim_and_caller_continues(tmp_path):
    from unittest.mock import AsyncMock, MagicMock

    from aiogram.exceptions import TelegramNetworkError

    from ductor_bot.messenger.telegram.callbacks import edit_button_choice

    store = HistoryStore(tmp_path)
    entry = await store.append("1", Entry("bot", "telegram", "Pick [button:Yes]"))
    assert await store.mark_answered("1", entry.id, "Yes")
    bot = MagicMock()
    bot.edit_message_text = AsyncMock(side_effect=TelegramNetworkError(MagicMock(), "unavailable"))
    bot.edit_message_reply_markup = AsyncMock(
        side_effect=TelegramNetworkError(MagicMock(), "unavailable")
    )
    process = AsyncMock()
    await edit_button_choice(bot, 1, 42, "Yes", html_text="Original")
    await process("Yes")
    process.assert_awaited_once_with("Yes")
    assert await HistoryStore(tmp_path).is_answered("1", entry.id)


async def test_selector_sync_metadata_survives_restart_without_entry_changes(tmp_path):
    from ductor_bot.chat_log.events import EventHub

    hub = EventHub()
    store = HistoryStore(tmp_path, hub)
    rows = [[{"text": "Next", "callback_data": "ms:p:claude"}]]
    entry = await store.append("1", Entry("bot", "web", "Original"), buttons=rows)
    before = (tmp_path / history_filename("1")).read_bytes()
    queue = hub.subscribe()
    result = await store.record_selector_response("1", entry.id, "Final <text>", [])
    assert result["selector_revision"] == 1
    assert result["text"] == "Final <text>"
    assert result["buttons"] == []
    assert (await queue.get())["meta"] == result
    assert (await HistoryStore(tmp_path).metadata("1"))[entry.id] == result
    assert (tmp_path / history_filename("1")).read_bytes() == before
    assert (await store.read("1"))[0].text == "Original"


def _large_metadata_sidecar(tmp_path):
    store = HistoryStore(tmp_path)
    path = store._metadata_path("1")
    records = [
        {
            "id": f"entry-{index % 3}",
            "buttons": [[{"text": "Refresh", "callback_data": "usg:refresh"}]],
            "telegram_message_id": 42 + index % 3,
            "telegram_html_text": "<pre>" + "x" * 3072 + "</pre>",
            "text": "x" * 3072,
            "answered": "Yes" if index % 3 == 0 else None,
            "selector_revision": index,
            "mirror_revision": index,
        }
        for index in range(64)
    ]
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    assert path.stat().st_size > 256 * 1024
    return store, path


async def test_metadata_compaction_preserves_latest_records_and_read_results(tmp_path):
    store, path = _large_metadata_sidecar(tmp_path)
    before = await store.metadata("1")
    await store.record_mirror_revision("1", "entry-0", before["entry-0"]["mirror_revision"])
    assert await HistoryStore(tmp_path).metadata("1") == before
    records = [json.loads(line) for line in path.read_bytes().splitlines()]
    assert len(records) == len(before)
    assert {
        record["id"]: {key: value for key, value in record.items() if key != "id"}
        for record in records
    } == before
    assert path.stat().st_size < 256 * 1024


async def test_large_metadata_sidecar_waits_for_growth_after_compaction(tmp_path, monkeypatch):
    from unittest.mock import Mock

    store = HistoryStore(tmp_path)
    save = Mock(wraps=store._save_compacted_metadata)
    monkeypatch.setattr(store, "_save_compacted_metadata", save)
    await store.record_display_markdown("1", "large", "x" * (256 * 1024))
    assert store._metadata_path("1").stat().st_size >= 256 * 1024
    for revision in range(10):
        await store.record_mirror_revision("1", "small", revision)
    assert save.call_count <= 1
    assert (await store.metadata("1"))["small"]["mirror_revision"] == 9


@pytest.mark.parametrize("failure", ["temp_creation", "replace"])
async def test_metadata_compaction_failure_preserves_appended_original(
    tmp_path, monkeypatch, caplog, failure
):
    import ductor_bot.chat_log.history as history_module

    store, path = _large_metadata_sidecar(tmp_path)
    before = path.read_bytes()
    snapshots = []
    save = store._save_compacted_metadata

    def capture_save(target, content):
        assert target == path
        snapshots.append(path.read_bytes())
        return save(target, content)

    monkeypatch.setattr(store, "_save_compacted_metadata", capture_save)
    if failure == "temp_creation":

        def fail_creation(*_args, **_kwargs):
            raise OSError("private-compaction-details")

        monkeypatch.setattr(history_module.tempfile, "NamedTemporaryFile", fail_creation)
    else:
        replace = Path.replace

        def fail_replace(source, target):
            if target == path:
                raise OSError("private-compaction-details")
            return replace(source, target)

        monkeypatch.setattr(Path, "replace", fail_replace)
    await store.record_mirror_revision("1", "entry-0", 999)
    assert len(snapshots) == 1
    assert path.read_bytes() == snapshots[0]
    assert path.read_bytes().startswith(before)
    assert len(path.read_bytes().splitlines()) == 65
    assert (await store.metadata("1"))["entry-0"]["mirror_revision"] == 999
    assert not list(tmp_path.glob("*.tmp"))
    assert "OSError" in caplog.text
    assert "private-compaction-details" not in caplog.text


async def test_metadata_compaction_keeps_readers_and_process_writer_safe(tmp_path, monkeypatch):
    import threading

    store, path = _large_metadata_sidecar(tmp_path)
    before = await store.metadata("1")
    replacing, release = threading.Event(), threading.Event()
    replace = Path.replace

    def pause_replace(source, target):
        if target == path:
            replacing.set()
            assert release.wait(10)
        return replace(source, target)

    monkeypatch.setattr(Path, "replace", pause_replace)
    write = None
    old_reader = None
    script = """
import sys
from pathlib import Path
from ductor_bot.chat_log.history import HistoryStore
from ductor_bot.infra.file_lock import exclusive_file_lock, LockTimeoutError
store = HistoryStore(Path(sys.argv[1]))
print("ready", flush=True)
sys.stdin.buffer.read()
try:
    with exclusive_file_lock(store._metadata_path("1"), timeout=0):
        print("unlocked", flush=True)
except LockTimeoutError:
    print("locked", flush=True)
store._update_metadata("1", "other-process", {"buttons": [], "text": "Other writer"})
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        str(tmp_path),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert await asyncio.wait_for(process.stdout.readline(), 15) == b"ready\n"
        write = asyncio.create_task(store.record_mirror_revision("1", "entry-0", 999))
        assert await asyncio.to_thread(replacing.wait, 3)
        expected = {**before, "entry-0": {**before["entry-0"], "mirror_revision": 999}}
        # Readers use another store's lock and see the intact pre-replacement snapshot.
        assert await HistoryStore(tmp_path).metadata("1") == expected
        old_bytes = path.read_bytes()
        old_reader = path.open("rb")
        process.stdin.close()
        assert await asyncio.wait_for(process.stdout.readline(), 3) == b"locked\n"
    finally:
        release.set()
        if write is not None:
            await write
        process.stdin.close()
        _, stderr = await process.communicate()
        assert process.returncode == 0, stderr.decode()
        if old_reader is not None:
            with old_reader:
                assert old_reader.read() == old_bytes
    latest = await HistoryStore(tmp_path).metadata("1")
    assert {key: value for key, value in latest.items() if key != "other-process"} == expected
    assert latest["other-process"]["text"] == "Other writer"
    assert len(path.read_bytes().splitlines()) == 4


async def test_compaction_syncs_published_inode_then_directory(tmp_path, monkeypatch):
    import os
    import stat

    store, path = _large_metadata_sidecar(tmp_path)
    original_inode = path.stat().st_ino
    events = []
    fsync, replace = os.fsync, Path.replace

    def sync(fd):
        info = os.fstat(fd)
        kind = "directory" if stat.S_ISDIR(info.st_mode) else "file"
        events.append(("fsync", kind, info.st_ino))
        if kind == "file" and info.st_ino != original_inode:
            # Bytes must have reached the descriptor before its fsync.
            content = Path(f"/proc/self/fd/{fd}").read_bytes()
            assert content.endswith(b"\n")
            assert len(content.splitlines()) == 3
        return fsync(fd)

    def renamed(source, target):
        if target == path:
            events.append(("replace", "file", source.stat().st_ino))
        return replace(source, target)

    monkeypatch.setattr(os, "fsync", sync)
    monkeypatch.setattr(Path, "replace", renamed)
    await store.record_mirror_revision("1", "entry-0", 999)
    published_inode = path.stat().st_ino
    assert published_inode != original_inode
    assert events == [
        ("fsync", "file", original_inode),
        ("fsync", "file", published_inode),
        ("replace", "file", published_inode),
        ("fsync", "directory", path.parent.stat().st_ino),
    ]


@pytest.mark.parametrize("failure", ["temp_fsync", "directory_fsync"])
async def test_compaction_sync_failure_preserves_latest_state(
    tmp_path, monkeypatch, caplog, failure
):
    import os
    import stat

    store, path = _large_metadata_sidecar(tmp_path)
    original_inode = path.stat().st_ino
    expected = await store.metadata("1")
    expected["entry-0"]["mirror_revision"] = 999
    fsync = os.fsync
    synced, failures, snapshots = [], [], []

    def sync(fd):
        info = os.fstat(fd)
        directory = stat.S_ISDIR(info.st_mode)
        if info.st_ino == original_inode:
            snapshots.append(path.read_bytes())
        if (failure == "directory_fsync" and directory) or (
            failure == "temp_fsync" and not directory and info.st_ino != original_inode
        ):
            failures.append(info.st_ino)
            raise OSError("private-sync-details")
        result = fsync(fd)
        synced.append(info.st_ino)
        return result

    monkeypatch.setattr(os, "fsync", sync)
    await store.record_mirror_revision("1", "entry-0", 999)
    assert len(failures) == 1
    assert await store.metadata("1") == expected
    if failure == "temp_fsync":
        assert path.read_bytes() == snapshots[0]
    else:
        assert path.stat().st_ino in synced
    assert not list(tmp_path.glob("*.tmp"))
    assert "OSError" in caplog.text
    assert "private-sync-details" not in caplog.text


@pytest.mark.parametrize("active", [False, True])
async def test_topic_delete_removes_only_its_idle_selector_locks(tmp_path, active):
    store = HistoryStore(tmp_path)
    for index in range(50):
        saved = await store.append(
            "1.7",
            Entry("bot", "web", str(index)),
            buttons=[[{"text": "Next", "callback_data": "ms:p:claude"}]],
        )
        async with store.selector_step("1.7", saved.id, "ms:p:claude", 0):
            pass
    other = store.selector_gate("1.8", "other")
    busy = store.selector_gate("1.7", "active") if active else None
    if busy is not None:
        await busy.acquire()
    try:
        assert await store.delete("1.7") == 50
        assert {key for key in store._selector_locks if key[0] == "1.7"} == (
            {("1.7", "active")} if active else set()
        )
        assert store.selector_gate("1.8", "other") is other
        if busy is not None:
            assert store.selector_gate("1.7", "active") is busy
    finally:
        if busy is not None:
            busy.release()
    await store.delete("1.7")
    assert not any(key[0] == "1.7" for key in store._selector_locks)


async def test_topic_delete_keeps_selector_lock_during_waiter_handoff(tmp_path, monkeypatch):
    import ductor_bot.chat_log.history as history_module

    store = HistoryStore(tmp_path)
    lock = store.selector_gate("1.7", "waiting")
    await lock.acquire()
    waiter = asyncio.create_task(lock.acquire())
    await asyncio.sleep(0)
    complete = history_module.complete_write

    async def release_after_delete(*args, **kwargs):
        result = await complete(*args, **kwargs)
        lock.release()
        return result

    monkeypatch.setattr(history_module, "complete_write", release_after_delete)
    try:
        await store.delete("1.7")
        assert store.selector_gate("1.7", "waiting") is lock
    finally:
        await asyncio.wait_for(waiter, 1)
        lock.release()
