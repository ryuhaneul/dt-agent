"""History-scoped file access and immutable raster previews for web chat."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import mimetypes
import os
import re
import stat
import tempfile
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import quote

import filetype
from aiohttp import web
from PIL import Image, ImageOps

from ductor_bot.chat_log.history import Entry
from ductor_bot.webchat.attachments import AttachmentIndex

_SAFE_IMAGES = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
    "GIF": "image/gif",
    "BMP": "image/bmp",
}
_PIXELS = 100_000_000
_PREVIEW_BYTES = 32 * 1024 * 1024
_BUFFER = 64 * 1024
_ID = re.compile(r"[a-f0-9]{32}\Z")


def open_regular(path: Path) -> BinaryIO:
    """Open every component without following links, including intermediate directories."""
    if not path.is_absolute() or ".." in path.parts:
        raise OSError("unsafe_attachment_path")
    directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:-1]:
            child = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
            )
            os.close(directory)
            directory = child
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise OSError("attachment_not_regular")
        return os.fdopen(fd, "rb")
    finally:
        os.close(directory)


def _magic_mime(stream: BinaryIO) -> str | None:
    stream.seek(0)
    kind = filetype.guess(stream.read(8192))
    stream.seek(0)
    mime = str(kind.mime) if kind else None
    return mime if mime in _SAFE_IMAGES.values() else None


def _image_mime(stream: BinaryIO) -> str | None:
    """Bound Pillow's encoded input as WebP opens otherwise read the entire file."""
    if os.fstat(stream.fileno()).st_size > _PREVIEW_BYTES:
        return None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            stream.seek(0)
            with Image.open(stream) as image:
                mime = _SAFE_IMAGES.get(image.format or "")
                if not mime or image.width * image.height > _PIXELS:
                    return None
                image.verify()
                return mime
    except (
        OSError,
        ValueError,
        SyntaxError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        return None
    finally:
        stream.seek(0)


@dataclass(frozen=True, slots=True)
class DownloadAttachment:
    path: Path
    metadata: dict[str, Any]
    preview: Path | None = None


class AttachmentDownloads:
    def __init__(self, workspace: Path, index: AttachmentIndex, cache: Path) -> None:
        self.workspace = workspace.absolute()
        self.index = index
        self.cache = cache.absolute()
        self._images = asyncio.Semaphore(1)

    def _thumbnail(self, stream: BinaryIO, info: os.stat_result, path: Path) -> Path | None:
        key = hashlib.sha256(
            f"{path}:{info.st_dev}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}:{info.st_ctime_ns}:512".encode()
        ).hexdigest()
        target = self.cache / f"{key}.webp"
        temporary: str | None = None
        try:
            if any(parent.is_symlink() for parent in (self.cache, *self.cache.parents)):
                return None
            self.cache.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Validate cache ancestors as well; never serve arbitrary cache symlinks.
            if target.exists():
                with open_regular(target):
                    return target
            with tempfile.NamedTemporaryFile(
                dir=self.cache, suffix=".webp", delete=False
            ) as output:
                temporary = output.name
                with warnings.catch_warnings():
                    warnings.simplefilter("error", Image.DecompressionBombWarning)
                    stream.seek(0)
                    with Image.open(stream) as image:
                        if image.width * image.height > _PIXELS:
                            return None
                        image.thumbnail((512, 512))
                        preview = ImageOps.exif_transpose(image).convert("RGBA")
                        preview.save(output, format="WEBP", quality=80)
            # Refuse symlinked cache directories before publishing a preview.
            with open_regular(Path(temporary)):
                pass
            Path(temporary).replace(target)
        except (
            OSError,
            ValueError,
            SyntaxError,
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
        ):
            return None
        else:
            return target
        finally:
            if temporary is not None:
                with contextlib.suppress(OSError):
                    Path(temporary).unlink()
            stream.seek(0)

    def _describe(self, path: Path, metadata: dict[str, Any]) -> DownloadAttachment | None:
        try:
            with open_regular(path) as stream:
                info = os.fstat(stream.fileno())
                mime = _magic_mime(stream)
                kind = filetype.guess(stream.read(8192))
                stream.seek(0)
                # A stored inline prohibition remains authoritative even if bytes are a raster.
                inline = mime is not None and metadata.get("inline_allowed", True)
                description = {
                    **metadata,
                    "path": path.name
                    if metadata.get("provenance") == "delivered"
                    else metadata["path"],
                    "size": info.st_size,
                    "mime": mime
                    or (
                        str(kind.mime)
                        if kind
                        else metadata.get("mime")
                        or mimetypes.guess_type(path.name)[0]
                        or "application/octet-stream"
                    ),
                    "inline_allowed": bool(inline),
                }
                preview = (
                    self._thumbnail(stream, info, path) if inline and _image_mime(stream) else None
                )
                return DownloadAttachment(path, description, preview)
        except (OSError, ValueError):
            return None

    async def list(self, tid: str, entry: Entry) -> list[DownloadAttachment]:
        indexed = await self.index.get(tid, entry.id)
        candidates = []
        for metadata in indexed:
            path = Path(metadata["path"])
            origin = metadata.get("provenance")
            if origin == "delivered":
                if entry.role in {"bot", "event"} and ".." not in path.parts:
                    candidates.append(
                        (path if path.is_absolute() else self.workspace / path, metadata)
                    )
            elif not path.is_absolute() and ".." not in path.parts and path.parts:
                received = (
                    origin == "received"
                    and entry.role == "user"
                    and entry.source == "telegram"
                    and path.parts[0] == "telegram_files"
                )
                uploaded = (
                    origin is None
                    and entry.role == "user"
                    and entry.source == "web"
                    and path.parts[0] == "webchat_files"
                )
                if received or uploaded:
                    candidates.append((self.workspace / path, metadata))
        result = []
        seen = set()
        for path, metadata in candidates:
            if path in seen:
                continue
            seen.add(path)
            async with self._images:
                item = await asyncio.to_thread(self._describe, path, metadata)
            if item is None:
                continue
            base = f"/api/topics/{quote(tid, safe='')}/messages/{entry.id}/attachments/{metadata['id']}"
            item.metadata.update(
                download_url=base,
                original_url=base + "?inline=1" if item.metadata["inline_allowed"] else None,
                preview_url=base + "/preview" if item.preview else None,
            )
            result.append(item)
        return result

    async def find(self, tid: str, entry: Entry, attachment_id: str) -> DownloadAttachment | None:
        if _ID.fullmatch(attachment_id) is None:
            return None
        return next(
            (item for item in await self.list(tid, entry) if item.metadata["id"] == attachment_id),
            None,
        )


def _range(value: str | None, size: int) -> tuple[int, int]:
    if value is None:
        return 0, size
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value)
    if match is None or not any(match.groups()) or size == 0:
        raise ValueError("invalid_range")
    first, last = match.groups()
    if not first:
        count = int(last)
        if count == 0:
            raise ValueError("invalid_range")
        return max(0, size - count), size
    start = int(first)
    end = min(size, int(last) + 1) if last else size
    if start >= end:
        raise ValueError("invalid_range")
    return start, end


