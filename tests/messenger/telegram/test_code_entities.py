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
    ("prefix", "content", "suffix"),
    [
        ("literal`", "value", ""),
        ("", "value", "` literal"),
        ("` press the key, then run ", "foo", " now"),
        ("a `` b ", "x`y", " end"),
        ("", "value", " with a ` later"),
        ("literal\\", "value", ""),
        ("https://example.com/", "api", " call"),
        ("<https://example.com/", "api", "> call"),
        ('<div class="', "main", '"> edit'),
        ("<!-- ", "TODO", " --> check"),
        ("", "api", "www.example.com call"),
        ("<", "br", "> tag"),
    ],
    ids=[
        "before",
        "after",
        "distant-before",
        "mixed-runs",
        "distant-after",
        "backslash-before",
        "url",
        "autolink",
        "html-tag",
        "html-comment",
        "www-suffix",
        "tag-name",
    ],
)
@pytest.mark.parametrize("kind", ["code", "pre"])
def test_unsafe_punctuation_outside_code(prefix: str, content: str, suffix: str, kind: str) -> None:
    entity = MessageEntity(type=kind, offset=len(prefix), length=len(content))
    assert code_entities_to_markdown(f"{prefix}{content}{suffix}", [entity]) is None


@pytest.mark.parametrize("content", ["https://example.com/api", '<div class="main">'])
def test_full_url_or_html_tag_is_code(content: str) -> None:
    entity = MessageEntity(type="code", offset=0, length=len(content))
    assert code_entities_to_markdown(f"{content} call", [entity]) == f"`{content}` call"


def test_less_than_in_code_block_keeps_later_inline_code() -> None:
    block = "for (i = 0; i < n; i++) { step(); }"
    text = f"{block}\nchange n here"
    entities = [
        MessageEntity(type="pre", offset=0, length=len(block)),
        MessageEntity(type="code", offset=text.index("n here"), length=1),
    ]
    assert code_entities_to_markdown(text, entities) == (
        f"\n\n```\n{block}\n```\n\n\nchange `n` here"
    )


def test_angle_brackets_in_two_inline_code_entities() -> None:
    text = "a < b instead of b > a"
    entities = [
        MessageEntity(type="code", offset=0, length=5),
        MessageEntity(type="code", offset=text.index("b > a"), length=5),
    ]
    assert code_entities_to_markdown(text, entities) == "`a < b` instead of `b > a`"


def test_html_tag_crossing_two_code_entities_falls_back() -> None:
    text = '<div class="a b">'
    entities = [
        MessageEntity(type="code", offset=text.index('"a') + 1, length=1),
        MessageEntity(type="code", offset=text.index('b"'), length=1),
    ]
    assert code_entities_to_markdown(text, entities) is None


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


@pytest.mark.parametrize("second_offset", [2, 1], ids=["touching", "overlapping"])
def test_code_entities_without_gap(second_offset: int) -> None:
    entities = [
        MessageEntity(type="code", offset=0, length=2),
        MessageEntity(type="code", offset=second_offset, length=2),
    ]
    assert code_entities_to_markdown("aabb", entities) is None


@pytest.mark.parametrize("line_break", ["\n\n", "\n", "\r"], ids=["blank-line", "lf", "cr"])
def test_multiline_inline_code(line_break: str) -> None:
    text = f"a = 1{line_break}b = 2"
    entity = MessageEntity(type="code", offset=0, length=len(text))
    assert code_entities_to_markdown(text, [entity]) is None


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
