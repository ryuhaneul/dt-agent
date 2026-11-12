"""Delivery observers run once after processing without changing delivery outcomes."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from ductor_bot.bus.bus import MessageBus
from ductor_bot.bus.envelope import DeliveryMode, Envelope, LockMode, Origin
from ductor_bot.bus.lock_pool import LockPool


def transport():
    adapter = MagicMock(transport_name="tg")
    adapter.deliver = AsyncMock()
    adapter.deliver_broadcast = AsyncMock()
    return adapter


def envelope(**kwargs):
    return Envelope(origin=Origin.CRON, chat_id=1, result_text="public result", **kwargs)


async def test_none_observer_preserves_delivery_outcome() -> None:
    bus = MessageBus()
    adapter = transport()
    bus.register_transport(adapter)
    before = await bus.submit(envelope())
    bus.set_delivery_observer(None)
    after = await bus.submit(envelope())
    assert before == after
    assert adapter.deliver.await_count == 2


@pytest.mark.parametrize("mode", [DeliveryMode.UNICAST, DeliveryMode.BROADCAST])
async def test_observer_sees_final_envelope_once_outside_lock(mode) -> None:
    pool = LockPool()
    bus = MessageBus(pool)
    adapter = transport()
    called = []

    async def deliver(env):
        assert pool.is_locked(env.lock_key)
        env.metadata["chat_log_entry_id"] = "123456abcdef"
        env.delivered = False
        env.delivery_error = "send failed"

    async def observe(env):
        assert not pool.is_locked(env.lock_key)
        assert env.metadata["chat_log_entry_id"] == "123456abcdef"
        assert env.delivered is False
        called.append(env)

    adapter.deliver.side_effect = deliver
    adapter.deliver_broadcast.side_effect = deliver
    bus.register_transport(adapter)
    bus.set_delivery_observer(observe)
    env = envelope(delivery=mode, lock_mode=LockMode.REQUIRED)
    outcome = await bus.submit(env)
    assert outcome.delivered is False
    assert called == [env]


async def test_raising_observer_does_not_change_outcome(caplog) -> None:
    bus = MessageBus()
    bus.register_transport(transport())
    before = await bus.submit(envelope())
    observer = AsyncMock(side_effect=RuntimeError("observer unavailable"))
    bus.set_delivery_observer(observer)
    after = await bus.submit(envelope())
    assert after == before
    observer.assert_awaited_once()
    assert "Delivery observer failed" in caplog.text
    bus.set_delivery_observer(None)
    await bus.submit(envelope())
    observer.assert_awaited_once()


async def test_observer_runs_after_injection_failure_and_delivery_failure() -> None:
    bus = MessageBus()
    adapter = transport()
    adapter.deliver.side_effect = OSError("send failed")
    injector = MagicMock()
    injector.inject_prompt = AsyncMock(side_effect=RuntimeError("injection failed"))
    bus.register_transport(adapter)
    bus.set_injector(injector)
    observer = AsyncMock()
    bus.set_delivery_observer(observer)
    env = envelope(needs_injection=True, prompt="internal injection", lock_mode=LockMode.REQUIRED)
    result = await bus.submit(env)
    assert result.ok is False
    assert result.injected is False
    assert result.delivered is False
    observer.assert_awaited_once_with(env)


async def test_fallback_is_not_submitted_to_observer() -> None:
    bus = MessageBus()
    adapter = transport()
    bus.register_transport(adapter)
    observer = AsyncMock()
    bus.set_delivery_observer(observer)
    env = envelope(transport="mx")
    await bus.submit(env)
    observer.assert_awaited_once_with(env)
    adapter.deliver_broadcast.assert_awaited_once()
    assert adapter.deliver_broadcast.call_args.args[0] is not env
