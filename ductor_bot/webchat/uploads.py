"""Resumable, owner-bound uploads stored without server-side image conversion."""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
import warnings
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

import filetype
from aiohttp import web
from PIL import Image

from ductor_bot.infra.atomic_io import complete_write
from ductor_bot.infra.json_store import atomic_json_save

CHUNK_BYTES = 8 * 1024 * 1024
MAX_FILE_BYTES = 2 * 1024**3
UPLOAD_TTL = 24 * 60 * 60
_BUFFER_BYTES = 64 * 1024
_ID = re.compile(r"[a-f0-9]{32}\Z")
_SHA256 = re.compile(r"[a-fA-F0-9]{64}\Z")
_PREFIX = "/webchat/v1/topics/{tid}/uploads"
_TEXT_EXTENSIONS = frozenset(
    [
        "py",
        "md",
        "json",
        "js",
        "ts",
        "yaml",
        "yml",
        "toml",
        "csv",
        "log",
        "txt",
        "sh",
        "sql",
        "xml",
        "css",
        "c",
        "h",
        "cpp",
        "hpp",
        "go",
        "rs",
        "rb",
        "java",
        "kt",
        "swift",
        "jsx",
        "tsx",
        "mjs",
        "cjs",
        "jsonl",
        "rst",
        "ini",
        "conf",
        "tsv",
    ]
)
_TEXT_MIMES = frozenset({"application/json", "application/javascript", "application/xml"})
logger = logging.getLogger(__name__)
_TEXT_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


class UploadError(Exception):
    """An upload request cannot be accepted."""

    def __init__(self, code: str, status: int) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


@dataclass(slots=True)
class UploadSession:
    """Private persistent session; owners never appear in public responses."""

    id: str
    tid: str
    owner: str
    name: str
    size: int
    candidate_mime: str
    updated: float
    offset: int = 0
    path: str | None = None
    mime: str | None = None
    sha256: str | None = None
    inline_allowed: bool = False

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "size": self.size,
            "offset": self.offset,
            "state": "complete" if self.path is not None else "uploading",
            "path": self.path,
            "mime": self.mime,
            "sha256": self.sha256,
            "inline_allowed": self.inline_allowed,
        }


def _sniff_text(sample: bytes) -> tuple[str, str]:
    try:
        text = (
            codecs.getincrementaldecoder("utf-8")()
            .decode(sample, final=len(sample) < _BUFFER_BYTES)
            .lstrip("\ufeff \t\r\n")
        )
    except UnicodeDecodeError:
        return "application/octet-stream", "bin"
    if _TEXT_CONTROLS.search(text):
        return "application/octet-stream", "bin"
    svg = r"(?:<\?xml.*?\?>\s*)?(?:<!--.*?-->\s*)*<svg(?:\s|>)"
    if re.match(svg, text, re.IGNORECASE | re.DOTALL):
        return "image/svg+xml", "svg"
    if re.match(r"(?:<!doctype\s+html|<html|<head|<body)(?:\s|>)", text, re.IGNORECASE):
        return "text/html", "html"
    return "text/plain", "txt"


def _valid_text(path: Path) -> bool:
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        with path.open("rb") as stream:
            while data := stream.read(_BUFFER_BYTES):
                if _TEXT_CONTROLS.search(decoder.decode(data)):
                    return False
            decoder.decode(b"", final=True)
    except UnicodeDecodeError:
        return False
    return True


def _sniff(path: Path, candidate: str, name: str) -> tuple[str, str, bool]:
    with path.open("rb") as stream:
        sample = stream.read(_BUFFER_BYTES)
    kind = filetype.guess(sample)
    mime, extension = "application/octet-stream", "bin"
    if kind is not None:
        mime, extension = str(kind.mime), str(kind.extension)
    elif sample or candidate.startswith("text/"):
        mime, extension = _sniff_text(sample)
    if mime == "text/plain" and not _valid_text(path):
        mime, extension = "application/octet-stream", "bin"
    candidate = {"image/jpg": "image/jpeg"}.get(candidate, candidate)
    text_candidate = candidate.startswith("text/") or candidate in _TEXT_MIMES
    if candidate not in {"", "application/octet-stream", mime} and not (
        mime == "text/plain" and text_candidate
    ):
        raise UploadError("mime_mismatch", 415)
    if mime == "text/plain":
        suffix = Path(name).suffix.lower().lstrip(".")
        extension = suffix if suffix in _TEXT_EXTENSIONS else "txt"
    inline = mime.startswith("image/") and mime != "image/svg+xml"
    if inline:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", Image.DecompressionBombWarning)
                with Image.open(path) as image:
                    if image.width * image.height > 100_000_000:
                        raise UploadError("image_too_large", 413)
                    image.verify()
        except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:
            raise UploadError("invalid_image", 415) from exc
    if re.fullmatch(r"[a-z0-9]{1,12}", extension) is None:
        raise UploadError("invalid_type", 415)
    return mime, extension, inline


