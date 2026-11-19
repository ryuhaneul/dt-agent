"""Single-use message attachments with a separate, rollback-compatible index."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ductor_bot.chat_log.ids import new_id
from ductor_bot.chat_log.recorder import ChatLogTurn
from ductor_bot.files.prompt import MediaInfo, build_media_prompt
from ductor_bot.files.tags import classify_mime
from ductor_bot.infra.file_lock import exclusive_file_lock
from ductor_bot.webchat.uploads import AttachmentSettings, UploadError, UploadSession, UploadStore

logger = logging.getLogger(__name__)
_ID = re.compile(r"[a-f0-9]{32}\Z")


@dataclass(frozen=True, slots=True)
class AttachmentMessage:
    """The prompt and history identity prepared before starting the turn."""

    text: str
    turn: ChatLogTurn
    caption: str
    files: tuple[dict[str, Any], ...]


class AttachmentIndex:
    """Append durable attachment snapshots, never modifying history."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @staticmethod
    def _valid_attachment(raw: object) -> bool:
        if not isinstance(raw, dict):
            return False
        path = raw.get("path")
        return (
            isinstance(raw.get("id"), str)
            and _ID.fullmatch(raw["id"]) is not None
            and isinstance(raw.get("name"), str)
            and type(raw.get("size")) is int
            and raw["size"] >= 0
            and isinstance(raw.get("mime"), str)
            and isinstance(path, str)
            and (not Path(path).is_absolute() or raw.get("provenance") == "delivered")
            and ".." not in Path(path).parts
            and type(raw.get("inline_allowed")) is bool
        )

    def _rows(self) -> tuple[list[dict[str, Any]], bool]:
        rows = []
        damaged = False
        if self.path.is_symlink():
            logger.warning("Attachment index unavailable; history remains readable")
            return [], True
        try:
            with self.path.open("rb") as stream:
                for line in stream:
                    try:
                        raw = json.loads(line.decode("utf-8"))
                        if (
                            not isinstance(raw, dict)
                            or not isinstance(raw.get("entry_id"), str)
                            or not isinstance(raw.get("tid"), str)
                            or not isinstance(raw.get("attachments"), list)
                            or not all(self._valid_attachment(item) for item in raw["attachments"])
                        ):
                            damaged = True
                            continue
                    except (ValueError, TypeError):
                        damaged = True
                        continue
                    rows.append(raw)
        except FileNotFoundError:
            return [], False
        except OSError:
            logger.warning("Attachment index unavailable; history remains readable")
            return [], True
        if damaged:
            logger.warning("Skipping damaged attachment index records")
        return rows, damaged

    @staticmethod
    def _write_record(fd: int, payload: bytes) -> None:
        if os.write(fd, payload) != len(payload):
            raise OSError("incomplete_attachment_index_write")
        os.fsync(fd)

    def append(self, tid: str, entry_id: str, attachments: list[dict[str, Any]]) -> None:
        """Serialize an append and remove partial bytes on ordinary I/O errors."""
        payload = (
            json.dumps(
                {"tid": tid, "entry_id": entry_id, "attachments": attachments}, ensure_ascii=False
            )
            + "\n"
        ).encode("utf-8")
        with exclusive_file_lock(self.path):
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            try:
                os.fchmod(fd, 0o600)
                previous = os.lseek(fd, 0, os.SEEK_END)
                if previous:
                    os.lseek(fd, previous - 1, os.SEEK_SET)
                    if os.read(fd, 1) != b"\n":
                        payload = b"\n" + payload
                try:
                    self._write_record(fd, payload)
                except BaseException:
                    os.ftruncate(fd, previous)
                    os.fsync(fd)
                    raise
            finally:
                os.close(fd)

    async def get(self, tid: str, entry_id: str) -> list[dict[str, Any]]:
        rows, _ = await asyncio.to_thread(self._rows)
        for row in reversed(rows):
            if row["tid"] == tid and row["entry_id"] == entry_id:
                return list(row["attachments"])
        return []


class MessageAttachments:
    """Keep attachment admission independent of command and queue dispatch."""

    def __init__(
        self, store: UploadStore, settings: AttachmentSettings, workspace: Path, index_path: Path
    ) -> None:
        self.store = store
        self.settings = settings
        self.workspace = workspace
        self.index = AttachmentIndex(index_path)

    async def recover(self) -> None:
        rows, damaged = await asyncio.to_thread(self.index._rows)
        if damaged:
            retained = await self.store.recover_claims(None)
            logger.warning("Retaining %d attachment claims: index_damaged_or_unavailable", retained)
        else:
            latest = {(row["tid"], row["entry_id"]): row for row in rows}
            await self.store.recover_claims(
                {row["entry_id"] for row in latest.values() if row["attachments"]}
            )

    async def rollback(self, tid: str, owner: str, prepared: AttachmentMessage) -> None:
        entry_id = prepared.turn["user_entry_id"]
        await self.store.release_claims(
            [item["id"] for item in prepared.files],
            tid,
            owner,
            entry_id,
            lambda: self.index.append(tid, entry_id, []),
        )

    def _metadata(self, session: UploadSession) -> dict[str, Any]:
        path = self.store.completed_path(session).relative_to(self.workspace).as_posix()
        return {
            "id": session.id,
            "name": session.name,
            "size": session.size,
            "mime": session.mime,
            "path": path,
            "inline_allowed": session.inline_allowed,
        }

    async def prepare(
        self, tid: str, owner: str, text: str, upload_ids: object
    ) -> AttachmentMessage:
        if (
            not isinstance(upload_ids, list)
            or not upload_ids
            or any(not isinstance(item, str) or _ID.fullmatch(item) is None for item in upload_ids)
            or len(set(upload_ids)) != len(upload_ids)
        ):
            raise UploadError("bad_attachments", 400)
        if not owner or len(owner) > 128:
            raise UploadError("forbidden", 403)
        limits = await self.settings.get()
        if len(upload_ids) > limits["max_files_per_message"]:
            raise UploadError("too_many_attachments", 413)
        turn: ChatLogTurn = {
            "turn_id": new_id(),
            "user_entry_id": new_id(),
            "reply_entry_id": new_id(),
        }

        def append(sessions: list[UploadSession]) -> None:
            self.index.append(
                tid, turn["user_entry_id"], [self._metadata(item) for item in sessions]
            )

        sessions = await self.store.claim(upload_ids, tid, owner, turn["user_entry_id"], append)
        prompts = []
        for index, session in enumerate(sessions):
            assert session.mime is not None
            prompts.append(
                build_media_prompt(
                    MediaInfo(
                        caption=text if index == 0 else None,
                        file_name=session.name,
                        media_type=session.mime,
                        original_type=classify_mime(session.mime),
                        path=self.store.completed_path(session),
                    ),
                    self.workspace,
                    transport="Web",
                )
            )
        return AttachmentMessage(
            "\n\n".join(prompts), turn, text, tuple(self._metadata(item) for item in sessions)
        )
