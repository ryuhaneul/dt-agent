"""Tests for format_technical_footer (model and duration only)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from ductor_bot import i18n
from ductor_bot.text.response_format import format_technical_footer


@pytest.fixture
def korean() -> Iterator[None]:
    i18n.init("ko")
    yield
    i18n.init("en")


class TestFormatTechnicalFooter:
    def test_model_and_time(self) -> None:
        result = format_technical_footer("opus", 5000.0)
        assert result.startswith("\n---\n")
        assert "Model: opus" in result
        assert "Time: 5.0s" in result

    def test_no_duration(self) -> None:
        result = format_technical_footer("haiku", None)
        assert "Time" not in result
        assert "Model: haiku" in result

    def test_no_token_or_cost_figures(self) -> None:
        result = format_technical_footer("opus", 1000.0)
        assert "Token" not in result
        assert "Cost" not in result
        assert "$" not in result

    def test_pipe_separator(self) -> None:
        result = format_technical_footer("opus", 1000.0)
        assert result == "\n---\nModel: opus | Time: 1.0s"

    def test_korean_has_model_and_time_only(self, korean: None) -> None:
        result = format_technical_footer("opus", 2500.0)
        assert result.count(" | ") == 1
        assert "opus" in result
        assert "2.5" in result
        assert "$" not in result
        assert not any(ch.isdigit() for ch in result.replace("2.5", "").replace("opus", ""))
