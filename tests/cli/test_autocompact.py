"""Autocompact validation, execution overrides and slot-independent settings."""

from __future__ import annotations

import importlib.util
import json
import shutil
import stat
import subprocess
import threading
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
    autocompact._claude_autocompact_flag_supported.cache_clear()
    yield
    autocompact._claude_autocompact_flag_supported.cache_clear()


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


@pytest.mark.parametrize(
    "error",
    [
        OSError("unavailable"),
        subprocess.TimeoutExpired("fake-cli", 5),
        subprocess.CalledProcessError(1, "fake-cli"),
    ],
)
def test_probe_failure_is_retried_then_success_cached(monkeypatch, error):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        assert kwargs["check"] is True
        if len(calls) == 1:
            raise error
        return subprocess.CompletedProcess(cmd, 0, "--autocompact", "")

    monkeypatch.setattr(autocompact.subprocess, "run", run)
    assert autocompact.claude_autocompact_flag_supported("retry-cli") is False
    assert autocompact.claude_autocompact_flag_supported("retry-cli") is True
    assert autocompact.claude_autocompact_flag_supported("retry-cli") is True
    assert len(calls) == 2


@pytest.mark.parametrize("mode", ["send", "stream", "stream_slot"])
async def test_runtime_probe_runs_outside_event_loop(tmp_path, monkeypatch, mode):
    loop_thread = threading.get_ident()
    probe_threads = []

    def probe(_cli):
        probe_threads.append(threading.get_ident())
        return True

    async def oneshot(**_kwargs):
        return CLIResponse(result="ok")

    async def stream(**_kwargs):
        yield ResultEvent(type="result", result="ok", is_error=False)

    monkeypatch.setattr("ductor_bot.cli.claude_provider.claude_autocompact_flag_supported", probe)
    monkeypatch.setattr("ductor_bot.cli.claude_provider.run_oneshot_subprocess", oneshot)
    monkeypatch.setattr("ductor_bot.cli.claude_provider.run_streaming_subprocess", stream)
    cli = ClaudeCodeCLI(
        CLIConfig(
            ductor_home=tmp_path,
            autocompact=300000,
            claude_accounts=["claude"] if mode == "stream_slot" else [],
        )
    )
    if mode == "send":
        await cli.send("hello")
    else:
        async for _ in cli.send_streaming("hello"):
            pass
    assert len(probe_threads) == 1
    assert probe_threads[0] != loop_thread


TOKEN_INPUT_CASES = [
    ("500000", 500000),
    ("500,000", 500000),
    ("500k", 500000),
    ("500K", 500000),
    ("0.5m", 500000),
    ("1M", 1000000),
    ("100k", 100000),
    ("100,000", 100000),
    ("1,000,000", 1000000),
    ("0.100001M", 100001),
    ("100.001k", 100001),
    (" 500k ", 500000),
    ("500000.0", 500000),
    ("000500k", 500000),
    ("", None),
    ("99.999k", None),
    ("1.000001M", None),
    ("500.0001k", None),
    ("500000.5", None),
    ("500,00", None),
    ("5,00,000", None),
    (",500000", None),
    ("500000,", None),
    ("5e5", None),
    ("+500000", None),
    ("-500k", None),
    ("500 k", None),
    ("500kk", None),
    ("500_000", None),
    ("\uff15\uff10\uff10\uff10\uff10\uff10", None),
    ("NaN", None),
    ("Infinity", None),
    (".5m", None),
    ("500.k", None),
    ("default", None),
    ("off", None),
]


@pytest.mark.parametrize(("text", "expected"), TOKEN_INPUT_CASES)
def test_token_input_parser(text, expected):
    if expected is None:
        with pytest.raises(ValueError, match=r"[Aa]utocompact"):
            autocompact.parse_autocompact_tokens(text)
    else:
        assert autocompact.parse_autocompact_tokens(text) == expected
        assert autocompact.parse_autocompact_input(text) == expected


def test_stdlib_tool_and_javascript_token_parsers_match():
    root = Path(__file__).resolve().parents[2]
    tool_path = root / "ductor_bot/_home_defaults/workspace/tools/_tool_shared.py"
    spec = importlib.util.spec_from_file_location("ac_tool_shared", tool_path)
    assert spec is not None
    assert spec.loader is not None
    shared = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(shared)
    for text, expected in TOKEN_INPUT_CASES:
        if text in {"default", "off"}:
            assert shared.parse_autocompact_option(text) == text
        elif expected is None:
            with pytest.raises(ValueError, match=r"[Aa]utocompact"):
                shared.parse_autocompact_option(text)
        else:
            assert shared.parse_autocompact_option(text) == expected
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js required for browser parser parity")
    source = (root / "ductor_bot/webchat/web/static/js/autocompact.js").read_text()
    parser = source[source.index("export function parseAutocompactTokens") :]
    parser = parser[: parser.index("export function initAutocompact")]
    result = subprocess.run(
        [
            node,
            "--input-type=module",
            "-e",
            parser
            + "console.log(JSON.stringify("
            + json.dumps([case[0] for case in TOKEN_INPUT_CASES])
            + ".map(parseAutocompactTokens)));",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == [case[1] for case in TOKEN_INPUT_CASES]
