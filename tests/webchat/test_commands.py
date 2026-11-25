"""Web command dispatch, Telegram parity and authenticated API contracts."""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import re
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from ductor_bot.chat_log.events import EventHub
from ductor_bot.chat_log.history import HistoryStore
from ductor_bot.chat_log.outbox import Outbox
from ductor_bot.cli.auth import AuthResult, AuthStatus
from ductor_bot.cli.codex_usage import CodexUsage
from ductor_bot.i18n import LANGUAGES, t
from ductor_bot.i18n.loader import TranslationStore
from ductor_bot.messenger.telegram import abort, middleware
from ductor_bot.messenger.telegram.secret_input import SecretInput
from ductor_bot.orchestrator.core import Orchestrator
from ductor_bot.orchestrator.registry import CommandRegistry
from ductor_bot.orchestrator.selectors.models import Button, ButtonGrid
from ductor_bot.session.key import SessionKey
from ductor_bot.webchat.commands import (
    WEB_COMMANDS,
    WebCommandBusyError,
    WebCommandDispatcher,
    command_list,
    parse_command,
    serialize_buttons,
)
from ductor_bot.webchat.turns import WebTurns
from tests.messenger.telegram.test_app import _make_tg_bot
from tests.orchestrator.conftest import orch as orch  # noqa: PLC0414 -- pytest fixtures
from tests.orchestrator.conftest import workspace as workspace  # noqa: PLC0414 -- pytest fixture
from tests.webchat.test_bridge_api import (
    bridge as bridge,  # noqa: PLC0414 -- pytest fixture
)
from tests.webchat.test_bridge_api import (
    conversation as conversation,  # noqa: PLC0414 -- pytest fixture
)
from tests.webchat.test_bridge_api import (
    headers,
    post_message,
    receive_until,
)
from tests.webchat.test_web_app import CLIENT, login
from tests.webchat.test_web_app import site as site  # noqa: PLC0414 -- pytest fixture


@pytest.fixture
async def commands(orch, monkeypatch):
    orch.config.ductor_home = str(orch.paths.ductor_home)
    orch.config.allowed_user_ids = [1]
    orch.register_multiagent_commands()
    tg, network = _make_tg_bot(orch.config)
    tg._orchestrator = orch
    orch._lock_pool = tg.lock_pool
    tg._chat_tracker = None
    network.leave_chat = AsyncMock(return_value=True)
    orch.interrupt = MagicMock(return_value=2)
    orch.abort_all = AsyncMock(return_value=3)
    tg._sequential.drain_pending = AsyncMock(return_value=0)
    tg._abort_all_callback = AsyncMock(return_value=4)
    monkeypatch.setattr("ductor_bot.orchestrator.commands.check_all_auth", dict)
    monkeypatch.setattr("ductor_bot.messenger.telegram.app.get_current_version", lambda: "1.2.3")
    monkeypatch.setattr(
        "ductor_bot.orchestrator.selectors.model_selector.check_all_auth",
        lambda: {
            "claude": AuthResult("claude", AuthStatus.AUTHENTICATED),
        },
    )
    monkeypatch.setattr(
        "ductor_bot.cli.codex_usage.query_codex_usage", AsyncMock(return_value=CodexUsage())
    )
    monkeypatch.setattr("ductor_bot.infra.restart.write_restart_sentinel", MagicMock())
    monkeypatch.setattr("ductor_bot.infra.restart.write_restart_marker", MagicMock())
    hub = EventHub()
    runtime = SimpleNamespace(
        events=hub,
        history=HistoryStore(orch.paths.chat_log_dir / "history", hub),
        outbox=Outbox(orch.paths.chat_log_dir / "outbox", hub),
        drainer=SimpleNamespace(wake=MagicMock()),
    )
    turns = WebTurns(tg, runtime)
    dispatcher = WebCommandDispatcher(tg, turns)
    try:
        yield SimpleNamespace(
            dispatcher=dispatcher, bot=tg, network=network, orch=orch, turns=turns, runtime=runtime
        )
    finally:
        await turns.stop()


def test_exact_commands_match_actual_main_ingress():
    tg, _ = _make_tg_bot()
    ingress = set()
    for handler in tg._router.message.handlers:
        for filter_object in handler.filters:
            if isinstance(filter_object.callback, Command):
                ingress.update(filter_object.callback.commands)
    holder = SimpleNamespace(_command_registry=CommandRegistry())
    Orchestrator._register_commands(holder)
    Orchestrator.register_multiagent_commands(holder)
    ingress.update(
        entry.name.strip().removeprefix("/") for entry in holder._command_registry._commands
    )
    ingress.update(command.removeprefix("/") for command in middleware.QUICK_COMMANDS)
    for detector in (
        abort.is_interrupt_message,
        abort.is_abort_all_message,
        abort.is_abort_message,
    ):
        tree = ast.parse(inspect.getsource(detector))
        candidates = {
            node.value.removesuffix("@")
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and re.fullmatch(r"/[a-z_]+@?", node.value)
        }
        ingress.update(value.removeprefix("/") for value in candidates if detector(value))
    secret_parts = SecretInput._command_parts(" /SeCrEt@TestBot set KEY")
    ingress.add(secret_parts[0].removeprefix("/"))
    assert len(ingress) == 31
    assert set(WEB_COMMANDS) == ingress
    assert len(WEB_COMMANDS) == 31


