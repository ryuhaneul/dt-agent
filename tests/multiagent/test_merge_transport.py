"""Sub-agent transport inference and Matrix store isolation."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ductor_bot.config import (
    AgentConfig,
    MatrixConfig,
    matrix_credentials_complete,
    safe_matrix_store,
)
from ductor_bot.multiagent.models import (
    SubAgentConfig,
    merge_sub_agent_config,
    resolve_sub_transports,
)
from ductor_bot.multiagent.supervisor import AgentSupervisor


def _matrix(**overrides: str) -> MatrixConfig:
    return MatrixConfig(homeserver="https://matrix.invalid", user_id="@sub:invalid", **overrides)


@pytest.mark.parametrize(
    ("matrix", "expected"),
    [
        ({}, ["headless"]),
        ({"homeserver": "https://matrix.invalid", "user_id": "@sub:invalid"}, ["headless"]),
        (
            {
                "homeserver": "https://matrix.invalid",
                "user_id": "@sub:invalid",
                "access_token": "dummy",
                "device_id": "DEVICE",
            },
            ["matrix"],
        ),
        (
            {
                "homeserver": "https://matrix.invalid",
                "user_id": "@sub:invalid",
                "access_token": "dummy",
            },
            ["headless"],
        ),
        (
            {
                "homeserver": "https://matrix.invalid",
                "user_id": "@sub:invalid",
                "password": "dummy",
            },
            ["matrix"],
        ),
    ],
)
def test_matrix_config_credential_completeness(
    tmp_path: Path, matrix: dict[str, str], expected: list[str]
) -> None:
    sub = SubAgentConfig(name="sub", matrix=matrix)
    assert resolve_sub_transports(sub, tmp_path / "sub") == expected
    assert matrix_credentials_complete(sub.matrix) is (expected == ["matrix"])


def test_saved_matrix_credentials_enable_transport(tmp_path: Path) -> None:
    home = tmp_path / "sub"
    store = home / "matrix_store"
    store.mkdir(parents=True)
    (store / "credentials.json").write_text(
        json.dumps({"user_id": "@sub:invalid", "device_id": "DEVICE", "access_token": "dummy"})
    )
    sub = SubAgentConfig(name="sub", matrix=_matrix())
    assert resolve_sub_transports(sub, home) == ["matrix"]


@pytest.mark.parametrize(
    "contents",
    [
        "{bad json",
        json.dumps({"user_id": "@sub:invalid", "device_id": "DEVICE"}),
        json.dumps({"user_id": "", "device_id": "DEVICE", "access_token": "dummy"}),
    ],
)
def test_incomplete_saved_matrix_credentials_are_ignored(tmp_path: Path, contents: str) -> None:
    home = tmp_path / "sub"
    store = home / "matrix_store"
    store.mkdir(parents=True)
    (store / "credentials.json").write_text(contents)
    sub = SubAgentConfig(name="sub", matrix=_matrix())
    assert resolve_sub_transports(sub, home) == ["headless"]


def test_saved_matrix_credentials_for_main_account_are_ignored(tmp_path: Path) -> None:
    home = tmp_path / "sub"
    store = home / "matrix_store"
    store.mkdir(parents=True)
    (store / "credentials.json").write_text(
        json.dumps({"user_id": "@main:invalid", "device_id": "DEVICE", "access_token": "dummy"})
    )
    sub = SubAgentConfig(name="sub", matrix=_matrix())
    assert resolve_sub_transports(sub, home) == ["headless"]
    with pytest.raises(ValueError, match="no usable credentials"):
        resolve_sub_transports(
            SubAgentConfig(name="sub", transport="matrix", matrix=_matrix()), home
        )


def test_saved_matrix_credentials_for_other_homeserver_are_ignored(tmp_path: Path) -> None:
    home = tmp_path / "sub"
    store = home / "matrix_store"
    store.mkdir(parents=True)
    (store / "credentials.json").write_text(
        json.dumps(
            {
                "user_id": "@sub:invalid",
                "device_id": "DEVICE",
                "access_token": "dummy",
                "homeserver": "https://other.invalid",
            }
        )
    )
    assert resolve_sub_transports(SubAgentConfig(name="sub", matrix=_matrix()), home) == [
        "headless"
    ]


def test_custom_matrix_store_is_used(tmp_path: Path) -> None:
    home = tmp_path / "sub"
    store = home / "custom" / "store"
    store.mkdir(parents=True)
    (store / "credentials.json").write_text(
        json.dumps({"user_id": "@sub:invalid", "device_id": "DEVICE", "access_token": "dummy"})
    )
    sub = SubAgentConfig(name="sub", matrix=_matrix(store_path="custom/store"))
    assert resolve_sub_transports(sub, home) == ["matrix"]


@pytest.mark.parametrize("path", ["../../matrix_store", "/tmp/x"])
def test_matrix_store_rejects_path_escape(tmp_path: Path, path: str) -> None:
    home = tmp_path / "agents" / "sub"
    with pytest.raises(ValueError, match="store_path"):
        safe_matrix_store(home, path)
    with pytest.raises(ValueError, match="store_path"):
        resolve_sub_transports(
            SubAgentConfig(name="sub", matrix=_matrix(password="dummy", store_path=path)),
            home,
        )


def test_matrix_store_rejects_symlink(tmp_path: Path) -> None:
    home = tmp_path / "agents" / "sub"
    home.mkdir(parents=True)
    main_store = tmp_path / "matrix_store"
    main_store.mkdir()
    (home / "linked").symlink_to(main_store, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinks"):
        safe_matrix_store(home, "linked/session")
    assert safe_matrix_store(home, "matrix_store") == home / "matrix_store"


def test_matrix_bot_rejects_unsafe_main_store(tmp_path: Path) -> None:
    from ductor_bot.messenger.matrix.bot import MatrixBot

    nio = ModuleType("nio")
    nio.AsyncClient = MagicMock()  # type: ignore[attr-defined]
    config = AgentConfig(
        ductor_home=str(tmp_path / "main"),
        matrix=_matrix(password="dummy", store_path="../outside"),
    )
    with (
        patch.dict(sys.modules, {"nio": nio}),
        pytest.raises(ValueError, match="store_path"),
    ):
        MatrixBot(config)


def test_explicit_transport_requires_its_own_credentials(tmp_path: Path) -> None:
    home = tmp_path / "sub"
    with pytest.raises(ValueError, match="matrix"):
        resolve_sub_transports(
            SubAgentConfig(name="sub", transport="matrix", matrix=_matrix(access_token="dummy")),
            home,
        )
    with pytest.raises(ValueError, match="telegram"):
        resolve_sub_transports(SubAgentConfig(name="sub", transport="telegram"), home)
    with pytest.raises(ValueError, match="slack"):
        resolve_sub_transports(SubAgentConfig(name="sub", transport="slack"), home)
    assert resolve_sub_transports(
        SubAgentConfig(
            name="sub",
            transport="headless",
            matrix=_matrix(store_path="../../unused"),
        ),
        home,
    ) == ["headless"]


def test_auto_transport_selection_and_merge(tmp_path: Path) -> None:
    home = tmp_path / "sub"
    main = AgentConfig(
        ductor_home=str(tmp_path / "main"),
        transports=["telegram", "matrix"],
        telegram_token="main-token",
        matrix=_matrix(password="main-password"),
    )
    telegram = SubAgentConfig(name="sub", telegram_token="sub-token")
    matrix = SubAgentConfig(name="sub", matrix=_matrix(password="dummy"))
    both = SubAgentConfig(name="sub", telegram_token="sub-token", matrix=_matrix(password="dummy"))
    none = SubAgentConfig(name="sub")

    assert resolve_sub_transports(telegram, home) == ["telegram"]
    assert resolve_sub_transports(matrix, home) == ["matrix"]
    assert resolve_sub_transports(both, home) == ["telegram", "matrix"]
    assert resolve_sub_transports(none, home) == ["headless"]
    assert merge_sub_agent_config(main, matrix, home).transport == "matrix"
    assert merge_sub_agent_config(main, telegram, home).transports == ["telegram"]
    assert merge_sub_agent_config(main, telegram, home).telegram_token == "sub-token"
    assert merge_sub_agent_config(main, both, home).transport == "telegram"
    assert merge_sub_agent_config(main, none, home).transports == ["headless"]


def test_operational_telegram_sub_agents_keep_previous_shape(tmp_path: Path) -> None:
    main = AgentConfig(
        ductor_home=str(tmp_path),
        transports=["telegram"],
        telegram_token="main-token",
    )
    for name in ("worker-a", "worker-b", "worker-c"):
        home = tmp_path / "agents" / name
        sub = SubAgentConfig(name=name, telegram_token=f"dummy-{name}")
        assert "transport" not in sub.model_fields_set
        merged = merge_sub_agent_config(main, sub, home)
        assert merged.transport == "telegram"
        assert merged.transports == ["telegram"]
        assert merged.ductor_home == str(home)
        assert merged.telegram_token == f"dummy-{name}"


def test_main_matrix_validator_keeps_access_token_compatibility() -> None:
    from ductor_bot.__main__ import _validate_matrix_config

    config = AgentConfig(
        matrix=MatrixConfig(
            homeserver="https://matrix.invalid",
            user_id="@main:invalid",
            access_token="dummy",
            allowed_rooms=["!room:invalid"],
        )
    )
    _validate_matrix_config(config)

    config.matrix.access_token = ""
    with pytest.raises(SystemExit) as exc:
        _validate_matrix_config(config)
    assert exc.value.code == 1


async def test_initial_sync_excludes_invalid_sub_and_starts_other(tmp_path: Path) -> None:
    supervisor = AgentSupervisor(AgentConfig(ductor_home=str(tmp_path)))
    bad = SubAgentConfig(
        name="bad", transport="matrix", matrix=_matrix(password="dummy", store_path="../../main")
    )
    good = SubAgentConfig(name="good", telegram_token="dummy-good")
    stack = MagicMock()
    stack.shutdown = AsyncMock()
    with (
        patch.object(supervisor._registry, "load", return_value=[bad, good]),
        patch.object(supervisor, "_notify_main_agent", new_callable=AsyncMock) as notify,
        patch(
            "ductor_bot.multiagent.supervisor.AgentStack.create", new_callable=AsyncMock
        ) as create,
        patch.object(supervisor, "_supervised_run", new_callable=AsyncMock, return_value=0),
    ):
        create.return_value = stack
        await supervisor._sync_sub_agents()
        await asyncio.sleep(0)

    assert "bad" not in supervisor.stacks
    assert "good" in supervisor.stacks
    create.assert_awaited_once()
    notify.assert_awaited_once()
    assert "bad" in notify.await_args.args[0]


async def test_hot_reload_invalid_change_stops_only_bad_sub(tmp_path: Path) -> None:
    supervisor = AgentSupervisor(AgentConfig(ductor_home=str(tmp_path)))
    old = MagicMock(config=AgentConfig(telegram_token="old-token"))
    old.shutdown = AsyncMock()
    supervisor._stacks["bad"] = old
    bad = SubAgentConfig(name="bad", transport="matrix", matrix=_matrix(access_token="dummy"))
    with (
        patch.object(supervisor._registry, "load", return_value=[bad]),
        patch.object(supervisor, "_notify_main_agent", new_callable=AsyncMock) as notify,
        patch(
            "ductor_bot.multiagent.supervisor.AgentStack.create", new_callable=AsyncMock
        ) as create,
    ):
        await supervisor._on_agents_changed()

    assert "bad" not in supervisor.stacks
    old.shutdown.assert_awaited_once()
    create.assert_not_awaited()
    notify.assert_awaited_once()
