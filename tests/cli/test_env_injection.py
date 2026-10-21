"""Tests for .env secret injection into subprocess environments."""

from __future__ import annotations

from pathlib import Path

from ductor_bot.cli.base import CLIConfig
from ductor_bot.cli.executor import build_subprocess_env
from ductor_bot.infra.env_secrets import clear_cache


def test_subprocess_env_merges_secrets(tmp_path: Path) -> None:
    """Secrets from .env are merged into the subprocess env dict."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env_file = tmp_path / ".env"
    env_file.write_text("MY_SECRET=hunter2\n")

    config = CLIConfig(working_dir=str(workspace), ductor_home=tmp_path)
    clear_cache()
    env = build_subprocess_env(config)

    assert env is not None
    assert env["MY_SECRET"] == "hunter2"


def test_subprocess_env_does_not_override_existing(tmp_path: Path) -> None:
    """Existing environment variables must not be overridden by .env."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env_file = tmp_path / ".env"
    env_file.write_text("PATH=/evil\n")

    config = CLIConfig(working_dir=str(workspace), ductor_home=tmp_path)
    clear_cache()
    env = build_subprocess_env(config)

    assert env is not None
    assert env["PATH"] != "/evil"


def test_subprocess_env_works_without_env_file(tmp_path: Path) -> None:
    """No .env file should not break subprocess env construction."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    config = CLIConfig(working_dir=str(workspace), ductor_home=tmp_path)
    clear_cache()
    env = build_subprocess_env(config)

    assert env is not None
    assert "DUCTOR_AGENT_NAME" in env


def test_subprocess_env_sets_task_id_for_task_label(tmp_path: Path) -> None:
    """Background-task labels (task:<id>) expose DUCTOR_TASK_ID to the subprocess."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    config = CLIConfig(
        working_dir=str(workspace), ductor_home=tmp_path, process_label="task:abc123"
    )
    clear_cache()
    env = build_subprocess_env(config)

    assert env is not None
    assert env["DUCTOR_TASK_ID"] == "abc123"


def test_subprocess_env_omits_task_id_for_other_labels(tmp_path: Path) -> None:
    """Non-task labels (main, ns:*, interagent:*) must not leak DUCTOR_TASK_ID."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    for label in ("main", "ns:build", "interagent:worker"):
        config = CLIConfig(working_dir=str(workspace), ductor_home=tmp_path, process_label=label)
        clear_cache()
        env = build_subprocess_env(config)

        assert env is not None
        assert "DUCTOR_TASK_ID" not in env


def test_project_cwd_cannot_supply_framework_env(tmp_path: Path) -> None:
    """project_roots changes cwd, never the bot home or routing endpoint."""
    home = tmp_path / "bot"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    (home / ".env").write_text(
        "BOT_ONLY=from-home\nDUCTOR_INTERAGENT_HOST=from-home\n",
        encoding="utf-8",
    )
    (project / ".env").write_text(
        "DUCTOR_INTERAGENT_HOST=evil.example\nPROJECT_ONLY=from-project\n",
        encoding="utf-8",
    )
    config = CLIConfig(
        working_dir=project,
        ductor_home=home,
        agent_name="developer",
        interagent_port=4321,
    )
    clear_cache()
    env = build_subprocess_env(
        config,
        {"DUCTOR_INTERAGENT_HOST": "extra.invalid", "DUCTOR_HOME": "/wrong"},
    )

    assert env["DUCTOR_INTERAGENT_HOST"] == "127.0.0.1"
    assert env["DUCTOR_INTERAGENT_PORT"] == "4321"
    assert env["DUCTOR_HOME"] == str(home)
    assert env["DUCTOR_AGENT_NAME"] == "developer"
    assert env["BOT_ONLY"] == "from-home"
    assert "PROJECT_ONLY" not in env


def test_workspace_cwd_still_uses_bot_home_env(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / ".env").write_text("BOT_ONLY=workspace-case\n", encoding="utf-8")
    clear_cache()
    env = build_subprocess_env(CLIConfig(working_dir=workspace, ductor_home=tmp_path))
    assert env["BOT_ONLY"] == "workspace-case"
    assert env["DUCTOR_HOME"] == str(tmp_path)


def test_routing_env_only_when_configured_and_overrides_other_sources(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / ".env").write_text(
        "DUCTOR_RESULT_ORIGIN=home-value\nDUCTOR_BROADCAST_REF=home-ref\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DUCTOR_RESULT_ORIGIN", "inherited-value")
    monkeypatch.setenv("DUCTOR_BROADCAST_REF", "inherited-ref")
    clear_cache()

    configured = build_subprocess_env(
        CLIConfig(
            ductor_home=tmp_path,
            result_origin="cron-broadcast",
            broadcast_ref="cron-job-1",
        ),
        {"DUCTOR_RESULT_ORIGIN": "extra-value", "DUCTOR_BROADCAST_REF": "extra-ref"},
    )
    assert configured["DUCTOR_RESULT_ORIGIN"] == "cron-broadcast"
    assert configured["DUCTOR_BROADCAST_REF"] == "cron-job-1"

    default = build_subprocess_env(CLIConfig(ductor_home=tmp_path))
    assert "DUCTOR_RESULT_ORIGIN" not in default
    assert "DUCTOR_BROADCAST_REF" not in default
