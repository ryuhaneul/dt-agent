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
import zipfile
import zlib
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

from ductor_bot.config import AgentConfig, WebchatConfig
from ductor_bot.webchat.bridge import create_bridge_app
from ductor_bot.webchat.topics import WebTopics
from ductor_bot.webchat.uploads import CHUNK_BYTES, UPLOAD_TTL, UPLOADS, UploadError, UploadStore
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


@pytest.mark.parametrize(
    ("text", "mime", "extension"),
    [
        ("<svg>", "image/svg+xml", "svg"),
        ("<SVG xmlns='example'>", "image/svg+xml", "svg"),
        ("\ufeff \t\r\n<svg >", "image/svg+xml", "svg"),
        ("<?XML version='1.0'?>\n<!--one--><!--two-->\n<svg />", "image/svg+xml", "svg"),
        ("<!--one--> text <!--two--><svg>", "image/svg+xml", "svg"),
        ("<!-- -->junk--><svg>", "image/svg+xml", "svg"),
        ("<?xml?> <!-- missing ?> <svg>", "image/svg+xml", "svg"),
        ("<?xml data ?>junk ?> <svg>", "image/svg+xml", "svg"),
        ("<!--one-->\u2003<svg >", "image/svg+xml", "svg"),
        ("<svg/>", "text/plain", "txt"),
        ("<svgx>", "text/plain", "txt"),
        ("<?xml?><svg/>", "text/plain", "txt"),
        ("<?xml <svg>", "text/plain", "txt"),
        ("<!--missing<svg>", "text/plain", "txt"),
        ("<!--valid--> <html>", "text/plain", "txt"),
        ("<!doctype html><html>", "text/html", "html"),
        ("<HEAD>", "text/html", "html"),
        ("<body >", "text/html", "html"),
        ("plain text", "text/plain", "txt"),
        ("null\x00byte", "application/octet-stream", "bin"),
    ],
)
def test_text_prefix_classification_retains_legacy_results(text, mime, extension):
    from ductor_bot.webchat.uploads import _sniff_text

    assert _sniff_text(text.encode()) == (mime, extension)


