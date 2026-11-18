"""Runtime ownership, persistent non-secret TTL, and trusted child environments."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from ductor_bot.cli.base import CLIConfig, format_cli_cmd
from ductor_bot.cli.executor import build_subprocess_env
from ductor_bot.cli.param_resolver import TaskExecutionConfig
from ductor_bot.cron.execution import OneShotCommand, OneShotExecutionResult
from ductor_bot.infra import secret_runtime
from ductor_bot.infra.secret_runtime import (
    SecretRuntime,
    secret_environment,
    start_secret_runtime,
    stop_secret_runtime,
    vault_delete_secret,
    vault_list_keys,
    vault_set_secret,
)
from ductor_bot.infra.secret_vault import VaultError, VaultState
from ductor_bot.infra.task_runner import TaskRunOptions, run_oneshot_task


def token_on_disk(folder: Path, token: str) -> bool:
    return any(token.encode() in path.read_bytes() for path in folder.rglob("*") if path.is_file())


@pytest.fixture
async def runtime(tmp_path: Path) -> AsyncIterator[SecretRuntime]:
    instance = start_secret_runtime(tmp_path)
    yield instance
    await stop_secret_runtime(instance)


async def test_restart_restores_only_ttl_and_rotates_token(
    runtime: SecretRuntime, tmp_path: Path
) -> None:
    token = runtime.token
    assert "DUCTOR_SECRETS_TOKEN" not in os.environ
    await runtime.vault.setup("sample-master-key")
    await runtime.vault.set("TOKEN", "sample-secret")
    await runtime.vault.set_ttl(120)
    settings = tmp_path / "config" / "secret_vault.json"
    assert json.loads(settings.read_text()) == {"ttl_seconds": 120}
    assert not token_on_disk(tmp_path, token)
    await stop_secret_runtime(runtime)
    assert secret_environment() == {}
    restarted = start_secret_runtime(tmp_path)
    try:
        assert restarted.token != token
        assert (await restarted.vault.status()).state is VaultState.LOCKED
        assert (await restarted.vault.status()).ttl_seconds == 120
    finally:
        await stop_secret_runtime(restarted)


async def test_ttl_save_failure_does_not_change_memory(
    runtime: SecretRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(secret_runtime, "atomic_text_save", Mock(side_effect=OSError("failed")))
    with pytest.raises(VaultError, match="VAULT_CONFIG_SAVE_FAILED"):
        await runtime.vault.set_ttl(10)
    assert (await runtime.vault.status()).ttl_seconds == 0


@pytest.mark.parametrize(
    "settings", ["[]", "{}", '{"ttl_seconds": true}', '{"ttl_seconds": -1}', "bad"]
)
def test_bad_ttl_configuration_fails_closed(tmp_path: Path, settings: str) -> None:
    folder = tmp_path / "config"
    folder.mkdir()
    (folder / "secret_vault.json").write_text(settings)
    with pytest.raises(VaultError, match="INVALID_VAULT_CONFIG"):
        SecretRuntime(tmp_path)


@pytest.mark.parametrize(
    ("route", "options"),
    [
        ("main", {}),
        ("sub-agent", {"agent_name": "worker"}),
        ("background-task", {"process_label": "task:example"}),
        (
            "named-background",
            {"process_label": "ns:example", "session_kind": "named", "session_name": "example"},
        ),
        ("inter-agent", {"interagent_turn": True, "session_kind": "interagent"}),
        ("webhook-wake", {"process_label": "wake:example"}),
    ],
)
async def test_token_forwarded_for_conversation_routes(
    runtime: SecretRuntime,
    tmp_path: Path,
    route: str,
    options: dict[str, object],
) -> None:
    home = tmp_path / "agents" / "worker" if route == "sub-agent" else tmp_path
    config = CLIConfig(ductor_home=home, working_dir=home / "workspace")
    for name, value in options.items():
        setattr(config, name, value)
    env = build_subprocess_env(config, {"DUCTOR_SECRETS_TOKEN": "untrusted-override"})
    assert env["DUCTOR_SECRETS_TOKEN"] == runtime.token
    assert env["DUCTOR_SECRETS_VAULT_FILE"] == str(runtime.vault.plaintext_path)
    assert "DUCTOR_SECRETS_TOKEN" not in os.environ


@pytest.mark.parametrize("route", ["cron", "webhook", "legacy-background"])
async def test_token_forwarded_to_oneshot_routes(
    runtime: SecretRuntime, tmp_path: Path, route: str
) -> None:
    command = OneShotCommand(cmd=["synthetic-cli"])
    config = TaskExecutionConfig(
        provider="codex",
        model="test",
        reasoning_effort="",
        cli_parameters=[],
        permission_mode="bypassPermissions",
        working_dir=str(tmp_path),
        file_access="all",
    )
    result = OneShotExecutionResult(
        status="success", result_text="ok", stdout=b"", stderr=b"", returncode=0, timed_out=False
    )
    with (
        patch("ductor_bot.cron.execution.build_cmd", return_value=command),
        patch("ductor_bot.cron.execution.execute_one_shot", new=AsyncMock(return_value=result)),
    ):
        await run_oneshot_task(
            config,
            "instruction",
            TaskRunOptions(
                cwd=tmp_path,
                timeout_seconds=10,
                timeout_label=route,
                ductor_home=tmp_path,
                env_overrides={"DUCTOR_SECRETS_TOKEN": "untrusted-override"},
            ),
        )
    assert command.env_overrides["DUCTOR_SECRETS_TOKEN"] == runtime.token
    assert command.env_overrides["DUCTOR_SECRETS_VAULT_FILE"] == str(runtime.vault.plaintext_path)


async def test_debug_docker_environment_is_redacted(
    runtime: SecretRuntime, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    command = ["docker", "exec", "-e", f"DUCTOR_SECRETS_TOKEN={runtime.token}", "container", "cli"]
    logging.getLogger("vault-child-test").debug("Starting: %s", format_cli_cmd(command))
    assert "DUCTOR_SECRETS_TOKEN=***" in caplog.text
    assert runtime.token not in caplog.text


async def test_bot_store_calls_use_vault_and_never_recreate_plaintext(
    runtime: SecretRuntime,
) -> None:
    path = runtime.vault.plaintext_path
    await vault_set_secret(path, "TOKEN", "sample-secret")
    await runtime.vault.setup("sample-master-key")
    assert await vault_list_keys(path) == ["TOKEN"]
    await vault_set_secret(path, "OTHER", "another-secret")
    assert await runtime.vault.get("OTHER") == "another-secret"
    assert await vault_delete_secret(path, "OTHER")
    assert not path.exists()
    await runtime.vault.lock()
    with pytest.raises(VaultError, match="SECRETS_LOCKED"):
        await vault_set_secret(path, "OTHER", "sample-secret")
    assert not path.exists()


async def test_child_without_cwd_still_receives_token(
    runtime: SecretRuntime, tmp_path: Path
) -> None:
    import sys

    from ductor_bot.cli.executor import SubprocessSpec, run_oneshot_subprocess
    from ductor_bot.cli.types import CLIResponse

    def parse(stdout: bytes, stderr: bytes, code: int | None) -> CLIResponse:
        assert not stderr
        assert code == 0
        return CLIResponse(result=stdout.decode().strip())

    response = await run_oneshot_subprocess(
        CLIConfig(ductor_home=tmp_path),
        SubprocessSpec(
            exec_cmd=[
                sys.executable,
                "-c",
                "import os; print(bool(os.getenv('DUCTOR_SECRETS_TOKEN')))",
            ],
            use_cwd=None,
            prompt="",
            timeout_seconds=10,
        ),
        parse,
    )
    assert response.result == str(bool(runtime.token))


async def test_custom_port_is_supplied_for_background_oneshot(tmp_path: Path) -> None:
    runtime = start_secret_runtime(tmp_path, interagent_port=9123)
    try:
        assert secret_environment(tmp_path)["DUCTOR_INTERAGENT_PORT"] == "9123"
        assert (
            secret_environment(tmp_path / "agents" / "worker")["DUCTOR_SECRETS_TOKEN"]
            == runtime.token
        )
        assert secret_environment(tmp_path / "unrelated") == {}
    finally:
        await stop_secret_runtime(runtime)
