"""Topic/global autocompact controls, actor isolation and private input expiry."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from ductor_bot.i18n.loader import TranslationStore
from ductor_bot.orchestrator.selectors import autocompact_selector as ac
from ductor_bot.session import SessionData, SessionKey
from tests.cli.test_autocompact import TOKEN_INPUT_CASES


async def test_command_and_initial_buttons(orch):
    key = SessionKey(chat_id=1, topic_id=42)
    result = await orch.handle_message(key, "/autocompact")
    assert "Automatic compaction" in result.text
    assert "memory flush" in result.text
    rows = [[b.callback_data for b in row] for row in result.buttons.rows]
    assert rows == [
        ["ac:topic:inherit", "ac:topic:default"],
        ["ac:topic:off"],
        ["ac:topic:input"],
        ["ac:cancel"],
        ["ac:scope:global"],
        ["ac:auto:list:0"],
    ]
    assert "automation" not in result.text.lower()
    orch._cli_service.execute.assert_not_called()


@pytest.mark.parametrize("mode", ["inherit", "default", "off"])
async def test_topic_value_persists_without_counters_or_target_changes(orch, mode):
    key = SessionKey(chat_id=1, topic_id=42)
    old = SessionData(chat_id=1, topic_id=42, model="sonnet", provider="claude", session_id="keep")
    await orch._sessions.update_session(old)
    before = await orch._sessions.get_active(key)
    response = await ac.handle_autocompact_callback(orch, key, f"ac:topic:{mode}", user_id=100)
    after = await orch._sessions.get_active(key)
    assert after.autocompact == (None if mode == "inherit" else mode)
    assert (after.model, after.session_id, after.message_count, after.activity_turns) == (
        before.model,
        before.session_id,
        before.message_count,
        before.activity_turns,
    )
    assert response.buttons


@pytest.mark.parametrize("text", ["99999", "1000001", "100000.5", "hello", "-1", ""])
async def test_invalid_input_retains_owner_and_never_calls_cli(orch, text):
    key = SessionKey(chat_id=1, topic_id=42)
    await ac.handle_autocompact_callback(orch, key, "ac:topic:input", user_id=100)
    response = await ac.consume_autocompact_input(orch, key, text, user_id=100)
    assert "100,000" in response.text
    assert (key, 100) in orch._autocompact_inputs
    assert await orch._sessions.get_active(key) is None
    orch._cli_service.execute.assert_not_called()


@pytest.mark.parametrize("value", [100000, 300000, 1000000])
async def test_numeric_completion_and_cancellation(orch, value):
    key = SessionKey(chat_id=1, topic_id=42)
    await ac.handle_autocompact_callback(orch, key, "ac:topic:input", user_id=100)
    response = await ac.consume_autocompact_input(orch, key, str(value), user_id=100)
    assert f"{value:,}" in response.text
    assert (await orch._sessions.get_active(key)).autocompact == value
    assert (key, 100) not in orch._autocompact_inputs
    await ac.handle_autocompact_callback(orch, key, "ac:global:input", user_id=100)
    response = await ac.consume_autocompact_input(orch, key, "/cancel", user_id=100)
    assert "cancelled" in response.text
    assert (key, 100) not in orch._autocompact_inputs
    assert orch._config.autocompact == "default"


async def test_different_users_topics_and_chats_do_not_consume_pending_input(orch):
    key = SessionKey(chat_id=1, topic_id=42)
    await ac.handle_autocompact_callback(orch, key, "ac:topic:input", user_id=100)
    for other, actor in [
        (key, 101),
        (SessionKey(chat_id=1, topic_id=43), 100),
        (SessionKey(chat_id=2, topic_id=42), 100),
    ]:
        assert await ac.consume_autocompact_input(orch, other, "300000", user_id=actor) is None
    assert (key, 100) in orch._autocompact_inputs
    await ac.handle_autocompact_callback(orch, key, "ac:cancel", user_id=101)
    assert (key, 100) in orch._autocompact_inputs
    await ac.handle_autocompact_callback(orch, key, "ac:cancel", user_id=100)
    assert not orch._autocompact_inputs


async def test_expiry_removes_pending_state(orch, monkeypatch):
    monkeypatch.setattr(ac, "INPUT_SECONDS", 0.01)
    key = SessionKey(chat_id=1, topic_id=42)
    await ac.handle_autocompact_callback(orch, key, "ac:topic:input", user_id=100)
    await asyncio.sleep(0.025)
    assert not orch._autocompact_inputs


async def test_global_value_saved_to_main_config_and_next_request(orch):
    key = SessionKey(chat_id=1)
    await ac.handle_autocompact_callback(orch, key, "ac:global:input", user_id=100)
    response = await ac.consume_autocompact_input(orch, key, "400000", user_id=100)
    assert response.buttons
    assert orch._config.autocompact == 400000
    assert json.loads(orch.paths.config_path.read_text())["autocompact"] == 400000
    orch._cli_service.update_config.assert_called_once()
    assert orch._cli_service.update_config.call_args.args[0].autocompact == 400000


async def test_sub_global_saved_to_registry_and_independent_after_restart(workspace):
    from ductor_bot.config import AgentConfig
    from ductor_bot.multiagent.models import merge_sub_agent_config
    from ductor_bot.multiagent.registry import AgentRegistry
    from ductor_bot.orchestrator.core import Orchestrator
    from ductor_bot.workspace.paths import DuctorPaths

    paths, cfg = workspace
    root = paths.ductor_home
    home = root / "agents" / "worker"
    target = root / "physical-home"
    target.mkdir()
    home.parent.mkdir()
    home.symlink_to(target, target_is_directory=True)
    subpaths = DuctorPaths(ductor_home=home.resolve())
    subpaths.config_path.parent.mkdir(parents=True)
    registry = root / "agents.json"
    registry.write_text(json.dumps([{"name": "worker", "transport": "headless"}]))
    cfg.ductor_home = str(home)
    cfg._main_home = root
    sub = Orchestrator(cfg, subpaths, agent_name="worker")
    await ac.handle_autocompact_callback(sub, SessionKey(chat_id=1), "ac:global:off", user_id=100)
    assert not subpaths.config_path.exists()
    assert json.loads(registry.read_text())[0]["autocompact"] == "off"
    restarted = merge_sub_agent_config(
        AgentConfig(autocompact=1000000), AgentRegistry(registry).load()[0], home
    )
    assert restarted.autocompact == "off"


@pytest.mark.parametrize(
    ("provider", "mode", "key_name"),
    [
        ("grok", "default", "unsupported"),
        ("gemini", "default", "unsupported"),
        ("codex", "off", "off_unsupported"),
    ],
)
async def test_unsupported_provider_notice(orch, provider, mode, key_name):
    key = SessionKey(chat_id=1)
    await orch._sessions.update_session(SessionData(chat_id=1, provider=provider, autocompact=mode))
    response = await ac.autocompact_selector_start(orch, key)
    assert (
        TranslationStore("en").chat(f"autocompact.{key_name}", provider=provider) in response.text
    )


async def test_web_shared_callback_never_opens_telegram_input(orch):
    key = SessionKey(chat_id=1)
    response = await ac.handle_autocompact_callback(orch, key, "ac:topic:input")
    assert "/autocompact modal" in response.text
    assert not orch._autocompact_inputs
    await ac.handle_autocompact_callback(orch, key, "ac:topic:off")
    assert (await orch._sessions.get_active(key)).autocompact == "off"
    await ac.handle_autocompact_callback(orch, key, "ac:cancel")
    assert not orch._autocompact_inputs


@pytest.mark.parametrize(
    "data", ["ac:", "ac:bad:input", "ac:global:inherit", "ac:topic:99999", "ac:scope:bad"]
)
async def test_forged_actions_do_not_mutate_settings(orch, data):
    result = await ac.handle_autocompact_callback(orch, SessionKey(chat_id=1), data, user_id=100)
    assert "Invalid selection" in result.text
    assert orch._config.autocompact == "default"
    assert not orch._autocompact_inputs


async def test_storage_failure_retains_numeric_input(orch, monkeypatch):
    key = SessionKey(chat_id=1)
    await ac.handle_autocompact_callback(orch, key, "ac:topic:input", user_id=100)
    monkeypatch.setattr(
        orch._sessions, "set_autocompact", AsyncMock(side_effect=OSError("synthetic"))
    )
    response = await ac.consume_autocompact_input(orch, key, "300000", user_id=100)
    assert "could not be saved" in response.text
    assert (key, 100) in orch._autocompact_inputs


@pytest.mark.parametrize("lang", ["en", "ko", "de", "nl", "es", "fr", "id", "pt", "ru"])
def test_translated_labels_and_command_description(lang):
    store = TranslationStore(lang)
    text = store.chat(
        "autocompact.status",
        agent="agent",
        provider="Claude",
        topic="topic",
        global_value="global",
        effective="effective",
    )
    assert "{" not in text
    assert "autocompact.status" not in text
    assert len(store.cmd("bot.autocompact")) <= 22


@pytest.mark.parametrize(
    ("text", "expected"),
    TOKEN_INPUT_CASES,
)
async def test_human_token_input_roundtrip(orch, text, expected):
    key = SessionKey(chat_id=1, topic_id=42)
    await ac.handle_autocompact_callback(orch, key, "ac:topic:input", user_id=100)
    await ac.consume_autocompact_input(orch, key, text, user_id=100)
    session = await orch._sessions.get_active(key)
    if expected is None:
        assert session is None
        assert (key, 100) in orch._autocompact_inputs
    else:
        assert session.autocompact == expected
        assert type(session.autocompact) is int
        assert (key, 100) not in orch._autocompact_inputs
    orch._cli_service.execute.assert_not_called()
