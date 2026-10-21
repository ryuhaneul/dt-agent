"""Tests for run_oneshot_task .env forwarding into one_shot.env_overrides."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

from ductor_bot.cli.param_resolver import TaskExecutionConfig
from ductor_bot.cli.slot_rotation import RotationResult
from ductor_bot.cron.execution import OneShotCommand, OneShotExecutionResult
from ductor_bot.infra.env_secrets import clear_cache
from ductor_bot.infra.task_runner import TaskRunOptions, run_oneshot_task

if TYPE_CHECKING:
    import pytest


def _exec_config() -> TaskExecutionConfig:
    return TaskExecutionConfig(
        provider="claude",
        model="opus",
        reasoning_effort="",
        cli_parameters=[],
        permission_mode="bypassPermissions",
        working_dir="/tmp",
        file_access="all",
    )


def _exec_result() -> OneShotExecutionResult:
    return OneShotExecutionResult(
        status="success",
        result_text="ok",
        stdout=b"",
        stderr=b"",
        returncode=0,
        timed_out=False,
    )


async def _run_and_capture(
    tmp_path: Path,
    routing_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Drive run_oneshot_task with a fixed OneShotCommand and capture env_overrides."""
    captured: list[dict[str, str]] = []
    cmd = OneShotCommand(cmd=["/usr/bin/claude", "-p", "--", "hi"])

    async def fake_exec(one_shot: OneShotCommand, **_: object) -> OneShotExecutionResult:
        captured.append(dict(one_shot.env_overrides))
        return _exec_result()

    with (
        patch("ductor_bot.cron.execution.build_cmd", return_value=cmd),
        patch(
            "ductor_bot.cron.execution.execute_one_shot",
            new=AsyncMock(side_effect=fake_exec),
        ),
    ):
        await run_oneshot_task(
            _exec_config(),
            "hi",
            TaskRunOptions(
                cwd=tmp_path,
                timeout_seconds=60,
                timeout_label="test",
                ductor_home=tmp_path,
                env_overrides=routing_env or {},
            ),
        )

    assert len(captured) == 1
    return captured[0]


class TestDotenvForwarding:
    """run_oneshot_task merges ~/.ductor/.env into one_shot.env_overrides."""

    async def test_forwards_keys_not_in_environ(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / ".env").write_text("DUCTOR_PR_TEST_DOTENV_KEY=from-file\n")
        monkeypatch.delenv("DUCTOR_PR_TEST_DOTENV_KEY", raising=False)
        clear_cache()

        overrides = await _run_and_capture(tmp_path)

        assert overrides["DUCTOR_PR_TEST_DOTENV_KEY"] == "from-file"
        assert overrides["DUCTOR_HOME"] == str(tmp_path)

    async def test_existing_environ_key_not_overridden(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / ".env").write_text("PRESET=from-file\n")
        monkeypatch.setenv("PRESET", "from-process")
        clear_cache()

        overrides = await _run_and_capture(tmp_path)

        assert "PRESET" not in overrides
        assert overrides["DUCTOR_HOME"] == str(tmp_path)

    async def test_ductor_home_not_overridden_by_dotenv(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / ".env").write_text("DUCTOR_HOME=/wrong\n")
        monkeypatch.delenv("DUCTOR_HOME", raising=False)
        clear_cache()

        overrides = await _run_and_capture(tmp_path)

        assert overrides["DUCTOR_HOME"] == str(tmp_path)



async def test_routing_overrides_reach_cli(tmp_path: Path) -> None:
    env = await _run_and_capture(
        tmp_path,
        {"DUCTOR_CHAT_ID": "-100123", "DUCTOR_TOPIC_ID": "8", "DUCTOR_TRANSPORT": "tg"},
    )
    assert {key: env[key] for key in ("DUCTOR_CHAT_ID", "DUCTOR_TOPIC_ID", "DUCTOR_TRANSPORT")} == {
        "DUCTOR_CHAT_ID": "-100123",
        "DUCTOR_TOPIC_ID": "8",
        "DUCTOR_TRANSPORT": "tg",
    }


async def test_rotated_oneshot_normalizes_route_after_all_env_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / ".env").write_text(
        "DUCTOR_TOPIC_ID=77\nDUCTOR_RESULT_ORIGIN=old\n"
    )
    monkeypatch.delenv("DUCTOR_TOPIC_ID", raising=False)
    monkeypatch.delenv("DUCTOR_RESULT_ORIGIN", raising=False)
    clear_cache()
    captured: dict[str, str | None] = {}

    async def fake_exec(one_shot: OneShotCommand, **_: object) -> OneShotExecutionResult:
        captured.update(one_shot.env_overrides)
        return _exec_result()

    async def fake_rotation(**kwargs: object) -> RotationResult[OneShotExecutionResult]:
        _, execution = await kwargs["attempt"]("slot")
        return RotationResult(payload=execution, slot="slot")

    with (
        patch("ductor_bot.cli.slot_rotation.make_registry", return_value=MagicMock()),
        patch("ductor_bot.cli.slot_rotation.slot_extra_env", return_value={
            "DUCTOR_CHAT_ID": "-100888",
        }),
        patch("ductor_bot.cli.slot_rotation.run_with_rotation", side_effect=fake_rotation),
        patch("ductor_bot.cron.execution.build_cmd", return_value=OneShotCommand(
            cmd=["cli"], env_overrides={"DUCTOR_ORIGIN_CHAT_ID": "-100999"},
        )),
        patch("ductor_bot.cron.execution.execute_one_shot", new=AsyncMock(side_effect=fake_exec)),
    ):
        result = await run_oneshot_task(
            replace(_exec_config(), claude_accounts=["slot"]),
            "work",
            TaskRunOptions(
                cwd=tmp_path,
                timeout_seconds=1,
                timeout_label="cron",
                ductor_home=tmp_path,
                routing_env={
                    "DUCTOR_TRANSPORT": "tg",
                    "DUCTOR_RESULT_ORIGIN": "cron-broadcast",
                    "DUCTOR_AGENT_NAME": "main",
                    "DUCTOR_INTERAGENT_PORT": "9123",
                },
            ),
        )

    assert result.status == "success"
    assert captured["DUCTOR_TRANSPORT"] == "tg"
    assert captured["DUCTOR_RESULT_ORIGIN"] == "cron-broadcast"
    assert captured["DUCTOR_AGENT_NAME"] == "main"
    assert captured["DUCTOR_INTERAGENT_PORT"] == "9123"
    assert captured["DUCTOR_CHAT_ID"] is None
    assert captured["DUCTOR_TOPIC_ID"] is None
    assert captured["DUCTOR_ORIGIN_CHAT_ID"] is None
