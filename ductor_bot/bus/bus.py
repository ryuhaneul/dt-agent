"""Central message bus: intake, lock, inject, deliver."""

from __future__ import annotations

import hashlib
import logging
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from inspect import getattr_static
from typing import Any, Protocol, runtime_checkable

from ductor_bot.bus.envelope import DeliveryMode, DeliveryOutcome, Envelope, LockMode, Origin
from ductor_bot.bus.lock_pool import LockPool
from ductor_bot.i18n import t
from ductor_bot.infra.input_context import input_lock, received_at
from ductor_bot.text.response_format import show_run_result

logger = logging.getLogger(__name__)


@runtime_checkable
class TransportAdapter(Protocol):
    """Protocol for transport-specific delivery.

    Adapters may also provide ``injection_typing(envelope)`` returning an async
    context manager. This optional capability is not a required protocol member.
    """

    @property
    def transport_name(self) -> str:
        """Short identifier for this transport (e.g. ``"tg"``, ``"mx"``)."""
        ...

    async def deliver(self, envelope: Envelope) -> None:
        """Send the envelope's result to the targeted chat."""
        ...

    async def deliver_broadcast(self, envelope: Envelope) -> None:
        """Send the envelope's result to all allowed users."""
        ...


@runtime_checkable
class SessionInjector(Protocol):
    """Protocol for injecting a prompt into the active CLI session.

    Implemented by the Orchestrator.  The bus calls this when
    ``envelope.needs_injection`` is True.
    """

    async def inject_prompt(  # noqa: PLR0913
        self,
        prompt: str,
        chat_id: int,
        label: str,
        *,
        topic_id: int | None = None,
        transport: str = "tg",
        isolated: bool = False,
        result_origin: str = "",
        broadcast_ref: str = "",
        counts_as_activity: bool = True,
    ) -> str:
        """Execute *prompt* in the active session. Returns response text."""
        ...


def _injection_label(envelope: Envelope) -> str:
    """Process label of an injected turn: origin, the job id when there is one, envelope id."""
    parts = [
        envelope.origin.value,
        str(envelope.metadata.get("job_id") or ""),
        envelope.envelope_id,
    ]
    return ":".join(p for p in parts if p)


def _activity_kwargs(envelope: Envelope) -> dict[str, bool]:
    """``counts_as_activity=False`` for an injected turn the user did not cause (cron jobs)."""
    if envelope.metadata.get("counts_as_activity") is False:
        return {"counts_as_activity": False}
    return {}


