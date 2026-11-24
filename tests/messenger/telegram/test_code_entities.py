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
    ("prefix", "suffix"),
    [("literal`", ""), ("", "` literal"), ("```", "```"), ("literal\\`", ""), ("literal\\", "")],
    ids=["before", "after", "both-triples", "escaped-prefix", "backslash-before"],
)
@pytest.mark.parametrize("kind", ["code", "pre"])
def test_literal_backticks_at_code_boundaries(prefix: str, suffix: str, kind: str) -> None:
    entity = MessageEntity(type=kind, offset=len(prefix), length=5)
    assert code_entities_to_markdown(f"{prefix}value{suffix}", [entity]) is None


def test_indented_backtick_before_pre() -> None:
    text = "Before\n\n    `value"
    entity = MessageEntity(type="pre", offset=13, length=5)
    assert code_entities_to_markdown(text, [entity]) is None


def test_boundary_disables_display_markdown_for_entire_message() -> None:
    entities = [
        MessageEntity(type="code", offset=0, length=4),
        MessageEntity(type="code", offset=6, length=5),
    ]
    assert code_entities_to_markdown("safe `value", entities) is None


def test_touching_entities_do_not_treat_code_content_as_literal() -> None:
    entities = [
        MessageEntity(type="pre", offset=0, length=5),
        MessageEntity(type="code", offset=5, length=6),
    ]
    assert code_entities_to_markdown("left``right", entities) == (
        "\n\n```\nleft`\n```\n\n`` `right ``"
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
