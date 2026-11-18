"""Upload integrity, bounded streaming, ownership and temporary retention."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import struct
import subprocess
import sys
import time
import zlib
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

from ductor_bot.config import AgentConfig, WebchatConfig
from ductor_bot.webchat.bridge import create_bridge_app
from ductor_bot.webchat.topics import WebTopics
from ductor_bot.webchat.uploads import UPLOAD_TTL, UPLOADS, UploadError, UploadStore
from ductor_bot.workspace.paths import DuctorPaths

_BASE = "/webchat/v1/topics/123/uploads"
_HEADERS = {"X-Dtw-Bridge-Token": "test-token", "X-Dtw-Owner": "session-one"}


async def chunks(data):
    for start in range(0, len(data), 65536):
        yield data[start : start + 65536]


async def begin(store, data, *, mime="application/octet-stream", name="sample.bin"):
    return await store.start("123", "session-one", {"name": name, "size": len(data), "mime": mime})


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(
        "ductor_bot.webchat.bridge.store_path", lambda _home: tmp_path / "secrets.env"
    )

    async def lookup(_self, tid):
        return {"tid": tid, "read_only": tid == "789"} if tid in {"123", "456", "789"} else None

    monkeypatch.setattr(WebTopics, "lookup", lookup)
    bot = SimpleNamespace(
        config=AgentConfig(
            allowed_user_ids=[123],
            webchat=WebchatConfig(
                enabled=True,
                domain="chat.example.com",
                allowed_ips=["203.0.113.0/24"],
                acme_email="admin@example.com",
            ),
        ),
        _orch=SimpleNamespace(paths=DuctorPaths(ductor_home=tmp_path)),
    )
    runtime = SimpleNamespace(topic_registry_lock=asyncio.Lock())
    async with TestClient(TestServer(create_bridge_app(bot, runtime, "test-token"))) as test_client:
        yield test_client


async def test_resume_restart_hash_and_idempotent_complete(tmp_path):
    store = UploadStore(tmp_path / "files")
    data = b"\x00\xff" * 600_000
    session = await begin(store, data, name="../../outside/secret.png")
    first = 800_000
    await store.put(session.id, "123", "session-one", 0, chunks(data[:first]))
    restarted = UploadStore(store.directory)
    assert (await restarted.status(session.id, "123", "session-one")).offset == first
    await restarted.put(session.id, "123", "session-one", first, chunks(data[first:]))
    finished = await restarted.complete(
        session.id, "123", "session-one", hashlib.sha256(data).hexdigest()
    )
    path = store.directory / finished.path
    assert path.is_relative_to(store.directory)
    assert path.suffix == ".bin"
    assert "secret" not in path.name
    assert path.read_bytes() == data
    assert finished.name == "../../outside/secret.png"
    assert not (tmp_path / "outside").exists()
    again = await restarted.complete(session.id, "123", "session-one", finished.sha256.upper())
    assert again.path == finished.path
    assert "owner" not in finished.public()


async def test_rejections_preserve_committed_offset(tmp_path):
    store = UploadStore(tmp_path / "files", chunk_bytes=4, max_file_bytes=10)
    session = await begin(store, b"abcdef")
    await store.put(session.id, "123", "session-one", 0, chunks(b"abcd"))
    for offset, data, code in [(0, b"ab", "offset_conflict"), (4, b"efg", "too_large")]:
        with pytest.raises(UploadError, match=code):
            await store.put(session.id, "123", "session-one", offset, chunks(data))
        assert (await store.status(session.id, "123", "session-one")).offset == 4
    with pytest.raises(UploadError, match="incomplete_upload"):
        await store.complete(session.id, "123", "session-one", "0" * 64)
    await store.put(session.id, "123", "session-one", 4, chunks(b"ef"))
    with pytest.raises(UploadError, match="hash_mismatch"):
        await store.complete(session.id, "123", "session-one", "0" * 64)
    await store.complete(session.id, "123", "session-one", hashlib.sha256(b"abcdef").hexdigest())
    with pytest.raises(UploadError, match="too_large"):
        await begin(store, b"a" * 11)
    other = await begin(store, b"a" * 10)
    with pytest.raises(UploadError, match="too_large"):
        await store.put(other.id, "123", "session-one", 0, chunks(b"a" * 5))


async def test_interrupted_and_short_chunk_do_not_publish_offset(tmp_path):
    store = UploadStore(tmp_path / "files")
    session = await begin(store, b"abcdef")
    await store.put(session.id, "123", "session-one", 0, chunks(b"ab"))

    async def interrupted():
        yield b"cd"
        raise ConnectionResetError

    with pytest.raises(ConnectionResetError):
        await store.put(session.id, "123", "session-one", 2, interrupted())
    with pytest.raises(UploadError, match="incomplete_chunk"):
        await store.put(session.id, "123", "session-one", 2, chunks(b"cd"), length=4)
    assert (await store.status(session.id, "123", "session-one")).offset == 2
    assert (store.incoming / f"{session.id}.part").read_bytes() == b"ab"
    assert not list(store.incoming.glob("*.chunk"))
    # Recover a crash between the append and metadata publication.
    with (store.incoming / f"{session.id}.part").open("ab") as stream:
        stream.write(b"unexpected")
    assert (await store.status(session.id, "123", "session-one")).offset == 2
    await store.put(session.id, "123", "session-one", 2, chunks(b"cdef"))


async def test_cancelled_put_and_idle_timeout(tmp_path):
    store = UploadStore(tmp_path / "files", idle_timeout=0.02)
    session = await begin(store, b"abcd")
    started = asyncio.Event()

    async def stalled():
        yield b"ab"
        started.set()
        await asyncio.Event().wait()
        yield b"cd"

    task = asyncio.create_task(store.put(session.id, "123", "session-one", 0, stalled()))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(TimeoutError):
        await store.put(session.id, "123", "session-one", 0, stalled())
    assert (await store.status(session.id, "123", "session-one")).offset == 0
    assert not list(store.incoming.glob("*.chunk"))


async def test_simultaneous_puts_do_not_duplicate_bytes(tmp_path):
    store = UploadStore(tmp_path / "files")
    session = await begin(store, b"abcdef")
    results = await asyncio.gather(
        store.put(session.id, "123", "session-one", 0, chunks(b"abc")),
        store.put(session.id, "123", "session-one", 0, chunks(b"abc")),
        return_exceptions=True,
    )
    assert sum(isinstance(value, UploadError) for value in results) == 1
    assert (store.incoming / f"{session.id}.part").read_bytes() == b"abc"


@pytest.mark.parametrize(
    ("payload", "mime", "extension"),
    [
        (b"<!doctype html><html><body>Hello</body></html>", "text/html", ".html"),
        (b'<svg xmlns="http://www.w3.org/2000/svg"></svg>', "image/svg+xml", ".svg"),
        (b"hello", "text/plain", ".txt"),
        (b"\x00\xff", "application/octet-stream", ".bin"),
    ],
)
async def test_sniffed_extension_and_active_content_is_download_only(
    tmp_path, payload, mime, extension
):
    store = UploadStore(tmp_path / "files")
    session = await begin(store, payload, mime=mime, name="untrusted.png")
    await store.put(session.id, "123", "session-one", 0, chunks(payload))
    finished = await store.complete(
        session.id, "123", "session-one", hashlib.sha256(payload).hexdigest()
    )
    assert finished.mime == mime
    assert finished.path.endswith(extension)
    assert finished.inline_allowed is False
    assert (store.directory / finished.path).read_bytes() == payload


@pytest.mark.parametrize(
    ("name", "mime", "extension"),
    [
        ("../../file.py", "text/x-python", ".py"),
        ("file.md", "text/markdown", ".md"),
        ("file.json", "application/json", ".json"),
        ("file.js", "application/javascript", ".js"),
        ("file.unknown", "text/plain", ".txt"),
    ],
)
async def test_verified_text_preserves_only_allowed_extensions(tmp_path, name, mime, extension):
    payload = b"sample UTF-8 text"
    store = UploadStore(tmp_path / "files")
    session = await begin(store, payload, name=name, mime=mime)
    await store.put(session.id, "123", "session-one", 0, chunks(payload))
    result = await store.complete(
        session.id, "123", "session-one", hashlib.sha256(payload).hexdigest()
    )
    assert result.path.endswith(extension)
    assert result.mime == "text/plain"
    assert result.inline_allowed is False


async def test_text_sniff_checks_past_the_prefix(tmp_path):
    payload = b"a" * 65536 + b"\x00\xff"
    store = UploadStore(tmp_path / "files")
    session = await begin(store, payload, name="bad.py", mime="text/plain")
    await store.put(session.id, "123", "session-one", 0, chunks(payload))
    with pytest.raises(UploadError, match="mime_mismatch"):
        await store.complete(session.id, "123", "session-one", hashlib.sha256(payload).hexdigest())


async def test_raster_is_verified_and_bytes_preserved(tmp_path):
    data = io.BytesIO()
    Image.new("RGB", (9, 7), "white").save(data, format="PNG")
    payload = data.getvalue()
    store = UploadStore(tmp_path / "files")
    session = await begin(store, payload, mime="image/png")
    await store.put(session.id, "123", "session-one", 0, chunks(payload))
    finished = await store.complete(
        session.id, "123", "session-one", hashlib.sha256(payload).hexdigest()
    )
    assert finished.inline_allowed is True
    assert finished.mime == "image/png"
    assert (store.directory / finished.path).read_bytes() == payload
    bad = await begin(store, b"pretend PNG", mime="image/png")
    await store.put(bad.id, "123", "session-one", 0, chunks(b"pretend PNG"))
    with pytest.raises(UploadError, match="mime_mismatch"):
        await store.complete(
            bad.id, "123", "session-one", hashlib.sha256(b"pretend PNG").hexdigest()
        )
    broken = await begin(store, payload[:20], mime="image/png")
    await store.put(broken.id, "123", "session-one", 0, chunks(payload[:20]))
    with pytest.raises(UploadError, match="invalid_image"):
        await store.complete(
            broken.id, "123", "session-one", hashlib.sha256(payload[:20]).hexdigest()
        )


async def test_image_pixel_limit_uses_dimensions_before_decoding(tmp_path):
    buffer = io.BytesIO()
    Image.new("RGB", (1, 1)).save(buffer, format="PNG")
    payload = bytearray(buffer.getvalue())
    payload[16:24] = struct.pack(">II", 10001, 10000)
    payload[29:33] = struct.pack(">I", zlib.crc32(payload[12:29]))
    store = UploadStore(tmp_path / "files")
    session = await begin(store, payload, mime="image/png")
    await store.put(session.id, "123", "session-one", 0, chunks(payload))
    with pytest.raises(UploadError, match="image_too_large"):
        await store.complete(session.id, "123", "session-one", hashlib.sha256(payload).hexdigest())


async def test_symlinks_cannot_escape_storage(tmp_path):
    store = UploadStore(tmp_path / "files")
    session = await begin(store, b"abcd")
    outside = tmp_path / "outside"
    outside.write_bytes(b"unchanged")
    part = store.incoming / f"{session.id}.part"
    part.unlink()
    part.symlink_to(outside)
    with pytest.raises(UploadError, match="storage_unavailable"):
        await store.put(session.id, "123", "session-one", 0, chunks(b"abcd"))
    assert outside.read_bytes() == b"unchanged"
    with pytest.raises(UploadError, match="not_found"):
        await store.status("../outside", "123", "session-one")


async def test_cleanup_expires_partials_but_keeps_completed_and_fresh(tmp_path):
    store = UploadStore(tmp_path / "files")
    expired = await begin(store, b"unfinished")
    fresh = await begin(store, b"fresh")
    complete = await begin(store, b"done")
    await store.put(complete.id, "123", "session-one", 0, chunks(b"done"))
    done = await store.complete(
        complete.id, "123", "session-one", hashlib.sha256(b"done").hexdigest()
    )
    old = time.time() - UPLOAD_TTL - 1
    for session in [expired, complete]:
        path = store.incoming / f"{session.id}.json"
        record = json.loads(path.read_text())
        record["updated"] = old
        path.write_text(json.dumps(record))
    orphan = store.incoming / f"{'f' * 32}.part"
    orphan.write_bytes(b"orphan")
    os.utime(orphan, (old, old))
    assert await store.cleanup() == 1
    assert not (store.incoming / f"{expired.id}.json").exists()
    assert not (store.incoming / f"{expired.id}.part").exists()
    assert not orphan.exists()
    assert (await store.status(fresh.id, "123", "session-one")).offset == 0
    assert (store.directory / done.path).read_bytes() == b"done"
    await store.cancel(fresh.id, "123", "session-one")
    assert not (store.incoming / f"{fresh.id}.part").exists()


async def test_bridge_streaming_over_json_limit_and_owner_isolation(client, monkeypatch):
    reads = []
    original = web.Request.read

    async def watch_read(request):
        if request.method == "PUT":
            reads.append(request.path)
        return await original(request)

    monkeypatch.setattr(web.Request, "read", watch_read)
    payload = b"\x00\xff" * 600_000
    response = await client.post(
        _BASE, json={"size": len(payload), "name": "sample.bin"}, headers=_HEADERS
    )
    assert response.status == 200
    session = await response.json()
    url = f"{_BASE}/{session['id']}"
    assert (await client.get(url)).status == 403
    assert (await client.get(url, headers={"X-Dtw-Bridge-Token": "test-token"})).status == 403
    foreign = {**_HEADERS, "X-Dtw-Owner": "other-session"}
    assert (await client.get(url, headers=foreign)).status == 404
    assert (await client.get(url.replace("/123/", "/456/"), headers=_HEADERS)).status == 404
    response = await client.put(
        url, data=chunks(payload), headers={**_HEADERS, "Upload-Offset": "0"}
    )
    assert response.status == 200
    assert response.headers["Upload-Offset"] == str(len(payload))
    assert not reads
    response = await client.post(
        url + "/complete", json={"sha256": hashlib.sha256(payload).hexdigest()}, headers=_HEADERS
    )
    assert response.status == 200
    assert (await response.json())["mime"] == "application/octet-stream"
    ordinary = await client.post(_BASE, data=b"x" * (512 * 1024 + 1), headers=_HEADERS)
    assert ordinary.status == 413
    for tid, status in [("999", 404), ("789", 409)]:
        assert (
            await client.post(_BASE.replace("123", tid), json={}, headers=_HEADERS)
        ).status == status


async def test_bridge_bad_offsets_and_size_and_cancel(client):
    response = await client.post(_BASE, json={"size": 4, "name": "test.bin"}, headers=_HEADERS)
    upload_id = (await response.json())["id"]
    url = f"{_BASE}/{upload_id}"
    assert (await client.put(url, data=b"abcd", headers=_HEADERS)).status == 400
    assert (
        await client.put(url, data=b"abcd", headers={**_HEADERS, "Upload-Offset": "1"})
    ).status == 409
    response = await client.put(url, data=b"abcde", headers={**_HEADERS, "Upload-Offset": "0"})
    assert response.status == 413
    assert (await (await client.get(url, headers=_HEADERS)).json())["offset"] == 0
    client.app[UPLOADS].chunk_bytes = 2
    assert (
        await client.put(url, data=b"abc", headers={**_HEADERS, "Upload-Offset": "0"})
    ).status == 413
    assert (await client.delete(url, headers=_HEADERS)).status == 200
    assert (await client.get(url, headers=_HEADERS)).status == 404


def test_real_256_mib_upload_memory_is_bounded(tmp_path):
    pytest.importorskip("resource")
    script = r"""