def test_repeated_comment_prefix_has_bounded_cpu_and_sample(tmp_path):
    program = r"""
import json,sys,time
from pathlib import Path
from ductor_bot.webchat import uploads
payload=b'<!--'+b'--><!--'*50_000+b'no svg'
path=Path(sys.argv[1]);path.write_bytes(payload)
original=uploads._sniff_text
samples=[]
def checked(sample):
    samples.append(len(sample))
    return original(sample)
uploads._sniff_text=checked
start=time.process_time()
direct=original(payload)
actual=uploads._sniff(path,'text/plain','comments.txt')
elapsed=time.process_time()-start
print(json.dumps({'seconds':elapsed,'direct':direct,'actual':actual,'sample_bytes':samples}))
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(tmp_path / "comments.txt")],
        check=True,
        text=True,
        capture_output=True,
        timeout=10,
    )
    measured = json.loads(result.stdout)
    assert measured["seconds"] <= 1.0, measured
    assert measured["direct"] == ["text/plain", "txt"]
    assert measured["actual"] == ["text/plain", "txt", False]
    assert measured["sample_bytes"] == [64 * 1024]
    print(json.dumps(measured))


async def test_text_sniff_checks_past_the_prefix(tmp_path):
    payload = b"a" * 65536 + b"\x00\xff"
    store = UploadStore(tmp_path / "files")
    session = await begin(store, payload, name="bad.py", mime="text/plain")
    await store.put(session.id, "123", "session-one", 0, chunks(payload))
    result = await store.complete(
        session.id, "123", "session-one", hashlib.sha256(payload).hexdigest()
    )
    assert result.mime == "application/octet-stream"
    assert result.path.endswith(".bin")
    assert result.inline_allowed is False


@pytest.mark.parametrize(
    ("candidate", "content", "mime", "extension"),
    [
        ("application/x-zip-compressed", "zip", "application/zip", ".zip"),
        ("image/png", "zip", None, None),
        ("application/pdf", "png", None, None),
        ("text/html", "text", "text/plain", ".txt"),
    ],
)
async def test_only_image_mime_mismatches_are_rejected(
    tmp_path, candidate, content, mime, extension
):
    data = io.BytesIO()
    if content == "zip":
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr("example.txt", "sample")
    elif content == "png":
        Image.new("RGB", (9, 7), "white").save(data, format="PNG")
    else:
        data.write(b"sample UTF-8 text")
    payload = data.getvalue()
    store = UploadStore(tmp_path / "files")
    session = await begin(store, payload, mime=candidate)
    await store.put(session.id, "123", "session-one", 0, chunks(payload))
    digest = hashlib.sha256(payload).hexdigest()
    if mime is None:
        with pytest.raises(UploadError, match="mime_mismatch") as error:
            await store.complete(session.id, "123", "session-one", digest)
        assert error.value.status == 415
    else:
        result = await store.complete(session.id, "123", "session-one", digest)
        assert result.mime == mime
        assert result.path.endswith(extension)
        assert result.inline_allowed is False
        assert (store.directory / result.path).read_bytes() == payload


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
    await store.claim([complete.id], "123", "session-one", "sent-entry", lambda _files: None)
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


@pytest.mark.parametrize(("extra", "accepted"), [(0, 2), (16 * 1024**2, 2)])
async def test_concurrent_reservations_preserve_exact_floor(tmp_path, extra, accepted):
    free = 5 * 1024**3 + extra
    store = UploadStore(tmp_path / "files", free_bytes=lambda _: free)
    results = await asyncio.gather(
        *(
            store.start("123", "owner", {"name": "large.bin", "size": 2 * 1024**3})
            for _ in range(3)
        ),
        return_exceptions=True,
    )
    sessions = [value for value in results if not isinstance(value, BaseException)]
    assert len(sessions) == accepted
    errors = [value for value in results if isinstance(value, UploadError)]
    assert all(error.code == "insufficient_space" for error in errors)
    assert len(errors) == 3 - accepted
    for session in sessions:
        record = json.loads((store.incoming / f"{session.id}.json").read_text())
        assert record["reserved_bytes"] == 2 * 1024**3


async def test_reservations_restore_decrease_and_release(tmp_path):
    free = 5 * 1024**3 + 16 * 1024**2
    store = UploadStore(tmp_path / "files", free_bytes=lambda _: free)
    first = await begin(store, b"abcdefgh")
    await store.put(first.id, "123", "session-one", 0, chunks(b"abcd"))
    record = json.loads((store.incoming / f"{first.id}.json").read_text())
    assert record["reserved_bytes"] == 4
    second = await begin(store, b"other")
    restarted = UploadStore(store.directory, free_bytes=lambda _: free)
    await restarted.status(first.id, "123", "session-one")
    assert sum(restarted._reservations.values()) == 9
    await restarted.cancel(second.id, "123", "session-one")
    assert second.id not in restarted._reservations
    await restarted.put(first.id, "123", "session-one", 4, chunks(b"efgh"))
    await restarted.complete(
        first.id, "123", "session-one", hashlib.sha256(b"abcdefgh").hexdigest()
    )
    assert not restarted._reservations
    stale = await begin(restarted, b"stale")
    assert await restarted.cleanup(now=time.time() + UPLOAD_TTL + 1) == 2
    assert stale.id not in restarted._reservations


async def test_disk_drops_during_stream_aborts_and_releases(tmp_path):
    free = 2 * 1024**3
    store = UploadStore(tmp_path / "files", free_bytes=lambda _: free)
    session = await begin(store, b"abcdef")

    async def falling_disk():
        nonlocal free
        yield b"abc"
        free = 1024**3 - 1
        yield b"def"

    with pytest.raises(UploadError, match="insufficient_space") as error:
        await store.put(session.id, "123", "session-one", 0, falling_disk())
    assert error.value.status == 507
    assert session.id not in store._reservations
    assert not list(store.incoming.iterdir())


async def test_disk_floor_also_checked_during_staging_append(tmp_path):
    free = 2 * 1024**3
    store = UploadStore(tmp_path / "files", free_bytes=lambda _: free)
    session = await begin(store, b"abc")

    async def exhausted_before_append():
        nonlocal free
        yield b"abc"
        free = 1024**3 - 1

    with pytest.raises(UploadError, match="insufficient_space"):
        await store.put(session.id, "123", "session-one", 0, exhausted_before_append())
    assert not store._reservations
    assert not list(store.incoming.iterdir())


async def test_attachment_settings_clamp_persist_and_recover(tmp_path, caplog):
    from ductor_bot.webchat.uploads import AttachmentSettings

    config = WebchatConfig(attach_max_file_bytes=4096, attach_max_files_per_message=4)
    path = tmp_path / "webchat" / "attach_settings.json"
    settings = AttachmentSettings(path, config)
    assert (await settings.get())["max_file_bytes"] == 4096
    saved = await settings.put({"max_file_bytes": 9999, "max_files_per_message": 99})
    assert saved["max_file_bytes"] == 4096
    assert saved["max_files_per_message"] == 4
    assert path.stat().st_mode & 0o777 == 0o600
    await settings.put({"max_file_bytes": 1024, "max_files_per_message": 2})
    restarted = AttachmentSettings(path, config)
    assert (await restarted.get())["max_file_bytes"] == 1024
    path.write_text("not JSON")
    assert (await restarted.get())["max_files_per_message"] == 4
    assert "using defaults" in caplog.text
    with pytest.raises(UploadError, match="bad_request"):
        await settings.put({"max_file_bytes": True, "max_files_per_message": 1})


@pytest.mark.parametrize("chunk", [0, 1024**2 - 1, 8 * 1024**2 + 1])
def test_server_chunk_config_bounds(chunk):
    with pytest.raises(ValueError, match="upload_chunk_bytes"):
        WebchatConfig(upload_chunk_bytes=chunk)


@pytest.fixture
async def relay(tmp_path):
    from ductor_bot.webchat.uploads import register_upload_routes
    from ductor_bot.webchat.web.app import STATE, create_app

    owners = []

    @web.middleware
    async def bridge_auth(request, handler):
        assert request.headers.get("X-Dtw-Bridge-Token") == "bridge-test-token"
        if "/uploads" in request.path:
            owners.append(request.headers.get("X-Dtw-Owner"))
        return await handler(request)

    async def lookup(tid):
        return {"tid": tid, "read_only": False} if tid == "123" else None

    async def auth_state(_request):
        return web.json_response(
            {
                "password_configured": True,
                "totp_configured": True,
                "credential_generation": "generation",
            }
        )

    async def events(request):
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        async for _ in socket:
            pass
        return socket

    backend = web.Application(middlewares=[bridge_auth], client_max_size=512 * 1024)
    backend.router.add_get("/webchat/v1/auth/state", auth_state)
    backend.router.add_get("/webchat/v1/events", events)
    config = WebchatConfig(attach_max_file_bytes=16 * 1024**2)
    register_upload_routes(
        backend,
        tmp_path / "files",
        lookup,
        config=config,
        settings_path=tmp_path / "attach_settings.json",
    )
    async with TestClient(TestServer(backend)) as bridge_client:
        app = create_app(
            bridge_url=str(bridge_client.make_url("")),
            bridge_token="bridge-test-token",
            public_origin="https://chat.example.com",
            static_dir=tmp_path,
        )
        async with TestClient(TestServer(app)) as public_client:
            state = app[STATE]
            token = state.sessions.create("generation")
            public_client.session.cookie_jar.update_cookies({"dtw_session": token})
            headers = {"X-Dtw-Client": "web", "X-Dtw-Csrf": state.sessions.get(token).csrf}
            yield public_client, bridge_client, state, headers, token, owners


async def test_web_relay_streams_and_replaces_owner(relay, monkeypatch):
    public, bridge, state, headers, token, owners = relay
    reads = []
    original = web.Request.read

    async def track(request):
        if request.method == "PUT" and "/uploads/" in request.path:
            reads.append(request.path)
        return await original(request)

    monkeypatch.setattr(web.Request, "read", track)
    response = await public.post(
        "/api/topics/123/uploads",
        json={"name": "file.bin", "size": CHUNK_BYTES},
        headers={**headers, "X-Dtw-Owner": "attacker"},
    )
    assert response.status == 200
    upload_id = (await response.json())["id"]
    url = f"/api/topics/123/uploads/{upload_id}"

    async def payload():
        for _ in range(CHUNK_BYTES // 65536):
            yield b"\x00" * 65536

    response = await public.put(
        url,
        data=payload(),
        headers={
            **headers,
            "Content-Type": "application/octet-stream",
            "Upload-Offset": "0",
            "X-Dtw-Owner": "attacker",
        },
    )
    assert response.status == 200
    assert response.headers["Upload-Offset"] == str(CHUNK_BYTES)
    assert not reads
    assert owners
    assert all(owner != "attacker" and len(owner) == 64 for owner in owners)
    assert len(set(owners)) == 1
    data = (bridge.app[UPLOADS].incoming / f"{upload_id}.part").read_bytes()
    assert len(data) == CHUNK_BYTES
    response = await public.post(
        url + "/complete", json={"sha256": hashlib.sha256(data).hexdigest()}, headers=headers
    )
    assert response.status == 200
    second = state.sessions.create("generation")
    public.session.cookie_jar.update_cookies({"dtw_session": second})
    assert (await public.get(url)).status == 404
    public.session.cookie_jar.update_cookies({"dtw_session": token})
    assert (await public.get(url)).status == 200


async def test_upload_relay_checks_auth_before_read_and_keeps_json_limit(relay, monkeypatch):
    public, _bridge, state, headers, token, _owners = relay
    reads = []
    original = web.Request.read

    async def track(request):
        if "/uploads/" in request.path and request.method == "PUT":
            reads.append(request.path)
        return await original(request)

    monkeypatch.setattr(web.Request, "read", track)
    url = "/api/topics/123/uploads/" + "a" * 32
    for bad_headers in [{}, {"X-Dtw-Client": "web"}, {**headers, "X-Dtw-Csrf": "wrong"}]:
        response = await public.put(url, data=b"a" * 600000, headers=bad_headers)
        assert response.status == 403
        assert "WWW-Authenticate" not in response.headers
    assert not reads
    assert (
        await public.put(url, data=b"abc", headers={**headers, "Content-Type": "text/plain"})
    ).status == 415
    response = await public.post(
        "/api/topics/123/uploads", data=b"x" * (512 * 1024 + 1), headers=headers
    )
    assert response.status == 413
    state.sessions.delete(token)
    assert (await public.get("/api/attach-settings")).status == 403


async def test_settings_api_changes_next_upload_limit(relay):
    public, _bridge, _state, headers, _token, _owners = relay
    response = await public.get("/api/attach-settings")
    settings = await response.json()
    assert settings["max_file_bytes"] == 16 * 1024**2
    assert settings["chunk_bytes"] == CHUNK_BYTES
    response = await public.put(
        "/api/attach-settings",
        json={"max_file_bytes": 999 * 1024**3, "max_files_per_message": 100},
        headers=headers,
    )
    settings = await response.json()
    assert settings["max_file_bytes"] == 16 * 1024**2
    assert settings["max_files_per_message"] == 10
    assert (
        await public.put(
            "/api/attach-settings", json={"max_file_bytes": 8, "max_files_per_message": 2}
        )
    ).status == 403
    assert (
        await public.put(
            "/api/attach-settings",
            json={"max_file_bytes": 8, "max_files_per_message": 2},
            headers=headers,
        )
    ).status == 200
    response = await public.post(
        "/api/topics/123/uploads", json={"name": "too-large", "size": 9}, headers=headers
    )
    assert response.status == 413
    response = await public.post(
        "/api/topics/123/uploads", json={"name": "allowed", "size": 8}, headers=headers
    )
    assert response.status == 200


async def test_relay_preserves_bridge_chunk_limit_error(relay):
    public, bridge, _state, headers, _token, _owners = relay
    bridge.app[UPLOADS].chunk_bytes = 1024**2
    response = await public.post(
        "/api/topics/123/uploads",
        json={"name": "too-big-chunk", "size": 2 * 1024**2},
        headers=headers,
    )
    upload_id = (await response.json())["id"]
    response = await public.put(
        f"/api/topics/123/uploads/{upload_id}",
        data=b"\x00" * (2 * 1024**2),
        headers={**headers, "Upload-Offset": "0", "Content-Type": "application/octet-stream"},
    )
    assert response.status == 413
    assert (await response.json())["error"] == "too_large"


async def test_gzip_bomb_is_rejected_without_inflating(relay, client):
    import tracemalloc

    compressor = zlib.compressobj(wbits=31)
    payload = b"".join(compressor.compress(b"x" * 65536) for _ in range(2048)) + compressor.flush()
    assert len(payload) < 256 * 1024
    public, _bridge, _state, headers, _token, _owners = relay
    tracemalloc.start()
    try:
        for server, path, auth in [
            (public, "/api/topics/123/uploads", headers),
            (client, _BASE, _HEADERS),
        ]:
            response = await server.post(
                path,
                data=payload,
                headers={**auth, "Content-Type": "application/json", "Content-Encoding": "gzip"},
            )
            assert response.status == 415
            assert (await response.json())["error"] == "unsupported_media_type"
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    print(
        "GZIP_MEMORY",
        json.dumps(
            {"wire_bytes": len(payload), "decoded_bytes": 128 * 1024**2, "python_peak_bytes": peak}
        ),
    )
    assert peak < 16 * 1024**2


@pytest.mark.parametrize("format_name", ["JPEG", "PNG", "WEBP", "GIF", "BMP"])
async def test_raster_header_validation_and_truncation(tmp_path, format_name):
    stream = io.BytesIO()
    Image.new("RGB", (9, 7), "white").save(stream, format=format_name)
    payload = stream.getvalue()
    mime = Image.MIME[format_name]
    store = UploadStore(tmp_path / "files")
    session = await begin(store, payload, mime=mime)
    await store.put(session.id, "123", "session-one", 0, chunks(payload))
    result = await store.complete(
        session.id, "123", "session-one", hashlib.sha256(payload).hexdigest()
    )
    assert result.inline_allowed is True
    assert (store.directory / result.path).read_bytes() == payload
    broken = payload[:20]
    session = await begin(store, broken, mime=mime)
    await store.put(session.id, "123", "session-one", 0, chunks(broken))
    with pytest.raises(UploadError, match="invalid_image") as error:
        await store.complete(session.id, "123", "session-one", hashlib.sha256(broken).hexdigest())
    assert error.value.status == 415


def oversized_image_header(format_name, payload):
    padding = b"x" * (1024**2 + 1)
    if format_name == "PNG":
        chunk = b"tEXt" + padding
        return (
            payload[:33]
            + struct.pack(">I", len(padding))
            + chunk
            + struct.pack(">I", zlib.crc32(chunk))
            + payload[33:]
        )
    if format_name == "WEBP":
        chunk = b"JUNK" + struct.pack("<I", len(padding)) + padding + b"\0"
        extended = (
            b"VP8X"
            + struct.pack("<I", 10)
            + b"\0" * 4
            + (8).to_bytes(3, "little")
            + (6).to_bytes(3, "little")
        )
        return (
            b"RIFF"
            + struct.pack("<I", len(payload) + len(extended) + len(chunk) - 8)
            + payload[8:12]
            + extended
            + chunk
            + payload[12:]
        )
    if format_name == "JPEG":
        marker = b"\xff\xe1" + struct.pack(">H", 65535) + b"x" * 65533
        return payload[:2] + marker * 17 + payload[2:]
    if format_name == "GIF":
        # No global palette; comment subblocks exceed the header budget before the frame.
        return (
            b"GIF89a"
            + struct.pack("<HHBBB", 9, 7, 0, 0, 0)
            + b"\x21\xfe"
            + (b"\xff" + b"x" * 255) * 4097
            + b"\0"
            + payload[13:]
        )
    # An oversized DIB header cannot be read from the bounded BMP header buffer.
    return payload[:14] + struct.pack("<I", 1024**2 + 1) + padding + payload[18:]


@pytest.mark.parametrize("format_name", ["JPEG", "PNG", "WEBP", "GIF", "BMP"])
async def test_raster_header_budget_is_enforced(tmp_path, format_name):
    stream = io.BytesIO()
    Image.new("RGB", (9, 7), "white").save(stream, format=format_name)
    payload = oversized_image_header(format_name, stream.getvalue())
    store = UploadStore(tmp_path / "files")
    session = await begin(store, payload, mime=Image.MIME[format_name])
    await store.put(session.id, "123", "session-one", 0, chunks(payload))
    with pytest.raises(UploadError, match="invalid_image") as error:
        await store.complete(session.id, "123", "session-one", hashlib.sha256(payload).hexdigest())
    assert error.value.status == 415


@pytest.mark.parametrize("format_name", ["TIFF", "ICO"])
async def test_other_image_formats_remain_download_only(tmp_path, format_name):
    stream = io.BytesIO()
    Image.new("RGB", (32, 32), "white").save(stream, format=format_name)
    payload = stream.getvalue()
    store = UploadStore(tmp_path / "files")
    session = await begin(store, payload, mime=Image.MIME[format_name])
    await store.put(session.id, "123", "session-one", 0, chunks(payload))
    result = await store.complete(
        session.id, "123", "session-one", hashlib.sha256(payload).hexdigest()
    )
    assert result.inline_allowed is False
    assert (store.directory / result.path).read_bytes() == payload


async def test_animated_and_lossless_webp_headers(tmp_path):
    for options in (
        {"lossless": True},
        {
            "save_all": True,
            "append_images": [Image.new("RGB", (8, 8), "blue")],
            "duration": 100,
            "loop": 0,
        },
    ):
        stream = io.BytesIO()
        Image.new("RGB", (8, 8), "red").save(stream, format="WEBP", **options)
        payload = stream.getvalue()
        store = UploadStore(tmp_path / "files")
        session = await begin(store, payload, mime="image/webp")
        await store.put(session.id, "123", "session-one", 0, chunks(payload))
        result = await store.complete(
            session.id, "123", "session-one", hashlib.sha256(payload).hexdigest()
        )
        assert result.inline_allowed is True
        assert (store.directory / result.path).read_bytes() == payload


async def test_reserved_bytes_do_not_double_count_committed_storage(tmp_path):
    free = 5 * 1024**3
    store = UploadStore(tmp_path / "files", free_bytes=lambda _: free)
    first = await store.start("123", "owner", {"name": "first.bin", "size": 2 * 1024**3})
    block = b"\0" * 65536
    await store.put(first.id, "123", "owner", 0, chunks(block))
    free -= len(block)
    restarted = UploadStore(store.directory, free_bytes=lambda _: free)
    second = await restarted.start("123", "owner", {"name": "second.bin", "size": 2 * 1024**3})
    assert restarted._reservations[first.id] == first.size - len(block)
    assert restarted._reservations[second.id] == second.size
    with pytest.raises(UploadError, match="insufficient_space"):
        await restarted.start("123", "owner", {"name": "third.bin", "size": 2 * 1024**3})


async def test_api_exact_five_gib_accepts_two_uploads(client):
    client.app[UPLOADS]._free_bytes = lambda _: 5 * 1024**3
    responses = await asyncio.gather(
        *(
            client.post(_BASE, json={"name": "large.bin", "size": 2 * 1024**3}, headers=_HEADERS)
            for _ in range(3)
        )
    )
    assert sorted(response.status for response in responses) == [200, 200, 507]
    rejected = next(response for response in responses if response.status == 507)
    assert (await rejected.json())["error"] == "insufficient_space"


@pytest.mark.parametrize("format_name", ["WEBP", "PNG"])
@pytest.mark.parametrize("size", [64 * 1024**2, 2 * 1024**3 - 16])
def test_large_image_completion_memory_is_bounded(tmp_path, format_name, size):
    pytest.importorskip("resource")
    script = r"""
