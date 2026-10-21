"""Data models for the background task system."""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass, field
from typing import Any

# #79: allowed priority levels for background tasks. The scheduler gives
# ``interactive`` a concurrency-cap bypass so user-facing follow-ups stay
# responsive while heavy ``batch`` work runs. ``background`` is the default
# and behaves exactly as before. Unknown values coerce to ``background``.
TASK_PRIORITIES: tuple[str, ...] = ("interactive", "background", "batch")
_DEFAULT_PRIORITY: str = "background"


def normalise_priority(value: str | None) -> str:
    """Return a valid priority or the default (``background``)."""
    if value and value in TASK_PRIORITIES:
        return value
    return _DEFAULT_PRIORITY


def normalise_origin_transport(value: str) -> str:
    """Normalize provenance aliases without changing API transport identity."""
    return {"telegram": "tg", "matrix": "mx"}.get(value, value)


def normalise_origin_kind(value: str) -> str:
    """Keep absent provenance distinct from an unrecognized session kind."""
    if not value or value in {"foreground", "named", "background"}:
        return value
    return "unknown"


@dataclass(slots=True)
class TaskSubmit:
    """Input for creating a background task."""

    chat_id: int
    prompt: str
    message_id: int
    thread_id: int | None
    parent_agent: str
    name: str = ""
    provider_override: str = ""
    model_override: str = ""
    thinking_override: str = ""
    reasoning_effort_override: str = ""
    priority: str = _DEFAULT_PRIORITY
    origin_transport: str = ""
    origin_session_kind: str = ""
    origin_session_id: str = ""


