# bus/

Unified result-delivery layer for observers, async inter-agent responses, and task callbacks.

## Files

- `bus/envelope.py`: `Envelope`, `DeliveryOutcome`, `Origin`, `DeliveryMode`, `LockMode`
- `bus/bus.py`: `MessageBus` coordinator (`submit`, lock/inject/deliver pipeline)
- `bus/lock_pool.py`: shared per-session lock pool (`(chat_id, topic_id)`)
- `bus/adapters.py`: conversion helpers from domain results to `Envelope`
- `messenger/telegram/transport.py`: Telegram transport adapter + origin-specific formatting
- `messenger/matrix/transport.py`: Matrix transport adapter for room delivery formatting/routing

## Why this module exists

Before the refactor, delivery logic was split across multiple `deliver_*` paths and lock dicts.
`bus/` centralizes delivery into one pipeline:

1. convert result -> `Envelope`
2. optionally acquire shared session lock
3. optionally inject prompt into active session
4. deliver through registered transport(s)

## `Envelope` model

Core fields:

- identity: `origin`, `chat_id`, `topic_id`, `transport` (default `"tg"`)
- input for injection: `prompt`, `prompt_preview`
- output for delivery: `result_text`, `status`, `is_error`
- routing flags: `delivery`, `lock_mode`, `needs_injection`, `injected`
- acknowledgement/retry: `delivered`, `delivery_error`, `delivered_chunks`, `start_chunk`, `delivered_text_sha256`, `chunk_scheme`, `broadcast_targets`, `failed_transports`
- telegram metadata: `reply_to_message_id`, `thread_id`
- context: `provider`, `model`, `session_name`, `session_id`, `metadata`

Lock key: `envelope.lock_key -> (chat_id, topic_id)`.

## `MessageBus` flow

`submit(envelope)`:

1. assign `envelope_id` when missing
2. if `lock_mode=REQUIRED`: lock via shared `LockPool`
3. wake heartbeat state for task result/question and inter-agent origins
4. if `needs_injection`: call `SessionInjector.inject_prompt(...)` (orchestrator)
5. after a non-empty inter-agent injection, invoke the injected-response persistence callback
6. route delivery and return `DeliveryOutcome`

Inter-agent injection failure, missing injector or empty injection response
prevents raw-result delivery. `DeliveryOutcome.ok` requires both injection and
delivery; cron acknowledgement uses `envelope.delivered` separately. Injection
persistence exceptions are logged; this is not an exactly-once guarantee.

Messenger adapters include `TelegramTransport`, `MatrixTransport`, and
`SlackTransport`; headless bots have no messenger adapter.

## Typing during result injection

During `SessionInjector.inject_prompt`, the matching envelope transport may provide
an optional `injection_typing(envelope)` async context manager. Telegram reuses
`TypingContext` for the envelope chat and topic; Matrix reuses `MatrixTypingContext`
when the chat maps to a known room. Typing ends before result acceptance and delivery.
Factory, context-entry, sending and context-exit failures do not prevent injection
or delivery. Injection errors and cancellation retain their existing behavior.

The delivery protocol still requires only `transport_name`, `deliver` and
`deliver_broadcast`; older adapters need no changes. Slack and headless remain no-op.
Private task-question answers call the orchestrator directly, outside this bus, so
they never show typing or deliver a public question or answer through this feature.

## Transport-aware delivery

Each `TransportAdapter` exposes a `transport_name` property (e.g. `"tg"`, `"mx"`). The `Envelope.transport` field identifies the target transport for routing.

Delivery modes:

- **BROADCAST**: delivered to all registered transports via `deliver_broadcast()` unless `broadcast_targets` restricts a retry to selected transports. Failures are collected in `failed_transports`.
- **UNICAST**: filtered by `envelope.transport` — only the matching transport receives the envelope via `deliver()`. When `envelope.transport` is unset, all transports receive the envelope (backward compatibility).

Cascading fallback for UNICAST:

- For non-inter-agent origins, an unregistered target transport can fall back to the first available transport. Failures in an already registered target do not trigger this cross-transport fallback.
- Inter-agent results never use that fallback: unavailable recorded transport produces a delivery failure for durable retry by the inter-agent layer.
- The fallback envelope is rewritten as a BROADCAST with a "Delivery fallback" explanation prepended to the result text, so the user understands the message was intended for a different transport.

## Adapter mapping (`adapters.py`)

- `from_background_result(...)`
- `from_cron_result(...)`
- `from_heartbeat(...)`
- `from_webhook_cron_result(...)`
- `from_webhook_wake(...)`
- `from_interagent_result(...)`
- `from_task_result(...)`
- `from_task_question(...)`
- `from_user_message(...)` (audit-only envelope)

Task/topic nuance:

- task result envelopes map `thread_id -> topic_id`
- task question envelopes also carry `topic_id`
- injected responses route back into the originating chat/topic session (including Telegram private-chat topics)

## Wiring

- Single-transport mode: the active bot creates `MessageBus(lock_pool=self._lock_pool)` and registers its transport
- Multi-transport mode: `MultiBotAdapter` creates one shared `MessageBus` and `LockPool`; Telegram, Matrix and Slack constructors preserve the supplied pool even while it is empty, creating a pool only when none is supplied. Each bot registers its own transport adapter.
- `run_startup()` calls `orch.wire_observers_to_bus(bot._bus, wake_handler=...)`
- `ObserverManager.wire_to_bus(...)` connects cron/heartbeat/background/webhook callbacks in one call
- `bus.set_injector(orchestrator)` enables prompt injection paths

## Locking model

A single `LockPool` is shared by:

- `SequentialMiddleware` (Telegram ingress)
- `MessageBus` (observer/result routing)

`Orchestrator` receives that pool during primary Telegram/Matrix/Slack/headless startup, before starting the optional API. `ApiServer` uses the passed pool for WebSocket user turns; active-session delegation questions also acquire it. Their 60s wait falls back to the separate source-header Named Session. A standalone `ApiServer` without a supplied pool still creates its own. Lock keys remain `(chat_id, topic_id)`; the API retains legacy `tg` session storage while CLI origin is `api`. Sources: [delegation questions](multiagent.md#questions-during-delegation).
