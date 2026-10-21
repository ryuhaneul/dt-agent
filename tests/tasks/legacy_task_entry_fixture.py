"""Frozen task entry reader for compatibility with added provenance keys."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any

TASK_PRIORITIES = ("interactive", "background", "batch")
_DEFAULT_PRIORITY = "background"


def normalise_priority(value: str | None) -> str:
    """Return a valid priority or the default (``background``)."""
    if value and value in TASK_PRIORITIES:
        return value
    return _DEFAULT_PRIORITY


def _count(value: object) -> int:
    """A stored counter; anything but a non-negative integer (an old file) counts as 0."""
    return value if isinstance(value, int) and (not isinstance(value, bool)) and (value >= 0) else 0


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
    status: str
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
    tasks_dir: str = ""
    thread_id: int | None = None
    priority: str = _DEFAULT_PRIORITY
    last_question_received_at: float = 0.0
    instruction_received_at: float = 0.0
    instruction_waited_seconds: float | None = None
    notify_pending: bool = False
    notify_attempts: int = 0

    def to_dict(self) -> dict[str, object]:
        d: dict[str, object] = {
            "task_id": self.task_id,
            "chat_id": self.chat_id,
            "parent_agent": self.parent_agent,
            "name": self.name,
            "prompt_preview": self.prompt_preview,
            "original_prompt": self.original_prompt,
            "provider": self.provider,
            "model": self.model,
            "status": self.status,
            "session_id": self.session_id,
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
            or (not 0 <= question_received_at <= 253402300799)
        ):
            question_received_at = 0.0
        instruction_received_at = d.get("instruction_received_at", 0.0)
        if (
            type(instruction_received_at) not in (int, float)
            or not math.isfinite(instruction_received_at)
            or (not 0 <= instruction_received_at <= 253402300799)
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
            original_prompt=d.get("original_prompt", ""),
            thinking=d.get("thinking", ""),
            reasoning_effort=d.get("reasoning_effort", ""),
            tasks_dir=d.get("tasks_dir", ""),
            thread_id=d.get("thread_id"),
            notify_pending=d.get("notify_pending") is True,
            notify_attempts=_count(d.get("notify_attempts")),
            priority=normalise_priority(d.get("priority")),
        )