class MessageBus:
    """Central coordinator for all background message routing.

    Usage::

        bus = MessageBus()
        bus.register_transport(TelegramTransport(bot))
        bus.set_injector(orchestrator)

        # Any observer callback:
        await bus.submit(from_cron_result(title, text, status))
    """

    def __init__(self, lock_pool: LockPool | None = None) -> None:
        self._locks = lock_pool if lock_pool is not None else LockPool()
        self._transports: list[TransportAdapter] = []
        self._injector: SessionInjector | None = None
        self._wake_hook: Callable[[Envelope], Awaitable[None]] | None = None
        self._on_injected: Callable[[Envelope], Awaitable[None]] | None = None
        self._delivery_observer: Callable[[Envelope], Awaitable[None]] | None = None
        # Process-local only: after a restart the same failure can be announced once again.
        self._injection_notices: dict[str, Envelope] = {}
        self._noticed_errors: set[tuple[str, str]] = set()

    @property
    def lock_pool(self) -> LockPool:
        """The shared lock pool."""
        return self._locks

    def register_transport(self, transport: TransportAdapter) -> None:
        """Add a transport adapter for delivery."""
        self._transports.append(transport)

    def set_injector(self, injector: SessionInjector) -> None:
        """Set the session injector (typically the Orchestrator)."""
        self._injector = injector

    def set_wake_hook(self, hook: Callable[[Envelope], Awaitable[None]]) -> None:
        """Set the callback that wakes heartbeat checks for delegation results."""
        self._wake_hook = hook

    def set_on_injected(self, callback: Callable[[Envelope], Awaitable[None]]) -> None:
        """Persist an injected inter-agent response before transport delivery."""
        self._on_injected = callback

    def set_delivery_observer(
        self,
        callback: Callable[[Envelope], Awaitable[None]] | None,
    ) -> None:
        """Observe each submitted envelope after processing and lock release."""
        self._delivery_observer = callback

    async def submit(self, envelope: Envelope) -> DeliveryOutcome:
        """Route an envelope: assign ID, acquire lock, inject, deliver."""
        timed = envelope.origin in {Origin.INTERAGENT, Origin.TASK_RESULT, Origin.TASK_QUESTION}
        with received_at(envelope.created_at, clear=not timed):
            return await self._submit(envelope)

    async def _submit(self, envelope: Envelope) -> DeliveryOutcome:
        if not envelope.envelope_id:
            envelope.envelope_id = secrets.token_hex(6)

        logger.debug(
            "Bus submit: origin=%s chat=%d delivery=%s lock=%s inject=%s",
            envelope.origin.value,
            envelope.chat_id,
            envelope.delivery.value,
            envelope.lock_mode.value,
            envelope.needs_injection,
        )

        if envelope.lock_mode == LockMode.REQUIRED:
            lock = self._locks.get(envelope.lock_key)
            async with input_lock(lock):
                await self._process(envelope)
        else:
            await self._process(envelope)

        if envelope.injection_error_text:
            await self._injection_notice(
                envelope, envelope.injection_error_text, envelope.injection_error_kind
            )
        await self._observe_delivery(envelope)

        injected = envelope.injected
        delivered = envelope.delivered
        error = envelope.delivery_error
        if (
            not injected
            and (envelope.needs_injection or envelope.origin == Origin.INTERAGENT)
            and not error
        ):
            error = "injection not completed"
        elif not delivered and not error:
            error = "delivery not completed"
        delivery_hash = envelope.delivered_text_sha256
        if envelope.origin == Origin.INTERAGENT and not delivery_hash and envelope.result_text:
            delivery_hash = hashlib.sha256(envelope.result_text.encode("utf-8")).hexdigest()
        return DeliveryOutcome(
            ok=injected and delivered,
            injected=injected,
            delivered=delivered,
            error=error,
            injection_error_kind=envelope.injection_error_kind,
            injection_error_text=envelope.injection_error_text,
            delivered_chunks=envelope.delivered_chunks,
            delivered_text_sha256=delivery_hash,
            chunk_scheme=envelope.chunk_scheme,
        )

    async def _observe_delivery(self, envelope: Envelope) -> None:
        if self._delivery_observer is not None:
            try:
                await self._delivery_observer(envelope)
            except Exception:
                logger.exception("Delivery observer failed")

    async def _injection_notice(self, envelope: Envelope, text: str, kind: str) -> None:
        """Send a separate notice, leaving the original result and retry state untouched."""
        task_id = envelope.metadata.get("ia_task_id")
        if not task_id or (task_id, kind) in self._noticed_errors:
            return
        # Claim before sending: a transport failure must not start another retry loop.
        self._noticed_errors.add((task_id, kind))
        notice = replace(
            envelope,
            prompt="",
            prompt_preview="",
            result_text=text,
            status="error",
            is_error=False,  # Use the IA raw-text path, not the failed-worker summary.
            needs_injection=False,
            injected=True,
            delivered=True,
            delivery_error="",
            injection_error_kind="",
            injection_error_text="",
            delivered_chunks=0,
            start_chunk=0,
            delivered_text_sha256="",
            chunk_scheme="",
            failed_transports=[],
            session_name="",
            envelope_id=secrets.token_hex(6),
            metadata={"ia_task_id": task_id},
        )
        if kind != "undelivered":
            # No prompt, task result, or original request is retained in this notice.
            self._injection_notices[task_id] = replace(notice, result_text="")
        await self._deliver(notice)
        await self._observe_delivery(notice)
        if not notice.delivered:
            logger.warning(
                "Inter-agent failure notice not delivered task=%s kind=%s", task_id, kind
            )

    async def notify_interagent_undelivered(self, task_id: str) -> None:
        """Announce a preserved result only if its injection failed with a CLI response."""
        notice = self._injection_notices.get(task_id)
        if notice is not None:
            await self._injection_notice(notice, t("notice.ia_result_preserved"), "undelivered")

    @asynccontextmanager
    async def _injection_typing(self, envelope: Envelope) -> AsyncIterator[None]:
        """Keep optional typing failures separate from injection and delivery."""
        context = None
        try:
            for adapter in self._transports:
                if adapter.transport_name != envelope.transport:
                    continue
                # Static lookup avoids fabricating a capability on dynamic mock adapters.
                if getattr_static(adapter, "injection_typing", None) is not None:
                    factory = getattr(adapter, "injection_typing", None)
                    if callable(factory):
                        candidate = factory(envelope)
                        await candidate.__aenter__()
                        context = candidate
                break
        except Exception:
            logger.debug("Injection typing could not start", exc_info=True)
        try:
            yield
        finally:
            if context is not None:
                try:
                    await context.__aexit__(None, None, None)
                except Exception:
                    logger.debug("Injection typing could not stop", exc_info=True)

    async def _process(self, envelope: Envelope) -> None:
        """Wake, inject if needed, then deliver."""
        if self._wake_hook is not None and envelope.origin in {
            Origin.TASK_RESULT,
            Origin.TASK_QUESTION,
            Origin.INTERAGENT,
        }:
            try:
                await self._wake_hook(envelope)
            except Exception:
                logger.exception(
                    "Heartbeat wake failed: origin=%s chat=%d",
                    envelope.origin.value,
                    envelope.chat_id,
                )

        if envelope.needs_injection and self._injector and envelope.prompt:
            label = _injection_label(envelope)
            try:
                kwargs: dict[str, Any] = {
                    "topic_id": envelope.topic_id,
                    "transport": envelope.transport,
                }
                kwargs.update(_activity_kwargs(envelope))
                if envelope.metadata.get("isolated"):
                    kwargs.update(
                        isolated=True,
                        result_origin=envelope.metadata.get("result_origin", ""),
                        broadcast_ref=envelope.metadata.get("broadcast_ref", ""),
                    )
                async with self._injection_typing(envelope):
                    response = await self._injector.inject_prompt(
                        envelope.prompt,
                        envelope.chat_id,
                        label,
                        **kwargs,
                    )
                if not await self._accept_injection(envelope, response):
                    return
            except Exception as exc:
                logger.exception(
                    "Injection failed: origin=%s chat=%d",
                    envelope.origin.value,
                    envelope.chat_id,
                )
                if envelope.origin == Origin.INTERAGENT:
                    self._capture_injection_failure(envelope, exc)
                    envelope.is_error = True
                    envelope.result_text = ""
                    envelope.injected = False
                    envelope.delivered = False
                    envelope.delivery_error = "injection failed"
                    return
                envelope.is_error = True
                if not envelope.result_text:
                    envelope.result_text = t(
                        "notice.processing_error", origin=envelope.origin.value
                    )

        if (
            envelope.origin == Origin.INTERAGENT
            and envelope.needs_injection
            and not envelope.injected
        ):
            envelope.result_text = ""
            envelope.delivered = False
            envelope.delivery_error = "injection unavailable"
            return

        await self._deliver(envelope)

    @staticmethod
    def _capture_injection_failure(envelope: Envelope, exc: Exception) -> None:
        from ductor_bot.orchestrator.injection import InjectionIncomplete

        if isinstance(exc, InjectionIncomplete):
            envelope.injection_error_text = exc.notice_text
            envelope.injection_error_kind = exc.error_kind

    async def _accept_injection(self, envelope: Envelope, response: str) -> bool:
        """Take the injected turn's reply as the envelope's result; ``False``: stop (IA failure).

        An empty reply fails an inter-agent result. For a cron result it is no reply either:
        the job's own result stays on the envelope and is delivered instead.
        """
        empty = not response.strip()
        if envelope.origin == Origin.INTERAGENT and empty:
            envelope.result_text = ""
            envelope.injected = False
            envelope.delivered = False
            envelope.delivery_error = "empty injection response"
            return False
        if envelope.origin == Origin.CRON and empty:
            logger.warning(
                "Empty injection response for cron chat=%d; sending its result", envelope.chat_id
            )
            return True
        envelope.result_text = response
        envelope.injected = True
        if envelope.origin == Origin.INTERAGENT and self._on_injected is not None:
            try:
                await self._on_injected(envelope)
            except Exception:
                logger.exception("Failed to persist injected inter-agent response")
        return True

    async def _deliver(self, envelope: Envelope) -> None:  # noqa: C901,PLR0912,PLR0915
        """Route to the correct transport(s) with cascading fallback."""
        if not self._transports:
            logger.warning(
                "No transports registered — envelope lost: origin=%s chat=%d",
                envelope.origin.value,
                envelope.chat_id,
            )
            envelope.delivered = False
            if envelope.delivery == DeliveryMode.BROADCAST and envelope.broadcast_targets:
                envelope.failed_transports = list(dict.fromkeys(envelope.broadcast_targets))
                envelope.delivery_error = "broadcast failed: " + ", ".join(
                    envelope.failed_transports
                )
            else:
                envelope.delivery_error = "no transports registered"
            return

        # A retry may target only the transports that failed previously.
        if envelope.delivery == DeliveryMode.BROADCAST:
            requested = set(envelope.broadcast_targets or [])
            available = {transport.transport_name for transport in self._transports}
            envelope.failed_transports.clear()
            for transport in self._transports:
                if requested and transport.transport_name not in requested:
                    continue
                envelope.delivered = True
                envelope.delivery_error = ""
                try:
                    await transport.deliver_broadcast(envelope)
                except Exception:
                    logger.exception("Broadcast delivery failed: %s", transport.transport_name)
                    envelope.delivered = False
                if (
                    not envelope.delivered
                    and transport.transport_name not in envelope.failed_transports
                ):
                    envelope.failed_transports.append(transport.transport_name)
            envelope.failed_transports.extend(sorted(requested - available))
            envelope.delivered = not envelope.failed_transports
            envelope.delivery_error = (
                "broadcast failed: " + ", ".join(envelope.failed_transports)
                if envelope.failed_transports
                else ""
            )
            return

        # UNICAST: filter by transport name.
        target_transport = envelope.transport
        if target_transport:
            matching = [t for t in self._transports if t.transport_name == target_transport]
            others = [t for t in self._transports if t.transport_name != target_transport]
        else:
            # No transport specified -> deliver to all (backward compat).
            matching = list(self._transports)
            others = []

        for transport in matching:
            try:
                await transport.deliver(envelope)
            except Exception as exc:
                logger.exception(
                    "Transport delivery failed: origin=%s transport=%s",
                    envelope.origin.value,
                    type(transport).__name__,
                )
                envelope.delivered = False
                envelope.delivery_error = type(exc).__name__

        # Cascading fallback: target transport not registered at all.
        if not matching and others:
            envelope.delivered = False
            envelope.delivery_error = f"transport '{target_transport}' unavailable"
            if envelope.origin == Origin.INTERAGENT:
                logger.error(
                    "Inter-agent transport '%s' unavailable; result not rerouted",
                    target_transport,
                )
                return
            logger.warning(
                "Transport '%s' not available for envelope origin=%s, falling back to %s",
                target_transport,
                envelope.origin.value,
                others[0].transport_name,
            )
            fallback_env = Envelope(
                origin=envelope.origin,
                chat_id=0,
                result_text=t(
                    "notice.delivery_fallback",
                    transport=target_transport,
                    text=show_run_result(envelope.status, envelope.result_text or ""),
                ),
                status=envelope.status,
                delivery=DeliveryMode.BROADCAST,
                lock_mode=envelope.lock_mode,
                metadata=envelope.metadata,
            )
            try:
                await others[0].deliver_broadcast(fallback_env)
            except Exception:
                logger.exception("Fallback delivery also failed")
