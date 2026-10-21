"""Secret store validation, writes, and compatibility with the tool loaders."""

from __future__ import annotations

import importlib.util
import os
import random
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

from ductor_bot.cli.base import CLIConfig
from ductor_bot.cli.executor import build_subprocess_env
from ductor_bot.infra.env_secrets import SECRETS_FILE_VARIABLE, clear_cache
from ductor_bot.infra.secret_store import (
    delete_secret,
    list_keys,
    set_secret,
    store_path,
    validate_key,
    validate_value,
)

TOOLS = Path(__file__).resolve().parents[2] / "ductor_bot/_home_defaults/workspace/tools"
LINE_BOUNDARIES = "\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029"


@pytest.mark.parametrize("key", ["", "a", "_KEY", "1KEY", "A-B", "A" * 57, "A\n"])
def test_invalid_keys(key: str) -> None:
    assert validate_key(key) == "key_invalid"


@pytest.mark.parametrize("key", ["A", "MY_KEY_1", "A" * 56])
def test_valid_keys(key: str) -> None:
    assert validate_key(key) is None


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("", "value_empty"),
        ("a" * 4097 + "\0", "value_too_long"),
        ("a\0\n", "value_nul"),
        (" a\n", "value_multiline"),
        (" 'a", "value_edge_space"),
        ("'a", "value_edge_quote"),
        ('a"', "value_edge_quote"),
        ("$1", "value_dollar"),
        ("$MY-KEY", "value_dollar"),
        ("${NAME}", "value_dollar"),
        ("$имя", "value_dollar"),
        ("\u00a0a", "value_edge_space"),
        ("a\u2003", "value_edge_space"),
    ],
)
def test_value_reasons_and_order(value: str, reason: str) -> None:
    assert validate_value(value) == reason


@pytest.mark.parametrize("boundary", list(LINE_BOUNDARIES))
def test_all_line_boundaries_rejected(boundary: str) -> None:
    assert validate_value("left" + boundary + "right") == "value_multiline"


def test_allowed_values_roundtrip_both_loaders(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("synthetic_secret_loader", TOOLS / "_secrets.py")
    assert spec is not None
    assert spec.loader is not None
    loader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loader)
    rng = random.Random(0)  # noqa: S311
    alphabet = "abcXYZ019 _-=#:$'\\\"/éж🙂\t"
    values = ["a" * 4096, "a b", "a#b", "a=b", "a$b", "a'b", 'a"b', "a\\b"]
    values.extend(
        "a" + "".join(rng.choice(alphabet) for _ in range(rng.randrange(1, 80))) + "z"
        for _ in range(80)
    )
    path = tmp_path / "store.env"
    for value in values:
        assert validate_value(value) is None
        for key in ("GH_TOKEN", "GITHUB_TOKEN"):
            path.write_text(
                "1=synthetic-reference\nMY-KEY=synthetic-reference\n"
                "{NAME}=synthetic-reference\nимя=synthetic-reference\n"
                "NAME=synthetic-reference\n" + key + "=old\n",
                encoding="utf-8",
            )
            set_secret(path, key, value)
            assert loader._parse(path)[key] == value
            env = dict(os.environ, DUCTOR_SECRETS_FILE=str(path))
            env.pop("GH_TOKEN", None)
            env.pop("GITHUB_TOKEN", None)
            result = subprocess.run(
                ["sh", str(TOOLS / "git_credential_from_ductor_env.sh"), "get"],
                input="protocol=https\nhost=github.com\n\n",
                text=True,
                capture_output=True,
                check=True,
                env=env,
            )
            assert result.stdout == "username=x-access-token\npassword=" + value + "\n"


def test_preserves_lines_and_removes_duplicate_keys(tmp_path: Path) -> None:
    path = tmp_path / "store.env"
    original = "# comment\r\nOTHER=one\r\n\r\n KEY =old\r\nKEEP=two\nKEY=last\ninvalid"
    path.write_bytes(original.encode())
    path.chmod(0o644)
    set_secret(path, "KEY", "synthetic-new")
    assert path.read_bytes() == (
        b"# comment\r\nOTHER=one\r\n\r\nKEY=synthetic-new\r\nKEEP=two\ninvalid"
    )
    assert path.stat().st_mode & 0o777 == 0o600
    assert list_keys(path) == ["KEEP", "KEY", "OTHER"]
    assert delete_secret(path, "KEY")
    assert path.read_bytes() == b"# comment\r\nOTHER=one\r\n\r\nKEEP=two\ninvalid"
    assert not delete_secret(path, "KEY")
    assert path.stat().st_mode & 0o777 == 0o600


