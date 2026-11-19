"""Message attachment admission without changing durable history records."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest
from aiohttp import ServerDisconnectedError

from ductor_bot.chat_log.ids import history_filename
from ductor_bot.config import WebchatConfig
from ductor_bot.session.key import SessionKey
from ductor_bot.webchat.attachments import AttachmentIndex, MessageAttachments
from ductor_bot.webchat.uploads import (
    ATTACH_SETTINGS,
    UPLOADS,
    AttachmentSettings,
    UploadError,
    UploadStore,
)
from tests.webchat.test_bridge_api import (
    conversation as conversation,  # noqa: PLC0414 -- re-export shared pytest fixture
)
from tests.webchat.test_bridge_api import (
    headers,
    receive_until,
)


async def chunks(data):
    yield data


def service_at(tmp_path):
    workspace = tmp_path / "workspace"
    store = UploadStore(workspace / "webchat_files")
    settings = AttachmentSettings(tmp_path / "webchat/attach_settings.json", WebchatConfig())
    return MessageAttachments(store, settings, workspace, tmp_path / "webchat/attachments.jsonl")


async def uploaded(
    service, payload=b"sample text", *, tid="123", owner="browser", name="sample.txt", complete=True
):
    session = await service.store.start(
        tid, owner, {"name": name, "size": len(payload), "mime": "text/plain"}
    )
    if complete:
        await service.store.put(session.id, tid, owner, 0, chunks(payload))
        session = await service.store.complete(
            session.id, tid, owner, hashlib.sha256(payload).hexdigest()
        )
    return session


async def test_prepare_prompt_index_and_single_use(tmp_path):
    service = service_at(tmp_path)
    first = await uploaded(service, name="report.md")
    second = await uploaded(service, name="data.csv")
    prepared = await service.prepare("123", "browser", "Read these files", [first.id, second.id])
    assert prepared.text.count("[INCOMING FILE]") == 2
    assert "via Web" in prepared.text
    assert prepared.text.count("User message: Read these files") == 1
    assert f"webchat_files/{first.path}" in prepared.text
    assert "Original filename: report.md" in prepared.text
    indexed = await service.index.get("123", prepared.turn["user_entry_id"])
    assert [item["id"] for item in indexed] == [first.id, second.id]
    assert all(item["inline_allowed"] is False for item in indexed)
    assert all(item["path"].startswith("webchat_files/") for item in indexed)
    assert service.index.path.stat().st_mode & 0o777 == 0o600
    assert (await service.store.status(first.id, "123", "browser")).used_by == prepared.turn[
        "user_entry_id"
    ]
    with pytest.raises(UploadError, match="attachment_used"):
        await service.prepare("123", "browser", "again", [first.id])


async def test_batch_validation_and_count_limit_are_non_consuming(tmp_path):
    service = service_at(tmp_path)
    first = await uploaded(service)
    second = await uploaded(service, complete=False)
    for tid, owner, ids, code in [
        ("456", "browser", [first.id], "not_found"),
        ("123", "other", [first.id], "not_found"),
        ("123", "browser", [first.id, second.id], "upload_incomplete"),
        ("123", "browser", [first.id, first.id], "bad_attachments"),
        ("123", "browser", ["../file"], "bad_attachments"),
    ]:
        with pytest.raises(UploadError, match=code):
            await service.prepare(tid, owner, "text", ids)
        assert (await service.store.status(first.id, "123", "browser")).used_by is None
    await service.settings.put({"max_file_bytes": 1024, "max_files_per_message": 1})
    with pytest.raises(UploadError, match="too_many_attachments"):
        await service.prepare("123", "browser", "text", [first.id, second.id])
    assert not service.index.path.exists()


async def test_simultaneous_claims_only_admit_once(tmp_path):
    service = service_at(tmp_path)
    session = await uploaded(service)
    results = await asyncio.gather(
        service.prepare("123", "browser", "first", [session.id]),
        service.prepare("123", "browser", "second", [session.id]),
        return_exceptions=True,
    )
    assert sum(isinstance(item, UploadError) for item in results) == 1
    assert len(service.index.path.read_text().splitlines()) == 1


async def test_failed_index_append_rolls_back_claims(tmp_path, monkeypatch):
    service = service_at(tmp_path)
    first = await uploaded(service)
    second = await uploaded(service)

    def fail(*_args):
        raise OSError("simulated storage failure")

    monkeypatch.setattr(service.index, "append", fail)
    with pytest.raises(OSError, match="simulated"):
        await service.prepare("123", "browser", "text", [first.id, second.id])
    assert (await service.store.status(first.id, "123", "browser")).used_by is None
    assert (await service.store.status(second.id, "123", "browser")).used_by is None


async def test_partial_index_write_restores_original_length(tmp_path, monkeypatch):
    service = service_at(tmp_path)
    first = await uploaded(service)
    prepared = await service.prepare("123", "browser", "one", [first.id])
    previous = service.index.path.read_bytes()
    second = await uploaded(service)
    original = os.write

    def short_write(fd, data):
        return original(fd, data[:7])

    monkeypatch.setattr("ductor_bot.webchat.attachments.os.write", short_write)
    with pytest.raises(OSError, match="incomplete_attachment_index_write"):
        await service.prepare("123", "browser", "two", [second.id])
    assert service.index.path.read_bytes() == previous
    assert (await service.index.get("123", prepared.turn["user_entry_id"]))[0]["id"] == first.id
    assert (await service.store.status(second.id, "123", "browser")).used_by is None


@pytest.mark.parametrize("index_state", ["normal", "missing", "damaged", "unreadable"])
async def test_startup_claim_recovery_uses_only_healthy_index(
    tmp_path, index_state, monkeypatch, caplog
):
    service = service_at(tmp_path)
    queued = await uploaded(service)
    prepared = await service.prepare("123", "browser", "queued", [queued.id])
    orphan = await uploaded(service)
    record_path = service.store.incoming / f"{orphan.id}.json"
    raw = json.loads(record_path.read_text())
    raw["used_by"] = "orphan-entry"
    record_path.write_text(json.dumps(raw))
    restarted = service_at(tmp_path)
    if index_state == "missing":
        service.index.path.unlink()
    elif index_state == "damaged":
        with service.index.path.open("ab") as stream:
            stream.write(b"bad row\n")
    elif index_state == "unreadable":
        original = type(service.index.path).open

        def inaccessible(path, *args, **kwargs):
            if path == service.index.path:
                raise PermissionError
            return original(path, *args, **kwargs)

        monkeypatch.setattr(type(service.index.path), "open", inaccessible)
    await restarted.recover()
    queued_after = await restarted.store.status(queued.id, "123", "browser")
    orphan_after = await restarted.store.status(orphan.id, "123", "browser")
    if index_state in {"damaged", "unreadable"}:
        assert queued_after.used_by == prepared.turn["user_entry_id"]
        assert orphan_after.used_by == "orphan-entry"
        assert "Retaining 2 attachment claims: index_damaged_or_unavailable" in caplog.text
        assert "sample text" not in caplog.text
    else:
        assert orphan_after.used_by is None
        assert queued_after.used_by == (
            prepared.turn["user_entry_id"] if index_state == "normal" else None
        )


async def upload_via_bridge(harness, *, tid="-123.7", owner="browser", name="report.md"):
    prefix = f"/webchat/v1/topics/{tid}/uploads"
    auth = {**headers(), "X-Dtw-Owner": owner}
    payload = b"# Example\n"
    response = await harness.client.post(
        prefix, json={"size": len(payload), "name": name, "mime": "text/plain"}, headers=auth
    )
    assert response.status == 200
    upload_id = (await response.json())["id"]
    response = await harness.client.put(
        prefix + "/" + upload_id, data=payload, headers={**auth, "Upload-Offset": "0"}
    )
    assert response.status == 200
    response = await harness.client.post(
        prefix + "/" + upload_id + "/complete",
        json={"sha256": hashlib.sha256(payload).hexdigest()},
        headers=auth,
    )
    assert response.status == 200
    return upload_id


async def attachment_post(
    harness, ids, *, text="Read the attached file", owner="browser", tid="-123.7"
):
    return await harness.client.post(
        f"/webchat/v1/topics/{tid}/messages",
        json={"text": text, "client_id": "attachment-request", "attachment_ids": ids},
        headers={**headers(), "X-Dtw-Owner": owner},
    )


async def test_message_prompt_history_index_and_legacy_reader(conversation, tmp_path):
    h = conversation
    upload_id = await upload_via_bridge(h)
    ws = await h.client.ws_connect("/webchat/v1/events", headers=headers())
    assert (await attachment_post(h, [upload_id], text="")).status == 202
    await receive_until(
        ws, lambda event: event.get("type") == "turn" and event.get("phase") == "done"
    )
    entries = await h.runtime.history.read("-123.7")
    assert len(entries) == 2
    assert "[INCOMING FILE]" in entries[0].text
    assert "via Web" in h.calls[0][1]
    index = AttachmentIndex(h.paths.webchat_dir / "attachments.jsonl")
    metadata = await index.get("-123.7", entries[0].id)
    assert metadata[0]["id"] == upload_id
    assert (h.paths.workspace / metadata[0]["path"]).read_bytes() == b"# Example\n"
    source = (Path(__file__).parent / "fixtures/ecede25/history.py.txt").read_text()
    legacy_path = tmp_path / "legacy_history.py"
    legacy_path.write_text(source)
    spec = importlib.util.spec_from_file_location("legacy_attachment_history", legacy_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        legacy_entries = await module.HistoryStore(h.paths.chat_log_dir / "history").read("-123.7")
        assert [(entry.id, entry.text) for entry in legacy_entries] == [
            (entry.id, entry.text) for entry in entries
        ]
    finally:
        sys.modules.pop(spec.name, None)
    row = json.loads(
        (h.paths.chat_log_dir / "history" / history_filename("-123.7")).read_text().splitlines()[0]
    )
    assert "attachments" not in row
    response = await h.client.get(
        f"/webchat/v1/topics/-123.7/messages/{entries[0].id}/attachments", headers=headers()
    )
    public = (await response.json())["attachments"]
    assert [{key: item[key] for key in metadata[0]} for item in public] == metadata
    assert public[0]["download_url"].endswith("/" + upload_id)
    assert public[0]["preview_url"] is None
    response = await h.client.get(
        f"/webchat/v1/topics/-123.7/messages/{entries[0].id}/attachments",
        headers={**headers(), "X-Dtw-Owner": "other-browser"},
    )
    assert response.status == 200
    public = (await response.json())["attachments"]
    assert [{key: item[key] for key in metadata[0]} for item in public] == metadata
    assert public[0]["download_url"].endswith("/" + upload_id)
    assert public[0]["preview_url"] is None
    index.path.write_text("corrupted index\n")
    response = await h.client.get(
        f"/webchat/v1/topics/-123.7/messages/{entries[0].id}/attachments", headers=headers()
    )
    assert await response.json() == {"attachments": []}
    assert [(entry.id, entry.text) for entry in await h.runtime.history.read("-123.7")] == [
        (entry.id, entry.text) for entry in entries
    ]
    await ws.close()


async def test_message_ownership_limits_and_command_rejections(conversation):
    h = conversation
    first = await upload_via_bridge(h)
    for ids, owner, text, status in [
        ([first], "other", "text", 404),
        ([first, first], "browser", "text", 400),
        ([first], "browser", "/new", 400),
        ([first], "", "text", 403),
    ]:
        response = await attachment_post(h, ids, owner=owner, text=text)
        assert response.status == status
    await h.bot._orch._sessions.resolve_session(SessionKey.telegram(-123, 8))
    assert (await attachment_post(h, [first], tid="-123.8")).status == 404
    assert not h.calls
    await h.client.app[ATTACH_SETTINGS].put({"max_file_bytes": 1024, "max_files_per_message": 1})
    second = await upload_via_bridge(h)
    assert (await attachment_post(h, [first, second])).status == 413
    assert (await h.client.app[UPLOADS].status(first, "-123.7", "browser")).used_by is None
    ws = await h.client.ws_connect("/webchat/v1/events", headers=headers())
    assert (await attachment_post(h, [first])).status == 202
    await receive_until(ws, lambda event: event.get("phase") == "done")
    assert (await attachment_post(h, [first])).status == 409
    assert (
        await h.client.get(
            "/webchat/v1/topics/-123.7/messages/missing/attachments", headers=headers()
        )
    ).status == 404
    assert (
        await h.client.get("/webchat/v1/topics/-123.7/messages/missing/attachments")
    ).status == 403
    await ws.close()


async def test_attachments_join_existing_fifo_queue(conversation):
    h = conversation
    upload_id = await upload_via_bridge(h)
    lock = h.pool.get(h.key.lock_key)
    await lock.acquire()
    ws = await h.client.ws_connect("/webchat/v1/events", headers=headers())
    try:
        assert (await attachment_post(h, [upload_id])).status == 202
        await receive_until(ws, lambda event: event.get("phase") == "queued")
        second = await upload_via_bridge(h)
        response = await attachment_post(h, [second])
        assert response.status == 202
        second_turn = (await response.json())["turn_id"]
        await receive_until(
            ws, lambda event: event.get("turn_id") == second_turn and event.get("phase") == "queued"
        )
        assert (await h.client.app[UPLOADS].status(second, "-123.7", "browser")).used_by
        topics = await h.client.get("/webchat/v1/topics", headers=headers())
        queued = (await topics.json())["topics"][0]["turns"]
        assert len(queued) == 2
        assert [item["phase"] for item in queued] == ["queued", "queued"]
        assert queued[1]["turn_id"] == second_turn
    finally:
        lock.release()
        await ws.close()


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("cancelled", [False, True])
async def test_rejected_attachment_admission_allows_same_upload_retry(
    conversation, monkeypatch, restart, cancelled
):
    from ductor_bot.webchat.turns import _QueueJournal

    h = conversation
    upload_id = await upload_via_bridge(h)
    with monkeypatch.context() as failing:

        def reject(*_args):
            if cancelled:
                raise asyncio.CancelledError
            raise OSError("journal unavailable")

        failing.setattr(_QueueJournal, "accept", reject)
        if cancelled:
            with pytest.raises(ServerDisconnectedError):
                await attachment_post(h, [upload_id])
        else:
            response = await attachment_post(h, [upload_id])
            assert response.status == 503
            assert await response.json() == {"error": "generic"}
    rows, damaged = AttachmentIndex(h.paths.webchat_dir / "attachments.jsonl")._rows()
    assert not damaged
    assert rows[-1]["attachments"] == []
    assert (await h.client.app[UPLOADS].status(upload_id, "-123.7", "browser")).used_by is None
    assert await h.runtime.history.read("-123.7") == []
    assert await h.runtime.outbox.pending() == []
    if restart:
        restarted = MessageAttachments(
            UploadStore(h.paths.workspace / "webchat_files"),
            h.client.app[ATTACH_SETTINGS],
            h.paths.workspace,
            h.paths.webchat_dir / "attachments.jsonl",
        )
        await restarted.recover()
    ws = await h.client.ws_connect("/webchat/v1/events", headers=headers())
    try:
        response = await attachment_post(h, [upload_id])
        assert response.status == 202, await response.text()
        await receive_until(ws, lambda event: event.get("phase") == "done")
    finally:
        await ws.close()


async def test_cancellation_snapshot_recovery_releases_only_rejected_entry(tmp_path):
    service = service_at(tmp_path)
    accepted = await uploaded(service)
    accepted_message = await service.prepare("123", "browser", "accepted", [accepted.id])
    rejected = await uploaded(service)
    rejected_message = await service.prepare("123", "browser", "rejected", [rejected.id])
    service.index.append("123", rejected_message.turn["user_entry_id"], [])
    restarted = service_at(tmp_path)
    await restarted.recover()
    assert (await restarted.store.status(accepted.id, "123", "browser")).used_by == (
        accepted_message.turn["user_entry_id"]
    )
    assert (await restarted.store.status(rejected.id, "123", "browser")).used_by is None
    retried = await restarted.prepare("123", "browser", "retry", [rejected.id])
    assert retried.turn["user_entry_id"] != rejected_message.turn["user_entry_id"]
    await restarted.rollback("123", "browser", rejected_message)
    assert (await restarted.store.status(rejected.id, "123", "browser")).used_by == (
        retried.turn["user_entry_id"]
    )


async def test_rejecting_other_attachment_preserves_accepted_claim(conversation, monkeypatch):
    from ductor_bot.webchat.turns import _QueueJournal

    h = conversation
    accepted = await upload_via_bridge(h)
    ws = await h.client.ws_connect("/webchat/v1/events", headers=headers())
    try:
        assert (await attachment_post(h, [accepted])).status == 202
        await receive_until(ws, lambda event: event.get("phase") == "done")
        accepted_entry = (await h.client.app[UPLOADS].status(accepted, "-123.7", "browser")).used_by
        rejected = await upload_via_bridge(h)
        with monkeypatch.context() as failing:

            def reject(*_args):
                raise OSError("journal unavailable")

            failing.setattr(_QueueJournal, "accept", reject)
            assert (await attachment_post(h, [rejected])).status == 503
        assert (await h.client.app[UPLOADS].status(accepted, "-123.7", "browser")).used_by == (
            accepted_entry
        )
        response = await attachment_post(h, [rejected])
        assert response.status == 202, await response.text()
        await receive_until(ws, lambda event: event.get("phase") == "done")
        assert (await h.client.app[UPLOADS].status(accepted, "-123.7", "browser")).used_by == (
            accepted_entry
        )
    finally:
        await ws.close()


def index_item(number=1):
    return {
        "id": f"{number:032x}",
        "name": "file.txt",
        "size": 1,
        "mime": "text/plain",
        "path": "webchat_files/file.txt",
        "inline_allowed": False,
    }


async def test_index_cache_lookup_at_ten_thousand_rows_is_under_five_ms(tmp_path, monkeypatch):
    import time

    path = tmp_path / "attachments.jsonl"
    path.write_text(
        "".join(
            json.dumps({"tid": "123", "entry_id": f"{i:012x}", "attachments": [index_item(i)]})
            + "\n"
            for i in range(10000)
        )
    )
    index = AttachmentIndex(path)
    expected = [index_item(9999)]
    assert await index.get("123", f"{9999:012x}") == expected

    def no_parse():
        raise AssertionError("cache hit parsed the index again")

    monkeypatch.setattr(index, "_rows", no_parse)
    start = time.perf_counter()
    actual = await index.get("123", f"{9999:012x}")
    elapsed = time.perf_counter() - start
    assert actual == expected
    assert elapsed <= 0.005, elapsed
    actual[0]["name"] = "caller mutation"
    assert await index.get("123", f"{9999:012x}") == expected
    print(json.dumps({"index_rows": 10000, "cache_hit_ms": elapsed * 1000}))


async def test_index_cache_invalidates_for_external_append_and_replacement(tmp_path):
    path = tmp_path / "attachments.jsonl"
    writer = AttachmentIndex(path)
    reader = AttachmentIndex(path)
    writer.append("123", "entry", [index_item(1)])
    assert await reader.get("123", "entry") == [index_item(1)]
    writer.append("123", "entry", [index_item(2)])
    assert await reader.get("123", "entry") == [index_item(2)]
    writer.append("123", "entry", [])
    assert await reader.get("123", "entry") == []
    writer.append("123", "entry", [index_item(3)])
    assert await reader.get("123", "entry") == [index_item(3)]
    before = path.stat()
    replacement = tmp_path / "replacement"
    replacement.write_bytes(path.read_bytes().replace(b'"tid": "123"', b'"tid": "456"'))
    os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
    replacement.replace(path)
    assert path.stat().st_size == before.st_size
    assert path.stat().st_mtime_ns == before.st_mtime_ns
    assert await reader.get("123", "entry") == []
    assert await reader.get("456", "entry") == [index_item(3)]
    path.unlink()
    assert await reader.get("456", "entry") == []


async def test_index_cache_publication_does_not_hide_concurrent_write(tmp_path, monkeypatch):
    import threading

    path = tmp_path / "attachments.jsonl"
    writer, reader = AttachmentIndex(path), AttachmentIndex(path)
    writer.append("123", "entry", [index_item(1)])
    read_done, publish = threading.Event(), threading.Event()
    original = reader._read_cache

    def delayed():
        snapshot = original()
        read_done.set()
        if not publish.wait(5):
            raise AssertionError("cache publication was not released")
        return snapshot

    monkeypatch.setattr(reader, "_read_cache", delayed)
    pending = asyncio.create_task(reader.get("123", "entry"))
    try:
        assert await asyncio.to_thread(read_done.wait, 5)
        await asyncio.to_thread(writer.append, "123", "entry", [index_item(2)])
    finally:
        publish.set()
    assert await pending == [index_item(1)]
    assert await reader.get("123", "entry") == [index_item(2)]


async def test_message_page_reports_indexed_attachments_without_entry_queries(
    conversation, monkeypatch
):
    from ductor_bot.chat_log.history import Entry

    h = conversation
    entries = [
        await h.runtime.history.append(h.key, Entry(role="user", source="web", text=f"plain {i}"))
        for i in range(100)
    ]

    async def no_entry_query(*_args):
        raise AssertionError("message page performed a per-entry history lookup")

    monkeypatch.setattr(h.runtime.history, "get_entry", no_entry_query)
    url = "/webchat/v1/topics/-123.7/messages?limit=100"
    page = await (await h.client.get(url, headers=headers())).json()
    assert len(page["entries"]) == 100
    assert not any(entry["has_attachments"] for entry in page["entries"])
    index = AttachmentIndex(h.paths.webchat_dir / "attachments.jsonl")
    index.append("-123.7", entries[0].id, [index_item()])
    page = await (await h.client.get(url, headers=headers())).json()
    assert [entry["id"] for entry in page["entries"] if entry["has_attachments"]] == [entries[0].id]
    index.append("-123.7", entries[0].id, [])
    page = await (await h.client.get(url, headers=headers())).json()
    assert not any(entry["has_attachments"] for entry in page["entries"])


async def test_live_entry_event_reports_existing_attachment(conversation):
    from ductor_bot.chat_log.history import Entry

    h = conversation
    entry = Entry(role="user", source="web", text="attached")
    index = AttachmentIndex(h.paths.webchat_dir / "attachments.jsonl")
    index.append("-123.7", entry.id, [index_item()])
    ws = await h.client.ws_connect("/webchat/v1/events", headers=headers())
    try:
        await h.runtime.history.append(h.key, entry)
        events = await receive_until(ws, lambda value: value.get("type") == "entry")
        assert events[-1]["entry"]["has_attachments"] is True
    finally:
        await ws.close()
