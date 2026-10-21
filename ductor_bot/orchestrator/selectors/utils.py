"""Shared utilities for interactive selector widgets."""

from __future__ import annotations

from ductor_bot.text.response_format import format_short_duration


def format_age(seconds: float) -> str:
    """Format elapsed seconds as a compact text in the configured language (45s / 2m / 3h / 1d)."""
    return format_short_duration(seconds)
