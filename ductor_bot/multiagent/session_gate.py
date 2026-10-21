"""FIFO admission for inter-agent sessions plus a per-agent execution-slot limit.

One :class:`SessionGateRegistry` lives on the inter-agent bus and is never replaced, so an agent
restart cannot create a second set of queues or slots. A request first *reserves* its place
(synchronous, no await), later *admits* itself (waits for its turn in the session queue, then for
a free execution slot) and finally *releases*. Reservation order is the execution order of one
session; a session never runs two requests at once; at most ``max_parallel`` sessions of one agent
run at the same time. A slot is never held while waiting for a session turn.

Releasing is idempotent and also correct for a request that was cancelled while waiting for its
turn, waiting for a slot or running: every reservation returns its place exactly once.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Self

logger = logging.getLogger(__name__)

GateKey = tuple[str, int, str]  # (receiving agent, stored chat id, Named session name)


class GateCancelled(Exception):  # noqa: N818 -- a control signal, not an error condition
    """The reservation was cancelled (for example its session ended) before it was admitted."""

    def __str__(self) -> str:
        return "session ended before the request started"


class _Slots:
    """FIFO counting semaphore whose capacity can change without replacing the object."""

    def __init__(self, capacity: int) -> None:
        self.capacity = max(1, capacity)
        self.in_use = 0
        self.waiters: deque[asyncio.Future[None]] = deque()

    async def acquire(self, owner: Reservation) -> None:
        if self.in_use < self.capacity and not self.waiters:
            self.in_use += 1
            return
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.waiters.append(future)
        owner.slot_future = future
        try:
            await future
        except BaseException:
            if future in self.waiters:
                self.waiters.remove(future)
            elif future.done() and not future.cancelled():
                self.release()  # granted while being cancelled: give the slot back
            raise
        finally:
            owner.slot_future = None

    def release(self) -> None:
        self.in_use -= 1
        self._wake()

    def _wake(self) -> None:
        while self.waiters and self.in_use < self.capacity:
            future = self.waiters.popleft()
            if future.done():
                continue
            self.in_use += 1
            future.set_result(None)

    def set_capacity(self, capacity: int) -> None:
        self.capacity = max(1, capacity)
        self._wake()


@dataclass(eq=False)
class Reservation:
    """A place in a session queue. Use :meth:`admit` and always :meth:`release`."""

    registry: SessionGateRegistry
    key: GateKey
    ahead: int  # reservations in front of this one when it was made (estimate for slots)
    turn: asyncio.Event = field(default_factory=asyncio.Event)
    has_turn: bool = False
    admitted: bool = False
    released: bool = False
    cancelled: bool = False
    slot_future: asyncio.Future[None] | None = None

    @property
    def queued(self) -> bool:
        """True when this request had to wait behind other requests when it was reserved."""
        return self.ahead > 0

    async def admit(self) -> None:
        """Wait for the session turn, then for an execution slot. Raises on cancellation."""
        try:
            await self.turn.wait()
            if self.cancelled:
                raise GateCancelled  # noqa: TRY301 -- control flow of this single method
            await self.registry._slots(self.key[0]).acquire(self)
            if self.cancelled or self.released:
                self.registry._slots(self.key[0]).release()
                raise GateCancelled  # noqa: TRY301 -- same
            self.admitted = True
        except BaseException:
            self.release()
            raise

    def release(self) -> None:
        """Return the place (and the slot, if held). Safe to call more than once."""
        if self.released:
            return
        self.released = True
        if self.admitted:
            self.admitted = False
            self.registry._slots(self.key[0]).release()
        self.registry._remove(self)

    def cancel(self) -> None:
        """Make a waiting request give up; a running one is not interrupted."""
        if self.admitted or self.released:
            return
        self.cancelled = True
        self.turn.set()
        if self.slot_future is not None and not self.slot_future.done():
            self.slot_future.set_exception(GateCancelled())

    async def __aenter__(self) -> Self:
        await self.admit()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self.release()


class SessionGateRegistry:
    """Per-(agent, chat, session) FIFO queues and per-agent execution slots."""

    def __init__(self, max_parallel: int = 4) -> None:
        self._max_parallel = max(1, max_parallel)
        self._agent_limits: dict[str, int] = {}
        self._queues: dict[GateKey, deque[Reservation]] = {}
        self._slot_pools: dict[str, _Slots] = {}
        self._heads: dict[str, int] = {}

    @property
    def max_parallel(self) -> int:
        return self._max_parallel

    def set_max_parallel(self, value: int) -> None:
        """Change the default limit (agents without an own limit) on the existing slot pools."""
        self._max_parallel = max(1, value)
        for agent, pool in self._slot_pools.items():
            if agent not in self._agent_limits:
                pool.set_capacity(self._max_parallel)

    def set_agent_limit(self, agent: str, value: int) -> None:
        """Set one agent's limit; an existing slot pool only changes its capacity."""
        self._agent_limits[agent] = max(1, value)
        pool = self._slot_pools.get(agent)
        if pool is not None:
            pool.set_capacity(self._agent_limits[agent])

    def limit(self, agent: str) -> int:
        return self._agent_limits.get(agent, self._max_parallel)

    def _slots(self, agent: str) -> _Slots:
        pool = self._slot_pools.get(agent)
        if pool is None:
            pool = self._slot_pools[agent] = _Slots(self.limit(agent))
        return pool

    def reserve(self, agent: str, chat_id: int, name: str) -> Reservation:
        """Take a place at the end of the session queue (synchronous: no await, no yield)."""
        key: GateKey = (agent, chat_id, name)
        queue = self._queues.setdefault(key, deque())
        behind = len(queue)
        heads = self._heads.get(agent, 0)
        capacity = self._slots(agent).capacity
        ahead = behind or max(0, heads - capacity + 1)
        reservation = Reservation(self, key, ahead)
        queue.append(reservation)
        if behind == 0:
            self._grant(reservation)
        return reservation

    def _grant(self, reservation: Reservation) -> None:
        reservation.has_turn = True
        agent = reservation.key[0]
        self._heads[agent] = self._heads.get(agent, 0) + 1
        reservation.turn.set()

    def _remove(self, reservation: Reservation) -> None:
        key = reservation.key
        queue = self._queues.get(key)
        if queue is None:
            return
        was_head = bool(queue) and queue[0] is reservation
        try:
            queue.remove(reservation)
        except ValueError:
            return
        if reservation.has_turn:
            reservation.has_turn = False
            self._heads[key[0]] = max(0, self._heads.get(key[0], 0) - 1)
        if queue:
            if was_head:
                self._grant(queue[0])
        else:
            del self._queues[key]

    def pending(self, agent: str, chat_id: int, name: str) -> int:
        """Number of reservations (waiting or running) for one session."""
        return len(self._queues.get((agent, chat_id, name), ()))

    def cancel_session(self, agent: str, chat_id: int, name: str) -> int:
        """Cancel the waiting reservations of one session; a running one is left to its caller."""
        waiting = [r for r in self._queues.get((agent, chat_id, name), ()) if not r.admitted]
        for reservation in waiting:
            reservation.cancel()
        return len(waiting)

    def stats(self, agent: str) -> tuple[int, int, int]:
        """``(running, waiting for a slot, open session queues)`` for diagnostics and tests."""
        pool = self._slot_pools.get(agent)
        queues = sum(1 for key in self._queues if key[0] == agent)
        return (pool.in_use if pool else 0, len(pool.waiters) if pool else 0, queues)
