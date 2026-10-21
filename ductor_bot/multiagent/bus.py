"""InterAgentBus: in-memory async message passing between agents."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import secrets
import time
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ductor_bot.bus.envelope import IA_CHUNK_SCHEMES, DeliveryOutcome
from ductor_bot.bus.failure_reason import QUEUE_WAIT_TIMEOUT, RECIPIENT_GONE
from ductor_bot.i18n import t
from ductor_bot.infra.input_context import (
    add_input_wait,
    current_input_context,
    current_input_timestamp,
    received_at,
    received_input,
)
from ductor_bot.multiagent.delegation import DelegationContext, capture_delegation, safe_preview
from ductor_bot.multiagent.failure_ledger import FailureLedger
from ductor_bot.multiagent.persistence import (
    AsyncRecordStore,
    CorruptAsyncRecordError,
    merge_redelivery_state,
    progress_reset_reason,
)
from ductor_bot.multiagent.session_gate import (
    Reservation,
    SessionGateRegistry,
)
from ductor_bot.session.named import (
    interagent_session_name,
    lane_from_session_name,
    normalize_lane,
)

if TYPE_CHECKING:
    from ductor_bot.bus.envelope import Envelope
    from ductor_bot.multiagent.stack import AgentStack

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 300.0  # 5 minutes — for synchronous sends
_ASYNC_TIMEOUT = 3600.0  # 1 hour — async tasks may run complex multi-step work
# Execution time counts from admission. Waiting (queue + preparation) has its own limit.
_DEFAULT_MAX_PARALLEL_SESSIONS = 4
_DEFAULT_MAX_QUEUE_WAIT = 3600.0
_RESTART_POLL_SECONDS = 0.05  # how often a waiting request looks for a (re)started recipient


class RecipientUnavailable(Exception):  # noqa: N818 -- carries the user-facing reason
    """The recipient is gone for good or never became ready."""


SYNC_DELEGATION_QUESTION_REJECTED = (
    "Questions to main are not allowed during a synchronous delegation. Continue the work and "
    "put any question you need in the final response. Do not invent unknown values: leave "
    "them empty or state your assumption."
)
SYNC_SELF_SESSION_REJECTED = (
    "A sync request cannot be sent to this agent's own current session: the session waits for "
    "the reply and cannot handle the request at the same time, so both would block. Send it on "
    "another lane (--lane) or asynchronously."
)
REDELIVERY_BACKOFF_BASE = 300
REDELIVERY_BACKOFF_MAX = 6 * 3600


class HeadlessAsyncRejected(ValueError):  # noqa: N818 — planned public error name
    """A headless agent cannot receive an asynchronous result."""

    def __init__(self, receiver: str = "") -> None:
        if receiver:
            super().__init__(
                f"headless agent '{receiver}' cannot receive async results — "
                "use ask_agent.py (sync) or set a messaging-enabled reply_to"
            )
        else:
            super().__init__(
                "A delegation-only agent cannot use asynchronous calls. Use ask_agent.py (sync)."
            )


class AsyncRecordError(ValueError):
    """The async request was rejected because its durable record failed."""


class AsyncResultReceiverError(ValueError):
    """The async result has no registered agent callback to receive it."""

    def __init__(self, receiver: str) -> None:
        super().__init__(
            f"no async result receiver for '{receiver}' — "
            "use ask_agent.py (sync) or set a valid reply_to"
        )


def _source_kwargs(chat_id: int, topic_id: int | None, transport: str) -> dict[str, object]:
    if chat_id or topic_id is not None:
        return {
            "source_chat_id": chat_id,
            "source_topic_id": topic_id,
            "source_transport": transport,
        }
    return {}


def _receipt_time() -> float:
    return time.time()


@dataclass(slots=True)
class InterAgentResponse:
    """Response from an inter-agent message."""

    sender: str
    text: str
    success: bool = True
    error: str | None = None
    received_at: float = field(default_factory=_receipt_time)
    received_context: str = ""
    queued: bool = False  # the request waited behind another one of the same session/limit
    ahead: int = 0


@dataclass(frozen=True, slots=True)
class AsyncSendOptions:
    """Optional metadata for :meth:`InterAgentBus.send_async`.

    Bundles keyword-only parameters that control session handling and
    Telegram routing so the public API stays within the argument limit.

    *new_session*: end any existing inter-agent session before processing.
    *summary*: notification preview shown in the recipient's Telegram chat.
    *chat_id* / *topic_id*: originating transport chat/topic context so
    that results are delivered back to the correct thread.
    *transport*: short transport id (``"tg"`` / ``"mx"``) for multi-transport
    result routing.
    """

    new_session: bool = False
    summary: str = ""
    chat_id: int = 0
    topic_id: int | None = None
    transport: str = ""
    # #86: when non-empty, the async result handler is resolved against this
    # agent name instead of ``sender`` — lets cross-server SSH pipelines
    # route replies to a specific agent even when ``sender`` is "unknown".
    reply_to: str = ""
    # #86: suppress the ``_notify_recipient`` "Async task received from X"
    # broadcast; useful in automated code-review-style pipelines where the
    # user only wants to see the final result.
    silent: bool = False
    result_origin: str = ""
    broadcast_ref: str = ""
    source_session_id: str = ""
    source_session_kind: str = ""
    source_session_name: str = ""
    # Normalized lane (``""`` = none): a parallel inter-agent session of the same sender/origin.
    lane: str = ""


@dataclass(slots=True)
class AsyncInterAgentTask:
    """Tracks an in-flight async inter-agent request."""

    task_id: str
    sender: str
    recipient: str
    message: str
    new_session: bool = False
    summary: str = ""
    timestamp: float = field(default_factory=time.time)
    asyncio_task: asyncio.Task[None] | None = field(default=None, repr=False)
    chat_id: int = 0
    topic_id: int | None = None
    transport: str = ""
    reply_to: str = ""  # #86
    silent: bool = False  # #86
    result_origin: str = ""
    broadcast_ref: str = ""
    source_session_id: str = ""
    source_session_kind: str = ""
    source_session_name: str = ""
    lane: str = ""
    # Admission: the place in the recipient's session queue, taken when the request is accepted.
    queued: bool = False
    ahead: int = 0
    reservation: Reservation | None = field(default=None, repr=False)


def _checked_lane(lane: str) -> tuple[str, str]:
    """Return ``(normalized lane, "")`` or ``("", error text)`` for a malformed lane."""
    try:
        return normalize_lane(lane), ""
    except ValueError as exc:
        return "", str(exc)


def _lane_kwargs(lane: str) -> dict[str, object]:
    """Pass the lane to the recipient only when set (unlaned calls stay unchanged)."""
    return {"lane": lane} if lane else {}


def task_session_name(task: AsyncInterAgentTask) -> str:
    """Name of the recipient's inter-agent session this task runs in (deterministic)."""
    return interagent_session_name(task.sender, task.chat_id, task.topic_id, task.lane)


@dataclass(slots=True)
class AsyncInterAgentResult:
    """Result delivered to the sender agent after async processing completes."""

    task_id: str
    sender: str
    recipient: str
    message_preview: str
    result_text: str
    success: bool = True
    error: str | None = None
    elapsed_seconds: float = 0.0
    session_name: str = ""
    provider_switch_notice: str = ""
    original_message: str = ""
    chat_id: int = 0
    topic_id: int | None = None
    transport: str = ""
    reply_to: str = ""  # #86 — overrides sender for handler lookup when set
    result_origin: str = ""
    broadcast_ref: str = ""
    failure_count: int = 0
    failure_kind: str = ""  # "result_lost" means execution may have completed.
    main_response: str = ""  # Populated only when retrying a completed injection.
    delivered_chunks: int = 0  # Leading final-report chunks already sent.
    delivery_retry: bool = False
    recipient_headless: bool = False  # Derived from the registry at delivery time.
    received_at: float = field(default_factory=_receipt_time)  # First receipt, not replay.


AsyncResultCallback = Callable[["AsyncInterAgentResult"], Awaitable[DeliveryOutcome]]


