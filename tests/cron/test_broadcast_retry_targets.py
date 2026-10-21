"""Cron broadcast retries only transports that missed the preserved result."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ductor_bot.bus.bus import MessageBus
from ductor_bot.orchestrator.observers import ObserverManager
from tests.cron.test_observer import _make_job, _make_manager, _make_observer, _make_paths


def _wired_observer(tmp_path, failed_names: tuple[str, ...]):
    paths = _make_paths(tmp_path)
    manager = _make_manager(paths)
    manager.add_job(_make_job("daily"))
    observer = _make_observer(
        paths,
        manager,
        cron_delivery_retry={"enabled": True, "interval_seconds": 60},
    )
    bus = MessageBus()
    transports = {}
    for name in ("tg", "mx"):
        transport = MagicMock()
        transport.transport_name = name
        transport.deliver_broadcast = AsyncMock()
        if name in failed_names:
            transport.deliver_broadcast.side_effect = RuntimeError("offline")
        bus.register_transport(transport)
        transports[name] = transport
    owner = SimpleNamespace(
        cron=observer,
        heartbeat=MagicMock(),
        background=None,
        webhook=None,
    )
    ObserverManager.wire_to_bus(owner, bus)
    return manager, observer, transports


@pytest.mark.parametrize("failed_names", [("tg",), ("tg", "mx")])
async def test_retry_targets_only_failed_broadcast_transports(
    tmp_path, failed_names: tuple[str, ...]
) -> None:
    manager, observer, transports = _wired_observer(tmp_path, failed_names)
    delivered, error, failed = await observer._deliver_result(
        "daily", "Daily Report", "Report", "success"
    )
    assert not delivered
    assert failed == list(failed_names)
    assert all(name in error for name in failed_names)
    manager.update_run_status(
        "daily",
        status="success",
        delivery_status="failed",
        delivery_error=error,
        failed_transports=failed,
        result_text="Report",
    )
    manager.reload()
    job = manager.get_job("daily")
    assert job is not None
    assert job.last_delivery_status == "failed"
    assert job.failed_transports == list(failed_names)
    for transport in transports.values():
        transport.deliver_broadcast.reset_mock()
        transport.deliver_broadcast.side_effect = None

    await observer._retry_failed_deliveries()

    for name, transport in transports.items():
        assert transport.deliver_broadcast.await_count == int(name in failed_names)
    assert job.last_delivery_status == "ok"
    assert job.last_result_text is None
    assert job.failed_transports == []


async def test_all_successful_broadcasts_have_no_retry_targets(tmp_path) -> None:
    _manager, observer, transports = _wired_observer(tmp_path, ())
    delivered, error, failed = await observer._deliver_result(
        "daily", "Daily Report", "Report", "success"
    )
    assert delivered
    assert error == ""
    assert failed == []
    for transport in transports.values():
        transport.deliver_broadcast.assert_awaited_once()


async def test_retry_failure_narrows_next_retry_to_remaining_transport(tmp_path) -> None:
    manager, observer, transports = _wired_observer(tmp_path, ("tg", "mx"))
    delivered, error, failed = await observer._deliver_result(
        "daily", "Daily Report", "Report", "success"
    )
    assert not delivered
    manager.update_run_status(
        "daily",
        status="success",
        delivery_status="failed",
        delivery_error=error,
        failed_transports=failed,
        result_text="Report",
    )
    transports["tg"].deliver_broadcast.side_effect = None
    await observer._retry_failed_deliveries()
    job = manager.get_job("daily")
    assert job is not None
    assert job.last_delivery_status == "failed"
    assert job.failed_transports == ["mx"]
    job.next_delivery_retry_at = None
    transports["tg"].deliver_broadcast.reset_mock()
    transports["mx"].deliver_broadcast.reset_mock()
    transports["mx"].deliver_broadcast.side_effect = None

    await observer._retry_failed_deliveries()

    transports["tg"].deliver_broadcast.assert_not_awaited()
    transports["mx"].deliver_broadcast.assert_awaited_once()
    assert job.last_delivery_status == "ok"


async def test_transport_flagged_failure_is_recorded_by_name(tmp_path) -> None:
    _manager, observer, transports = _wired_observer(tmp_path, ())

    async def flag_failure(envelope) -> None:
        envelope.delivered = False
        envelope.delivery_error = "send failed"

    transports["tg"].deliver_broadcast.side_effect = flag_failure
    delivered, error, failed = await observer._deliver_result(
        "daily", "Daily Report", "Report", "success"
    )
    assert not delivered
    assert failed == ["tg"]
    assert "tg" in error
    transports["mx"].deliver_broadcast.assert_awaited_once()


async def test_targeted_retry_with_no_registered_transport_keeps_target() -> None:
    from ductor_bot.bus.envelope import DeliveryMode, Envelope, Origin

    bus = MessageBus()
    env = Envelope(
        origin=Origin.CRON,
        chat_id=0,
        delivery=DeliveryMode.BROADCAST,
        broadcast_targets=["tg"],
        result_text="Report",
    )
    await bus.submit(env)
    assert not env.delivered
    assert env.failed_transports == ["tg"]
    assert "tg" in env.delivery_error
