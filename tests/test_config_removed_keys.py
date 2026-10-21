"""A config saved before key removal remains parseable."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ductor_bot.config import AgentConfig
from ductor_bot.multiagent.models import SubAgentConfig, merge_sub_agent_config


def test_removed_keys_in_saved_config_are_ignored() -> None:
    old_config = json.loads(
        json.dumps(
            {
                "transport": "telegram",
                "telegram_token": "dummy-token",
                "allowed_user_ids": [123],
                "docker": {"enabled": True, "container_name": "old-sandbox"},
                "update_check": {"enabled": True},
                "notifications": {"upgrade_targets": [123]},
                "cli_parameters": {"antigravity": {"enabled": True}},
            }
        )
    )

    config = AgentConfig.model_validate(old_config)

    assert config.telegram_token == "dummy-token"
    assert config.allowed_user_ids == [123]
    assert "docker" not in config.model_dump()
    assert "update_check" not in config.model_dump()
    assert "upgrade_targets" not in config.notifications.model_dump()
    assert "antigravity" not in config.cli_parameters.model_dump()


@pytest.fixture
def operational_config_shape() -> dict[str, object]:
    """Dummy values with key structure from the operating main and sub configs."""
    return {
        "main": {
            "provider": "claude",
            "model": "dummy-model",
            "ductor_home": "/ductor",
            "transport": "telegram",
            "transports": ["telegram"],
            "telegram_token": "dummy-main-token",
            "allowed_user_ids": [123],
            "heartbeat": {"enabled": False},
            "webhooks": {"enabled": False},
            "cli_parameters": {"claude": [], "antigravity": {"enabled": True}},
            "notifications": {"upgrade_targets": [123]},
            "docker": {"enabled": True},
            "update_check": {"enabled": True},
        },
        "subs": [
            {
                "name": "developer",
                "telegram_token": "dummy-sub-token",
                "allowed_user_ids": [123],
                "allowed_group_ids": [],
                "provider": "claude",
                "model": "dummy-model",
                "heartbeat": {"enabled": False},
                "webhooks": {"enabled": False},
                "cli_timeout": 1800,
                "reasoning_effort": "medium",
            }
        ],
    }


def test_operational_shape_with_removed_keys_is_parseable(
    operational_config_shape: dict[str, object], tmp_path: Path
) -> None:
    """Removed keys are ignored and operating-shaped main/sub structures parse."""
    main_data = operational_config_shape["main"]
    sub_data = operational_config_shape["subs"][0]
    main = AgentConfig.model_validate(main_data)
    sub = SubAgentConfig.model_validate(sub_data)
    merged = merge_sub_agent_config(main, sub, tmp_path / "agents" / sub.name)

    assert main.telegram_token == "dummy-main-token"
    assert sub.telegram_token == "dummy-sub-token"
    assert merged.telegram_token == sub.telegram_token
    assert merged.ductor_home == str(tmp_path / "agents" / sub.name)
    assert "docker" not in main.model_dump()
    assert "update_check" not in main.model_dump()
    assert "upgrade_targets" not in main.notifications.model_dump()
    assert "antigravity" not in main.cli_parameters.model_dump()
