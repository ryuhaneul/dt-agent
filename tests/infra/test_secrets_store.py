"""The shared secrets store: the loader tool and the path every CLI run is given."""

from __future__ import annotations

import importlib.util
import logging
import os
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ductor_bot.cli.base import CLIConfig
from ductor_bot.cli.executor import build_subprocess_env
from ductor_bot.cli.slot_rotation import RotationResult
from ductor_bot.cron.execution import OneShotCommand, OneShotExecutionResult
from ductor_bot.infra.env_secrets import SECRETS_FILE_VARIABLE, clear_cache
from ductor_bot.infra.task_runner import TaskRunOptions, run_oneshot_task
from tests.infra.test_task_runner import _exec_config, _exec_result

TOOL = Path(__file__).resolve().parents[2] / "ductor_bot/_home_defaults/workspace/tools/_secrets.py"
KEY = SECRETS_FILE_VARIABLE
EXPLICIT = "/explicit/in-process/secrets.env"
FROM_DOTENV = "/explicit/in-dotenv/secrets.env"


@pytest.fixture
def loader(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    spec = importlib.util.spec_from_file_location("_secrets_under_test", TOOL)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.delenv(KEY, raising=False)
    return module


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The loader writes into ``os.environ``; every test gets it back as it was."""
    with patch.dict(os.environ):
        monkeypatch.delenv(KEY, raising=False)
        clear_cache()
        yield


def _store(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


# -- the loader tool --------------------------------------------------------------------------


class TestLoaderPaths:
    def test_environment_names_the_store(
        self, loader: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            loader, "_DEFAULT_PATH", str(_store(tmp_path / "default.env", "A_KEY=default\n"))
        )
        monkeypatch.setenv(KEY, str(_store(tmp_path / "named.env", "A_KEY=named\n")))
        monkeypatch.delenv("A_KEY", raising=False)
        assert loader.load_secrets("A_KEY") == {"A_KEY": "named"}

    def test_default_path_without_a_named_store(
        self, loader: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            loader, "_DEFAULT_PATH", str(_store(tmp_path / "default.env", "A_KEY=default\n"))
        )
        monkeypatch.delenv("A_KEY", raising=False)
        assert loader.load_secrets("A_KEY") == {"A_KEY": "default"}
        os.environ.pop("A_KEY", None)
        monkeypatch.setenv(KEY, "")  # an empty name counts as none
        assert loader.load_secrets("A_KEY") == {"A_KEY": "default"}

    def test_default_is_the_container_path(self, loader: ModuleType) -> None:
        assert loader._DEFAULT_PATH == "/ductor/secrets.env"


class TestLoaderCompatibility:
    @pytest.fixture(autouse=True)
    def _store_file(
        self, loader: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _store(
            tmp_path / "store.env",
            "# a comment\n"
            "MY_API_KEY=dummy-one\n"
            'QUOTED="dummy two"\n'
            "SINGLE='dummy-three'\n"
            "EMPTY=\n"
            "COPY=$MY_API_KEY\n"
            "DANGLING=$NOT_THERE\n"
            "export EXPORTED=dummy-four\n"
            "NO_EQUALS_LINE\n"
            "  SPACED  =  padded  \n",
        )
        monkeypatch.setenv(KEY, str(path))
        for name in (
            "MY_API_KEY",
            "QUOTED",
            "SINGLE",
            "EMPTY",
            "COPY",
            "DANGLING",
            "EXPORTED",
            "SPACED",
        ):
            monkeypatch.delenv(name, raising=False)

    def test_named_keys_only(self, loader: ModuleType) -> None:
        assert loader.load_secrets("MY_API_KEY") == {"MY_API_KEY": "dummy-one"}
        assert "QUOTED" not in os.environ

    def test_no_arguments_loads_every_entry(self, loader: ModuleType) -> None:
        loaded = loader.load_secrets()
        assert loaded["MY_API_KEY"] == "dummy-one"
        assert loaded["QUOTED"] == "dummy two"
        assert loaded["SINGLE"] == "dummy-three"
        assert loaded["SPACED"] == "padded"

    def test_empty_comment_and_malformed_lines_give_nothing(self, loader: ModuleType) -> None:
        loaded = loader.load_secrets()
        assert "EMPTY" not in loaded
        assert not any(name.startswith("#") or "NO_EQUALS" in name for name in loaded)

    def test_reference_to_another_key(self, loader: ModuleType) -> None:
        loaded = loader.load_secrets("COPY", "DANGLING")
        assert loaded["COPY"] == "dummy-one"
        assert loaded["DANGLING"] == "$NOT_THERE"  # an unknown reference stays as written

    def test_export_prefix_is_not_understood(self, loader: ModuleType) -> None:
        loader.load_secrets("EXPORTED")
        assert "EXPORTED" not in os.environ
        assert "EXPORTED" not in loader.load_secrets()

    def test_environment_wins(self, loader: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_API_KEY", "from-environment")
        assert loader.load_secrets("MY_API_KEY") == {}
        assert os.environ["MY_API_KEY"] == "from-environment"
        assert loader.get_secret("MY_API_KEY") == "from-environment"

    def test_get_secret_falls_back_to_the_store_and_the_default(self, loader: ModuleType) -> None:
        assert loader.get_secret("MY_API_KEY") == "dummy-one"
        assert loader.get_secret("NOT_STORED") is None
        assert loader.get_secret("NOT_STORED", "fallback") == "fallback"


class TestLoaderRobustness:
    def test_missing_file_gives_nothing(
        self, loader: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(KEY, str(tmp_path / "absent.env"))
        assert loader.load_secrets() == {}
        assert loader.load_secrets("MY_API_KEY") == {}
        assert loader.get_secret("MY_API_KEY") is None

    def test_a_directory_or_unreadable_file_gives_nothing(
        self, loader: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(KEY, str(tmp_path))
        assert loader.load_secrets("MY_API_KEY") == {}
        path = _store(tmp_path / "locked.env", "MY_API_KEY=dummy-secret-value\n")
        monkeypatch.setenv(KEY, str(path))
        with patch.object(Path, "read_text", side_effect=OSError("denied")):
            assert loader.load_secrets("MY_API_KEY") == {}

    def test_values_stay_out_of_logs_and_errors(
        self,
        loader: ModuleType,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        marker = "dummy-secret-marker-value"
        path = _store(tmp_path / "store.env", f"MY_API_KEY={marker}\nBROKEN\n")
        monkeypatch.setenv(KEY, str(path))
        monkeypatch.delenv("MY_API_KEY", raising=False)
        with caplog.at_level(logging.DEBUG):
            loader.load_secrets("MY_API_KEY")
            loader.get_secret("OTHER")
        out = capsys.readouterr()
        assert marker not in caplog.text
        assert marker not in out.out + out.err
        monkeypatch.setenv(KEY, str(tmp_path / "absent.env"))
        os.environ.pop("MY_API_KEY", None)
        try:
            loader.load_secrets("MY_API_KEY")
        except Exception as error:
            assert marker not in str(error)  # noqa: PT017


# -- the path every CLI run is given ----------------------------------------------------------


def _conversation_env(
    home: Path, agent: str, extra: dict[str, str] | None = None
) -> dict[str, str]:
    workspace = home / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    config = CLIConfig(working_dir=str(workspace), ductor_home=home, agent_name=agent)
    return dict(build_subprocess_env(config, extra))


async def _oneshot_overrides(
    home: Path,
    *,
    caller: dict[str, str | None] | None = None,
    command_env: dict[str, str | None] | None = None,
    rotation: bool = False,
) -> dict[str, str | None]:
    captured: dict[str, str | None] = {}

    async def fake_exec(one_shot: OneShotCommand, **_: object) -> OneShotExecutionResult:
        captured.update(one_shot.env_overrides)
        return _exec_result()

    async def fake_rotation(**kwargs: object) -> RotationResult[OneShotExecutionResult]:
        _, execution = await kwargs["attempt"]("slot")  # type: ignore[operator]
        return RotationResult(payload=execution, slot="slot")

    config = replace(_exec_config(), claude_accounts=["slot"]) if rotation else _exec_config()
    command = OneShotCommand(cmd=["cli"], env_overrides=dict(command_env or {}))
    with (
        patch("ductor_bot.cli.slot_rotation.make_registry", return_value=MagicMock()),
        patch("ductor_bot.cli.slot_rotation.slot_extra_env", return_value={}),
        patch("ductor_bot.cli.slot_rotation.run_with_rotation", side_effect=fake_rotation),
        patch("ductor_bot.cron.execution.build_cmd", return_value=command),
        patch("ductor_bot.cron.execution.execute_one_shot", new=AsyncMock(side_effect=fake_exec)),
    ):
        await run_oneshot_task(
            config,
            "work",
            TaskRunOptions(
                cwd=home,
                timeout_seconds=1,
                timeout_label="test",
                ductor_home=home,
                env_overrides=dict(caller or {}),
            ),
        )
    return captured


PATHS = ("conversation", "oneshot", "rotation")
CASES = ("none", "dotenv only", "process only", "both")


async def _effective_store(
    path: str, case: str, home: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[str | None, bool]:
    """The store path the CLI would see, and whether the run itself sets it (override)."""
    if case in ("dotenv only", "both"):
        (home / ".env").write_text(f"{KEY}={FROM_DOTENV}\n", encoding="utf-8")
    if case in ("process only", "both"):
        monkeypatch.setenv(KEY, EXPLICIT)
    clear_cache()
    if path == "conversation":
        return _conversation_env(home, "main").get(KEY), True
    overrides = await _oneshot_overrides(home, rotation=path == "rotation")
    return overrides.get(KEY, os.environ.get(KEY)), KEY in overrides


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("case", CASES)
async def test_store_path_given_to_every_run(
    path: str, case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    effective, set_by_run = await _effective_store(path, case, tmp_path, monkeypatch)
    expected = {
        "none": str(tmp_path / "secrets.env"),
        "dotenv only": FROM_DOTENV,
        "process only": EXPLICIT,
        "both": EXPLICIT,
    }[case]
    assert effective == expected
    if path != "conversation" and case in ("process only", "both"):
        assert not set_by_run  # the inherited environment already carries it


async def test_callers_own_value_is_kept(tmp_path: Path) -> None:
    assert _conversation_env(tmp_path, "main", {KEY: "/caller/secrets.env"})[KEY] == (
        "/caller/secrets.env"
    )
    for rotation in (False, True):
        from_caller = await _oneshot_overrides(
            tmp_path, caller={KEY: "/caller/secrets.env"}, rotation=rotation
        )
        assert from_caller[KEY] == "/caller/secrets.env"
        from_command = await _oneshot_overrides(
            tmp_path, command_env={KEY: "/command/secrets.env"}, rotation=rotation
        )
        assert from_command[KEY] == "/command/secrets.env"
        removed = await _oneshot_overrides(tmp_path, command_env={KEY: None}, rotation=rotation)
        assert removed[KEY] is None  # a caller that asks to unset it is not overruled


async def test_main_sub_and_one_shot_runs_choose_the_same_store(tmp_path: Path) -> None:
    main = tmp_path / "main"
    sub = main / "agents" / "worker"
    sub.mkdir(parents=True)
    expected = str(main / "secrets.env")
    chosen = {
        "main conversation": _conversation_env(main, "main")[KEY],
        "sub conversation": _conversation_env(sub, "worker")[KEY],
    }
    for label, home in (("main", main), ("sub", sub)):
        chosen[f"{label} one-shot"] = (await _oneshot_overrides(home))[KEY]  # type: ignore[assignment]
        chosen[f"{label} rotation"] = (await _oneshot_overrides(home, rotation=True))[KEY]  # type: ignore[assignment]
    assert set(chosen.values()) == {expected}, chosen


def test_a_sub_agent_tool_reads_the_main_store(
    loader: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = tmp_path / "main"
    sub = main / "agents" / "worker"
    sub.mkdir(parents=True)
    _store(main / "secrets.env", "MY_API_KEY=dummy-one\nOTHER_KEY=dummy-two\n")
    env = _conversation_env(sub, "worker")
    monkeypatch.setenv(KEY, env[KEY])
    monkeypatch.delenv("MY_API_KEY", raising=False)
    monkeypatch.delenv("OTHER_KEY", raising=False)
    assert loader.load_secrets("MY_API_KEY") == {"MY_API_KEY": "dummy-one"}
    assert "MY_API_KEY" not in env  # the store is not injected; only its path is
    assert "OTHER_KEY" not in env


def test_store_contents_are_not_injected(tmp_path: Path) -> None:
    _store(tmp_path / "secrets.env", "MY_API_KEY=dummy-one\n")
    assert "MY_API_KEY" not in _conversation_env(tmp_path, "main")


def test_image_context_leaves_the_store_out_at_any_depth() -> None:
    root = Path(__file__).resolve().parents[2]
    assert "**/secrets.env" in (root / ".dockerignore").read_text().splitlines()
    lines = (root / ".dockerignore").read_text().splitlines()
    assert "**/.env" in lines
    assert "**/.env.*" in lines


def test_loader_expands_store_home(loader: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(KEY, "~/secrets.env")
    _store(tmp_path / "secrets.env", "SYNTHETIC_KEY=synthetic-value\n")
    monkeypatch.delenv("SYNTHETIC_KEY", raising=False)
    assert loader.load_secrets("SYNTHETIC_KEY") == {"SYNTHETIC_KEY": "synthetic-value"}
