"""Tests for __main__.py entry point."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ductor_bot import i18n
from ductor_bot.__main__ import main
from ductor_bot.config import AgentConfig
from ductor_bot.i18n import t_rich
from ductor_bot.workspace.paths import DuctorPaths

# Shorthand module paths for patching the new submodules.
_LIFECYCLE = "ductor_bot.cli_commands.lifecycle"
_STATUS = "ductor_bot.cli_commands.status"


class TestLoadConfig:
    """Test config loading, creation, and smart-merge."""

    def test_creates_config_from_example(self, tmp_path: Path) -> None:
        from ductor_bot.__main__ import load_config

        home = tmp_path / ".ductor"
        fw = tmp_path / "framework"
        fw.mkdir()
        example = {"telegram_token": "TEST", "provider": "claude"}
        (fw / "config.example.json").write_text(json.dumps(example))

        with patch("ductor_bot.__main__.resolve_paths") as mock_paths:
            paths = DuctorPaths(ductor_home=home, home_defaults=fw / "workspace", framework_root=fw)
            mock_paths.return_value = paths
            with patch("ductor_bot.__main__.init_workspace"):
                config = load_config()

        assert config.telegram_token == "TEST"
        assert paths.config_path.exists()

    def test_preserves_existing_user_config(self, tmp_path: Path) -> None:
        from ductor_bot.__main__ import load_config

        home = tmp_path / ".ductor"
        config_dir = home / "config"
        config_dir.mkdir(parents=True)
        fw = tmp_path / "framework"
        fw.mkdir()
        user_cfg = {"telegram_token": "MY_TOKEN", "provider": "codex", "model": "gpt-5.2-codex"}
        (config_dir / "config.json").write_text(json.dumps(user_cfg))

        with patch("ductor_bot.__main__.resolve_paths") as mock_paths:
            paths = DuctorPaths(ductor_home=home, home_defaults=fw / "workspace", framework_root=fw)
            mock_paths.return_value = paths
            with patch("ductor_bot.__main__.init_workspace"):
                config = load_config()

        assert config.telegram_token == "MY_TOKEN"
        assert config.provider == "codex"

    def test_merges_new_defaults_into_existing(self, tmp_path: Path) -> None:
        from ductor_bot.__main__ import load_config

        home = tmp_path / ".ductor"
        config_dir = home / "config"
        config_dir.mkdir(parents=True)
        fw = tmp_path / "framework"
        fw.mkdir()
        old_cfg = {"telegram_token": "TOKEN", "provider": "claude"}
        (config_dir / "config.json").write_text(json.dumps(old_cfg))

        with patch("ductor_bot.__main__.resolve_paths") as mock_paths:
            paths = DuctorPaths(ductor_home=home, home_defaults=fw / "workspace", framework_root=fw)
            mock_paths.return_value = paths
            with patch("ductor_bot.__main__.init_workspace"):
                config = load_config()

        assert config.streaming.enabled is True
        assert config.gemini_api_key is None
        merged = json.loads((config_dir / "config.json").read_text(encoding="utf-8"))
        assert merged["gemini_api_key"] == "null"

    def test_creates_default_config_when_no_example(self, tmp_path: Path) -> None:
        from ductor_bot.__main__ import load_config

        home = tmp_path / ".ductor"
        fw = tmp_path / "framework"
        fw.mkdir()

        with patch("ductor_bot.__main__.resolve_paths") as mock_paths:
            paths = DuctorPaths(ductor_home=home, home_defaults=fw / "workspace", framework_root=fw)
            mock_paths.return_value = paths
            with patch("ductor_bot.__main__.init_workspace"):
                config = load_config()

        assert paths.config_path.exists()
        assert config.provider == "claude"
        created = json.loads(paths.config_path.read_text(encoding="utf-8"))
        assert created["gemini_api_key"] == "null"

    def test_normalizes_existing_null_gemini_api_key_to_string(self, tmp_path: Path) -> None:
        from ductor_bot.__main__ import load_config

        home = tmp_path / ".ductor"
        config_dir = home / "config"
        config_dir.mkdir(parents=True)
        fw = tmp_path / "framework"
        fw.mkdir()
        user_cfg = {"telegram_token": "TOKEN", "provider": "claude", "gemini_api_key": None}
        (config_dir / "config.json").write_text(json.dumps(user_cfg), encoding="utf-8")

        with patch("ductor_bot.__main__.resolve_paths") as mock_paths:
            paths = DuctorPaths(ductor_home=home, home_defaults=fw / "workspace", framework_root=fw)
            mock_paths.return_value = paths
            with patch("ductor_bot.__main__.init_workspace"):
                config = load_config()

        assert config.gemini_api_key is None
        merged = json.loads((config_dir / "config.json").read_text(encoding="utf-8"))
        assert merged["gemini_api_key"] == "null"


class TestIsConfigured:
    def test_unconfigured_when_no_config(self, tmp_path: Path) -> None:
        from ductor_bot.__main__ import _is_configured

        with patch("ductor_bot.__main__.resolve_paths") as mock_paths:
            paths = DuctorPaths(
                ductor_home=tmp_path / "home",
                home_defaults=tmp_path / "fw" / "workspace",
                framework_root=tmp_path / "fw",
            )
            mock_paths.return_value = paths
            assert _is_configured() is False

    def test_configured_with_valid_token(self, tmp_path: Path) -> None:
        from ductor_bot.__main__ import _is_configured

        home = tmp_path / "home"
        config_dir = home / "config"
        config_dir.mkdir(parents=True)
        cfg = {"telegram_token": "123456:ABC", "allowed_user_ids": [1]}
        (config_dir / "config.json").write_text(json.dumps(cfg))

        with patch("ductor_bot.__main__.resolve_paths") as mock_paths:
            paths = DuctorPaths(
                ductor_home=home,
                home_defaults=tmp_path / "fw" / "workspace",
                framework_root=tmp_path / "fw",
            )
            mock_paths.return_value = paths
            assert _is_configured() is True


class TestRunTelegram:
    async def test_exits_on_missing_token(self, tmp_path: Path) -> None:
        from ductor_bot.__main__ import run_telegram

        config = AgentConfig(telegram_token="", ductor_home=str(tmp_path))
        with pytest.raises(SystemExit):
            await run_telegram(config)

    async def test_exits_on_placeholder_token(self, tmp_path: Path) -> None:
        from ductor_bot.__main__ import run_telegram

        config = AgentConfig(telegram_token="YOUR_TOKEN_HERE", ductor_home=str(tmp_path))
        with pytest.raises(SystemExit):
            await run_telegram(config)

    async def test_exits_on_empty_allowed_users(self, tmp_path: Path) -> None:
        from ductor_bot.__main__ import run_telegram

        config = AgentConfig(
            telegram_token="valid:token", allowed_user_ids=[], ductor_home=str(tmp_path)
        )
        with pytest.raises(SystemExit):
            await run_telegram(config)

    async def test_runs_bot_with_valid_config(self, tmp_path: Path) -> None:
        from ductor_bot.__main__ import run_telegram

        config = AgentConfig(
            telegram_token="valid:token", allowed_user_ids=[123], ductor_home=str(tmp_path)
        )
        mock_supervisor = MagicMock()
        mock_supervisor.start = AsyncMock(return_value=0)
        mock_supervisor.stop_all = AsyncMock()

        with (
            patch("ductor_bot.__main__.resolve_paths"),
            patch("ductor_bot.infra.pidlock.acquire_instance_lock"),
            patch("ductor_bot.__main__.atomic_bytes_save") as write_pid,
            patch("ductor_bot.infra.pidlock.release_lock"),
            patch(
                "ductor_bot.multiagent.supervisor.AgentSupervisor",
                return_value=mock_supervisor,
            ),
        ):
            await run_telegram(config)

        write_pid.assert_called_once()
        mock_supervisor.start.assert_called_once()
        mock_supervisor.stop_all.assert_called_once()


def _make_paths(tmp_path: Path) -> DuctorPaths:
    home = tmp_path / "home"
    fw = tmp_path / "fw"
    fw.mkdir(parents=True, exist_ok=True)
    return DuctorPaths(ductor_home=home, home_defaults=fw / "workspace", framework_root=fw)


def _write_config(paths: DuctorPaths, data: dict[str, object]) -> None:
    paths.config_path.parent.mkdir(parents=True, exist_ok=True)
    paths.config_path.write_text(json.dumps(data), encoding="utf-8")


class TestIsConfiguredExtended:
    def test_unconfigured_with_placeholder_token(self, tmp_path: Path) -> None:
        from ductor_bot.__main__ import _is_configured

        paths = _make_paths(tmp_path)
        _write_config(paths, {"telegram_token": "YOUR_TOKEN", "allowed_user_ids": [1]})
        with patch("ductor_bot.__main__.resolve_paths", return_value=paths):
            assert _is_configured() is False

    def test_unconfigured_with_empty_users(self, tmp_path: Path) -> None:
        from ductor_bot.__main__ import _is_configured

        paths = _make_paths(tmp_path)
        _write_config(paths, {"telegram_token": "123:ABC", "allowed_user_ids": []})
        with patch("ductor_bot.__main__.resolve_paths", return_value=paths):
            assert _is_configured() is False

    def test_unconfigured_with_corrupt_json(self, tmp_path: Path) -> None:
        from ductor_bot.__main__ import _is_configured

        paths = _make_paths(tmp_path)
        paths.config_path.parent.mkdir(parents=True)
        paths.config_path.write_text("{invalid json", encoding="utf-8")
        with patch("ductor_bot.__main__.resolve_paths", return_value=paths):
            assert _is_configured() is False

    def test_configured_with_slack(self, tmp_path: Path) -> None:
        from ductor_bot.__main__ import _is_configured

        paths = _make_paths(tmp_path)
        _write_config(
            paths,
            {
                "transport": "slack",
                "transports": ["slack"],
                "slack": {
                    "bot_token": "xoxb-token",
                    "app_token": "xapp-token",
                    "allowed_users": ["U0123456789"],
                    "allowed_channels": [],
                },
            },
        )
        with patch("ductor_bot.__main__.resolve_paths", return_value=paths):
            assert _is_configured() is True

    def test_configured_with_stale_transports_is_normalized(self, tmp_path: Path) -> None:
        from ductor_bot.__main__ import _is_configured, load_config

        paths = _make_paths(tmp_path)
        _write_config(
            paths,
            {
                "transport": "slack",
                "transports": ["telegram"],
                "telegram_token": "",
                "allowed_user_ids": [],
                "slack": {
                    "bot_token": "xoxb-token",
                    "app_token": "xapp-token",
                    "allowed_users": ["U0123456789"],
                    "allowed_channels": [],
                },
            },
        )
        with patch("ductor_bot.__main__.resolve_paths", return_value=paths):
            assert _is_configured() is True
            config = load_config()
        assert config.transport == "slack"
        assert config.transports == ["slack"]


class TestIsConfiguredMultiTransport:
    def test_configured_multi_transport_both_valid(self, tmp_path: Path) -> None:
        from ductor_bot.__main__ import _is_configured

        paths = _make_paths(tmp_path)
        _write_config(
            paths,
            {
                "transports": ["telegram", "matrix"],
                "telegram_token": "123:ABC",
                "allowed_user_ids": [1],
                "matrix": {"homeserver": "https://mx.test", "user_id": "@bot:test"},
            },
        )
        with patch("ductor_bot.__main__.resolve_paths", return_value=paths):
            assert _is_configured() is True

    def test_unconfigured_multi_transport_missing_matrix(self, tmp_path: Path) -> None:
        from ductor_bot.__main__ import _is_configured

        paths = _make_paths(tmp_path)
        _write_config(
            paths,
            {
                "transports": ["telegram", "matrix"],
                "telegram_token": "123:ABC",
                "allowed_user_ids": [1],
                "matrix": {},
            },
        )
        with patch("ductor_bot.__main__.resolve_paths", return_value=paths):
            assert _is_configured() is False

    def test_unconfigured_multi_transport_missing_telegram(self, tmp_path: Path) -> None:
        from ductor_bot.__main__ import _is_configured

        paths = _make_paths(tmp_path)
        _write_config(
            paths,
            {
                "transports": ["telegram", "matrix"],
                "telegram_token": "",
                "allowed_user_ids": [],
                "matrix": {"homeserver": "https://mx.test", "user_id": "@bot:test"},
            },
        )
        with patch("ductor_bot.__main__.resolve_paths", return_value=paths):
            assert _is_configured() is False

    def test_multi_transport_not_dropped_when_primary_not_first(self, tmp_path: Path) -> None:
        """Configured transport lists preserve both Telegram and Matrix regardless of primary ordering."""
        from ductor_bot.__main__ import _is_configured, load_config

        paths = _make_paths(tmp_path)
        _write_config(
            paths,
            {
                "transport": "matrix",
                "transports": ["telegram", "matrix"],
                "telegram_token": "123:ABC",
                "allowed_user_ids": [1],
                "matrix": {"homeserver": "https://mx.test", "user_id": "@bot:test"},
            },
        )
        with patch("ductor_bot.__main__.resolve_paths", return_value=paths):
            assert _is_configured() is True
            with patch("ductor_bot.__main__.init_workspace"):
                config = load_config()
        assert set(config.transports) == {"telegram", "matrix"}
        assert config.transport == "matrix"


class TestNormalizedTransportList:
    def test_empty_falls_back_to_primary(self) -> None:
        from ductor_bot.__main__ import _normalized_transport_list

        assert _normalized_transport_list({"transport": "matrix"}) == ["matrix"]

    def test_defaults_to_telegram(self) -> None:
        from ductor_bot.__main__ import _normalized_transport_list

        assert _normalized_transport_list({}) == ["telegram"]

    def test_multi_transport_preserved_with_primary_first(self) -> None:
        from ductor_bot.__main__ import _normalized_transport_list

        result = _normalized_transport_list(
            {"transport": "matrix", "transports": ["telegram", "matrix"]}
        )
        assert result == ["matrix", "telegram"]

    def test_stale_single_list_collapses_to_primary(self) -> None:
        from ductor_bot.__main__ import _normalized_transport_list

        result = _normalized_transport_list({"transport": "slack", "transports": ["telegram"]})
        assert result == ["slack"]

    def test_non_string_entries_ignored(self) -> None:
        from ductor_bot.__main__ import _normalized_transport_list

        result = _normalized_transport_list(
            {"transport": "telegram", "transports": ["telegram", 123, "matrix"]}
        )
        assert result == ["telegram", "matrix"]


def _mock_asyncio_run(return_value: int):
    def _side_effect(coro):
        coro.close()
        return return_value

    return _side_effect


class TestStartBotRestart:
    def _mock_config(self) -> AgentConfig:
        return AgentConfig(telegram_token="test:token", allowed_user_ids=[1])

    def test_exit42_with_supervisor_exits(self) -> None:
        from ductor_bot.cli_commands.lifecycle import start_bot

        with (
            patch(f"{_LIFECYCLE}.resolve_paths"),
            patch("ductor_bot.infra.pidlock.acquire_instance_lock"),
            patch("ductor_bot.logging_config.setup_logging"),
            patch("ductor_bot.__main__.load_config", return_value=self._mock_config()),
            patch(f"{_LIFECYCLE}.asyncio.run", side_effect=_mock_asyncio_run(42)),
            patch.dict("os.environ", {"DUCTOR_SUPERVISOR": "1"}),
            pytest.raises(SystemExit) as exc_info,
        ):
            start_bot()
        assert exc_info.value.code == 42

    def test_exit42_without_supervisor_exits(self) -> None:
        from ductor_bot.cli_commands.lifecycle import start_bot

        with (
            patch(f"{_LIFECYCLE}.resolve_paths"),
            patch("ductor_bot.infra.pidlock.acquire_instance_lock"),
            patch("ductor_bot.logging_config.setup_logging"),
            patch("ductor_bot.__main__.load_config", return_value=self._mock_config()),
            patch(f"{_LIFECYCLE}.asyncio.run", side_effect=_mock_asyncio_run(42)),
            patch.dict("os.environ", {}, clear=True),
            pytest.raises(SystemExit) as exc_info,
        ):
            start_bot()
        assert exc_info.value.code == 42

    def test_exit0_does_nothing(self) -> None:
        from ductor_bot.cli_commands.lifecycle import start_bot

        with (
            patch(f"{_LIFECYCLE}.resolve_paths"),
            patch("ductor_bot.infra.pidlock.acquire_instance_lock"),
            patch("ductor_bot.logging_config.setup_logging"),
            patch("ductor_bot.__main__.load_config", return_value=self._mock_config()),
            patch(f"{_LIFECYCLE}.asyncio.run", side_effect=_mock_asyncio_run(0)),
        ):
            start_bot()

    def test_nonzero_non42_exits(self) -> None:
        from ductor_bot.cli_commands.lifecycle import start_bot

        with (
            patch(f"{_LIFECYCLE}.resolve_paths"),
            patch("ductor_bot.infra.pidlock.acquire_instance_lock"),
            patch("ductor_bot.logging_config.setup_logging"),
            patch("ductor_bot.__main__.load_config", return_value=self._mock_config()),
            patch(f"{_LIFECYCLE}.asyncio.run", side_effect=_mock_asyncio_run(1)),
            pytest.raises(SystemExit) as exc_info,
        ):
            start_bot()
        assert exc_info.value.code == 1


class TestCountLogErrors:
    def test_counts_errors_in_log(self, tmp_path: Path) -> None:
        from ductor_bot.cli_commands.status import count_log_errors

        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        (log_dir / "ductor.log").write_text(
            "2024-01-01 INFO Started\n2024-01-01 ERROR Something broke\n"
            "2024-01-01 INFO Continued\n2024-01-01 ERROR Another error\n",
            encoding="utf-8",
        )
        assert count_log_errors(log_dir) == 2

    def test_returns_zero_no_dir(self, tmp_path: Path) -> None:
        from ductor_bot.cli_commands.status import count_log_errors

        assert count_log_errors(tmp_path / "nonexistent") == 0

    def test_returns_zero_no_log_files(self, tmp_path: Path) -> None:
        from ductor_bot.cli_commands.status import count_log_errors

        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        assert count_log_errors(log_dir) == 0


class TestMainDispatch:
    def test_removed_command_shows_usage_and_exits_2(self) -> None:
        from ductor_bot.__main__ import main

        with (
            patch("sys.argv", ["ductor", "stop"]),
            patch("ductor_bot.__main__._print_usage") as mock_usage,
            patch("ductor_bot.__main__._start_bot") as mock_start,
            pytest.raises(SystemExit) as exc_info,
        ):
            main()
        assert exc_info.value.code == 2
        mock_usage.assert_called_once()
        mock_start.assert_not_called()

    def test_help_command(self) -> None:
        from ductor_bot.__main__ import main

        with (
            patch("sys.argv", ["ductor", "help"]),
            patch("ductor_bot.__main__._print_usage") as mock_usage,
        ):
            main()
        mock_usage.assert_called_once()

    def test_status_command(self) -> None:
        from ductor_bot.__main__ import main

        with (
            patch("sys.argv", ["ductor", "status"]),
            patch("ductor_bot.__main__._cmd_status") as mock_status,
        ):
            main()
        mock_status.assert_called_once()

    def test_default_starts_bot_when_configured(self) -> None:
        from ductor_bot.__main__ import main

        with (
            patch("sys.argv", ["ductor"]),
            patch("ductor_bot.__main__._is_configured", return_value=True),
            patch("ductor_bot.__main__._start_bot") as mock_start,
        ):
            main()
        mock_start.assert_called_once_with(False)

    def test_verbose_flag_passed(self) -> None:
        from ductor_bot.__main__ import main

        with (
            patch("sys.argv", ["ductor", "-v"]),
            patch("ductor_bot.__main__._is_configured", return_value=True),
            patch("ductor_bot.__main__._start_bot") as mock_start,
        ):
            main()
        mock_start.assert_called_once_with(True)

    def test_dash_h_maps_to_help(self) -> None:
        from ductor_bot.__main__ import main

        with (
            patch("sys.argv", ["ductor", "-h"]),
            patch("ductor_bot.__main__._print_usage") as mock_usage,
        ):
            main()
        mock_usage.assert_called_once()

    def test_version_flag_prints_product_name(self) -> None:
        from ductor_bot.__main__ import main

        with (
            patch("sys.argv", ["ductor", "--version"]),
            patch("ductor_bot.__main__._console") as mock_console,
        ):
            main()
        printed = mock_console.print.call_args.args[0]
        assert printed.startswith("DT Agent ")


class TestMainHelpers:
    def test_print_usage_calls_status_when_configured(self) -> None:
        from ductor_bot.cli_commands.status import print_usage

        with (
            patch("ductor_bot.__main__._is_configured", return_value=True),
            patch(f"{_STATUS}.print_status") as mock_status,
        ):
            print_usage()
        mock_status.assert_called_once()

    def test_print_usage_shows_not_configured_panel(self) -> None:
        from ductor_bot.cli_commands.status import print_usage

        with patch("ductor_bot.__main__._is_configured", return_value=False):
            print_usage()

    def test_cmd_status_prints_not_configured(self) -> None:
        from ductor_bot.__main__ import _cmd_status

        with patch("ductor_bot.__main__._is_configured", return_value=False):
            _cmd_status()

    def test_default_action_configured_starts(self) -> None:
        from ductor_bot.__main__ import _default_action

        with (
            patch("ductor_bot.__main__._is_configured", return_value=True),
            patch("ductor_bot.__main__._start_bot") as mock_start,
        ):
            _default_action(verbose=True)
        mock_start.assert_called_once_with(True)

    def test_default_action_unconfigured_exits(self) -> None:
        from ductor_bot.__main__ import _default_action

        with (
            patch("ductor_bot.__main__._is_configured", return_value=False),
            patch("ductor_bot.__main__._console.print") as mock_print,
            patch("ductor_bot.__main__._start_bot") as mock_start,
            pytest.raises(SystemExit) as exc_info,
        ):
            _default_action(verbose=False)
        assert exc_info.value.code == 1
        assert "scripts/onboard.sh" in str(mock_print.call_args)
        mock_start.assert_not_called()


# -- console commands follow the configured language ---------------------------------------------


def _plain(text: str) -> str:
    return re.sub(r"\[/?[a-z ]+\]", "", text)


def _in(language: str, key: str, **values: object) -> str:
    """The text of *key* in *language*, without Rich markup (the language is reset to English)."""
    i18n.init(language)
    try:
        return _plain(t_rich(key, **values))
    finally:
        i18n.init("en")


class TestConsoleLanguage:
    @pytest.fixture(autouse=True)
    def _english_again(self) -> Iterator[None]:
        yield
        i18n.init("en")

    @staticmethod
    def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: str | None) -> Path:
        home = tmp_path / "home"
        (home / "config").mkdir(parents=True)
        if config is not None:
            (home / "config" / "config.json").write_text(config, encoding="utf-8")
        monkeypatch.setenv("DUCTOR_HOME", str(home))
        return home

    @staticmethod
    def _run(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], *args: str
    ) -> str:
        monkeypatch.setattr(sys, "argv", ["ductor", *args])
        main()
        return capsys.readouterr().out

    @staticmethod
    def _configured(language: object) -> str:
        return json.dumps(
            {
                "telegram_token": "123:abc",
                "allowed_user_ids": [1],
                "language": language,
            }
        )

    def test_help_uses_the_configured_language(self, tmp_path, monkeypatch, capsys) -> None:
        self._home(tmp_path, monkeypatch, json.dumps({"language": "ko"}))
        shown = self._run(monkeypatch, capsys, "help")
        for key in ("help.title_commands", "help.agents", "status.title"):
            assert _in("ko", key) in shown
            assert _in("ko", key) != _in("en", key)

    def test_status_without_a_configuration_is_english(self, tmp_path, monkeypatch, capsys) -> None:
        self._home(tmp_path, monkeypatch, None)
        shown = self._run(monkeypatch, capsys, "status")
        assert _in("en", "status.title") in shown
        assert _in("ko", "status.title") not in shown

    @pytest.mark.parametrize(
        "config", ["{broken", json.dumps({"language": 5}), json.dumps({"language": "xx"})]
    )
    def test_unreadable_or_unknown_language_falls_back_to_english(
        self, tmp_path, monkeypatch, capsys, config
    ) -> None:
        self._home(tmp_path, monkeypatch, config)
        shown = self._run(monkeypatch, capsys, "help")
        assert _in("en", "help.agents") in shown

    def test_status_paths_are_labelled_in_the_configured_language(
        self, tmp_path, monkeypatch, capsys
    ) -> None:
        self._home(tmp_path, monkeypatch, self._configured("ko"))
        shown = self._run(monkeypatch, capsys, "status")
        for key in ("status.path_home", "status.path_logs", "status.title_main"):
            label = _in("ko", key, path="").strip()
            assert label in shown
            assert label != _in("en", key, path="").strip()

    def test_agents_list_table_title_and_restarts_label(
        self, tmp_path, monkeypatch, capsys
    ) -> None:
        home = self._home(tmp_path, monkeypatch, json.dumps({"language": "ko"}))
        (home / "agents.json").write_text(json.dumps([{"name": "worker"}]), encoding="utf-8")
        shown = self._run(monkeypatch, capsys, "agents")
        assert _in("ko", "agents.title", count=1) in shown
        assert _in("ko", "agents.inherited") in shown
        assert "worker" in shown

    def test_agents_help_table_uses_the_configured_language(
        self, tmp_path, monkeypatch, capsys
    ) -> None:
        from ductor_bot.cli_commands.agents import print_agents_help

        i18n.init("de")
        print_agents_help()
        shown = capsys.readouterr().out
        for key in (
            "agents.title_help",
            "agents.help_list",
            "agents.help_add",
            "agents.help_remove",
        ):
            assert _in("de", key)[:15] in shown
        assert "ductor agents add <name>" in shown

    def test_headless_main_notice_is_translated(self, capsys) -> None:
        from ductor_bot.__main__ import _reject_main_headless

        i18n.init("fr")
        with pytest.raises(SystemExit):
            _reject_main_headless(AgentConfig())
        assert _in("fr", "config.headless_main")[:30] in capsys.readouterr().out


class TestNonObjectConfig:
    """A config.json that is not a JSON object still gives the English console, unchanged."""

    @pytest.fixture(autouse=True)
    def _english_again(self) -> Iterator[None]:
        yield
        i18n.init("en")

    @pytest.mark.parametrize("config", ["[]", "null", '"text"', "5", '[{"language": "ko"}]'])
    @pytest.mark.parametrize("command", ["help", "status", "agents"])
    def test_commands_finish_in_english_without_touching_files(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        config: str,
        command: str,
    ) -> None:
        home = TestConsoleLanguage._home(tmp_path, monkeypatch, config)
        before = sorted(p.relative_to(home).as_posix() for p in home.rglob("*"))
        shown = TestConsoleLanguage._run(monkeypatch, capsys, command)
        assert shown.strip()
        key = {"help": "help.agents", "status": "status.title", "agents": "agents.empty"}[command]
        assert _in("en", key) in shown
        assert _in("ko", key) not in shown
        assert (home / "config" / "config.json").read_text(encoding="utf-8") == config
        assert sorted(p.relative_to(home).as_posix() for p in home.rglob("*")) == before

    def test_live_health_lookup_survives_a_non_object_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ductor_bot.cli_commands.agents import fetch_live_health

        TestConsoleLanguage._home(tmp_path, monkeypatch, "[]")
        with patch("urllib.request.build_opener", side_effect=OSError("no server")):
            assert fetch_live_health() == {}

    def test_object_config_keeps_its_language(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        TestConsoleLanguage._home(tmp_path, monkeypatch, json.dumps({"language": "ko"}))
        shown = TestConsoleLanguage._run(monkeypatch, capsys, "help")
        assert _in("ko", "help.agents") in shown


class TestInheritedAgentFields:
    """``ductor agents`` says "inherited" only where the merge really inherits."""

    SAMPLES = [
        ("absent", {}, True, True),
        ("null", {"provider": None, "model": None}, True, True),
        ("empty", {"provider": "", "model": ""}, False, False),
        ("model-empty", {"model": ""}, True, False),
    ]

    @pytest.fixture(autouse=True)
    def _english_again(self) -> Iterator[None]:
        yield
        i18n.init("en")

    @pytest.mark.parametrize("language", ["en", "ko"])
    def test_display_matches_the_merge(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        language: str,
    ) -> None:
        from ductor_bot.multiagent.models import SubAgentConfig, merge_sub_agent_config

        home = TestConsoleLanguage._home(tmp_path, monkeypatch, json.dumps({"language": language}))
        entries = [{"name": f"agent-{label}", **fields} for label, fields, _, _ in self.SAMPLES]
        (home / "agents.json").write_text(json.dumps(entries), encoding="utf-8")
        shown = TestConsoleLanguage._run(monkeypatch, capsys, "agents")
        inherited = _in(language, "agents.inherited")
        main_config = AgentConfig(provider="codex", model="gpt-5")
        for (label, _fields, provider_inherits, model_inherits), entry in zip(
            self.SAMPLES, entries, strict=True
        ):
            merged = merge_sub_agent_config(
                main_config, SubAgentConfig.model_validate(entry), tmp_path / label
            )
            assert (merged.provider == "codex") is provider_inherits
            assert (merged.model == "gpt-5") is model_inherits
            line = next(row for row in shown.splitlines() if f"agent-{label}" in row)
            assert line.count(inherited) == int(provider_inherits) + int(model_inherits)


def test_startup_home_lock_precedes_config_and_pid_writes(tmp_path):
    import fcntl
    import os
    import subprocess
    import sys

    home = tmp_path / "home"
    (home / "config").mkdir(parents=True)
    config = home / "config/config.json"
    config.write_text('{"language":"en"}')
    pid = home / "bot.pid"
    pid.write_text("preserved")
    before = (config.read_bytes(), config.stat().st_mtime_ns, pid.read_bytes())
    code = """from unittest.mock import AsyncMock, patch
