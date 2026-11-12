"""Conversions between Telegram session identities and storage names."""

from __future__ import annotations

import re
import secrets
from datetime import UTC, datetime

from ductor_bot.session.key import SessionKey

_TID = re.compile(r"^-?[0-9]+(?:\.[0-9]+)?$")
_FILENAME = re.compile(r"^tg_(-?[0-9]+)(?:_([0-9]+))?\.jsonl$")


def new_id() -> str:
    return secrets.token_hex(6)


def timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def to_tid(key: SessionKey) -> str:
    if key.transport != "tg" or (key.topic_id is not None and key.topic_id < 0):
        raise ValueError("Expected a Telegram session key")
    return str(key.chat_id) if key.topic_id is None else f"{key.chat_id}.{key.topic_id}"


def from_tid(tid: str) -> SessionKey:
    if not _TID.fullmatch(tid):
        raise ValueError("Invalid topic identifier")
    chat, _, topic = tid.partition(".")
    return SessionKey.telegram(int(chat), int(topic) if topic else None)


def history_filename(tid: str) -> str:
    if tid == "unknown":
        return "unknown.jsonl"
    key = from_tid(tid)
    suffix = "" if key.topic_id is None else f"_{key.topic_id}"
    return f"tg_{key.chat_id}{suffix}.jsonl"


def filename_tid(filename: str) -> str:
    if filename == "unknown.jsonl":
        return "unknown"
    match = _FILENAME.fullmatch(filename)
    if match is None:
        raise ValueError("Invalid history filename")
    chat, topic = match.groups()
    return to_tid(SessionKey.telegram(int(chat), int(topic) if topic else None))
