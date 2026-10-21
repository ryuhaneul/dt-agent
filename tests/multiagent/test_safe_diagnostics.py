"""agents.json diagnostics never include rejected values or unverified names."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ductor_bot.config import AgentConfig
from ductor_bot.infra import feature_deps
from ductor_bot.multiagent.registry import AgentRegistry
from ductor_bot.multiagent.supervisor import AgentSupervisor
from ductor_bot.workspace.paths import resolve_paths


def _write(home: Path, entries: list[object]) -> None:
    (home / "agents.json").write_text(json.dumps(entries))


async def _notify_for_exclusion(home: Path) -> tuple[dict[str, str], str]:
    config = AgentConfig(ductor_home=str(home))
    excluded = feature_deps.check_feature_deps(config, resolve_paths(ductor_home=home))
    supervisor = AgentSupervisor(config, excluded_subs=excluded)
    supervisor._stacks["main"] = MagicMock()
    supervisor._notify_main_agent = AsyncMock()
    with patch("ductor_bot.multiagent.supervisor.AgentStack.create", new_callable=AsyncMock) as create:
        await supervisor._sync_sub_agents()
    create.assert_not_awaited()
    return excluded, " ".join(call.args[0] for call in supervisor._notify_main_agent.await_args_list)


async def test_bad_token_type_redacts_value_and_reports_field(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "SYNTHETIC_TOKEN_NEVER_LOG"
    _write(tmp_path, [{}, {"name": "sub", "telegram_token": {"token": secret}}])
    excluded, notices = await _notify_for_exclusion(tmp_path)
    assert set(excluded) == {"sub"}
    assert "item #1" in caplog.text
    assert "item #1" in notices
    assert "telegram_token" in caplog.text
    assert "telegram_token" in notices
    assert "ValidationError" in notices
    assert "type=string_type" in notices
    assert secret not in caplog.text + notices
    assert "Sub-agent 'sub' excluded" not in caplog.text + notices
    assert all(record.exc_info is None for record in caplog.records)


async def test_valid_shape_secret_name_is_hidden_on_merge_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    secret_name = "a" * 40
    _write(tmp_path, [{}, {"name": secret_name, "transport": "matrix"}])
    excluded, notices = await _notify_for_exclusion(tmp_path)
    assert set(excluded) == {secret_name}
    assert "item #1" in caplog.text + notices
    assert "ValueError" in caplog.text + notices
    assert secret_name not in caplog.text + notices


async def test_invalid_character_name_is_hidden_on_validation_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    secret_name = "synthetic:secret"
    _write(tmp_path, [{}, {"name": secret_name, "allowed_user_ids": "invalid"}])
    excluded, notices = await _notify_for_exclusion(tmp_path)
    assert set(excluded) == {secret_name}
    assert "item #1" in caplog.text + notices
    assert "allowed_user_ids" in caplog.text + notices
    assert secret_name not in caplog.text + notices


@pytest.mark.parametrize("secret_name", ["synthetic:secret", "synthetic..secret"])
async def test_hot_reload_hides_invalid_name_before_and_after_check(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, secret_name: str,
) -> None:
    caplog.set_level("INFO")
    supervisor = AgentSupervisor(AgentConfig(ductor_home=str(tmp_path)))
    supervisor._stacks["main"] = MagicMock()
    supervisor._notify_main_agent = AsyncMock()
    await supervisor._on_agents_changed()
    _write(tmp_path, [{"name": secret_name}])
    with patch("ductor_bot.multiagent.supervisor.AgentStack.create", new_callable=AsyncMock) as create:
        await supervisor._on_agents_changed()
    create.assert_not_awaited()
    assert set(supervisor._excluded) == {secret_name}
    notices = " ".join(call.args[0] for call in supervisor._notify_main_agent.await_args_list)
    assert "new agent 'item #0' detected" in caplog.text
    assert "Sub-agent 'item #0' excluded" in caplog.text + notices
    assert secret_name not in caplog.text + notices


async def test_hot_reload_valid_name_merge_failure_hides_name(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO")
    secret_name = "a" * 40
    _write(tmp_path, [{"name": secret_name}])
    supervisor = AgentSupervisor(AgentConfig(ductor_home=str(tmp_path)))
    supervisor._stacks["main"] = MagicMock()
    supervisor._notify_main_agent = AsyncMock()
    with (
        patch(
            "ductor_bot.multiagent.supervisor.merge_sub_agent_config",
            side_effect=ValueError("synthetic rejected value"),
        ),
        patch("ductor_bot.multiagent.supervisor.AgentStack.create", new_callable=AsyncMock) as create,
    ):
        await supervisor._on_agents_changed()
    create.assert_not_awaited()
    assert set(supervisor._excluded) == {secret_name}
    notices = " ".join(call.args[0] for call in supervisor._notify_main_agent.await_args_list)
    assert "new agent 'item #0' detected" in caplog.text
    assert "Sub-agent 'item #0' excluded" in caplog.text + notices
    assert secret_name not in caplog.text + notices
    assert "synthetic rejected value" not in caplog.text + notices


async def test_valid_name_is_kept_for_missing_dependency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write(tmp_path, [{
        "name": "matrix-sub", "transport": "matrix",
        "matrix": {
            "homeserver": "https://matrix.invalid", "user_id": "@sub:invalid",
            "password": "synthetic",
        },
    }])
    monkeypatch.setattr(
        feature_deps, "_missing_for_features",
        lambda features: ["nio"] if "matrix" in features else [],
    )
    excluded, notices = await _notify_for_exclusion(tmp_path)
    assert set(excluded) == {"matrix-sub"}
    assert "Sub-agent 'matrix-sub' excluded" in notices
    assert "nio" in notices


def test_valid_registry_item_still_loads_and_keeps_index(tmp_path: Path) -> None:
    _write(tmp_path, [{}, {"name": "valid-sub", "transport": "headless"}])
    registry = AgentRegistry(tmp_path / "agents.json")
    assert [sub.name for sub in registry.load()] == ["valid-sub"]
    assert registry.index_for("valid-sub") == 1


def test_error_location_does_not_expose_unknown_key() -> None:
    from pydantic import BaseModel, ConfigDict, ValidationError

    from ductor_bot.infra.agent_diagnostics import safe_error

    class StrictModel(BaseModel):
        model_config = ConfigDict(extra="forbid")

    secret = "b" * 40
    with pytest.raises(ValidationError) as failure:
        StrictModel.model_validate({secret: "synthetic"})
    message = safe_error(2, failure.value)
    assert "item #2" in message
    assert "loc=<field>" in message
    assert secret not in message


async def test_invalid_name_precheck_keeps_matching_key_and_safe_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    secret_name = "synthetic..secret"
    _write(tmp_path, [{
        "name": secret_name, "transport": "matrix",
        "matrix": {
            "homeserver": "https://matrix.invalid", "user_id": "@sub:invalid",
            "password": "synthetic",
        },
    }])
    monkeypatch.setattr(
        feature_deps, "_missing_for_features",
        lambda features: ["nio"] if "matrix" in features else [],
    )
    excluded, notices = await _notify_for_exclusion(tmp_path)
    assert set(excluded) == {secret_name}
    assert "item #0" in notices
    assert secret_name not in caplog.text + notices


@pytest.mark.parametrize("named", [False, True], ids=["unnamed", "named"])
def test_registry_validation_error_visible_at_error_level(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, named: bool,
) -> None:
    secret = "SYNTHETIC_ERROR_LEVEL_TOKEN"
    secret_name = "c" * 40
    entry: dict[str, object] = {"telegram_token": {"token": secret}}
    if named:
        entry["name"] = secret_name
    _write(tmp_path, [entry, {"name": "valid-sub", "transport": "headless"}])
    config = AgentConfig(ductor_home=str(tmp_path), log_level="ERROR")
    with caplog.at_level(config.log_level):
        registry = AgentRegistry(tmp_path / "agents.json")
        assert [sub.name for sub in registry.load()] == ["valid-sub"]
    errors = [record for record in caplog.records if record.name.endswith("registry")]
    assert len(errors) == 1
    assert errors[0].levelname == "ERROR"
    assert errors[0].exc_info is None
    assert "item #0" in caplog.text
    assert "ValidationError" in caplog.text
    assert "loc=telegram_token type=string_type" in caplog.text
    assert secret not in caplog.text
    assert secret_name not in caplog.text


@pytest.mark.parametrize(
    ("declaration", "feature", "modules"),
    [
        ({"transport": "matrix"}, "matrix", {"nio"}),
        ({"transports": ["matrix"]}, "matrix", {"nio"}),
        ({"transports": ["matrix", {}]}, "matrix", {"nio"}),
        ({"api": {"enabled": True}}, "api", {"nacl.public"}),
        ({"matrix": {"homeserver": {"token": "SYNTHETIC_FIELD_VALUE"}}}, "matrix", {"nio"}),
        ({"transport": "matrix", "matrix": "SYNTHETIC_BLOCK_VALUE"}, "matrix", {"nio"}),
        ({"slack": {}}, "slack", {"slack_bolt", "slack_sdk"}),
    ],
)
async def test_invalid_sub_reports_declared_dependencies_and_validation_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    *, declaration: dict[str, object], feature: str, modules: set[str],
) -> None:
    from types import ModuleType

    original = feature_deps.importlib.import_module

    def load(name: str, package: str | None = None) -> ModuleType:
        if name in modules:
            raise ImportError("SYNTHETIC_IMPORT_ERROR")
        if name in {"nio", "slack_bolt", "slack_sdk", "nacl.public"}:
            return ModuleType(name)
        return original(name, package)

    monkeypatch.setattr(feature_deps.importlib, "import_module", load)
    monkeypatch.setenv("DUCTOR_BUILD_EXTRAS", "")
    secret_name = "d" * 40
    _write(tmp_path, [{}, {
        "name": secret_name, "telegram_token": {"token": "SYNTHETIC_BAD_TOKEN"},
        **declaration,
    }])
    with caplog.at_level("ERROR"):
        excluded, notices = await _notify_for_exclusion(tmp_path)
    assert set(excluded) == {secret_name}
    for output in (excluded[secret_name], caplog.text, notices):
        assert "item #1" in output
        assert "ValidationError" in output
        assert "loc=telegram_token type=string_type" in output
        assert f"feature {feature} is on" in output
        assert "DUCTOR_EXTRAS" in output
        if isinstance(declaration.get("matrix"), dict):
            assert "loc=matrix.homeserver type=string_type" in output
        for module in modules:
            assert f"module {module} is missing" in output
        for secret in (
            secret_name, "SYNTHETIC_BAD_TOKEN", "SYNTHETIC_FIELD_VALUE",
            "SYNTHETIC_BLOCK_VALUE", "SYNTHETIC_IMPORT_ERROR",
        ):
            assert secret not in output
    assert all(record.exc_info is None for record in caplog.records)


async def test_merge_failure_keeps_declared_dependency_hint_and_safe_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(
        feature_deps, "_missing_for_features",
        lambda features: ["nio"] if "matrix" in features else [],
    )
    secret_name = "e" * 40
    _write(tmp_path, [{"name": secret_name, "transport": "matrix"}])
    excluded, notices = await _notify_for_exclusion(tmp_path)
    for output in (excluded[secret_name], caplog.text, notices):
        assert "item #0" in output
        assert "ValueError" in output
        assert "module nio is missing" in output
        assert secret_name not in output


@pytest.mark.parametrize(
    ("feature", "modules"),
    [("matrix", {"nio"}), ("slack", {"slack_bolt", "slack_sdk"})],
)
async def test_null_feature_block_reports_validation_without_dependency_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    feature: str, modules: set[str],
) -> None:
    from types import ModuleType

    original = feature_deps.importlib.import_module
    requested: list[str] = []

    def load(name: str, package: str | None = None) -> ModuleType:
        if name in modules:
            requested.append(name)
            raise ImportError("SYNTHETIC_IMPORT_ERROR")
        if name in {"nio", "slack_bolt", "slack_sdk", "nacl.public"}:
            return ModuleType(name)
        return original(name, package)

    monkeypatch.setattr(feature_deps.importlib, "import_module", load)
    secret_name = "f" * 40
    _write(tmp_path, [{}, {
        "name": secret_name, feature: None, "allowed_user_ids": ["SYNTHETIC_BAD_ID"],
    }])
    with caplog.at_level("ERROR"):
        excluded, notices = await _notify_for_exclusion(tmp_path)
    assert set(excluded) == {secret_name}
    assert requested == []
    for output in (excluded[secret_name], caplog.text, notices):
        assert "item #1" in output
        assert "ValidationError" in output
        assert "loc=allowed_user_ids.0 type=int_parsing" in output
        assert "feature " not in output
        assert "module " not in output
        assert "DUCTOR_EXTRAS" not in output
        assert secret_name not in output
        assert "SYNTHETIC_BAD_ID" not in output
        assert "SYNTHETIC_IMPORT_ERROR" not in output
    assert all(record.exc_info is None for record in caplog.records)
