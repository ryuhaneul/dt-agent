"""Durable records for asynchronous inter-agent requests.

Records retain the original routing target and the complete result. Delivery,
retry, and cleanup policy belongs to the bus stages that consume these records.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import time
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from ductor_bot.bus.envelope import IA_CHUNK_SCHEMES
from ductor_bot.infra.atomic_io import atomic_text_save
from ductor_bot.session.named import interagent_session_name, normalize_lane
from ductor_bot.workspace.paths import DuctorPaths

if TYPE_CHECKING:
    from ductor_bot.multiagent.bus import AsyncInterAgentResult, AsyncInterAgentTask

logger = logging.getLogger(__name__)
_TASK_ID = re.compile(r"[0-9a-f]{12}\Z")
_TEXT_HASH = re.compile(r"[0-9a-f]{64}\Z")
_CHUNK_SCHEME = re.compile(r"[a-z][a-z0-9._-]{0,63}\Z")
_OPTIONAL_RECORD_DEFAULTS = {
    "delivered_chunks": 0,
    "delivered_text_sha256": "",
    "chunk_scheme": "",
    "redelivery_attempts": 0,
    "next_redelivery_at": None,
    # Lane support: absent in older records (read as no lane); older products ignore them.
    "lane": "",
    "session_name": "",
}
_OPTIONAL_RESULT_DEFAULTS = {
    "main_response": "",
    "delivered_chunks": 0,
    "delivery_retry": False,
    "failure_kind": "",
    "recipient_headless": False,
}

_ROUTING_FIELDS = (
    "sender",
    "recipient",
    "reply_to",
    "chat_id",
    "topic_id",
    "transport",
    "result_origin",
    "broadcast_ref",
)
_RESULT_STRING_FIELDS = frozenset(
    {
        "task_id",
        "sender",
        "recipient",
        "message_preview",
        "result_text",
        "session_name",
        "provider_switch_notice",
        "original_message",
        "transport",
        "reply_to",
        "result_origin",
        "broadcast_ref",
        "main_response",
        "failure_kind",
    }
)
_RESULT_INT_FIELDS = frozenset({"chat_id", "failure_count", "delivered_chunks"})
_RESULT_BOOL_FIELDS = frozenset({"success", "delivery_retry", "recipient_headless"})
_RESULT_REQUIRED_FIELDS = (
    _RESULT_STRING_FIELDS
    | _RESULT_INT_FIELDS
    | _RESULT_BOOL_FIELDS
    | {"topic_id", "error", "elapsed_seconds", "received_at"}
)

_REQUIRED_FIELDS = frozenset(
    {
        "schema",
        "task_id",
        "sender",
        "recipient",
        "reply_to",
        "target",
        "target_key",
        "chat_id",
        "topic_id",
        "transport",
        "result_origin",
        "broadcast_ref",
        "status",
        "stage",
        "main_response",
        "injected_at",
        "message",
        "created_at",
        "updated_at",
    }
)


def _valid_lane_fields(record: dict[str, Any]) -> bool:
    lane, name = record.get("lane", ""), record.get("session_name", "")
    if type(lane) is not str or type(name) is not str:
        return False
    try:
        return normalize_lane(lane) == lane
    except ValueError:
        return False


class CorruptAsyncRecordError(ValueError):
    """A persisted async record does not match the durable schema."""


def _valid_result(record: dict[str, Any]) -> bool:
    result = record.get("result")
    if result is None:
        # Delivery acknowledgement can outlive an earlier result write failure.
        return cast("bool", record["status"] == "inflight" or record["stage"] == "delivered")
    if not isinstance(result, dict) or set(result) != _RESULT_REQUIRED_FIELDS:
        return False
    return (
        all(type(result[key]) is str for key in _RESULT_STRING_FIELDS)
        and all(type(result[key]) is int for key in _RESULT_INT_FIELDS)
        and all(result[key] >= 0 for key in _RESULT_INT_FIELDS if key != "chat_id")
        and all(type(result[key]) is bool for key in _RESULT_BOOL_FIELDS)
        and (result["topic_id"] is None or type(result["topic_id"]) is int)
        and (result["error"] is None or type(result["error"]) is str)
        and type(result["elapsed_seconds"]) in (int, float)
        and type(result["received_at"]) in (int, float)
        and math.isfinite(result["received_at"])
        and 0 <= result["received_at"] <= 253402300799
        and result["task_id"] == record["task_id"]
        and all(result[key] == record[key] for key in _ROUTING_FIELDS)
    )


def merge_redelivery_state(
    first: tuple[int, float | None],
    second: tuple[int, float | None],
) -> tuple[int, float | None]:
    """Keep the stronger persisted or in-memory retry state."""
    if first[0] != second[0]:
        return max(first, second, key=lambda item: item[0])
    first_at = float("-inf") if first[1] is None else first[1]
    second_at = float("-inf") if second[1] is None else second[1]
    return first if first_at >= second_at else second


def progress_reset_reason(record: dict[str, Any], *, transport: str = "") -> str:
    """Why saved chunks cannot be reused for the next final-report body."""
    if record["stage"] != "injected":
        return "stage is not injected"
    expected_scheme = IA_CHUNK_SCHEMES.get(transport or record["transport"], "")
    if not record["chunk_scheme"]:
        return "chunk scheme missing"
    if record["chunk_scheme"] != expected_scheme:
        return "chunk scheme mismatch"
    saved_hash = record["delivered_text_sha256"]
    if not saved_hash:
        return "text hash missing"
    if saved_hash != hashlib.sha256(record["main_response"].encode("utf-8")).hexdigest():
        return "text hash mismatch"
    return ""


def target_key(target: str) -> str:
    """Use a stable path component without trusting an agent name as a path."""
    return hashlib.sha256(target.encode("utf-8")).hexdigest()[:32]


class AsyncRecordStore:
    """Store one JSON file per task beneath the main agent's home."""

    def __init__(self, main_home: Path) -> None:
        self.root = DuctorPaths(main_home).interagent_dir
        self._warned_invalid_backoff: set[tuple[str, str]] = set()

    def _checked_path(self, path: Path) -> Path:
        for directory in (self.root, path.parent.parent, path.parent):
            if directory.is_symlink():
                raise ValueError("async record directory is a symlink")
        if not path.resolve().is_relative_to(self.root.resolve()):
            raise ValueError("async record path escapes storage root")
        if path.is_symlink():
            raise ValueError("async record file is a symlink")
        return path

    def path_for(self, target: str, task_id: str, *, area: str = "inflight") -> Path:
        if _TASK_ID.fullmatch(task_id) is None:
            raise ValueError("invalid async task_id")
        if area not in {"inflight", "undelivered"}:
            raise ValueError("invalid async record area")
        return self._checked_path(self.root / area / target_key(target) / f"{task_id}.json")

    def corrupt_path_for(self, target: str, task_id: str, timestamp: str) -> Path:
        """Keep damaged bytes outside the directories scanned for delivery."""
        if (
            _TASK_ID.fullmatch(task_id) is None
            or re.fullmatch(
                r"[0-9]{8}T[0-9]{12}Z",
                timestamp,
            )
            is None
        ):
            raise ValueError("invalid corrupt async record path")
        return self._checked_path(
            self.root / "corrupt" / target_key(target) / f"{task_id}.{timestamp}.json"
        )

    def _write(self, path: Path, record: dict[str, Any]) -> None:
        old_umask = os.umask(0o077)
        try:
            for directory in (self.root, path.parent.parent, path.parent):
                if directory.is_symlink():
                    raise ValueError("async record directory is a symlink")
                directory.mkdir(mode=0o700, exist_ok=True)
                directory.chmod(0o700)
            # Recheck after creation, before atomic_text_save creates its temp file.
            if path != self.path_for(
                record["target"], record["task_id"], area=path.parent.parent.name
            ):
                raise ValueError("async record path changed")
            atomic_text_save(path, json.dumps(record, ensure_ascii=False, sort_keys=True))
            path.chmod(0o600)
        finally:
            os.umask(old_umask)

    def _new_record(self, task: AsyncInterAgentTask) -> dict[str, Any]:
        target = task.reply_to or task.sender
        now = time.time()
        return {
            "schema": 1,
            "task_id": task.task_id,
            "sender": task.sender,
            "recipient": task.recipient,
            "reply_to": task.reply_to,
            "target": target,
            "target_key": target_key(target),
            "chat_id": task.chat_id,
            "topic_id": task.topic_id,
            "transport": task.transport,
            "result_origin": task.result_origin,
            "broadcast_ref": task.broadcast_ref,
            "lane": task.lane,
            "session_name": interagent_session_name(
                task.sender, task.chat_id, task.topic_id, task.lane
            ),
            "status": "inflight",
            "stage": "received",
            "main_response": "",
            "delivered_chunks": 0,
            "delivered_text_sha256": "",
            "chunk_scheme": "",
            "injected_at": None,
            "message": task.message,
            "result": None,
            "created_at": now,
            "updated_at": now,
        }

    def create(self, task: AsyncInterAgentTask) -> None:
        target = task.reply_to or task.sender
        self._write(self.path_for(target, task.task_id), self._new_record(task))

    def recover_corrupt(  # noqa: PLR0913 -- restore known stage and chunk progress
        self,
        task: AsyncInterAgentTask,
        result: AsyncInterAgentResult,
        *,
        stage: str = "received",
        main_response: str = "",
        delivered_chunks: int = 0,
        delivered_text_sha256: str = "",
        chunk_scheme: str = "",
        area: str = "inflight",
    ) -> Path:
        """Archive a damaged record and atomically replace it from memory."""
        target = task.reply_to or task.sender
        source = self.path_for(target, task.task_id, area=area)
        archived = self.quarantine_corrupt(target, task.task_id, area=area)

        record = self._new_record(task)
        normalized = replace(
            result,
            sender=task.sender,
            recipient=task.recipient,
            reply_to=task.reply_to,
            chat_id=task.chat_id,
            topic_id=task.topic_id,
            transport=task.transport,
            result_origin=task.result_origin,
            broadcast_ref=task.broadcast_ref,
            original_message=task.message,
        )
        record["status"] = "completed" if normalized.success else "failed"
        record["result"] = asdict(normalized)
        if stage in {"injected", "delivered"} and main_response.strip():
            record["stage"] = stage
            record["main_response"] = main_response
            record["injected_at"] = time.time()
            expected_hash = hashlib.sha256(main_response.encode("utf-8")).hexdigest()
            if (
                delivered_text_sha256 == expected_hash
                and chunk_scheme in IA_CHUNK_SCHEMES.values()
                and (not task.transport or chunk_scheme == IA_CHUNK_SCHEMES.get(task.transport))
                and delivered_chunks > 0
            ):
                record["delivered_chunks"] = delivered_chunks
                record["delivered_text_sha256"] = expected_hash
                record["chunk_scheme"] = chunk_scheme
        record["updated_at"] = time.time()
        self._write(source, record)
        self.load(target, task.task_id, area=area)
        return archived

    def quarantine_corrupt(self, target: str, task_id: str, *, area: str = "undelivered") -> Path:
        """Atomically preserve damaged bytes outside both delivery directories."""
        source = self.path_for(target, task_id, area=area)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        archived = self.corrupt_path_for(target, task_id, stamp)
        old_umask = os.umask(0o077)
        try:
            for directory in (self.root, archived.parent.parent, archived.parent):
                if directory.is_symlink():
                    raise ValueError("async record directory is a symlink")
                directory.mkdir(mode=0o700, exist_ok=True)
                directory.chmod(0o700)
            archived = self.corrupt_path_for(target, task_id, stamp)
            if archived.exists():
                raise FileExistsError("corrupt async record archive already exists")
            os.replace(source, archived)  # noqa: PTH105
            archived.chmod(0o600)
        finally:
            os.umask(old_umask)
        return archived

    def load(self, target: str, task_id: str, *, area: str = "inflight") -> dict[str, Any]:
        path = self.path_for(target, task_id, area=area)
        record = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(record, dict):
            for key, default in _OPTIONAL_RECORD_DEFAULTS.items():
                record.setdefault(key, default)
            if isinstance(record.get("result"), dict):
                for key, default in _OPTIONAL_RESULT_DEFAULTS.items():
                    record["result"].setdefault(key, default)
                # Legacy records cannot recover exact receipt; retain their saved
                # result-record time instead of inventing a new time on every replay.
                record["result"].setdefault("received_at", record.get("updated_at"))
        if (
            not isinstance(record, dict)
            or not _REQUIRED_FIELDS.issubset(record)
            or type(record["schema"]) is not int
            or record["schema"] != 1
            or record["task_id"] != task_id
            or record["target"] != target
            or record["target_key"] != target_key(target)
            or record["target"] != (record["reply_to"] or record["sender"])
            or any(
                type(record[key]) is not str
                for key in _ROUTING_FIELDS
                if key not in {"chat_id", "topic_id"}
            )
            or type(record["chat_id"]) is not int
            or type(record["message"]) is not str
            or type(record["created_at"]) not in (int, float)
            or type(record["updated_at"]) not in (int, float)
            or (record["topic_id"] is not None and type(record["topic_id"]) is not int)
            or type(record.get("delivered_chunks", 0)) is not int
            or record["delivered_chunks"] < 0
            or type(record["delivered_text_sha256"]) is not str
            or type(record["chunk_scheme"]) is not str
            or not _valid_lane_fields(record)
            or (record["chunk_scheme"] and _CHUNK_SCHEME.fullmatch(record["chunk_scheme"]) is None)
            or (
                record["delivered_text_sha256"]
                and _TEXT_HASH.fullmatch(record["delivered_text_sha256"]) is None
            )
            or not isinstance(record["status"], str)
            or record["status"] not in {"inflight", "completed", "failed"}
            or not isinstance(record["stage"], str)
            or record["stage"] not in {"received", "injected", "delivered"}
            or type(record["main_response"]) is not str
            or (record["stage"] == "injected" and not record["main_response"].strip())
            or not _valid_result(record)
        ):
            raise CorruptAsyncRecordError("invalid async record schema")
        invalid_backoff = (
            type(record["redelivery_attempts"]) is not int
            or record["redelivery_attempts"] < 0
            or (
                record["next_redelivery_at"] is not None
                and (
                    type(record["next_redelivery_at"]) not in (int, float)
                    or record["next_redelivery_at"] < 0
                    or (
                        type(record["next_redelivery_at"]) is float
                        and not math.isfinite(record["next_redelivery_at"])
                    )
                )
            )
        )
        if invalid_backoff:
            if (target, task_id) not in self._warned_invalid_backoff:
                logger.warning(
                    "Invalid async redelivery metadata task=%s; using defaults",
                    task_id,
                )
                self._warned_invalid_backoff.add((target, task_id))
            record["redelivery_attempts"] = 0
            record["next_redelivery_at"] = None
        return record

    def update(  # noqa: C901, PLR0913
        self,
        target: str,
        task_id: str,
        *,
        result: AsyncInterAgentResult | None = None,
        stage: str | None = None,
        main_response: str | None = None,
        delivered_chunks: int | None = None,
        delivered_text_sha256: str | None = None,
        chunk_scheme: str | None = None,
        reset_progress: bool = False,
        redelivery_state: tuple[int, float | None] | None = None,
    ) -> None:
        path = self.path_for(target, task_id)
        record = self.load(target, task_id)
        prior_stage = record["stage"]
        if result is not None:
            record["status"] = "completed" if result.success else "failed"
            record["result"] = asdict(result)
        if stage is not None:
            record["stage"] = stage
        if main_response is not None:
            if prior_stage != "injected" or record["main_response"] != main_response:
                reset_progress = True
            record["main_response"] = main_response
            record["injected_at"] = time.time()
        if reset_progress:
            record["delivered_chunks"] = 0
            record["delivered_text_sha256"] = ""
            record["chunk_scheme"] = ""
        if delivered_chunks is not None:
            text_hash = delivered_text_sha256 or ""
            if delivered_chunks > 0 and (not text_hash or not chunk_scheme):
                raise ValueError("chunk progress requires a text hash and scheme")
            if text_hash and chunk_scheme:
                if (
                    text_hash != record["delivered_text_sha256"]
                    or chunk_scheme != record["chunk_scheme"]
                ):
                    record["delivered_chunks"] = delivered_chunks
                    record["delivered_text_sha256"] = text_hash
                    record["chunk_scheme"] = chunk_scheme
                else:
                    record["delivered_chunks"] = max(record["delivered_chunks"], delivered_chunks)
        if redelivery_state is not None:
            current = record["redelivery_attempts"], record["next_redelivery_at"]
            attempts, next_at = merge_redelivery_state(current, redelivery_state)
            record["redelivery_attempts"] = attempts
            record["next_redelivery_at"] = next_at
        record["updated_at"] = time.time()
        self._write(path, record)

    def move_to_undelivered(self, target: str, task_id: str) -> Path:
        """Preserve the complete record for a later delivery attempt."""
        source = self.path_for(target, task_id)
        destination = self.path_for(target, task_id, area="undelivered")
        old_umask = os.umask(0o077)
        try:
            for directory in (self.root, destination.parent.parent, destination.parent):
                if directory.is_symlink():
                    raise ValueError("async record directory is a symlink")
                directory.mkdir(mode=0o700, exist_ok=True)
                directory.chmod(0o700)
            destination = self.path_for(target, task_id, area="undelivered")
            os.replace(source, destination)  # noqa: PTH105
            destination.chmod(0o600)
        finally:
            os.umask(old_umask)
        return destination

    def move_to_inflight(self, target: str, task_id: str) -> Path:
        """Move a parked result into the active delivery slot."""
        source = self.path_for(target, task_id, area="undelivered")
        destination = self.path_for(target, task_id)
        old_umask = os.umask(0o077)
        try:
            for directory in (self.root, destination.parent.parent, destination.parent):
                if directory.is_symlink():
                    raise ValueError("async record directory is a symlink")
                directory.mkdir(mode=0o700, exist_ok=True)
                directory.chmod(0o700)
            destination = self.path_for(target, task_id)
            if destination.exists():
                raise FileExistsError("async result is already inflight")
            os.replace(source, destination)  # noqa: PTH105
            destination.chmod(0o600)
        finally:
            os.umask(old_umask)
        return destination

    def delete(self, target: str, task_id: str) -> None:
        """Remove a record only after the final report is delivered."""
        self.path_for(target, task_id).unlink()

    @staticmethod
    def result_from_record(record: dict[str, Any]) -> AsyncInterAgentResult:
        from ductor_bot.multiagent.bus import AsyncInterAgentResult

        result = dict(record["result"])
        result.setdefault("received_at", record["updated_at"])
        reason = progress_reset_reason(record)
        if reason and record["delivered_chunks"]:
            logger.warning(
                "IA chunk progress reset task=%s reason=%s; duplicate delivery possible",
                record["task_id"],
                reason,
            )
        result["delivered_chunks"] = 0 if reason else record["delivered_chunks"]
        result["main_response"] = record["main_response"] if record["stage"] == "injected" else ""
        for key in _ROUTING_FIELDS:
            result[key] = record[key]
        return AsyncInterAgentResult(**result)
