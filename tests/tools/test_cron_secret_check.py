"""Secret-aware rule rendering and static cron inspection return names, never values."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from ductor_bot.cli.auth import AuthResult, AuthStatus
from ductor_bot.infra.secret_runtime import (
    TOKEN_HEADER,
    start_secret_runtime,
    stop_secret_runtime,
)
from ductor_bot.multiagent.internal_api import InternalAgentAPI
from ductor_bot.tasks.models import TaskSubmit
from ductor_bot.tasks.registry import TaskRegistry
from ductor_bot.workspace.paths import DuctorPaths
from ductor_bot.workspace.rules_selector import RulesSelector
from tests.workspace import test_rule_generators as generators

TOOLS = (
    Path(__file__).resolve().parents[2] / "ductor_bot/_home_defaults/workspace/tools"
)
CHECK = TOOLS / "cron_tools/cron_check_secrets.py"
CANARY = "DO_NOT_PRINT_TOKEN"
KEY = "synthetic-master-key"


@pytest.fixture
def load_tool(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DUCTOR_HOME", str(tmp_path))
    previous = sys.modules.get("_shared")

    def load(group: str, name: str):
        monkeypatch.syspath_prepend(str(TOOLS / group))
        sys.modules.pop("_shared", None)
        spec = importlib.util.spec_from_file_location(
            name, TOOLS / group / f"{name}.py"
        )
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    try:
        yield load
    finally:
        if previous is None:
            sys.modules.pop("_shared", None)
        else:
            sys.modules["_shared"] = previous


def run_check(home: Path, *args: str) -> tuple[subprocess.CompletedProcess[str], dict]:
    env = {
        **os.environ,
        "DUCTOR_HOME": str(home),
        "DUCTOR_SECRETS_FILE": str(home / "secrets.env"),
    }
    for key in ("DUCTOR_SECRETS_TOKEN", "DUCTOR_SECRETS_VAULT_FILE"):
        env.pop(key, None)
    result = subprocess.run(
        [sys.executable, str(CHECK), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert CANARY not in result.stdout
    assert CANARY not in result.stderr
    return result, json.loads(result.stdout)


def task(home: Path, description: str = "Read public weather.") -> Path:
    folder = home / "workspace/cron_tasks/daily"
    (folder / "scripts").mkdir(parents=True)
    (folder / "TASK_DESCRIPTION.md").write_text(description)
    return folder


def test_static_names_are_classified_without_printing_literals(tmp_path: Path):
    folder = task(
        tmp_path, 'Use EXISTING_TOKEN; load_secrets("MISSING_TOKEN"). Also CUSTOM_NAME.'
    )
    (tmp_path / "secrets.env").write_text(f"EXISTING_TOKEN={CANARY}\n")
    (folder / "scripts/job.py").write_text("""from _secrets import load_secrets
load_secrets("EXISTING_TOKEN", "MISSING_TOKEN")
value = "DO_NOT_PRINT_TOKEN"
""")
    (
        folder / "scripts/fill.sh"
    ).write_text("""python3 tools/browser_tools/fill_secret.py --origin https://example.invalid '#password' FORM_PASSWORD