@pytest.mark.parametrize("language", LANGUAGES)
def test_command_descriptions_are_localized_without_global_language_changes(language):
    store = TranslationStore(language)
    rows = command_list(language)
    assert [row["name"] for row in rows] == list(WEB_COMMANDS)
    assert rows == [
        {"name": name, "description": store.cmd(key)} for name, key in WEB_COMMANDS.items()
    ]
    assert all(row["description"] and "MISSING" not in row["description"] for row in rows)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("  /MoDeL@Any_Bot Sonnet ", ("model", "/model Sonnet")),
        ("/NEW @Work Topic", ("new", "/new @Work Topic")),
        ("/SECRET@bot set KEY value", ("secret", "/secret set KEY value")),
        ("/x hello", None),
        ("hello /help", None),
        ("", None),
        ("/", None),
    ],
)
def test_parse_command_preserves_arguments(text, expected):
    assert parse_command(text) == expected


def test_keyboard_conversion_preserves_rows_callbacks_and_urls():
    assert serialize_buttons(None) is None
    assert serialize_buttons(ButtonGrid(rows=[[Button("Model", "ms:claude")]])) == [
        [{"text": "Model", "callback_data": "ms:claude"}]
    ]
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Browse", callback_data="sf:")],
            [InlineKeyboardButton(text="GitHub", url="https://github.com/ryuhaneul/dt-agent")],
        ]
    )
    assert serialize_buttons(markup) == [
        [{"text": "Browse", "callback_data": "sf:"}],
        [{"text": "GitHub", "url": "https://github.com/ryuhaneul/dt-agent"}],
    ]


@pytest.mark.parametrize(
    "name",
    [
        "new",
        "reset",
        "stop",
        "interrupt",
        "model",
        "effort",
        "status",
        "usage",
        "memory",
        "session",
        "tasks",
        "cron",
        "agent_commands",
        "showfiles",
        "info",
        "help",
        "diagnose",
        "restart",
        "secret",
        "accounts",
        "sessions",
        "agents",
        "agent_start",
        "agent_stop",
        "agent_restart",
        "stop_all",
        "start",
        "where",
        "leave",
    ],
)
async def test_every_command_returns_text_and_buttons_without_cli_or_recording(commands, name):
    h = commands
    result = await h.dispatcher.dispatch("1.7", f"/{name}")
    assert set(result) == (
        {"text", "buttons", "secret"} if name == "secret" else {"text", "buttons"}
    )
    assert isinstance(result["text"], str)
    assert result["text"]
    assert result["buttons"] is None or isinstance(result["buttons"], list)
    h.orch._cli_service.execute.assert_not_awaited()
    h.orch._cli_service.execute_streaming.assert_not_awaited()
    assert await h.runtime.history.read("1.7") == []
    assert await h.runtime.outbox.pending() == []


@pytest.mark.parametrize("command", ["/status", "/effort", "/tasks"])
async def test_registry_results_match_telegram_orchestrator_path(commands, command):
    h = commands
    key = SessionKey.telegram(1, 7)
    telegram = await h.orch.handle_message(key, command)
    web = await h.dispatcher.dispatch("1.7", command)
    assert web == {"text": telegram.text, "buttons": serialize_buttons(telegram.buttons)}


@pytest.mark.parametrize("name", WEB_COMMANDS)
@pytest.mark.parametrize("argument", ["", " x"])
async def test_command_argument_classification_matches_telegram(
    commands, name, argument, monkeypatch
):
    h = commands
    text = f"/{name}{argument}"
    # These Telegram routes forward the complete input to the orchestrator.
    registry_routes = {
        command
        for handler in h.bot._router.message.handlers
        if handler.callback.__name__ in {"_on_command", "_on_sessions", "_on_tasks"}
        for filter_object in handler.filters
        if isinstance(filter_object.callback, Command)
        for command in filter_object.callback.commands
    }
    expected = name not in registry_routes or h.orch.has_command(text)
    execute = AsyncMock(return_value={"text": "command result", "buttons": None})
    monkeypatch.setattr(h.dispatcher, "_execute", execute)
    started, finished = AsyncMock(), AsyncMock()
    result = await h.dispatcher.dispatch("1.7", text, on_start=started, on_finish=finished)
    assert (result is not None) == expected
    if name != "secret":
        assert execute.await_count == int(expected)
        assert started.await_count == int(expected)
        assert finished.await_count == int(expected)


async def test_direct_command_catalog_matches_execution_branches(commands, monkeypatch):
    from ductor_bot.webchat.commands import _DIRECT_COMMANDS

    fallback = AsyncMock(return_value={"text": "registry result", "buttons": None})
    monkeypatch.setattr(commands.dispatcher, "_registry_response", fallback)
    key = SessionKey.telegram(1, 7)
    for name in WEB_COMMANDS:
        fallback.reset_mock()
        await commands.dispatcher.dispatch("1.7", f"/{name}")
        assert (fallback.await_count == 0) == (name in _DIRECT_COMMANDS)
        if name not in _DIRECT_COMMANDS:
            fallback.assert_awaited_once_with(name, key, f"/{name}")


