"""Tests for stop and interrupt command detection."""

from __future__ import annotations

import pytest

from ductor_bot.messenger.telegram.abort import (
    is_abort_all_message,
    is_abort_message,
    is_interrupt_message,
)

BARE_WORDS = [
    "stop",
    "abort",
    "cancel",
    "halt",
    "hold",
    "wait",
    "quit",
    "exit",
    "esc",
    "interrupt",
    "skip",
    "stopp",
    "warte",
    "abbruch",
    "stop all",
    "cancel all",
    "stopp alle",
    "alles stoppen",
]


class TestIsAbortMessage:
    @pytest.mark.parametrize("text", ["/stop", "/STOP", "/stop@ductor_bot", "  /stop  "])
    def test_stop_command(self, text: str) -> None:
        assert is_abort_message(text) is True

    @pytest.mark.parametrize("text", BARE_WORDS)
    def test_bare_word_is_an_ordinary_message(self, text: str) -> None:
        assert is_abort_message(text) is False

    @pytest.mark.parametrize(
        "text", ["", "tell me about dogs", "please stop", "/status", "/stop_all", "!interrupt"]
    )
    def test_other_text_not_abort(self, text: str) -> None:
        assert is_abort_message(text) is False


class TestIsAbortAllMessage:
    @pytest.mark.parametrize(
        "text", ["/stop_all", "/STOP_ALL", "/stop_all@ductor_bot", "  /stop_all  "]
    )
    def test_stop_all_command(self, text: str) -> None:
        assert is_abort_all_message(text) is True

    @pytest.mark.parametrize("text", BARE_WORDS)
    def test_bare_word_is_an_ordinary_message(self, text: str) -> None:
        assert is_abort_all_message(text) is False

    @pytest.mark.parametrize("text", ["", "please stop everything", "/stop", "/status"])
    def test_other_text_not_abort_all(self, text: str) -> None:
        assert is_abort_all_message(text) is False


class TestIsInterruptMessage:
    @pytest.mark.parametrize(
        "text", ["/interrupt", "/INTERRUPT", "!interrupt", "/interrupt@ductor_bot"]
    )
    def test_interrupt_command(self, text: str) -> None:
        assert is_interrupt_message(text) is True

    @pytest.mark.parametrize("text", BARE_WORDS)
    def test_bare_word_is_an_ordinary_message(self, text: str) -> None:
        assert is_interrupt_message(text) is False

    @pytest.mark.parametrize("text", ["", "hello", "/stop", "/stop_all"])
    def test_other_text_not_interrupt(self, text: str) -> None:
        assert is_interrupt_message(text) is False