SHELL_TOKEN="DO_NOT_PRINT_TOKEN"
echo '--env DO_NOT_PRINT_TOKEN'
""")
    result, body = run_check(tmp_path, "daily", "--key", "CUSTOM_NAME")
    assert result.returncode == 0
    assert body["requires_secrets"]
    assert body["known"] == ["EXISTING_TOKEN"]
    assert body["unknown"] == [
        "CUSTOM_NAME",
        "FORM_PASSWORD",
        "MISSING_TOKEN",
        "SHELL_TOKEN",
    ]
    assert body["indicators"] == ["fill_secret", "load_secrets"]
    assert body["inventory_available"]
    assert "SECRETS_LOCKED" in body["warning"]
    assert not any(key in body for key in ("values", "source", "lines", "matches"))


def test_negative_result_is_not_a_guarantee(tmp_path: Path):
    folder = task(tmp_path)
    (folder / "scripts/public.py").write_text(f'output = "{CANARY}"\n')
    result, body = run_check(tmp_path, str(folder))
    assert result.returncode == 0
    assert not body["requires_secrets"]
    assert body["keys"] == []
    assert body["warning"] is None
    assert "no evidence" in body["limitations"][0]


def test_locked_inventory_does_not_fall_back_to_plaintext(tmp_path: Path):
    folder = task(tmp_path)
    (folder / "scripts/job.py").write_text('load_secrets("EXISTING_TOKEN")\n')
    (tmp_path / "secrets.env").write_text(f"SHOULD_NOT_BE_LISTED={CANARY}\n")
    (tmp_path / "secrets.env.enc").write_text("synthetic-ciphertext")
    result, body = run_check(tmp_path, "daily")
    assert result.returncode == 0
    assert body["keys"] == ["EXISTING_TOKEN"]
    assert not body["inventory_available"]
    assert "known" not in body
    assert "unknown" not in body
    assert "SECRETS_LOCKED" in " ".join(body["limitations"])


@pytest.mark.parametrize("case", ["syntax", "link", "missing-description"])
def test_incomplete_scan_reports_safe_error(tmp_path: Path, case: str):
    folder = task(tmp_path)
    if case == "syntax":
        (folder / "scripts/broken.py").write_text(
            f'load_secrets("BROKEN_TOKEN") {CANARY}\n'
        )
    elif case == "link":
        external = tmp_path / "outside.py"
        external.write_text(f'load_secrets("{CANARY}")\n')
        (folder / "scripts/linked.py").symlink_to(external)
    else:
        (folder / "TASK_DESCRIPTION.md").unlink()
    result, body = run_check(tmp_path, "daily")
    assert result.returncode == 1
    assert body["error"] == "SCAN_INCOMPLETE"
    assert body["skipped_files"] == 1


def test_invalid_task_path_never_echoes_it(tmp_path: Path):
    outside = tmp_path / CANARY
    outside.mkdir()
    result, body = run_check(tmp_path, str(outside))
    assert result.returncode == 1
    assert body == {"error": "INVALID_TASK_FOLDER"}


def helper():
    spec = importlib.util.spec_from_file_location(
        "secret_inventory_test", TOOLS / "_secrets.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_plain_inventory_never_decodes_values_or_loads_environment(
    tmp_path: Path, monkeypatch
):
    path = tmp_path / "secrets.env"
    path.write_bytes(
        b"EXISTING_TOKEN=\xff\xfe\nOTHER_PASSWORD=$REFERENCE\n# IGNORE_TOKEN=value\n"
    )
    monkeypatch.setenv("DUCTOR_SECRETS_FILE", str(path))
    monkeypatch.delenv("EXISTING_TOKEN", raising=False)
    module = helper()
    module._parse = MagicMock(side_effect=AssertionError("value parser must not run"))
    assert module.secret_keys() == ["EXISTING_TOKEN", "OTHER_PASSWORD"]
    module._parse.assert_not_called()
    assert "EXISTING_TOKEN" not in os.environ


async def test_internal_inventory_is_authenticated_and_names_only(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("DUCTOR_SECRETS_FILE", str(tmp_path / "secrets.env"))
    runtime = start_secret_runtime(tmp_path)
    api = InternalAgentAPI(MagicMock(), secret_runtime=runtime)
    client = TestClient(TestServer(api._app))
    await client.start_server()
    try:
        for headers in ({}, {TOKEN_HEADER: "incorrect-token"}):
            response = await client.get("/secrets/keys", headers=headers)
            assert response.status == 403
            assert "WWW-Authenticate" not in response.headers
        await runtime.vault.set("EXISTING_TOKEN", CANARY)
        await runtime.vault.setup(KEY)
        headers = {TOKEN_HEADER: runtime.token}
        response = await client.get("/secrets/keys", headers=headers)
        assert response.status == 200
        assert await response.json() == {"keys": ["EXISTING_TOKEN"]}
        assert response.headers["Cache-Control"] == "no-store"
        assert CANARY not in await response.text()
        port = client.server.port
        monkeypatch.setenv("DUCTOR_SECRETS_TOKEN", runtime.token)
        monkeypatch.setenv(
            "DUCTOR_SECRETS_VAULT_FILE", str(runtime.vault.plaintext_path)
        )
        monkeypatch.setenv("DUCTOR_INTERAGENT_PORT", str(port))
        module = helper()
        module._parse = MagicMock(side_effect=AssertionError("no plaintext fallback"))
        import asyncio

        assert await asyncio.to_thread(module.secret_keys) == ["EXISTING_TOKEN"]
        await runtime.vault.lock()
        response = await client.get("/secrets/keys", headers=headers)
        assert response.status == 423
        assert await response.json() == {"error": "SECRETS_LOCKED"}
        with pytest.raises(module.SecretsError, match="SECRETS_LOCKED"):
            await asyncio.to_thread(module.secret_keys)
        module._parse.assert_not_called()
    finally:
        await client.close()
        await stop_secret_runtime(runtime)


def assert_guidance(content: str):
    for phrase in (
        "tools/secret_tools/secret_status.py",
        "SECRETS_LOCKED",
        "SECRETS_RATE_LIMITED",
        "/masterkey unlock",
        "Never ask",
        "Never print",
        "load_secrets",
        "plaintext",
        "webchat",
    ):
        assert phrase in content


@pytest.mark.parametrize(
    "providers", [("claude",), ("codex",), ("claude", "codex", "gemini")]
)
def test_common_rule_rendering_carries_secret_contract(tmp_path: Path, providers):
    auth = {
        provider: AuthResult(provider, AuthStatus.AUTHENTICATED)
        for provider in providers
    }
    paths = DuctorPaths(ductor_home=tmp_path)
    with patch("ductor_bot.cli.auth.check_all_auth", return_value=auth):
        RulesSelector(paths).deploy_rules()
    for provider, filename in (
        ("claude", "CLAUDE.md"),
        ("codex", "AGENTS.md"),
        ("gemini", "GEMINI.md"),
    ):
        if provider not in providers:
            continue
        for relative in (
            "workspace",
            "workspace/tools",
            "workspace/cron_tasks",
            "workspace/tools/cron_tools",
        ):
            assert_guidance((tmp_path / relative / filename).read_text())
        assert (
            "cron_check_secrets.py"
            in (tmp_path / "workspace/cron_tasks" / filename).read_text()
        )


def test_background_and_webhook_rules_include_secret_contract(
    tmp_path: Path, load_tool
):
    registry = TaskRegistry(tmp_path / "tasks.json", tmp_path / "workspace/tasks")
    entry = registry.create(
        TaskSubmit(
            chat_id=1,
            prompt="Read public data",
            message_id=1,
            thread_id=None,
            parent_agent="main",
        ),
        "claude",
        "sonnet",
    )
    for filename in ("CLAUDE.md", "AGENTS.md", "GEMINI.md"):
        assert_guidance((registry.task_folder(entry.task_id) / filename).read_text())
    generators.seed_parent(tmp_path)
    tool = load_tool("webhook_tools", "webhook_add")
    folder = tool._create_task_folder("hook", "Hook", "Read public data")
    for filename in ("CLAUDE.md", "AGENTS.md", "GEMINI.md"):
        assert_guidance((folder / filename).read_text())


def test_cron_creation_reports_final_check_and_renders_safe_rules(tmp_path: Path):
    from tests.cron.test_cron_add_tool import _full_args, _run_tool

    result = _run_tool(tmp_path, _full_args("secret-aware"))
    assert result.returncode == 0
    body = json.loads(result.stdout)
    actions = " ".join(body["action_required"])
    assert "cron_check_secrets.py" in actions
    assert "SECRETS_LOCKED" in actions
    assert "even if the static check is negative" in actions
    assert_guidance(
        (tmp_path / "workspace/cron_tasks/secret-aware/CLAUDE.md").read_text()
    )
