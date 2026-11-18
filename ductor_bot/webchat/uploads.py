"""Resumable, owner-bound uploads stored without server-side image conversion."""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import errno
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
import warnings
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO

import filetype
from aiohttp import web
from PIL import Image

from ductor_bot.infra.atomic_io import complete_write
from ductor_bot.infra.json_store import atomic_json_save

if TYPE_CHECKING:
    from ductor_bot.config import WebchatConfig

CHUNK_BYTES = 8 * 1024 * 1024
MAX_FILE_BYTES = 2 * 1024**3
UPLOAD_TTL = 24 * 60 * 60
SPACE_FLOOR = 1024**3
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
    reserved_bytes: int = 0
    used_by: str | None = None

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
        free_bytes: Callable[[Path], int] | None = None,
    ) -> None:
        self.directory = directory.absolute()
        self.incoming = self.directory / ".incoming"
        self.max_file_bytes = max_file_bytes
        self.chunk_bytes = chunk_bytes
        self.idle_timeout = idle_timeout
        self._locks: dict[str, asyncio.Lock] = {}
        self._space_lock = threading.RLock()
        self._reservations: dict[str, int] = {}
        self._ledger_ready = False
        self._free_bytes = free_bytes or (lambda path: shutil.disk_usage(path).free)

    def _lock(self, upload_id: str) -> asyncio.Lock:
        if _ID.fullmatch(upload_id) is None:
            raise UploadError("not_found", 404)
        return self._locks.setdefault(upload_id, asyncio.Lock())

    def _prepare(self) -> None:
        with self._space_lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            if self.directory.is_symlink():
                raise UploadError("storage_unavailable", 403)
            self.incoming.mkdir(exist_ok=True, mode=0o700)
            self._safe(self.incoming)
            if not self._ledger_ready:
                self._restore_reservations()

    def _restore_reservations(self) -> None:
        for path in self.incoming.glob("*.json"):
            if _ID.fullmatch(path.stem) is None:
                continue
            try:
                session = UploadSession(**json.loads(self._safe(path).read_text()))
                self._validate_record(session)
            except (ValueError, TypeError, UploadError):
                logger.warning("Ignoring invalid upload reservation record")
                continue
            if session.path is None:
                session.reserved_bytes = session.size - session.offset + self.chunk_bytes
                self._reservations[session.id] = session.reserved_bytes
                self._save(session)
        self._ledger_ready = True

    @staticmethod
    def _validate_record(session: UploadSession) -> None:
        if (
            _ID.fullmatch(session.id) is None
            or not isinstance(session.owner, str)
            or type(session.size) is not int
            or type(session.offset) is not int
            or not 0 <= session.offset <= session.size
            or (session.used_by is not None and not isinstance(session.used_by, str))
        ):
            raise UploadError("upload_corrupt", 409)

    def _write_bytes(self, stream: BinaryIO, data: bytes) -> None:
        with self._space_lock:
            if self._free_bytes(self.directory) - len(data) < SPACE_FLOOR:
                raise UploadError("insufficient_space", 507)
            try:
                stream.write(data)
                stream.flush()
                if self._free_bytes(self.directory) < SPACE_FLOOR:
                    raise UploadError("insufficient_space", 507)
            except OSError as exc:
                if exc.errno == errno.ENOSPC:
                    raise UploadError("insufficient_space", 507) from exc
                raise

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
        self._prepare()
        try:
            session = UploadSession(**json.loads(self._metadata(upload_id).read_text()))
            self._validate_record(session)
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

    async def start(
        self, tid: str, owner: str, data: dict[str, Any], *, max_file_bytes: int | None = None
    ) -> UploadSession:
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
        limit = (
            self.max_file_bytes
            if max_file_bytes is None
            else min(self.max_file_bytes, max_file_bytes)
        )
        if size > limit:
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
        with self._space_lock:
            self._prepare()
            reservation = session.size + self.chunk_bytes
            available = self._free_bytes(self.directory) - sum(self._reservations.values())
            if available - SPACE_FLOOR < reservation:
                raise UploadError("insufficient_space", 507)
            session.reserved_bytes = reservation
            self._create_files(session)
            self._reservations[session.id] = reservation

    def _create_files(self, session: UploadSession) -> None:
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

    async def put(  # noqa: PLR0913 -- request identity and rollback on partial input
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
                        raise UploadError("too_large", 413)  # noqa: TRY301 -- discard only disk failures
                    await complete_write(self._write_bytes, stage, chunk)
                if total == 0 or (length is not None and total != length):
                    raise UploadError("incomplete_chunk", 409)  # noqa: TRY301 -- preserve prior offset
                await complete_write(stage.close)
                await complete_write(self._commit_chunk, session, total)
            except UploadError as exc:
                if exc.code == "insufficient_space":
                    await complete_write(stage.close)
                    await complete_write(self._discard, upload_id)
                raise
            finally:
                await complete_write(stage.close)
                await complete_write(stage_path.unlink, missing_ok=True)
            return session

    def _commit_chunk(self, session: UploadSession, size: int) -> None:
        with self._space_lock:
            self._append_chunk(session, size)

    def _append_chunk(self, session: UploadSession, size: int) -> None:
        path = self._safe(self.incoming / f"{session.id}.part")
        stage = self._safe(self.incoming / f"{session.id}.chunk")
        offset = session.offset
        try:
            with path.open("r+b") as target, stage.open("rb") as source:
                target.seek(offset)
                while data := source.read(_BUFFER_BYTES):
                    self._write_bytes(target, data)
                target.flush()
                os.fsync(target.fileno())
            session.offset += size
            session.reserved_bytes = session.size - session.offset + self.chunk_bytes
            session.updated = time.time()
            self._save(session)
            self._reservations[session.id] = session.reserved_bytes
        except BaseException:
            session.offset = offset
            session.reserved_bytes = session.size - offset + self.chunk_bytes
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
            with self._space_lock:
                session.reserved_bytes = 0
                self._save(session)
                self._reservations.pop(session.id, None)
        except BaseException:
            destination.replace(path)
            raise

    def _discard(self, upload_id: str) -> None:
        with self._space_lock:
            for suffix in ("part", "chunk", "json"):
                self._safe(self.incoming / f"{upload_id}.{suffix}").unlink(missing_ok=True)
            self._reservations.pop(upload_id, None)

    def completed_path(self, session: UploadSession) -> Path:
        """Resolve only the finalized file belonging to this upload."""
        if session.path is None or session.mime is None:
            raise UploadError("upload_incomplete", 409)
        relative = Path(session.path)
        if relative.is_absolute() or ".." in relative.parts or relative.stem != session.id:
            raise UploadError("file_unavailable", 404)
        path = self._safe(self.directory / relative)
        if not path.is_file() or path.stat().st_size != session.size:
            raise UploadError("file_unavailable", 404)
        return path

    async def claim(
        self,
        upload_ids: list[str],
        tid: str,
        owner: str,
        entry_id: str,
        append: Callable[[list[UploadSession]], None],
    ) -> list[UploadSession]:
        """Claim a validated batch once, rolling back ordinary storage failures."""
        if len(set(upload_ids)) != len(upload_ids):
            raise UploadError("bad_attachments", 400)
        async with contextlib.AsyncExitStack() as stack:
            for upload_id in sorted(upload_ids):
                await stack.enter_async_context(self._lock(upload_id))
            return await complete_write(self._claim, upload_ids, tid, owner, entry_id, append)

    def _claim(
        self,
        upload_ids: list[str],
        tid: str,
        owner: str,
        entry_id: str,
        append: Callable[[list[UploadSession]], None],
    ) -> list[UploadSession]:
        sessions = [self._load(upload_id, tid, owner) for upload_id in upload_ids]
        for session in sessions:
            self.completed_path(session)
            if session.used_by is not None:
                raise UploadError("attachment_used", 409)
        marked = []
        try:
            for session in sessions:
                session.used_by = entry_id
                marked.append(session)
                self._save(session)
            append(sessions)
        except BaseException:
            for session in marked:
                session.used_by = None
                self._save(session)
            raise
        return sessions

    async def recover_claims(self, indexed_entries: set[str] | None) -> int:
        """Release interrupted claims only when their index entry is absent."""
        await complete_write(self._prepare)
        paths = await asyncio.to_thread(lambda: list(self.incoming.glob("*.json")))
        retained = 0
        for path in paths:
            if _ID.fullmatch(path.stem) is None:
                continue
            async with self._lock(path.stem):
                retained += await complete_write(self._recover_claim, path, indexed_entries)
        return retained

    def _recover_claim(self, path: Path, indexed_entries: set[str] | None) -> int:
        try:
            session = UploadSession(**json.loads(self._safe(path).read_text()))
            self._validate_record(session)
        except (FileNotFoundError, ValueError, TypeError, UploadError):
            return 0
        if indexed_entries is None:
            return int(session.used_by is not None)
        if session.used_by is not None and session.used_by not in indexed_entries:
            session.used_by = None
            self._save(session)
        return 0

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


class AttachmentSettings:
    """Persist browser preferences within administrator configured hard caps."""

    def __init__(self, path: Path, config: WebchatConfig) -> None:
        self.path = path
        self.config = config
        self._lock = asyncio.Lock()

    def _view(self, values: dict[str, Any]) -> dict[str, int]:
        file_cap = self.config.attach_max_file_bytes
        count_cap = self.config.attach_max_files_per_message
        size = values.get("max_file_bytes", MAX_FILE_BYTES)
        count = values.get("max_files_per_message", 10)
        if type(size) is not int or type(count) is not int:
            raise TypeError("invalid_attachment_settings")
        return {
            "max_file_bytes": max(1, min(file_cap, size)),
            "max_files_per_message": max(1, min(count_cap, count)),
            "server_max_file_bytes": file_cap,
            "server_max_files_per_message": count_cap,
            "chunk_bytes": self.config.upload_chunk_bytes,
        }

    def _invalid(self) -> dict[str, int]:
        logger.warning("Attachment settings are invalid; using defaults")
        return self._view({})

    def _read(self) -> dict[str, int]:
        try:
            raw = json.loads(self.path.read_text())
            if not isinstance(raw, dict):
                return self._invalid()
            return self._view(raw)
        except FileNotFoundError:
            return self._view({})
        except (ValueError, TypeError):
            return self._invalid()

    async def get(self) -> dict[str, int]:
        async with self._lock:
            return await asyncio.to_thread(self._read)

    def _save(self, values: dict[str, Any]) -> dict[str, int]:
        effective = self._view(values)
        atomic_json_save(
            self.path,
            {key: effective[key] for key in ("max_file_bytes", "max_files_per_message")},
        )
        self.path.chmod(0o600)
        return effective

    async def put(self, values: dict[str, Any]) -> dict[str, int]:
        if set(values) != {"max_file_bytes", "max_files_per_message"}:
            raise UploadError("bad_request", 400)
        try:
            self._view(values)
        except (ValueError, TypeError):
            raise UploadError("bad_request", 400) from None
        async with self._lock:
            return await complete_write(self._save, values)


UPLOADS = web.AppKey("webchat_uploads", UploadStore)
ATTACH_SETTINGS = web.AppKey("webchat_attachment_settings", AttachmentSettings)


def register_upload_routes(  # noqa: C901, PLR0915 -- route lifecycle and request dispatch
    app: web.Application,
    directory: Path,
    lookup: Callable[[str], Awaitable[dict[str, Any] | None]],
    *,
    config: WebchatConfig,
    settings_path: Path,
) -> None:
    """Register bridge-only streaming routes; bridge middleware authenticates first."""
    store = UploadStore(
        directory,
        max_file_bytes=config.attach_max_file_bytes,
        chunk_bytes=config.upload_chunk_bytes,
    )
    settings = AttachmentSettings(settings_path, config)
    app[UPLOADS] = store
    app[ATTACH_SETTINGS] = settings

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
                    limits = await settings.get()
                    session = await store.start(
                        tid, owner, data, max_file_bytes=limits["max_file_bytes"]
                    )
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

    async def attachment_settings(request: web.Request) -> web.Response:
        try:
            if request.method == "GET":
                return web.json_response(await settings.get())
            raw = await request.json()
            if not isinstance(raw, dict):
                return web.json_response({"error": "bad_request"}, status=400)
            return web.json_response(await settings.put(raw))
        except UploadError as exc:
            return web.json_response({"error": exc.code}, status=exc.status)
        except (ValueError, UnicodeError):
            return web.json_response({"error": "bad_request"}, status=400)
        except OSError:
            return web.json_response({"error": "storage_unavailable"}, status=507)

    app.router.add_get("/webchat/v1/attach-settings", attachment_settings)
    app.router.add_put("/webchat/v1/attach-settings", attachment_settings)
    app.router.add_post(_PREFIX, upload)
    app.router.add_get(_PREFIX + "/{upload_id}", upload)
    app.router.add_put(_PREFIX + "/{upload_id}", upload)
    app.router.add_delete(_PREFIX + "/{upload_id}", upload)
    app.router.add_post(_PREFIX + "/{upload_id}/complete", upload)
