"""Optional dependencies are checked before main or sub-agent startup."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import ModuleType

import pytest

from ductor_bot.config import AgentConfig
from ductor_bot.infra import feature_deps
from ductor_bot.workspace.paths import resolve_paths


def _config(home: Path, **kwargs: object) -> AgentConfig:
    return AgentConfig(ductor_home=str(home), **kwargs)


def _agents(home: Path, entries: list[object]) -> None:
    (home / "agents.json").write_text(json.dumps(entries))


def _imports(monkeypatch: pytest.MonkeyPatch, missing: set[str]) -> None:
    original = importlib.import_module

    def load(name: str, package: str | None = None) -> ModuleType:
        if name in missing:
            raise ImportError(name)
        if name in {"nio", "slack_bolt", "slack_sdk", "nacl.public"}:
            return ModuleType(name)
        return original(name, package)

    monkeypatch.setattr(feature_deps.importlib, "import_module", load)


def test_main_telegram_without_subs_needs_no_extra(tmp_path: Path) -> None:
    assert feature_deps.check_feature_deps(_config(tmp_path), resolve_paths(ductor_home=tmp_path)) == {}


def test_main_matrix_missing_exits_three(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    _imports(monkeypatch, {"nio"})
    monkeypatch.setenv("DUCTOR_BUILD_EXTRAS", "")
    with pytest.raises(SystemExit) as exc:
        feature_deps.check_feature_deps(
            _config(tmp_path, transports=["telegram", "matrix"]),
            resolve_paths(ductor_home=tmp_path),
        )
    assert exc.value.code == 3
    assert "nio" in caplog.text
    assert "DUCTOR_EXTRAS" in caplog.text


@pytest.mark.parametrize(
    ("entry", "missing", "expected"),
    [
        ({"name": "sub", "transport": "matrix"}, {"nio"}, "ValueError"),
        ({"name": "sub", "transports": ["matrix"]}, {"nio"}, "nio"),
        ({"name": "sub", "api": {"enabled": True}}, {"nacl.public"}, "nacl.public"),
        (
            {"name": "sub", "transport": "slack", "allowed_user_ids": "invalid"},
            {"slack_bolt", "slack_sdk"}, "ValidationError",
        ),
        (
            {"name": "sub", "matrix": {
                "homeserver": "https://matrix.invalid", "user_id": "@sub:invalid",
                "password": "synthetic",
            }},
            {"nio"}, "nio",
        ),
    ],
)
def test_sub_declarations_exclude_only_sub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    entry: dict[str, object], missing: set[str], expected: str,
) -> None:
    _imports(monkeypatch, missing)
    _agents(tmp_path, [entry])
    result = feature_deps.check_feature_deps(
        _config(tmp_path), resolve_paths(ductor_home=tmp_path),
    )
    assert set(result) == {"sub"}
    assert expected in result["sub"]
    if expected in {"ValueError", "ValidationError"}:
        assert "item #0" in result["sub"]
        assert "only sub-agent 'sub'" not in result["sub"]
    else:
        assert "only sub-agent 'sub' is excluded" in result["sub"]


def test_missing_matrix_credentials_excludes_sub_without_exiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _imports(monkeypatch, set())
    _agents(tmp_path, [{"name": "sub", "transport": "matrix"}])
    excluded = feature_deps.check_feature_deps(
        _config(tmp_path), resolve_paths(ductor_home=tmp_path),
    )
    assert "sub" in excluded
    assert "ValueError" in excluded["sub"]


def test_main_primary_matrix_missing_exits_three(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _imports(monkeypatch, {"nio"})
    with pytest.raises(SystemExit) as exc:
        feature_deps.check_feature_deps(
            _config(tmp_path, transport="matrix"), resolve_paths(ductor_home=tmp_path),
        )
    assert exc.value.code == 3


def test_import_supplied_by_any_extra_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _imports(monkeypatch, set())
    _agents(tmp_path, [{"name": "sub", "transport": "matrix", "matrix": {
        "homeserver": "https://matrix.invalid", "user_id": "@sub:invalid",
        "password": "synthetic",
    }}])
    assert feature_deps.check_feature_deps(
        _config(tmp_path, api={"enabled": True}),
        resolve_paths(ductor_home=tmp_path),
    ) == {}


@pytest.mark.parametrize("contents", [None, "{broken"])
def test_missing_or_broken_agents_file_checks_main_only(
    tmp_path: Path, contents: str | None,
) -> None:
    if contents is not None:
        (tmp_path / "agents.json").write_text(contents)
    assert feature_deps.check_feature_deps(
        _config(tmp_path), resolve_paths(ductor_home=tmp_path),
    ) == {}


def test_main_api_missing_exits_three(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _imports(monkeypatch, {"nacl.public"})
    with pytest.raises(SystemExit) as exc:
        feature_deps.check_feature_deps(
            _config(tmp_path, api={"enabled": True}),
            resolve_paths(ductor_home=tmp_path),
        )
    assert exc.value.code == 3


def test_start_bot_checks_deps_before_event_loop(
    tmp_path: Path,
) -> None:
    from unittest.mock import patch

    from ductor_bot.cli_commands.lifecycle import start_bot

    paths = resolve_paths(ductor_home=tmp_path)
    config = _config(tmp_path)
    with (
        patch("ductor_bot.cli_commands.lifecycle.resolve_paths", return_value=paths),
        patch("ductor_bot.logging_config.setup_logging"),
        patch("ductor_bot.__main__.load_config", return_value=config),
        patch("ductor_bot.infra.feature_deps.check_feature_deps", side_effect=SystemExit(3)),
        patch("ductor_bot.cli_commands.lifecycle.asyncio.run") as run,
        pytest.raises(SystemExit) as exc,
    ):
        start_bot()
    assert exc.value.code == 3
    run.assert_not_called()


@pytest.mark.parametrize(
    ("entry", "missing", "expected"),
    [
        (
            {
                "name": "sub",
                "matrix": {"homeserver": "https://matrix.invalid", "user_id": "@sub:invalid"},
                "allowed_user_ids": "invalid",
            },
            {"nio"},
            "ValidationError",
        ),
        (
            {"name": "sub", "slack": {}, "allowed_user_ids": "invalid"},
            {"slack_bolt"},
            "ValidationError",
        ),
        (
            {"name": "sub", "allowed_user_ids": "invalid"},
            set(),
            "config error",
        ),
    ],
)
def test_invalid_sub_is_excluded_using_raw_feature_blocks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry: dict[str, object],
    missing: set[str],
    expected: str,
) -> None:
    _imports(monkeypatch, missing)
    _agents(tmp_path, [entry])
    excluded = feature_deps.check_feature_deps(
        _config(tmp_path), resolve_paths(ductor_home=tmp_path),
    )
    assert set(excluded) == {"sub"}
    assert expected in excluded["sub"]
    assert "item #0" in excluded["sub"]
    assert "only sub-agent 'sub'" not in excluded["sub"]


async def test_invalid_matrix_sub_exclusion_notifies_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock, MagicMock, patch

    from ductor_bot.multiagent.supervisor import AgentSupervisor

    _imports(monkeypatch, {"nio"})
    _agents(tmp_path, [{
        "name": "sub",
        "matrix": {"homeserver": "https://matrix.invalid", "user_id": "@sub:invalid"},
        "allowed_user_ids": "invalid",
    }])
    config = _config(tmp_path)
    excluded = feature_deps.check_feature_deps(
        config, resolve_paths(ductor_home=tmp_path),
    )
    supervisor = AgentSupervisor(config, excluded_subs=excluded)
    supervisor._stacks["main"] = MagicMock()
    supervisor._notify_main_agent = AsyncMock()
    with patch(
        "ductor_bot.multiagent.supervisor.AgentStack.create", new_callable=AsyncMock,
    ) as create:
        await supervisor._sync_sub_agents()
    create.assert_not_awaited()
    supervisor._notify_main_agent.assert_awaited_once()
    assert "item #0" in supervisor._notify_main_agent.await_args.args[0]
    assert "allowed_user_ids" in supervisor._notify_main_agent.await_args.args[0]
