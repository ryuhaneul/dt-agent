"""Autocompact validation, execution overrides and slot-independent settings."""

from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from ductor_bot.cli import autocompact
from ductor_bot.cli._log_redact import redact_cmd_for_log
from ductor_bot.cli.base import CLIConfig
from ductor_bot.cli.claude_provider import ClaudeCodeCLI
from ductor_bot.cli.codex_provider import CodexCLI
from ductor_bot.cli.executor import build_subprocess_env
from ductor_bot.cli.stream_events import ResultEvent
from ductor_bot.cli.types import CLIResponse
from ductor_bot.config import AgentConfig
from ductor_bot.multiagent.models import SubAgentConfig


@pytest.fixture(autouse=True)
def _no_real_claude_probe(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "ductor_bot.cli.claude_provider.claude_autocompact_flag_supported", lambda _: True
    )
    autocompact.claude_autocompact_flag_supported.cache_clear()
    yield
    autocompact.claude_autocompact_flag_supported.cache_clear()


@pytest.mark.parametrize("value", ["default", "off", 100000, 300000, 1000000])
def test_valid_values(value):
    assert autocompact.validate_autocompact(value) == value
    assert AgentConfig(autocompact=value).autocompact == value
    assert SubAgentConfig(name="worker", autocompact=value).autocompact == value


@pytest.mark.parametrize(
    "value",
    [None, True, False, 99999, 1000001, 100000.0, "100000", "100k", "inherit", "auto", {}, []],
)
def test_invalid_values(value):
    with pytest.raises(ValidationError):
        autocompact.validate_autocompact(value)
    with pytest.raises(ValidationError):
        AgentConfig(autocompact=value)
    if value is not None:
        with pytest.raises(ValidationError):
            SubAgentConfig(name="worker", autocompact=value)


@pytest.mark.parametrize(
    ("topic", "global_value", "expected"),
    [
        (None, None, "default"),
        (None, "off", "off"),
        (None, 300000, 300000),
        ("default", 300000, "default"),
        ("off", 300000, "off"),
        (400000, "off", 400000),
    ],
)
def test_resolution(topic, global_value, expected):
    assert autocompact.resolve_autocompact(topic, global_value) == expected


def test_missing_config_key_preserves_cli_default():
    assert AgentConfig().autocompact == "default"


@pytest.mark.parametrize("provider", ["claude", "codex", "grok", "gemini"])
@pytest.mark.parametrize("value", ["default", "off", 300000])
def test_process_environment(tmp_path, monkeypatch, provider, value):
    for key in ["DISABLE_AUTO_COMPACT", "DISABLE_COMPACT"]:
        monkeypatch.setenv(key, "1")
    monkeypatch.setenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "100000")
    monkeypatch.setenv("DUCTOR_AUTOCOMPACT", "stale")
    env = build_subprocess_env(
        CLIConfig(provider=provider, ductor_home=tmp_path, autocompact=value)
    )
    assert env["DUCTOR_AUTOCOMPACT"] == str(value)
    if provider == "claude" and isinstance(value, int):
        assert "DISABLE_AUTO_COMPACT" not in env
        assert "DISABLE_COMPACT" not in env
        assert env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == str(value)
    else:
        assert env["DISABLE_AUTO_COMPACT"] == "1"
        assert env["DISABLE_COMPACT"] == "1"
        assert env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "100000"
    if provider != "claude":
        assert autocompact.autocompact_env(provider, value) == {}


@pytest.mark.parametrize("value", ["default", "off", 300000])
@pytest.mark.parametrize("resume", [None, "session-1"])
def test_codex_new_and_resume(value, resume):
    cmd = CodexCLI(CLIConfig(provider="codex", autocompact=value))._build_command("hi", resume)
    limit = "model_auto_compact_token_limit=300000"
    assert (limit in cmd) == isinstance(value, int)
    if isinstance(value, int):
        assert cmd.index(limit) < cmd.index("--")
        assert cmd[cmd.index(limit) - 1] == "-c"
    assert not any("DISABLE_" in arg or "--autocompact" in arg for arg in cmd)


