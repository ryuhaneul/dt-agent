"""Slack message sender."""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ductor_bot.files.tags import path_from_file_tag
from ductor_bot.messenger.send_opts import BaseSendOpts

if TYPE_CHECKING:
    from typing import Any

logger = logging.getLogger(__name__)

_MAX_MESSAGE_LENGTH = 39_000
_FILE_TAG_RE = re.compile(r"<file:(.*?)>")


@dataclass(slots=True)
class SlackSendOpts(BaseSendOpts):
    """Options for sending Slack messages."""

    thread_ts: str | None = None


async def send_rich(
    client: Any,
    channel_id: str,
    text: str,
    opts: SlackSendOpts | None = None,
) -> str | None:
    """Send a Slack message, splitting long text and uploading tagged files."""
    _ok, last_ts = await send_rich_checked(client, channel_id, text, opts)
    return last_ts


async def send_rich_checked(
    client: Any,
    channel_id: str,
    text: str,
    opts: SlackSendOpts | None = None,
) -> tuple[bool, str | None]:
    """Return whether every chunk and file upload succeeded, plus the last timestamp."""
    opts = opts or SlackSendOpts()
    all_sent = True
    files = _FILE_TAG_RE.findall(text)
    cleaned = _FILE_TAG_RE.sub("", text).strip()
    last_ts: str | None = None

    for chunk in _split_text(cleaned):
        if not chunk:
            continue
        response = await client.chat_postMessage(
            channel=channel_id,
            text=_to_slack_mrkdwn(chunk),
            mrkdwn=True,
            thread_ts=opts.thread_ts,
        )
        last_ts = _response_value(response, "ts")
        if last_ts is None or _response_ok(response) is False:
            all_sent = False

    for file_path_str in files:
        file_path = path_from_file_tag(file_path_str)
        if not _file_accessible(file_path, opts.allowed_roots):
            all_sent = False
            continue
        response = await client.files_upload_v2(
            channel=channel_id,
            file=str(file_path),
            filename=file_path.name,
            thread_ts=opts.thread_ts,
        )
        upload_ts = _response_value(response, "ts")
        if _response_ok(response) is False or (
            _response_ok(response) is None and upload_ts is None
        ):
            all_sent = False
        last_ts = upload_ts or last_ts

    return all_sent, last_ts


async def send_rich_checked_progress(
    client: Any,
    channel_id: str,
    text: str,
    opts: SlackSendOpts | None = None,
    *,
    start_chunk: int = 0,
) -> tuple[bool, str | None, int]:
    """Send an IA report from a saved chunk offset; stop on the first failure."""
    opts = opts or SlackSendOpts()
    files = _FILE_TAG_RE.findall(text)
    cleaned = _FILE_TAG_RE.sub("", text).strip()
    chunks = [chunk for chunk in _split_text(cleaned) if chunk]
    total = len(chunks) + len(files)
    if start_chunk < 0 or start_chunk > total:
        return False, None, start_chunk

    last_ts: str | None = None
    for index in range(start_chunk, total):
        try:
            if index < len(chunks):
                response = await client.chat_postMessage(
                    channel=channel_id,
                    text=_to_slack_mrkdwn(chunks[index]),
                    mrkdwn=True,
                    thread_ts=opts.thread_ts,
                )
                event_ts = _response_value(response, "ts")
                if not event_ts or event_ts == "None" or _response_ok(response) is False:
                    return False, last_ts, index
            else:
                file_path = path_from_file_tag(files[index - len(chunks)])
                if not _file_accessible(file_path, opts.allowed_roots):
                    return False, last_ts, index
                response = await client.files_upload_v2(
                    channel=channel_id,
                    file=str(file_path),
                    filename=file_path.name,
                    thread_ts=opts.thread_ts,
                )
                event_ts = _response_value(response, "ts")
                if _response_ok(response) is not True or _uploaded_file_id(response) is None:
                    return False, last_ts, index
        except Exception as exc:
            logger.warning("Slack IA chunk send failed: %s", type(exc).__name__)
            return False, last_ts, index
        last_ts = event_ts or last_ts
    return True, last_ts, total


def _uploaded_file_id(response: object) -> str | None:
    """Return the file ID acknowledged by files_upload_v2."""
    data = response if isinstance(response, dict) else getattr(response, "data", None)
    if not isinstance(data, dict):
        return None
    files = data.get("files")
    if not isinstance(files, list) or not files:
        return None
    for file_info in files:
        if not isinstance(file_info, dict):
            continue
        file_id = file_info.get("id")
        if isinstance(file_id, str) and file_id.strip() and file_id != "None":
            return file_id
    return None


def _response_ok(response: object) -> bool | None:
    if isinstance(response, dict):
        return response.get("ok") if isinstance(response.get("ok"), bool) else None
    data = getattr(response, "data", None)
    if isinstance(data, dict):
        return data.get("ok") if isinstance(data.get("ok"), bool) else None
    value = getattr(response, "ok", None)
    return value if isinstance(value, bool) else None


def _response_value(response: object, key: str) -> str | None:
    if isinstance(response, dict):
        value = response.get(key)
        return value if isinstance(value, str) else None
    data = getattr(response, "data", None)
    if isinstance(data, dict):
        value = data.get(key)
        return value if isinstance(value, str) else None
    value = getattr(response, key, None)
    return value if isinstance(value, str) else None


def _file_accessible(file_path: Path, allowed_roots: Sequence[Path] | None) -> bool:
    if not file_path.exists():
        logger.warning("File not found: %s", file_path)
        return False
    if allowed_roots is not None and not any(
        file_path.resolve().is_relative_to(root.resolve()) for root in allowed_roots
    ):
        logger.warning("File outside allowed roots: %s", file_path)
        return False
    return True


def _split_text(text: str) -> list[str]:
    """Split long Slack messages into reasonably sized chunks."""
    if not text:
        return [""]
    if len(text) <= _MAX_MESSAGE_LENGTH:
        return [text]

    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= _MAX_MESSAGE_LENGTH:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, _MAX_MESSAGE_LENGTH)
        if split_at <= 0:
            split_at = _MAX_MESSAGE_LENGTH
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    return chunks


def _to_slack_mrkdwn(text: str) -> str:
    """Best-effort Markdown -> Slack mrkdwn normalization."""
    return re.sub(r"__(.+?)__", r"_\1_", re.sub(r"\*\*(.+?)\*\*", r"*\1*", text))