async def stream_attachment(request: web.Request, item: DownloadAttachment) -> web.StreamResponse:
    """Stream a securely opened descriptor; never delegate path reopening to FileResponse."""
    preview = request.path.endswith("/preview")
    path = item.preview if preview else item.path
    if path is None:
        raise web.HTTPNotFound
    try:
        stream = await asyncio.to_thread(open_regular, path)
    except (OSError, ValueError):
        raise web.HTTPNotFound from None
    with stream:
        info = os.fstat(stream.fileno())
        # Recheck bytes on this descriptor so replacement cannot promote active content inline.
        safe_mime = await asyncio.to_thread(_magic_mime, stream)
        inline = preview or request.query.get("inline") == "1"
        inline = inline and bool(item.metadata["inline_allowed"]) and safe_mime is not None
        headers = {
            "Content-Type": safe_mime or "application/octet-stream",
            "Content-Disposition": ("inline" if inline else "attachment")
            + "; filename*=UTF-8''"
            + quote(item.metadata["name"], safe=""),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private",
            "Accept-Ranges": "bytes",
        }
        try:
            start, end = _range(request.headers.get("Range"), info.st_size)
        except ValueError:
            return web.Response(
                status=416, headers={**headers, "Content-Range": f"bytes */{info.st_size}"}
            )
        status = 206 if request.headers.get("Range") is not None else 200
        if status == 206:
            headers["Content-Range"] = f"bytes {start}-{end - 1}/{info.st_size}"
        headers["Content-Length"] = str(end - start)
        response = web.StreamResponse(status=status, headers=headers)
        await response.prepare(request)
        if request.method != "HEAD":
            await asyncio.to_thread(stream.seek, start)
            remaining = end - start
            try:
                while remaining:
                    data = await asyncio.to_thread(stream.read, min(_BUFFER, remaining))
                    if not data:
                        raise OSError("attachment_changed")  # noqa: TRY301 -- close an already prepared stream
                    async with asyncio.timeout(30):
                        await response.write(data)
                    remaining -= len(data)
            except (OSError, TimeoutError):
                if request.transport is not None:
                    request.transport.close()
                return response
        await response.write_eof()
        return response
