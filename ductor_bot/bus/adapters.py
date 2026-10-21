"""Convert legacy result types to Envelope.

Each function maps a domain-specific result type to a unified
:class:`~ductor_bot.bus.envelope.Envelope` with the correct delivery,
lock, and injection flags.  The original result types are NOT replaced;
observers keep producing them and these adapters convert at the boundary.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from ductor_bot.bus.envelope import DeliveryMode, Envelope, LockMode, Origin
from ductor_bot.bus.failure_reason import english_reason
from ductor_bot.infra.input_context import current_input_timestamp
from ductor_bot.session.named import lane_from_session_name

if TYPE_CHECKING:
    from ductor_bot.background.models import BackgroundResult
    from ductor_bot.multiagent.bus import AsyncInterAgentResult
    from ductor_bot.tasks.models import TaskResult
    from ductor_bot.webhook.models import WebhookResult


# -- Background tasks ----------------------------------------------------------


def from_background_result(result: BackgroundResult) -> Envelope:
    """Convert a ``BackgroundResult`` (named session or stateless)."""
    return Envelope(
        origin=Origin.BACKGROUND,
        chat_id=result.chat_id,
        topic_id=result.thread_id,
        transport=result.transport,
        prompt_preview=result.prompt_preview,
        result_text=result.result_text,
        status=result.status,
        is_error=result.status.startswith("error:"),
        delivery=DeliveryMode.UNICAST,
        lock_mode=LockMode.NONE,
        reply_to_message_id=result.message_id,
        thread_id=result.thread_id,
        elapsed_seconds=result.elapsed_seconds,
        provider=result.provider,
        model=result.model,
        session_name=result.session_name,
        session_id=result.session_id,
        metadata={"task_id": result.task_id},
    )


# -- Cron jobs -----------------------------------------------------------------


def from_cron_result(  # noqa: PLR0913
    title: str,
    result: str,
    status: str,
    *,
    chat_id: int = 0,
    topic_id: int | None = None,
    transport: str = "tg",
    broadcast_targets: list[str] | None = None,
    prompt: str = "",
    job_id: str = "",
) -> Envelope:
    """Convert a cron job result (title, text, status triple).

    When *chat_id* is non-zero the envelope is unicast to that chat/topic.
    Otherwise it broadcasts to all users (legacy behaviour).

    A *prompt* (``delivery_mode`` inject / in_session) is injected into the chat/topic's
    active session under the session lock, and the session's reply is what gets delivered;
    *result* stays the fallback text when the injection fails.
    """
    if chat_id and prompt:
        return Envelope(
            origin=Origin.CRON,
            chat_id=chat_id,
            topic_id=topic_id,
            transport=transport,
            prompt=prompt,
            prompt_preview=title,
            result_text=result,
            status=status,
            is_error=status.startswith("error:"),
            delivery=DeliveryMode.UNICAST,
            lock_mode=LockMode.REQUIRED,
            needs_injection=True,
            metadata={"title": title, "job_id": job_id, "counts_as_activity": False},
        )
    if chat_id:
        return Envelope(
            origin=Origin.CRON,
            chat_id=chat_id,
            topic_id=topic_id,
            transport=transport,
            result_text=result,
            status=status,
            delivery=DeliveryMode.UNICAST,
            lock_mode=LockMode.NONE,
            metadata={"title": title},
        )
    return Envelope(
        origin=Origin.CRON,
        chat_id=0,
        transport=transport,
        result_text=result,
        status=status,
        delivery=DeliveryMode.BROADCAST,
        lock_mode=LockMode.NONE,
        broadcast_targets=broadcast_targets,
        metadata={"title": title},
    )


CRON_RESULT_PROMPT_CHARS = 6000


def cron_result_prompt(  # noqa: PLR0913
    title: str,
    job_id: str,
    status: str,
    result: str,
    *,
    ran_at: str,
    result_file: str = "",
) -> str:
    """Prompt injected into a topic session for ``delivery_mode: inject``."""
    failed = status != "success"
    head = (
        f"[CRON JOB {'FAILED' if failed else 'RESULT'}: title='{title}' job_id='{job_id}']\n"
        f"Status: {status} | Ran at: {ran_at}\n"
    )
    body = result[:CRON_RESULT_PROMPT_CHARS]
    if len(result) > CRON_RESULT_PROMPT_CHARS:
        body += (
            f"\n[... {len(result) - CRON_RESULT_PROMPT_CHARS} more characters; "
            f"the full result is in {result_file}]"
        )
    ask = (
        "Tell the user that this scheduled job failed and what went wrong, and suggest next steps."
        if failed
        else "Report this result to the user in this conversation (the essentials, briefly) "
        "and keep it in mind for follow-up questions."
    )
    return f"{head}\nResult:\n{body or '(empty)'}\n\n{ask}"


def cron_instruction_prompt(  # noqa: PLR0913
    title: str,
    job_id: str,
    instruction: str,
    *,
    started_at: str,
    task_folder: str,
    description_file: str,
    description: str,
) -> str:
    """Prompt injected into a topic session for ``delivery_mode: in_session``."""
    memory_file = f"{job_id}_MEMORY.md"
    return (
        f"[CRON JOB STARTED IN THIS SESSION: title='{title}' job_id='{job_id}']\n"
        f"It is {started_at}. The scheduler runs this job now, in this conversation instead of a "
        f"separate run. Do the work yourself, then report the result here.\n\n"
        f"Instruction:\n{instruction}\n\n"
        f"Task folder: {task_folder} (the job's scripts and files live there; read "
        f"{memory_file} first and update it with DATE + TIME and what you did when finished)\n"
        f"Task description file: {description_file}\n"
        f"---\n{description}\n---\n"
        "Do not include transport/debug/tool confirmations in the result."
    )


# -- Heartbeat ----------------------------------------------------------------


def from_heartbeat(
    chat_id: int,
    text: str,
    topic_id: int | None = None,
    *,
    transport: str = "tg",
) -> Envelope:
    """Convert a heartbeat alert."""
    return Envelope(
        origin=Origin.HEARTBEAT,
        chat_id=chat_id,
        topic_id=topic_id,
        transport=transport,
        result_text=text,
        status="success",
        delivery=DeliveryMode.UNICAST,
        lock_mode=LockMode.NONE,
    )


# -- Webhooks ------------------------------------------------------------------


def from_webhook_cron_result(result: WebhookResult) -> Envelope:
    """Convert a webhook cron_task result (broadcast)."""
    return Envelope(
        origin=Origin.WEBHOOK_CRON,
        chat_id=0,
        result_text=result.result_text,
        status=result.status,
        delivery=DeliveryMode.BROADCAST,
        lock_mode=LockMode.NONE,
        metadata={
            "hook_id": result.hook_id,
            "hook_title": result.hook_title,
        },
    )


def from_webhook_wake(chat_id: int, prompt: str) -> Envelope:
    """Convert a webhook wake request (acquires lock, executes via orchestrator)."""
    return Envelope(
        origin=Origin.WEBHOOK_WAKE,
        chat_id=chat_id,
        prompt=prompt,
        delivery=DeliveryMode.UNICAST,
        lock_mode=LockMode.REQUIRED,
    )


# -- Inter-agent ---------------------------------------------------------------


def _session_line(result: AsyncInterAgentResult) -> str:
    """Session (and lane) the request ran in; empty when the result carries no name."""
    if not result.session_name:
        return ""
    lane = lane_from_session_name(result.session_name)
    lane_text = f" · Lane: `{lane}`" if lane else ""
    return f"\nSession: `{result.session_name}`{lane_text}"


def build_interagent_injection_prompt(
    result: AsyncInterAgentResult,
    *,
    agent_name: str,
    transport_label: str,
) -> str:
    """Build the prompt injected into the recipient agent's session.

    Used for successful and failed async tasks. The receiving agent must
    process the result and report to the original chat and topic.
    """
    if not result.success:
        if result.failure_kind == "result_lost":
            return (
                f"[ASYNC INTER-AGENT RESULT LOST from '{result.recipient or result.sender}' "
                f"(task {result.task_id})]\n"
                f"Reason: {english_reason(result.error or 'result lost')}{_session_line(result)}\n"
                f"Original request: {result.original_message or result.message_preview}\n"
                f"Original chat ID: {result.chat_id}, original topic ID: {result.topic_id}, "
                f"transport: {result.transport or 'unknown'}\n"
                "The original work may already be done and only its result was lost. "
                "Do not delegate the same request again. First tell the user in the original "
                "chat/topic that the result was lost and ask whether to run it again. "
                "Do not run it again until the user asks for it."
            )
        count = getattr(result, "failure_count", 0)
        count_hint = f"\nFailures of this request in the last 24 hours: {count}" if count else ""
        repeat_hint = (
            "\nDo not delegate the same request again; report the failure to the user."
            if count >= 2
            else ""
        )
        return (
            f"[ASYNC INTER-AGENT FAILURE from '{result.recipient or result.sender}' "
            f"(task {result.task_id})]\n"
            f"Reason: {english_reason(result.error or 'unknown')}{_session_line(result)}\n"
            f"Original request: {result.original_message or result.message_preview}"
            f"{count_hint}{repeat_hint}\n"
            "Decide whether to retry and how to report it to the user, then give the final "
            "report in this chat."
        )
    recipient = result.recipient or result.sender
    session_hint = ""
    if result.session_name:
        session_hint = f"\nThe recipient processed this in session `{result.session_name}`."
        if lane := lane_from_session_name(result.session_name):
            session_hint += f" Lane: `{lane}`."
        if not result.recipient_headless:
            session_hint += (
                f" The user can continue this session in the recipient's {transport_label} "
                f"via `@{result.session_name} <message>`."
            )
    task_context = (
        f"\n\nOriginal task you sent to '{recipient}':\n{result.original_message}"
        if result.original_message
        else ""
    )
    return (
        f"[ASYNC INTER-AGENT RESPONSE from '{recipient}'"
        f" (task {result.task_id})]\n"
        f"{result.result_text}\n"
        f"[END ASYNC INTER-AGENT RESPONSE]{session_hint}{task_context}\n\n"
        f"You are agent '{agent_name}'. Process this response from agent "
        f"'{recipient}' and communicate the relevant results to the user "
        f"in your {transport_label}."
    )


def from_interagent_result(
    result: AsyncInterAgentResult,
    chat_id: int,
    *,
    injection_prompt: str = "",
    transport: str | None = None,
) -> Envelope:
    """Convert an async inter-agent result.

    Uses ``result.chat_id`` / ``result.topic_id`` when available so that
    results are routed back to the originating group topic.  Falls back to
    the sender's default *chat_id*.

    Both success and failure results acquire the lock and request injection.
    The caller supplies an injection prompt for the receiving agent.
    """
    broadcast = getattr(result, "result_origin", "") == "cron-broadcast" and not result.chat_id
    delivery_chat_id = 0 if broadcast else (result.chat_id or chat_id)
    delivery_transport = transport or getattr(result, "transport", "") or "tg"
    main_response = getattr(result, "main_response", "")
    timestamp = getattr(result, "received_at", None)
    meta = {
        "task_id": result.task_id,
        "ia_task_id": result.task_id,
        "ia_target": getattr(result, "reply_to", "") or result.sender,
        "sender": result.sender,
        "recipient": result.recipient,
        "error": result.error,
        "provider_switch_notice": (
            "" if getattr(result, "delivery_retry", False) else result.provider_switch_notice
        ),
        "original_message": result.original_message,
        "transport": delivery_transport,
        "result_origin": getattr(result, "result_origin", ""),
        "broadcast_ref": getattr(result, "broadcast_ref", ""),
        "isolated": broadcast,
    }

    return Envelope(
        origin=Origin.INTERAGENT,
        created_at=time.time() if timestamp is None else timestamp,
        chat_id=delivery_chat_id,
        topic_id=result.topic_id,
        transport=delivery_transport,
        prompt="" if main_response else injection_prompt,
        prompt_preview=result.message_preview,
        result_text=main_response or result.result_text,
        status="success" if result.success else "error",
        delivery=DeliveryMode.BROADCAST if broadcast else DeliveryMode.UNICAST,
        lock_mode=LockMode.REQUIRED,
        needs_injection=False
        if main_response
        else (bool(injection_prompt) if result.success else True),
        injected=bool(main_response),
        delivered_chunks=getattr(result, "delivered_chunks", 0),
        start_chunk=getattr(result, "delivered_chunks", 0),
        elapsed_seconds=result.elapsed_seconds,
        session_name=result.session_name,
        metadata=meta,
    )


# -- Task results & questions --------------------------------------------------


def from_task_result(result: TaskResult) -> Envelope:
    """Convert a background task result.

    done/failed: acquire lock, inject into parent session.
    cancelled/timeout: unicast notification only (no injection).
    """
    needs_inject = result.status in ("done", "failed")
    prompt = _build_task_injection_prompt(result) if needs_inject else ""
    return Envelope(
        origin=Origin.TASK_RESULT,
        chat_id=result.chat_id,
        topic_id=result.thread_id,
        prompt=prompt,
        prompt_preview=result.prompt_preview,
        result_text=result.result_text,
        status=result.status,
        is_error=result.status == "failed",
        delivery=DeliveryMode.UNICAST,
        lock_mode=LockMode.REQUIRED if needs_inject else LockMode.NONE,
        needs_injection=needs_inject,
        elapsed_seconds=result.elapsed_seconds,
        provider=result.provider,
        model=result.model,
        session_id=result.session_id,
        metadata={
            "task_id": result.task_id,
            "name": result.name,
            "parent_agent": result.parent_agent,
            "error": result.error,
            "task_folder": result.task_folder,
        },
    )


def _build_task_injection_prompt(result: TaskResult) -> str:
    """Build the prompt injected into the parent agent's session."""
    task_id = result.task_id
    if result.status in ("failed", "timeout"):
        return (
            f"[BACKGROUND TASK FAILED: task_id='{task_id}' name='{result.name}']\n"
            f"Error: {result.error}\n"
            f"Provider: {result.provider}/{result.model} | "
            f"Duration: {result.elapsed_seconds:.0f}s\n\n"
            f"Original task: {result.original_prompt}\n\n"
            f"Inform the user that the background task '{result.name}' failed "
            f"and suggest next steps."
        )
    return (
        f"[BACKGROUND TASK COMPLETED: task_id='{task_id}' name='{result.name}']\n"
        f"Provider: {result.provider}/{result.model} | "
        f"Duration: {result.elapsed_seconds:.0f}s\n\n"
        f"{result.result_text}\n\n"
        f"[END TASK RESULT]\n\n"
        f"Original task: {result.original_prompt}\n\n"
        f"IMPORTANT: This is a background task result delivery. "
        f"ONLY process this task result. Do NOT process, respond to, or "
        f"create tasks for any user messages that appear earlier in your "
        f"conversation history — those have already been handled in a "
        f"previous turn.\n\n"
        f"Review this result critically:\n"
        f"- Does it fully answer the original task?\n"
        f"- Is anything missing, incomplete, or unclear?\n"
        f"- If yes → resume the task with a follow-up "
        f"(see resume_task.py command above)\n"
        f"- If the result is complete → summarize findings for the user\n"
    )


def from_task_question(
    task_id: str,
    question: str,
    prompt_preview: str,
    chat_id: int,
    *,
    topic_id: int | None = None,
) -> Envelope:
    """Convert a task question (worker asks parent agent)."""
    timestamp = current_input_timestamp()
    return Envelope(
        origin=Origin.TASK_QUESTION,
        created_at=time.time() if timestamp is None else timestamp,
        chat_id=chat_id,
        topic_id=topic_id,
        prompt=question,
        prompt_preview=prompt_preview,
        delivery=DeliveryMode.UNICAST,
        lock_mode=LockMode.REQUIRED,
        needs_injection=True,
        metadata={"task_id": task_id},
    )