def _count(value: object) -> int:
    """A stored counter; anything but a non-negative integer (an old file) counts as 0."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


@dataclass(slots=True)
class TaskEntry:
    """Persisted task metadata."""

    task_id: str
    chat_id: int
    parent_agent: str
    name: str
    prompt_preview: str
    provider: str
    model: str
    status: str  # "running" | "done" | "failed" | "cancelled" | "waiting"
    session_id: str = ""
    created_at: float = field(default_factory=time.time)
    completed_at: float = 0.0
    elapsed_seconds: float = 0.0
    error: str = ""
    result_preview: str = ""
    question_count: int = 0
    num_turns: int = 0
    last_question: str = ""
    original_prompt: str = ""
    thinking: str = ""
    reasoning_effort: str = ""
    tasks_dir: str = ""  # Agent's tasks directory (for per-agent folder resolution)
    thread_id: int | None = None  # Forum topic ID (for routing results back to topic)
    priority: str = _DEFAULT_PRIORITY  # #79: interactive | background | batch
    last_question_received_at: float = 0.0  # Unknown in legacy registries.
    instruction_received_at: float = 0.0  # Latest accepted submit/resume receipt.
    instruction_waited_seconds: float | None = None
    # A restart cut the task off and its parent has not been told yet (see TaskHub).
    notify_pending: bool = False
    notify_attempts: int = 0
    origin_transport: str = ""
    origin_session_kind: str = ""
    origin_session_id: str = ""

    def to_dict(self) -> dict[str, object]:
        d: dict[str, object] = {
            "task_id": self.task_id,
            "chat_id": self.chat_id,
            "parent_agent": self.parent_agent,
            "name": self.name,
            "prompt_preview": self.prompt_preview,
            # #90: original_prompt MUST round-trip so that a restart mid-task
            # preserves the full prompt for resume/retry and for the parent
            # agent's "Original task: <prompt>" injection. prompt_preview
            # (80 chars) alone is too short.
            "original_prompt": self.original_prompt,
            "provider": self.provider,
            "model": self.model,
            "status": self.status,
            "session_id": self.session_id,
            "origin_transport": self.origin_transport,
            "origin_session_kind": self.origin_session_kind,
            "origin_session_id": self.origin_session_id,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "elapsed_seconds": self.elapsed_seconds,
            "error": self.error,
            "result_preview": self.result_preview,
            "question_count": self.question_count,
            "num_turns": self.num_turns,
            "last_question": self.last_question,
            "last_question_received_at": self.last_question_received_at,
            "instruction_received_at": self.instruction_received_at,
            "instruction_waited_seconds": self.instruction_waited_seconds,
            "thinking": self.thinking,
            "reasoning_effort": self.reasoning_effort,
            "tasks_dir": self.tasks_dir,
            "priority": self.priority,
            "notify_pending": self.notify_pending,
            "notify_attempts": self.notify_attempts,
        }
        if self.thread_id is not None:
            d["thread_id"] = self.thread_id
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TaskEntry:
        question_received_at = d.get("last_question_received_at", 0.0)
        if (
            type(question_received_at) not in (int, float)
            or not math.isfinite(question_received_at)
            or not 0 <= question_received_at <= 253402300799
        ):
            question_received_at = 0.0
        instruction_received_at = d.get("instruction_received_at", 0.0)
        if (
            type(instruction_received_at) not in (int, float)
            or not math.isfinite(instruction_received_at)
            or not 0 <= instruction_received_at <= 253402300799
        ):
            instruction_received_at = 0.0
        instruction_waited_seconds: Any = d.get("instruction_waited_seconds")
        if (
            type(instruction_waited_seconds) not in (int, float)
            or not math.isfinite(instruction_waited_seconds)
            or instruction_waited_seconds < 0
        ):
            instruction_waited_seconds = None
        return cls(
            task_id=d["task_id"],
            chat_id=d["chat_id"],
            parent_agent=d.get("parent_agent", "main"),
            name=d.get("name", ""),
            prompt_preview=d.get("prompt_preview", ""),
            provider=d.get("provider", ""),
            model=d.get("model", ""),
            status=d.get("status", "running"),
            session_id=d.get("session_id", ""),
            origin_transport=d.get("origin_transport", ""),
            origin_session_kind=d.get("origin_session_kind", ""),
            origin_session_id=d.get("origin_session_id", ""),
            created_at=d.get("created_at", 0.0),
            completed_at=d.get("completed_at", 0.0),
            elapsed_seconds=d.get("elapsed_seconds", 0.0),
            error=d.get("error", ""),
            result_preview=d.get("result_preview", ""),
            question_count=d.get("question_count", 0),
            num_turns=d.get("num_turns", 0),
            last_question=d.get("last_question", ""),
            last_question_received_at=question_received_at,
            instruction_received_at=instruction_received_at,
            instruction_waited_seconds=instruction_waited_seconds,
            # #90: .get(..., "") default preserves backward-compat for tasks.json
            # files written before the original_prompt field was persisted.
            original_prompt=d.get("original_prompt", ""),
            thinking=d.get("thinking", ""),
            reasoning_effort=d.get("reasoning_effort", ""),
            tasks_dir=d.get("tasks_dir", ""),
            thread_id=d.get("thread_id"),
            notify_pending=d.get("notify_pending") is True,
            notify_attempts=_count(d.get("notify_attempts")),
            priority=normalise_priority(d.get("priority")),
        )


@dataclass(slots=True)
class TaskInFlight:
    """In-memory tracking for a running task."""

    entry: TaskEntry
    asyncio_task: asyncio.Task[None] | None = field(default=None, repr=False)
    has_pending_question: bool = False
    run_seq: int = 0
    status_final: bool = False
    cancel_requested: bool = False


@dataclass(slots=True)
class TaskResult:
    """Outcome delivered to parent agent after task completion."""

    task_id: str
    chat_id: int
    parent_agent: str
    name: str
    prompt_preview: str
    result_text: str
    status: str  # "done" | "failed" | "cancelled" | "timeout"
    elapsed_seconds: float
    provider: str
    model: str
    session_id: str = ""
    error: str = ""
    task_folder: str = ""
    original_prompt: str = ""
    thread_id: int | None = None