import asyncio, hashlib, json, sys, tracemalloc
from pathlib import Path
from types import SimpleNamespace
from aiohttp.test_utils import TestClient, TestServer
from ductor_bot.config import AgentConfig, WebchatConfig
from ductor_bot.webchat.bridge import create_bridge_app
from ductor_bot.webchat.topics import WebTopics
from ductor_bot.webchat.uploads import CHUNK_BYTES, UPLOADS
from ductor_bot.workspace.paths import DuctorPaths
async def main():
    root = Path(sys.argv[1])
    async def lookup(_self, tid):
        return {"tid":tid, "read_only":False}
    WebTopics.lookup = lookup
    bot = SimpleNamespace(config=AgentConfig(allowed_user_ids=[123], webchat=WebchatConfig(
        enabled=True,domain="chat.example.com",allowed_ips=["203.0.113.0/24"],acme_email="admin@example.com")),
        _orch=SimpleNamespace(paths=DuctorPaths(ductor_home=root)))
    runtime = SimpleNamespace(topic_registry_lock=asyncio.Lock())
    app = create_bridge_app(bot, runtime, "test-token")
    headers = {"X-Dtw-Bridge-Token":"test-token", "X-Dtw-Owner":"session"}
    base = "/webchat/v1/topics/123/uploads"
    async with TestClient(TestServer(app)) as client:
        size = 256 * 1024 * 1024
        response = await client.post(base, json={"name":"large.bin","size":size}, headers=headers)
        assert response.status == 200
        session = await response.json()
        url = base + "/" + session["id"]
        block = b"\x00\xff" * 32768
        digest = hashlib.sha256()
        def rss():
            for line in Path("/proc/self/status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
            raise AssertionError("RSS unavailable")
        tracemalloc.start()
        baseline = rss()
        sampled_peak = baseline
        for offset in range(0, size, CHUNK_BYTES):
            async def chunks():
                for _ in range(CHUNK_BYTES // len(block)):
                    digest.update(block)
                    yield block
            response = await client.put(url, data=chunks(), headers={**headers,"Upload-Offset":str(offset)})
            assert response.status == 200, await response.text()
            assert (await response.json())["offset"] == offset + CHUNK_BYTES
            sampled_peak = max(sampled_peak, rss())
        response = await client.post(url+"/complete", json={"sha256":digest.hexdigest()}, headers=headers)
        assert response.status == 200, await response.text()
        finished = await response.json()
        assert (app[UPLOADS].directory / finished["path"]).stat().st_size == size
        sampled_peak = max(sampled_peak, rss())
        _, python_peak = tracemalloc.get_traced_memory()
        print(json.dumps({"bytes":size,"baseline_kib":baseline,"sampled_peak_kib":sampled_peak,"growth_kib":sampled_peak-baseline,"python_peak_bytes":python_peak}))
asyncio.run(main())
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "large")],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    measured = json.loads(result.stdout)
    print("UPLOAD_MEMORY", json.dumps(measured))
    (tmp_path / "memory.json").write_text(json.dumps(measured))
    assert measured["bytes"] == 256 * 1024 * 1024
    assert measured["growth_kib"] < 32 * 1024
    assert measured["python_peak_bytes"] < 32 * 1024 * 1024
