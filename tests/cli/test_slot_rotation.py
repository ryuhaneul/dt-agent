"""Tests for the shared Claude slot-rotation driver."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from ductor_bot.cli.claude_accounts import SlotRegistry, SlotSnapshot
from ductor_bot.cli.slot_rotation import (
    AttemptOutcome,
    RotationResult,
    build_fallback_prefix,
    build_unparsable_prefix,
    run_with_rotation,
    slot_extra_env,
)
from ductor_bot.i18n import init


@dataclass
class _Payload:
    """Opaque payload the driver just shuttles through to the caller."""

    tag: str


def _ok(tag: str = "ok") -> tuple[AttemptOutcome, _Payload]:
    return (
        AttemptOutcome(returncode=0, is_error=False, stderr="", result_text="hello"),
        _Payload(tag=tag),
    )


def _rl(reset_text: str = "resets 3pm", tag: str = "rl") -> tuple[AttemptOutcome, _Payload]:
    return (
        AttemptOutcome(
            returncode=1,
            is_error=True,
            stderr=f"Error: Claude rate limit reached, {reset_text}",
            result_text="",
        ),
        _Payload(tag=tag),
    )


def _rl_unparsable(tag: str = "rl_unp") -> tuple[AttemptOutcome, _Payload]:
    return (
        AttemptOutcome(
            returncode=1,
            is_error=True,
            stderr="Error: rate limit, try again later",
            result_text="",
        ),
        _Payload(tag=tag),
    )


class _ScriptedAttempter:
    """Callable that returns scripted (outcome, payload) pairs in order."""

    def __init__(self, scripted: list[tuple[AttemptOutcome, _Payload]]) -> None:
        self._scripted = list(scripted)
        self.calls: list[str | None] = []

    async def __call__(self, slot: str | None) -> tuple[AttemptOutcome, _Payload]:
        self.calls.append(slot)
        assert self._scripted, "attempt() called more times than scripted"
        return self._scripted.pop(0)


@pytest.fixture
def utc() -> ZoneInfo:
    return ZoneInfo("UTC")


@pytest.fixture
def registry(tmp_path: Path) -> SlotRegistry:
    return SlotRegistry(tmp_path / "slots.json")


class TestRunWithRotation:
    async def test_empty_accounts_single_attempt_no_rotation(
        self, utc: ZoneInfo, registry: SlotRegistry
    ) -> None:
        attempter = _ScriptedAttempter([_ok("single")])

        result: RotationResult[_Payload] = await run_with_rotation(
            accounts=[],
            registry=registry,
            default_tz=utc,
            attempt=attempter,
        )

        assert attempter.calls == [None]
        assert result.payload is not None
        assert result.payload.tag == "single"
        assert result.slot is None
        assert result.first_rl_slot is None
        assert result.all_cooling is False
        assert result.all_rate_limited is False

    async def test_first_slot_success_no_prefix(
        self, utc: ZoneInfo, registry: SlotRegistry
    ) -> None:
        attempter = _ScriptedAttempter([_ok("first")])

        result = await run_with_rotation(
            accounts=["claude", "claude-alt"],
            registry=registry,
            default_tz=utc,
            attempt=attempter,
        )

        assert attempter.calls == ["claude"]
        assert result.payload is not None
        assert result.payload.tag == "first"
        assert result.slot == "claude"
        assert result.first_rl_slot is None

    async def test_first_429_then_success_records_cooldown_and_sets_first_rl(
        self, utc: ZoneInfo, registry: SlotRegistry
    ) -> None:
        attempter = _ScriptedAttempter([_rl("resets 3pm"), _ok("second")])

        result = await run_with_rotation(
            accounts=["claude", "claude-alt"],
            registry=registry,
            default_tz=utc,
            attempt=attempter,
        )

        assert attempter.calls == ["claude", "claude-alt"]
        assert result.payload is not None
        assert result.payload.tag == "second"
        assert result.slot == "claude-alt"
        assert result.first_rl_slot == "claude"
        assert result.first_rl_reset is not None
        assert result.first_rl_reset_estimated is False
        # Cooldown got written for the first slot.
        snap = registry.load()
        assert "claude" in snap.cooldowns
        assert snap.cooldowns["claude"] > 0
        assert "claude-alt" not in snap.cooldowns
        assert snap.last_used == "claude"

    async def test_all_hot_slots_rate_limited_surfaces_last_payload(
        self, utc: ZoneInfo, registry: SlotRegistry
    ) -> None:
        attempter = _ScriptedAttempter(
            [
                _rl("resets 3pm", tag="p1"),
                _rl("resets 4pm", tag="p2"),
                _rl("resets 5pm", tag="p3"),
            ]
        )

        result = await run_with_rotation(
            accounts=["claude", "claude-alt", "claude-third"],
            registry=registry,
            default_tz=utc,
            attempt=attempter,
        )

        assert attempter.calls == ["claude", "claude-alt", "claude-third"]
        assert result.all_rate_limited is True
        assert result.payload is not None
        assert result.payload.tag == "p3"
        assert result.slot == "claude-third"
        assert result.first_rl_slot == "claude"
        snap = registry.load()
        assert set(snap.cooldowns) == {"claude", "claude-alt", "claude-third"}

    async def test_all_cooling_attempts_top_once_and_sets_flag(
        self, utc: ZoneInfo, registry: SlotRegistry
    ) -> None:
        future = (datetime.now(tz=utc) + timedelta(hours=5)).timestamp()
        registry.save(SlotSnapshot(cooldowns={"claude": future, "claude-alt": future}))
        attempter = _ScriptedAttempter([_rl("resets 3pm", tag="cool")])

        result = await run_with_rotation(
            accounts=["claude", "claude-alt"],
            registry=registry,
            default_tz=utc,
            attempt=attempter,
        )

        assert attempter.calls == ["claude"]
        assert result.all_cooling is True
        assert result.payload is not None
        assert result.payload.tag == "cool"
        assert result.slot == "claude"
        # No fallback prefix should be applied (caller checks all_cooling).
        assert result.first_rl_slot is None

    async def test_unparsable_reset_cools_and_tries_next_slot(
        self, utc: ZoneInfo, registry: SlotRegistry
    ) -> None:
        attempter = _ScriptedAttempter([_rl_unparsable("boom"), _ok("second")])

        result = await run_with_rotation(
            accounts=["claude", "claude-alt"],
            registry=registry,
            default_tz=utc,
            attempt=attempter,
        )

        assert attempter.calls == ["claude", "claude-alt"]
        assert result.unparsable_stderr is None
        assert result.payload.tag == "second"
        assert result.first_rl_slot == "claude"
        assert result.first_rl_reset_estimated is True
        assert registry.load().cooldowns["claude"] == pytest.approx(
            datetime.now(tz=utc).timestamp() + 3600, abs=5
        )


@pytest.mark.parametrize("all_cooling", [False, True])
async def test_missing_model_reset_uses_one_hour_cooldown(registry, utc, all_cooling):
    now = datetime.now(tz=utc).timestamp()
    future = now + 7200
    if all_cooling:
        registry.save(SlotSnapshot(cooldowns={"claude": future, "claude-alt": future}))
    limited = (
        AttemptOutcome(
            returncode=1,
            is_error=True,
            stderr="",
            result_text="You've reached your Fable 5 limit. Run /usage-credits to continue or switch models with /model.",
        ),
        _Payload(tag="limited"),
    )
    attempter = _ScriptedAttempter([limited] if all_cooling else [limited, _ok("second")])
    result = await run_with_rotation(
        accounts=["claude", "claude-alt"], registry=registry, default_tz=utc, attempt=attempter
    )
    snapshot = registry.load()
    assert snapshot.cooldowns["claude"] == pytest.approx(now + 3600, abs=5)
    assert snapshot.last_used == "claude"
    assert result.unparsable_stderr is None
    assert result.all_cooling is all_cooling
    if all_cooling:
        assert attempter.calls == ["claude"]
        assert snapshot.cooldowns["claude-alt"] == future
        assert result.payload.tag == "limited"
        assert result.first_rl_slot is None
    else:
        assert attempter.calls == ["claude", "claude-alt"]
        assert "claude-alt" not in snapshot.cooldowns
        assert result.payload.tag == "second"
        assert result.first_rl_slot == "claude"
        assert result.first_rl_reset.timestamp() == snapshot.cooldowns["claude"]

class TestSlotExtraEnv:
    def test_default_slot_uses_explicit_config_dir(self, tmp_path: Path) -> None:
        assert slot_extra_env("claude", working_dir=str(tmp_path)) == {
            "CLAUDE_CONFIG_DIR": str(tmp_path / ".cli-auth/claude")
        }

    def test_alt_slot_host_uses_host_path(self, tmp_path: Path) -> None:
        home = tmp_path / "ductor-home"
        (home / "workspace").mkdir(parents=True)
        env = slot_extra_env(
            "claude-alt",
            working_dir=str(home / "workspace"),
        )
        assert "CLAUDE_CONFIG_DIR" in env
        assert env["CLAUDE_CONFIG_DIR"].endswith(".cli-auth/claude-alt")

    def test_alt_slot_without_working_dir_uses_container_path(self) -> None:
        env = slot_extra_env(
            "claude-alt",
            working_dir=None,
        )
        assert env == {"CLAUDE_CONFIG_DIR": "/ductor/.cli-auth/claude-alt"}


class TestBuildFallbackPrefix:
    def test_includes_date_and_tz_label(self, utc: ZoneInfo) -> None:
        reset = datetime(2026, 4, 21, 15, 0, tzinfo=utc)
        prefix = build_fallback_prefix("claude", "claude-alt", reset, utc)
        assert prefix.startswith("[Claude: claude → claude-alt switched (quota exhausted")
        assert "2026-04-21 15:00 UTC" in prefix
        assert prefix.endswith("\n\n")

    def test_keeps_the_leading_shape_in_korean(self, utc: ZoneInfo) -> None:
        reset = datetime(2026, 4, 21, 15, 0, tzinfo=utc)
        init("ko")
        try:
            prefix = build_fallback_prefix("claude", "claude-alt", reset, utc)
            unparsable = build_unparsable_prefix("raw text")
        finally:
            init("en")
        assert prefix.startswith("[Claude: claude → claude-alt 전환 (할당량 소진")
        assert "2026-04-21 15:00 UTC" in prefix
        assert prefix.endswith("리셋)]\n\n")
        assert unparsable == "[Claude: 쿨다운 파싱 실패, 원본 stderr 이하]\nraw text\n\n"

    def test_unparsable_prefix_in_english(self) -> None:
        assert build_unparsable_prefix("raw text") == (
            "[Claude: cooldown time could not be parsed, raw stderr below]\nraw text\n\n"
        )


def test_default_slot_without_working_dir_is_explicit():
    assert slot_extra_env("claude", working_dir=None) == {
        "CLAUDE_CONFIG_DIR": "/ductor/.cli-auth/claude"
    }


async def test_named_only_account_remains_authenticated_for_model_selector(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from ductor_bot.cli import auth
    from ductor_bot.cli.claude_accounts import pick_slot
    from ductor_bot.config import AgentConfig
    from ductor_bot.orchestrator.selectors import model_selector
    from ductor_bot.session.key import SessionKey

    config = AgentConfig(claude_accounts=["claude-main"])
    (tmp_path / ".cli-auth/claude").mkdir(parents=True)
    main = tmp_path / ".cli-auth/claude-main"
    main.mkdir()
    credential = main / ".credentials.json"
    credential.write_text(json.dumps({"synthetic": True}))
    (tmp_path / ".claude").symlink_to(tmp_path / ".cli-auth/claude")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("DUCTOR_HOME", str(tmp_path))
    monkeypatch.setattr(auth, "_CHECKERS", {
        "claude": auth.check_claude_auth,
        "codex": lambda: auth.AuthResult("codex", auth.AuthStatus.AUTHENTICATED),
    })
    monkeypatch.setattr(model_selector, "_status_line", AsyncMock(return_value="status"))
    checked = auth.check_claude_auth()
    assert checked.is_authenticated
    assert checked.auth_file == credential
    selected = pick_slot(config.claude_accounts, {}, now=0)
    assert selected == "claude-main"
    assert slot_extra_env(selected, working_dir=tmp_path)["CLAUDE_CONFIG_DIR"] == str(main)
    response = await model_selector.model_selector_start(
        SimpleNamespace(), SessionKey("tg", 100)
    )
    assert response.buttons is not None
    assert "ms:p:claude" in [
        button.callback_data for row in response.buttons.rows for button in row
    ]


@pytest.mark.parametrize("language", ["en", "de", "nl", "es", "fr", "id", "pt", "ru", "ko"])
def test_estimated_reset_notice_differs_from_known_reset_in_every_locale(language):
    from ductor_bot.i18n import t

    utc = ZoneInfo("UTC")
    reset = datetime(2026, 4, 21, 15, 0, tzinfo=utc)
    init(language)
    try:
        known = build_fallback_prefix("claude", "claude-alt", reset, utc)
        estimated = build_fallback_prefix("claude", "claude-alt", reset, utc,
                                          reset_estimated=True)
        assert t("slot.switched_estimated") in estimated
        assert "MISSING" not in estimated
        assert estimated != known
        assert "2026-04-21 15:00" in known
        assert "2026-04-21 15:00" not in estimated
    finally:
        init("en")


async def test_auth_expired_top_is_not_probed_while_other_slot_is_cooling(registry, utc):
    from ductor_bot.cli.slot_rotation import record_auth_failure

    registry.save(SlotSnapshot(cooldowns={"claude-alt": 9999999999}))
    await record_auth_failure(registry, "claude", registry.credential_mtimes("claude"))
    attempter = _ScriptedAttempter([_ok()])
    result = await run_with_rotation(
        accounts=["claude", "claude-alt"],
        registry=registry,
        default_tz=utc,
        attempt=attempter,
    )
    assert attempter.calls == ["claude-alt"]
    assert result.all_cooling is True
    assert "claude" in registry.load().auth_failures


@pytest.mark.parametrize("language", ["en", "de", "nl", "es", "fr", "id", "pt", "ru", "ko"])
def test_auth_notice_localized_and_stripped_for_structured_output(language):
    from ductor_bot.cli.slot_rotation import (
        build_auth_fallback_prefix,
        build_reauth_notice,
        strip_fallback_prefix,
    )
    from ductor_bot.i18n import t

    init(language)
    try:
        prefix = build_auth_fallback_prefix("claude", "claude-alt")
        assert t("slot.switched_auth") in prefix
        assert "slot." not in prefix
        assert strip_fallback_prefix(prefix + '{"ok": true}') == '{"ok": true}'
        assert t("slot.reauth_required") in build_reauth_notice()
        assert "slot." not in build_reauth_notice()
    finally:
        init("en")


@pytest.mark.parametrize("old_success", [False, True])
async def test_old_attempt_cannot_overwrite_or_clear_new_login_quarantine(
    registry, utc, old_success
):
    import asyncio
    import os

    from ductor_bot.cli.slot_rotation import hot_slots, record_auth_failure
    from tests.cli.test_claude_accounts import _AUTH_EXPIRED

    credential = registry.state_path.parent / "claude-a" / ".credentials.json"
    credential.parent.mkdir()
    credential.write_text("{}")
    accounts = ["claude-a"] if old_success else ["claude-a", "claude-b"]
    if old_success:
        await record_auth_failure(registry, "claude-a", registry.credential_mtimes("claude-a"))
    entered = asyncio.Event()
    release = asyncio.Event()

    async def old_attempt(slot):
        if slot == "claude-a":
            entered.set()
            await release.wait()
            if old_success:
                return _ok()
            return AttemptOutcome(1, True, _AUTH_EXPIRED, ""), _Payload("old-failure")
        return _ok()

    async def new_attempt(slot):
        if slot == "claude-a":
            return AttemptOutcome(1, True, _AUTH_EXPIRED, ""), _Payload("new-failure")
        return _ok()

    pending = asyncio.create_task(
        run_with_rotation(
            accounts=accounts,
            registry=registry,
            default_tz=utc,
            attempt=old_attempt,
        )
    )
    try:
        await entered.wait()
        old = credential.stat().st_mtime_ns
        os.utime(credential, ns=(old + 1000000, old + 1000000))
        newest = registry.credential_mtimes("claude-a")
        await run_with_rotation(
            accounts=accounts,
            registry=registry,
            default_tz=utc,
            attempt=new_attempt,
        )
        assert registry.load().auth_failures["claude-a"] == newest
    finally:
        release.set()
        await pending
    assert registry.load().auth_failures.get("claude-a") == newest
    assert await hot_slots(registry, accounts) == ([] if old_success else ["claude-b"])


async def test_api_key_auth_failure_record_is_noop_and_stale_record_is_ignored(tmp_path):
    import json

    from ductor_bot.cli.slot_rotation import fallback_slot, hot_slots, record_auth_failure

    registry = SlotRegistry(tmp_path / "slots.json")
    slot = tmp_path / "claude-api"
    slot.mkdir()
    (slot / "settings.json").write_text('{"apiKeyHelper": "helper.sh"}')
    mtimes = registry.credential_mtimes("claude-api")
    await record_auth_failure(registry, "claude-api", mtimes)
    assert not registry.state_path.exists()
    registry.state_path.write_text(json.dumps({"auth_failures": {"claude-api": mtimes}}))
    before = registry.state_path.read_bytes()
    await record_auth_failure(registry, "claude-api", mtimes)
    assert registry.state_path.read_bytes() == before
    assert await hot_slots(registry, ["claude-api", "claude"]) == ["claude-api", "claude"]
    assert await fallback_slot(registry, ["claude-api", "claude"]) == "claude-api"


@pytest.mark.parametrize("driver", ["send", "streaming", "task"])
@pytest.mark.parametrize("cooling", [False, True])
@pytest.mark.parametrize("stale_failure", [False, True])
@pytest.mark.parametrize("subscription_failure", [False, True])
async def test_api_key_login_error_has_no_quarantine_or_relogin_notice(
    tmp_path, monkeypatch, driver, cooling, stale_failure, *, subscription_failure
):
    import json
    import time

    from ductor_bot.cli.base import CLIConfig
    from ductor_bot.cli.claude_provider import ClaudeCodeCLI
    from ductor_bot.cli.param_resolver import resolve_cli_config
    from ductor_bot.cli.stream_events import ResultEvent
    from ductor_bot.config import AgentConfig
    from ductor_bot.i18n import t
    from ductor_bot.infra.task_runner import TaskRunOptions, run_oneshot_task
    from tests.cli.test_claude_accounts import _AUTH_EXPIRED, _SUBSCRIPTION_UNAVAILABLE

    error = _SUBSCRIPTION_UNAVAILABLE if subscription_failure else _AUTH_EXPIRED
    from tests.cli.test_claude_provider import _install_slot_cli, _read_slot_calls

    init("en")
    home, control, calls = _install_slot_cli(tmp_path, monkeypatch)
    slot = home / ".cli-auth" / "claude-api"
    (home / ".cli-auth" / "claude-a").rename(slot)
    (slot / ".credentials.json").unlink()
    (slot / "settings.json").write_text('{"apiKeyHelper": "helper.sh"}')
    fake = tmp_path / "fake-claude"
    fake.write_text(
        fake.read_text().replace(
            "(auth / '.credentials.json').exists()",
            "((auth / '.credentials.json').exists() or (auth / 'settings.json').is_file())",
        )
    )
    control.write_text(json.dumps({"limited": ["claude-api"], "error": error}))
    registry = SlotRegistry(home / ".cli-auth" / "_claude-slots.json")
    registry.state_path.write_text(
        json.dumps(
            {
                "cooldowns": dict.fromkeys(["claude-api", "claude-b"], time.time() + 3600)
                if cooling
                else {},
                "auth_failures": {"claude-api": registry.credential_mtimes("claude-api")}
                if stale_failure
                else {},
            }
        )
    )
    accounts = ["claude-api", "claude-b"]
    if driver == "task":
        config = AgentConfig(
            ductor_home=str(home),
            provider="claude",
            model="opus",
            claude_accounts=accounts,
            user_timezone="UTC",
        )
        response = await run_oneshot_task(
            resolve_cli_config(config, None),
            "hello",
            TaskRunOptions(cwd=tmp_path, timeout_seconds=10, timeout_label="test"),
        )
        result = response.result_text
    else:
        cli = ClaudeCodeCLI(
            CLIConfig(
                provider="claude",
                model="opus",
                ductor_home=home,
                working_dir=str(tmp_path),
                claude_accounts=accounts,
                user_timezone="UTC",
            )
        )
        if driver == "streaming":
            events = [event async for event in cli.send_streaming("hello")]
            result = next(event.result for event in events if isinstance(event, ResultEvent))
        else:
            result = (await cli.send("hello")).result
    assert result == error
    assert t("slot.reauth_required") not in result
    assert t("slot.switched_auth") not in result
    assert registry.load().auth_failures == {}
    assert [Path(row["auth"]).name for row in _read_slot_calls(calls)] == ["claude-api"]


async def test_credit_exhaustion_uses_hour_cooldown_and_probes_again(registry, utc, monkeypatch):
    from ductor_bot.cli import slot_rotation

    monkeypatch.setattr(slot_rotation.time, "time", lambda: 1800000000.0)
    credit_error = AttemptOutcome(
        returncode=1,
        is_error=True,
        stderr="",
        result_text="Credit balance is too low",
    )
    attempter = _ScriptedAttempter([(credit_error, _Payload("credit")), _ok()])
    result = await run_with_rotation(
        accounts=["claude-api", "claude"],
        registry=registry,
        default_tz=utc,
        attempt=attempter,
    )
    assert attempter.calls == ["claude-api", "claude"]
    assert result.first_rl_slot == "claude-api"
    assert result.first_rl_reset_estimated
    assert registry.load().cooldowns["claude-api"] == 1800003600.0
    registry.save(SlotSnapshot(cooldowns={"claude-api": 1800003600.0}))
    probe = _ScriptedAttempter([(credit_error, _Payload("credit"))])
    result = await run_with_rotation(
        accounts=["claude-api"],
        registry=registry,
        default_tz=utc,
        attempt=probe,
    )
    assert result.all_cooling
    assert probe.calls == ["claude-api"]
    assert not result.reauth_required


async def test_subscription_retry_backoff_cap_and_restart(registry, utc, monkeypatch):
    import asyncio
    import json
    import subprocess
    import sys
    from types import SimpleNamespace

    from ductor_bot.cli import slot_rotation
    from ductor_bot.cli.slot_rotation import hot_slots
    from tests.cli.test_claude_accounts import _SUBSCRIPTION_UNAVAILABLE

    clock = [1800000000.0]
    monkeypatch.setattr(slot_rotation, "time", SimpleNamespace(time=lambda: clock[0]))
    failure = AttemptOutcome(1, True, _SUBSCRIPTION_UNAVAILABLE, "")
    for delay in [1800, 3600, 7200, 14400, 21600, 21600]:
        attempter = _ScriptedAttempter([(failure, _Payload("billing")), _ok()])
        result = await run_with_rotation(
            accounts=["claude-a", "claude-b"],
            registry=SlotRegistry(registry.state_path),
            default_tz=utc,
            attempt=attempter,
        )
        assert attempter.calls == ["claude-a", "claude-b"]
        assert result.slot == "claude-b"
        details = SlotRegistry(registry.state_path).load().auth_failure_details["claude-a"]
        assert (details.failed_at, details.retry_delay) == (clock[0], delay)
        clock[0] += delay - 1
        assert await hot_slots(registry, ["claude-a", "claude-b"]) == ["claude-b"]
        clock[0] += 1
        assert await hot_slots(registry, ["claude-a", "claude-b"]) == ["claude-a", "claude-b"]
    child = await asyncio.to_thread(
        subprocess.run,
        [
            sys.executable,
            "-c",
            """