@pytest.mark.parametrize("error", [OSError, AttributeError])
async def test_failed_direct_command_records_and_mirrors_error(
    conversation, monkeypatch, caplog, error
):
    h = conversation
    monkeypatch.setattr(
        "ductor_bot.webchat.commands.file_browser_start",
        AsyncMock(side_effect=error("private filesystem detail")),
    )
    response = await post_message(h, text="/showfiles")
    assert response.status == 200
    assert await response.json() == {"text": t("error.internal"), "buttons": None}
    entries = await h.runtime.history.read("-123.7")
    assert [(entry.role, entry.kind, entry.text) for entry in entries] == [
        ("user", "command", "/showfiles"),
        ("bot", "message", t("error.internal")),
    ]
    pending = await h.runtime.outbox.pending()
    assert [item.history_id for item in pending] == [entry.id for entry in entries]
    assert [item.text for item in pending] == ["Command executed: /showfiles", t("error.internal")]
    assert "private filesystem detail" not in caplog.text
    assert not h.calls
    assert not h.pool.get(h.key.lock_key).locked()


async def test_cancelled_command_propagates_without_error_result(commands, monkeypatch):
    monkeypatch.setattr(
        commands.dispatcher, "_execute", AsyncMock(side_effect=asyncio.CancelledError)
    )
    started, finished = AsyncMock(), AsyncMock()
    with pytest.raises(asyncio.CancelledError):
        await commands.dispatcher.dispatch(
            "1.7", "/showfiles", on_start=started, on_finish=finished
        )
    started.assert_awaited_once_with()
    finished.assert_not_awaited()
    assert not commands.orch._lock_pool.get((1, 7)).locked()


async def test_execution_busy_error_remains_conflict(conversation, monkeypatch):
    monkeypatch.setattr(
        "ductor_bot.webchat.commands.file_browser_start",
        AsyncMock(side_effect=WebCommandBusyError),
    )
    response = await post_message(conversation, text="/showfiles")
    assert response.status == 409
    assert await response.json() == {"error": "web_turn_running"}


async def test_new_named_topic_and_idle_lock_contract(commands):
    h = commands
    h.bot._topic_names.set(1, 9, "Work")
    locks = []

    async def reset(key):
        locks.append(
            (
                key,
                h.orch._lock_pool.get((1, 7)).locked(),
                h.orch._lock_pool.get(key.lock_key).locked(),
            )
        )
        return "claude"

    h.orch.reset_active_provider_session = AsyncMock(side_effect=reset)
    result = await h.dispatcher.dispatch("1.7", " /NeW@Test @Work")
    assert result["text"] == t("new.topic_reset", name="Work", provider="claude")
    assert locks == [(SessionKey.telegram(1, 9), True, True)]
    result = await h.dispatcher.dispatch("1.7", "/new")
    assert locks[-1] == (SessionKey.telegram(1, 7), True, True)
    assert "session reset" in result["text"].lower()


async def test_busy_new_current_or_named_target_is_rejected_and_interrupt_is_immediate(commands):
    h = commands
    h.bot._topic_names.set(1, 9, "Work")
    for target, command in [(7, "/new"), (9, "/new @Work")]:
        async with h.orch._lock_pool.get((1, target)):
            with pytest.raises(WebCommandBusyError):
                await h.dispatcher.dispatch("1.7", command)
            result = await h.dispatcher.dispatch("1.7", "/interrupt")
            assert result["text"] == t("interrupt.done", count=2)


async def test_version_is_listed_and_replies_during_active_web_turn(commands, monkeypatch):
    monkeypatch.setattr("ductor_bot.orchestrator.commands.get_current_version", lambda: "9.8.7")
    assert {"name": "version", "description": "Release version"} in command_list("en")

    async with commands.orch._lock_pool.get((1, 7)):
        result = await asyncio.wait_for(commands.dispatcher.dispatch("1.7", "/version"), 1)
    assert result == {"text": "Version: `9.8.7`", "buttons": None}


async def test_usage_contains_queried_result_and_refresh_button(commands):
    result = await commands.dispatcher.dispatch("1.7", "/usage")
    assert "loading" not in result["text"].lower()
    assert any(
        button["callback_data"] == "usg:refresh" for row in result["buttons"] for button in row
    )


async def test_info_returns_url_buttons_and_leave_uses_explicit_group(commands):
    info = await commands.dispatcher.dispatch("1.7", "/info")
    assert info["buttons"] == [
        [
            {"text": "GitHub", "url": "https://github.com/ryuhaneul/dt-agent"},
            {"text": "Changelog", "url": "https://github.com/ryuhaneul/dt-agent/releases"},
        ]
    ]
    result = await commands.dispatcher.dispatch("1.7", "/leave -100123")
    commands.network.leave_chat.assert_awaited_once_with(-100123)
    assert result["text"] == t("telegram.left_group", group_id=-100123)