import asyncio, gc, hashlib, io, json, os, struct, sys, tempfile, threading, tracemalloc, zlib
from pathlib import Path
from PIL import Image
from ductor_bot.webchat.uploads import UploadStore
async def main():
    size, format_name = int(sys.argv[2]), sys.argv[3]
    with tempfile.TemporaryDirectory(dir=sys.argv[1]) as directory:
        store = UploadStore(Path(directory))
        stream = io.BytesIO()
        Image.new("RGB", (4, 4), "white").save(stream, format=format_name)
        payload = stream.getvalue()
        mime = "image/webp" if format_name == "WEBP" else "image/png"
        session = await store.start("123", "owner", {"name":"large-image", "size":size, "mime":mime})
        padding = size - len(payload) - (8 if format_name == "WEBP" else 12)
        if format_name == "WEBP":
            prefix = payload[:4] + struct.pack("<I", size - 8) + payload[8:] + b"JUNK" + struct.pack("<I", padding)
        else:
            prefix = payload[:-12] + struct.pack(">I", padding) + b"paDd"
        digest, crc = hashlib.sha256(), zlib.crc32(b"paDd")
        path = store.incoming / (session.id + ".part")
        with path.open("wb") as output:
            output.write(prefix); digest.update(prefix)
            block = b"\0" * 65536
            while padding:
                data = block[:min(padding, len(block))]
                output.write(data); digest.update(data)
                if format_name == "PNG": crc = zlib.crc32(data, crc)
                padding -= len(data)
            if format_name == "PNG":
                suffix = struct.pack(">I", crc) + payload[-12:]
                output.write(suffix); digest.update(suffix)
            output.flush(); os.fsync(output.fileno())
        assert path.stat().st_size == size
        session.offset = size
        store._save(session)
        gc.collect()
        def rss():
            return int(next(line.split()[1] for line in Path("/proc/self/status").read_text().splitlines() if line.startswith("VmRSS:")))
        baseline = rss()
        sampled = [baseline]
        stop = threading.Event()
        def sample():
            while not stop.wait(0.002): sampled[0] = max(sampled[0], rss())
        watcher = threading.Thread(target=sample)
        watcher.start()
        tracemalloc.start()
        try:
            result = await store.complete(session.id, "123", "owner", digest.hexdigest())
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop(); stop.set(); watcher.join()
        rss_peak = max(sampled[0], rss())
        stored = store.directory / result.path
        actual = hashlib.sha256()
        with stored.open("rb") as source:
            while data := source.read(65536): actual.update(data)
        assert stored.stat().st_size == size
        assert actual.hexdigest() == digest.hexdigest() == result.sha256
        assert result.inline_allowed and result.mime == mime
        print(json.dumps({"format":format_name,"bytes":size,"python_peak_bytes":peak,"baseline_rss_kib":baseline,"peak_rss_kib":rss_peak,"rss_growth_kib":rss_peak-baseline,"sha256":result.sha256}))
