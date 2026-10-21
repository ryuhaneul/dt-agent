"""``--system-prompt-snapshot``: CLI support detection and where the flag is added."""

from __future__ import annotations

import asyncio
import logging
import os
import stat
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from ductor_bot.cli import claude_features
from ductor_bot.cli.base import CLIConfig
from ductor_bot.cli.claude_provider import ClaudeCodeCLI
from ductor_bot.cli.process_registry import ProcessRegistry
from ductor_bot.cli.service import CLIService, CLIServiceConfig
from ductor_bot.cli.types import AgentRequest, CLIResponse
from ductor_bot.config import ModelRegistry

FLAG = "--system-prompt-snapshot"


@pytest.fixture(autouse=True)
def _fresh_cache():
    claude_features._supported = None
    claude_features._probing = None
    yield
    claude_features._supported = None
    claude_features._probing = None


def _fake_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    script = tmp_path / "claude"
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}/usr/bin:/bin")


# -- detection ---------------------------------------------------------------------------


async def test_supported_when_help_lists_the_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_claude(tmp_path, monkeypatch, f"echo 'Options: {FLAG} <mode>'")
    assert await claude_features.snapshot_flag_supported() is True


async def test_unsupported_when_help_lacks_the_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_claude(tmp_path, monkeypatch, "echo 'Options: --model'")
    assert await claude_features.snapshot_flag_supported() is False


async def test_unsupported_when_help_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_claude(tmp_path, monkeypatch, f"echo {FLAG}; exit 3")
    assert await claude_features.snapshot_flag_supported() is False


async def test_unsupported_when_help_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_claude(tmp_path, monkeypatch, f"sleep 5; echo {FLAG}")
    monkeypatch.setattr(claude_features, "_HELP_TIMEOUT_SECONDS", 0.3)
    assert await claude_features.snapshot_flag_supported() is False


async def test_unsupported_when_the_cli_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    assert await claude_features.snapshot_flag_supported() is False


async def test_probe_runs_once_and_warns_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls: list[int] = []

    def probe() -> bool:
        calls.append(1)
        return False

    monkeypatch.setattr(claude_features, "_probe", probe)
    with caplog.at_level(logging.WARNING, logger=claude_features.logger.name):
        for _ in range(3):
            assert await claude_features.snapshot_refresh_active("off", "claude") is False
    assert len(calls) == 1
    assert sum(FLAG in r.getMessage() for r in caplog.records) == 1


async def test_refresh_needs_claude_and_mode_off(monkeypatch: pytest.MonkeyPatch) -> None:
    def probe() -> bool:
        raise AssertionError("the CLI is not probed for these turns")

    monkeypatch.setattr(claude_features, "_probe", probe)
    assert await claude_features.snapshot_refresh_active("on", "claude") is False
    for provider in ("codex", "gemini", "grok"):
        assert await claude_features.snapshot_refresh_active("off", provider) is False


async def test_refresh_active_for_claude_off_when_supported() -> None:
    claude_features._supported = True
    assert await claude_features.snapshot_refresh_active("off", "claude") is True


# -- the flag on the command line ---------------------------------------------------------


def _service() -> CLIService:
    config = CLIServiceConfig(
        working_dir="/tmp",
        default_model="opus",
        provider="claude",
        max_turns=None,
        max_budget_usd=None,
        permission_mode="bypassPermissions",
    )
    return CLIService(
        config=config,
        models=ModelRegistry(),
        available_providers=frozenset({"claude", "codex", "gemini", "grok"}),
        process_registry=ProcessRegistry(),
    )


def _params_built_for(request: AgentRequest, svc: CLIService | None = None) -> list[str]:
    with patch("ductor_bot.cli.service.create_cli") as create:
        (svc or _service())._make_cli(request)
    config: CLIConfig = create.call_args[0][0]
    return list(config.cli_parameters)


def test_flag_added_for_an_effective_claude_turn() -> None:
    params = _params_built_for(AgentRequest(prompt="x", claude_snapshot_off=True))
    assert params[-2:] == [FLAG, "off"]


def test_flag_not_added_without_the_request_mark() -> None:
    assert FLAG not in _params_built_for(AgentRequest(prompt="x"))


@pytest.mark.parametrize("model", ["gpt-5.2-codex", "gemini-2.5-pro"])
def test_flag_not_added_when_the_effective_provider_is_not_claude(model: str) -> None:
    svc = _service()
    provider = svc._models.provider_for(model)
    assert provider != "claude"
    request = AgentRequest(
        prompt="x", claude_snapshot_off=True, model_override=model, provider_override=provider
    )
    assert FLAG not in _params_built_for(request, svc)


def test_request_provider_override_decides_not_the_service_default() -> None:
    request = AgentRequest(
        prompt="x",
        claude_snapshot_off=True,
        model_override="gpt-5.2-codex",
        provider_override="codex",
    )
    assert _service().resolve_provider(request)[0] == "codex"
    assert FLAG not in _params_built_for(request)


def test_user_given_parameter_is_not_doubled() -> None:
    svc = _service()
    with patch.object(CLIServiceConfig, "cli_parameters_for_provider", return_value=[FLAG, "on"]):
        params = _params_built_for(AgentRequest(prompt="x", claude_snapshot_off=True), svc)
    assert params == [FLAG, "on"]


def test_claude_command_carries_the_flag_but_never_the_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ClaudeCodeCLI, "_find_cli", staticmethod(lambda: "claude"))
    cli = ClaudeCodeCLI(CLIConfig(cli_parameters=[FLAG, "off"]))
    cmd = cli._build_command(resume_session="sid")
    assert cmd[cmd.index(FLAG) + 1] == "off"
    assert "--resume" in cmd


async def test_service_executes_with_the_flag_end_to_end() -> None:
    svc = _service()
    with patch("ductor_bot.cli.service.create_cli") as create:
        cli = AsyncMock()
        cli.send.return_value = CLIResponse(result="ok", session_id="s")
        create.return_value = cli
        await svc.execute(AgentRequest(prompt="x", chat_id=1, claude_snapshot_off=True))
    assert create.call_args[0][0].cli_parameters[-2:] == [FLAG, "off"]


async def test_concurrent_first_calls_share_one_probe_and_one_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import time

    calls: list[int] = []

    def slow_probe() -> bool:
        calls.append(1)
        time.sleep(0.2)
        return False

    monkeypatch.setattr(claude_features, "_probe", slow_probe)
    with caplog.at_level(logging.WARNING, logger=claude_features.logger.name):
        results = await asyncio.gather(
            *(claude_features.snapshot_refresh_active("off", "claude") for _ in range(4))
        )

    assert results == [False] * 4
    assert len(calls) == 1
    assert sum(FLAG in r.getMessage() for r in caplog.records) == 1
