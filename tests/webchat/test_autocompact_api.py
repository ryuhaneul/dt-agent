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
from ductor_bot.i18n import t
from ductor_bot.multiagent.models import merge_sub_agent_config
from ductor_bot.multiagent.registry import AgentRegistry
from ductor_bot.orchestrator.core import Orchestrator
from ductor_bot.session import SessionData, SessionKey
from ductor_bot.workspace.paths import DuctorPaths
from tests.cli.test_autocompact import TOKEN_INPUT_CASES
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


async def test_get_and_save_during_turn_takes_effect_on_next_turn(api):
    orch = api.bot._orch
    entered, release = asyncio.Event(), asyncio.Event()
    configured = []

    async def execute(request):
        configured.append(orch._cli_service._make_cli(request)._config.autocompact)
        entered.set()
        await release.wait()
        return AgentResponse(result="answer")

    async def turn(text):
        async with api.pool.get(api.key.lock_key):
            return await orch.handle_message(api.key, text)

    orch._cli_service.execute = AsyncMock(side_effect=execute)
    assert (await _put(api, {"topic": 200000})).status == 200
    active = asyncio.create_task(turn("first turn"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        response = await asyncio.wait_for(api.client.get(PATH, headers=headers()), 2)
        assert response.status == 200
        assert (await response.json())["effective_value"] == 200000

        response = await asyncio.wait_for(_put(api, {"topic": None, "global": 400000}), 2)
        assert response.status == 200
        assert (await response.json())["effective_value"] == 400000

        response = await asyncio.wait_for(api.client.get(PATH, headers=headers()), 2)
        assert response.status == 200
        assert (await response.json())["effective_value"] == 400000
        assert not active.done()
        assert configured == [200000]
    finally:
        release.set()
        await active

    await turn("next turn")
    assert configured == [200000, 400000]


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
        {"topic": "99k"},
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
    assert request.autocompact == value
    cli = orch._cli_service._make_cli(request)
    cmd = cli._build_command("hello")
    env = build_subprocess_env(cli._config)
    assert env["DUCTOR_AUTOCOMPACT"] == ("inherit" if value is None else str(expected))
    assert ("--autocompact" in cmd) == isinstance(expected, int)
    if isinstance(expected, int):
        assert cmd[cmd.index("--autocompact") + 1] == str(expected)
        assert env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == str(expected)
    elif expected == "off":
        assert env["DISABLE_AUTO_COMPACT"] == "1"


async def test_sub_global_uses_independent_registry(api):
    root = api.paths.ductor_home
    subhome = root / "agents" / "worker"
    paths = DuctorPaths(ductor_home=subhome)
    paths.config_path.parent.mkdir(parents=True)
    registry = root / "agents.json"
    registry.write_text(json.dumps([{"name": "worker", "transport": "headless"}]))
    cfg = api.bot.config.model_copy(update={"ductor_home": str(subhome), "autocompact": "default"})
    cfg._main_home = api.paths.ductor_home
    api.bot._orch = Orchestrator(cfg, paths, agent_name="worker", lock_pool=api.pool)
    await api.bot._orch._sessions.update_session(SessionData(chat_id=-123, topic_id=7))
    response = await _put(api, {"global": 400000})
    assert response.status == 200
    assert api.bot.config.autocompact == "default"
    assert not paths.config_path.exists()
    assert json.loads(registry.read_text())[0]["autocompact"] == 400000
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


AUTO_PATH = PATH + "/automations"
AUTO_BROWSER_PATH = BROWSER_PATH + "/automations"


@pytest.fixture
async def automation_api(api):
    from tests.orchestrator.test_autocompact_automation import seed

    seed(api.bot._orch)
    return api


async def test_automation_list_is_public_metadata_and_agent_local(automation_api):
    h = automation_api
    response = await h.client.get(AUTO_PATH, headers=headers())
    assert response.status == 200
    state = await response.json()
    assert len(state["items"]) == 2
    assert all(
        set(item) == {"kind", "id", "name", "schedule", "enabled", "value", "effective_value"}
        for item in state["items"]
    )
    assert "private" not in json.dumps(state)
    h.bot._orch._config.autocompact = 400000
    state = await (await h.client.get(AUTO_PATH, headers=headers())).json()
    assert state["items"][1]["effective_value"] == 400000
    # Same topic ID in a sub-agent still sees only that agent's registry.
    subhome = h.paths.ductor_home / "agents" / "worker"
    subpaths = DuctorPaths(ductor_home=subhome)
    cfg = h.bot.config.model_copy(update={"ductor_home": str(subhome)})
    h.bot._orch = Orchestrator(cfg, subpaths, agent_name="worker", lock_pool=h.pool)
    await h.bot._orch._sessions.update_session(SessionData(chat_id=-123, topic_id=7))
    response = await h.client.get(AUTO_PATH, headers=headers())
    assert (await response.json())["items"] == []
    response = await h.client.put(
        AUTO_PATH + "/cron/job-0", json={"value": "off"}, headers=headers()
    )
    assert response.status == 404


@pytest.mark.parametrize(
    ("kind", "item_id", "value"),
    [
        ("cron", "job-0", 100000),
        ("cron", "job-0", 1000000),
        ("cron", "job-0", "default"),
        ("cron", "job-0", None),
        ("cron", "job-0", "off"),
        ("webhook", "hook", None),
        ("webhook", "hook", 300000),
    ],
)
async def test_automation_save_roundtrip_and_next_execution(
    automation_api, kind, item_id, value, monkeypatch
):
    from ductor_bot.cli.param_resolver import TaskOverrides, resolve_cli_config
    from ductor_bot.cron.execution import build_cmd

    h = automation_api
    h.bot._orch._config.autocompact = 500000
    response = await h.client.put(
        AUTO_PATH + f"/{kind}/{item_id}", json={"value": value}, headers=headers()
    )
    assert response.status == 200
    state = await response.json()
    item = next(item for item in state["items"] if item["kind"] == kind)
    assert item["value"] == value
    config = resolve_cli_config(
        h.bot._orch._config, None, task_overrides=TaskOverrides(autocompact=item["value"])
    )
    monkeypatch.setattr(
        "ductor_bot.cron.execution.claude_autocompact_flag_supported", lambda _: True
    )
    monkeypatch.setattr("ductor_bot.cron.execution.which", lambda _: "/fake/claude")
    command = build_cmd(config, "hello")
    assert command is not None
    expected = 500000 if value is None else value
    assert command.env_overrides["DUCTOR_AUTOCOMPACT"] == ("inherit" if value is None else str(expected))
    assert ("--autocompact" in command.cmd) == isinstance(expected, int)
    if isinstance(expected, int):
        assert command.cmd[command.cmd.index("--autocompact") + 1] == str(expected)
    assert (await h.bot._orch._sessions.get_active(h.key)).autocompact is None
    readback = await (await h.client.get(AUTO_PATH, headers=headers())).json()
    assert readback == state


@pytest.mark.parametrize(
    ("kind", "body"),
    [
        ("cron", {"value": 99999}),
        ("webhook", {"value": 1000001}),
        ("cron", {"value": True}),
        ("webhook", {"value": "inherit"}),
        ("cron", {"value": "500.0001k"}),
        ("cron", {"value": 300000.0}),
        ("cron", {}),
        ("cron", {"value": "off", "extra": 1}),
        ("task", {"value": "off"}),
    ],
)
async def test_automation_invalid_body_does_not_write(automation_api, kind, body):
    h = automation_api
    before = (h.paths.cron_jobs_path.read_text(), h.paths.webhooks_path.read_text())
    response = await h.client.put(AUTO_PATH + f"/{kind}/job-0", json=body, headers=headers())
    assert response.status == 400
    assert before == (h.paths.cron_jobs_path.read_text(), h.paths.webhooks_path.read_text())


@pytest.mark.parametrize("kind", ["cron", "webhook"])
async def test_automation_missing_item_returns_404(automation_api, kind):
    response = await automation_api.client.put(
        AUTO_PATH + f"/{kind}/missing", json={"value": "off"}, headers=headers()
    )
    assert response.status == 404


@pytest.mark.parametrize(("method", "suffix"), [("get", ""), ("put", "/cron/job-0")])
async def test_automation_bridge_requires_token(automation_api, method, suffix):
    response = await getattr(automation_api.client, method)(
        AUTO_PATH + suffix, json={"value": "off"}
    )
    assert response.status == 403
    assert "WWW-Authenticate" not in response.headers


@pytest.mark.parametrize(("method", "suffix"), [("get", ""), ("put", "/cron/job-0")])
async def test_automation_browser_requires_session(site, method, suffix):
    client, _ = site
    response = await getattr(client, method)(
        AUTO_BROWSER_PATH + suffix, json={"value": "off"}, headers={"X-Dtw-Client": "web"}
    )
    assert response.status == 403
    assert "WWW-Authenticate" not in response.headers


@pytest.mark.parametrize("missing", ["X-Dtw-Client", "X-Dtw-Csrf", "wrong-csrf"])
async def test_automation_browser_requires_csrf_and_client(site, missing):
    client, fake = site
    auth, _ = await login(client)
    if missing == "wrong-csrf":
        auth["X-Dtw-Csrf"] = "bad"
    else:
        auth.pop(missing)
    response = await client.put(
        AUTO_BROWSER_PATH + "/cron/job-0", json={"value": "off"}, headers=auth
    )
    assert response.status == 403
    assert "WWW-Authenticate" not in response.headers
    assert not any("/automations" in call[1] for call in fake.calls)


async def test_automation_browser_relay_forwards_authenticated_contract(site):
    client, fake = site
    auth, _ = await login(client)
    assert (await client.get(AUTO_BROWSER_PATH, headers=auth)).status == 200
    assert (
        await client.put(AUTO_BROWSER_PATH + "/webhook/hook", json={"value": None}, headers=auth)
    ).status == 200
    calls = [(call[0], call[1], call[3]) for call in fake.calls if "/automations" in call[1]]
    assert calls == [
        ("GET", "/topics/-123.7/autocompact/automations", None),
        ("PUT", "/topics/-123.7/autocompact/automations/webhook/hook", {"value": None}),
    ]


async def test_automation_save_error_is_safe_and_does_not_report_success(
    automation_api, monkeypatch
):
    monkeypatch.setattr(
        automation_api.bot._orch._cron_manager,
        "set_autocompact",
        lambda *_: (_ for _ in ()).throw(OSError("private-path")),
    )
    response = await automation_api.client.put(
        AUTO_PATH + "/cron/job-0", json={"value": "off"}, headers=headers()
    )
    assert response.status == 503
    assert await response.json() == {"error": "generic"}


async def _sub_setting_fixture(api, *, absent=False):
    previous = "default" if absent else 200000
    paths = DuctorPaths(ductor_home=api.paths.ductor_home / "agents" / "worker")
    paths.config_path.parent.mkdir(parents=True)
    worker = {"name": "worker", "transport": "headless", "custom": "keep"}
    if not absent:
        worker["autocompact"] = previous
    registry = api.paths.ductor_home / "agents.json"
    registry.write_text(json.dumps([worker, {"name": "other", "autocompact": 900000}]))
    cfg = api.bot.config.model_copy(
        update={"ductor_home": str(paths.ductor_home), "autocompact": previous}
    )
    cfg._main_home = api.paths.ductor_home
    api.bot._orch = Orchestrator(cfg, paths, agent_name="worker", lock_pool=api.pool)
    api.bot._orch._cli_service.execute = AsyncMock(return_value=AgentResponse(result="ok"))
    await api.bot._orch._sessions.update_session(SessionData(chat_id=-123, topic_id=7))
    return paths, registry, previous


async def _change_sub_setting(api, transport):
    if transport == "web":
        response = await _put(api, {"global": 400000, "topic": 300000})
        return response.status, await response.json()
    from ductor_bot.orchestrator.selectors.autocompact_selector import save_autocompact

    response = await save_autocompact(api.bot._orch, api.key, "global", 400000)
    return (200 if response.buttons else 503), response.text


@pytest.mark.parametrize("transport", ["selector", "web"])
@pytest.mark.parametrize("absent", [False, True])
async def test_sub_global_registry_write_failure_preserves_state(
    api, monkeypatch, transport, absent
):
    from ductor_bot.infra import json_store

    paths, registry, previous = await _sub_setting_fixture(api, absent=absent)
    before = registry.read_bytes()

    def fail_write(path, _content):
        assert path == registry
        assert api.bot._orch._config.autocompact == previous
        raise OSError("synthetic")

    monkeypatch.setattr(json_store, "atomic_text_save", fail_write)
    status, body = await _change_sub_setting(api, transport)
    assert status == 503
    assert not paths.config_path.exists()
    assert registry.read_bytes() == before
    assert api.bot._orch._config.autocompact == previous
    assert api.bot._orch._cli_service._config.autocompact == previous
    assert (await api.bot._orch._sessions.get_active(api.key)).autocompact is None
    api.bot._orch._cli_service.execute.assert_not_called()
    if transport == "web":
        assert body["error"] == "generic"
        assert body["global_value"] == previous
        assert body["topic_value"] is None
    else:
        assert body == t("autocompact.save_error")


@pytest.mark.parametrize("transport", ["selector", "web"])
async def test_sub_global_success_updates_registry_runtime_and_next_request(api, transport):
    paths, registry, _ = await _sub_setting_fixture(api)
    assert (await _change_sub_setting(api, transport))[0] == 200
    assert not paths.config_path.exists()
    assert json.loads(registry.read_text()) == [
        {"name": "worker", "transport": "headless", "custom": "keep", "autocompact": 400000},
        {"name": "other", "autocompact": 900000},
    ]
    orch = api.bot._orch
    assert orch._config.autocompact == orch._cli_service._config.autocompact == 400000
    await orch.handle_message(api.key, "hello")
    request = orch._cli_service.execute.await_args.args[0]
    assert request.autocompact == (300000 if transport == "web" else None)
    assert orch._cli_service._make_cli(request)._config.autocompact == (
        300000 if transport == "web" else 400000
    )


@pytest.mark.parametrize("registry_text", [None, "{broken", "{}", '[{"name":"other"}]'])
async def test_sub_global_missing_or_invalid_registry_returns_error(api, registry_text):
    paths, registry, previous = await _sub_setting_fixture(api)
    if registry_text is None:
        registry.unlink()
    else:
        registry.write_text(registry_text)

    response = await _put(api, {"global": 400000})
    assert response.status == 503
    state = await response.json()
    assert state["error"] == "generic"
    assert state["global_value"] == previous
    assert not paths.config_path.exists()
    assert (registry.read_text() if registry.exists() else None) == registry_text


@pytest.mark.parametrize("rollback_fails", [False, True])
async def test_sub_global_rollback_after_topic_failure_uses_registry(
    api, monkeypatch, caplog, rollback_fails
):
    from ductor_bot.infra import json_store

    paths, registry, previous = await _sub_setting_fixture(api)
    original = json_store.atomic_text_save
    writes = []

    def write(path, content):
        assert path == registry
        value = json.loads(content)[0]["autocompact"]
        writes.append(value)
        if rollback_fails and value == previous:
            raise OSError("synthetic-private-path")
        original(path, content)

    monkeypatch.setattr(json_store, "atomic_text_save", write)
    monkeypatch.setattr(
        api.bot._orch._sessions, "set_autocompact", AsyncMock(side_effect=OSError("synthetic"))
    )
    response = await _put(api, {"global": 400000, "topic": 300000})
    assert response.status == 503
    state = await response.json()
    expected = 400000 if rollback_fails else previous
    assert state["error"] == "generic"
    assert state["topic_value"] is None
    assert state["global_value"] == expected
    assert writes == [400000, previous]
    assert not paths.config_path.exists()
    assert json.loads(registry.read_text())[0]["autocompact"] == expected
    assert (
        api.bot._orch._config.autocompact
        == api.bot._orch._cli_service._config.autocompact
        == expected
    )
    assert ("Autocompact global rollback failed" in caplog.text) is rollback_fails
    assert "synthetic-private-path" not in caplog.text


async def test_sub_global_save_ignores_invalid_legacy_config(api):
    paths, registry, _ = await _sub_setting_fixture(api)
    paths.config_path.write_text("{broken")
    response = await _put(api, {"global": 400000})
    assert response.status == 200
    assert (await response.json())["global_value"] == 400000
    assert paths.config_path.read_text() == "{broken"
    assert json.loads(registry.read_text())[0]["autocompact"] == 400000


@pytest.mark.parametrize(("text", "expected"), TOKEN_INPUT_CASES)
async def test_human_token_input_api_normalizes_before_saving(api, text, expected):
    if text in {"default", "off"}:
        return  # Existing mode tests cover these API values.
    response = await _put(api, {"topic": text, "global": text})
    assert response.status == (200 if expected is not None else 400)
    if expected is not None:
        state = await response.json()
        assert state["topic_value"] == state["global_value"] == expected
        assert json.loads(api.paths.config_path.read_text())["autocompact"] == expected
        assert (await api.bot._orch._sessions.get_active(api.key)).autocompact == expected
    else:
        assert json.loads(api.paths.config_path.read_text()) == {}
        assert (await api.bot._orch._sessions.get_active(api.key)).autocompact is None


@pytest.mark.parametrize(("text", "expected"), TOKEN_INPUT_CASES)
async def test_automation_human_token_input_api(automation_api, text, expected):
    if text in {"default", "off"}:
        return
    h = automation_api
    before = (h.paths.cron_jobs_path.read_text(), h.paths.webhooks_path.read_text())
    for kind, item_id, path, key in [
        ("cron", "job-0", h.paths.cron_jobs_path, "jobs"),
        ("webhook", "hook", h.paths.webhooks_path, "hooks"),
    ]:
        response = await h.client.put(
            AUTO_PATH + f"/{kind}/{item_id}", json={"value": text}, headers=headers()
        )
        assert response.status == (200 if expected is not None else 400)
        if expected is not None:
            record = next(x for x in json.loads(path.read_text())[key] if x["id"] == item_id)
            assert record["autocompact"] == expected
            assert type(record["autocompact"]) is int
    if expected is None:
        assert before == (h.paths.cron_jobs_path.read_text(), h.paths.webhooks_path.read_text())