from ductor_bot.cli_commands.lifecycle import start_bot
with patch('ductor_bot.logging_config.setup_logging'), patch('ductor_bot.infra.feature_deps.check_feature_deps', return_value={}), patch('ductor_bot.__main__.run_telegram', new_callable=AsyncMock):
    start_bot()
"""
    with (home / "bot.lock").open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = subprocess.run(
            [sys.executable, "-c", code],
            env=dict(os.environ, DUCTOR_HOME=str(home)),
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    assert result.returncode == 1
    assert (config.read_bytes(), config.stat().st_mtime_ns, pid.read_bytes()) == before


def test_startup_overwrites_live_foreign_pid_after_lock(tmp_path):
    import os
    import subprocess
    import sys

    home = tmp_path / "home"
    (home / "config").mkdir(parents=True)
    config = AgentConfig(ductor_home=str(home), telegram_token="test:token", allowed_user_ids=[1])
    (home / "config/config.json").write_text(config.model_dump_json())
    (home / "bot.pid").write_text(str(os.getpid()))
    code = """import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from ductor_bot.cli_commands.lifecycle import start_bot
home = Path(os.environ['DUCTOR_HOME'])
async def start():
    assert (home / 'bot.pid').read_text() == str(os.getpid())
    print('supervisor started with own pid')
    return 0
