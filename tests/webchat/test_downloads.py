"""Attachment downloads use recorded identities, secure descriptors and bounded relay buffers."""

from __future__ import annotations

import hashlib
import io
import os
import resource
import tracemalloc
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

from ductor_bot.chat_log.history import Entry
from ductor_bot.files.prompt import MediaInfo, build_media_prompt
from ductor_bot.webchat.attachments import AttachmentIndex
from ductor_bot.webchat.downloads import AttachmentDownloads, open_regular
from ductor_bot.webchat.web.app import STATE, create_app
from tests.webchat.test_bridge_api import (
    conversation as conversation,  # noqa: PLC0414 -- shared conversation fixture
)
from tests.webchat.test_bridge_api import headers


def image_bytes():
    output = io.BytesIO()
    Image.new("RGB", (1200, 700), "navy").save(output, "PNG")
    return output.getvalue()


def downloads_at(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    index = AttachmentIndex(tmp_path / "webchat/attachments.jsonl")
    return AttachmentDownloads(workspace, index, tmp_path / "webchat/thumbnails")


def write_file(path, payload=b"sample file"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


@pytest.mark.parametrize(
    ("role", "source"), [("user", "web"), ("user", "telegram"), ("event", "system")]
)
async def test_user_and_event_tags_never_grant_access(tmp_path, role, source):
    service = downloads_at(tmp_path)
    path = write_file(tmp_path / "private.txt")
    assert await service.list("123", Entry(role=role, source=source, text=f"<file:{path}>")) == []


async def test_bot_exact_path_allows_external_file_without_opening_its_parent(tmp_path):
    service = downloads_at(tmp_path)
    path = write_file(tmp_path / "delivered.txt")
    entry = Entry(role="bot", source="telegram", text=f"Result <file:{path}>")
    (item,) = await service.list("123", entry)
    assert item.path == path
    assert item.metadata["name"] == "delivered.txt"
    assert item.metadata["preview_url"] is None
    assert (
        await service.find(
            "123", entry, hashlib.sha256(str(tmp_path / "other.txt").encode()).hexdigest()[:32]
        )
        is None
    )


async def test_telegram_exact_prompt_only_and_real_root(tmp_path):
    service = downloads_at(tmp_path)
    path = write_file(service.workspace / "telegram_files/2026-11-18/photo.png", image_bytes())
    text = build_media_prompt(
        MediaInfo("look", "photo.png", "image/png", "photo", path),
        service.workspace,
        transport="Telegram",
    )
    entry = Entry(role="user", source="telegram", text=text)
    (item,) = await service.list("123", entry)
    assert item.metadata["inline_allowed"]
    for invalid in [
        text.replace("Check tools/media_tools/CLAUDE.md", "Changed tools/media_tools/CLAUDE.md"),
        "Quoted: " + text,
        text.replace("via Telegram", "via Web"),
        text.replace("telegram_files/", "../telegram_files/"),
    ]:
        assert await service.list("123", Entry(role="user", source="telegram", text=invalid)) == []
    assert await service.list("123", Entry(role="user", source="web", text=text)) == []


@pytest.mark.parametrize("kind", ["file", "directory", "root", "traversal", "encoded"])
async def test_symlinks_traversal_and_encoded_paths_are_rejected(tmp_path, kind):
    service = downloads_at(tmp_path)
    target = write_file(tmp_path / "secret.txt")
    directory = service.workspace / "telegram_files"
    directory.mkdir()
    if kind == "file":
        path = directory / "link.txt"
        path.symlink_to(target)
    elif kind == "directory":
        (directory / "link").symlink_to(tmp_path, target_is_directory=True)
        path = directory / "link/secret.txt"
    elif kind == "root":
        directory.rmdir()
        directory.symlink_to(tmp_path, target_is_directory=True)
        path = directory / "secret.txt"
    elif kind == "traversal":
        path = directory / "../../secret.txt"
    else:
        path = Path(str(directory) + "/%2e%2e/%2e%2e/secret.txt")
    entry = Entry(role="bot", source="telegram", text=f"<file:{path}>")
    assert await service.list("123", entry) == []


async def test_index_limits_roots_and_respects_inline_prohibition(tmp_path):
    service = downloads_at(tmp_path)
    path = write_file(service.workspace / "webchat_files/image.png", image_bytes())
    metadata = {
        "id": "a" * 32,
        "name": "image.png",
        "size": path.stat().st_size,
        "mime": "image/png",
        "path": "webchat_files/image.png",
        "inline_allowed": False,
    }
    service.index.path.parent.mkdir(parents=True)
    service.index.append("123", "entry", [metadata])
    entry = Entry(role="user", source="web", text="file", id="entry")
    (item,) = await service.list("123", entry)
    assert not item.metadata["inline_allowed"]
    assert item.preview is None
    service.index.append("123", "entry", [{**metadata, "path": "private.txt"}])
    write_file(service.workspace / "private.txt")
    assert await service.list("123", entry) == []
    assert not AttachmentIndex._valid_attachment({**metadata, "path": str(path)})
    assert not AttachmentIndex._valid_attachment({**metadata, "path": "../secret.txt"})


async def test_thumbnail_cache_immutable_source_limits_and_failure(tmp_path, monkeypatch):
    service = downloads_at(tmp_path)
    path = write_file(tmp_path / "image.png", image_bytes())
    original = path.read_bytes()
    entry = Entry(role="bot", source="telegram", text=f"<file:{path}>")
    (item,) = await service.list("123", entry)
    assert item.preview
    with Image.open(item.preview) as preview:
        assert max(preview.size) == 512
        assert preview.format == "WEBP"
    assert item.preview.stat().st_mode & 0o777 == 0o600
    cached = item.preview.stat().st_mtime_ns
    (again,) = await service.list("123", entry)
    assert again.preview.stat().st_mtime_ns == cached
    assert path.read_bytes() == original
    monkeypatch.setattr("ductor_bot.webchat.downloads._PIXELS", 100)
    (limited,) = await service.list("123", entry)
    assert limited.metadata["preview_url"] is None
    monkeypatch.setattr("ductor_bot.webchat.downloads._PIXELS", 100_000_000)
    monkeypatch.setattr(
        Image.Image,
        "save",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("encoder failure")),
    )
    item.preview.unlink()
    (failed,) = await service.list("123", entry)
    assert failed.metadata["preview_url"] is None
    assert path.read_bytes() == original


async def bridge_entry(harness, path, *, role="bot", text=None):
    entry = await harness.runtime.history.append(
        harness.key, Entry(role=role, source="telegram", text=text or f"<file:{path}>")
    )
    base = f"/webchat/v1/topics/-123.7/messages/{entry.id}/attachments"
    response = await harness.client.get(base, headers=headers())
    assert response.status == 200
    return base, (await response.json())["attachments"]


async def test_bridge_image_original_preview_headers_and_head(conversation, tmp_path):
    h = conversation
    payload = image_bytes()
    path = write_file(tmp_path / '사진 "a".png', payload)
    base, items = await bridge_entry(h, path)
    (item,) = items
    url = base + "/" + item["id"]
    assert (await h.client.get(url)).status == 403
    for suffix, disposition in [
        ("", "attachment"),
        ("?inline=1", "inline"),
        ("/preview", "inline"),
    ]:
        response = await h.client.get(url + suffix, headers=headers())
        assert response.status == 200
        assert response.headers["Content-Disposition"].startswith(disposition + ";")
        assert response.headers["Cache-Control"] == "private"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        body = await response.read()
        if suffix == "/preview":
            with Image.open(io.BytesIO(body)) as image:
                assert max(image.size) == 512
        else:
            assert body == payload
    response = await h.client.head(url, headers=headers())
    assert response.status == 200
    assert int(response.headers["Content-Length"]) == len(payload)
    assert await response.read() == b""


@pytest.mark.parametrize(
    ("range_value", "expected", "status"),
    [
        ("bytes=2-5", b"2345", 206),
        ("bytes=7-", b"789", 206),
        ("bytes=-3", b"789", 206),
        ("bytes=99-", b"", 416),
        ("bytes=2-1", b"", 416),
        ("bytes=0-1,4-5", b"", 416),
        ("bytes=-0", b"", 416),
    ],
)
async def test_range(conversation, tmp_path, range_value, expected, status):
    path = write_file(tmp_path / "data.bin", b"0123456789")
    base, items = await bridge_entry(conversation, path)
    response = await conversation.client.get(
        base + "/" + items[0]["id"], headers={**headers(), "Range": range_value}
    )
    assert response.status == status
    assert await response.read() == expected
    assert response.headers["Accept-Ranges"] == "bytes"
    assert "Content-Range" in response.headers


@pytest.mark.parametrize(
    "payload",
    [
        b"<svg><script>alert(1)</script></svg>",
        b"<html><script>alert(1)</script></html>",
        b"not an image",
    ],
)
async def test_mime_spoof_never_inline(conversation, tmp_path, payload):
    path = write_file(tmp_path / "fake.png", payload)
    base, items = await bridge_entry(conversation, path)
    assert not items[0]["inline_allowed"]
    assert items[0]["preview_url"] is None
    url = base + "/" + items[0]["id"]
    response = await conversation.client.get(url + "?inline=1", headers=headers())
    assert response.headers["Content-Disposition"].startswith("attachment;")
    assert await response.read() == payload
    assert (await conversation.client.get(url + "/preview", headers=headers())).status == 404


async def test_download_scope_and_path_requests(conversation, tmp_path):
    h = conversation
    path = write_file(tmp_path / "allowed.txt")
    base, items = await bridge_entry(h, path)
    assert (await h.client.get(base + "/" + "f" * 32, headers=headers())).status == 404
    for bad in ["%2e%2e%2fsecret", "%2Ftmp%2Fsecret", "%252e%252e%252fsecret"]:
        assert (await h.client.get(base + "/" + bad, headers=headers())).status in {400, 404}
    user_base, user_items = await bridge_entry(h, path, role="user")
    assert user_items == []
    assert (await h.client.get(user_base + "/" + items[0]["id"], headers=headers())).status == 404


async def web_client_for(harness, tmp_path):
    app = create_app(
        bridge_url=str(harness.client.make_url("")),
        bridge_token=headers()["X-Dtw-Bridge-Token"],
        public_origin="https://chat.example.com",
        static_dir=tmp_path,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    auth = await (await harness.client.get("/webchat/v1/auth/state", headers=headers())).json()
    # Browser auth validation is real; only the configured credential state is stubbed.
    original = app[STATE].bridge.request

    async def bridge_request(method, path, **kwargs):
        if path == "/auth/state":
            return 200, {**auth, "password_configured": True, "totp_configured": True}
        return await original(method, path, **kwargs)

    app[STATE].bridge.request = bridge_request
    token = app[STATE].sessions.create(credential_generation=auth["credential_generation"])
    return client, {"Cookie": "dtw_session=" + token}


async def test_streaming_web_relay_auth_range_and_large_memory(conversation, tmp_path):
    h = conversation
    path = tmp_path / "large.bin"
    block = bytes(range(256)) * 4096
    with path.open("wb") as stream:
        for _ in range(256):
            stream.write(block)
    base, items = await bridge_entry(h, path)
    url = (base + "/" + items[0]["id"]).replace("/webchat/v1", "/api")
    client, cookies = await web_client_for(h, tmp_path)
    try:
        assert (await client.get(url)).status == 403
        response = await client.get(
            url,
            headers={
                **cookies,
                "Range": "bytes=10-99",
                "X-Dtw-Bridge-Token": "forged",
                "Authorization": "forged",
            },
        )
        assert response.status == 206
        assert await response.read() == block[10:100]
        assert response.headers["Content-Range"] == f"bytes 10-99/{path.stat().st_size}"
        assert response.headers["Cache-Control"] == "private"
        assert "WWW-Authenticate" not in response.headers
        baseline_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        tracemalloc.start()
        total = 0
        checksum = hashlib.sha256()
        response = await client.get(url, headers=cookies)
        async for chunk in response.content.iter_chunked(64 * 1024):
            total += len(chunk)
            checksum.update(chunk)
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert total == 256 * 1024 * 1024
        expected = hashlib.sha256()
        for _ in range(256):
            expected.update(block)
        assert checksum.digest() == expected.digest()
        assert peak < 16 * 1024 * 1024, peak
        rss_growth = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - baseline_rss
        assert rss_growth < 32 * 1024, rss_growth
        print(f"256MiB relay: tracemalloc peak={peak} bytes; RSS growth={rss_growth} KiB")
        response = await client.head(url, headers=cookies)
        assert response.status == 200
        assert int(response.headers["Content-Length"]) == total
        assert await response.read() == b""
        response = await client.get(url.replace(items[0]["id"], "e" * 32), headers=cookies)
        assert response.status == 404
        assert response.headers["Cache-Control"] == "no-store"
    finally:
        await client.close()


def test_regular_descriptor_refuses_fifo(tmp_path):
    path = tmp_path / "fifo"
    os.mkfifo(path)
    with pytest.raises(OSError, match="not_regular"):
        open_regular(path)


@pytest.mark.parametrize("image_format", ["JPEG", "PNG", "WEBP", "GIF", "BMP"])
async def test_safe_raster_formats(tmp_path, image_format):
    service = downloads_at(tmp_path)
    output = io.BytesIO()
    Image.new("RGB", (700, 500), "green").save(output, image_format)
    path = write_file(tmp_path / "image.data", output.getvalue())
    (item,) = await service.list("123", Entry(role="bot", source="web", text=f"<file:{path}>"))
    assert item.metadata["inline_allowed"]
    assert item.preview is not None


async def test_large_encoded_webp_original_skips_pillow(conversation, tmp_path, monkeypatch):
    output = io.BytesIO()
    Image.new("RGB", (40, 40), "red").save(output, "WEBP")
    path = tmp_path / "padded.webp"
    with path.open("wb") as stream:
        stream.write(output.getvalue())
        for _ in range(33):
            stream.write(b"\0" * 1024 * 1024)

    def no_pillow(*_args, **_kwargs):
        pytest.fail("Large encoded images must not enter Pillow")

    monkeypatch.setattr(Image, "open", no_pillow)
    base, items = await bridge_entry(conversation, path)
    assert items[0]["preview_url"] is None
    assert items[0]["original_url"].endswith("?inline=1")
    response = await conversation.client.get(
        base + "/" + items[0]["id"] + "?inline=1",
        headers={**headers(), "Range": "bytes=0-31"},
    )
    assert response.status == 206
    assert response.headers["Content-Type"] == "image/webp"
    assert response.headers["Content-Disposition"].startswith("inline;")
    with path.open("rb") as source:
        assert await response.read() == source.read(32)


async def test_symlinked_thumbnail_cache_is_not_written(tmp_path):
    service = downloads_at(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    service.cache.parent.mkdir()
    service.cache.symlink_to(external, target_is_directory=True)
    path = write_file(tmp_path / "image.png", image_bytes())
    (item,) = await service.list("123", Entry(role="bot", source="web", text=f"<file:{path}>"))
    assert item.metadata["preview_url"] is None
    assert list(external.iterdir()) == []


async def test_telegram_photo_legacy_http_without_index(conversation):
    h = conversation
    path = write_file(h.paths.workspace / "telegram_files/2026-11-18/photo.png", image_bytes())
    text = build_media_prompt(
        MediaInfo(None, "photo.png", "image/png", "photo", path),
        h.paths.workspace,
        transport="Telegram",
    )
    base, items = await bridge_entry(h, path, role="user", text=text)
    assert len(items) == 1
    response = await h.client.get(base + "/" + items[0]["id"] + "/preview", headers=headers())
    assert response.status == 200
    assert response.headers["Content-Type"] == "image/webp"
    assert not (h.paths.webchat_dir / "attachments.jsonl").exists()


async def test_file_replacement_after_listing_never_follows_symlink(tmp_path):
    service = downloads_at(tmp_path)
    path = write_file(tmp_path / "original.png", image_bytes())
    entry = Entry(role="bot", source="web", text=f"<file:{path}>")
    (item,) = await service.list("123", entry)
    path.unlink()
    path.symlink_to(write_file(tmp_path / "secret"))
    with pytest.raises(OSError, match="symbolic links"):
        open_regular(item.path)


@pytest.mark.parametrize(
    ("mime", "original_type"),
    [
        ("audio/mpeg", "document"),
        ("audio/mpeg", "audio"),
        ("video/mp4", "document"),
        ("video/mp4", "video"),
    ],
)
async def test_legacy_media_document_prompt_variants(tmp_path, mime, original_type):
    service = downloads_at(tmp_path)
    path = write_file(service.workspace / "telegram_files/media.bin")
    text = build_media_prompt(
        MediaInfo("caption", "media.bin", mime, original_type, path),
        service.workspace,
        transport="Telegram",
    )
    (item,) = await service.list("123", Entry(role="user", source="telegram", text=text))
    assert item.path == path