import asyncio, json, sys
from pathlib import Path
from types import SimpleNamespace
from ductor_bot.cli import slot_rotation
from ductor_bot.cli.claude_accounts import SlotRegistry
registry = SlotRegistry(Path(sys.argv[1]))
detail = registry.load().auth_failure_details['claude-a']
slot_rotation.time = SimpleNamespace(time=lambda: detail.failed_at + detail.retry_delay - 1)
assert asyncio.run(slot_rotation.hot_slots(registry, ['claude-a', 'claude-b'])) == ['claude-b']
slot_rotation.time = SimpleNamespace(time=lambda: detail.failed_at + detail.retry_delay)
assert asyncio.run(slot_rotation.hot_slots(registry, ['claude-a', 'claude-b'])) == ['claude-a', 'claude-b']
print(json.dumps([detail.kind, detail.failed_at, detail.retry_delay]))
""",
            str(registry.state_path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(child.stdout) == ["subscription_unavailable", details.failed_at, 21600]


@pytest.mark.parametrize("legacy", [False, True])
async def test_login_quarantine_never_becomes_time_retryable(registry, utc, monkeypatch, legacy):
    import json
    from types import SimpleNamespace

    from ductor_bot.cli import slot_rotation
    from ductor_bot.cli.slot_rotation import hot_slots, record_auth_failure

    mtimes = registry.credential_mtimes("claude-a")
    if legacy:
        registry.state_path.write_text(json.dumps({"auth_failures": {"claude-a": mtimes}}))
    else:
        await record_auth_failure(registry, "claude-a", mtimes)
        assert registry.load().auth_failure_details["claude-a"].kind == "login_expired"
    monkeypatch.setattr(slot_rotation, "time", SimpleNamespace(time=lambda: 9999999999.0))
    assert await hot_slots(SlotRegistry(registry.state_path), ["claude-a", "claude-b"]) == [
        "claude-b"
    ]
    attempter = _ScriptedAttempter([_ok()])
    await run_with_rotation(
        accounts=["claude-a", "claude-b"],
        registry=registry,
        default_tz=utc,
        attempt=attempter,
    )
    assert attempter.calls == ["claude-b"]
    assert registry.load().auth_failures == {"claude-a": mtimes}


async def test_duplicate_subscription_failures_preserve_retry_schedule(registry, monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from ductor_bot.cli import slot_rotation
    from ductor_bot.cli.slot_rotation import hot_slots, record_auth_failure

    clock = [1800000000.0]
    monkeypatch.setattr(slot_rotation, "time", SimpleNamespace(time=lambda: clock[0]))
    mtimes = registry.credential_mtimes("claude-a")

    async def fail_three_requests():
        await asyncio.gather(*(
            record_auth_failure(
                SlotRegistry(registry.state_path), "claude-a", mtimes,
                kind="subscription_unavailable",
            )
            for _ in range(3)
        ))
        return SlotRegistry(registry.state_path).load().auth_failure_details["claude-a"]

    for delay in [1800, 3600, 7200, 14400, 21600, 21600]:
        details = await fail_three_requests()
        assert (details.failed_at, details.retry_delay) == (clock[0], delay)
        deadline = details.failed_at + details.retry_delay
        clock[0] += 60
        assert await fail_three_requests() == details
        clock[0] = deadline - 1
        assert await fail_three_requests() == details
        assert await hot_slots(registry, ["claude-a", "claude-b"]) == ["claude-b"]
        clock[0] = deadline
        assert await hot_slots(registry, ["claude-a", "claude-b"]) == ["claude-a", "claude-b"]


@pytest.fixture
def deletion_home(tmp_path):
    import json

    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.json").write_text(
        json.dumps({"claude_accounts": ["claude-a", "claude-b"]})
    )
    (tmp_path / ".cli-auth" / "claude-a").mkdir(parents=True)
    return tmp_path


async def test_running_slot_blocks_removal(deletion_home, utc):
    import asyncio

    from ductor_bot.cli.claude_accounts import state_file_path
    from ductor_bot.cli.claude_slots import remove

    entered, release = asyncio.Event(), asyncio.Event()

    async def attempt(slot):
        assert slot == "claude-a"
        entered.set()
        await release.wait()
        return _ok()

    rotation = asyncio.create_task(run_with_rotation(
        accounts=["claude-a"],
        registry=SlotRegistry(state_file_path(deletion_home)),
        default_tz=utc,
        attempt=attempt,
    ))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        with pytest.raises(ValueError, match="slot_busy"):
            await asyncio.to_thread(remove, deletion_home, "claude-a")
        assert (deletion_home / ".cli-auth" / "claude-a").is_dir()
    finally:
        release.set()
        await rotation
    await asyncio.to_thread(remove, deletion_home, "claude-a")


@pytest.mark.parametrize("driver", ["regular", "streaming", "one-shot"])
@pytest.mark.parametrize("alternate_cooling", [False, True])
async def test_deleted_slot_is_skipped_during_and_after_removal(
    deletion_home, utc, monkeypatch, driver, alternate_cooling
):
    import asyncio
    import threading

    from ductor_bot.cli import claude_slots
    from ductor_bot.cli.claude_accounts import state_file_path
    from ductor_bot.cli.base import CLIConfig
    from ductor_bot.cli.claude_provider import ClaudeCodeCLI
    from ductor_bot.cli.slot_rotation import make_registry
    from ductor_bot.cli.stream_events import ResultEvent
    from ductor_bot.cron.execution import OneShotExecutionResult
    from ductor_bot.cli.param_resolver import TaskExecutionConfig
    from ductor_bot.infra.task_runner import TaskRunOptions, run_oneshot_task

    entered, release = threading.Event(), threading.Event()
    config = deletion_home / "config" / "config.json"
    transform = claude_slots.transform_config_file

    def hold_removal(*args):
        entered.set()
        assert release.wait(5)
        return transform(*args)

    monkeypatch.setattr(claude_slots, "transform_config_file", hold_removal)
    child = deletion_home / "agents" / "fixture"
    child.mkdir(parents=True)
    accounts = ["claude-a", "claude-b"]
    if alternate_cooling:
        SlotRegistry(state_file_path(deletion_home)).save(
            SlotSnapshot(cooldowns={"claude-b": 9999999999.0})
        )
    selected = []

    async def attempt(slot):
        selected.append(slot)
        return _ok()

    async def streaming(_self, _prompt, *, slot_name, capture, **_options):
        selected.append(slot_name)
        capture.final_result = ResultEvent(type="result", result="OK", returncode=0)
        yield capture.final_result

    async def execute(command, **_kwargs):
        selected.append(Path(command.env_overrides["CLAUDE_CONFIG_DIR"]).name)
        return OneShotExecutionResult(
            status="success", result_text="OK", stdout=b"{}", stderr=b"", returncode=0, timed_out=False
        )

    async def run():
        if driver == "regular":
            await run_with_rotation(
                accounts=accounts,
                registry=make_registry(working_dir=child / "workspace"),
                default_tz=utc,
                attempt=attempt,
            )
        elif driver == "streaming":
            monkeypatch.setattr(ClaudeCodeCLI, "_find_cli", staticmethod(lambda: "claude"))
            monkeypatch.setattr(ClaudeCodeCLI, "_stream_attempt", streaming)
            cli = ClaudeCodeCLI(CLIConfig(
                working_dir=child, ductor_home=child, claude_accounts=accounts
            ))
            assert [event.result async for event in cli.send_streaming("test")] == ["OK"]
        else:
            monkeypatch.setattr("ductor_bot.cron.execution.which", lambda _name: "claude")
            monkeypatch.setattr("ductor_bot.cron.execution.execute_one_shot", execute)
            response = await run_oneshot_task(
                TaskExecutionConfig(
                    provider="claude", model="sonnet", reasoning_effort="medium",
                    cli_parameters=[], permission_mode="bypassPermissions",
                    working_dir=str(child), file_access="full", claude_accounts=accounts,
                ),
                "test",
                TaskRunOptions(cwd=child, timeout_seconds=10, timeout_label="test"),
            )
            assert response.result_text == "OK"

    removal = asyncio.create_task(asyncio.to_thread(claude_slots.remove, deletion_home, "claude-a"))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        assert "claude-a" in config.read_text()
        await run()
        assert selected == ["claude-b"]
    finally:
        release.set()
        await removal
    await run()
    assert selected == ["claude-b", "claude-b"]


@pytest.mark.parametrize("stop", ["close", "cancel"])
async def test_stream_terminates_child_before_releasing_slot(
    deletion_home, monkeypatch, stop
):
    import asyncio
    import os
    import sys

    from ductor_bot.cli import executor
    from ductor_bot.cli.base import CLIConfig
    from ductor_bot.cli.claude_provider import ClaudeCodeCLI
    from ductor_bot.cli.claude_slots import remove
    from ductor_bot.infra.process_tree import force_kill_process_tree

    folder = deletion_home / ".cli-auth" / "claude-a"
    child_pid = deletion_home / "child.pid"
    fake = deletion_home / "fake-claude"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import os, pathlib, time\n"
        f"pathlib.Path({str(child_pid)!r}).write_text(str(os.getpid()))\n"
        "print('{\x22type\x22:\x22system\x22,\x22subtype\x22:\x22init\x22,"
        "\x22session_id\x22:\x22test\x22}', flush=True)\n"
        "time.sleep(30)\n"
    )
    fake.chmod(0o700)
    monkeypatch.setattr(ClaudeCodeCLI, "_find_cli", staticmethod(lambda: str(fake)))
    cli = ClaudeCodeCLI(CLIConfig(
        working_dir=deletion_home, ductor_home=deletion_home, claude_accounts=["claude-a"]
    ))
    killed = []
    terminate = executor._terminate_stream_group

    async def kill(process):
        assert process.pid == int(child_pid.read_text())
        with pytest.raises(ValueError, match="slot_busy"):
            remove(deletion_home, "claude-a")
        await terminate(process)
        killed.append(process.pid)

    monkeypatch.setattr(executor, "_terminate_stream_group", kill)
    stream = cli.send_streaming("fixture")
    try:
        await asyncio.wait_for(anext(stream), 5)
        pid = int(child_pid.read_text())
        assert folder.is_dir()
        if stop == "close":
            await asyncio.wait_for(stream.aclose(), 5)
        else:
            pending = asyncio.create_task(anext(stream))
            await asyncio.sleep(0)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(pending, 5)
        assert killed == [pid]
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        remove(deletion_home, "claude-a")
        assert not folder.exists()
    finally:
        await stream.aclose()
        if child_pid.exists() and int(child_pid.read_text()) not in killed:
            force_kill_process_tree(int(child_pid.read_text()))


def test_slot_locks_coordinate_with_other_processes(deletion_home):
    import subprocess
    import sys

    from ductor_bot.cli.claude_accounts import host_cli_auth_dir
    from ductor_bot.cli.slot_rotation import slot_use_lock

    root = host_cli_auth_dir(deletion_home)
    script = (
        "from pathlib import Path\n"
        "import sys\n"
        "from ductor_bot.cli.slot_rotation import slot_use_lock\n"
        "with slot_use_lock(Path(sys.argv[1]), 'claude-a', deleting=sys.argv[2]=='delete') as held:\n"
        "    print(held)\n"
    )

    def other_process(mode):
        result = subprocess.run(
            [sys.executable, "-c", script, str(root), mode],
            check=True, capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() == "True"

    with slot_use_lock(root, "claude-a") as in_use:
        assert in_use
        assert other_process("use")
        assert not other_process("delete")
    with slot_use_lock(root, "claude-a", deleting=True) as deleting:
        assert deleting
        assert not other_process("use")


@pytest.mark.parametrize("model_override", [False, True])
@pytest.mark.parametrize("driver", ["regular", "streaming", "one-shot"])
async def test_subagent_executes_replacement_main_slot(
    deletion_home, monkeypatch, model_override, driver
):
    import json
    import sys

    from ductor_bot.cli.base import CLIConfig
    from ductor_bot.cli.claude_accounts import state_file_path
    from ductor_bot.cli.claude_provider import ClaudeCodeCLI
    from ductor_bot.cli.claude_slots import remove
    from ductor_bot.cli.param_resolver import TaskExecutionConfig
    from ductor_bot.cli.stream_events import ResultEvent
    from ductor_bot.config import AgentConfig
    from ductor_bot.infra.task_runner import TaskRunOptions, run_oneshot_task
    from ductor_bot.multiagent.models import merge_sub_agent_config
    from ductor_bot.multiagent.registry import AgentRegistry
    from ductor_bot.webchat.cli_login_slots import ClaudeLoginSlots
    from ductor_bot.workspace.paths import DuctorPaths

    agents_path = deletion_home / "agents.json"
    entry = {"name": "fixture", "transport": "headless"}
    if model_override:
        entry.update(provider="claude", model="sonnet", claude_accounts=["claude-unrecognized"])
    agents_path.write_text(json.dumps([entry]))
    main_paths = DuctorPaths(deletion_home)
    main_paths.config_path.write_text('{"claude_accounts": ["claude-a"]}')
    main = AgentConfig.model_validate_json(main_paths.config_path.read_text())
    child_home = deletion_home / "agents" / "fixture"
    child_config = child_home / "config" / "config.json"
    child_config.parent.mkdir(parents=True)
    child_config.write_text('{"claude_accounts": []}')
    sub, = AgentRegistry(agents_path).load()
    child = merge_sub_agent_config(main, sub, child_home)
    assert child.claude_accounts == main.claude_accounts == ["claude-a"]
    assert child.model == ("sonnet" if model_override else main.model)

    calls = deletion_home / "calls"
    (deletion_home / ".cli-auth" / "claude-a" / ".credentials.json").write_text("{}")
    fake = deletion_home / "fake-claude"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "sys.stdin.read()\n"
        "slot = Path(os.environ['CLAUDE_CONFIG_DIR'])\n"
        f"with Path({str(calls)!r}).open('a') as log: log.write(slot.name + '\\n')\n"
        "ok = (slot / '.credentials.json').is_file()\n"
        "print(json.dumps({'type': 'result', 'result': str(slot), 'is_error': not ok}))\n"
        "sys.exit(0 if ok else 1)\n"
    )
    fake.chmod(0o700)
    monkeypatch.setattr(ClaudeCodeCLI, "_find_cli", staticmethod(lambda: str(fake)))
    monkeypatch.setattr("ductor_bot.cron.execution.which", lambda _name: str(fake))
    cli = ClaudeCodeCLI(CLIConfig(
        working_dir=child_home, ductor_home=child.ductor_home, model=child.model,
        agent_name=sub.name, claude_accounts=child.claude_accounts,
    ))
    assert cli._registry.state_path == state_file_path(deletion_home)
    task = TaskExecutionConfig(
        provider="claude", model=child.model, reasoning_effort="medium", cli_parameters=[],
        permission_mode="bypassPermissions", working_dir=str(child_home), file_access="full",
        claude_accounts=child.claude_accounts,
    )

    async def execute():
        if driver == "streaming":
            result, = [e async for e in cli.send_streaming("fixture") if isinstance(e, ResultEvent)]
            assert not result.is_error
            return result.result
        if driver == "one-shot":
            result = await run_oneshot_task(task, "fixture", TaskRunOptions(child_home, 3, "fixture"))
            assert result.status == "success"
            return result.result_text
        result = await cli.send("fixture")
        assert not result.is_error
        return result.result

    assert await execute() == str(deletion_home / ".cli-auth" / "claude-a")
    login = ClaudeLoginSlots(main_paths)
    prepared = login.prepare("claude-b", new_slot=True)
    (prepared.path / ".credentials.json").write_text("{}")
    login.finish(prepared, succeeded=True)
    remove(deletion_home, "claude-a")

    assert child.claude_accounts == ["claude-a"]
    assert await execute() == str(prepared.path)
    assert calls.read_text().splitlines() == ["claude-a", "claude-b"]


@pytest.mark.parametrize("mode", ["cancel", "timeout", "normal"])
async def test_stream_handles_child_after_leader_exits(deletion_home, monkeypatch, mode):
    import asyncio
    import os
    import signal
    import sys

    from ductor_bot.cli.base import CLIConfig
    from ductor_bot.cli.claude_provider import ClaudeCodeCLI
    from ductor_bot.cli.claude_slots import remove

    folder = deletion_home / ".cli-auth" / "claude-a"
    pid_file = deletion_home / "child.pid"
    leader_file = deletion_home / "leader.pid"
    release_pipes = deletion_home / "release-pipes"
    write_slot = deletion_home / "write-slot"
    marker = folder / "child-wrote"
    fake = deletion_home / "fake-claude"
    result_line = "    print(json.dumps({'type':'result','result':'OK'}), flush=True)\n"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import json, os, time\n"
        "from pathlib import Path\n"
        f"Path({str(leader_file)!r}).write_text(str(os.getpid()))\n"
        "if os.fork():\n"
        "    print(json.dumps({'type':'system','subtype':'init','session_id':'fixture'}), flush=True)\n"
        + (result_line if mode == "normal" else "")
        + "    os._exit(0)\n"
        f"Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        f"release = Path({str(release_pipes)!r})\n"
        f"write = Path({str(write_slot)!r})\n"
        "deadline = time.monotonic() + 15\n"
        "while not release.exists() and time.monotonic() < deadline: time.sleep(0.01)\n"
        "os.close(1)\n"
        "os.close(2)\n"
        "while not write.exists() and time.monotonic() < deadline: time.sleep(0.01)\n"
        "if write.exists():\n"
        "    slot = Path(os.environ['CLAUDE_CONFIG_DIR'])\n"
        "    slot.mkdir(exist_ok=True)\n"
        "    (slot / 'child-wrote').touch()\n"
    )
    fake.chmod(0o700)
    monkeypatch.setattr(ClaudeCodeCLI, "_find_cli", staticmethod(lambda: str(fake)))
    cli = ClaudeCodeCLI(CLIConfig(
        working_dir=deletion_home, ductor_home=deletion_home, claude_accounts=["claude-a"]
    ))

    def alive(pid):
        try:
            status = (Path("/proc") / str(pid) / "stat").read_text().rpartition(") ")[2]
        except (FileNotFoundError, ProcessLookupError):
            return False
        return not status.startswith("Z ")

    async def until(predicate):
        async with asyncio.timeout(5):
            while not predicate():
                await asyncio.sleep(0.01)

    stream = cli.send_streaming("fixture", timeout_seconds=2 if mode == "timeout" else 10)
    reader = None
    try:
        await asyncio.wait_for(anext(stream), 5)
        await until(lambda: pid_file.exists() and not alive(int(leader_file.read_text())))
        pid = int(pid_file.read_text())
        assert alive(pid)
        reader = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        assert not reader.done()
        with pytest.raises(ValueError, match="slot_busy"):
            remove(deletion_home, "claude-a")

        if mode == "normal":
            release_pipes.touch()
            assert (await asyncio.wait_for(reader, 5)).result == "OK"
            with pytest.raises(StopAsyncIteration):
                await anext(stream)
            assert alive(pid)
            write_slot.touch()
            await until(marker.exists)
        elif mode == "cancel":
            reader.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(reader, 5)
        else:
            result = await asyncio.wait_for(reader, 5)
            assert result.is_error and result.result.startswith("__TIMEOUT__")
            with pytest.raises(StopAsyncIteration):
                await anext(stream)

        if mode != "normal":
            assert not alive(pid)
            remove(deletion_home, "claude-a")
            write_slot.touch()
            assert not folder.exists()
    finally:
        if pid_file.exists() and alive(pid := int(pid_file.read_text())):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if reader is not None and not reader.done():
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        await stream.aclose()
