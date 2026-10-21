"""Inter-agent responses inject into active CLI sessions and wait for shared locks."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from unittest.mock import AsyncMock

from ductor_bot.bus.adapters import from_interagent_result
from ductor_bot.bus.bus import MessageBus
from ductor_bot.bus.envelope import LockMode, Origin
from ductor_bot.bus.lock_pool import LockPool

# -- Shared fixtures -----------------------------------------------------------


@dataclass
class _FakeIAResult:
    task_id: str = "task-result-1"
    sender: str = "dev"
    recipient: str = "main"
    message_preview: str = "glossary task done"
    result_text: str = "priority override implemented"
    success: bool = True
    error: str | None = None
    elapsed_seconds: float = 312.0
    session_name: str = "ia-dev"
    provider_switch_notice: str = ""
    original_message: str = "implement the glossary"
    chat_id: int = 0
    topic_id: int | None = None


def _build_injection_prompt(result: _FakeIAResult, agent_name: str = "main") -> str:
    recipient = result.recipient or result.sender
    session_hint = (
        f"\nThe recipient processed this in session `{result.session_name}`."
        if result.session_name
        else ""
    )
    task_context = (
        f"\n\nOriginal task you sent to '{recipient}':\n{result.original_message}"
        if result.original_message
        else ""
    )
    return (
        f"[ASYNC INTER-AGENT RESPONSE from '{recipient}'"
        f" (task {result.task_id})]\n"
        f"{result.result_text}\n"
        f"[END ASYNC INTER-AGENT RESPONSE]{session_hint}{task_context}\n\n"
        f"You are agent '{agent_name}'. Process this response from agent "
        f"'{recipient}' and communicate the relevant results to the user "
        f"in your Telegram chat."
    )


def _mock_transport(name: str = "tg") -> AsyncMock:
    t = AsyncMock()
    t.transport_name = name
    t.deliver = AsyncMock()
    t.deliver_broadcast = AsyncMock()
    return t


# -- Pattern 1: idle — injection fires immediately ----------------------------


async def test_idle_injection_fires() -> None:
    """Pattern 1: Main idle, ia-async result → inject_prompt called once."""
    bus = MessageBus()
    injector = AsyncMock()
    injector.inject_prompt = AsyncMock(return_value="processed by main")
    bus.set_injector(injector)
    bus.register_transport(_mock_transport())

    result = _FakeIAResult()
    prompt = _build_injection_prompt(result)
    env = from_interagent_result(result, chat_id=111222333, injection_prompt=prompt)

    await bus.submit(env)

    injector.inject_prompt.assert_awaited_once()
    call_args = injector.inject_prompt.call_args
    assert call_args.args[0] == prompt
    assert call_args.args[1] == 111222333


async def test_idle_envelope_result_replaced_by_injected_response() -> None:
    """Pattern 1: envelope.result_text is replaced by inject_prompt's return value."""
    bus = MessageBus()
    injector = AsyncMock()
    injector.inject_prompt = AsyncMock(return_value="main's processed reply")
    bus.set_injector(injector)
    bus.register_transport(_mock_transport())

    result = _FakeIAResult()
    prompt = _build_injection_prompt(result)
    env = from_interagent_result(result, chat_id=111222333, injection_prompt=prompt)
    original_raw = env.result_text

    await bus.submit(env)

    assert env.result_text == "main's processed reply"
    assert env.result_text != original_raw


# -- Pattern 2: Main busy — injection queued behind lock, not dropped ----------


async def test_busy_main_injection_not_dropped() -> None:
    """Inter-agent injection waits for a held main lock and delivers after acquisition."""
    lock_pool = LockPool()
    bus = MessageBus(lock_pool=lock_pool)

    inject_called = asyncio.Event()
    injector = AsyncMock()

    async def _inject(_prompt: str, _chat_id: int, _label: str, **_: object) -> str:
        inject_called.set()
        return "injected while busy"

    injector.inject_prompt = AsyncMock(side_effect=_inject)
    bus.set_injector(injector)
    bus.register_transport(_mock_transport())

    chat_lock = lock_pool.get((111222333, None))

    # Simulate Main being busy by holding the lock in a background task
    lock_held = asyncio.Event()
    lock_release = asyncio.Event()

    async def _hold_lock() -> None:
        async with chat_lock:
            lock_held.set()
            await lock_release.wait()

    holder = asyncio.create_task(_hold_lock())
    await lock_held.wait()  # lock is now held

    result = _FakeIAResult()
    prompt = _build_injection_prompt(result)
    env = from_interagent_result(result, chat_id=111222333, injection_prompt=prompt)

    # Submit ia-async while lock is held — should queue, not drop
    submit_task = asyncio.create_task(bus.submit(env))

    # Give the event loop a moment; inject must NOT have fired yet
    await asyncio.sleep(0)
    assert not inject_called.is_set(), "inject_prompt fired while lock was held — race!"

    # Release the lock
    lock_release.set()
    await holder

    # Now the envelope should proceed
    await submit_task

    assert inject_called.is_set(), "inject_prompt never called after lock released — silent drop!"
    assert env.result_text == "injected while busy"


