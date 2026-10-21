"""SessionGateRegistry: FIFO per session, execution slots per agent, exactly-once release."""

from __future__ import annotations

import asyncio

import pytest

from ductor_bot.multiagent.session_gate import GateCancelled, Reservation, SessionGateRegistry

A = ("agent", 1)


async def _settle(times: int = 5) -> None:
    for _ in range(times):
        await asyncio.sleep(0)


def _clean(registry: SessionGateRegistry, agent: str = "agent") -> bool:
    return registry.stats(agent) == (0, 0, 0) and not registry._queues


async def _run(r: Reservation, log: list[str], tag: str, hold: asyncio.Event | None = None) -> None:
    async with r:
        log.append(f"start:{tag}")
        if hold is not None:
            await hold.wait()
        log.append(f"end:{tag}")


async def test_same_session_runs_in_reservation_order_never_overlapping() -> None:
    registry = SessionGateRegistry(max_parallel=4)
    log: list[str] = []
    holds = [asyncio.Event() for _ in range(3)]
    reservations = [registry.reserve("agent", 1, "s") for _ in range(3)]
    # start the waiters in the reverse order: the reservation order still decides
    tasks = [asyncio.create_task(_run(reservations[i], log, str(i), holds[i])) for i in (2, 1, 0)]
    await _settle()
    assert log == ["start:0"]
    holds[0].set()
    await _settle()
    assert log == ["start:0", "end:0", "start:1"]
    holds[1].set()
    holds[2].set()
    await asyncio.gather(*tasks)
    assert log == ["start:0", "end:0", "start:1", "end:1", "start:2", "end:2"]
    assert _clean(registry)


async def test_sessions_run_in_parallel_up_to_the_limit() -> None:
    registry = SessionGateRegistry(max_parallel=2)
    log: list[str] = []
    holds = [asyncio.Event() for _ in range(3)]
    tasks = [
        asyncio.create_task(_run(registry.reserve("agent", 1, f"s{i}"), log, str(i), holds[i]))
        for i in range(3)
    ]
    await _settle()
    assert log == ["start:0", "start:1"]  # the third waits for a slot, in order
    assert registry.stats("agent") == (2, 1, 3)
    holds[0].set()
    await _settle()
    assert "start:2" in log
    assert registry.stats("agent")[0] == 2  # never above the limit
    holds[1].set()
    holds[2].set()
    await asyncio.gather(*tasks)
    assert _clean(registry)


async def test_the_limit_is_per_agent() -> None:
    registry = SessionGateRegistry(max_parallel=1)
    log: list[str] = []
    hold = asyncio.Event()
    tasks = [
        asyncio.create_task(_run(registry.reserve(agent, 1, "s"), log, agent, hold))
        for agent in ("a", "b")
    ]
    await _settle()
    assert sorted(log) == ["start:a", "start:b"]
    hold.set()
    await asyncio.gather(*tasks)


async def test_a_slot_is_never_held_while_waiting_for_the_session_turn() -> None:
    registry = SessionGateRegistry(max_parallel=1)
    log: list[str] = []
    hold_a1, hold_b = asyncio.Event(), asyncio.Event()
    a1 = registry.reserve("agent", 1, "a")
    a2 = registry.reserve("agent", 1, "a")  # waits for a1's turn
    b = registry.reserve("agent", 1, "b")  # waits for the only slot
    tasks = [
        asyncio.create_task(_run(a1, log, "a1", hold_a1)),
        asyncio.create_task(_run(a2, log, "a2")),
        asyncio.create_task(_run(b, log, "b", hold_b)),
    ]
    await _settle()
    assert registry.stats("agent")[0] == 1  # only a1 holds a slot, a2 does not
    hold_a1.set()
    await _settle()
    # a1 done: b (waiting for a slot since before a2 had its turn) goes first, a2 after it
    assert "start:b" in log
    assert "start:a2" not in log
    hold_b.set()
    await asyncio.gather(*tasks)
    assert log.index("start:b") < log.index("start:a2")
    assert _clean(registry)


async def test_queued_and_ahead_reflect_reservations_of_the_same_tick() -> None:
    registry = SessionGateRegistry(max_parallel=2)
    same = [registry.reserve("agent", 1, "one") for _ in range(3)]
    assert [(r.queued, r.ahead) for r in same] == [(False, 0), (True, 1), (True, 2)]
    registry2 = SessionGateRegistry(max_parallel=2)
    many = [registry2.reserve("agent", 1, f"s{i}") for i in range(4)]
    # nothing has run yet: the third and fourth session will wait for a slot
    assert [(r.queued, r.ahead) for r in many] == [(False, 0), (False, 0), (True, 1), (True, 2)]
    for r in [*same, *many]:
        r.release()


# -- cancellation: the place and the slot come back exactly once --------------------------


