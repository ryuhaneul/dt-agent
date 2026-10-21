"""Shared types for the CLI layer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from ductor_bot.cli.timeout_controller import TimeoutController


_TASK_LABEL_PREFIX = "task:"


def task_id_from_label(process_label: str) -> str:
    """Extract the task id from a ``task:<id>`` process label.

    Background tasks are the only requests labelled with this prefix
    (see ``TaskHub._run`` and ``ProcessRegistry.kill_for_task``).
    Returns an empty string for non-task labels.
    """
    if process_label.startswith(_TASK_LABEL_PREFIX):
        return process_label[len(_TASK_LABEL_PREFIX) :]
    return ""


class CLIResponse(BaseModel):
    """Response from a CLI call -- provider-agnostic.

    The previous token/cost tracking was wrong: it added the CLI's cumulative session cost on
    every turn and left cache tokens out of the token count. An accurate rework was judged not
    worth it, so the tracking was removed.
    """

    session_id: str | None = None
    result: str = ""
    is_error: bool = False
    returncode: int | None = None
    stderr: str = ""
    timed_out: bool = False
    stale_killed: bool = False  # the stale cleanup killed the process
    duration_ms: float | None = None
    duration_api_ms: float | None = None
    num_turns: int | None = None


@dataclass(frozen=True, slots=True)
class AgentRequest:
    """Immutable specification for a CLI call."""

    prompt: str
    system_prompt: str | None = None
    append_system_prompt: str | None = None
    model_override: str | None = None
    provider_override: str | None = None
    effort_override: str | None = None
    chat_id: int = 0
    topic_id: int | None = None
    transport: str = "tg"
    # Thread that *requested* this turn, when it differs from the chat the turn
    # is anchored to (inter-agent handling). Lets the agent's own bus tools
    # reply into the requesting thread instead of the anchor chat.
    origin_chat_id: int = 0
    origin_topic_id: int | None = None
    origin_transport: str = ""
    interagent_turn: bool = False
    delegation_id: str = ""
    session_kind: str = "background"
    session_name: str = ""
    result_origin: str = ""
    broadcast_ref: str = ""
    process_label: str = "main"
    resume_session: str | None = None
    continue_session: bool = False
    timeout_seconds: float | None = None
    timeout_controller: TimeoutController | None = None
    queue_context: str | None = None
    received_context: str | None = None
    # Claude turn whose system prompt is rendered anew each turn (see claude_features).
    claude_snapshot_off: bool = False
    private_task_question: bool = False

    @property
    def cli_prompt(self) -> str:
        """Add receipt/wait metadata only for CLI input; preserve display/record previews."""
        if self.queue_context:
            return f"{self.queue_context}\n\n{self.prompt}"
        return f"{self.received_context}\n{self.prompt}" if self.received_context else self.prompt


@dataclass(frozen=True, slots=True)
class AgentResponse:
    """Immutable result from a CLI call."""

    result: str
    returncode: int | None = None
    session_id: str | None = None
    is_error: bool = False
    num_turns: int = 0
    timed_out: bool = False
    duration_ms: float | None = None
    stream_fallback: bool = False
    stderr: str = ""
    stale_killed: bool = False