asyncio.run(main())
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), str(size), format_name],
        check=True,
        capture_output=True,
        text=True,
        timeout=300,
    )
    measured = json.loads(result.stdout)
    print("IMAGE_COMPLETION_MEMORY", json.dumps(measured))
    (tmp_path / "memory.json").write_text(json.dumps(measured))
    assert measured["bytes"] == size
    assert measured["python_peak_bytes"] < 32 * 1024**2
    assert measured["rss_growth_kib"] < 32 * 1024


async def test_webp_canvas_pixel_limit_precedes_decoding(tmp_path):
    stream = io.BytesIO()
    Image.new("RGB", (4, 4), "white").save(stream, format="WEBP")
    payload = stream.getvalue()
    extended = (
        b"VP8X"
        + struct.pack("<I", 10)
        + b"\0" * 4
        + (10000).to_bytes(3, "little")
        + (9999).to_bytes(3, "little")
    )
    payload = (
        payload[:4]
        + struct.pack("<I", len(payload) + len(extended) - 8)
        + payload[8:12]
        + extended
        + payload[12:]
    )
    store = UploadStore(tmp_path / "files")
    session = await begin(store, payload, mime="image/webp")
    await store.put(session.id, "123", "session-one", 0, chunks(payload))
    with pytest.raises(UploadError, match="image_too_large") as error:
        await store.complete(session.id, "123", "session-one", hashlib.sha256(payload).hexdigest())
    assert error.value.status == 413


