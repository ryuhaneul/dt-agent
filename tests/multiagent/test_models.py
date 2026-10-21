"""Tests for multiagent/models.py: SubAgentConfig and merge_sub_agent_config."""

from __future__ import annotations

from pathlib import Path

import pytest

from ductor_bot.config import AgentConfig, ApiConfig
from ductor_bot.multiagent.models import SubAgentConfig, merge_sub_agent_config


class TestSubAgentConfig:
    """Test SubAgentConfig model validation and defaults."""

    def test_minimal_config(self) -> None:
        cfg = SubAgentConfig(name="sub1", telegram_token="tok:123")
        assert cfg.name == "sub1"
        assert cfg.telegram_token == "tok:123"
        assert cfg.allowed_user_ids is None
        assert cfg.allowed_group_ids is None
        assert cfg.provider is None
        assert cfg.model is None

    def test_with_overrides(self) -> None:
        cfg = SubAgentConfig(
            name="sub1",
            telegram_token="tok:123",
            allowed_user_ids=[100, 200],
            provider="codex",
            model="gpt-4",
        )
        assert cfg.allowed_user_ids == [100, 200]
        assert cfg.provider == "codex"
        assert cfg.model == "gpt-4"

    def test_model_dump_excludes_none(self) -> None:
        cfg = SubAgentConfig(name="sub1", telegram_token="tok:123")
        dumped = cfg.model_dump(exclude_none=True)
        assert "provider" not in dumped
        assert "model" not in dumped
        assert "allowed_user_ids" not in dumped


