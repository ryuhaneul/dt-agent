"""Exact localized account usage views and reset boundaries."""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from zoneinfo import ZoneInfo

import pytest

from ductor_bot.config import AgentConfig
from ductor_bot.i18n import init, t
from ductor_bot.cli.claude_usage import SlotUsage, UsageWindow
from ductor_bot.cli.codex_usage import CodexUsage, CodexWindow
from ductor_bot.orchestrator.selectors.usage_view import (
    render_usage,
    render_account_usage,
    usage_buttons,
    usage_bar,
    window_label,
)


def stamp(day, hour, minute, timezone="Asia/Seoul"):
    return int(datetime(2026, 10, day, hour, minute, tzinfo=ZoneInfo(timezone)).timestamp())


@pytest.fixture(autouse=True)
def language():
    init("ko")
    yield
    init("en")


def sample():
    slots = {}
    for name, status, captured, five, seven, reset, day, hour in [
        ("claude-sub", "rejected", 20, 1.0, 0.36, (13, 40), 27, 4),
        ("claude-main", "allowed", 25, 0.11, 0.02, (14, 30), 28, 6),
    ]:
        slots[name] = SlotUsage(
            status=status,
            rate_limit_type="five_hour",
            captured_at=stamp(21, 13, captured),
            windows={
                "five_hour": UsageWindow(
                    utilization=five,
                    resets_at=stamp(21, *reset),
                    captured_at=stamp(21, 13, captured),
                ),
                "seven_day": UsageWindow(
                    utilization=seven,
                    resets_at=stamp(day, hour, 0),
                    captured_at=stamp(21, 13, captured),
                ),
            },
        )
    codex = CodexUsage(
        windows=(
            CodexWindow(used_percent=23, window_minutes=300, resets_at=stamp(21, 16, 10)),
            CodexWindow(used_percent=41, window_minutes=10080, resets_at=stamp(25, 9, 0)),
        ),
        captured_at=stamp(21, 13, 30),
    )
    config = AgentConfig(claude_accounts=["claude-sub", "claude-main"], user_timezone="Asia/Seoul")
    return config, slots, codex


# SHA-256 of the design-approved Korean bodies, kept free of locale text.
GOLDEN_SHA256 = "4866cca7f41e491514fba7330b1171af607eefe2f24d8d84da584616af82d5cb"
AFTER_SHA256 = "b1419f5c1b45989e71a4b8ba9e23f2eed5e0b22439aa8f363d03ebb237adca1f"


def test_design_examples_exactly():
    config, slots, codex = sample()
    assert (
        sha256(render_usage(config, slots, codex, now=stamp(21, 13, 30)).encode()).hexdigest()
        == GOLDEN_SHA256
    )
    assert (
        sha256(render_usage(config, slots, codex, now=stamp(21, 14, 5)).encode()).hexdigest()
        == AFTER_SHA256
    )
    assert render_account_usage(slots["claude-main"], config, now=stamp(21, 13, 30)) == (
        "5h 11% · 14:30 / 7d 2% · 10/28 06:00 (" + t("usage.captured", time="13:25") + ")"
    )
    assert render_account_usage(slots["claude-sub"], config, now=stamp(21, 14, 5)) == (
        "5h "
        + t("usage.reset", time="13:40")
        + " / 7d 36% · 10/27 04:00 ("
        + t("usage.captured", time="13:20")
        + ")"
    )


@pytest.mark.parametrize(
    ("value", "filled"), [(0.04, 0), (0.05, 1), (0.95, 10), (1.0, 10), (1.2, 10)]
)
def test_half_up_bars(value, filled):
    assert usage_bar(value) == "█" * filled + "░" * (10 - filled)


def test_half_up_percentage_and_over_limit():
    config, slots, codex = sample()
    slots["claude-main"].windows["five_hour"].utilization = 0.005
    text = render_usage(config, slots, codex, now=stamp(21, 13, 30))
    assert "   1%  14:30" in text
    slots["claude-main"].windows["five_hour"].utilization = 1.2
    assert "██████████ 120%" in render_usage(config, slots, codex, now=stamp(21, 13, 30))


