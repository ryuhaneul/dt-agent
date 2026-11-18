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