supervisor = MagicMock()
supervisor.start = start
supervisor.stop_all = AsyncMock()
with patch('ductor_bot.logging_config.setup_logging'), patch('ductor_bot.infra.feature_deps.check_feature_deps', return_value={}), patch('ductor_bot.multiagent.supervisor.AgentSupervisor', return_value=supervisor):
    start_bot()
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=dict(os.environ, DUCTOR_HOME=str(home)),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "supervisor started with own pid" in result.stdout
    assert not (home / "bot.pid").exists()


_CONFIGURED_HOME_START = """import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from ductor_bot.cli_commands.lifecycle import start_bot
from ductor_bot.infra.pidlock import acquire_instance_lock
config = SimpleNamespace(ductor_home=sys.argv[1], language='en', log_level='INFO')
async def run(_config, **_kwargs):
    (Path(config.ductor_home) / 'bot.pid').write_text('started')
    print('started ' + str(locks.call_count), flush=True)
    sys.stdin.read()
    return 0
with patch('ductor_bot.__main__.load_config', return_value=config), patch('ductor_bot.logging_config.setup_logging'), patch('ductor_bot.infra.feature_deps.check_feature_deps', return_value={}), patch('ductor_bot.__main__.run_telegram', side_effect=run), patch('ductor_bot.infra.pidlock.acquire_instance_lock', wraps=acquire_instance_lock) as locks:
    start_bot()
"""