def test_new_file_append_and_missing_delete(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "store.env"
    assert list_keys(path) == []
    assert not delete_secret(path, "KEY")
    assert not path.exists()
    set_secret(path, "KEY", "synthetic-one")
    assert path.read_text() == "KEY=synthetic-one\n"
    assert path.stat().st_mode & 0o777 == 0o600
    path.write_text("# comment")
    set_secret(path, "OTHER", "synthetic-two")
    assert path.read_text() == "# comment\nOTHER=synthetic-two\n"


def test_concurrent_writers_keep_both_keys(tmp_path: Path) -> None:
    path = tmp_path / "store.env"
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(set_secret, path, key, "synthetic") for key in ("FIRST", "SECOND")]
        for job in jobs:
            job.result()
    assert list_keys(path) == ["FIRST", "SECOND"]


def test_failed_atomic_replace_preserves_original(tmp_path: Path) -> None:
    path = tmp_path / "store.env"
    path.write_text("KEY=original\n")
    with (
        patch.object(Path, "replace", side_effect=OSError("synthetic failure")),
        pytest.raises(OSError, match="synthetic failure"),
    ):
        set_secret(path, "KEY", "synthetic-new")
    assert path.read_text() == "KEY=original\n"
    assert not list(tmp_path.glob("*.tmp"))


def test_rejected_value_never_written_or_exposed(tmp_path: Path) -> None:
    path = tmp_path / "store.env"
    value = "$synthetic-sensitive"
    with pytest.raises(ValueError, match=r"^value_dollar$") as error:
        set_secret(path, "KEY", value)
    assert value not in str(error.value)
    assert not path.exists()


@pytest.mark.parametrize("agent", ["main", "worker"])
@pytest.mark.parametrize("source", ["default", "dotenv", "process", "both", "empty_process"])
def test_path_matches_executor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent: str, source: str
) -> None:
    main = tmp_path / "main"
    home = main if agent == "main" else main / "agents" / agent
    home.mkdir(parents=True)
    monkeypatch.delenv(SECRETS_FILE_VARIABLE, raising=False)
    dotenv_path = tmp_path / "dotenv-store.env"
    process_path = tmp_path / "process-store.env"
    if source in {"dotenv", "both", "empty_process"}:
        (home / ".env").write_text(f"DUCTOR_SECRETS_FILE={dotenv_path}\n")
    if source in {"process", "both"}:
        monkeypatch.setenv(SECRETS_FILE_VARIABLE, str(process_path))
    elif source == "empty_process":
        monkeypatch.setenv(SECRETS_FILE_VARIABLE, "")
    clear_cache()
    config = CLIConfig(working_dir=str(home), ductor_home=home, agent_name=agent)
    expected = {
        "default": main / "secrets.env",
        "dotenv": dotenv_path,
        "process": process_path,
        "both": process_path,
        "empty_process": main / "secrets.env",
    }[source]
    assert store_path(home) == expected
    assert build_subprocess_env(config)[SECRETS_FILE_VARIABLE] == str(expected)


@pytest.mark.parametrize("operation", ["set", "delete"])
def test_writes_preserve_secret_store_symlink(tmp_path: Path, operation: str) -> None:
    target = tmp_path / "target.env"
    target.write_text("KEY=old\nOTHER=keep\n")
    target.chmod(0o644)
    link = tmp_path / "secrets.env"
    link.symlink_to(target.name)
    if operation == "set":
        set_secret(link, "KEY", "new")
        assert target.read_text() == "KEY=new\nOTHER=keep\n"
    else:
        assert delete_secret(link, "KEY")
        assert target.read_text() == "OTHER=keep\n"
    assert link.is_symlink()
    assert link.readlink() == Path(target.name)
    assert target.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("source", ["process", "dotenv"])
def test_store_path_expands_user_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv(SECRETS_FILE_VARIABLE, raising=False)
    if source == "process":
        monkeypatch.setenv(SECRETS_FILE_VARIABLE, "~/private/secrets.env")
    else:
        (tmp_path / ".env").write_text("DUCTOR_SECRETS_FILE=~/private/secrets.env\n")
    clear_cache()
    assert store_path(tmp_path) == tmp_path / "private/secrets.env"
