"""Autocompact API validation, rollback, request propagation and browser guards."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ductor_bot.cli.executor import build_subprocess_env
from ductor_bot.cli.types import AgentResponse
from ductor_bot.config import AgentConfig
from ductor_bot.multiagent.models import merge_sub_agent_config
from ductor_bot.multiagent.registry import AgentRegistry
from ductor_bot.orchestrator.core import Orchestrator
from ductor_bot.session import SessionData, SessionKey
from ductor_bot.workspace.paths import DuctorPaths
from tests.webchat.test_bridge_api import (
    conversation as conversation,  # noqa: PLC0414 -- register shared pytest fixture
)
from tests.webchat.test_bridge_api import headers
from tests.webchat.test_web_app import login
from tests.webchat.test_web_app import (
    site as site,  # noqa: PLC0414 -- register shared pytest fixture
)

PATH = "/webchat/v1/topics/-123.7/autocompact"
BROWSER_PATH = "/api/topics/-123.7/autocompact"


@pytest.fixture
async def api(conversation):
    h = conversation
    h.bot.config.ductor_home = str(h.paths.ductor_home)
    h.paths.config_path.parent.mkdir(parents=True, exist_ok=True)
    h.paths.config_path.write_text("{}")
    h.bot._orch = Orchestrator(h.bot.config, h.paths, lock_pool=h.pool)
    h.bot._sequential = SimpleNamespace(get_lock=h.pool.get)
    h.bot._orch._cli_service.execute = AsyncMock(return_value=AgentResponse(result="answer"))
    return h


async def _put(api, body, path=PATH):
    return await api.client.put(path, json=body, headers=headers())


async def test_read_defaults_does_not_write_or_call_cli(api):
    before = api.paths.sessions_path.read_text()
    response = await api.client.get(PATH, headers=headers())
    assert response.status == 200
    assert await response.json() == {
        "topic_value": None,
        "global_value": "default",
        "effective_value": "default",
        "provider": "claude",
        "supported": True,
        "unsupported_reason": None,
    }
    assert api.paths.sessions_path.read_text() == before
    api.bot._orch._cli_service.execute.assert_not_called()


@pytest.mark.parametrize("method", ["get", "put"])
@pytest.mark.parametrize("token", [None, "wrong"])
async def test_bridge_token_required(api, method, token):
    response = await getattr(api.client, method)(
        PATH,
        headers={} if token is None else {"X-Dtw-Bridge-Token": token},
        json={"topic": "off"} if method == "put" else None,
    )
    assert response.status == 403
    assert "WWW-Authenticate" not in response.headers


async def test_save_both_and_one_scope_with_explicit_default_and_inheritance(api):
    response = await _put(api, {"global": 200000, "topic": 400000})
    assert response.status == 200
    state = await response.json()
    assert state["topic_value"] == state["effective_value"] == 400000
    assert state["global_value"] == 200000
    assert json.loads(api.paths.config_path.read_text())["autocompact"] == 200000
    response = await _put(api, {"topic": "default"})
    assert (await response.json())["effective_value"] == "default"
    response = await _put(api, {"global": "off"})
    assert (await response.json())["effective_value"] == "default"
    response = await _put(api, {"topic": None})
    state = await response.json()
    assert state["effective_value"] == state["global_value"] == "off"
    assert state["topic_value"] is None


@pytest.mark.parametrize(
    "body",
    [
        {},
        [],
        {"global": None},
        {"topic": 99999, "global": 200000},
        {"topic": 400000, "global": 1000001},
        {"topic": True},
        {"topic": 100000.0},
        {"topic": "100000"},
        {"topic": "inherit"},
        {"topic": {}},
        {"topic": []},
        {"topic": "off", "unexpected": "field"},
    ],
)
async def test_invalid_values_never_write_either_scope(api, body, monkeypatch):
    save = AsyncMock()
    monkeypatch.setattr("ductor_bot.webchat.bridge.save_autocompact", save)
    response = await _put(api, body)
    assert response.status == 400
    assert await response.json() == {"error": "bad_request"}
    save.assert_not_called()
    assert api.bot._orch._config.autocompact == "default"
    assert (await api.bot._orch._sessions.get_active(api.key)).autocompact is None


@pytest.mark.parametrize("value", [100000, 1000000])
async def test_token_range_endpoints(api, value):
    response = await _put(api, {"topic": value})
    assert response.status == 200
    assert (await response.json())["effective_value"] == value


@pytest.mark.parametrize("body", ["{", "null", '"text"'])
async def test_malformed_json_and_nonobject_body(api, body):
    response = await api.client.put(PATH, data=body, headers=headers())
    assert response.status == 400


@pytest.mark.parametrize("tid", ["missing", "-123.99", "other:123.7"])
async def test_unknown_topics_are_not_created(api, tid):
    url = f"/webchat/v1/topics/{tid}/autocompact"
    for method in ("get", "put"):
        response = await getattr(api.client, method)(url, json={"topic": "off"}, headers=headers())
        assert response.status == 404


async def test_topic_changes_are_isolated(api):
    other = SessionKey(chat_id=-123, topic_id=8)
    await api.bot._orch._sessions.update_session(SessionData(chat_id=-123, topic_id=8))
    await _put(api, {"topic": 300000})
    assert (await api.bot._orch._sessions.get_active(other)).autocompact is None
    response = await api.client.get("/webchat/v1/topics/-123.8/autocompact", headers=headers())
    assert (await response.json())["effective_value"] == "default"


@pytest.mark.parametrize(
    ("provider", "value", "supported", "reason"),
    [
        ("claude", "off", True, None),
        ("codex", "off", False, "off_unsupported"),
        ("codex", 300000, True, None),
        ("grok", 300000, False, "provider_unsupported"),
        ("gemini", "default", False, "provider_unsupported"),
    ],
)
async def test_support_follows_effective_value_and_current_provider(
    api, provider, value, supported, reason
):
    session = await api.bot._orch._sessions.get_active(api.key)
    session.provider = provider
    await api.bot._orch._sessions._save({api.key.storage_key: session})
    response = await _put(api, {"topic": value})
    assert response.status == 200
    state = await response.json()
    assert state["provider"] == provider
    assert state["supported"] is supported
    assert state["unsupported_reason"] == reason


async def test_second_save_failure_restores_global_and_returns_actual_state(api, monkeypatch):
    await _put(api, {"global": 200000})
    monkeypatch.setattr(
        api.bot._orch._sessions, "set_autocompact", AsyncMock(side_effect=OSError("synthetic"))
    )
    response = await _put(api, {"global": 400000, "topic": 300000})
    assert response.status == 503
    state = await response.json()
    assert state["error"] == "generic"
    assert state["global_value"] == state["effective_value"] == 200000
    assert state["topic_value"] is None
    assert json.loads(api.paths.config_path.read_text())["autocompact"] == 200000
    assert api.bot._orch._cli_service._config.autocompact == 200000


async def test_concurrent_global_save_waits_until_rollback_finishes(api, monkeypatch):
    await api.bot._orch._sessions.update_session(SessionData(chat_id=-123, topic_id=8))
    entered, release = asyncio.Event(), asyncio.Event()

    async def fail_topic(_key, _value):
        entered.set()
        await release.wait()
        raise OSError("synthetic")

    monkeypatch.setattr(api.bot._orch._sessions, "set_autocompact", fail_topic)
    first = asyncio.create_task(_put(api, {"global": 300000, "topic": 400000}))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        second = asyncio.create_task(
            _put(api, {"global": 500000}, "/webchat/v1/topics/-123.8/autocompact")
        )
        await asyncio.sleep(0)
        assert not second.done()
    finally:
        release.set()
    assert (await asyncio.wait_for(first, 2)).status == 503
    assert (await asyncio.wait_for(second, 2)).status == 200
    assert api.bot._orch._config.autocompact == 500000


@pytest.mark.parametrize("value", [300000, "off", "default", None])
async def test_saved_value_reaches_next_request_argv_and_env(api, value, monkeypatch):
    monkeypatch.setattr(
        "ductor_bot.cli.claude_provider.claude_autocompact_flag_supported", lambda _: True
    )
    await _put(api, {"global": 200000, "topic": value})
    orch = api.bot._orch
    await orch.handle_message(api.key, "hello")
    request = orch._cli_service.execute.await_args.args[0]
    expected = 200000 if value is None else value
    assert request.autocompact == expected
    cli = orch._cli_service._make_cli(request)
    cmd = cli._build_command("hello")
    env = build_subprocess_env(cli._config)
    assert env["DUCTOR_AUTOCOMPACT"] == str(expected)
    assert ("--autocompact" in cmd) == isinstance(expected, int)
    if isinstance(expected, int):
        assert cmd[cmd.index("--autocompact") + 1] == str(expected)
        assert env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == str(expected)
    elif expected == "off":
        assert env["DISABLE_AUTO_COMPACT"] == "1"


async def test_sub_global_uses_independent_registry_and_config(api):
    root = api.paths.ductor_home
    subhome = root / "agents" / "worker"
    paths = DuctorPaths(ductor_home=subhome)
    paths.config_path.parent.mkdir(parents=True)
    paths.config_path.write_text("{}")
    registry = root / "agents.json"
    registry.write_text(json.dumps([{"name": "worker", "transport": "headless"}]))
    cfg = api.bot.config.model_copy(update={"ductor_home": str(subhome), "autocompact": "default"})
    api.bot._orch = Orchestrator(cfg, paths, agent_name="worker", lock_pool=api.pool)
    await api.bot._orch._sessions.update_session(SessionData(chat_id=-123, topic_id=7))
    response = await _put(api, {"global": 400000})
    assert response.status == 200
    assert api.bot.config.autocompact == "default"
    assert json.loads(paths.config_path.read_text())["autocompact"] == 400000
    restarted = merge_sub_agent_config(
        AgentConfig(autocompact=1000000), AgentRegistry(registry).load()[0], subhome
    )
    assert restarted.autocompact == 400000


@pytest.mark.parametrize("method", ["get", "put"])
async def test_browser_requires_session(site, method):
    client, _ = site
    response = await getattr(client, method)(
        BROWSER_PATH, json={"topic": "off"}, headers={"X-Dtw-Client": "web"}
    )
    assert response.status == 403
    assert "WWW-Authenticate" not in response.headers


@pytest.mark.parametrize("missing", ["X-Dtw-Client", "X-Dtw-Csrf", "wrong-csrf"])
async def test_browser_save_requires_client_header_and_csrf(site, missing):
    client, fake = site
    auth, _ = await login(client)
    if missing == "wrong-csrf":
        auth["X-Dtw-Csrf"] = "invalid"
    else:
        auth.pop(missing)
    response = await client.put(BROWSER_PATH, json={"topic": "off"}, headers=auth)
    assert response.status == 403
    assert "WWW-Authenticate" not in response.headers
    assert not any(call[1].endswith("/autocompact") for call in fake.calls)


async def test_browser_authenticated_relay_preserves_path_body_and_bridge_token(site):
    client, fake = site
    auth, _ = await login(client)
    response = await client.get(BROWSER_PATH, headers=auth)
    assert response.status == 200
    response = await client.put(BROWSER_PATH, json={"topic": None, "global": 300000}, headers=auth)
    assert response.status == 200
    forwarded = [call for call in fake.calls if call[1].endswith("/autocompact")]
    assert [(call[0], call[1], call[3]) for call in forwarded] == [
        ("GET", "/topics/-123.7/autocompact", None),
        ("PUT", "/topics/-123.7/autocompact", {"topic": None, "global": 300000}),
    ]


async def test_failed_rollback_returns_actual_global_value(api, monkeypatch, caplog):
    from ductor_bot.orchestrator.selectors import autocompact_selector as selector

    original = selector.update_config_file_async
    calls = 0

    async def write_then_fail(path, **updates):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic-private-path")
        await original(path, **updates)

    monkeypatch.setattr(selector, "update_config_file_async", write_then_fail)
    monkeypatch.setattr(
        api.bot._orch._sessions, "set_autocompact", AsyncMock(side_effect=OSError("synthetic"))
    )
    response = await _put(api, {"global": 400000, "topic": 300000})
    assert response.status == 503
    state = await response.json()
    assert state["error"] == "generic"
    assert state["global_value"] == state["effective_value"] == 400000
    assert state["topic_value"] is None
    assert json.loads(api.paths.config_path.read_text())["autocompact"] == 400000
    assert "Autocompact global rollback failed" in caplog.text
    assert "synthetic-private-path" not in caplog.text


async def test_first_save_failure_does_not_write_topic(api, monkeypatch):
    monkeypatch.setattr(
        "ductor_bot.orchestrator.selectors.autocompact_selector.update_config_file_async",
        AsyncMock(side_effect=OSError("synthetic")),
    )
    topic_save = AsyncMock()
    monkeypatch.setattr(api.bot._orch._sessions, "set_autocompact", topic_save)
    response = await _put(api, {"global": 400000, "topic": 300000})
    assert response.status == 503
    state = await response.json()
    assert state["global_value"] == state["effective_value"] == "default"
    assert state["topic_value"] is None
    topic_save.assert_not_called()