async def test_file_browser_followup_uses_same_topic_turn(commands):
    h = commands
    h.turns.submit = AsyncMock(return_value="turn1")
    initial = await h.dispatcher.dispatch("1.7", "/showfiles")
    assert initial["buttons"]
    result = await h.dispatcher.file_browser_response("1.7", "sf!")
    h.turns.submit.assert_awaited_once_with(
        "1.7", t("file_browser.file_request_prompt", dir=h.orch.paths.ductor_home.resolve())
    )
    assert set(result) == {"text", "buttons"}


async def test_named_session_followup_preserves_topic_and_has_no_fake_reply(commands):
    h = commands
    h.orch.get_named_session = MagicMock(
        return_value=SimpleNamespace(provider="claude", model="opus")
    )
    h.orch.submit_named_followup_bg = MagicMock(return_value="task1")
    h.orch.resolve_session_directive = MagicMock(return_value=None)
    result = await h.dispatcher.dispatch("1.7", "/session @existing hello")
    h.orch.submit_named_followup_bg.assert_called_once_with(1, "existing", "hello", 0, 7)
    assert "existing" in result["text"]
    assert not h.turns.busy("1.7")


async def test_restart_prepares_sentinel_and_requests_existing_marker(commands, monkeypatch):
    sentinel = MagicMock()
    marker = MagicMock()
    monkeypatch.setattr("ductor_bot.infra.restart.write_restart_sentinel", sentinel)
    monkeypatch.setattr("ductor_bot.infra.restart.write_restart_marker", marker)
    result = await commands.dispatcher.dispatch("1.7", "/restart")
    sentinel.assert_called_once_with(
        1,
        t("startup.restart_default"),
        sentinel_path=commands.orch.paths.ductor_home / "restart-sentinel.json",
    )
    marker.assert_called_once_with(
        marker_path=commands.orch.paths.ductor_home / "restart-requested"
    )
    assert "Restarting" in result["text"]


@pytest.mark.parametrize(
    "text",
    [
        "/secret",
        " /SeCrEt@BOT set KEY hidden-value",
        "/secret list",
        "/secret set KEY\nhidden-value",
        "/secret delete KEY",
        "/secret cancel",
    ],
)
async def test_secret_is_intercepted_before_turn_history_outbox_and_cli(conversation, text, caplog):
    h = conversation
    response = await post_message(h, text=text)
    assert response.status == 200
    result = await response.json()
    assert "secret" in result
    assert result["buttons"] is None
    assert not h.calls
    entries = await h.runtime.history.read("-123.7")
    pending = await h.runtime.outbox.pending()
    assert entries[0].kind == "command"
    from ductor_bot.webchat.commands import parse_command
    from ductor_bot.webchat.secret import WebSecrets

    assert entries[0].text == WebSecrets.safe_command(parse_command(text)[1])
    assert pending[0].text == f"Command executed: {entries[0].text}"
    assert "hidden-value" not in repr(entries) + repr(pending) + repr(result) + caplog.text


@pytest.mark.parametrize("text", ["/unknown some text", "@existing followup"])
async def test_unknown_command_and_named_followup_remain_normal_turns(conversation, text):
    h = conversation
    ws = await h.client.ws_connect("/webchat/v1/events", headers=headers())
    try:
        response = await post_message(h, text=text)
        assert response.status == 202
        await receive_until(
            ws, lambda event: event.get("type") == "turn" and event.get("phase") == "done"
        )
        assert h.calls[0][0] == h.key
        assert h.calls[0][1] == text
    finally:
        await ws.close()


async def test_commands_api_auth_and_language_validation(bridge, monkeypatch):
    client, bot, _, _ = bridge
    described_languages = []

    def describe(language):
        described_languages.append(language)
        return command_list(language)

    monkeypatch.setattr("ductor_bot.webchat.bridge.command_list", describe)
    denied = await client.get("/webchat/v1/commands")
    assert denied.status == 403
    assert "WWW-Authenticate" not in denied.headers
    for language in [None, *LANGUAGES]:
        suffix = f"?lang={language}" if language else ""
        response = await client.get("/webchat/v1/commands" + suffix, headers=headers())
        assert response.status == 200
        assert await response.json() == {"commands": command_list(language or bot.config.language)}
        canonical = next(key for key in LANGUAGES if key == (language or bot.config.language))
        assert described_languages[-1] is canonical
    count = len(described_languages)
    for language in ["../../secret", "/tmp", "en/../ko", "EN", ""]:
        response = await client.get(
            "/webchat/v1/commands", params={"lang": language}, headers=headers()
        )
        assert response.status == 400
    assert len(described_languages) == count


async def test_commands_browser_route_uses_existing_authenticated_relay(site):
    client, fake = site
    response = await client.get("/api/commands?lang=ko", headers=CLIENT)
    assert response.status == 403
    session_headers, _ = await login(client)
    response = await client.get("/api/commands?lang=ko", headers=session_headers)
    assert response.status == 200
    assert fake.calls[-1][:3] == ("GET", "/commands", {"lang": "ko"})