@pytest.mark.parametrize("relation", ["same", "different", "symlink"])
def test_startup_locks_each_resolved_home(tmp_path, relation):
    import os
    import subprocess

    from tests.infra.test_pidlock import _TRY_FLOCK_CODE

    home = tmp_path / "environment-home"
    home.mkdir()
    configured = home
    if relation == "different":
        configured = tmp_path / "configured-home"
        configured.mkdir()
    elif relation == "symlink":
        configured = tmp_path / "configured-alias"
        configured.symlink_to(home, target_is_directory=True)
    expected = 2 if relation == "different" else 1
    first = subprocess.Popen(
        [sys.executable, "-c", _CONFIGURED_HOME_START, str(configured)],
        env=dict(os.environ, DUCTOR_HOME=str(home)),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert first.stdout.readline() == f"started {expected}\n"
        for lock in {home.resolve() / "bot.lock", configured.resolve() / "bot.lock"}:
            result = subprocess.run(
                [sys.executable, "-c", _TRY_FLOCK_CODE, str(lock)],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            assert result.returncode == 1, result.stderr
    finally:
        first.communicate(input="", timeout=20)
    assert first.returncode == 0


def test_startup_refuses_busy_configured_home_without_changing_pid(tmp_path):
    import fcntl
    import os
    import subprocess

    home = tmp_path / "environment-home"
    configured = tmp_path / "configured-home"
    home.mkdir()
    configured.mkdir()
    pid = configured / "bot.pid"
    pid.write_text("preserved")
    before = (pid.read_bytes(), pid.stat().st_mtime_ns)
    with (configured / "bot.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = subprocess.run(
            [sys.executable, "-c", _CONFIGURED_HOME_START, str(configured)],
            env=dict(os.environ, DUCTOR_HOME=str(home)),
            input="",
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    assert result.returncode == 1, result.stderr
    assert "already holds the home lock" in result.stderr
    assert (pid.read_bytes(), pid.stat().st_mtime_ns) == before