@pytest.mark.parametrize("format_name", ["PNG", "WEBP"])
async def test_streamed_image_structure_rejects_corruption(tmp_path, format_name):
    stream = io.BytesIO()
    Image.new("RGB", (4, 4), "white").save(stream, format=format_name)
    payload = bytearray(stream.getvalue())
    if format_name == "PNG":
        payload[-13] ^= 1  # Damage an IDAT CRC, keeping the MIME and dimensions intact.
    else:
        payload[16:20] = struct.pack("<I", len(payload) + 1)  # Chunk exceeds its RIFF boundary.
    store = UploadStore(tmp_path / "files")
    session = await begin(store, payload, mime=Image.MIME[format_name])
    await store.put(session.id, "123", "session-one", 0, chunks(payload))
    with pytest.raises(UploadError, match="invalid_image"):
        await store.complete(session.id, "123", "session-one", hashlib.sha256(payload).hexdigest())


async def test_completed_unused_upload_can_be_deleted(client):
    payload = b"completed draft"
    response = await client.post(
        _BASE, json={"size": len(payload), "name": "draft.txt"}, headers=_HEADERS
    )
    upload_id = (await response.json())["id"]
    url = f"{_BASE}/{upload_id}"
    assert (
        await client.put(url, data=payload, headers={**_HEADERS, "Upload-Offset": "0"})
    ).status == 200
    assert (
        await client.post(
            url + "/complete",
            json={"sha256": hashlib.sha256(payload).hexdigest()},
            headers=_HEADERS,
        )
    ).status == 200
    store = client.app[UPLOADS]
    session = await store.status(upload_id, "123", "session-one")
    path = store.completed_path(session)
    metadata = store.incoming / f"{upload_id}.json"
    foreign = {**_HEADERS, "X-Dtw-Owner": "other-browser"}
    assert (await client.delete(url, headers=foreign)).status == 404
    assert path.exists()
    assert metadata.exists()
    assert (await client.delete(url, headers=_HEADERS)).status == 200
    assert not path.exists()
    assert not metadata.exists()
    assert (await client.get(url, headers=_HEADERS)).status == 404


