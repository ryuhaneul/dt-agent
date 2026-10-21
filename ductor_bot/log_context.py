"""Logging context: ContextVar-based log enrichment for async operations.

Every log record is automatically enriched with a ``[op:chat:sid]`` prefix
via a `ContextFilter` attached to the root logger handlers.

Operation codes: ``msg`` (user message), ``cb`` (callback query),
``cron`` (cron job), ``hb`` (heartbeat), ``wh`` (webhook).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

# Cross-cutting context propagated through asyncio tasks.
ctx_agent_name: ContextVar[str | None] = ContextVar("ctx_agent_name", default=None)
ctx_chat_id: ContextVar[int | None] = ContextVar("ctx_chat_id", default=None)
ctx_topic: ContextVar[str | None] = ContextVar("ctx_topic", default=None)
ctx_session_id: ContextVar[str | None] = ContextVar("ctx_session_id", default=None)
ctx_operation: ContextVar[str | None] = ContextVar("ctx_operation", default=None)


class ContextFilter(logging.Filter):
    """Inject ContextVar values into every LogRecord as ``record.ctx``."""

    def filter(self, record: logging.LogRecord) -> bool:
        agent = ctx_agent_name.get(None)
        chat = ctx_chat_id.get(None)
        topic = ctx_topic.get(None)
        sid = ctx_session_id.get(None)
        op = ctx_operation.get(None)
        parts: list[str] = []
        if agent:
            parts.append(agent)
        if op:
            parts.append(op)
        if chat is not None:
            parts.append(str(chat))
        if topic:
            parts.append(topic)
        if sid:
            parts.append(sid[:8])
        record.ctx = f"[{':'.join(parts)}] " if parts else ""
        return True


def set_log_context(
    *,
    agent_name: str | None = None,
    operation: str | None = None,
    chat_id: int | None = None,
    topic: str | None = None,
    session_id: str | None = None,
) -> None:
    """Set logging context for the current asyncio task.

    Values propagate to all coroutines called within the same task.
    Each ``asyncio.create_task()`` copies the current context automatically.
    """
    if agent_name is not None:
        ctx_agent_name.set(agent_name)
    if operation is not None:
        ctx_operation.set(operation)
    if chat_id is not None:
        ctx_chat_id.set(chat_id)
    if topic is not None:
        ctx_topic.set(topic)
    if session_id is not None:
        ctx_session_id.set(session_id)


@contextmanager
def scoped_log_context(*, clear_session: bool = False) -> Iterator[None]:
    """Run a block with its own log context and restore the original one afterwards.

    ``clear_session`` hides the session id of whatever ran before in this task (the block sets
    its own once the session is known). Everything the block sets is dropped at the end.
    """
    agent, chat, topic = ctx_agent_name.get(None), ctx_chat_id.get(None), ctx_topic.get(None)
    session, operation = ctx_session_id.get(None), ctx_operation.get(None)
    if clear_session:
        ctx_session_id.set(None)
    try:
        yield
    finally:
        ctx_agent_name.set(agent)
        ctx_chat_id.set(chat)
        ctx_topic.set(topic)
        ctx_session_id.set(session)
        ctx_operation.set(operation)