class TestMergeSubAgentConfig:
    """Test merge_sub_agent_config behavior."""

    def _main_config(self) -> AgentConfig:
        return AgentConfig(
            provider="claude",
            model="opus",
            allowed_user_ids=[1, 2, 3],
            ductor_home="/main/home",
            cli_timeout=600,
            telegram_token="main-token",
        )

    def test_inherits_from_main(self) -> None:
        """Sub-agent inherits main config values when not overridden."""
        main = self._main_config()
        sub = SubAgentConfig(name="sub1", telegram_token="sub-token")
        result = merge_sub_agent_config(main, sub, Path("/agents/sub1"))

        assert result.provider == "claude"
        assert result.model == "opus"
        assert result.cli_timeout == 600

    def test_overrides_from_sub(self) -> None:
        """Sub-agent overrides take precedence over main config."""
        main = self._main_config()
        sub = SubAgentConfig(
            name="sub1",
            telegram_token="sub-token",
            provider="codex",
            model="gpt-4",
            cli_timeout=300,
        )
        result = merge_sub_agent_config(main, sub, Path("/agents/sub1"))

        assert result.provider == "codex"
        assert result.model == "gpt-4"
        assert result.cli_timeout == 300

    def test_ductor_home_always_set_to_agent_home(self) -> None:
        """ductor_home is always the agent's home dir, not main's."""
        main = self._main_config()
        sub = SubAgentConfig(name="sub1", telegram_token="sub-token")
        result = merge_sub_agent_config(main, sub, Path("/agents/sub1"))

        assert result.ductor_home == "/agents/sub1"

    def test_telegram_token_always_from_sub(self) -> None:
        """Telegram token always comes from sub-agent definition."""
        main = self._main_config()
        sub = SubAgentConfig(name="sub1", telegram_token="sub-token")
        result = merge_sub_agent_config(main, sub, Path("/agents/sub1"))

        assert result.telegram_token == "sub-token"

    def test_allowed_user_ids_from_sub(self) -> None:
        """Sub-agent's allowed_user_ids override main config."""
        main = self._main_config()
        sub = SubAgentConfig(
            name="sub1",
            telegram_token="sub-token",
            allowed_user_ids=[100, 200],
        )
        result = merge_sub_agent_config(main, sub, Path("/agents/sub1"))

        assert result.allowed_user_ids == [100, 200]

    def test_allowed_user_ids_none_uses_empty_list(self) -> None:
        """When sub-agent has no allowed_user_ids (None), result is empty list."""
        main = self._main_config()
        sub = SubAgentConfig(name="sub1", telegram_token="sub-token")
        result = merge_sub_agent_config(main, sub, Path("/agents/sub1"))

        # allowed_user_ids=None in sub -> fallback to empty list
        assert result.allowed_user_ids == []

    def test_allowed_user_ids_none_does_not_inherit_main(self) -> None:
        """Sub-agent with allowed_user_ids=None should NOT inherit main's users.

        This is intentional: sub-agents need explicit user lists for security.
        """
        main = self._main_config()
        assert main.allowed_user_ids == [1, 2, 3]

        sub = SubAgentConfig(name="sub1", telegram_token="sub-token")
        result = merge_sub_agent_config(main, sub, Path("/agents/sub1"))

        # Should be empty, NOT [1, 2, 3] from main
        assert result.allowed_user_ids == []

    def test_allowed_group_ids_from_sub(self) -> None:
        """Sub-agent's allowed_group_ids override main config."""
        main = self._main_config()
        main.allowed_group_ids = [-1001, -1002]
        sub = SubAgentConfig(
            name="sub1",
            telegram_token="sub-token",
            allowed_group_ids=[-2001],
        )
        result = merge_sub_agent_config(main, sub, Path("/agents/sub1"))

        assert result.allowed_group_ids == [-2001]

    def test_allowed_group_ids_none_uses_empty_list(self) -> None:
        """When sub-agent has no allowed_group_ids (None), result is empty list."""
        main = self._main_config()
        main.allowed_group_ids = [-1001]
        sub = SubAgentConfig(name="sub1", telegram_token="sub-token")
        result = merge_sub_agent_config(main, sub, Path("/agents/sub1"))

        # Should be empty, NOT [-1001] from main (same pattern as allowed_user_ids)
        assert result.allowed_group_ids == []

    def test_partial_overrides(self) -> None:
        """Only specified fields override, rest inherit from main."""
        main = self._main_config()
        sub = SubAgentConfig(
            name="sub1",
            telegram_token="sub-token",
            model="sonnet",
        )
        result = merge_sub_agent_config(main, sub, Path("/agents/sub1"))

        assert result.model == "sonnet"
        assert result.provider == "claude"  # inherited

    def test_api_disabled_when_not_overridden(self) -> None:
        """Sub-agent without explicit api config gets api.enabled=False."""
        main = self._main_config()
        main.api = ApiConfig(enabled=True, port=8741)
        sub = SubAgentConfig(name="sub1", telegram_token="sub-token")
        result = merge_sub_agent_config(main, sub, Path("/agents/sub1"))

        assert result.api.enabled is False
        assert result.api.port == 8741  # rest inherited

    def test_api_preserved_when_explicitly_overridden(self) -> None:
        """Sub-agent with explicit api config keeps their settings."""
        main = self._main_config()
        main.api = ApiConfig(enabled=True, port=8741)
        sub = SubAgentConfig(
            name="sub1",
            telegram_token="sub-token",
            api=ApiConfig(enabled=True, port=8742),
        )
        result = merge_sub_agent_config(main, sub, Path("/agents/sub1"))

        assert result.api.enabled is True
        assert result.api.port == 8742


def test_receipt_notification_default_disabled() -> None:
    assert AgentConfig().interagent_notify_received is False
    assert SubAgentConfig(name="worker").interagent_notify_received is None


@pytest.mark.parametrize("main_enabled", [False, True])
@pytest.mark.parametrize("override", [None, False, True])
def test_receipt_notification_inheritance_and_override(
    main_enabled: bool, override: bool | None, tmp_path: Path
) -> None:
    main = AgentConfig(interagent_notify_received=main_enabled)
    data = {"name": "worker"}
    if override is not None:
        data["interagent_notify_received"] = override
    # Use the same model-validation/merge path as agents.json definitions.
    sub = SubAgentConfig.model_validate(data)
    merged = merge_sub_agent_config(main, sub, tmp_path / "worker")
    assert merged.interagent_notify_received is (main_enabled if override is None else override)
    assert main.interagent_notify_received is main_enabled


# -- memory_lint is never inherited from main --------------------------------------------------

_LINT_DEFAULTS = {
    "mode": "structure",
    "every_hours": 168,
    "schedule": None,
    "report": "topic",
    "provider": None,
    "model": None,
    "reasoning_effort": None,
}


def _merged(main: AgentConfig, tmp_path: Path, **sub_fields: object) -> AgentConfig:
    sub = SubAgentConfig.model_validate({"name": "worker", **sub_fields})
    return merge_sub_agent_config(main, sub, tmp_path / "worker")


