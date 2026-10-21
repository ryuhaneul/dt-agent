"""Tests for the Claude Code multi-slot account registry."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from ductor_bot.cli.claude_accounts import (
    DEFAULT_SLOT,
    SlotRegistry,
    SlotSnapshot,
    container_slot_config_dir,
    detect_rate_limit,
    host_slot_config_dir,
    parse_reset_time,
    pick_slot,
    resolve_ductor_home,
    slot_is_authenticated,
    state_file_path,
)


class TestResolveDuctorHome:
    def test_workspace_leaf(self, tmp_path: Path) -> None:
        ws = tmp_path / "workspace"
        ws.mkdir()
        assert resolve_ductor_home(str(ws)) == tmp_path

    def test_non_workspace_leaf(self, tmp_path: Path) -> None:
        assert resolve_ductor_home(str(tmp_path)) == tmp_path


class TestSlotPaths:
    def test_host_slot_default(self, tmp_path: Path) -> None:
        assert host_slot_config_dir(tmp_path, "claude") == tmp_path / ".cli-auth" / "claude"

    def test_host_slot_alt(self, tmp_path: Path) -> None:
        assert host_slot_config_dir(tmp_path, "claude-alt") == tmp_path / ".cli-auth" / "claude-alt"

    def test_container_slot_default(self) -> None:
        assert container_slot_config_dir("claude") == "/ductor/.cli-auth/claude"

    def test_container_slot_alt(self) -> None:
        assert container_slot_config_dir("claude-work") == "/ductor/.cli-auth/claude-work"

    def test_state_file_location(self, tmp_path: Path) -> None:
        assert state_file_path(tmp_path) == tmp_path / ".cli-auth" / "_claude-slots.json"


class TestDetectRateLimit:
    def test_zero_returncode_never_matches(self) -> None:
        assert (
            detect_rate_limit(
                returncode=0,
                is_error=False,
                stderr="rate limit exhausted",
            )
            is False
        )

    def test_none_returncode_never_matches(self) -> None:
        # Treat None (timeout) as non-rate-limit
        assert (
            detect_rate_limit(
                returncode=None,
                is_error=True,
                stderr="quota exhausted",
            )
            is False
        )

    def test_rate_keyword_in_stderr(self) -> None:
        assert detect_rate_limit(
            returncode=1,
            is_error=True,
            stderr="Error: rate limit reached, resets 3pm",
        )

    def test_429_token(self) -> None:
        assert detect_rate_limit(
            returncode=2,
            is_error=True,
            stderr="HTTP 429 Too Many Requests",
        )

    def test_quota_token(self) -> None:
        assert detect_rate_limit(
            returncode=1,
            is_error=True,
            stderr="",
            result="quota exhausted, resets 15:00",
        )

    def test_benign_stderr_not_matched(self) -> None:
        assert (
            detect_rate_limit(
                returncode=1,
                is_error=True,
                stderr="Error: network unreachable",
            )
            is False
        )

    def test_empty_inputs_not_matched(self) -> None:
        assert detect_rate_limit(returncode=1, is_error=True, stderr="", result="") is False


class TestParseResetTime:
    def _now(self) -> datetime:
        # Fixed "now" at 2026-04-17 10:00 UTC for deterministic assertions.
        return datetime(2026, 4, 17, 10, 0, tzinfo=ZoneInfo("UTC"))

    def test_ampm_pm(self) -> None:
        now = self._now()
        reset = parse_reset_time("resets 3pm", now=now, default_tz=ZoneInfo("UTC"))
        assert reset is not None
        assert reset.hour == 15
        assert reset.minute == 0

    def test_ampm_am_next_day(self) -> None:
        now = self._now()  # 10:00 UTC
        reset = parse_reset_time("resets 9am", now=now, default_tz=ZoneInfo("UTC"))
        # 9am is before 10am — must roll to the next day
        assert reset is not None
        assert reset.hour == 9
        assert reset - now == timedelta(hours=23)

    def test_hhmm_at(self) -> None:
        now = self._now()
        reset = parse_reset_time(
            "resets at 15:30",
            now=now,
            default_tz=ZoneInfo("UTC"),
        )
        assert reset is not None
        assert reset.hour == 15
        assert reset.minute == 30

    def test_hhmm_bare(self) -> None:
        now = self._now()
        reset = parse_reset_time(
            "your quota resets 18:00",
            now=now,
            default_tz=ZoneInfo("UTC"),
        )
        assert reset is not None
        assert reset.hour == 18
        assert reset.minute == 0

    def test_hhmm_with_tz(self) -> None:
        now = self._now()
        reset = parse_reset_time(
            "resets 15:00 (America/Los_Angeles)",
            now=now,
            default_tz=ZoneInfo("UTC"),
        )
        assert reset is not None
        assert str(reset.tzinfo) == "America/Los_Angeles"
        assert reset.hour == 15
        assert reset.minute == 0

    def test_unknown_tz_falls_back_to_default(self) -> None:
        now = self._now()
        reset = parse_reset_time(
            "resets 15:00 (Atlantis/Capital)",
            now=now,
            default_tz=ZoneInfo("UTC"),
        )
        assert reset is not None
        assert str(reset.tzinfo) == "UTC"

    def test_twelve_am_is_midnight(self) -> None:
        now = self._now()
        reset = parse_reset_time("resets 12am", now=now, default_tz=ZoneInfo("UTC"))
        assert reset is not None
        assert reset.hour == 0

    def test_twelve_pm_is_noon(self) -> None:
        now = self._now()
        reset = parse_reset_time("resets 12pm", now=now, default_tz=ZoneInfo("UTC"))
        assert reset is not None
        assert reset.hour == 12

    def test_no_match_returns_none(self) -> None:
        now = self._now()
        assert parse_reset_time("no reset info here", now=now, default_tz=ZoneInfo("UTC")) is None

    def test_hour_out_of_range_returns_none(self) -> None:
        now = self._now()
        # "resets 99:99" matches the HH:MM pattern but both fields are out of
        # range — must not be silently clamped to "now".
        assert parse_reset_time("resets 99:99", now=now, default_tz=ZoneInfo("UTC")) is None

    def test_hour_out_of_range_with_at_returns_none(self) -> None:
        now = self._now()
        assert parse_reset_time("resets at 25:00", now=now, default_tz=ZoneInfo("UTC")) is None

    def test_minute_out_of_range_with_tz_returns_none(self) -> None:
        now = self._now()
        assert parse_reset_time("resets 12:99 (UTC)", now=now, default_tz=ZoneInfo("UTC")) is None


class TestPickSlot:
    def test_first_expired_slot_picked(self) -> None:
        now = 1000.0
        cooldowns = {"claude": 2000.0}  # still cooling down
        assert pick_slot(["claude", "claude-alt"], cooldowns, now=now) == "claude-alt"

    def test_all_expired_picks_first(self) -> None:
        now = 3000.0
        cooldowns = {"claude": 1000.0, "claude-alt": 2000.0}
        assert pick_slot(["claude", "claude-alt"], cooldowns, now=now) == "claude"

    def test_all_cooled_returns_none(self) -> None:
        now = 500.0
        cooldowns = {"claude": 1000.0, "claude-alt": 2000.0}
        assert pick_slot(["claude", "claude-alt"], cooldowns, now=now) is None

    def test_unknown_slot_considered_expired(self) -> None:
        # A slot with no recorded cooldown is considered available.
        assert pick_slot(["never-seen"], {}, now=1.0) == "never-seen"


class TestSlotRegistry:
    def test_load_missing_file(self, tmp_path: Path) -> None:
        reg = SlotRegistry(tmp_path / "state.json")
        snap = reg.load()
        assert snap.cooldowns == {}
        assert snap.last_used is None

    def test_save_then_load_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        reg = SlotRegistry(path)
        reg.save(SlotSnapshot(cooldowns={"claude": 123.45}, last_used="claude"))

        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["cooldowns"] == {"claude": 123.45}
        assert raw["last_used"] == "claude"

        snap = reg.load()
        assert snap.cooldowns == {"claude": 123.45}
        assert snap.last_used == "claude"

    def test_save_creates_parent_dir(self, tmp_path: Path) -> None:
        nested = tmp_path / ".cli-auth" / "_claude-slots.json"
        reg = SlotRegistry(nested)
        reg.save(SlotSnapshot(cooldowns={"claude": 1.0}))
        assert nested.is_file()

    def test_load_corrupted_file_returns_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        path.write_text("not json at all", encoding="utf-8")
        reg = SlotRegistry(path)
        snap = reg.load()
        assert snap.cooldowns == {}

    def test_load_ignores_non_numeric_cooldowns(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        path.write_text(
            json.dumps({"cooldowns": {"claude": "tomorrow", "alt": 99.0}}),
            encoding="utf-8",
        )
        reg = SlotRegistry(path)
        snap = reg.load()
        assert snap.cooldowns == {"alt": 99.0}

    def test_atomic_save_uses_tmp(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        reg = SlotRegistry(path)
        reg.save(SlotSnapshot(cooldowns={"claude": 1.0}))
        # tmp file should be gone after atomic replace
        assert not (tmp_path / "state.json.tmp").exists()
        assert path.is_file()


class TestSlotIsAuthenticated:
    def test_missing_dir(self, tmp_path: Path) -> None:
        assert slot_is_authenticated(tmp_path / "nope") is False

    def test_empty_dir(self, tmp_path: Path) -> None:
        (tmp_path / "claude").mkdir()
        assert slot_is_authenticated(tmp_path / "claude") is False

    def test_credentials_present(self, tmp_path: Path) -> None:
        d = tmp_path / "claude"
        d.mkdir()
        (d / "credentials.json").write_text("{}")
        assert slot_is_authenticated(d) is True

    def test_hidden_credentials_present(self, tmp_path: Path) -> None:
        d = tmp_path / "claude-alt"
        d.mkdir()
        (d / ".credentials.json").write_text("{}")
        assert slot_is_authenticated(d) is True


def test_default_slot_constant() -> None:
    assert DEFAULT_SLOT == "claude"


def test_pick_slot_uses_time_time_when_now_none() -> None:
    # Sanity — if no `now` is passed, it uses the current wall clock.
    pick = pick_slot(["claude"], {"claude": time.time() + 100})
    assert pick is None


@pytest.mark.parametrize("text", ["generate", "separate", "delimiter", "warning: no rate sampling"])
@pytest.mark.parametrize("is_error", [False, True])
def test_rate_limit_rejects_unrelated_words(text: str, is_error: bool) -> None:
    assert not detect_rate_limit(returncode=1, is_error=is_error, stderr=text, result=text)


@pytest.mark.parametrize("text", ["rate limit exceeded", "429 Too Many Requests", "usage limit reached … resets at 3pm"])
@pytest.mark.parametrize("is_error", [False, True])
def test_rate_limit_matches_actual_messages(text: str, is_error: bool) -> None:
    assert detect_rate_limit(returncode=1, is_error=is_error, stderr=text)


def test_successful_result_text_is_not_a_rate_limit() -> None:
    assert not detect_rate_limit(returncode=1, is_error=False, stderr="", result="rate limit")


def test_home_resolution_expands_tilde(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert resolve_ductor_home("~/agent/workspace") == tmp_path / "agent"



@pytest.mark.parametrize("is_error", [False, True])
@pytest.mark.parametrize(
    "text",
    [
        "Connection reset by peer",
        "fatal: git reset failed",
        "git reset --hard failed",
        "Connection reset after 3 retries",
        "fatal: git reset 1234567 failed",
        "Connection reset at 15:30 (UTC)",
    ],
)
def test_reset_transport_is_not_rate_limit(is_error, text):
    assert not detect_rate_limit(returncode=1, is_error=is_error, stderr=text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("You've hit your limit · resets 3pm", True),
        ("Claude usage limit reached", True),
        ("5-hour limit reached · resets 3pm", True),
        ("Error: rate limit reached, resets 3pm", True),
        ("quota exhausted, resets 15:00", True),
        ("429 Too Many Requests", True),
        ("rate limit exceeded", True),
        ("resets 3pm", False),
        ("resets at 15:30", False),
        ("resets 15:00 (America/Los_Angeles)", False),
        ("resets Apr 5, 3pm", False),
        ("resets April 5, 2027, 3pm", False),
        ("resets in 2 hours", False),
    ],
)
def test_reset_phrases_require_limit_context(text, expected):
    assert detect_rate_limit(returncode=1, is_error=True, stderr=text) is expected
    assert not detect_rate_limit(returncode=1, is_error=False, stderr="", result=text)


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize(
    "error",
    [
        "Connection reset after 3 retries",
        "fatal: git reset 1234567 failed",
        "Connection reset at 15:30 (UTC)",
    ],
)
async def test_reset_errors_do_not_cool_or_switch_accounts(tmp_path, monkeypatch, streaming, error):
    from ductor_bot.cli.base import CLIConfig
    from ductor_bot.cli.claude_provider import ClaudeCodeCLI
    from ductor_bot.cli.stream_events import ResultEvent
    from tests.cli.test_claude_provider import _install_slot_cli, _read_slot_calls

    home, control, calls = _install_slot_cli(tmp_path, monkeypatch)
    control.write_text(json.dumps({"limited": ["claude-a"], "error": error}))
    cli = ClaudeCodeCLI(
        CLIConfig(
            provider="claude",
            model="opus",
            ductor_home=home,
            working_dir=str(tmp_path),
            claude_accounts=["claude-a", "claude-b"],
        )
    )
    if streaming:
        events = [event async for event in cli.send_streaming("hello")]
        result = next(event.result for event in events if isinstance(event, ResultEvent))
    else:
        result = (await cli.send("hello")).result
    assert result == error
    assert [Path(row["auth"]).name for row in _read_slot_calls(calls)] == ["claude-a"]
    assert not list(home.rglob("_claude-slots.json"))



@pytest.mark.parametrize("text", ["context window exhausted", "Disk quota exceeded", "quota"])
@pytest.mark.parametrize("is_error", [False, True])
def test_resource_errors_are_not_claude_rate_limits(text, is_error):
    assert not detect_rate_limit(returncode=1, is_error=is_error, stderr=text)


@pytest.mark.parametrize(
    ("text", "expected_time"),
    [
        ("You've hit your session limit · resets 10:40pm (Asia/Seoul)", (22, 40)),
        ("You've hit your session limit · resets 12:40pm (Asia/Seoul)", (12, 40)),
        ("You've hit your session limit · resets 1pm (Asia/Seoul)", (13, 0)),
        ("You've hit your session limit · resets 4am (Asia/Seoul)", (4, 0)),
        ("You've hit your session limit · resets 5:40pm (Asia/Seoul)", (17, 40)),
        ("You've hit your session limit · resets 5:50am (Asia/Seoul)", (5, 50)),
        ("You've hit your session limit · resets 5am (Asia/Seoul)", (5, 0)),
        ("You've hit your session limit · resets 6:30pm (Asia/Seoul)", (18, 30)),
        ("You've hit your session limit · resets 6pm (Asia/Seoul)", (18, 0)),
        (
            "You've reached your Fable 5 limit. Run /usage-credits to continue or switch models with /model.",
            None,
        ),
    ],
)
def test_recorded_account_limits_and_reset_times(text, expected_time):
    from ductor_bot.cli.slot_rotation import parse_reset_from_text

    assert detect_rate_limit(returncode=1, is_error=True, stderr="", result=text)
    reset = parse_reset_from_text(text, ZoneInfo("Asia/Seoul"))
    if expected_time is None:
        assert reset is None
    else:
        assert reset is not None
        assert (reset.hour, reset.minute) == expected_time
        assert reset.tzinfo == ZoneInfo("Asia/Seoul")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "API Error: 500 Internal server error. This is a server-side issue, usually temporary — try again in a moment. If it persists, check https://status.claude.com.",
            False,
        ),
        (
            "API Error: 529 Overloaded. This is a server-side issue, usually temporary — try again in a moment. If it persists, check https://status.claude.com.",
            False,
        ),
        (
            "API Error: Server is temporarily limiting requests (not your usage limit) · Rate limited",
            True,
        ),
        ("API Error: Unable to connect to API (ConnectionRefused)", False),
        ("Failed to authenticate. API Error: 401 Invalid authentication credentials", False),
        (
            "Failed to authenticate. API Error: 401 The socket connection was closed unexpectedly. For more information, pass `verbose: true` in the second argument to fetch()",
            False,
        ),
        ("Failed to authenticate: OAuth session expired and could not be refreshed", False),
        ("Not logged in · Please run /login", False),
        (
            "There's an issue with the selected model (claude-fable-5). It may not exist or you may not have access to it. Run --model to pick a different model.",
            False,
        ),
        (
            "Your organization has disabled Claude subscription access for Claude Code · Use an Anthropic API key instead, or ask your admin to enable access",
            False,
        ),
    ],
)
def test_recorded_other_api_errors(text, expected):
    assert detect_rate_limit(returncode=1, is_error=True, stderr="", result=text) is expected


@pytest.mark.parametrize("text", [
    "You have hit the context limit",
    "hit the token limit",
    "Tool output hit the 30000-character limit",
    "You've reached your context window limit",
    "cli.js:429:1234",
    "hit your context limit",
])
def test_non_usage_limits_do_not_trigger_rotation(text):
    assert not detect_rate_limit(returncode=1, is_error=True, stderr=text)


@pytest.mark.parametrize("text", [
    "You've hit your session limit · resets 3pm",
    "usage limit reached",
    "hit the usage limit",
    "hit the limit",
    "429 Too Many Requests",
])
def test_usage_limits_still_trigger_rotation(text):
    assert detect_rate_limit(returncode=1, is_error=True, stderr=text)



@pytest.mark.parametrize(("text", "expected"), [
    ("Context limit reached", False), ("CONTEXT LIMIT REACHED", False),
    ("limit reached", True), ("Usage limit reached", True),
])
def test_context_limit_is_not_a_usage_limit(text, expected):
    assert detect_rate_limit(returncode=1, is_error=True, stderr=text) is expected