async def test_stop_all_cancels_waiting_web_turn_before_killing(commands):
    h = commands
    lock = h.orch._lock_pool.get((1, 7))
    await lock.acquire()
    queue = h.runtime.events.subscribe()
    try:
        await h.turns.submit("1.7", "waiting prompt")
        task = h.turns._tasks[h.turns.active_turn_id("1.7")]
        while True:
            event = await asyncio.wait_for(queue.get(), 2)
            if event.get("type") == "turn" and event.get("phase") == "queued":
                break
        order = []

        async def drain(_chat_id):
            assert not task.cancelled()
            order.append("telegram_queue")
            return 0

        async def kill():
            assert task.cancelled()
            order.append("kill")
            return 3

        h.bot._sequential.drain_pending.side_effect = drain
        h.orch.abort_all.side_effect = kill
        result = await h.dispatcher.dispatch("1.7", "/stop_all")
        assert order == ["telegram_queue", "kill"]
        assert result["text"] == t("abort_all.done", count=7)
        assert not h.turns.busy("1.7")
        h.orch._cli_service.execute_streaming.assert_not_awaited()
    finally:
        lock.release()
        h.runtime.events.unsubscribe(queue)


async def test_stop_all_keeps_running_turn_until_existing_registry_kill(commands):
    from ductor_bot.orchestrator.registry import OrchestratorResult

    h = commands
    started, finished = asyncio.Event(), asyncio.Event()

    async def running(*_args, **_kwargs):
        started.set()
        await finished.wait()
        return OrchestratorResult(text="")

    h.orch.handle_message_streaming = AsyncMock(side_effect=running)
    await h.turns.submit("1.7", "running prompt")
    await asyncio.wait_for(started.wait(), 2)
    task = h.turns._tasks[h.turns.active_turn_id("1.7")]

    async def kill_active():
        assert not task.done()
        assert h.turns.active_turn_id("1.7") in h.turns._started
        finished.set()
        return 3

    h.orch._process_registry.kill_all_active = AsyncMock(side_effect=kill_active)
    h.orch.abort_all = MethodType(Orchestrator.abort_all, h.orch)
    result = await h.dispatcher.dispatch("1.7", "/stop_all")
    h.orch._process_registry.kill_all_active.assert_awaited_once_with()
    await asyncio.wait_for(task, 2)
    assert not task.cancelled()
    assert result["text"] == t("abort_all.done", count=7)


async def test_running_web_turn_new_conflict_and_stop_are_immediate(conversation):
    h = conversation
    started, finished = asyncio.Event(), asyncio.Event()
    original = h.bot._orch.handle_message_streaming.side_effect

    async def running(*args, **kwargs):
        started.set()
        await finished.wait()
        return await original(*args, **kwargs)

    async def kill(*_args):
        finished.set()
        return 1

    h.bot._orch.handle_message_streaming.side_effect = running
    h.bot._orch.active_provider_name = "Claude Code"
    h.bot._orch._process_registry.kill_by_chat_topic.side_effect = kill
    response = await post_message(h, text="running prompt")
    assert response.status == 202
    await asyncio.wait_for(started.wait(), 2)
    response = await post_message(h, text="/new")
    assert response.status == 409
    assert await response.json() == {"error": "web_turn_running"}
    response = await asyncio.wait_for(post_message(h, text="/stop"), 1)
    assert response.status == 202
    assert await response.json() == {"killed": 1}
    h.bot._orch._process_registry.kill_by_chat_topic.assert_awaited_once_with(-123, 7)


async def test_web_quick_command_preserves_aborted_event(conversation, monkeypatch):
    from ductor_bot.cli.process_registry import ProcessRegistry
    from ductor_bot.orchestrator.commands import cmd_version
    from tests.cli.test_process_registry import _mock_process

    h = conversation
    orch = h.bot._orch
    registry = ProcessRegistry()
    orch._process_registry = registry
    orch._inflight_tracker = SimpleNamespace(shutting_down=False)
    orch._observers = SimpleNamespace(heartbeat=SimpleNamespace(notify_activity=MagicMock()))
    orch._command_registry = CommandRegistry()
    orch._command_registry.register_async("/version", cmd_version)
    orch.active_provider_name = "Claude Code"
    monkeypatch.setattr(
        "ductor_bot.cli.process_registry._kill_processes", AsyncMock(return_value=1)
    )
    started, release = asyncio.Event(), asyncio.Event()
    original = orch.handle_message_streaming.side_effect

    async def running(key, text, **callbacks):
        registry.register(key.chat_id, _mock_process(), "main", topic_id=key.topic_id)
        started.set()
        await release.wait()
        return await original(key, text, **callbacks)

    orch.handle_message_streaming.side_effect = running
    async with h.client.ws_connect("/webchat/v1/events", headers=headers()) as ws:
        response = await post_message(h)
        assert response.status == 202
        turn_id = (await response.json())["turn_id"]
        try:
            await asyncio.wait_for(started.wait(), 2)
            stopped = await post_message(h, text="/stop")
            assert stopped.status == 202
            assert await stopped.json() == {"killed": 1}
            version = await asyncio.wait_for(post_message(h, text="/version"), 2)
            assert version.status == 200
            assert (await version.json())["text"]
        finally:
            release.set()

        events = await receive_until(
            ws,
            lambda event: (
                event.get("turn_id") == turn_id
                and event.get("phase") in {"aborted", "done", "error"}
            ),
        )
        assert events[-1]["phase"] == "aborted"