# -- Pattern 3: no prompt → no injection, raw text delivered ------------------


async def test_no_injection_prompt_skips_injection() -> None:
    """Raw deliver path: without injection_prompt, inject_prompt is never called."""
    bus = MessageBus()
    injector = AsyncMock()
    injector.inject_prompt = AsyncMock(return_value="should not be called")
    bus.set_injector(injector)
    bus.register_transport(_mock_transport())

    result = _FakeIAResult()
    env = from_interagent_result(result, chat_id=111222333)  # no injection_prompt

    await bus.submit(env)

    injector.inject_prompt.assert_not_awaited()
    assert env.result_text == result.result_text  # raw text unchanged


async def test_matrix_transport_reaches_injector_and_delivery() -> None:
    """Matrix async results must inject into mx session and deliver to mx transport."""
    bus = MessageBus()
    injector = AsyncMock()
    injector.inject_prompt = AsyncMock(return_value="processed for matrix")
    mx = _mock_transport("mx")
    tg = _mock_transport("tg")
    bus.set_injector(injector)
    bus.register_transport(tg)
    bus.register_transport(mx)

    result = _FakeIAResult(chat_id=555)
    prompt = _build_injection_prompt(result)
    env = from_interagent_result(result, chat_id=100, injection_prompt=prompt, transport="mx")

    await bus.submit(env)

    injector.inject_prompt.assert_awaited_once()
    assert injector.inject_prompt.call_args.kwargs["transport"] == "mx"
    mx.deliver.assert_awaited_once_with(env)
    tg.deliver.assert_not_awaited()


# -- Pattern 4: multiple concurrent tasks, each injected independently ---------


async def test_multiple_concurrent_tasks_all_injected() -> None:
    """Pattern 4: dev + strategy + reviewer all return async results concurrently.
    Each must be injected; none dropped or cross-contaminated.
    """
    lock_pool = LockPool()
    bus = MessageBus(lock_pool=lock_pool)

    injected: list[str] = []

    async def _inject(prompt: str, _chat_id: int, _label: str, **_: object) -> str:
        await asyncio.sleep(0)  # yield to allow interleaving
        injected.append(prompt[:30])
        return f"processed:{prompt[:20]}"

    injector = AsyncMock()
    injector.inject_prompt = AsyncMock(side_effect=_inject)
    bus.set_injector(injector)
    bus.register_transport(_mock_transport())

    senders = ["dev", "strategy", "reviewer"]
    results = [_FakeIAResult(sender=s, task_id=f"task-{s}") for s in senders]
    prompts = [_build_injection_prompt(r) for r in results]
    envs = [
        from_interagent_result(r, chat_id=111222333, injection_prompt=p)
        for r, p in zip(results, prompts, strict=True)
    ]

    # Submit all three concurrently
    await asyncio.gather(*[bus.submit(e) for e in envs])

    assert len(injected) == 3, f"Expected 3 injections, got {len(injected)}"
    assert injector.inject_prompt.await_count == 3


# -- Adapter unit tests for injection behavior -----------------------------------------


def test_adapter_prompt_empty_no_injection() -> None:
    """from_interagent_result without injection_prompt → needs_injection=False."""
    env = from_interagent_result(_FakeIAResult(), chat_id=100)
    assert not env.needs_injection
    assert env.prompt == ""
    assert env.lock_mode == LockMode.REQUIRED  # lock still required


def test_adapter_prompt_set_enables_injection() -> None:
    """from_interagent_result with injection_prompt → needs_injection=True."""
    prompt = _build_injection_prompt(_FakeIAResult())
    env = from_interagent_result(_FakeIAResult(), chat_id=100, injection_prompt=prompt)
    assert env.needs_injection
    assert env.prompt == prompt
    assert env.origin == Origin.INTERAGENT
    assert env.lock_mode == LockMode.REQUIRED


def test_adapter_error_result_requires_injection_prompt() -> None:
    """Failed delegation enters the same main-session injection path."""
    prompt = "should be ignored"
    env = from_interagent_result(
        _FakeIAResult(success=False, error="timeout"),
        chat_id=100,
        injection_prompt=prompt,
    )
    assert not env.is_error
    assert env.needs_injection
    assert env.prompt == prompt
    assert env.lock_mode == LockMode.REQUIRED


def test_adapter_prompt_contains_result_text() -> None:
    """Prompt built from result must embed the raw result_text."""
    result = _FakeIAResult(result_text="OAuth flow complete: tokens stored")
    prompt = _build_injection_prompt(result)
    env = from_interagent_result(result, chat_id=100, injection_prompt=prompt)
    assert result.result_text in env.prompt


def test_adapter_prompt_contains_task_id() -> None:
    result = _FakeIAResult(task_id="oauth-task-75")
    prompt = _build_injection_prompt(result)
    assert "oauth-task-75" in prompt
