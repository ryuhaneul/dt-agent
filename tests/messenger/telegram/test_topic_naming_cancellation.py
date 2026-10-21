"""Cancellation and reaping regressions through actual CLI wrappers and topic tasks."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from ductor_bot.cli import (
    claude_provider,
    codex_provider,
    executor,
    gemini_provider,
    grok_provider,
    process_registry,
)
from ductor_bot.cli.base import CLIConfig
from ductor_bot.cli.process_registry import ProcessRegistry
from ductor_bot.messenger.telegram import topic_naming
from tests.messenger.telegram.test_topic_naming import implicit, message, wired  # noqa: F401

PROVIDERS = ["gemini", "codex", "claude", "grok"]


def create_wrapper(monkeypatch, provider, config):
    monkeypatch.setattr(gemini_provider, "find_gemini_cli", lambda: "/synthetic/gemini")
    monkeypatch.setattr(gemini_provider, "find_gemini_cli_js", lambda: None)
    monkeypatch.setattr(gemini_provider.GeminiCLI, "_prepare_env", lambda *_: {})
    for mod, cls, name in [
        (codex_provider, "CodexCLI", "codex"),
        (claude_provider, "ClaudeCodeCLI", "claude"),
        (grok_provider, "GrokCLI", "grok"),
    ]:
        monkeypatch.setattr(
            getattr(mod, cls), "_find_cli", staticmethod(lambda n=name: "/synthetic/" + n)
        )
    wrappers = {
        "gemini": gemini_provider.GeminiCLI,
        "codex": codex_provider.CodexCLI,
        "claude": claude_provider.ClaudeCodeCLI,
        "grok": grok_provider.GrokCLI,
    }
    return wrappers[provider](config)


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize(
    "case", ["single_cancel", "repeat_cancel", "timeout_repeat_cancel", "already_exited", "success"]
)
async def test_wrapper_cancellation_wait_contract(monkeypatch, tmp_path, provider, case):  # noqa: C901
    started = asyncio.Event()
    wait_started = asyncio.Event()
    reap = asyncio.Event()
    communicate_release = asyncio.Event()
    order = []
    snapshot = []
    repeating = case in {"repeat_cancel", "timeout_repeat_cancel"}
    if case == "success":
        communicate_release.set()
        reap.set()
    elif case == "already_exited":
        reap.set()

    async def wait():
        order.append("wait-start")
        wait_started.set()
        await reap.wait()
        proc.returncode = 0
        order.append("wait-done")
        return 0

    async def communicate(*_args, **_kwargs):
        started.set()
        if case != "timeout_repeat_cancel" or proc.communicate.await_count == 1:
            await communicate_release.wait()
        await proc.wait()
        return b'{"response":"Synthetic reply","result":"Synthetic reply"}', b""

    proc = SimpleNamespace(
        pid=998877,
        returncode=0 if case == "already_exited" else None,
        communicate=AsyncMock(side_effect=communicate),
        wait=AsyncMock(side_effect=wait),
    )

    def kill(pid):
        assert pid == proc.pid
        order.append("kill")
        if not repeating:
            reap.set()

    def register(*_args, **_kwargs):
        order.append("register")
        return "tracked"

    def unregister(*_args):
        snapshot.append(proc.returncode)
        order.append("unregister")

    reg = Mock(register=Mock(side_effect=register), unregister=Mock(side_effect=unregister))
    monkeypatch.setattr(executor, "force_kill_process_tree", kill)
    monkeypatch.setattr(gemini_provider, "force_kill_process_tree", kill)
    monkeypatch.setattr(executor.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    cli = create_wrapper(
        monkeypatch,
        provider,
        CLIConfig(
            provider=provider,
            model="synthetic-model",
            working_dir=tmp_path,
            ductor_home=tmp_path,
            process_registry=reg,
            chat_id=100,
            topic_id=42,
        ),
    )
    timeout = 0.03 if case == "timeout_repeat_cancel" else 5
    cancellations = []

    async def send():
        try:
            return await cli.send(prompt="Synthetic question", timeout_seconds=timeout)
        except asyncio.CancelledError as cancellation:
            cancellations.append(cancellation.args)
            raise

    task = asyncio.create_task(send())
    await asyncio.wait_for(started.wait(), 1)
    if case not in {"success", "timeout_repeat_cancel"}:
        task.cancel("original cancellation")
    if repeating:
        await asyncio.wait_for(wait_started.wait(), 1)
        task.cancel("second cancellation")
        await asyncio.sleep(0)
        task.cancel("third cancellation")
        await asyncio.sleep(0)
        # Captured before simulated OS exit/reaping completes.
        before_reap = (list(snapshot), task.done(), list(order))
        reap.set()
    result = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 1)
    assert reg.unregister.call_count == 1
    if case in {"single_cancel", "repeat_cancel"}:
        assert cancellations == [("original cancellation",)]
    if repeating:
        assert before_reap[0] == [], {"provider": provider, "before_reap": before_reap}
        assert not before_reap[1], {"provider": provider, "before_reap": before_reap}
        assert proc.wait.await_count == 1
        assert isinstance(result[0], asyncio.CancelledError)
        assert snapshot == [0]
        assert order.index("wait-done") < order.index("unregister")
    elif case == "already_exited":
        assert order.count("kill") == 0, {
            "provider": provider,
            "order": order,
            "already_exited_returncode": 0,
        }
    elif case == "single_cancel":
        assert order == ["register", "kill", "wait-start", "wait-done", "unregister"]
        assert isinstance(result[0], asyncio.CancelledError)
    else:
        assert order == ["register", "wait-start", "wait-done", "unregister"]


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("mode", ["stop", "interrupt", "shutdown", "stop_then_cancel"])
async def test_normal_dialog_registry_controls(monkeypatch, tmp_path, provider, mode):  # noqa: C901
    started = asyncio.Event()
    terminated = asyncio.Event()
    released = asyncio.Event()
    order = []

    async def wait():
        await released.wait()
        proc.returncode = 0
        order.append("wait")
        return 0

    async def communicate(*_args, **_kwargs):
        started.set()
        await released.wait()
        await proc.wait()
        return b'{"response":"Synthetic reply","result":"Synthetic reply"}', b""

    proc = SimpleNamespace(
        pid=998877,
        returncode=None,
        stdin=None,
        communicate=AsyncMock(side_effect=communicate),
        wait=AsyncMock(side_effect=wait),
    )

    def terminate(_pid):
        order.append("terminate")
        terminated.set()
        proc.returncode = 0
        released.set()

    def kill(_pid):
        order.append("kill")
        released.set()

    def interrupt(_pid):
        order.append("interrupt")
        released.set()

    for mod in [executor, gemini_provider, process_registry]:
        monkeypatch.setattr(mod, "force_kill_process_tree", kill)
    monkeypatch.setattr(process_registry, "terminate_process_tree", terminate)
    monkeypatch.setattr(process_registry, "interrupt_process", interrupt)
    monkeypatch.setattr(process_registry, "_SIGTERM_GRACE_SECONDS", 0.001)
    monkeypatch.setattr(executor.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    registry = ProcessRegistry()
    cli = create_wrapper(
        monkeypatch,
        provider,
        CLIConfig(
            provider=provider,
            model="synthetic-model",
            working_dir=tmp_path,
            ductor_home=tmp_path,
            process_registry=registry,
            process_label="main",
            chat_id=100,
            topic_id=42,
        ),
    )
    task = asyncio.create_task(cli.send(prompt="Synthetic question", timeout_seconds=5))
    await asyncio.wait_for(started.wait(), 1)
    assert registry.has_active(100, 42)
    if mode == "interrupt":
        assert registry.interrupt_all(100) == 1
    elif mode == "shutdown":
        task.cancel()
    else:
        stop = asyncio.create_task(registry.kill_by_chat_topic(100, 42))
        # /stop can kill first and the owner can cancel before pipe draining finishes.
        if mode == "stop_then_cancel":
            await asyncio.wait_for(terminated.wait(), 1)
            task.cancel()
        assert await asyncio.wait_for(stop, 1) == 1
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 1)
    assert not registry.has_active(100, 42)
    if mode == "interrupt":
        assert order == ["interrupt", "wait"]
    elif mode == "shutdown":
        assert order == ["kill", "wait"]
    elif mode == "stop":
        assert order == ["terminate", "wait"]
    else:
        assert order.count("kill") == 0, {
            "provider": provider,
            "order": order,
            "returncode": proc.returncode,
        }


@pytest.mark.parametrize("provider", ["codex", "gemini"])
async def test_namer_shutdown_during_deadline_reap(wired, monkeypatch, provider):  # noqa: F811
    tg, bot, _, orch, _, _, _ = wired
    orch._sessions.get_active.return_value = SimpleNamespace(
        provider=provider, model="synthetic-model"
    )
    started = asyncio.Event()
    waiting = asyncio.Event()
    reap = asyncio.Event()
    order = []
    configs = []

    async def wait():
        waiting.set()
        order.append("wait-start")
        await reap.wait()
        proc.returncode = 0
        order.append("wait-done")
        return 0

    async def communicate(*_args, **_kwargs):
        started.set()
        await asyncio.Event().wait()

    proc = SimpleNamespace(
        pid=998877,
        returncode=None,
        communicate=AsyncMock(side_effect=communicate),
        wait=AsyncMock(side_effect=wait),
    )

    def kill(_pid):
        order.append("kill")

    def create(config):
        configs.append(config)
        cli = create_wrapper(monkeypatch, provider, config)
        send = cli.send

        async def inner(**kwargs):
            return await send(**dict(kwargs, timeout_seconds=5))

        cli.send = inner
        return cli

    monkeypatch.setattr(executor, "force_kill_process_tree", kill)
    monkeypatch.setattr(gemini_provider, "force_kill_process_tree", kill)
    monkeypatch.setattr(executor.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    monkeypatch.setattr(topic_naming, "create_cli", create)
    monkeypatch.setattr(topic_naming, "_GENERATION_TIMEOUT", 0.03)
    orch._process_registry.unregister.side_effect = lambda *_: order.append("unregister")
    await implicit(tg)
    await tg._on_message(message(text="Safe normal request"))
    await asyncio.wait_for(waiting.wait(), 1)
    stop_task = asyncio.create_task(tg._topic_namer.stop())
    for _ in range(20):
        await asyncio.sleep(0)
    state = {
        "provider": provider,
        "stop_returned_before_reap": stop_task.done(),
        "wait_completed": "wait-done" in order,
        "unregistered": orch._process_registry.unregister.call_count,
        "cwd_exists": Path(configs[0].working_dir).exists(),  # noqa: ASYNC240
        "order": list(order),
    }
    reap.set()
    await asyncio.wait_for(stop_task, 1)
    for _ in range(20):
        if "unregister" in order:
            break
        await asyncio.sleep(0)
    bot.edit_forum_topic.assert_not_awaited()
    assert not state["stop_returned_before_reap"], state
    assert not state["wait_completed"]
    assert state["unregistered"] == 0
    assert state["cwd_exists"]
    assert "wait-done" in order
    assert orch._process_registry.unregister.call_count == 1
    assert order.index("wait-done") < order.index("unregister")
    assert order.count("kill") == 1
    assert proc.wait.await_count == 1
    assert not Path(configs[0].working_dir).exists()  # noqa: ASYNC240