async def test_zero_source_message_id_reaches_sender_without_reply_quote(commands):
    from ductor_bot.background.models import BackgroundResult
    from ductor_bot.bus.adapters import from_background_result
    from ductor_bot.messenger.telegram.transport import TelegramTransport

    h = commands
    result = BackgroundResult(
        task_id="task1",
        chat_id=1,
        message_id=0,
        thread_id=7,
        transport="tg",
        prompt_preview="hello",
        result_text="Result\n[button:Continue]",
        status="ok",
        elapsed_seconds=1,
        provider="claude",
        model="opus",
        session_name="existing",
    )
    env = from_background_result(result)
    assert env.session_name == "existing"
    transport = TelegramTransport(h.bot)
    options = transport._opts(env)
    assert options.thread_id == 7
    h.network.send_message.return_value = SimpleNamespace(message_id=42)
    await transport.deliver(env)
    kwargs = h.network.send_message.call_args.kwargs
    assert kwargs["message_thread_id"] == 7
    assert "reply_parameters" not in kwargs
    keyboard = h.network.edit_message_reply_markup.call_args.kwargs
    assert keyboard["message_id"] == 42
    assert keyboard["reply_markup"].inline_keyboard[0][0].callback_data == "ns:existing:Continue"


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        ("en", "Command executed: /status"),
        ("ko", "명령어 실행: /status"),
        ("de", "Befehl ausgeführt: /status"),
        ("nl", "Opdracht uitgevoerd: /status"),
        ("es", "Comando ejecutado: /status"),
        ("fr", "Commande exécutée : /status"),
        ("id", "Perintah dijalankan: /status"),
        ("pt", "Comando executado: /status"),
        ("ru", "Команда выполнена: /status"),
    ],
)
def test_command_mirror_wording_in_every_locale(language, expected):
    strings = TranslationStore(language)
    assert strings.chat("chat_log.mirror_command", text="/status") == expected
    assert "🌐" in strings.chat("chat_log.mirror_user", text="hello")


async def test_web_command_records_original_input_then_result_and_mirrors(conversation):
    h = conversation
    h.bot._format_where = lambda: "Tracked chats"
    raw = " /WhErE@TestBot "
    response = await post_message(h, text=raw)
    assert response.status == 200
    entries = await h.runtime.history.read("-123.7")
    assert [(entry.role, entry.source, entry.kind, entry.text) for entry in entries] == [
        ("user", "web", "command", raw),
        ("bot", "web", "message", "Tracked chats"),
    ]
    assert entries[0].seq < entries[1].seq
    pending = await h.runtime.outbox.pending()
    assert [item.history_id for item in pending] == [entry.id for entry in entries]
    assert [item.text for item in pending] == ["Command executed: " + raw, "Tracked chats"]
    assert not h.calls


@pytest.mark.parametrize("command", ["/info", "/showfiles"])
async def test_command_keyboards_survive_outage_restart_and_old_outbox_reader(
    conversation, command
):
    from aiogram.exceptions import TelegramNetworkError

    from ductor_bot.chat_log.history import Entry, HistoryStore
    from ductor_bot.chat_log.link import OutboxDrainer, TelegramLinkMonitor
    from ductor_bot.chat_log.outbox import Outbox, OutboxItem
    from ductor_bot.messenger.telegram.webchat_ops import TelegramWebchatOps

    h = conversation
    network = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=42)),
        get_me=AsyncMock(return_value=True),
    )
    h.bot.bot_instance = network
    h.bot.file_roots = MagicMock(return_value=None)
    response = await post_message(h, text=command)
    assert response.status == 200
    result = await response.json()
    assert result["buttons"]
    entries = await h.runtime.history.read("-123.7")
    metadata = await h.runtime.history.metadata("-123.7")
    assert metadata[entries[-1].id] == {"buttons": result["buttons"]}
    path = h.runtime.history._path("-123.7")
    assert [Entry(**json.loads(line)) for line in path.read_text().splitlines()] == entries
    for item_path in (h.runtime.outbox.directory / "pending").glob("*.json"):
        assert OutboxItem(**json.loads(item_path.read_text())).state == "pending"
    view = await (
        await h.client.get("/webchat/v1/topics/-123.7/messages", headers=headers())
    ).json()
    assert view["entries"][-1]["buttons"] == result["buttons"]

    ops = TelegramWebchatOps(h.bot)
    link = TelegramLinkMonitor(ops, h.runtime.events)
    link.record(up=False, probe=True)
    drainer = OutboxDrainer(h.runtime.outbox, ops, link, history=h.runtime.history)
    await drainer.drain()
    network.send_message.assert_not_awaited()
    # Send the input mirror, then lose connectivity before the result is accepted.
    network.send_message.side_effect = [
        SimpleNamespace(message_id=41),
        TelegramNetworkError(method=MagicMock(), message="offline"),
    ]
    network.get_me.side_effect = TelegramNetworkError(method=MagicMock(), message="offline")
    link.record(up=True, probe=True)
    await drainer.drain()
    remaining = await h.runtime.outbox.pending()
    assert len(remaining) == 1
    assert remaining[0].history_id == entries[-1].id
    assert remaining[0].sent_chunks == 0
    # A fresh worker loads the unchanged item and its separate keyboard metadata.
    network.send_message.reset_mock(side_effect=True)
    network.send_message.return_value = SimpleNamespace(message_id=42)
    restarted = Outbox(h.runtime.outbox.directory)
    link = TelegramLinkMonitor(ops, h.runtime.events)
    await OutboxDrainer(
        restarted, ops, link, history=HistoryStore(h.runtime.history.directory)
    ).drain()
    assert not await restarted.pending()
    kwargs = network.send_message.call_args.kwargs
    assert kwargs["message_thread_id"] == 7
    markup = kwargs["reply_markup"]
    assert serialize_buttons(markup) == result["buttons"]
    assert not (restarted.directory / "corrupt").exists()


