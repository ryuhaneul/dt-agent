"""Translate Telegram code entities into display Markdown."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from aiogram.types import MessageEntity


def _escape_leading_backticks(text: str) -> str:
    rest = text.lstrip("`")
    return "&#96;" * (len(text) - len(rest)) + rest


def _escape_trailing_backticks(text: str) -> str:
    prefix = text.rstrip("`")
    if len(prefix) == len(text):
        return text
    # Encode preceding backslashes so they cannot escape the first reference.
    boundary = len(prefix.rstrip("\\"))
    return text[:boundary] + text[boundary:].replace("\\", "&#92;").replace("`", "&#96;")


def code_entities_to_markdown(text: str, entities: Sequence[MessageEntity] | None) -> str | None:
    """Wrap only code entities, using Telegram's UTF-16 offsets."""
    spans = sorted(
        (entity for entity in entities or () if entity.type in {"pre", "code"}),
        key=lambda entity: entity.offset,
    )
    if not spans:
        return None

    raw = text.encode("utf-16-le")
    parts: list[str] = []
    cursor = 0
    for entity in spans:
        start = entity.offset * 2
        end = start + entity.length * 2
        if not cursor <= start < end <= len(raw):
            continue
        try:
            before = raw[cursor:start].decode("utf-16-le")
            content = raw[start:end].decode("utf-16-le")
        except UnicodeDecodeError:
            continue
        fence_size = 1 + max(map(len, re.findall(r"`+", content)), default=0)
        if entity.type == "pre":
            fence = "`" * max(3, fence_size)
            language = entity.language or ""
            if not re.fullmatch(r"[A-Za-z0-9_+.#-]{1,32}", language):
                language = ""
            ending = "" if content.endswith("\n") else "\n"
            wrapped = f"\n\n{fence}{language}\n{content}{ending}{fence}\n\n"
        else:
            fence = "`" * fence_size
            if (
                content.startswith("`")
                or content.endswith("`")
                or (content.startswith(" ") and content.endswith(" ") and content.strip(" "))
            ):
                content = f" {content} "
            wrapped = f"{fence}{content}{fence}"
        if parts:
            before = _escape_leading_backticks(before)
        parts.extend((_escape_trailing_backticks(before), wrapped))
        cursor = end
    if not parts:
        return None
    parts.append(_escape_leading_backticks(raw[cursor:].decode("utf-16-le")))
    return "".join(parts)
