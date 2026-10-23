"""Localized usage text and refresh buttons, independent of command handlers."""

from __future__ import annotations

import time
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING

from ductor_bot.config import AgentConfig, resolve_user_timezone
from ductor_bot.i18n import t
from ductor_bot.orchestrator.selectors.models import Button, ButtonGrid

if TYPE_CHECKING:
    from zoneinfo import ZoneInfo

    from ductor_bot.cli.claude_usage import SlotUsage
    from ductor_bot.cli.codex_usage import CodexUsage


_WINDOW_LABELS = {"five_hour": "5h", "seven_day": "7d"}


def _half_up(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_HALF_UP))


def usage_bar(utilization: float) -> str:
    """Render ten cells using half-up rounding, clipping only the bar."""
    filled = max(0, min(10, _half_up(Decimal(str(utilization)) * 10)))
    return "█" * filled + "░" * (10 - filled)


def window_label(minutes: int | None) -> str:
    """Format the server's duration without assuming a fixed pair of windows."""
    if minutes is None:
        return "?"
    if minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def _clock(epoch: float, now: float, timezone: ZoneInfo) -> str:
    try:
        value = datetime.fromtimestamp(epoch, timezone)
        today = datetime.fromtimestamp(now, timezone).date()
        return value.strftime("%H:%M" if value.date() == today else "%m/%d %H:%M")
    except (ValueError, OverflowError, OSError):
        return "?"


def _window_text(  # noqa: PLR0913
    label: str,
    utilization: float,
    reset: int | None,
    *,
    now: float,
    timezone: ZoneInfo,
    compact: bool = False,
) -> str:
    prefix = label if compact else f"{label:>3}"
    if reset is not None and now >= reset:
        return f"{prefix} " + t("usage.reset", time=_clock(reset, now, timezone))
    percent = _half_up(Decimal(str(utilization)) * 100)
    if compact:
        suffix = f" · {_clock(reset, now, timezone)}" if reset is not None else ""
        return f"{prefix} {percent}%{suffix}"
    suffix = f"  {_clock(reset, now, timezone)}" if reset is not None else ""
    return f"{prefix} {usage_bar(utilization)} {percent:>3}%{suffix}"


def render_account_usage(
    usage: SlotUsage | None,
    config: AgentConfig,
    *,
    now: float | None = None,
) -> str:
    """Build the one-line Claude slot summary, without a bar or indentation."""
    if usage is None or not usage.windows:
        return t("usage.account_empty")
    current = time.time() if now is None else now
    timezone = resolve_user_timezone(config.user_timezone)
    parts = [
        _window_text(
            label,
            window.utilization,
            window.resets_at,
            now=current,
            timezone=timezone,
            compact=True,
        )
        for name, label in _WINDOW_LABELS.items()
        if (window := usage.windows.get(name)) is not None
    ]
    captured = t("usage.captured", time=_clock(usage.captured_at, current, timezone))
    return f"{' / '.join(parts)} ({captured})"


def render_usage(
    config: AgentConfig,
    slots: dict[str, SlotUsage],
    codex: CodexUsage | None,
    *,
    now: float | None = None,
) -> str:
    """Build only the fixed-width block's body; callers choose transport wrapping."""
    current = time.time() if now is None else now
    timezone = resolve_user_timezone(config.user_timezone)
    lines = [t("usage.title", time=_clock(current, current, timezone))]
    for name in config.claude_accounts or ["claude"]:
        usage = slots.get(name)
        title = f"Claude · {name}"
        if usage is not None:
            limited = usage.windows.get(usage.rate_limit_type or "")
            if usage.status == "rejected" and limited is not None and current < limited.resets_at:
                title += " ⛔"
            title += " · " + t("usage.captured", time=_clock(usage.captured_at, current, timezone))
        lines.append(title)
        if usage is None or not usage.windows:
            lines.append(t("usage.empty"))
            continue
        lines.extend(
            _window_text(
                label, window.utilization, window.resets_at, now=current, timezone=timezone
            )
            for key, label in _WINDOW_LABELS.items()
            if (window := usage.windows.get(key)) is not None
        )
    if codex is None:
        lines.append("Codex  " + t("usage.loading"))
    elif codex.error is not None:
        lines.append("Codex  " + t("usage.failed", reason=t(f"usage.{codex.error}")))
    else:
        title = "Codex"
        if codex.captured_at is not None:
            title += " · " + t("usage.queried", time=_clock(codex.captured_at, current, timezone))
        lines.append(title)
        lines.extend(
            _window_text(
                window_label(window.window_minutes),
                window.used_percent / 100,
                window.resets_at,
                now=current,
                timezone=timezone,
            )
            for window in codex.windows
        )
        if not codex.windows:
            lines.append(t("usage.empty"))
    return "\n".join(lines)


def usage_buttons() -> ButtonGrid:
    """Create the refresh callback without registering a transport handler."""
    return ButtonGrid(rows=[[Button(text=t("usage.refresh"), callback_data="usg:refresh")]])