async def test_restart_records_and_queues_result_before_marker(conversation, monkeypatch):
    h = conversation
    order = []

    def sentinel(*_args, **_kwargs):
        order.append("sentinel")

    def marker(**_kwargs):
        pending = h.runtime.outbox._pending()
        assert [item.kind for item in pending] == ["user_mirror", "reply_mirror"]
        assert h.runtime.history._read(h.runtime.history._path("-123.7"))[-1].role == "bot"
        order.append("marker")

    monkeypatch.setattr("ductor_bot.infra.restart.write_restart_sentinel", sentinel)
    monkeypatch.setattr("ductor_bot.infra.restart.write_restart_marker", marker)
    response = await post_message(h, text="/restart")
    assert response.status == 200
    assert order == ["sentinel", "marker"]
    assert len(await h.runtime.history.read("-123.7")) == 2


async def test_stop_keeps_legacy_http_contract_and_records_command(conversation):
    response = await post_message(conversation, text=" /STOP ")
    assert response.status == 202
    assert await response.json() == {"killed": 0}
    entries = await conversation.runtime.history.read("-123.7")
    assert [entry.role for entry in entries] == ["user", "bot"]
    assert entries[0].kind == "command"
    assert "Stop" in entries[1].text


async def test_stop_runs_while_drainer_waits_for_telegram(conversation):
    from ductor_bot.chat_log.link import OutboxDrainer, TelegramLinkMonitor
    from ductor_bot.messenger.telegram.webchat_ops import TelegramWebchatOps

    h = conversation
    started, finish = asyncio.Event(), asyncio.Event()

    async def send(**_kwargs):
        started.set()
        await finish.wait()
        return SimpleNamespace(message_id=42)

    h.bot.bot_instance = SimpleNamespace(send_message=AsyncMock(side_effect=send))
    h.bot.file_roots = MagicMock(return_value=None)
    h.bot._orch.active_provider_name = "Claude Code"
    h.bot._orch._process_registry.kill_by_chat_topic.return_value = 1
    ops = TelegramWebchatOps(h.bot)
    drainer = OutboxDrainer(
        h.runtime.outbox, ops, TelegramLinkMonitor(ops, h.runtime.events), history=h.runtime.history
    )
    await h.runtime.outbox.enqueue(kind="bus", chat_id=-123, topic_id=7, text="older delivery")
    draining = asyncio.create_task(drainer.drain())
    try:
        await asyncio.wait_for(started.wait(), 2)
        assert h.runtime.outbox.delivery_lock.locked()
        response = await asyncio.wait_for(post_message(h, text="/stop"), 1)
        assert response.status == 202
        assert await response.json() == {"killed": 1}
        h.bot._orch._process_registry.kill_by_chat_topic.assert_awaited_once_with(-123, 7)
        assert not finish.is_set()
        pending = await h.runtime.outbox.pending()
        assert [item.kind for item in pending] == ["bus", "user_mirror", "reply_mirror"]
    finally:
        finish.set()
        await asyncio.wait_for(draining, 3)
    assert not await h.runtime.outbox.pending()
    assert h.bot.bot_instance.send_message.await_count == 3


