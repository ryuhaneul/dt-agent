"""Versioned editing of the main agent's four permitted Markdown files."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from ductor_bot.infra.atomic_io import atomic_bytes_save, complete_write
from ductor_bot.multiagent.shared_knowledge import (
    _END_MARKER,
    _LEGACY_END,
    _LEGACY_START,
    _START_MARKER,
    render_shared_knowledge,
)

if TYPE_CHECKING:
    from pathlib import Path

    from ductor_bot.workspace.paths import DuctorPaths

_LIMIT = 262144
_STARTS = {_START_MARKER: _END_MARKER, _LEGACY_START: _LEGACY_END}
_MARKERS = re.compile("|".join(re.escape(marker) for marker in (*_STARTS, *_STARTS.values())))


class WebFileError(Exception):
    def __init__(self, code: str, status: int) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


def _split_shared(text: str) -> tuple[str, list[str]]:
    spans: list[tuple[int, int]] = []
    opened: tuple[int, str] | None = None
    for match in _MARKERS.finditer(text):
        marker = match.group()
        if marker in _STARTS:
            if opened is not None:
                raise WebFileError("shared_block_invalid", 409)
            opened = (match.start(), _STARTS[marker])
        else:
            if opened is None or marker != opened[1]:
                raise WebFileError("shared_block_invalid", 409)
            spans.append((opened[0], match.end()))
            opened = None
    if opened is not None:
        raise WebFileError("shared_block_invalid", 409)
    body: list[str] = []
    blocks: list[str] = []
    cursor = 0
    for start, end in spans:
        body.append(text[cursor:start])
        blocks.append(text[start:end])
        cursor = end
    body.append(text[cursor:])
    return "".join(body), blocks


class WebFiles:
    def __init__(self, paths: DuctorPaths) -> None:
        self.paths = paths
        self._paths = {
            "PERSONA.md": paths.workspace / "PERSONA.md",
            "BEHAVIOR.md": paths.workspace / "BEHAVIOR.md",
            "MAINMEMORY.md": paths.mainmemory_path,
            "SHAREDMEMORY.md": paths.ductor_home / "SHAREDMEMORY.md",
        }
        self._locks = {name: asyncio.Lock() for name in self._paths}

    def allowed(self, name: str) -> bool:
        return name in self._paths

    def _path(self, name: str) -> Path:
        logical = self._paths.get(name)
        if logical is None:
            raise WebFileError("not_found", 404)
        try:
            expected = logical.parent.resolve() / logical.name
            resolved = logical.resolve()
            home = self.paths.ductor_home.resolve()
            if resolved != expected or not resolved.is_relative_to(home) or not resolved.is_file():
                raise WebFileError("not_found", 404)
        except (OSError, RuntimeError):
            raise WebFileError("not_found", 404) from None
        return resolved

    def _snapshot(self, name: str) -> tuple[Path, bytes, os.stat_result]:
        path = self._path(name)
        try:
            with path.open("rb") as stream:
                original = stream.read()
                stat = os.fstat(stream.fileno())
        except (FileNotFoundError, IsADirectoryError):
            raise WebFileError("not_found", 404) from None
        return path, original, stat

    def _metadata(self, name: str) -> dict[str, Any]:
        _, original, stat = self._snapshot(name)
        return {
            "name": name,
            "sha256": hashlib.sha256(original).hexdigest(),
            "size": len(original),
            "mtime": datetime.fromtimestamp(stat.st_mtime, UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
        }

    async def list(self) -> list[dict[str, Any]]:
        result = []
        for name in self._paths:
            async with self._locks[name]:
                try:
                    result.append(await asyncio.to_thread(self._metadata, name))
                except WebFileError as exc:
                    if exc.status != 404:
                        raise
        return result

    def _get(self, name: str) -> dict[str, Any]:
        _, original, _ = self._snapshot(name)
        try:
            content = original.decode("utf-8")
        except UnicodeError:
            raise WebFileError("bad_request", 400) from None
        blocks: list[str] = []
        if name == "MAINMEMORY.md":
            content, blocks = _split_shared(content)
        return {
            "name": name,
            "content": content,
            "sha256": hashlib.sha256(original).hexdigest(),
            "shared_block": bool(blocks),
        }

    async def get(self, name: str) -> dict[str, Any]:
        if not self.allowed(name):
            raise WebFileError("not_found", 404)
        async with self._locks[name]:
            return await asyncio.to_thread(self._get, name)

    def _memory_bytes(self, content: str) -> bytes:
        shared = self._paths["SHAREDMEMORY.md"]
        if shared.is_file():
            shared = self._path("SHAREDMEMORY.md")
            content = render_shared_knowledge(content, shared.read_text(encoding="utf-8"))
        encoded = content.encode("utf-8")
        if len(encoded) > _LIMIT:
            raise WebFileError("too_large", 413)
        return encoded

    def _put(self, name: str, content: str, base_sha256: str) -> dict[str, str]:  # noqa: C901
        path, original, _ = self._snapshot(name)
        if hashlib.sha256(original).hexdigest() != base_sha256:
            raise WebFileError("conflict", 409)
        try:
            encoded = content.encode("utf-8")
            if len(encoded) > _LIMIT:
                raise WebFileError("too_large", 413)
            if name in {"MAINMEMORY.md", "SHAREDMEMORY.md"} and _MARKERS.search(content):
                raise WebFileError("bad_request", 400)
            if name == "MAINMEMORY.md":
                _, blocks = _split_shared(original.decode("utf-8"))
                if blocks:
                    content = content.rstrip() + "\n\n" + "\n\n".join(blocks) + "\n"
            elif name == "SHAREDMEMORY.md":
                content = content.strip() + "\n"
            encoded = (
                self._memory_bytes(content) if name == "MAINMEMORY.md" else content.encode("utf-8")
            )
        except UnicodeError:
            raise WebFileError("bad_request", 400) from None
        if len(encoded) > _LIMIT:
            raise WebFileError("too_large", 413)
        now = datetime.now(ZoneInfo("Asia/Seoul"))
        directory = self.paths.webchat_dir / "backups" / now.strftime("%Y-%m-%d")
        stem = f"{name}.{now.strftime('%H%M%S')}"
        backup = directory / stem
        suffix = 0
        while backup.exists():
            suffix += 1
            backup = directory / f"{stem}.{suffix}"
        atomic_bytes_save(backup, original)
        # Recheck after backup I/O, immediately before committing the edit.
        if self._path(name) != path or path.read_bytes() != original:
            backup.unlink()
            raise WebFileError("conflict", 409)
        atomic_bytes_save(path, encoded)
        return {
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "backup": backup.relative_to(self.paths.webchat_dir).as_posix(),
        }

    async def put(self, name: str, content: str, base_sha256: str) -> dict[str, str]:
        if not self.allowed(name):
            raise WebFileError("not_found", 404)
        async with self._locks[name]:
            return await complete_write(self._put, name, content, base_sha256)
