"""Authenticated localhost secret RPC and the three real tool read paths."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import runpy
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from ductor_bot.infra.secret_runtime import (
    TOKEN_HEADER,
    SecretRuntime,
    secret_environment,
    start_secret_runtime,
    stop_secret_runtime,
)
from ductor_bot.infra.secret_vault import VaultError
from ductor_bot.multiagent.internal_api import InternalAgentAPI

TOOLS = Path(__file__).resolve().parents[2] / "ductor_bot/_home_defaults/workspace/tools"
MASTER = "sample-master-key"
VALUE = "sample-rpc-secret"


Service = tuple[SecretRuntime, TestClient[web.Request, web.Application], ModuleType]


def load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
async def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Service]:
    runtime = start_secret_runtime(tmp_path)
    api = InternalAgentAPI(secret_runtime=runtime)
    client = TestClient(TestServer(api._app))
    await client.start_server()
    monkeypatch.setenv("DUCTOR_SECRETS_FILE", str(runtime.vault.plaintext_path))
    for key, value in secret_environment().items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DUCTOR_INTERAGENT_HOST", "127.0.0.1")
    monkeypatch.setenv("DUCTOR_INTERAGENT_PORT", str(client.server.port))
    monkeypatch.setenv("GH_TOKEN", "")
    monkeypatch.setenv("GITHUB_TOKEN", "")
    loader = load_module("_vault_rpc_loader", TOOLS / "_secrets.py")
    monkeypatch.setitem(sys.modules, "_secrets", loader)
    yield runtime, client, loader
    await client.close()
    await stop_secret_runtime(runtime)


async def run_tool(*args: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *args, env=os.environ.copy(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode or 0, stdout.decode(), stderr.decode()


async def test_rpc_token_is_mandatory_for_values_and_status(service: Service) -> None:
    runtime, client, _ = service
    await runtime.vault.set("GH_TOKEN", VALUE)
    for token in (None, "wrong-token"):
        headers = {TOKEN_HEADER: token} if token else {}
        for path in ("/secrets/status", "/secrets/GH_TOKEN"):
            response = await client.get(path, headers=headers)
            assert response.status == 403
            assert await response.json() == {"error": "forbidden"}
            assert response.headers["Cache-Control"] == "no-store"
    headers = {TOKEN_HEADER: runtime.token}
    response = await client.get("/secrets/GH_TOKEN", headers=headers)
    assert response.status == 200
    assert await response.json() == {"value": VALUE}
    response = await client.get("/secrets/status", headers=headers)
    assert (await response.json())["state"] == "unconfigured"
    await runtime.vault.setup(MASTER)
    await runtime.vault.lock()
    response = await client.get("/secrets/GH_TOKEN", headers=headers)
    assert response.status == 423
    assert await response.json() == {"error": "SECRETS_LOCKED"}
    response = await client.get("/secrets/status", headers=headers)
    body = await response.json()
    assert body == {"state": "locked", "ttl_seconds": 0, "remaining_seconds": None}
    assert VALUE not in json.dumps(body)
    await runtime.vault.unlock(MASTER)
    response = await client.get("/secrets/MISSING", headers=headers)
    assert response.status == 404
    assert await response.json() == {"error": "SECRET_NOT_SET"}
    response = await client.get("/secrets", headers=headers)
    assert response.status == 404


async def test_rpc_maps_rate_limit(service: Service, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime, client, _ = service
    monkeypatch.setattr(
        runtime.vault,
        "get",
        AsyncMock(side_effect=VaultError("SECRETS_RATE_LIMITED", retry_after=8)),
    )
    response = await client.get("/secrets/GH_TOKEN", headers={TOKEN_HEADER: runtime.token})
    assert response.status == 429
    assert await response.json() == {"error": "SECRETS_RATE_LIMITED", "retry_after": 8}


async def test_all_three_read_paths_locked_then_unlocked(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _, loader = service
    await runtime.vault.set("GH_TOKEN", VALUE)
    await runtime.vault.setup(MASTER)
    await runtime.vault.lock()
    fill = load_module("_vault_fill", TOOLS / "browser_tools/fill_secret.py")
    monkeypatch.setattr(
        fill,
        "_post",
        lambda expression: json.dumps(
            {
                "result": "https://example.com" if expression == "location.origin" else len(VALUE),
                "insecure": False,
            }
        ),
    )
    for operation in (
        lambda: loader.load_secrets("GH_TOKEN"),
        lambda: fill.main(["--origin", "https://example.com", "#field", "GH_TOKEN"]),
    ):
        with pytest.raises(loader.SecretsError, match="SECRETS_LOCKED") as error:
            await asyncio.to_thread(operation)
        assert "Secrets store is locked - ask the owner to enter the master key" in str(error.value)
    # The shell protocol check happens before it invokes the Python helper.
    proc = await asyncio.create_subprocess_exec(
        "sh",
        str(TOOLS / "git_credential_from_ductor_env.sh"),
        "get",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate(b"protocol=https\nhost=github.com\n\n")
    assert proc.returncode == 1
    assert stdout == b""
    assert b"SECRETS_LOCKED" in stderr
    await runtime.vault.unlock(MASTER)
    assert await asyncio.to_thread(loader.store_secrets, "GH_TOKEN") == {"GH_TOKEN": VALUE}
    monkeypatch.setenv("GH_TOKEN", "environment-only-value")
    assert await asyncio.to_thread(loader.load_secrets, "GH_TOKEN") == {}
    assert await asyncio.to_thread(loader.get_secret, "GH_TOKEN") == "environment-only-value"
    expressions: list[str] = []

    def post(expression: str) -> str:
        expressions.append(expression)
        return json.dumps(
            {
                "result": "https://example.com" if expression == "location.origin" else len(VALUE),
                "insecure": False,
            }
        )

    monkeypatch.setattr(fill, "_post", post)
    assert (
        await asyncio.to_thread(
            fill.main, ["--origin", "https://example.com", "#field", "GH_TOKEN"]
        )
        == 0
    )
    assert VALUE in expressions[-1]
    assert "environment-only-value" not in expressions[-1]
    monkeypatch.setenv("GH_TOKEN", "")
    proc = await asyncio.create_subprocess_exec(
        "sh",
        str(TOOLS / "git_credential_from_ductor_env.sh"),
        "get",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate(b"protocol=https\nhost=github.com\n\n")
    assert proc.returncode == 0
    assert stdout == f"username=x-access-token\npassword={VALUE}\n".encode()
    assert stderr == b""
    assert await asyncio.to_thread(loader.load_secrets, "GH_TOKEN") == {"GH_TOKEN": VALUE}


async def test_loader_fail_closed_no_token_bad_token_other_path_and_no_bot(
    service: Service, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime, client, loader = service
    await runtime.vault.setup(MASTER)
    await runtime.vault.set("GH_TOKEN", VALUE)
    for token in ("", "wrong-token"):
        monkeypatch.setenv("DUCTOR_SECRETS_TOKEN", token)
        with pytest.raises(loader.SecretsError, match="SECRETS_LOCKED"):
            await asyncio.to_thread(loader.store_secrets, "GH_TOKEN")
    monkeypatch.setenv("DUCTOR_SECRETS_TOKEN", runtime.token)
    other = tmp_path / "other.env"
    other.with_name("other.env.enc").write_text("encrypted-marker")
    monkeypatch.setenv("DUCTOR_SECRETS_FILE", str(other))
    with pytest.raises(loader.SecretsError, match="SECRETS_LOCKED"):
        await asyncio.to_thread(loader.store_secrets, "GH_TOKEN")
    monkeypatch.setenv("DUCTOR_SECRETS_FILE", str(runtime.vault.plaintext_path))
    with pytest.raises(loader.SecretsError, match="SECRETS_KEYS_REQUIRED"):
        loader.load_secrets()
    await client.close()
    with pytest.raises(loader.SecretsError, match="SECRETS_LOCKED"):
        await asyncio.to_thread(loader.store_secrets, "GH_TOKEN")


async def test_status_tool_returns_metadata_only(service: Service) -> None:
    runtime, _, _ = service
    await runtime.vault.setup(MASTER)
    await runtime.vault.set("GH_TOKEN", VALUE)
    code, stdout, stderr = await run_tool(
        sys.executable, str(TOOLS / "secret_tools/secret_status.py")
    )
    assert code == 0
    assert json.loads(stdout) == {"state": "unlocked", "ttl_seconds": 0, "remaining_seconds": None}
    assert VALUE not in stdout
    assert runtime.token not in stdout
    assert stderr == ""


async def test_rpc_ignores_proxy_environment(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _, loader = service
    await runtime.vault.setup(MASTER)
    await runtime.vault.set("GH_TOKEN", VALUE)
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    assert await asyncio.to_thread(loader.store_secrets, "GH_TOKEN") == {"GH_TOKEN": VALUE}


def test_status_tool_projects_only_public_metadata(monkeypatch, capsys):
    module = ModuleType("_secrets")
    module.SecretsError = RuntimeError
    module.secret_status = lambda: {
        "state": "locked",
        "ttl_seconds": 900,
        "remaining_seconds": None,
        "value": VALUE,
        "master_key": MASTER,
        "token": "synthetic-boot-token",
    }
    monkeypatch.setitem(sys.modules, "_secrets", module)
    monkeypatch.setattr(sys, "path", list(sys.path))
    script = runpy.run_path(str(TOOLS / "secret_tools/secret_status.py"))
    assert script["main"]() == 0
    output = capsys.readouterr()
    assert json.loads(output.out) == {
        "state": "locked",
        "ttl_seconds": 900,
        "remaining_seconds": None,
    }
    assert output.err == ""
    assert VALUE not in output.out
    assert MASTER not in output.out
    assert "synthetic-boot-token" not in output.out