async def test_cancel_while_waiting_for_the_session_turn() -> None:
    registry = SessionGateRegistry(max_parallel=2)
    first = registry.reserve("agent", 1, "s")
    second = registry.reserve("agent", 1, "s")
    third = registry.reserve("agent", 1, "s")
    await first.admit()
    waiter = asyncio.create_task(second.admit())
    await _settle()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert registry.pending("agent", 1, "s") == 2  # second is gone
    first.release()
    await asyncio.wait_for(third.admit(), 1)  # the next in line proceeds
    third.release()
    assert _clean(registry)


async def test_cancel_while_waiting_for_a_slot() -> None:
    registry = SessionGateRegistry(max_parallel=1)
    running = registry.reserve("agent", 1, "a")
    await running.admit()
    waiting = registry.reserve("agent", 1, "b")
    waiter = asyncio.create_task(waiting.admit())
    await _settle()
    assert registry.stats("agent") == (1, 1, 2)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert registry.stats("agent") == (1, 0, 1)
    running.release()
    assert _clean(registry)


async def test_cancel_while_running_returns_the_slot_once() -> None:
    registry = SessionGateRegistry(max_parallel=1)
    log: list[str] = []
    task = asyncio.create_task(_run(registry.reserve("agent", 1, "a"), log, "a", asyncio.Event()))
    await _settle()
    assert registry.stats("agent")[0] == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _clean(registry)


async def test_cancel_after_the_slot_was_granted_but_before_the_waiter_woke() -> None:
    registry = SessionGateRegistry(max_parallel=1)
    holder = registry.reserve("agent", 1, "a")
    await holder.admit()
    waiting = registry.reserve("agent", 1, "b")
    waiter = asyncio.create_task(waiting.admit())
    await _settle()
    holder.release()  # grants the slot to the waiter's future ...
    waiter.cancel()  # ... which is cancelled before it ran again
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert _clean(registry)  # the granted slot came back


async def test_reservation_that_never_runs_and_double_release_are_safe() -> None:
    registry = SessionGateRegistry(max_parallel=1)
    never = registry.reserve("agent", 1, "a")
    follower = registry.reserve("agent", 1, "a")
    never.release()
    never.release()
    never.release()
    assert registry.stats("agent") == (0, 0, 1)
    await follower.admit()
    follower.release()
    follower.release()
    assert _clean(registry)
    assert registry.stats("agent")[0] == 0  # never negative


async def test_released_while_admit_is_waiting_does_not_leak_a_slot() -> None:
    registry = SessionGateRegistry(max_parallel=1)
    holder = registry.reserve("agent", 1, "a")
    await holder.admit()
    waiting = registry.reserve("agent", 1, "b")
    waiter = asyncio.create_task(waiting.admit())
    await _settle()
    waiting.release()  # external release without cancelling the task
    holder.release()
    with pytest.raises(GateCancelled):
        await waiter
    assert _clean(registry)


async def test_cancel_session_stops_waiters_only() -> None:
    registry = SessionGateRegistry(max_parallel=1)
    running = registry.reserve("agent", 1, "s")
    await running.admit()
    turn_waiter = registry.reserve("agent", 1, "s")
    other = registry.reserve("agent", 1, "t")  # waits for the slot, other session
    slot_waiter = registry.reserve("agent", 1, "u")
    t1 = asyncio.create_task(turn_waiter.admit())
    t2 = asyncio.create_task(slot_waiter.admit())
    t3 = asyncio.create_task(other.admit())
    await _settle()
    assert registry.cancel_session("agent", 1, "s") == 1
    with pytest.raises(GateCancelled):
        await t1
    assert registry.cancel_session("agent", 1, "u") == 1
    with pytest.raises(GateCancelled):
        await t2
    assert running.admitted  # the running request is not interrupted
    running.release()
    await asyncio.wait_for(t3, 1)
    other.release()
    assert _clean(registry)


async def test_changing_the_limit_keeps_one_set_of_slots() -> None:
    registry = SessionGateRegistry(max_parallel=1)
    pool = registry._slots("agent")
    first = registry.reserve("agent", 1, "a")
    second = registry.reserve("agent", 1, "b")
    await first.admit()
    waiter = asyncio.create_task(second.admit())
    await _settle()
    assert registry.stats("agent") == (1, 1, 2)
    registry.set_max_parallel(2)
    await asyncio.wait_for(waiter, 1)
    assert registry._slots("agent") is pool  # same object, not a second semaphore
    assert registry.stats("agent")[0] == 2
    first.release()
    second.release()
    assert _clean(registry)


def test_limit_is_at_least_one() -> None:
    assert SessionGateRegistry(max_parallel=0).max_parallel == 1
    registry = SessionGateRegistry(4)
    registry.set_max_parallel(-3)
    assert registry.max_parallel == 1