async def test_used_upload_cannot_be_deleted(tmp_path):
    store = UploadStore(tmp_path / "files")
    payload = b"accepted attachment"
    session = await begin(store, payload)
    await store.put(session.id, "123", "session-one", 0, chunks(payload))
    session = await store.complete(
        session.id, "123", "session-one", hashlib.sha256(payload).hexdigest()
    )
    path = store.completed_path(session)
    await store.claim([session.id], "123", "session-one", "accepted-entry", lambda _files: None)
    with pytest.raises(UploadError, match="already_complete"):
        await store.cancel(session.id, "123", "session-one")
    assert path.read_bytes() == payload
    assert (await store.status(session.id, "123", "session-one")).used_by == "accepted-entry"


async def test_missing_upload_queries_do_not_retain_locks(tmp_path):
    store = UploadStore(tmp_path / "files")
    for number in range(2000):
        with pytest.raises(UploadError, match="not_found"):
            await store.status(f"{number:032x}", "123", "session-one")
    assert not store._locks
    assert not store._lock_users


async def test_upload_lock_retains_waiters_and_releases_cancelled_waiter(tmp_path):
    store = UploadStore(tmp_path / "files")
    upload_id = "a" * 32
    entered = asyncio.Event()

    async def wait():
        async with store._lock(upload_id):
            entered.set()

    async with store._lock(upload_id):
        lock = store._locks[upload_id]
        cancelled = asyncio.create_task(wait())
        waiter = asyncio.create_task(wait())
        await asyncio.sleep(0)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        assert store._locks[upload_id] is lock
        assert store._lock_users[upload_id] == 2
        assert not entered.is_set()
    # The waiting request still owns a reference when the previous holder exits.
    assert store._locks[upload_id] is lock
    await waiter
    assert entered.is_set()
    assert not store._locks
    assert not store._lock_users