@pytest.mark.parametrize("value", ["default", "off", 300000])
def test_claude_inline_settings_merge_preserves_other_keys(tmp_path, value):
    prior = {
        "env": {"SYNTHETIC_SECRET": "fake-secret", "DISABLE_AUTO_COMPACT": "1"},
        "permissions": {"allow": ["Read"]},
    }
    cli = ClaudeCodeCLI(
        CLIConfig(
            ductor_home=tmp_path,
            autocompact=value,
            cli_parameters=["--settings", json.dumps(prior)],
        )
    )
    cmd = cli._build_command()
    assert cmd.count("--settings") == 1
    settings = json.loads(cmd[cmd.index("--settings") + 1])
    assert settings["env"]["SYNTHETIC_SECRET"] == "fake-secret"
    assert settings["permissions"] == prior["permissions"]
    assert "fake-secret" not in " ".join(redact_cmd_for_log(cmd))
    if isinstance(value, int):
        assert cmd[cmd.index("--autocompact") + 1] == str(value)
        assert settings["autoCompactEnabled"] is True
        assert settings["autoCompactWindow"] == value
        assert settings["env"]["DISABLE_AUTO_COMPACT"] == "0"
        assert settings["env"]["DISABLE_COMPACT"] == "0"
    elif value == "off":
        assert settings["autoCompactEnabled"] is False
        assert settings["env"]["DISABLE_AUTO_COMPACT"] == "1"
        assert "DISABLE_COMPACT" not in settings["env"]
    else:
        assert settings == prior


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("failure", [False, True])
async def test_file_settings_private_and_removed(tmp_path, monkeypatch, streaming, failure):
    original = tmp_path / "prior.json"
    original.write_text(json.dumps({"env": {"SYNTHETIC_SECRET": "fake-secret"}}))
    cli = ClaudeCodeCLI(
        CLIConfig(
            working_dir=tmp_path,
            ductor_home=tmp_path,
            autocompact=300000,
            cli_parameters=["--settings=prior.json"],
        )
    )
    seen = []

    def check_spec(spec):
        cmd = spec.exec_cmd
        assert "fake-secret" not in " ".join(cmd)
        path = Path(cmd[cmd.index("--settings") + 1])
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        settings = json.loads(path.read_text())
        assert settings["env"]["SYNTHETIC_SECRET"] == "fake-secret"
        assert settings["autoCompactWindow"] == 300000
        seen.append(path)
        if failure:
            raise OSError("synthetic execution error")

    async def oneshot(*, spec, **_kwargs):
        check_spec(spec)
        return CLIResponse(result="ok")

    async def stream(*, spec, **_kwargs):
        check_spec(spec)
        yield ResultEvent(type="result", result="ok", is_error=False)

    monkeypatch.setattr("ductor_bot.cli.claude_provider.run_oneshot_subprocess", oneshot)
    monkeypatch.setattr("ductor_bot.cli.claude_provider.run_streaming_subprocess", stream)

    async def run():
        if streaming:
            async for _ in cli.send_streaming("hi"):
                pass
        else:
            await cli.send("hi")

    if failure:
        with pytest.raises(OSError, match="synthetic execution error"):
            await run()
    else:
        await run()
    assert len(seen) == 1
    assert not seen[0].exists()
    assert not seen[0].parent.exists()
    assert original.exists()


async def test_settings_write_failure_cleans_private_directory(tmp_path, monkeypatch):
    source = tmp_path / "prior.json"
    source.write_text("{}")
    monkeypatch.setattr(
        "ductor_bot.cli.claude_provider.tempfile.mkdtemp", lambda **_: str(tmp_path / "private")
    )
    (tmp_path / "private").mkdir(mode=0o700)

    def fail(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("ductor_bot.cli.claude_provider.write_private_temp_file", fail)
    cli = ClaudeCodeCLI(
        CLIConfig(
            working_dir=tmp_path,
            ductor_home=tmp_path,
            autocompact=300000,
            cli_parameters=["--settings", "prior.json"],
        )
    )
    with pytest.raises(OSError, match="disk full"):
        await cli.send("hi")
    assert not (tmp_path / "private").exists()


@pytest.mark.parametrize("help_output", ["--autocompact <auto|tokens>", "old CLI"])
def test_support_probe_cached_without_model_call(monkeypatch, help_output):
    calls = []

    def run(cmd, **_kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, help_output, "")

    monkeypatch.setattr(autocompact.subprocess, "run", run)
    for _ in range(2):
        assert autocompact.claude_autocompact_flag_supported("fake-cli") == (
            "--autocompact" in help_output
        )
    assert calls == [["fake-cli", "--help"]]


def test_failed_probe_uses_env_and_settings_only(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "ductor_bot.cli.claude_provider.claude_autocompact_flag_supported", lambda _: False
    )
    cli = ClaudeCodeCLI(CLIConfig(ductor_home=tmp_path, autocompact=300000))
    cmd = cli._build_command()
    assert "--autocompact" not in cmd
    assert json.loads(cmd[cmd.index("--settings") + 1])["autoCompactWindow"] == 300000
    assert cli._slot_extra_env("claude")["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "300000"


@pytest.mark.parametrize("value", ["off", 300000])
async def test_three_slot_rotation_same_settings_and_env(tmp_path, monkeypatch, value):
    slots = ["claude", "claude-sub", "claude-api"]
    monkeypatch.setattr("ductor_bot.cli.slot_rotation.hot_slots", AsyncMock(return_value=slots))
    monkeypatch.setattr("ductor_bot.cli.slot_rotation.record_cooldown", AsyncMock())
    monkeypatch.setattr(
        "ductor_bot.cli.claude_provider.slot_extra_env",
        lambda slot, **_: {"CLAUDE_CONFIG_DIR": str(tmp_path / slot)},
    )
    cli = ClaudeCodeCLI(
        CLIConfig(
            ductor_home=tmp_path, autocompact=value, claude_accounts=slots, user_timezone="UTC"
        )
    )
    seen = []

    async def run(*, config, spec, **_kwargs):
        env = build_subprocess_env(config, spec.extra_env)
        seen.append(
            (
                spec.exec_cmd,
                {
                    k: v
                    for k, v in env.items()
                    if k
                    in {
                        "DISABLE_COMPACT",
                        "DISABLE_AUTO_COMPACT",
                        "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
                        "DUCTOR_AUTOCOMPACT",
                    }
                },
                env["CLAUDE_CONFIG_DIR"],
            )
        )
        if len(seen) < 3:
            return CLIResponse(is_error=True, returncode=1, stderr="rate limit reached, resets 3pm")
        return CLIResponse(result="ok", returncode=0)

    monkeypatch.setattr("ductor_bot.cli.claude_provider.run_oneshot_subprocess", run)
    response = await cli.send("hi")
    assert not response.is_error
    assert [Path(row[2]).name for row in seen] == slots
    assert seen[0][:2] == seen[1][:2] == seen[2][:2]


@pytest.mark.parametrize("arg", ["--settings", "--settings=secret"])
def test_settings_argument_redacted(arg):
    cmd = ["claude", arg, "secret"] if arg == "--settings" else ["claude", arg]
    assert "secret" not in " ".join(redact_cmd_for_log(cmd))