class UploadStore:
    """Serialize each upload and publish offsets only after a complete chunk."""

    def __init__(
        self,
        directory: Path,
        *,
        max_file_bytes: int = MAX_FILE_BYTES,
        chunk_bytes: int = CHUNK_BYTES,
        idle_timeout: float = 30,
    ) -> None:
        self.directory = directory.absolute()
        self.incoming = self.directory / ".incoming"
        self.max_file_bytes = max_file_bytes
        self.chunk_bytes = chunk_bytes
        self.idle_timeout = idle_timeout
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, upload_id: str) -> asyncio.Lock:
        if _ID.fullmatch(upload_id) is None:
            raise UploadError("not_found", 404)
        return self._locks.setdefault(upload_id, asyncio.Lock())

    def _prepare(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        if self.directory.is_symlink():
            raise UploadError("storage_unavailable", 403)
        self.incoming.mkdir(exist_ok=True, mode=0o700)
        self._safe(self.incoming)

    def _safe(self, path: Path) -> Path:
        if self.directory.is_symlink() or self.incoming.is_symlink() or path.is_symlink():
            raise UploadError("storage_unavailable", 403)
        if not path.resolve().is_relative_to(self.directory.resolve()):
            raise UploadError("storage_unavailable", 403)
        return path

    def _metadata(self, upload_id: str) -> Path:
        if _ID.fullmatch(upload_id) is None:
            raise UploadError("not_found", 404)
        return self._safe(self.incoming / f"{upload_id}.json")

    def _save(self, session: UploadSession) -> None:
        path = self._metadata(session.id)
        atomic_json_save(path, asdict(session))
        path.chmod(0o600)

    def _load(self, upload_id: str, tid: str, owner: str) -> UploadSession:
        try:
            session = UploadSession(**json.loads(self._metadata(upload_id).read_text()))
        except (FileNotFoundError, ValueError, TypeError):
            raise UploadError("not_found", 404) from None
        if (
            session.id != upload_id
            or session.tid != tid
            or not hmac.compare_digest(session.owner.encode(), owner.encode())
        ):
            raise UploadError("not_found", 404)
        if session.path is None:
            data = self._safe(self.incoming / f"{upload_id}.part")
            if not data.is_file() or data.stat().st_size < session.offset:
                raise UploadError("upload_corrupt", 409)
            # An interrupted append may precede the atomic offset publication.
            if data.stat().st_size > session.offset:
                with data.open("r+b") as stream:
                    stream.truncate(session.offset)
        return session

    async def start(self, tid: str, owner: str, data: dict[str, Any]) -> UploadSession:
        size, name, mime = data.get("size"), data.get("name"), data.get("mime", "")
        if (
            type(size) is not int
            or size < 0
            or not isinstance(name, str)
            or not 1 <= len(name) <= 255
            or any(ord(c) < 32 for c in name)
            or not isinstance(mime, str)
            or len(mime) > 255
            or any(ord(c) < 32 for c in mime)
        ):
            raise UploadError("bad_request", 400)
        if size > self.max_file_bytes:
            raise UploadError("too_large", 413)
        try:
            name.encode("utf-8")
            mime.encode("utf-8")
        except UnicodeError:
            raise UploadError("bad_request", 400) from None
        session = UploadSession(
            secrets.token_hex(16), tid, owner, name, size, mime.lower(), time.time()
        )
        await complete_write(self._create, session)
        return session

    def _create(self, session: UploadSession) -> None:
        self._prepare()
        if self._metadata(session.id).exists():
            raise UploadError("storage_conflict", 409)
        path = self._safe(self.incoming / f"{session.id}.part")
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        try:
            self._save(session)
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    async def status(self, upload_id: str, tid: str, owner: str) -> UploadSession:
        async with self._lock(upload_id):
            return await complete_write(self._load, upload_id, tid, owner)

    def _stage(self, upload_id: str) -> BinaryIO:
        path = self._safe(self.incoming / f"{upload_id}.chunk")
        fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        return os.fdopen(fd, "wb")

    async def put(  # noqa: PLR0913 -- request identity, offset, stream and optional length
        self,
        upload_id: str,
        tid: str,
        owner: str,
        offset: int,
        chunks: AsyncIterator[bytes],
        *,
        length: int | None = None,
    ) -> UploadSession:
        async with self._lock(upload_id):
            session = await complete_write(self._load, upload_id, tid, owner)
            if session.path is not None or type(offset) is not int or offset != session.offset:
                raise UploadError("offset_conflict", 409)
            if length is not None and (length > self.chunk_bytes or offset + length > session.size):
                raise UploadError("too_large", 413)
            stage = self._stage(upload_id)
            stage_path = self.incoming / f"{upload_id}.chunk"
            total = 0
            try:
                while True:
                    try:
                        async with asyncio.timeout(self.idle_timeout):
                            chunk = await anext(chunks)
                    except StopAsyncIteration:
                        break
                    total += len(chunk)
                    if total > self.chunk_bytes or offset + total > session.size:
                        raise UploadError("too_large", 413)
                    await complete_write(stage.write, chunk)
                if total == 0 or (length is not None and total != length):
                    raise UploadError("incomplete_chunk", 409)
                await complete_write(stage.close)
                await complete_write(self._commit_chunk, session, total)
            finally:
                await complete_write(stage.close)
                await complete_write(stage_path.unlink, missing_ok=True)
            return session

    def _commit_chunk(self, session: UploadSession, size: int) -> None:
        path = self._safe(self.incoming / f"{session.id}.part")
        stage = self._safe(self.incoming / f"{session.id}.chunk")
        offset = session.offset
        try:
            with path.open("r+b") as target, stage.open("rb") as source:
                target.seek(offset)
                while data := source.read(_BUFFER_BYTES):
                    target.write(data)
                target.flush()
                os.fsync(target.fileno())
            session.offset += size
            session.updated = time.time()
            self._save(session)
        except BaseException:
            session.offset = offset
            with path.open("r+b") as target:
                target.truncate(offset)
            raise

    async def complete(self, upload_id: str, tid: str, owner: str, digest: object) -> UploadSession:
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise UploadError("bad_hash", 400)
        async with self._lock(upload_id):
            session = await complete_write(self._load, upload_id, tid, owner)
            if session.path is not None:
                if session.sha256 != digest.lower():
                    raise UploadError("hash_mismatch", 409)
                return session
            if session.offset != session.size:
                raise UploadError("incomplete_upload", 409)
            await complete_write(self._finish, session, digest.lower())
            return session

    def _finish(self, session: UploadSession, digest: str) -> None:
        path = self._safe(self.incoming / f"{session.id}.part")
        calculated = hashlib.sha256()
        with path.open("rb") as stream:
            while data := stream.read(_BUFFER_BYTES):
                calculated.update(data)
        if not hmac.compare_digest(calculated.hexdigest(), digest):
            raise UploadError("hash_mismatch", 409)
        mime, extension, inline = _sniff(path, session.candidate_mime, session.name)
        day = self._safe(self.directory / datetime.now(UTC).strftime("%Y-%m-%d"))
        day.mkdir(exist_ok=True)
        destination = self._safe(day / f"{session.id}.{extension}")
        if destination.exists():
            raise UploadError("storage_conflict", 409)
        path.replace(destination)
        session.path = destination.relative_to(self.directory).as_posix()
        session.mime, session.inline_allowed, session.sha256 = mime, inline, digest
        session.updated = time.time()
        try:
            self._save(session)
        except BaseException:
            destination.replace(path)
            raise

    def _discard(self, upload_id: str) -> None:
        for suffix in ("part", "chunk", "json"):
            self._safe(self.incoming / f"{upload_id}.{suffix}").unlink(missing_ok=True)

    async def cancel(self, upload_id: str, tid: str, owner: str) -> None:
        async with self._lock(upload_id):
            session = await complete_write(self._load, upload_id, tid, owner)
            if session.path is not None:
                raise UploadError("already_complete", 409)
            await complete_write(self._discard, upload_id)

    async def cleanup(self, *, now: float | None = None) -> int:
        """Discard only expired partial uploads; completed files are retained."""
        await complete_write(self._prepare)
        cutoff = (time.time() if now is None else now) - UPLOAD_TTL
        paths = await asyncio.to_thread(lambda: list(self.incoming.glob("*.json")))
        removed = 0
        for path in paths:
            if _ID.fullmatch(path.stem) is None or path.is_symlink():
                continue
            async with self._lock(path.stem):
                if await complete_write(self._expire, path, cutoff):
                    removed += 1
        # A process can stop before the first metadata publication or during staging.
        orphans = await asyncio.to_thread(lambda: list(self.incoming.iterdir()))
        for path in orphans:
            if path.suffix not in {".part", ".chunk"} or _ID.fullmatch(path.stem) is None:
                continue
            async with self._lock(path.stem):
                if not self._metadata(path.stem).exists() and path.lstat().st_mtime < cutoff:
                    await complete_write(self._safe(path).unlink, missing_ok=True)
        return removed

    def _expire(self, path: Path, cutoff: float) -> bool:
        try:
            raw = json.loads(self._safe(path).read_text())
        except FileNotFoundError:
            return False
        except (ValueError, TypeError):
            if path.stat().st_mtime >= cutoff:
                return False
        else:
            if not isinstance(raw, dict) or raw.get("path") is not None:
                return False
            updated = raw.get("updated")
            if not isinstance(updated, (int, float)) or updated >= cutoff:
                return False
        self._discard(path.stem)
        return True


UPLOADS = web.AppKey("webchat_uploads", UploadStore)


def register_upload_routes(  # noqa: C901, PLR0915 -- route lifecycle and request dispatch
    app: web.Application,
    directory: Path,
    lookup: Callable[[str], Awaitable[dict[str, Any] | None]],
) -> None:
    """Register bridge-only streaming routes; bridge middleware authenticates first."""
    store = UploadStore(directory)
    app[UPLOADS] = store

    async def lifecycle(_app: web.Application) -> AsyncIterator[None]:
        await store.cleanup()

        async def sweep() -> None:
            while True:
                await asyncio.sleep(3600)
                try:
                    await store.cleanup()
                except (OSError, UploadError) as exc:
                    logger.warning("Upload cleanup unavailable: %s", type(exc).__name__)

        task = asyncio.create_task(sweep(), name="webchat-upload-cleanup")
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app.cleanup_ctx.append(lifecycle)

    async def upload(  # noqa: C901, PLR0911, PLR0912 -- HTTP status mapping
        request: web.Request,
    ) -> web.Response:
        tid = request.match_info["tid"]
        owner = request.headers.get("X-Dtw-Owner", "")
        if not owner or len(owner) > 128:
            return web.json_response({"error": "forbidden"}, status=403)
        topic = await lookup(tid)
        if topic is None:
            return web.json_response({"error": "unknown_topic"}, status=404)
        if topic.get("read_only"):
            return web.json_response({"error": "read_only"}, status=409)
        upload_id = request.match_info.get("upload_id", "")
        try:
            if request.method == "PUT":
                value = request.headers.get("Upload-Offset", "")
                if re.fullmatch(r"0|[1-9][0-9]*", value) is None or len(value) > 20:
                    return web.json_response({"error": "bad_offset"}, status=400)
                # StreamReader bypasses the JSON path's read()/512KiB buffering.
                session = await store.put(
                    upload_id,
                    tid,
                    owner,
                    int(value),
                    request.content.iter_chunked(_BUFFER_BYTES),
                    length=request.content_length,
                )
            elif request.method == "GET":
                session = await store.status(upload_id, tid, owner)
            elif request.method == "DELETE":
                await store.cancel(upload_id, tid, owner)
                return web.json_response({"ok": True})
            else:
                data = await request.json()
                if not isinstance(data, dict):
                    return web.json_response({"error": "bad_request"}, status=400)
                if upload_id:
                    session = await store.complete(upload_id, tid, owner, data.get("sha256"))
                else:
                    session = await store.start(tid, owner, data)
            return web.json_response(
                session.public(), headers={"Upload-Offset": str(session.offset)}
            )
        except UploadError as exc:
            return web.json_response({"error": exc.code}, status=exc.status)
        except (ValueError, UnicodeError):
            return web.json_response({"error": "bad_request"}, status=400)
        except TimeoutError:
            return web.json_response({"error": "upload_timeout"}, status=408)
        except ConnectionError:
            return web.json_response({"error": "incomplete_chunk"}, status=400)
        except OSError:
            return web.json_response({"error": "storage_unavailable"}, status=507)

    app.router.add_post(_PREFIX, upload)
    app.router.add_get(_PREFIX + "/{upload_id}", upload)
    app.router.add_put(_PREFIX + "/{upload_id}", upload)
    app.router.add_delete(_PREFIX + "/{upload_id}", upload)
    app.router.add_post(_PREFIX + "/{upload_id}/complete", upload)