async def test_upload_chunk_uses_inactivity_timeout_only():
    from unittest.mock import Mock

    from ductor_bot.webchat.web.upstream import BridgeClient

    class Response:
        status = 200
        headers = {"Upload-Offset": "4"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def json(self):
            return {"offset": 4}

    client = Mock()
    client.put.return_value = Response()
    bridge = BridgeClient("http://bridge.invalid", "fixture-token", client)
    assert await bridge.upload_chunk(
        "/topics/123/uploads/fixture", chunks(b"data"), owner="browser", offset="0", length=4
    ) == (200, {"offset": 4}, "4")
    timeout = client.put.call_args.kwargs["timeout"]
    assert timeout.total is None
    assert timeout.sock_read == 30
    assert timeout.sock_connect == 5


@pytest.mark.parametrize("elapsed", [UPLOAD_TTL - 1, UPLOAD_TTL, UPLOAD_TTL + 1])
async def test_unused_completed_upload_expires_from_completion_at_24_hours(tmp_path, elapsed):
    store = UploadStore(tmp_path / "files")
    payload = b"unused completed attachment"
    session = await begin(store, payload)
    await store.put(session.id, "123", "session-one", 0, chunks(payload))
    session = await store.complete(
        session.id, "123", "session-one", hashlib.sha256(payload).hexdigest()
    )
    path = store.completed_path(session)
    # File mtime and metadata mtime do not define the unused completion deadline.
    os.utime(path, (1, 1))
    os.utime(store.incoming / f"{session.id}.json", (1, 1))
    expired = elapsed >= UPLOAD_TTL
    assert await store.cleanup(now=session.updated + elapsed) == int(expired)
    assert path.exists() is not expired
    assert (store.incoming / f"{session.id}.json").exists() is not expired


async def test_retained_used_upload_metadata_removed_after_original_disappears(tmp_path):
    store = UploadStore(tmp_path / "files")
    payload = b"sent attachment"
    session = await begin(store, payload)
    await store.put(session.id, "123", "session-one", 0, chunks(payload))
    session = await store.complete(
        session.id, "123", "session-one", hashlib.sha256(payload).hexdigest()
    )
    await store.claim([session.id], "123", "session-one", "sent-entry", lambda _files: None)
    assert await store.cleanup(now=session.updated + 2 * UPLOAD_TTL) == 0
    store.completed_path(session).unlink()
    assert await store.cleanup(now=session.updated + 2 * UPLOAD_TTL) == 1
    assert not (store.incoming / f"{session.id}.json").exists()
