"""Webhook tool validation of execution overrides."""

from __future__ import annotations

import json
import os
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


def test_sub_agent_tools_use_main_config_and_registry_overrides(tmp_path: Path) -> None:
    main_config = tmp_path / "config" / "config.json"
    sub_home = tmp_path / "agents" / "worker"
    sub_config = sub_home / "config" / "config.json"
    for config, timezone, port in (
        (main_config, "Europe/Berlin", 8800),
        (sub_config, "America/New_York", 8801),
    ):
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({
            "user_timezone": timezone,
            "webhooks": {"enabled": True, "port": port, "token": "dummy"},
        }))
    registry_path = tmp_path / "agents.json"
    worker = {"name": "worker", "transport": "headless"}
    override = {
        "user_timezone": "Asia/Tokyo",
        "webhooks": {"enabled": True, "port": 8802},
    }
    for entry, expected in (
        (worker, ["Europe/Berlin", True, 8800, "dummy"]),
        ({**worker, **override}, ["Asia/Tokyo", True, 8802, ""]),
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
            env={**os.environ, "DUCTOR_HOME": str(sub_home)},
            capture_output=True,
            check=True,
            text=True,
        )
        assert json.loads(result.stdout) == expected
