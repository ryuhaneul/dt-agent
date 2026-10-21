"""Webhook tool validation of execution overrides."""

from __future__ import annotations

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
    import json

    from ductor_bot._home_defaults.workspace.tools._tool_shared import save_collection

    path = tmp_path / "webhooks.json"
    save_collection(path, {"hooks": [{"id": "a"}]})
    first_inode = path.stat().st_ino
    save_collection(path, {"hooks": [{"id": "a"}, {"id": "b"}]})

    assert json.loads(path.read_text(encoding="utf-8"))["hooks"][1] == {"id": "b"}
    assert path.stat().st_ino != first_inode  # replaced by a rename, not rewritten in place
    assert [p.name for p in tmp_path.iterdir()] == ["webhooks.json"]  # no temp file, no lock
