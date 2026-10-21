"""The user-visible product name is DT Agent in every bundled language."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from ductor_bot.i18n import LANGUAGES

_ROOT = Path(__file__).resolve().parents[2] / "ductor_bot"


@pytest.mark.parametrize("language", LANGUAGES)
def test_chat_strings_use_new_name_and_no_old_brand(language: str) -> None:
    text = (_ROOT / "i18n" / language / "chat.toml").read_text(encoding="utf-8")
    data = tomllib.loads(text)
    assert "DT Agent" in data["welcome"]["header"]
    assert data["info"]["header"] == "**DT Agent**"
    assert "ductor.dev" not in text.lower()


def test_banner_is_dt_agent_ascii_art_without_old_name() -> None:
    lines = (_ROOT / "_banner.txt").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 6
    assert max(len(line) for line in lines) <= 99
    assert len({len(line) for line in lines}) == 1  # rectangular like the old banner
    # Eight glyph columns "DT AGENT": the third row starts with the D block.
    assert lines[2].startswith("██║  ██║")