async def test_command_enqueue_during_direct_send_has_no_loss_or_duplicate(conversation):
    from collections import Counter

    from ductor_bot.bus.envelope import Envelope, Origin
    from ductor_bot.chat_log.link import OutboxDrainer, TelegramLinkMonitor
    from ductor_bot.messenger.telegram.sender import SendRichOpts
    from ductor_bot.messenger.telegram.transport import TelegramTransport
    from ductor_bot.messenger.telegram.webchat_ops import TelegramWebchatOps

    h = conversation
    started, finish = asyncio.Event(), asyncio.Event()

    async def send(**kwargs):
        if kwargs["text"] == "direct delivery":
            started.set()
            await finish.wait()
        return SimpleNamespace(message_id=42)

    network = SimpleNamespace(send_message=AsyncMock(side_effect=send))
    h.bot.bot_instance = network
    h.bot.file_roots = MagicMock(return_value=None)
    h.bot._format_where = lambda: "Tracked chats"
    transport = TelegramTransport(h.bot)
    transport.set_outbox(h.runtime.outbox)
    env = Envelope(origin=Origin.HEARTBEAT, chat_id=-123, topic_id=7)
    direct = asyncio.create_task(
        transport._send_part(env, -123, "direct delivery", SendRichOpts(thread_id=7))
    )
    try:
        await asyncio.wait_for(started.wait(), 2)
        response = await asyncio.wait_for(post_message(h, text="/where"), 1)
        assert response.status == 200
        pending = await h.runtime.outbox.pending()
        assert [item.kind for item in pending] == ["user_mirror", "reply_mirror"]
    finally:
        finish.set()
        await asyncio.wait_for(direct, 3)
    ops = TelegramWebchatOps(h.bot)
    await OutboxDrainer(
        h.runtime.outbox, ops, TelegramLinkMonitor(ops, h.runtime.events), history=h.runtime.history
    ).drain()
    assert not await h.runtime.outbox.pending()
    texts = [call.kwargs["text"] for call in network.send_message.await_args_list]
    assert Counter(texts) == {
        "direct delivery": 1,
        "Command executed: /where": 1,
        "Tracked chats": 1,
    }


@pytest.mark.parametrize("action", ["/stop", "/interrupt", "/stop_all", "abort"])
async def test_long_usage_does_not_delay_control_requests(conversation, monkeypatch, action):
    h = conversation
    started, release = asyncio.Event(), asyncio.Event()
    entered = asyncio.Event()
    turn_started, turn_release = asyncio.Event(), asyncio.Event()
    original = h.bot._orch.handle_message_streaming.side_effect

    async def foreground(*args, **kwargs):
        turn_started.set()
        await turn_release.wait()
        return await original(*args, **kwargs)

    async def usage(_self):
        started.set()
        await release.wait()
        return {"text": "usage", "buttons": None}

    async def kill(*_args):
        entered.set()
        turn_release.set()
        return 1

    def interrupt(_chat_id):
        entered.set()
        return 1

    h.bot._orch.handle_message_streaming.side_effect = foreground
    h.bot._orch.active_provider_name = "Claude Code"
    h.bot._orch._process_registry.kill_by_chat_topic.side_effect = kill
    h.bot._orch.interrupt = interrupt
    h.bot._orch.abort_all = AsyncMock(side_effect=kill)
    h.bot._sequential = SimpleNamespace(drain_pending=AsyncMock(return_value=0))
    h.bot._abort_all_callback = None
    monkeypatch.setattr(WebCommandDispatcher, "_usage_response", usage)
    assert (await post_message(h, text="foreground")).status == 202
    await asyncio.wait_for(turn_started.wait(), 2)
    pending_usage = asyncio.create_task(post_message(h, text="/usage"))
    control = None
    try:
        await asyncio.wait_for(started.wait(), 2)
        loop = asyncio.get_running_loop()
        before = loop.time()
        control = asyncio.create_task(
            h.client.post("/webchat/v1/topics/-123.7/abort", headers=headers())
            if action == "abort"
            else post_message(h, text=action)
        )
        await asyncio.wait_for(entered.wait(), 1)
        elapsed = loop.time() - before
        print(f"{action}: control entered after {elapsed:.4f}s while /usage pending")
        assert not pending_usage.done()
        response = await asyncio.wait_for(control, 1)
        assert response.status in (200, 202)
    finally:
        release.set()
        turn_release.set()
        await pending_usage
        if control is not None:
            await control


@pytest.mark.parametrize("action", ["/stop", "/interrupt", "/stop_all", "abort"])
async def test_topic_deletion_excludes_control_requests(conversation, action):
    h = conversation
    deleting, release = asyncio.Event(), asyncio.Event()

    async def delete(*_args):
        deleting.set()
        await release.wait()
        return True

    h.runtime.ops.delete_topic.side_effect = delete
    deletion = asyncio.create_task(
        h.client.delete(
            "/webchat/v1/topics/-123.7", json={"confirm_name": "Topic #7"}, headers=headers()
        )
    )
    control = None
    try:
        await asyncio.wait_for(deleting.wait(), 2)
        h.bot._orch._process_registry.kill_by_chat_topic.reset_mock()
        control = asyncio.create_task(
            h.client.post("/webchat/v1/topics/-123.7/abort", headers=headers())
            if action == "abort"
            else post_message(h, text=action)
        )
        done, _ = await asyncio.wait([control], timeout=0.05)
        assert not done
        h.bot._orch._process_registry.kill_by_chat_topic.assert_not_awaited()
        release.set()
        assert (await deletion).status == 200
        assert (await control).status == 404
        assert await h.runtime.history.read("-123.7") == []
        assert await h.runtime.outbox.pending() == []
    finally:
        release.set()
        await deletion
        if control is not None:
            await control
