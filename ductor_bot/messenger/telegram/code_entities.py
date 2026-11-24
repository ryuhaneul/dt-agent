"""Translate Telegram code entities into display Markdown."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from aiogram.types import MessageEntity


def _wrap_code_entity(content: str, entity: MessageEntity) -> str:
    fence_size = 1 + max(map(len, re.findall(r"`+", content)), default=0)
    if entity.type == "pre":
        fence = "`" * max(3, fence_size)
        language = entity.language or ""
        if not re.fullmatch(r"[A-Za-z0-9_+.#-]{1,32}", language):
            language = ""
        ending = "" if content.endswith("\n") else "\n"
        return f"\n\n{fence}{language}\n{content}{ending}{fence}\n\n"

    fence = "`" * fence_size
    if (
        content.startswith("`")
        or content.endswith("`")
        or (content.startswith(" ") and content.endswith(" ") and content.strip(" "))
    ):
        content = f" {content} "
    return f"{fence}{content}{fence}"


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
        if not 0 <= start < end <= len(raw):
            continue
        if parts and start <= cursor:
            return None
        try:
            before = raw[cursor:start].decode("utf-16-le")
            content = raw[start:end].decode("utf-16-le")
        except UnicodeDecodeError:
            continue
        if "`" in before or before.endswith("\\"):
            return None
        if entity.type == "code" and ("\n" in content or "\r" in content):
            return None
        parts.extend((before, _wrap_code_entity(content, entity)))
        cursor = end
    after = raw[cursor:].decode("utf-16-le")
    if not parts or "`" in after:
        return None
    parts.append(after)
    return "".join(parts)
