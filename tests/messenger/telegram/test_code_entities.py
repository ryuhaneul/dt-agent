"""Tests for Telegram code entity conversion."""

from __future__ import annotations

import pytest
from aiogram.types import MessageEntity

from ductor_bot.messenger.telegram.code_entities import code_entities_to_markdown


@pytest.mark.parametrize(
    ("language", "expected"),
    [("c++", "c++"), (None, ""), ("sh another", ""), ("x" * 33, "")],
)
def test_pre_language(language: str | None, expected: str) -> None:
    text = "before echo hi after"
    entity = MessageEntity(type="pre", offset=7, length=7, language=language)
    assert code_entities_to_markdown(text, [entity]) == (
        f"before \n\n```{expected}\necho hi\n```\n\n after"
    )


def test_pre_with_triple_backticks() -> None:
    text = "before\n```\nafter\n"
    entity = MessageEntity(type="pre", offset=0, length=len(text))
    assert code_entities_to_markdown(text, [entity]) == f"\n\n````\n{text}````\n\n"


@pytest.mark.parametrize(
    ("text", "wrapped"),
    [
        ("ls", "`ls`"),
        ("a`b", "``a`b``"),
        ("`abc``", "``` `abc`` ```"),
        ("  a`b ", "``   a`b  ``"),
        ("   ", "`   `"),
    ],
)
def test_inline_code(text: str, wrapped: str) -> None:
    entity = MessageEntity(type="code", offset=0, length=len(text))
    assert code_entities_to_markdown(text, [entity]) == wrapped


@pytest.mark.parametrize(
    ("prefix", "suffix", "escaped"),
    [
        ("literal`", "", ("literal&#96;", "")),
        ("", "` literal", ("", "&#96; literal")),
        ("```", "```", ("&#96;" * 3, "&#96;" * 3)),
        ("literal\\`", "", ("literal&#92;&#96;", "")),
    ],
    ids=["before", "after", "both-triples", "escaped-prefix"],
)
@pytest.mark.parametrize(
    ("kind", "wrapped"),
    [("code", "`value`"), ("pre", "\n\n```\nvalue\n```\n\n")],
)
def test_literal_backticks_at_code_boundaries(
    prefix: str, suffix: str, escaped: tuple[str, str], kind: str, wrapped: str
) -> None:
    escaped_prefix, escaped_suffix = escaped
    entity = MessageEntity(type=kind, offset=len(prefix), length=len("value"))
    assert code_entities_to_markdown(f"{prefix}value{suffix}", [entity]) == (
        f"{escaped_prefix}{wrapped}{escaped_suffix}"
    )


def test_utf16_offsets_and_unmodified_other_entities() -> None:
    emoji = chr(0x1F642)
    text = f"raw {emoji}a{emoji}b{emoji}z"
    entities = [
        MessageEntity(type="bold", offset=0, length=3),
        MessageEntity(type="code", offset=12, length=1),
        MessageEntity(type="code", offset=6, length=4),
    ]
    assert code_entities_to_markdown(text, entities) == f"raw {emoji}`a{emoji}b`{emoji}`z`"


def test_no_code_entities() -> None:
    assert code_entities_to_markdown("plain", None) is None
    assert (
        code_entities_to_markdown("plain", [MessageEntity(type="bold", offset=0, length=5)]) is None
    )