@pytest.mark.parametrize(
    ("minutes", "label"),
    [(300, "5h"), (10080, "7d"), (2880, "2d"), (120, "2h"), (45, "45m"), (None, "?")],
)
def test_duration_labels(minutes, label):
    assert window_label(minutes) == label


@pytest.mark.parametrize("error", ["timeout", "login_required", "invalid_response"])
def test_failure_messages(error):
    config, slots, _ = sample()
    text = render_usage(config, slots, CodexUsage(error=error), now=stamp(21, 13, 30))
    assert "Codex  " + t("usage.failed", reason=t("usage." + error)) in text


def test_no_values_default_slot_and_refresh():
    config = AgentConfig(user_timezone="Asia/Seoul")
    text = render_usage(config, {}, CodexUsage(), now=stamp(21, 13, 30))
    assert ("Claude · claude\n" + t("usage.empty")) in text
    assert render_account_usage(None, config, now=stamp(21, 13, 30)) == (t("usage.account_empty"))
    button = usage_buttons().rows[0][0]
    assert button.text == (t("usage.refresh")) and button.callback_data == "usg:refresh"
    assert len(button.callback_data.encode()) <= 64


def test_timezone_date_rollover_and_rejection_window():
    config, slots, codex = sample()
    config.user_timezone = "America/New_York"
    slots["claude-sub"].rate_limit_type = "seven_day"
    text = render_usage(config, slots, codex, now=stamp(22, 0, 5))
    assert text.startswith((t("usage.title", time="11:05")))
    assert "claude-sub ⛔" in text
    assert (t("usage.captured", time="13:20")) not in text
    config.user_timezone = "Asia/Seoul"
    text = render_usage(config, slots, codex, now=stamp(22, 0, 5))
    assert (t("usage.captured", time="10/21 13:20")) in text
    assert (t("usage.reset", time="10/21 13:40")) in text
    slots["claude-sub"].status = "allowed"
    assert "⛔" not in render_usage(config, slots, codex, now=stamp(22, 0, 5))


def test_null_reset_omits_time_and_trailing_space():
    config, slots, _ = sample()
    codex = CodexUsage(
        windows=(CodexWindow(used_percent=16, window_minutes=10080, resets_at=None),),
        captured_at=stamp(21, 13, 30),
    )
    text = render_usage(config, slots, codex, now=stamp(21, 13, 30))
    assert text.splitlines()[-2] == " 7d ██░░░░░░░░  16%"
    assert all(line == line.rstrip() for line in text.splitlines())


def test_reset_boundary_and_unknown_rejection_window():
    config, slots, codex = sample()
    text = render_usage(config, slots, codex, now=stamp(21, 13, 40))
    assert "claude-sub ⛔" not in text
    assert " 5h " + t("usage.reset", time="13:40") in text
    slots["claude-sub"].rate_limit_type = None
    assert "⛔" not in render_usage(config, slots, codex, now=stamp(21, 13, 30))
    assert render_account_usage(None, config, now=stamp(21, 13, 30)) == t("usage.account_empty")


def test_percentage_half_up_at_two_and_a_half():
    config, slots, codex = sample()
    slots["claude-main"].windows["five_hour"].utilization = 0.025
    assert "   3%  14:30" in render_usage(config, slots, codex, now=stamp(21, 13, 30))



def test_unrepresentable_reset_keeps_values_and_marks_time_unknown():
    config, slots, codex = sample()
    slots["claude-main"].windows["five_hour"].resets_at = 10**13
    codex.windows[0].resets_at = 10**13
    text = render_usage(config, slots, codex, now=stamp(21, 13, 30))
    assert " 5h █░░░░░░░░░  11%  ?" in text
    assert " 5h ██░░░░░░░░  23%  ?" in text
    assert "5h 11% · ?" in render_account_usage(slots["claude-main"], config, now=stamp(21, 13, 30))