def _make_result(  # noqa: PLR0913  -- result fields have natural multi-arity
    task: AsyncInterAgentTask,
    t0: float,
    *,
    result_text: str = "",
    success: bool = True,
    error: str | None = None,
    session_name: str = "",
    provider_switch_notice: str = "",
) -> AsyncInterAgentResult:
    """Build an :class:`AsyncInterAgentResult` carrying the task's routing fields."""
    return AsyncInterAgentResult(
        task_id=task.task_id,
        sender=task.sender,
        recipient=task.recipient,
        message_preview=task.message[:60],
        result_text=result_text,
        success=success,
        error=error,
        elapsed_seconds=time.time() - t0,
        session_name=session_name or task_session_name(task),
        provider_switch_notice=provider_switch_notice,
        original_message=task.message,
        chat_id=task.chat_id,
        topic_id=task.topic_id,
        transport=task.transport,
        reply_to=task.reply_to,
        result_origin=task.result_origin,
        broadcast_ref=task.broadcast_ref,
    )


class InterAgentBus:
    """In-memory async bus for agent-to-agent communication.

    All agents in the same process share this bus. Messages are handled
    by calling the target agent's Orchestrator.handle_interagent_message().
    """

    def __init__(
        self,
        main_home: Path | None = None,
        *,
        max_parallel_sessions: int = _DEFAULT_MAX_PARALLEL_SESSIONS,
        max_queue_wait_seconds: float = _DEFAULT_MAX_QUEUE_WAIT,
    ) -> None:
        # One registry for the whole process life: agent restarts never create a second one.
        self._gates = SessionGateRegistry(max_parallel_sessions)
        self._max_queue_wait = max_queue_wait_seconds
        # Agents whose stack is being stopped (restart or removal): never run requests on it.
        self._unavailable: set[str] = set()
        # Handlers (admitted requests holding an orchestrator) per recipient, so that a restart
        # can wait for the old stack's handlers before a new stack reads the stored state.
        self._running_handlers: dict[str, int] = {}
        self._drain_waiters: dict[str, list[asyncio.Future[None]]] = {}
        # Supervisor hook: is this agent expected to come (back)? ``None``: no restart handling.
        self._agent_expected: Callable[[str], bool] | None = None
        self._failure_ledger = FailureLedger(main_home) if main_home is not None else None
        self._record_store = AsyncRecordStore(main_home) if main_home is not None else None
        self._agents: dict[str, AgentStack] = {}
        self._delegations: dict[str, DelegationContext] = {}
        self._async_tasks: dict[str, AsyncInterAgentTask] = {}
        self._async_result_handlers: dict[str, AsyncResultCallback] = {}
        self._retry_delays = (2, 4, 8, 16, 32)
        self._retry_waiting: dict[str, AsyncInterAgentResult] = {}
        self._is_delivery_ready: Callable[[str], bool] | None = None
        self._delivering_results: dict[str, AsyncInterAgentResult] = {}
        self._injected_responses: dict[str, str] = {}
        # Keep memory copies only when durable parking fails. Restart can lose those.
        self._unpersisted_results: dict[str, AsyncInterAgentResult] = {}
        self._unpersisted_redelivery: dict[str, tuple[int, float | None]] = {}
        self._parked_results: dict[str, AsyncInterAgentResult] = {}
        self._delivery_claims: set[str] = set()
        # Delivery acknowledgements survive failed cleanup until both copies are gone.
        self._delivered_results: set[str] = set()

    def _session_chat(self, recipient: str) -> int:
        """Chat id under which the recipient stores its inter-agent sessions."""
        stack = self._agents.get(recipient)
        users = getattr(getattr(stack, "config", None), "allowed_user_ids", None)
        return users[0] if users else 0

    def _reserve(self, recipient: str, session_name: str) -> Reservation:
        return self._gates.reserve(recipient, self._session_chat(recipient), session_name)

    @property
    def gates(self) -> SessionGateRegistry:
        """The inter-agent session gates (one registry for the whole bus life)."""
        return self._gates

    def admission_info(self, task_id: str) -> tuple[bool, int]:
        """``(queued, requests ahead)`` for an accepted async task (``(False, 0)`` if unknown)."""
        task = self._async_tasks.get(task_id)
        return (task.queued, task.ahead) if task is not None else (False, 0)

    def set_max_parallel_sessions(self, value: int) -> None:
        """Change the concurrent-session limit on the existing gates."""
        self._gates.set_max_parallel(value)

    def set_delivery_ready_checker(self, checker: Callable[[str], bool]) -> None:
        """Use supervisor runtime readiness before delivering async results."""
        self._is_delivery_ready = checker

    def _delivery_ready(self, target: str) -> bool:
        return self._is_delivery_ready is None or self._is_delivery_ready(target)

    def set_agent_expected_checker(self, checker: Callable[[str], bool]) -> None:
        """Let the supervisor say whether a missing agent is restarting (True) or gone (False)."""
        self._agent_expected = checker

    @contextlib.contextmanager
    def _handler_scope(self, name: str) -> Iterator[None]:
        """Count a running handler of *name* (taken in the same step as the orchestrator lookup)."""
        self._running_handlers[name] = self._running_handlers.get(name, 0) + 1
        try:
            yield
        finally:
            self._handler_done(name)

    def _handler_done(self, name: str) -> None:
        left = self._running_handlers[name] - 1
        if left > 0:
            self._running_handlers[name] = left
            return
        del self._running_handlers[name]
        for waiter in self._drain_waiters.pop(name, []):
            if not waiter.done():
                waiter.set_result(None)

    async def drain_agent(self, name: str, timeout: float) -> bool:  # noqa: ASYNC109
        """Wait until the handlers already running on *name*'s stack have finished.

        Returns ``False`` when *timeout* ran out first. Call it after the stack was marked
        unavailable (no new handler starts) and its CLIs were stopped.
        """
        if not self._running_handlers.get(name):
            return True
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._drain_waiters.setdefault(name, []).append(waiter)
        try:
            await asyncio.wait_for(waiter, timeout)
        except TimeoutError:
            return False
        return True

    def mark_unavailable(self, name: str) -> None:
        """The agent's stack is shutting down: waiting requests must wait for the next stack."""
        self._unavailable.add(name)

    def register(self, name: str, stack: AgentStack) -> None:
        """Register an agent (a new stack of a restarted agent included)."""
        self._agents[name] = stack
        self._unavailable.discard(name)
        interagent = getattr(getattr(stack, "config", None), "interagent", None)
        limit = getattr(interagent, "max_parallel_sessions", None)
        if isinstance(limit, int) and not isinstance(limit, bool):
            # Same slot pool, new capacity: a restart never creates a second set of slots.
            self._gates.set_agent_limit(name, limit)
        logger.debug("Bus: registered agent '%s'", name)

    def _queue_wait_for(self, recipient: str) -> float:
        """Wait limit (queue + preparation + restart) from the recipient's configuration."""
        stack = self._agents.get(recipient)
        interagent = getattr(getattr(stack, "config", None), "interagent", None)
        value = getattr(interagent, "max_queue_wait_seconds", None)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return float(value)
        return self._max_queue_wait

    def _orchestrator_now(self, name: str) -> Any:
        """The orchestrator of the recipient's current stack if it is ready right now."""
        stack = self._agents.get(name)
        if stack is None or name in self._unavailable:
            return None
        return stack.bot.orchestrator

    async def _await_orchestrator(self, name: str, deadline: float) -> Any:
        """Ready orchestrator of a synchronous request's recipient within its time budget."""
        orch = self._orchestrator_now(name)
        if orch is None:
            orch = await asyncio.wait_for(
                self._ready_orchestrator(name),
                timeout=max(0, deadline - asyncio.get_running_loop().time()),
            )
        return await self._confirm_orchestrator(
            name, orch, max(0, deadline - asyncio.get_running_loop().time())
        )

    async def _confirm_orchestrator(self, name: str, orch: Any, wait: float) -> Any:
        """The orchestrator to run on, re-checked in the step that starts the handler.

        A wait hop can end after the stack was marked unavailable; running on it would let the
        old stack's handler outlive the restart's drain. No suspension when nothing changed.
        """
        deadline = asyncio.get_running_loop().time() + wait
        while self._orchestrator_now(name) is not orch:
            orch = await asyncio.wait_for(
                self._ready_orchestrator(name),
                timeout=max(0, deadline - asyncio.get_running_loop().time()),
            )
        return orch

    async def _ready_orchestrator(self, name: str) -> Any:
        """The recipient's orchestrator once its current stack is ready.

        A restarting recipient (stack stopped, not yet registered again, or still starting) is
        waited for; the caller bounds the wait. A recipient that is gone for good, or without any
        restart handling, fails at once.
        """
        while True:
            stack = self._agents.get(name)
            usable = stack is not None and name not in self._unavailable
            orch = stack.bot.orchestrator if usable and stack is not None else None
            if orch is not None:
                return orch
            if self._agent_expected is None or not self._agent_expected(name):
                if usable:
                    msg = f"Agent '{name}' orchestrator not initialized"
                else:
                    msg = f"{RECIPIENT_GONE}: '{name}'"
                raise RecipientUnavailable(msg)
            await asyncio.sleep(_RESTART_POLL_SECONDS)

    def unregister(self, name: str) -> None:
        """Unregister an agent from the bus."""
        self._agents.pop(name, None)
        self._unavailable.discard(name)
        self._async_result_handlers.pop(name, None)
        logger.debug("Bus: unregistered agent '%s'", name)

    def list_agents(self) -> list[str]:
        """List all registered agent names."""
        return list(self._agents.keys())

    def has_async_result_handler(self, agent_name: str) -> bool:
        """Whether an agent has a registered async result receiver."""
        return agent_name in self._async_result_handlers

    def is_headless_agent(self, agent_name: str) -> bool:
        """Whether the registered agent has no messenger transport."""
        stack = self._agents.get(agent_name)
        return stack is not None and stack.config.transport == "headless"

    def delegation_mode(self, delegation_id: str, sender: str) -> str:
        """Look up the mode of main's active delegation to this sender."""
        context = self._delegations.get(delegation_id)
        if context and context.sender == "main" and context.recipient == sender:
            return context.mode
        return "unknown"

    def assert_async_sender_allowed(self, sender: str) -> None:
        """Reject async sends originating from a headless agent."""
        if self.is_headless_agent(sender):
            raise HeadlessAsyncRejected

    def assert_async_result_receiver_available(self, receiver: str) -> None:
        """Require a registered, messaging-enabled agent with a result callback."""
        if self.is_headless_agent(receiver):
            raise HeadlessAsyncRejected(receiver)
        if receiver not in self._agents or not self.has_async_result_handler(receiver):
            raise AsyncResultReceiverError(receiver)

    def tool_response_metadata(self, receiver: str) -> dict[str, str | float]:
        """Timestamp a tool reply at bus/API receipt, in the calling agent's timezone."""
        stack = self._agents.get(receiver)
        orch = stack.bot.orchestrator if stack is not None else None
        config = getattr(orch, "_config", stack.config) if stack is not None else None
        timezone = getattr(config, "user_timezone", "UTC")
        if not isinstance(timezone, str):
            timezone = "UTC"
        with received_at():
            stamp = current_input_timestamp()
            context = current_input_context(timezone)
            assert stamp is not None
            assert context is not None
            return {"received_at": stamp, "received_context": context}

    @received_input
    async def send(  # noqa: PLR0913
        self,
        sender: str,
        recipient: str,
        message: str,
        *,
        send_timeout: float = _DEFAULT_TIMEOUT,
        new_session: bool = False,
        chat_id: int = 0,
        topic_id: int | None = None,
        transport: str = "",
        delegation_id: str = "",
        ia_turn: bool = False,
        source_session_id: str = "",
        source_session_kind: str = "",
        source_session_name: str = "",
        lane: str = "",
    ) -> InterAgentResponse:
        """Send a message to another agent and wait for the response.

        The target agent's Orchestrator runs a one-shot CLI turn to process
        the message. Returns the response text or an error.
        """
        response = self._sync_response(sender)

        lane, lane_error = _checked_lane(lane)
        target_name = interagent_session_name(sender, chat_id, topic_id, lane)
        # Waiting for its own current session would never end (a question never has this shape).
        own_session = sender == recipient and target_name == source_session_name

        if lane_error or own_session or recipient not in self._agents:
            available = ", ".join(self._agents.keys()) or "(none)"
            return response(
                sender=recipient,
                text="",
                success=False,
                error=lane_error
                or (SYNC_SELF_SESSION_REJECTED if own_session else None)
                or f"Agent '{recipient}' not found. Available: {available}",
            )

        question = self._matching_question(sender, recipient, delegation_id)
        if question is None and ia_turn and recipient == "main" and sender != "main":
            question = DelegationContext(
                request_id="",
                mode="unknown",
                sender=recipient,
                recipient=sender,
                chat_id=chat_id,
                topic_id=topic_id,
                transport=transport or "tg",
            )
        if question and question.mode == "sync":
            logger.info(
                "Bus: rejected %s -> main question during sync delegation %s",
                sender,
                question.request_id,
            )
            return response(
                sender=recipient,
                text="",
                success=False,
                error=SYNC_DELEGATION_QUESTION_REJECTED,
            )
        logger.info("Bus: %s -> %s (%d chars)", sender, recipient, len(message))

        # Reserve the place in the recipient's session queue before the first await. A question
        # reserves later (only if it needs a Named session after the original-session answer).
        reservation = None if question else self._reserve(recipient, target_name)
        context: DelegationContext | None = None
        deadline = asyncio.get_running_loop().time() + send_timeout
        try:
            kwargs: dict[str, object] = {"new_session": new_session, **_lane_kwargs(lane)}
            if question:
                # A question reserves only if it needs a Named session after the original-session
                # answer failed: the recipient calls this factory at that point.
                kwargs["admission"] = lambda session_chat, name: self._gates.reserve(
                    recipient, session_chat, name
                )
                # Use the dispatch snapshot, not the question's claimed origin.
                chat_id, topic_id, transport = (
                    question.chat_id,
                    question.topic_id,
                    question.transport,
                )
                kwargs["question_context"] = question
            if sender == "main" and recipient != "main":
                context = await asyncio.wait_for(
                    self._capture_delegation(
                        secrets.token_hex(6),
                        "sync",
                        sender,
                        recipient,
                        message,
                        chat_id,
                        topic_id,
                        transport,
                        source_session_id,
                        source_session_kind,
                        source_session_name,
                    ),
                    timeout=max(0, deadline - asyncio.get_running_loop().time()),
                )
                self._delegations[context.request_id] = context
                kwargs["delegation"] = context
            kwargs.update(_source_kwargs(chat_id, topic_id, transport))
            queued, ahead = await self._enter_sync(reservation, deadline)
            # The stack is looked up now (after any wait): a restarted recipient runs on its
            # new stack, and a recipient that is gone fails here.
            try:
                orch = await self._await_orchestrator(recipient, deadline)
            except RecipientUnavailable as exc:
                return response(sender=recipient, text="", success=False, error=str(exc))
            execution = orch.handle_interagent_message(sender, message, **kwargs)
            with self._handler_scope(recipient):
                result_text, _session_name, _notice = await asyncio.wait_for(
                    execution,
                    timeout=max(0, deadline - asyncio.get_running_loop().time()),
                )
            logger.info(
                "Bus: %s -> %s completed (%d chars response)",
                sender,
                recipient,
                len(result_text),
            )
            return response(sender=recipient, text=result_text, queued=queued, ahead=ahead)

        except TimeoutError:
            logger.warning("Bus: %s -> %s timed out after %.0fs", sender, recipient, send_timeout)
            return response(
                sender=recipient,
                text="",
                success=False,
                error=f"Timeout after {send_timeout:.0f}s",
            )
        except Exception as exc:
            logger.exception("Bus: %s -> %s failed", sender, recipient)
            return response(
                sender=recipient,
                text="",
                success=False,
                error=f"{type(exc).__name__}: {exc}",
            )

        finally:
            self._leave_sync(reservation)
            if context:
                self._delegations.pop(context.request_id, None)

    def _sync_response(self, caller: str) -> Callable[..., InterAgentResponse]:
        def response(  # noqa: PLR0913 -- result fields
            *,
            sender: str,
            text: str,
            success: bool = True,
            error: str | None = None,
            queued: bool = False,
            ahead: int = 0,
        ) -> InterAgentResponse:
            metadata = self.tool_response_metadata(caller)
            return InterAgentResponse(
                sender=sender,
                text=text,
                success=success,
                error=error,
                received_at=float(metadata["received_at"]),
                received_context=str(metadata["received_context"]),
                queued=queued,
                ahead=ahead,
            )

        return response

    def _matching_question(
        self, sender: str, recipient: str, delegation_id: str
    ) -> DelegationContext | None:
        question = self._delegations.get(delegation_id)
        if not (
            question and question.recipient == sender and question.sender == recipient == "main"
        ):
            question = None
        return question

    async def _enter_sync(
        self, reservation: Reservation | None, deadline: float
    ) -> tuple[bool, int]:
        """Admit a synchronous request within its own time budget (questions have no place yet)."""
        if reservation is None:
            return False, 0
        waited_from = time.monotonic()
        await asyncio.wait_for(
            reservation.admit(),
            timeout=max(0, deadline - asyncio.get_running_loop().time()),
        )
        add_input_wait(time.monotonic() - waited_from)
        return reservation.queued, reservation.ahead

    @staticmethod
    def _leave_sync(reservation: Reservation | None) -> None:
        if reservation is not None:
            reservation.release()

    async def _capture_delegation(  # noqa: PLR0913, PLR0917 -- keeps the existing positional call contract
        self,
        request_id: str,
        mode: str,
        sender: str,
        recipient: str,
        message: str,
        chat_id: int,
        topic_id: int | None,
        transport: str,
        source_session_id: str,
        source_session_kind: str,
        source_session_name: str,
    ) -> DelegationContext:
        stack = self._agents.get(sender)
        orch = stack.bot.orchestrator if stack else None
        context = DelegationContext(
            request_id=request_id,
            mode=mode,
            sender=sender,
            recipient=recipient,
            chat_id=chat_id,
            topic_id=topic_id,
            transport=transport or "tg",
            request_preview=safe_preview(message),
        )
        return await capture_delegation(
            orch, context, source_session_id, source_session_kind, source_session_name
        )

    # -- Async (fire-and-forget) communication ---------------------------------

    def set_async_result_handler(
        self,
        agent_name: str,
        handler: AsyncResultCallback,
    ) -> None:
        """Register callback for delivering async results back to a sender agent."""
        self._async_result_handlers[agent_name] = handler
        stack = self._agents.get(agent_name)
        message_bus = getattr(stack.bot, "_bus", None) if stack is not None else None
        if self._record_store is not None and message_bus is not None:
            message_bus.set_on_injected(self._on_injected)

    def send_async(
        self,
        sender: str,
        recipient: str,
        message: str,
        *,
        opts: AsyncSendOptions | None = None,
    ) -> str | None:
        """Send a message to another agent asynchronously.

        Returns a task_id immediately. The response will be delivered to the
        sender agent's registered callback when the target agent finishes.
        Returns None if the recipient is not found.

        Optional *opts* controls session handling and Telegram routing.
        See :class:`AsyncSendOptions` for details.
        """
        self.assert_async_sender_allowed(sender)
        if recipient not in self._agents:
            return None

        o = opts or AsyncSendOptions()
        lane = normalize_lane(o.lane)  # ValueError for a malformed lane, before anything exists
        result_receiver = o.reply_to or sender
        self.assert_async_result_receiver_available(result_receiver)
        task_id = secrets.token_hex(6)
        task = AsyncInterAgentTask(
            task_id=task_id,
            sender=sender,
            recipient=recipient,
            message=message,
            new_session=o.new_session,
            summary=o.summary,
            chat_id=o.chat_id,
            topic_id=o.topic_id,
            transport=o.transport,
            reply_to=o.reply_to,
            silent=o.silent,
            result_origin=o.result_origin,
            broadcast_ref=o.broadcast_ref,
            source_session_id=o.source_session_id,
            source_session_kind=o.source_session_kind,
            source_session_name=o.source_session_name,
            lane=lane,
        )
        if self._record_store is not None:
            try:
                self._record_store.create(task)
            except (OSError, ValueError) as exc:
                logger.error(  # noqa: TRY400
                    "Async task record creation failed task=%s: %s", task_id, type(exc).__name__
                )
                raise AsyncRecordError("Unable to persist async task") from exc
        # Take the place in the recipient's session queue now, in acceptance order: before the
        # coroutine exists, so several requests accepted in one tick keep their order and an
        # accurate "queued" answer.
        reservation = self._reserve(recipient, task_session_name(task))
        task.reservation = reservation
        task.queued, task.ahead = reservation.queued, reservation.ahead
        atask = asyncio.create_task(
            self._run_async(task),
            name=f"ia-async:{sender}->{recipient}:{task_id}",
        )
        task.asyncio_task = atask
        # Also covers a task cancelled before its first step (its own cleanup never runs).
        atask.add_done_callback(lambda _: reservation.release())
        atask.add_done_callback(lambda _: self._async_tasks.pop(task_id, None))
        self._async_tasks[task_id] = task

        logger.info(
            "Bus async: %s -> %s task=%s (%d chars) chat_id=%s topic_id=%s transport=%s",
            sender,
            recipient,
            task_id,
            len(message),
            task.chat_id,
            task.topic_id,
            task.transport,
        )
        return task_id

    async def _run_async(self, task: AsyncInterAgentTask) -> None:
        """Execute the async inter-agent message and deliver result to sender."""
        with received_at(task.timestamp):
            await self._execute_async(task)

    async def _prepare_and_admit(
        self, task: AsyncInterAgentTask, reservation: Reservation
    ) -> tuple[DelegationContext | None, float, Any]:
        """Preparation (notice, provenance), admission and the recipient lookup (all bounded).

        Returns the delegation context, the seconds spent waiting for admission and the
        orchestrator of the recipient's *current* ready stack (looked up after the wait).
        """
        context: DelegationContext | None = None
        try:
            # Notify the recipient agent's chat about the incoming task
            await self._notify_recipient(task)
            if task.sender == "main" and task.recipient != "main":
                context = await self._capture_delegation(
                    task.task_id,
                    "async",
                    task.sender,
                    task.recipient,
                    task.message,
                    task.chat_id,
                    task.topic_id,
                    task.transport,
                    task.source_session_id,
                    task.source_session_kind,
                    task.source_session_name,
                )
                self._delegations[context.request_id] = context
            waited_from = time.monotonic()
            await reservation.admit()
            waited = time.monotonic() - waited_from
            orch = self._orchestrator_now(task.recipient) or await self._ready_orchestrator(
                task.recipient
            )
        except BaseException:
            if context is not None:
                self._delegations.pop(context.request_id, None)
            raise
        else:
            return context, waited, orch

    async def _execute_async(self, task: AsyncInterAgentTask) -> None:
        t0 = time.time()
        context: DelegationContext | None = None
        reservation = task.reservation or self._reserve(task.recipient, task_session_name(task))
        wait_limit = self._queue_wait_for(task.recipient)
        try:
            # Waiting (preparation + queue + a restarting recipient) and execution have separate
            # limits; execution time counts from admission.
            try:
                context, waited, orch = await asyncio.wait_for(
                    self._prepare_and_admit(task, reservation),
                    timeout=wait_limit,
                )
                orch = await self._confirm_orchestrator(task.recipient, orch, wait_limit)
            except RecipientUnavailable as exc:
                reservation.release()
                await self._deliver_async_result(
                    _make_result(task, t0, success=False, error=str(exc))
                )
                return
            except TimeoutError:
                logger.warning(
                    "Bus async: %s -> %s task=%s not started after %.0fs of waiting",
                    task.sender,
                    task.recipient,
                    task.task_id,
                    wait_limit,
                )
                reservation.release()
                await self._deliver_async_result(
                    _make_result(
                        task,
                        t0,
                        success=False,
                        error=f"{QUEUE_WAIT_TIMEOUT} ({wait_limit:.0f}s)",
                    )
                )
                return
            add_input_wait(waited)
            deadline = asyncio.get_running_loop().time() + _ASYNC_TIMEOUT

            kwargs: dict[str, object] = _lane_kwargs(task.lane)
            if context is not None:
                kwargs["delegation"] = context
            if task.chat_id or task.topic_id is not None:
                execution = orch.handle_interagent_message(
                    task.sender,
                    task.message,
                    new_session=task.new_session,
                    **kwargs,
                    source_chat_id=task.chat_id,
                    source_topic_id=task.topic_id,
                    source_transport=task.transport,
                )
            else:
                execution = orch.handle_interagent_message(
                    task.sender,
                    task.message,
                    new_session=task.new_session,
                    **kwargs,
                )
            with self._handler_scope(task.recipient):
                result_text, session_name, provider_notice = await asyncio.wait_for(
                    execution,
                    timeout=max(0, deadline - asyncio.get_running_loop().time()),
                )
            # The session is free again before the result callback (original-chat lock) runs.
            reservation.release()
            logger.info(
                "Bus async: %s -> %s task=%s session=%s completed (%d chars, %.1fs) "
                "chat_id=%s topic_id=%s transport=%s",
                task.sender,
                task.recipient,
                task.task_id,
                session_name,
                len(result_text),
                time.time() - t0,
                task.chat_id,
                task.topic_id,
                task.transport,
            )
            await self._deliver_async_result(
                _make_result(
                    task,
                    t0,
                    result_text=result_text,
                    session_name=session_name,
                    provider_switch_notice=provider_notice,
                )
            )

        except TimeoutError:
            logger.warning(
                "Bus async: %s -> %s task=%s timed out",
                task.sender,
                task.recipient,
                task.task_id,
            )
            reservation.release()
            await self._deliver_async_result(
                _make_result(
                    task,
                    t0,
                    success=False,
                    error=f"Timeout after {_ASYNC_TIMEOUT:.0f}s",
                )
            )

        except Exception as exc:
            logger.exception("Bus async: %s -> %s failed", task.sender, task.recipient)
            reservation.release()
            await self._deliver_async_result(
                _make_result(
                    task,
                    t0,
                    success=False,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )

        finally:
            reservation.release()
            if context:
                self._delegations.pop(context.request_id, None)

    async def _notify_recipient(self, task: AsyncInterAgentTask) -> None:
        """Send a short notification to the recipient agent's chat.

        This makes async task delegation visible — the recipient's user sees
        what task was received and from whom before processing begins.
        Best-effort: failures are logged but never block execution.

        Opt-in through the receiving agent's ``interagent_notify_received`` config.
        #86: skipped entirely when ``task.silent`` is set or when
        ``task.reply_to`` is set (automated pipelines route replies
        explicitly and don't want routing-noise notifications).
        """
        if task.silent or task.reply_to:
            return
        try:
            target = self._agents.get(task.recipient)
            if target is None or not target.config.interagent_notify_received:
                return

            ns = target.bot.notification_service

            # Use explicit summary if provided, otherwise truncate message
            if task.summary:
                preview = task.summary
            else:
                preview = task.message if len(task.message) <= 200 else task.message[:200] + "…"
            session_name = task_session_name(task)
            text = t(
                "multiagent.async_task_received",
                sender=task.sender,
                session=session_name,
                task_id=task.task_id,
                preview=preview,
            )

            if task.chat_id:
                await ns.notify_origin(task.transport, task.chat_id, text, topic_id=task.topic_id)
                return
            if task.topic_id is not None:
                logger.warning(
                    "Skipping async task notification with topic_id=%s and no chat_id",
                    task.topic_id,
                )
                return
            chat_id = target.config.allowed_user_ids[0] if target.config.allowed_user_ids else 0
            if chat_id:
                await ns.notify(chat_id, text)
            else:
                await ns.notify_all(text)
        except Exception:
            logger.debug(
                "Failed to notify recipient '%s' about async task %s (non-critical)",
                task.recipient,
                task.task_id,
                exc_info=True,
            )

    def _repair_if_corrupt(  # noqa: PLR0913 -- recovery needs saved stage and progress
        self,
        target: str,
        task_id: str,
        *,
        stage: str | None = None,
        main_response: str | None = None,
        delivered_chunks: int | None = None,
        delivered_text_sha256: str | None = None,
        chunk_scheme: str | None = None,
    ) -> bool:
        store = self._record_store
        result = self._delivering_results.get(task_id)
        if store is None or result is None:
            return False
        path = "(unresolved)"
        try:
            path = str(store.path_for(target, task_id))
            store.load(target, task_id)
        except (
            CorruptAsyncRecordError,
            json.JSONDecodeError,
            UnicodeDecodeError,
            TypeError,
        ) as damage:
            response = (
                main_response or self._injected_responses.get(task_id, "") or result.main_response
            )
            known_stage = (
                stage
                if stage in {"injected", "delivered"} and response.strip()
                else ("injected" if response.strip() else "received")
            )
            task = self._async_tasks.get(task_id)
            if task is None:
                task = AsyncInterAgentTask(
                    task_id=task_id,
                    sender=result.sender,
                    recipient=result.recipient,
                    message=result.original_message,
                    chat_id=result.chat_id,
                    topic_id=result.topic_id,
                    transport=result.transport,
                    reply_to=result.reply_to,
                    result_origin=result.result_origin,
                    broadcast_ref=result.broadcast_ref,
                    lane=lane_from_session_name(result.session_name),
                )
            progress_reusable = (
                known_stage == "injected"
                and bool(delivered_chunks)
                and delivered_text_sha256 == hashlib.sha256(response.encode("utf-8")).hexdigest()
                and chunk_scheme in IA_CHUNK_SCHEMES.values()
                and (not task.transport or chunk_scheme == IA_CHUNK_SCHEMES.get(task.transport))
            )
            if (
                known_stage == "received"
                or result.delivered_chunks
                or (delivered_chunks and not progress_reusable)
            ):
                logger.warning(
                    "Async record stage/progress reset task=%s; duplicate delivery possible",
                    task_id,
                )
            try:
                archived = store.recover_corrupt(
                    task,
                    result,
                    stage=known_stage,
                    main_response=response,
                    delivered_chunks=delivered_chunks or 0,
                    delivered_text_sha256=delivered_text_sha256 or "",
                    chunk_scheme=chunk_scheme or "",
                )
            except Exception as exc:
                logger.error(  # noqa: TRY400
                    "Async record recovery failed path=%s error=%s",
                    path,
                    type(exc).__name__,
                )
                self._unpersisted_results[task_id] = result
                return False
            logger.error(  # noqa: TRY400 -- log only the exception class, never record contents
                "Async corrupt record quarantined path=%s archive=%s error=%s",
                path,
                archived,
                type(damage).__name__,
            )
            self._unpersisted_results.pop(task_id, None)
            return True
        except Exception:
            return False
        return False

    def _update_record(  # noqa: PLR0913 -- keeps the existing positional call contract
        self,
        target: str,
        task_id: str,
        *,
        result: AsyncInterAgentResult | None = None,
        stage: str | None = None,
        main_response: str | None = None,
        delivered_chunks: int | None = None,
        delivered_text_sha256: str | None = None,
        chunk_scheme: str | None = None,
        reset_progress: bool = False,
    ) -> bool:
        if self._record_store is None:
            return True
        path = "(unresolved)"
        pending = self._unpersisted_redelivery.get(task_id)
        if pending is not None and result is None:
            result = self._unpersisted_results.get(task_id)
        try:
            path = str(self._record_store.path_for(target, task_id))
            self._record_store.update(
                target,
                task_id,
                result=result,
                stage=stage,
                main_response=main_response,
                delivered_chunks=delivered_chunks,
                delivered_text_sha256=delivered_text_sha256,
                chunk_scheme=chunk_scheme,
                reset_progress=reset_progress,
                redelivery_state=pending,
            )
        except Exception as exc:
            logger.error(  # noqa: TRY400
                "Async record update failed path=%s error=%s",
                path,
                type(exc).__name__,
            )
            if not self._repair_if_corrupt(
                target,
                task_id,
                stage=stage,
                main_response=main_response,
                delivered_chunks=delivered_chunks,
                delivered_text_sha256=delivered_text_sha256,
                chunk_scheme=chunk_scheme,
            ):
                return False
            if pending is not None:
                try:
                    self._record_store.update(
                        target,
                        task_id,
                        redelivery_state=pending,
                    )
                except Exception:
                    return False
        if pending is not None:
            self._unpersisted_redelivery.pop(task_id, None)
        return True

    async def _on_injected(self, envelope: Envelope) -> None:
        """Save main's final response before a transport attempts delivery."""
        if self._record_store is None:
            return
        task_id = envelope.metadata.get("ia_task_id")
        if not task_id:
            return
        target = envelope.metadata.get("ia_target")
        if not target:
            # The target is fixed at intake, independent of the active handler.
            target = envelope.metadata.get("reply_to") or envelope.metadata.get("sender")
        if target:
            self._injected_responses[task_id] = envelope.result_text
            self._update_record(
                target, task_id, stage="injected", main_response=envelope.result_text
            )

    def _delivery_result(self, result: AsyncInterAgentResult, target: str) -> AsyncInterAgentResult:
        """Reuse a saved response only when its chunk offset belongs to that text."""
        if self._record_store is None:
            return result
        path = "(unresolved)"
        try:
            path = str(self._record_store.path_for(target, result.task_id))
            record = self._record_store.load(target, result.task_id)
        except Exception as exc:
            logger.error(  # noqa: TRY400
                "Async record read failed path=%s error=%s; duplicate chunk delivery is possible",
                path,
                type(exc).__name__,
            )
            self._repair_if_corrupt(target, result.task_id)
            response = self._injected_responses.get(result.task_id, "") or result.main_response
            logger.warning(
                "IA chunk progress reset task=%s reason=record unreadable; duplicate delivery possible",
                result.task_id,
            )
            return replace(result, main_response=response, delivered_chunks=0)
        transport = record["transport"]
        if not transport:
            stack = self._agents.get(target)
            configured = getattr(getattr(stack, "config", None), "transport", "")
            transport = {"telegram": "tg", "matrix": "mx", "slack": "sl"}.get(
                configured,
                "",
            )
        reason = progress_reset_reason(record, transport=transport)
        if reason:
            if (
                record["delivered_chunks"]
                or record["delivered_text_sha256"]
                or record["chunk_scheme"]
            ):
                logger.warning(
                    "IA chunk progress reset task=%s reason=%s; duplicate delivery possible",
                    result.task_id,
                    reason,
                )
                self._update_record(target, result.task_id, reset_progress=True)
            return replace(
                result,
                main_response=record["main_response"] if record["stage"] == "injected" else "",
                delivered_chunks=0,
            )
        return replace(
            result,
            main_response=record["main_response"],
            delivered_chunks=record["delivered_chunks"],
        )

    def _redelivery_state(
        self,
        task_id: str,
        record: dict[str, Any] | None = None,
    ) -> tuple[int, float | None]:
        stored = (
            (record["redelivery_attempts"], record["next_redelivery_at"])
            if record is not None
            else (0, None)
        )
        return merge_redelivery_state(
            stored,
            self._unpersisted_redelivery.get(task_id, (0, None)),
        )

    def _defer_redelivery(
        self,
        task_id: str,
        target: str,
        record: dict[str, Any] | None,
    ) -> None:
        attempts, _ = self._redelivery_state(task_id, record)
        attempts += 1
        max_exponent = (REDELIVERY_BACKOFF_MAX // REDELIVERY_BACKOFF_BASE).bit_length()
        delay = min(
            REDELIVERY_BACKOFF_BASE * 2 ** min(attempts - 1, max_exponent),
            REDELIVERY_BACKOFF_MAX,
        )
        next_at = time.time() + delay
        self._unpersisted_redelivery[task_id] = (attempts, next_at)
        logger.error(
            "Async result redelivery deferred task=%s target=%s attempts=%s next_redelivery_at=%s",
            task_id,
            target,
            attempts,
            next_at,
        )

    def _park_record(
        self, result: AsyncInterAgentResult, target: str, *, not_ready: bool = False
    ) -> None:
        self._parked_results[result.task_id] = result
        if self._record_store is None:
            return
        path = "(unresolved)"
        redelivery_state: tuple[int, float | None] | None = None
        try:
            path = str(self._record_store.path_for(target, result.task_id))
            try:
                record = self._record_store.load(target, result.task_id)
            except Exception:
                if not self._repair_if_corrupt(target, result.task_id):
                    if not not_ready:
                        self._defer_redelivery(result.task_id, target, None)
                    self._unpersisted_results[result.task_id] = result
                    return
                record = self._record_store.load(target, result.task_id)
            if not not_ready:
                self._defer_redelivery(result.task_id, target, record)
            redelivery_state = self._redelivery_state(result.task_id, record)
            if (
                record["result"] != asdict(result) or result.task_id in self._unpersisted_redelivery
            ) and not self._update_record(target, result.task_id, result=result):
                self._unpersisted_results[result.task_id] = result
                logger.error("Async result cannot be persisted before parking path=%s", path)
                return
            self._record_store.move_to_undelivered(target, result.task_id)
            self._record_store.load(target, result.task_id, area="undelivered")
            self._unpersisted_results.pop(result.task_id, None)
            self._parked_results.pop(result.task_id, None)
        except Exception as exc:
            # Metadata may already be on disk while its result still needs memory replay.
            if redelivery_state is not None:
                self._unpersisted_redelivery[result.task_id] = redelivery_state
            logger.error(  # noqa: TRY400
                "Async result preservation failed path=%s error=%s",
                path,
                type(exc).__name__,
            )
            return
        message = (
            "Async result parked before receiver ready task=%s target=%s chat_id=%s "
            "topic_id=%s transport=%s"
            if not_ready
            else "Async result undelivered task=%s target=%s chat_id=%s topic_id=%s transport=%s"
        )
        logger.log(
            logging.WARNING if not_ready else logging.ERROR,
            message,
            result.task_id,
            target,
            result.chat_id,
            result.topic_id,
            result.transport,
        )

    async def _attempt_delivery(
        self, result: AsyncInterAgentResult, target: str, *, retry: bool = False
    ) -> bool:
        """Re-read the current handler on every attempt."""
        stack = self._agents.get(target)
        handler = self._async_result_handlers.get(target)
        if (
            not self._delivery_ready(target)
            or handler is None
            or (
                self._record_store is not None and (stack is None or self.is_headless_agent(target))
            )
        ):
            return False
        try:
            delivery_result = replace(
                self._delivery_result(result, target),
                recipient_headless=self.is_headless_agent(result.recipient or result.sender),
            )
            if retry:
                delivery_result = replace(delivery_result, delivery_retry=True)
            outcome = await handler(delivery_result)
        except Exception as exc:
            logger.error(  # noqa: TRY400
                "Async result handler failed task=%s target=%s: %s",
                result.task_id,
                target,
                type(exc).__name__,
            )
            return False
        if isinstance(outcome, DeliveryOutcome) and not outcome.ok:
            self._update_record(
                target,
                result.task_id,
                delivered_chunks=outcome.delivered_chunks,
                delivered_text_sha256=outcome.delivered_text_sha256,
                chunk_scheme=outcome.chunk_scheme,
            )
        if not isinstance(outcome, DeliveryOutcome) or not outcome.ok:
            logger.error(
                "Async result delivery incomplete task=%s target=%s: %s",
                result.task_id,
                target,
                outcome.error if isinstance(outcome, DeliveryOutcome) else "no outcome",
            )
            return False
        self._delivered_results.add(result.task_id)
        if not self._update_record(target, result.task_id, stage="delivered"):
            logger.error(
                "Delivered async result retained unreadable record task=%s", result.task_id
            )
        self._cleanup_delivered_record(target, result.task_id)
        self._unpersisted_results.pop(result.task_id, None)
        self._unpersisted_redelivery.pop(result.task_id, None)
        self._parked_results.pop(result.task_id, None)
        logger.info("Async result delivered task=%s to '%s'", result.task_id, target)
        return True

    def _cleanup_delivered_record(self, target: str, task_id: str) -> None:
        """Clean acknowledged copies without ever delivering them again."""
        store = self._record_store
        if store is None:
            self._delivered_results.discard(task_id)
            return
        for area in ("inflight", "undelivered"):
            path = "(unresolved)"
            try:
                path = str(store.path_for(target, task_id, area=area))
                if not store.path_for(target, task_id, area=area).exists():
                    continue
                record = store.load(target, task_id, area=area)
                if area == "undelivered":
                    store.move_to_inflight(target, task_id)
                if record["stage"] != "delivered" and not self._update_record(
                    target,
                    task_id,
                    stage="delivered",
                ):
                    continue
                store.delete(target, task_id)
                logger.info("Async delivered record cleaned task=%s target=%s", task_id, target)
            except Exception as exc:
                logger.error(  # noqa: TRY400
                    "Async delivered record cleanup failed path=%s error=%s",
                    path,
                    type(exc).__name__,
                )
        try:
            if not any(
                store.path_for(target, task_id, area=area).exists()
                for area in ("inflight", "undelivered")
            ):
                self._delivered_results.discard(task_id)
        except (OSError, ValueError):
            pass

    async def _deliver_async_result(self, result: AsyncInterAgentResult) -> None:
        """Retry failed result delivery and preserve any unreported result."""
        if result.task_id in self._delivery_claims:
            logger.warning("Async result already being delivered task=%s", result.task_id)
            return
        self._delivery_claims.add(result.task_id)
        self._delivering_results[result.task_id] = result
        try:
            await self._deliver_async_result_active(result)
        finally:
            self._delivering_results.pop(result.task_id, None)
            self._injected_responses.pop(result.task_id, None)
            self._delivery_claims.discard(result.task_id)

    async def _deliver_async_result_active(self, result: AsyncInterAgentResult) -> None:
        target = result.reply_to or result.sender
        if self._failure_ledger is not None:
            if result.success:
                await self._failure_ledger.clear_success(target, result)
            else:
                result.failure_count = await self._failure_ledger.record_failure(target, result)
        self._update_record(target, result.task_id, result=result)
        if not self._delivery_ready(target) and not self.is_headless_agent(target):
            self._park_record(result, target, not_ready=True)
            return
        if await self._attempt_delivery(result, target):
            return
        if self._record_store is None:
            return
        for delay in self._retry_delays:
            self._retry_waiting[result.task_id] = result
            try:
                await asyncio.sleep(delay)
            finally:
                self._retry_waiting.pop(result.task_id, None)
            if not self._delivery_ready(target) and not self.is_headless_agent(target):
                self._park_record(result, target, not_ready=True)
                return
            if await self._attempt_delivery(result, target, retry=True):
                return
        self._park_record(result, target)

    async def redeliver_undelivered(  # noqa: C901, PLR0912, PLR0915
        self,
        target: str,
        *,
        exclude_task_ids: set[str] | None = None,
        respect_backoff: bool = False,
    ) -> int:
        """Replay valid parked results for a ready target; return delivered count."""
        store = self._record_store
        if store is None or not self._delivery_ready(target):
            return 0
        try:
            directory = store.path_for(target, "000000000000", area="undelivered").parent
            if directory.is_symlink():
                logger.error("Async parked scan unavailable target=%s error=symlink", target)
                return 0
        except (OSError, ValueError) as exc:
            logger.error(  # noqa: TRY400
                "Async parked scan unavailable target=%s error=%s",
                target,
                type(exc).__name__,
            )
            return 0
        candidates = sorted(directory.glob("*.json")) if directory.exists() else []

        delivered_count = 0
        seen: set[str] = set()
        attempted: set[str] = set()
        deferred: set[str] = set()
        for candidate in candidates:
            task_id = candidate.stem
            if (
                task_id in seen
                or task_id in self._delivery_claims
                or (exclude_task_ids is not None and task_id in exclude_task_ids)
            ):
                continue
            seen.add(task_id)
            if task_id in self._delivered_results:
                self._cleanup_delivered_record(target, task_id)
                continue
            try:
                record = store.load(target, task_id, area="undelivered")
            except (
                CorruptAsyncRecordError,
                json.JSONDecodeError,
                UnicodeDecodeError,
                TypeError,
                KeyError,
            ) as exc:
                memory_result = self._parked_results.get(task_id)
                if memory_result is not None and (
                    memory_result.task_id != task_id
                    or (memory_result.reply_to or memory_result.sender) != target
                ):
                    memory_result = None
                try:
                    if memory_result is None:
                        archived = store.quarantine_corrupt(target, task_id)
                    else:
                        task = AsyncInterAgentTask(
                            task_id=task_id,
                            sender=memory_result.sender,
                            recipient=memory_result.recipient,
                            message=memory_result.original_message,
                            chat_id=memory_result.chat_id,
                            topic_id=memory_result.topic_id,
                            transport=memory_result.transport,
                            reply_to=memory_result.reply_to,
                            result_origin=memory_result.result_origin,
                            broadcast_ref=memory_result.broadcast_ref,
                            lane=lane_from_session_name(memory_result.session_name),
                        )
                        archived = store.recover_corrupt(
                            task,
                            memory_result,
                            stage="injected" if memory_result.main_response.strip() else "received",
                            main_response=memory_result.main_response,
                            area="undelivered",
                        )
                        record = store.load(target, task_id, area="undelivered")
                except Exception as archive_exc:
                    logger.error(  # noqa: TRY400
                        "Async corrupt parked record recovery failed path=%s error=%s",
                        candidate,
                        type(archive_exc).__name__,
                    )
                    continue
                logger.error(  # noqa: TRY400
                    "Async corrupt parked record quarantined path=%s archive=%s error=%s",
                    candidate,
                    archived,
                    type(exc).__name__,
                )
                if memory_result is None:
                    continue
            except (OSError, ValueError) as exc:
                logger.error(  # noqa: TRY400
                    "Async parked record read failed path=%s error=%s",
                    candidate,
                    type(exc).__name__,
                )
                continue

            if record["stage"] == "delivered":
                self._delivered_results.add(task_id)
                self._cleanup_delivered_record(target, task_id)
                continue
            if respect_backoff:
                _, next_at = self._redelivery_state(task_id, record)
                if next_at is not None and next_at > time.time():
                    deferred.add(task_id)
                    continue
            memory_result = self._parked_results.get(task_id)
            if memory_result is not None:
                result = memory_result
            elif record["result"] is None:
                task = AsyncInterAgentTask(
                    task_id=task_id,
                    sender=record["sender"],
                    recipient=record["recipient"],
                    message=record["message"],
                    chat_id=record["chat_id"],
                    topic_id=record["topic_id"],
                    transport=record["transport"],
                    reply_to=record["reply_to"],
                    result_origin=record["result_origin"],
                    broadcast_ref=record["broadcast_ref"],
                    lane=record["lane"],
                )
                result = _make_result(
                    task,
                    record["created_at"],
                    success=False,
                    error="result lost: not persisted before restart",
                )
                result.failure_kind = "result_lost"
                logger.error(
                    "Async result absent after restart task=%s target=%s chat_id=%s topic_id=%s",
                    task_id,
                    target,
                    result.chat_id,
                    result.topic_id,
                )
            else:
                result = store.result_from_record(record)

            self._delivery_claims.add(task_id)
            try:
                store.move_to_inflight(target, task_id)
            except FileExistsError:
                self._delivery_claims.discard(task_id)
                logger.info("Async parked result already active task=%s target=%s", task_id, target)
                continue
            except (OSError, ValueError) as exc:
                self._delivery_claims.discard(task_id)
                logger.error(  # noqa: TRY400
                    "Async parked result claim failed path=%s error=%s",
                    candidate,
                    type(exc).__name__,
                )
                continue

            attempted.add(task_id)
            delivered = False
            self._delivering_results[task_id] = result
            try:
                if record["result"] is None:
                    saved = self._update_record(
                        target,
                        task_id,
                        result=result,
                        stage="received",
                        main_response="",
                        reset_progress=True,
                    )
                    if not saved and memory_result is None:
                        continue
                elif memory_result is not None:
                    # A live result is authoritative if the parked file is stale.
                    self._update_record(target, task_id, result=result)
                if await self._attempt_delivery(result, target):
                    delivered = True
                else:
                    for delay in self._retry_delays:
                        if not self._delivery_ready(target):
                            break
                        await asyncio.sleep(delay)
                        if await self._attempt_delivery(result, target, retry=True):
                            delivered = True
                            break
                if delivered:
                    delivered_count += 1
                else:
                    self._park_record(result, target, not_ready=not self._delivery_ready(target))
            finally:
                self._delivering_results.pop(task_id, None)
                self._injected_responses.pop(task_id, None)
                self._delivery_claims.discard(task_id)
                # A cancellation or storage error must not strand an undelivered
                # result in the active directory after this replay returns.
                if not delivered:
                    try:
                        if store.path_for(target, task_id).exists():
                            store.move_to_undelivered(target, task_id)
                    except (OSError, ValueError) as exc:
                        logger.error(  # noqa: TRY400
                            "Async parked result restore failed task=%s target=%s error=%s",
                            task_id,
                            target,
                            type(exc).__name__,
                        )
        # Parking may have failed before a valid undelivered file was written.
        # Keep the original in-memory result ahead of any synthetic loss report.
        for task_id, result in list(self._parked_results.items()):
            if (
                (result.reply_to or result.sender) != target
                or task_id in attempted
                or task_id in deferred
                or task_id in self._delivery_claims
                or (exclude_task_ids is not None and task_id in exclude_task_ids)
                or not self._delivery_ready(target)
            ):
                continue
            if respect_backoff:
                _, next_at = self._redelivery_state(task_id)
                if next_at is not None and next_at > time.time():
                    continue
            self._delivery_claims.add(task_id)
            self._delivering_results[task_id] = result
            try:
                delivered = await self._attempt_delivery(result, target)
                if not delivered:
                    for delay in self._retry_delays:
                        if not self._delivery_ready(target):
                            break
                        await asyncio.sleep(delay)
                        if await self._attempt_delivery(result, target, retry=True):
                            delivered = True
                            break
                if delivered:
                    delivered_count += 1
                else:
                    self._park_record(result, target, not_ready=not self._delivery_ready(target))
            finally:
                self._delivering_results.pop(task_id, None)
                self._injected_responses.pop(task_id, None)
                self._delivery_claims.discard(task_id)
        return delivered_count

    async def recover_inflight(  # noqa: C901, PLR0912, PLR0915
        self,
        target: str,
        *,
        newly_parked: set[str] | None = None,
        respect_backoff: bool = False,
    ) -> int:
        """Report work left in inflight after restart; replay completed results."""
        store = self._record_store
        if store is None or not self._delivery_ready(target):
            return 0
        try:
            directory = store.path_for(target, "000000000000").parent
            if directory.is_symlink():
                logger.error("Async inflight scan unavailable target=%s error=symlink", target)
                return 0
        except (OSError, ValueError) as exc:
            logger.error(  # noqa: TRY400
                "Async inflight scan unavailable target=%s error=%s",
                target,
                type(exc).__name__,
            )
            return 0

        delivered_count = 0
        for candidate in sorted(directory.glob("*.json")) if directory.exists() else []:
            if not self._delivery_ready(target):
                break
            task_id = candidate.stem
            live = self._async_tasks.get(task_id)
            if task_id in self._delivery_claims or (
                live is not None and live.asyncio_task is not None and not live.asyncio_task.done()
            ):
                continue
            if task_id in self._delivered_results:
                self._cleanup_delivered_record(target, task_id)
                continue
            self._delivery_claims.add(task_id)
            result: AsyncInterAgentResult | None = None
            delivered = False
            try:
                try:
                    record = store.load(target, task_id)
                except (
                    CorruptAsyncRecordError,
                    json.JSONDecodeError,
                    UnicodeDecodeError,
                    TypeError,
                    KeyError,
                ) as damage:
                    memory_result = self._unpersisted_results.get(
                        task_id
                    ) or self._parked_results.get(task_id)
                    if memory_result is not None and (
                        memory_result.task_id != task_id
                        or (memory_result.reply_to or memory_result.sender) != target
                    ):
                        memory_result = None
                    try:
                        if memory_result is None:
                            archived = store.quarantine_corrupt(
                                target,
                                task_id,
                                area="inflight",
                            )
                        else:
                            task = AsyncInterAgentTask(
                                task_id=task_id,
                                sender=memory_result.sender,
                                recipient=memory_result.recipient,
                                message=memory_result.original_message,
                                chat_id=memory_result.chat_id,
                                topic_id=memory_result.topic_id,
                                transport=memory_result.transport,
                                reply_to=memory_result.reply_to,
                                result_origin=memory_result.result_origin,
                                broadcast_ref=memory_result.broadcast_ref,
                                lane=lane_from_session_name(memory_result.session_name),
                            )
                            archived = store.recover_corrupt(
                                task,
                                memory_result,
                                stage=(
                                    "injected"
                                    if memory_result.main_response.strip()
                                    else "received"
                                ),
                                main_response=memory_result.main_response,
                            )
                            record = store.load(target, task_id)
                    except Exception as exc:
                        logger.error(  # noqa: TRY400
                            "Async inflight recovery failed path=%s error=%s",
                            candidate,
                            type(exc).__name__,
                        )
                        continue
                    logger.error(  # noqa: TRY400
                        "Async corrupt inflight record quarantined path=%s archive=%s error=%s",
                        candidate,
                        archived,
                        type(damage).__name__,
                    )
                    if memory_result is None:
                        continue
                except (OSError, ValueError) as exc:
                    logger.error(  # noqa: TRY400
                        "Async inflight read failed path=%s error=%s",
                        candidate,
                        type(exc).__name__,
                    )
                    continue

                if record["stage"] == "delivered":
                    self._delivered_results.add(task_id)
                    self._cleanup_delivered_record(target, task_id)
                    continue
                memory_result = self._unpersisted_results.get(task_id) or self._parked_results.get(
                    task_id
                )
                if memory_result is not None and (
                    memory_result.task_id == task_id
                    and (memory_result.reply_to or memory_result.sender) == target
                ):
                    # Actual results belong to parked replay, not restart interruption reports.
                    self._parked_results[task_id] = memory_result
                    state = self._redelivery_state(task_id, record)
                    if state != (0, None):
                        self._unpersisted_redelivery[task_id] = state
                    continue
                if respect_backoff and record["status"] in {"completed", "failed"}:
                    _, next_at = self._redelivery_state(task_id, record)
                    if next_at is not None and next_at > time.time():
                        continue
                if record["status"] in {"completed", "failed"}:
                    result = store.result_from_record(record)
                else:
                    task = AsyncInterAgentTask(
                        task_id=task_id,
                        sender=record["sender"],
                        recipient=record["recipient"],
                        message=record["message"],
                        chat_id=record["chat_id"],
                        topic_id=record["topic_id"],
                        transport=record["transport"],
                        reply_to=record["reply_to"],
                        result_origin=record["result_origin"],
                        broadcast_ref=record["broadcast_ref"],
                        lane=record["lane"],
                    )
                    result = _make_result(
                        task,
                        record["created_at"],
                        success=False,
                        error="interrupted by restart",
                    )
                    if self._failure_ledger is not None:
                        result.failure_count = await self._failure_ledger.record_failure(
                            target,
                            result,
                        )
                self._delivering_results[task_id] = result
                if record["status"] == "inflight" and not self._update_record(
                    target,
                    task_id,
                    result=result,
                    stage="received",
                    main_response="",
                    reset_progress=True,
                ):
                    continue
                if await self._attempt_delivery(result, target):
                    delivered = True
                else:
                    for delay in self._retry_delays:
                        if not self._delivery_ready(target):
                            break
                        await asyncio.sleep(delay)
                        if await self._attempt_delivery(result, target, retry=True):
                            delivered = True
                            break
                if delivered:
                    delivered_count += 1
            finally:
                if result is not None and not delivered:
                    try:
                        if store.path_for(target, task_id).exists():
                            if newly_parked is not None:
                                newly_parked.add(task_id)
                            self._park_record(
                                result,
                                target,
                                not_ready=not self._delivery_ready(target),
                            )
                    except (OSError, ValueError) as exc:
                        logger.error(  # noqa: TRY400
                            "Async inflight result parking failed task=%s target=%s error=%s",
                            task_id,
                            target,
                            type(exc).__name__,
                        )
                self._delivering_results.pop(task_id, None)
                self._injected_responses.pop(task_id, None)
                self._delivery_claims.discard(task_id)
        return delivered_count

    async def cancel_all_async(self) -> int:
        """Cancel all in-flight async tasks. Returns the number cancelled."""
        tasks = list(self._async_tasks.values())
        cancelled = 0
        for task in tasks:
            if task.asyncio_task and not task.asyncio_task.done():
                waiting_result = self._retry_waiting.get(task.task_id)
                if waiting_result is not None:
                    self._park_record(
                        waiting_result, waiting_result.reply_to or waiting_result.sender
                    )
                task.asyncio_task.cancel()
                cancelled += 1
                logger.warning(
                    "Cancelled in-flight async task=%s (%s -> %s)",
                    task.task_id,
                    task.sender,
                    task.recipient,
                )
        self._async_tasks.clear()
        return cancelled
