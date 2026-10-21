"""Abort and interrupt command detection for the Telegram bot.

Only commands stop work: ``/stop``, ``/stop_all`` and ``/interrupt``, each also with an
``@botname`` suffix. A bare word such as "stop" or "esc" is an ordinary message.
"""

from __future__ import annotations


def _command(text: str) -> str:
    stripped = text.strip()
    return stripped.lower().split(None, 1)[0] if stripped else ""


def is_abort_message(text: str) -> bool:
    """Return *True* if *text* is the ``/stop`` command (``/stop@bot`` included)."""
    command = _command(text)
    return command == "/stop" or command.startswith("/stop@")


def is_abort_all_message(text: str) -> bool:
    """Return *True* if *text* is the ``/stop_all`` command (``/stop_all@bot`` included)."""
    command = _command(text)
    return command == "/stop_all" or command.startswith("/stop_all@")


def is_interrupt_message(text: str) -> bool:
    """Return *True* if *text* is the ``/interrupt`` command (also ``!interrupt``)."""
    command = _command(text)
    return command in ("/interrupt", "!interrupt") or command.startswith("/interrupt@")
