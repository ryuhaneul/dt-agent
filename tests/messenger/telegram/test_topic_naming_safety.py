"""Topic-naming safety regressions through real wrappers and a fake Telegram Bot."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ductor_bot.cli import claude_provider, codex_provider, executor, gemini_provider
from ductor_bot.cli.types import CLIResponse
from ductor_bot.messenger.telegram import topic_naming
from ductor_bot.multiagent.delegation import safe_preview
from tests.messenger.telegram.test_topic_naming import drain, implicit, message
from tests.messenger.telegram.test_topic_naming import wired as wired


@pytest.mark.parametrize("path", ["generated", "fallback"])
@pytest.mark.parametrize(
    "text",
    ["sk-\u200bSYNTHETICKEY123", "Bearer\u200b synthetic-secret", "xoxb-\u200b1234-FAKE"],
)
async def test_final_api_title_rechecks_normalized_credentials(wired, path, text):
    tg, bot, _, _, cli, _, _ = wired
    await implicit(tg)
    cli.send.return_value = CLIResponse(result=text if path == "generated" else "")
    await tg._on_message(message(text=text if path == "fallback" else "Normal request"))
    await drain(tg)
    bot.edit_forum_topic.assert_awaited_once()
    title = bot.edit_forum_topic.await_args.kwargs["name"]
    assert not safe_preview(title).startswith("[request preview omitted:")
    assert title == ("42" if path == "fallback" else "Normal request")


async def test_api_boundary_also_checks_the_final_generated_candidate(wired, monkeypatch):
    tg, bot, _, _, _, _, _ = wired
    await implicit(tg)
    monkeypatch.setattr(
        topic_naming.DMTopicNamer, "_generate", AsyncMock(return_value="Bearer\u200b synthetic")
    )
    await tg._on_message(message(text="Safe fallback"))
    await drain(tg)
    assert bot.edit_forum_topic.await_args.kwargs["name"] == "Safe fallback"


@pytest.mark.parametrize("provider", ["gemini", "codex", "claude"])
@pytest.mark.parametrize("stop_mode", ["deadline", "shutdown", "inner_timeout", "success"])
async def test_real_wrapper_reaps_before_unregister_and_cwd_removal(
    wired, monkeypatch, provider, stop_mode
):
    tg, bot, _, orch, _, _, _ = wired
    orch._sessions.get_active.return_value = SimpleNamespace(
        provider=provider, model="synthetic-model"
    )
    released, started = asyncio.Event(), asyncio.Event()
    order, configs = [], []
    deadline_context = None
    if stop_mode == "success":
        released.set()

    async def wait():
        await released.wait()
        order.append("wait")
        proc.returncode = 0
        assert Path(configs[0].working_dir).is_dir()
        return 0

    async def communicate(*args, **kwargs):
        started.set()
        if deadline_context is not None and deadline_context.when() is None:
            # Start the actual 100ms deadline only after the owned process is registered.
            deadline_context.reschedule(asyncio.get_running_loop().time() + 0.1)
        await released.wait()
        # asyncio.Process.communicate() itself waits/reaps on successful completion.
        await proc.wait()
        return b'{"response":"Synthetic title","result":"Synthetic title"}', b""

    proc = SimpleNamespace(
        pid=998877,
        returncode=None,
        communicate=AsyncMock(side_effect=communicate),
        wait=AsyncMock(side_effect=wait),
    )

    def kill(pid):
        assert pid == proc.pid
        order.append("kill")
        released.set()

    def register(*args, **kwargs):
        order.append("register")
        return "synthetic-registration"

    def unregister(tracked):
        assert tracked == "synthetic-registration"
        assert proc.returncode == 0
        assert Path(configs[0].working_dir).is_dir()
        order.append("unregister")

    orch._process_registry.register.side_effect = register
    orch._process_registry.unregister.side_effect = unregister
    monkeypatch.setattr(gemini_provider, "find_gemini_cli", lambda: "/synthetic/gemini")
    monkeypatch.setattr(gemini_provider, "find_gemini_cli_js", lambda: None)
    monkeypatch.setattr(gemini_provider, "force_kill_process_tree", kill)
    monkeypatch.setattr(executor, "force_kill_process_tree", kill)
    monkeypatch.setattr(
        executor.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)
    )
    monkeypatch.setattr(gemini_provider.GeminiCLI, "_prepare_env", lambda *_: {})
    monkeypatch.setattr(
        codex_provider.CodexCLI, "_find_cli", staticmethod(lambda: "/synthetic/codex")
    )
    monkeypatch.setattr(
        claude_provider.ClaudeCodeCLI, "_find_cli", staticmethod(lambda: "/synthetic/claude")
    )
    wrappers = {
        "gemini": gemini_provider.GeminiCLI,
        "codex": codex_provider.CodexCLI,
        "claude": claude_provider.ClaudeCodeCLI,
    }

    def create(config):
        configs.append(config)
        cli = wrappers[provider](config)
        if stop_mode == "inner_timeout":
            send = cli.send

            async def shorter(**kwargs):
                return await send(**dict(kwargs, timeout_seconds=0.01))

            cli.send = shorter
        return cli

    monkeypatch.setattr(topic_naming, "create_cli", create)
    if stop_mode == "deadline":
        # A loaded worker can legitimately take longer than 100ms before process registration.
        auth = topic_naming.check_provider_auth(provider)

        def slow_auth(_provider):
            time.sleep(0.15)
            return auth

        monkeypatch.setattr(topic_naming, "check_provider_auth", slow_auth)
        real_timeout = asyncio.timeout

        def registration_timeout(seconds):
            nonlocal deadline_context
            if seconds != 0.1:
                return real_timeout(seconds)
            deadline_context = real_timeout(None)
            return deadline_context

        monkeypatch.setattr(topic_naming.asyncio, "timeout", registration_timeout)
    monkeypatch.setattr(topic_naming, "_GENERATION_TIMEOUT", 0.1 if stop_mode == "deadline" else 5)
    await implicit(tg)
    await tg._on_message(message(text="Normal request"))
    if stop_mode == "shutdown":
        await asyncio.wait_for(started.wait(), 1)
        await asyncio.wait_for(tg._topic_namer.stop(), 1)
        bot.edit_forum_topic.assert_not_awaited()
    else:
        await asyncio.wait_for(drain(tg), 5)
        bot.edit_forum_topic.assert_awaited_once()
    assert order == (
        ["register", "wait", "unregister"]
        if stop_mode == "success"
        else ["register", "kill", "wait", "unregister"]
    )
    assert proc.wait.await_count == 1
    assert proc.returncode == 0
    assert not Path(configs[0].working_dir).exists()


async def test_deadline_before_registration_falls_back_without_owned_process(wired, monkeypatch):
    tg, bot, _, orch, cli, factory, _ = wired
    auth = topic_naming.check_provider_auth("codex")

    def slow_auth(_provider):
        time.sleep(0.05)
        return auth

    monkeypatch.setattr(topic_naming, "check_provider_auth", slow_auth)
    monkeypatch.setattr(topic_naming, "_GENERATION_TIMEOUT", 0.01)
    await implicit(tg)
    await tg._on_message(message(text="Normal request"))
    await asyncio.wait_for(drain(tg), 5)
    factory.assert_not_called()
    cli.send.assert_not_awaited()
    orch._process_registry.register.assert_not_called()
    orch._process_registry.unregister.assert_not_called()
    bot.edit_forum_topic.assert_awaited_once_with(
        chat_id=100, message_thread_id=42, name="Normal request"
    )
