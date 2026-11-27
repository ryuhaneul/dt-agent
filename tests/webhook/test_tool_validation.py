"""Webhook tool validation of execution overrides."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from ductor_bot._home_defaults.workspace.tools.webhook_tools._shared import (
    reject_wake_overrides,
)


@pytest.mark.parametrize(
    "override",
    [
        {"provider": "codex"},
        {"model": "gpt-5.5"},
        {"reasoning_effort": "high"},
        {"cli_parameters": '["--chrome"]'},
        {"cli_parameters": ["--chrome"]},
    ],
)
def test_wake_with_any_override_is_rejected(override: dict[str, object]) -> None:
    kwargs: dict[str, object] = {
        "provider": None,
        "model": None,
        "reasoning_effort": None,
        "cli_parameters": None,
    }
    kwargs.update(override)
    error = reject_wake_overrides("wake", **kwargs)  # type: ignore[arg-type]
    assert error is not None
    assert "wake" in error
    assert "cron_task" in error


def test_wake_without_overrides_passes() -> None:
    assert (
        reject_wake_overrides(
            "wake", provider=None, model=None, reasoning_effort=None, cli_parameters=None
        )
        is None
    )


def test_cron_task_with_overrides_passes() -> None:
    assert (
        reject_wake_overrides(
            "cron_task",
            provider="codex",
            model="gpt-5.5",
            reasoning_effort="high",
            cli_parameters=["--chrome"],
        )
        is None
    )


def test_error_names_the_offending_flags() -> None:
    error = reject_wake_overrides(
        "wake", provider="codex", model=None, reasoning_effort=None, cli_parameters=None
    )
    assert error is not None
    assert "--provider" in error
    assert "--model" not in error


def test_shared_save_replaces_the_file_atomically_without_a_lock_file(tmp_path) -> None:
    from ductor_bot._home_defaults.workspace.tools._tool_shared import save_collection

    path = tmp_path / "webhooks.json"
    save_collection(path, {"hooks": [{"id": "a"}]})
    first_inode = path.stat().st_ino
    save_collection(path, {"hooks": [{"id": "a"}, {"id": "b"}]})

    assert json.loads(path.read_text(encoding="utf-8"))["hooks"][1] == {"id": "b"}
    assert path.stat().st_ino != first_inode  # replaced by a rename, not rewritten in place
    assert [p.name for p in tmp_path.iterdir()] == ["webhooks.json"]  # no temp file, no lock


def test_tools_use_launcher_identity_for_linked_sub_and_standalone_main(tmp_path: Path) -> None:
    from ductor_bot.cli.base import CLIConfig
    from ductor_bot.cli.executor import build_subprocess_env

    main_home = tmp_path / "main"
    physical_home = tmp_path / "physical-home"
    sub_home = main_home / "agents" / "worker"
    standalone = tmp_path / "other" / "agents" / "standalone"
    for home, timezone, port in (
        (main_home, "Europe/Berlin", 8800),
        (physical_home, "America/New_York", 8801),
        (standalone, "Etc/UTC", 8803),
    ):
        config = home / "config" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({
            "user_timezone": timezone,
            "webhooks": {"enabled": True, "port": port, "token": "dummy"},
        }))
    sub_home.parent.mkdir()
    sub_home.symlink_to(physical_home, target_is_directory=True)
    registry_path = main_home / "agents.json"
    worker = {"name": "worker", "transport": "headless"}
    override = {
        "user_timezone": "Asia/Tokyo",
        "webhooks": {"enabled": True, "port": 8802},
    }
    for cli, entry, expected in (
        (
            CLIConfig(ductor_home=sub_home.resolve(), agent_name="worker", main_home=main_home),
            worker, ["Europe/Berlin", True, 8800, "dummy"],
        ),
        (
            CLIConfig(ductor_home=sub_home.resolve(), agent_name="worker", main_home=main_home),
            {**worker, **override}, ["Asia/Tokyo", True, 8802, ""],
        ),
        (CLIConfig(ductor_home=standalone), worker, ["Etc/UTC", True, 8803, "dummy"]),
    ):
        registry_path.write_text(json.dumps([{"name": "other", **override}, entry]))
        result = subprocess.run(
            [
                sys.executable, "-c",
                "import json; "
                "from ductor_bot._home_defaults.workspace.tools.cron_tools._shared "
                "import read_user_timezone; "
                "from ductor_bot._home_defaults.workspace.tools.webhook_tools._shared "
                "import load_webhook_config; "
                "webhooks = load_webhook_config(); "
                "print(json.dumps([read_user_timezone(), webhooks['enabled'], "
                "webhooks['port'], webhooks['token']]))",
            ],
            env=build_subprocess_env(cli),
            capture_output=True,
            check=True,
            text=True,
        )
        assert json.loads(result.stdout) == expected
