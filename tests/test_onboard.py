"""Onboarding input and output contract, without a Docker daemon."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from ductor_bot.__main__ import _validate_transports
from ductor_bot.config import AgentConfig


SOURCE = Path(__file__).resolve().parents[1]


@pytest.fixture
def onboarding(tmp_path: Path):
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    for name in ("onboard.sh", "_home.sh", "onboard.py"):
        shutil.copy2(SOURCE / "scripts" / name, scripts / name)
    shutil.copy2(SOURCE / "config.example.json", repo / "config.example.json")
    shutil.copytree(SOURCE / "ductor_bot/i18n", repo / "ductor_bot/i18n")
    shutil.copytree(SOURCE / "scripts/onboard_i18n", scripts / "onboard_i18n")
    shutil.copy2(SOURCE / "compose.yaml", repo / "compose.yaml")
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_docker = fake_bin / "docker"
    fake_docker.write_text("#!/bin/sh\nprintf '5.0.0\\n'\n")
    fake_docker.chmod(0o755)
    fake_id = fake_bin / "id"
    fake_id.write_text("#!/bin/sh\nprintf '1000\\n'\n")
    fake_id.chmod(0o755)
    token = tmp_path / "telegram-token"
    token.write_text("123:FAKE_TOKEN\n")
    home = tmp_path / "home"
    environment = os.environ.copy()
    environment["HOME"] = str(tmp_path)
    environment["PATH"] = f"{fake_bin}:{os.environ['PATH']}"
    environment["TZ"] = "UTC"
    for key in tuple(environment):
        if key in {"DUCTOR_HOME", "LC_ALL", "LC_MESSAGES", "LANG"} or key.startswith("ONBOARD_"):
            environment.pop(key)
    def run(*args: str):
        return subprocess.run(
            [
                str(scripts / "onboard.sh"), "--no-prompt", "--home", str(home),
                "--telegram-token-file", str(token), "--allowed-users", "123",
                *args,
            ],
            cwd=repo, env=environment, capture_output=True, text=True,
        )
    return repo, home, run


def parse_env(path: Path) -> dict[str, str]:
    return {
        key: json.loads(value)
        for key, value in (line.split("=", 1) for line in path.read_text().splitlines())
    }


@pytest.mark.parametrize(
    ("options", "extras", "api", "webhook", "bind"),
    [
        ((), "", False, False, "127.0.0.1"),
        (("--webhook",), "", False, True, "127.0.0.1"),
        (("--api", "--bind", "192.0.2.10"), "api", True, False, "192.0.2.10"),
    ],
)
def test_noninteractive_outputs(onboarding, options, extras, api, webhook, bind):
    repo, home, run = onboarding
    result = run(*options)
    assert result.returncode == 0, result.stderr
    root_env = parse_env(repo / ".env")
    shared_env = parse_env(home / ".env.shared")
    config = json.loads((home / "config/config.json").read_text())
    assert root_env == {
        "DUCTOR_HOME": str(home), "DUCTOR_EXTRAS": extras,
        "DUCTOR_WEBHOOK_BIND": bind, "DUCTOR_API_BIND": bind,
        "DUCTOR_UID": str(os.getuid()), "DUCTOR_GID": str(os.getgid()),
    }
    assert shared_env["TZ"] == "UTC"
    assert len(shared_env["BROWSER_TOKEN"]) == 64
    assert config["ductor_home"] == "/ductor"
    assert config["transports"] == ["telegram"]
    assert config["allowed_user_ids"] == [123]
    assert config["api"]["enabled"] is api
    assert config["api"]["token"] == ""
    assert config["webhooks"]["enabled"] is webhook
    assert config["webhooks"]["host"] == ("0.0.0.0" if webhook else "127.0.0.1")
    override = repo / "compose.override.yml"
    if webhook or api:
        assert override.exists()
        expected_port = "8742" if webhook else "8741"
        bind_var = "DUCTOR_WEBHOOK_BIND" if webhook else "DUCTOR_API_BIND"
        assert f"${{{bind_var}:-127.0.0.1}}:{expected_port}:{expected_port}" in override.read_text()
    else:
        assert not override.exists()
    for path in (
        repo / ".env", home / ".env", home / ".env.shared", home / "secrets.env",
        home / "config/config.json",
    ):
        assert path.stat().st_mode & 0o777 == 0o600
    assert (home / ".cli-auth").stat().st_mode & 0o777 == 0o700
    # The store for tool credentials starts as a header without any key.
    store_lines = [line for line in (home / "secrets.env").read_text().splitlines() if line.strip()]
    assert store_lines
    assert all(line.startswith("#") for line in store_lines)
    assert "load_secrets" in store_lines[1] + store_lines[2] + store_lines[3]
    _validate_transports(AgentConfig.model_validate(config))
    assert "FAKE_TOKEN" not in result.stdout + result.stderr


def test_optional_transports_and_groups(onboarding, tmp_path):
    repo, home, run = onboarding
    access = tmp_path / "matrix-token"
    access.write_text("FAKE_MATRIX\n")
    bot = tmp_path / "slack-bot"
    bot.write_text("FAKE_BOT\n")
    app = tmp_path / "slack-app"
    app.write_text("FAKE_APP\n")
    result = run(
        "--allowed-groups", "-1001,-1002", "--transport", "matrix",
        "--transport", "slack", "--matrix-homeserver", "https://example.test",
        "--matrix-user-id", "@bot:example.test",
        "--matrix-access-token-file", str(access), "--matrix-device-id", "DEVICE",
        "--matrix-allowed-rooms", "!room:example.test",
        "--slack-bot-token-file", str(bot), "--slack-app-token-file", str(app),
        "--slack-allowed-users", "U123",
    )
    assert result.returncode == 0, result.stderr
    assert parse_env(repo / ".env")["DUCTOR_EXTRAS"] == "matrix,slack"
    config = json.loads((home / "config/config.json").read_text())
    assert config["transports"] == ["telegram", "matrix", "slack"]
    assert config["allowed_group_ids"] == [-1001, -1002]
    _validate_transports(AgentConfig.model_validate(config))
    assert "FAKE_MATRIX" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    ("options", "error"),
    [
        (("--transport", "matrix", "--matrix-homeserver", "https://example.test",
          "--matrix-user-id", "@bot:example.test", "--matrix-allowed-users",
          "@owner:example.test", "--matrix-access-token-file", "MISSING"), "Matrix device ID"),
        (("--transport", "slack", "--slack-allowed-users", "U123"), "Slack bot token"),
    ],
)
def test_optional_transport_missing_input(onboarding, options, error):
    _, _, run = onboarding
    result = run(*options)
    assert result.returncode == 2
    assert error in result.stderr


def test_required_input_missing(onboarding):
    repo, home, run = onboarding
    result = subprocess.run(
        [str(repo / "scripts/onboard.sh"), "--no-prompt", "--home", str(home)],
        cwd=repo, capture_output=True, text=True,
        env={"HOME": str(home), "PATH": f"{repo.parent / 'fake-bin'}:{os.environ['PATH']}"},
    )
    assert result.returncode == 2
    assert "Telegram token file is required" in result.stderr
    assert not (repo / ".env").exists()


def test_existing_files_preserved_without_force(onboarding):
    repo, home, run = onboarding
    assert run().returncode == 0
    paths = [repo / ".env", home / ".env", home / ".env.shared", home / "config/config.json"]
    before = {path: path.read_bytes() for path in paths}
    store = home / "secrets.env"
    store_before = store.read_bytes()
    second = run("--api")
    assert second.returncode == 0
    assert second.stderr.count("keeping it") == len(paths)
    assert {path: path.read_bytes() for path in paths} == before
    assert store.read_bytes() == store_before
    assert run("--api", "--force").returncode == 0
    assert parse_env(repo / ".env")["DUCTOR_EXTRAS"] == "api"


@pytest.mark.parametrize("options", [(), ("--force",)])
def test_existing_secrets_file_is_left_alone(onboarding, options):
    _, home, run = onboarding
    home.mkdir(parents=True)
    store = home / "secrets.env"
    store.write_text("MY_API_KEY=dummy-value\n")
    store.chmod(0o640)
    os.utime(store, (1_000_000_000, 1_000_000_000))

    def state():
        info = store.stat()
        return store.read_bytes(), info.st_mode & 0o777, info.st_mtime_ns

    before = state()
    result = run(*options)
    assert result.returncode == 0, result.stderr
    assert state() == before
    assert "secrets.env" not in result.stderr
    assert "dummy-value" not in result.stdout + result.stderr


def test_secrets_file_that_is_a_link_is_not_followed(onboarding, tmp_path):
    _, home, run = onboarding
    home.mkdir(parents=True)
    target = tmp_path / "external-store"
    target.write_text("MY_API_KEY=dummy-value\n")
    target.chmod(0o644)
    (home / "secrets.env").symlink_to(target)
    for options in ((), ("--force",)):
        assert run(*options).returncode == 0
        assert (home / "secrets.env").is_symlink()
        assert target.read_text() == "MY_API_KEY=dummy-value\n"
        assert target.stat().st_mode & 0o777 == 0o644


def test_cli_values_override_environment(onboarding, tmp_path):
    repo, home, _ = onboarding
    token = tmp_path / "telegram-token"
    environment = {k: v for k, v in os.environ.items() if k not in {"LC_ALL", "LC_MESSAGES", "LANG"}}
    environment.update({
        "HOME": str(tmp_path), "PATH": f"{tmp_path / 'fake-bin'}:{os.environ['PATH']}",
        "ONBOARD_TELEGRAM_TOKEN_FILE": str(token),
        "ONBOARD_ALLOWED_USERS": "999", "ONBOARD_TZ": "UTC",
        "ONBOARD_API": "true", "ONBOARD_TRANSPORTS": "matrix",
    })
    result = subprocess.run(
        [
            str(repo / "scripts/onboard.sh"), "--no-prompt", "--home", str(home),
            "--allowed-users", "123", "--tz", "Asia/Seoul", "--no-api",
            "--transport", "none",
        ],
        cwd=repo, env=environment, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    config = json.loads((home / "config/config.json").read_text())
    assert config["allowed_user_ids"] == [123]
    assert config["user_timezone"] == "Asia/Seoul"
    assert config["api"]["enabled"] is False
    assert config["transports"] == ["telegram"]
    assert parse_env(repo / ".env")["DUCTOR_EXTRAS"] == ""


def test_matrix_access_token_requires_device_even_without_transport(onboarding, tmp_path):
    _, _, run = onboarding
    access = tmp_path / "matrix-token"
    access.write_text("FAKE_MATRIX\n")
    result = run("--matrix-access-token-file", str(access))
    assert result.returncode == 2
    assert "Matrix device ID" in result.stderr


@pytest.mark.parametrize(
    ("available", "version", "expected"),
    [
        ((), "", "python3 is required"),
        (("python3",), "", "docker is required"),
        (("python3", "docker"), "1.29.2", "docker compose v2 or newer"),
        (("python3", "docker"), "", "docker compose v2 or newer"),
    ],
)
def test_prerequisite_checks(onboarding, tmp_path, available, version, expected):
    repo, home, _ = onboarding
    isolated_bin = tmp_path / "isolated-bin"
    isolated_bin.mkdir()
    if "python3" in available:
        (isolated_bin / "python3").symlink_to(shutil.which("python3"))
    if "docker" in available:
        docker = isolated_bin / "docker"
        docker.write_text(f"#!/bin/sh\nprintf '{version}\\n'\n")
        docker.chmod(0o755)
    result = subprocess.run(
        ["/bin/bash", str(repo / "scripts/onboard.sh"), "--no-prompt", "--home", str(home)],
        cwd=repo, env={"HOME": str(tmp_path), "PATH": str(isolated_bin)},
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert expected in result.stderr
    assert not (repo / ".env").exists()


def test_port_override_handles_both_enabled_and_removal(onboarding):
    repo, home, run = onboarding
    override = repo / "compose.override.yml"
    assert run("--api", "--webhook").returncode == 0
    content = override.read_text()
    assert content.count("      - ") == 2
    assert "DUCTOR_API_BIND" in content
    assert "DUCTOR_WEBHOOK_BIND" in content
    assert run("--no-api", "--no-webhook", "--force").returncode == 0
    assert not override.exists()
    config = json.loads((home / "config/config.json").read_text())
    assert not config["api"]["enabled"]
    assert not config["webhooks"]["enabled"]


def test_override_uses_saved_ports_and_enabled_listeners(onboarding):
    repo, home, run = onboarding
    assert run("--api", "--webhook").returncode == 0
    config_path = home / "config/config.json"
    config = json.loads(config_path.read_text())
    config["api"]["port"] = 9001
    config["webhooks"]["port"] = 9002
    override = repo / "compose.override.yml"

    def assert_ports(expected: set[tuple[int, int]]) -> None:
        if expected:
            assert override.exists()
        else:
            assert not override.exists()
        if shutil.which("docker") is None:
            pytest.skip("Docker Compose is unavailable")
        result = subprocess.run(
            ["docker", "compose", "config", "--format", "json"],
            cwd=repo, capture_output=True, text=True,
            env={key: value for key, value in os.environ.items()
                 if key not in {"DUCTOR_HOME", "DUCTOR_WEBHOOK_BIND", "DUCTOR_API_BIND"}},
        )
        assert result.returncode == 0
        published = {
            (int(item["published"]), int(item["target"]))
            for item in json.loads(result.stdout)["services"]["ductor-bot"].get("ports", [])
        }
        assert published == expected

    config_path.write_text(json.dumps(config))
    assert run().returncode == 0
    assert_ports({(9001, 9001), (9002, 9002)})

    config["webhooks"]["enabled"] = False
    config_path.write_text(json.dumps(config))
    assert run().returncode == 0
    assert_ports({(9001, 9001)})

    config["webhooks"]["enabled"] = True
    config["api"]["enabled"] = False
    config_path.write_text(json.dumps(config))
    assert run().returncode == 0
    assert_ports({(9002, 9002)})

    config["webhooks"]["enabled"] = False
    config_path.write_text(json.dumps(config))
    assert run().returncode == 0
    assert_ports(set())


def test_force_replaces_public_config_from_private_temporary_file(tmp_path, monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location("onboard_under_test", SOURCE / "scripts/onboard.py")
    assert spec is not None and spec.loader is not None
    onboard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(onboard)

    config = tmp_path / "config.json"
    config.write_text("FAKE_OLD")
    config.chmod(0o644)
    observed = {}
    real_replace = os.replace

    def inspect_replace(source, destination):
        observed["old_mode"] = config.stat().st_mode & 0o777
        observed["old_content"] = config.read_text()
        observed["new_mode"] = Path(source).stat().st_mode & 0o777
        observed["new_content"] = Path(source).read_text()
        real_replace(source, destination)

    monkeypatch.setattr(onboard.os, "replace", inspect_replace)
    previous_umask = os.umask(0)
    try:
        onboard.put(config, "FAKE_NEW", force=True)
    finally:
        os.umask(previous_umask)

    assert observed == {
        "old_mode": 0o644, "old_content": "FAKE_OLD",
        "new_mode": 0o600, "new_content": "FAKE_NEW",
    }
    assert config.read_text() == "FAKE_NEW"
    assert config.stat().st_mode & 0o777 == 0o600
    assert len(list(tmp_path.iterdir())) == 1


def test_preserved_file_content_kept_and_public_permissions_tightened(
    tmp_path, capsys,
):
    import importlib.util

    spec = importlib.util.spec_from_file_location("onboard_under_test", SOURCE / "scripts/onboard.py")
    assert spec is not None and spec.loader is not None
    onboard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(onboard)

    config = tmp_path / "config.json"
    config.write_text("FAKE_OLD")
    config.chmod(0o644)
    onboard.put(config, "FAKE_NEW", force=False)

    assert config.read_text() == "FAKE_OLD"
    assert config.stat().st_mode & 0o777 == 0o600
    assert "keeping it" in capsys.readouterr().err


@pytest.mark.parametrize(
    "secret_path",
    [".env", "home/.env", "home/.env.shared", "home/config/config.json"],
)
def test_secret_symlink_is_rejected_or_replaced_without_touching_target(
    onboarding, tmp_path: Path, secret_path: str,
):
    repo, home, run = onboarding
    link = repo / secret_path if secret_path == ".env" else home / secret_path.removeprefix("home/")
    target = tmp_path / "external-shared-file"
    target.write_text("FAKE_EXTERNAL")
    target.chmod(0o644)
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target)

    rejected = run()
    assert rejected.returncode == 2
    assert "symbolic link" in rejected.stderr
    assert "--force" in rejected.stderr
    assert "Traceback" not in rejected.stderr
    assert link.is_symlink()
    assert target.read_text() == "FAKE_EXTERNAL"
    assert target.stat().st_mode & 0o777 == 0o644

    replaced = run("--force")
    assert replaced.returncode == 0
    assert link.is_file() and not link.is_symlink()
    assert link.stat().st_mode & 0o777 == 0o600
    assert target.read_text() == "FAKE_EXTERNAL"
    assert target.stat().st_mode & 0o777 == 0o644


def test_new_file_is_private_even_with_open_umask(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location("onboard_under_test", SOURCE / "scripts/onboard.py")
    assert spec is not None and spec.loader is not None
    onboard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(onboard)

    config = tmp_path / "config.json"
    previous_umask = os.umask(0)
    try:
        onboard.put(config, "FAKE_NEW", force=False)
    finally:
        os.umask(previous_umask)
    assert config.stat().st_mode & 0o777 == 0o600


def test_invalid_timezone_has_short_input_error(onboarding):
    repo, _, run = onboarding
    result = run("--tz", "../etc/passwd")
    assert result.returncode == 2
    assert "timezone is not recognized" in result.stderr
    assert "Traceback" not in result.stderr
    assert not (repo / ".env").exists()


@pytest.mark.parametrize("language", ["ko", "de", "en"])
def test_explicit_bot_language_is_saved(onboarding, language):
    _, home, run = onboarding
    result = run("--language", language)
    assert result.returncode == 0, result.stderr
    assert json.loads((home / "config/config.json").read_text())["language"] == language
    texts = json.loads((SOURCE / "scripts/onboard_i18n" / f"{language}.json").read_text(encoding="utf-8"))
    assert texts["out_language"].format(language=language) in result.stdout


def test_invalid_bot_language_fails_before_any_home_write(onboarding):
    _, home, run = onboarding
    result = run("--language", "invalid-language")
    assert result.returncode != 0
    assert "language must be one of" in result.stderr
    assert not home.exists()



def test_existing_root_environment_keeps_missing_identity_settings(onboarding):
    repo, _home, run = onboarding
    legacy = b'LEGACY="kept"\n'
    (repo / ".env").write_bytes(legacy)
    result = run()
    assert result.returncode == 0, result.stderr
    assert (repo / ".env").read_bytes() == legacy