@pytest.mark.parametrize(
    "main_lint",
    [
        {"mode": "full"},
        {"mode": "off"},
        {"report": "off", "every_hours": 1},
        {"mode": "full", "schedule": "0 9 * * 1", "model": "haiku", "provider": "claude"},
    ],
)
def test_sub_without_memory_lint_uses_the_framework_defaults(
    main_lint: dict[str, object], tmp_path: Path
) -> None:
    main = AgentConfig(memory_lint=main_lint)
    merged = _merged(main, tmp_path)
    assert merged.memory_lint.model_dump() == _LINT_DEFAULTS
    assert main.memory_lint.model_dump() != _LINT_DEFAULTS  # main itself is unchanged


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({"mode": "full"}, {"mode": "full"}),
        ({"mode": "off"}, {"mode": "off"}),
        ({"enabled": False}, {"mode": "off"}),
        ({"enabled": False, "mode": "full"}, {"mode": "full"}),
        ({"enabled": True}, {}),
        (
            {"schedule": "0 9 * * 1", "model": "haiku", "reasoning_effort": "low"},
            {"schedule": "0 9 * * 1", "model": "haiku", "reasoning_effort": "low"},
        ),
        ({"report": "dm", "every_hours": 24}, {"report": "dm", "every_hours": 24}),
        ({}, {}),
    ],
)
def test_sub_memory_lint_override_is_partial_on_top_of_the_defaults(
    override: dict[str, object], expected: dict[str, object], tmp_path: Path
) -> None:
    # main differs in every field: nothing of it may leak into the sub
    main = AgentConfig(
        memory_lint={
            "mode": "full",
            "every_hours": 5,
            "schedule": "1 2 * * *",
            "report": "off",
            "provider": "codex",
            "model": "gpt",
            "reasoning_effort": "high",
        }
    )
    merged = _merged(main, tmp_path, memory_lint=override)
    assert merged.memory_lint.model_dump() == {**_LINT_DEFAULTS, **expected}
    assert main.memory_lint.mode == "full"


def test_main_full_with_a_full_sub_override_only_changes_that_sub(tmp_path: Path) -> None:
    main = AgentConfig(memory_lint={"mode": "full"})
    plain = _merged(main, tmp_path)
    lint_sub = _merged(main, tmp_path, memory_lint={"mode": "full"})
    assert (plain.memory_lint.mode, lint_sub.memory_lint.mode) == ("structure", "full")


def test_invalid_sub_memory_lint_is_rejected_like_other_fields(tmp_path: Path) -> None:
    import json

    from pydantic import ValidationError

    from ductor_bot.multiagent.registry import AgentRegistry

    with pytest.raises(ValidationError):
        SubAgentConfig.model_validate({"name": "worker", "memory_lint": {"mode": "weekly"}})
    path = tmp_path / "agents.json"
    path.write_text(
        json.dumps(
            [
                {"name": "ok", "telegram_token": "tok:1", "memory_lint": {"mode": "full"}},
                {"name": "bad", "telegram_token": "tok:2", "memory_lint": {"mode": "weekly"}},
            ]
        )
    )
    loaded = AgentRegistry(path).load()
    assert [(a.name, a.memory_lint.mode if a.memory_lint else None) for a in loaded] == [
        ("ok", "full")
    ]


def test_sub_without_override_serializes_without_memory_lint() -> None:
    assert "memory_lint" not in SubAgentConfig(name="worker").model_dump(exclude_none=True)


async def test_the_sub_override_survives_a_change_of_its_config_file(tmp_path: Path) -> None:
    """The supervisor persists the effective value; a later file change must not revert it."""
    import json

    from ductor_bot.config import update_config_file
    from ductor_bot.config_reload import ConfigReloader

    main = AgentConfig(memory_lint={"mode": "off"})
    merged = _merged(main, tmp_path, memory_lint={"mode": "full"})
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(AgentConfig().model_dump(mode="json")))
    update_config_file(config_path, memory_lint=merged.memory_lint.model_dump(mode="json"))
    reloader = ConfigReloader(config_path, merged)
    reloader._last_mtime = 0.0  # the file changes after the reloader started
    update_config_file(config_path, model="haiku")  # e.g. a /model change in the sub chat
    await reloader._check()
    assert merged.memory_lint.mode == "full"
