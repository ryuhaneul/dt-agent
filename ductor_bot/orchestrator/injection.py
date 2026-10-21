"""Session injection: routes inter-agent messages and task questions through CLIService.

Extracts the common "build prompt → get active session → execute → update"
pattern from the Orchestrator into reusable helpers.

Note: task *results* are injected via the MessageBus (see ``bus.adapters``).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from typing import TYPE_CHECKING, TypedDict

from ductor_bot.cli.claude_features import snapshot_refresh_active
from ductor_bot.cli.types import AgentRequest
from ductor_bot.infra.input_context import (
    acquire_input_lock,
    add_input_wait,
    current_input_context,
)
from ductor_bot.multiagent.delegation import (
    DelegationContext,
    delegation_session_key,
    source_header,
)
from ductor_bot.orchestrator.flows import (
    _is_invalid_session,
    _update_session,
    build_append_prompt,
)
from ductor_bot.session.key import SessionKey
from ductor_bot.session.named import NamedSession, interagent_session_name
from ductor_bot.workspace.loader import build_appended_files_block

if TYPE_CHECKING:
    from collections.abc import Callable

    from ductor_bot.multiagent.session_gate import Reservation
    from ductor_bot.orchestrator.core import Orchestrator

logger = logging.getLogger(__name__)


class InjectionIncomplete(RuntimeError):  # noqa: N818 -- reads as the state it reports
    """The injected turn ended in an error or timeout instead of a reply."""


_TRANSPORT_ALIASES = {"telegram": "tg", "matrix": "mx"}
# Leave 240s of the sub-agent's 300s synchronous-call budget for an answer.
_ASYNC_QUESTION_LOCK_WAIT = 60.0


# Approved rules block, injected verbatim into every delegation request a sub-agent receives.
_RULES_HEAD = (
    "[Delegated work rules]\n"
    "- Do this work inside your current turn and return the result in your reply. "
    "Do not hand it to a background task (create_task): its result would come back to "
    "your own chat, not to the delegating agent's original conversation.\n"
    "- This turn has a hard limit (1 hour for async delegation). If the request cannot "
    "finish in time, do not start: reply that it needs to be split and suggest how. "
    "That reply is expected, not a failure.\n"
)
_RULES_END = "[/Delegated work rules]"
DELEGATED_WORK_RULES = _RULES_HEAD + _RULES_END


def _delegated_work_rules(lane: str) -> str:
    """The rules block; a laned request gets one more line naming its lane."""
    if not lane:
        return DELEGATED_WORK_RULES
    lane_line = (
        f"- This request runs in lane '{lane}'. Other lanes may run concurrently in the "
        "same workspace. Make repository changes in a git worktree dedicated to this lane. "
        "Do not edit shared memory files unless instructed; put that content in your report.\n"
    )
    return _RULES_HEAD + lane_line + _RULES_END


async def _with_snapshot_flag(orch: Orchestrator, request: AgentRequest) -> AgentRequest:
    """Mark a Claude turn (judged on the effective provider) whose system prompt refreshes."""
    provider = orch._cli_service.resolve_provider(request)[0]
    if await snapshot_refresh_active(orch._config.claude_system_prompt_snapshot, provider):
        return replace(request, claude_snapshot_off=True)
    return request


def _transport_id(value: str) -> str:
    """Return the short transport id used by SessionKey and Envelope."""
    stripped = value.strip().lower()
    return _TRANSPORT_ALIASES.get(stripped, stripped or "tg")


# ---------------------------------------------------------------------------
# Shared injection helper
# ---------------------------------------------------------------------------


async def _inject_prompt(  # noqa: PLR0913
    orch: Orchestrator,
    prompt: str,
    chat_id: int,
    process_label: str,
    *,
    topic_id: int | None = None,
    transport: str = "tg",
    session_transport: str | None = None,
    isolated: bool = False,
    result_origin: str = "",
    broadcast_ref: str = "",
    counts_as_activity: bool = True,
    private_task_question: bool = False,
) -> str:
    """Execute *prompt* in the current active session and update session state.

    *counts_as_activity* is False for a turn the user did not cause (a cron result or job):
    it is recorded as a message but not as user activity of the session.

    Backs ``Orchestrator.inject_prompt`` (the ``SessionInjector`` protocol).
    """
    key = SessionKey(transport=session_transport or transport, chat_id=chat_id, topic_id=topic_id)
    active = None if isolated else await orch._sessions.get_active(key)
    if active is None and not isolated:
        # No session yet: create the one the next turn here will continue, so that the id this
        # turn returns is stored for exactly this (transport, chat, topic).
        model_name, provider_name = orch.resolve_runtime_target(orch._config.model)
        active, _created = await orch._sessions.resolve_session(
            key,
            provider=provider_name,
            model=model_name,
            reasoning_effort=orch._config.reasoning_effort,
        )
    resume_id = (active.session_id or None) if active else None

    request = AgentRequest(
        prompt=prompt,
        received_context=current_input_context(orch._config.user_timezone),
        chat_id=chat_id,
        topic_id=topic_id,
        transport=transport,
        result_origin=result_origin,
        broadcast_ref=broadcast_ref,
        process_label=process_label,
        private_task_question=private_task_question,
        provider_override=active.provider if active else None,
        model_override=active.model if active else None,
        effort_override=(active.reasoning_effort or None) if active else None,
        session_kind="background" if isolated else "foreground",
        resume_session=resume_id,
        timeout_seconds=orch._config.cli_timeout,
    )

    if isolated:
        # A separate session: the file blocks only, plus the snapshot flag.
        files_block = await build_appended_files_block(
            orch.paths, orch._config.append_system_prompt_files
        )
        request = await _with_snapshot_flag(
            orch, replace(request, append_system_prompt=files_block)
        )
    else:
        # The user's own session continues: assembled like a normal conversation turn.
        provider = orch._cli_service.resolve_provider(request)[0]
        append, snapshot_off = await build_append_prompt(
            orch, provider=provider, is_new=not resume_id
        )
        request = replace(request, append_system_prompt=append, claude_snapshot_off=snapshot_off)

    response = await orch._cli_service.execute(request)

    if active and response:
        # A failed or timed-out turn is stored like any turn, but never as user activity.
        completed = not (response.is_error or response.timed_out)
        await _update_session(
            orch, active, response, counts_as_activity=counts_as_activity and completed
        )

    if response and (response.is_error or response.timed_out):
        # An error or timeout is no reply: do not hand its (usually empty) text on as one.
        msg = f"injected turn did not complete ({'timeout' if response.timed_out else 'error'})"
        raise InjectionIncomplete(msg)
    return response.result if response else ""


async def answer_task_question(  # noqa: C901, PLR0911, PLR0912, PLR0913, PLR0915, PLR0917
    orch: Orchestrator,
    task_id: str,
    task_name: str,
    question: str,
    chat_id: int,
    topic_id: int | None,
    transport: str,
    resolve_origin_session_id: Callable[[], str],
    connection_alive: Callable[[], bool],
    deadline: float,
) -> tuple[str | None, str]:
    """Answer privately in the matching conversation within an absolute deadline."""
    loop = asyncio.get_running_loop()
    started = loop.time()
    waited = 0.0
    reason = "error"
    try:
        key = delegation_session_key(transport, chat_id, topic_id)
        if orch._lock_pool is None or not chat_id:
            reason = "no_session"
            return None, reason
        remaining = deadline - loop.time()
        if remaining <= 0:
            reason = "deadline"
            return None, reason
        lock = orch._lock_pool.get(key.lock_key)
        acquired = False
        try:
            wait_started = loop.time()
            try:
                async with asyncio.timeout(min(_ASYNC_QUESTION_LOCK_WAIT, remaining)):
                    await acquire_input_lock(lock)
                    acquired = True
            except TimeoutError:
                reason = "busy"
                return None, reason
            finally:
                waited = loop.time() - wait_started

            origin_session_id = resolve_origin_session_id()
            active = await orch._sessions.get_active(key)
            if not origin_session_id or active is None or active.session_id != origin_session_id:
                reason = "session_mismatch"
                return None, reason
            if not connection_alive():
                reason = "client_gone"
                return None, reason
            remaining = deadline - loop.time()
            if remaining <= 0:
                reason = "deadline"
                return None, reason
            prompt = (
                f"Background task '{task_name}' ({task_id}), which you started from this "
                f"conversation, asks a question while it is still running: {question}\n\n"
                "Answer from this conversation's context. Your reply goes only to the task "
                "(it is not posted to the user chat) and the task continues with it.\n\n"
                "In this turn reply with the answer only: do not create, resume or cancel "
                "tasks, do not message other agents, do not commit, push, deploy or edit files.\n\n"
                "If answering needs the user's decision or approval, or information you do "
                "not have, reply with a line containing only NEEDS_USER; the task will finish "
                "this run and the question will be shown in the chat."
            )
            try:
                async with asyncio.timeout(remaining):
                    answer = await _inject_prompt(
                        orch,
                        prompt,
                        key.chat_id,
                        process_label=f"task-question:{task_id}",
                        topic_id=key.topic_id,
                        transport=transport,
                        session_transport=key.transport,
                        private_task_question=True,
                    )
            except TimeoutError:
                reason = "deadline"
                return None, reason
        finally:
            if acquired:
                lock.release()

        if not answer.strip():
            reason = "empty"
            return None, reason
        if any(line.strip().upper() == "NEEDS_USER" for line in answer.splitlines()):
            reason = "needs_user"
            return None, reason
        reason = ""
    except Exception:
        reason = "error"
        return None, reason
    else:
        return answer, reason
    finally:
        logger.info(
            "Task question task=%s result=%s reason=%s waited_ms=%d elapsed_ms=%d",
            task_id,
            "fallback" if reason else "answered",
            reason,
            round(waited * 1000),
            round((loop.time() - started) * 1000),
        )


async def _answer_async_delegation_question(
    orch: Orchestrator,
    sender: str,
    message: str,
    context: DelegationContext,
) -> str | None:
    """Answer privately in the current source session, or request named fallback.

    The pool is wired from MessageBus, shared with user turns and result
    injections. Never substitute a private lock when that pool is unavailable.
    """
    if orch._lock_pool is None or not context.chat_id:
        return None
    transport = _transport_id(context.transport)
    key = delegation_session_key(transport, context.chat_id, context.topic_id)
    # Named/background callers cannot safely resume an unrelated foreground turn.
    # Use the existing dedicated session and captured provenance instead.
    if context.session_kind in {"named", "background", "unknown"} or (
        context.session_key and context.session_key != key.storage_key
    ):
        return None
    lock = orch._lock_pool.get(key.lock_key)
    acquired = False
    try:
        try:
            async with asyncio.timeout(_ASYNC_QUESTION_LOCK_WAIT):
                await acquire_input_lock(lock)
                acquired = True
        except TimeoutError:
            logger.info(
                "Delegation question lock wait expired sender=%s task=%s chat=%s topic=%s",
                sender,
                context.request_id,
                key.chat_id,
                key.topic_id,
            )
            return None
        try:
            active = await orch._sessions.get_active(key)
        except (OSError, ValueError, TypeError):
            return None
        if active is None or not active.session_id:
            return None
        prompt = (
            f"Sub-agent '{sender}' asks a question while performing delegated task "
            f"{context.request_id}:\n{message}\n\n"
            f"Answer this question in the current conversation context. Your answer "
            f"is returned only to '{sender}', not posted to the user chat."
        )
        return await _inject_prompt(
            orch,
            prompt,
            key.chat_id,
            f"interagent-question:{sender}",
            topic_id=key.topic_id,
            transport=transport,
            session_transport=key.transport,
        )
    finally:
        if acquired:
            lock.release()


# ---------------------------------------------------------------------------
# Inter-agent session helpers
# ---------------------------------------------------------------------------


def _settle(ns: NamedSession) -> None:
    """Mark the session idle again, unless it was ended meanwhile (never revive it)."""
    if ns.status != "ended":
        ns.status = "idle"


def _interagent_chat_id(orch: Orchestrator) -> int:
    """Return the real Telegram chat_id for inter-agent sessions."""
    if not orch._config.allowed_user_ids:
        logger.warning("No allowed_user_ids configured — inter-agent sessions use chat_id=0")
        return 0
    return orch._config.allowed_user_ids[0]


def _get_or_create_interagent_session(  # noqa: PLR0913 -- identity + origin + lane
    orch: Orchestrator,
    sender: str,
    *,
    new_session: bool = False,
    source_chat_id: int = 0,
    source_topic_id: int | None = None,
    lane: str = "",
) -> tuple[NamedSession, bool, str]:
    """Get or create a Named Session for an inter-agent conversation.

    Uses a deterministic name scoped by sender chat/topic and optional lane.
    Calls without source context and lane keep the legacy ``ia-{sender}`` name.

    If *new_session* is True, any existing session for this sender is
    ended first so a fresh one is created.

    If the active provider/model has changed since the session was created,
    the old session is ended automatically (the CLI session ID is not
    portable across providers) and a provider-switch notice is returned.

    Returns ``(session, is_new, provider_switch_notice)``.
    """
    chat_id = _interagent_chat_id(orch)
    session_name = interagent_session_name(sender, source_chat_id, source_topic_id, lane)
    provider_switch_notice = ""

    if new_session and orch._named_sessions.end_session(chat_id, session_name):
        logger.info("Inter-agent session reset: %s (sender=%s)", session_name, sender)

    model_name, provider_name = orch.resolve_runtime_target(orch._config.model)

    ns = orch._named_sessions.get(chat_id, session_name)
    if ns is not None and ns.status != "ended":
        # Detect provider/model mismatch → session ID is not portable
        if ns.provider != provider_name:
            old_provider = ns.provider
            orch._named_sessions.end_session(chat_id, session_name)
            logger.info(
                "Inter-agent session %s reset: provider changed %s -> %s",
                session_name,
                old_provider,
                provider_name,
            )
            provider_switch_notice = (
                f"Agent `{orch._cli_service._config.agent_name}` switched "
                f"provider from `{old_provider}` to `{provider_name}`.\n"
                f"The previous inter-agent session `{session_name}` is no longer "
                f"resumable and has been ended.\n"
                f"A new session `{session_name}` was started with `{provider_name}`."
            )
        else:
            return ns, False, ""

    ns = NamedSession(
        name=session_name,
        chat_id=chat_id,
        provider=provider_name,
        model=model_name,
        session_id="",
        prompt_preview=f"Inter-agent session with {sender}",
        status="running",
        created_at=time.time(),
    )
    orch._named_sessions.add(ns)
    logger.info("Inter-agent named session created: %s (sender=%s)", session_name, sender)
    return ns, True, provider_switch_notice


# ---------------------------------------------------------------------------
# Public handlers (called by Orchestrator as thin delegations)
# ---------------------------------------------------------------------------


def _delegation_notes(
    own_name: str,
    sender: str,
    delegation: DelegationContext | None,
    question_context: DelegationContext | None,
    lane: str = "",
) -> str:
    """Extra prompt text for work requests received by a sub-agent (never for questions)."""
    if own_name == "main" or question_context is not None:
        return ""
    notes = ""
    if delegation and delegation.mode == "sync" and sender == "main":
        notes += (
            "\nThis is a synchronous delegation. Do not send question messages to main "
            "during this work; the server rejects them. Include any questions in your final report. "
            "Continue the work; do not invent missing values, and leave them empty or state assumptions.\n"
        )
    # Every work request a sub-agent receives (sync or async) is a delegation.
    return notes + "\n" + _delegated_work_rules(lane)


async def handle_interagent_message(  # noqa: PLR0913
    orch: Orchestrator,
    sender: str,
    message: str,
    *,
    new_session: bool = False,
    source_chat_id: int = 0,
    source_topic_id: int | None = None,
    source_transport: str = "",
    delegation: DelegationContext | None = None,
    question_context: DelegationContext | None = None,
    lane: str = "",
    admission: Callable[[int, str], Reservation] | None = None,
) -> tuple[str, str, str]:
    """Process a message from another agent via the InterAgentBus.

    Async delegation questions resume the locked source session privately.
    Other messages use a sender/chat/topic-scoped Named Session and
    reuses it for follow-up messages from the same chat/topic. Calls without
    source context keep the legacy ``ia-{sender}`` name. The exact session name
    is returned so callers can report it and users can resume it manually from
    Telegram via ``@<session-name> <message>``.

    Returns ``(result_text, session_name, provider_switch_notice)``.
    The *provider_switch_notice* is non-empty when a provider change
    caused an automatic session reset — callers should notify the user.
    """
    own_name = orch._cli_service._config.agent_name
    if (
        own_name == "main"
        and question_context
        and question_context.mode == "async"
        and (
            answer := await _answer_async_delegation_question(
                orch, sender, message, question_context
            )
        )
        is not None
    ):
        return answer, "", ""
    args = (orch, sender, message)
    options: _SessionTurnOptions = {
        "new_session": new_session,
        "source_chat_id": source_chat_id,
        "source_topic_id": source_topic_id,
        "source_transport": source_transport,
        "delegation": delegation,
        "question_context": question_context,
        "lane": lane,
        "own_name": own_name,
    }
    if admission is None:
        return await _interagent_session_turn(*args, **options)
    # A question needs a Named session only now (the original-session answer failed or is not
    # possible): reserve and enter the session queue at this point, not earlier.
    reservation = admission(
        _interagent_chat_id(orch),
        interagent_session_name(sender, source_chat_id, source_topic_id, lane),
    )
    waited_from = time.monotonic()
    async with reservation:
        add_input_wait(time.monotonic() - waited_from)
        return await _interagent_session_turn(*args, **options)


class _SessionTurnOptions(TypedDict):
    new_session: bool
    source_chat_id: int
    source_topic_id: int | None
    source_transport: str
    delegation: DelegationContext | None
    question_context: DelegationContext | None
    lane: str
    own_name: str


async def _interagent_session_turn(  # noqa: PLR0913
    orch: Orchestrator,
    sender: str,
    message: str,
    *,
    new_session: bool,
    source_chat_id: int,
    source_topic_id: int | None,
    source_transport: str,
    delegation: DelegationContext | None,
    question_context: DelegationContext | None,
    lane: str,
    own_name: str,
) -> tuple[str, str, str]:
    """Run the request in its Named session (the part protected by the session queue)."""
    chat_id = _interagent_chat_id(orch)
    transport = _transport_id(orch._config.transport)

    # The CLI turn stays anchored to this agent's own chat: background tasks it
    # spawns belong in the recipient's chat, not in a group the recipient's bot
    # may not even be a member of. The *origin* travels alongside instead, so a
    # reply the recipient sends back through the bus can be routed to the thread
    # the request came from.
    origin_transport = _transport_id(source_transport) if source_transport else transport

    ns, _is_new, provider_switch_notice = _get_or_create_interagent_session(
        orch,
        sender,
        new_session=new_session,
        source_chat_id=source_chat_id,
        source_topic_id=source_topic_id,
        lane=lane,
    )

    prompt = (
        f"[INTER-AGENT MESSAGE from '{sender}' to '{own_name}']\n"
        f"{message}\n"
        f"[END INTER-AGENT MESSAGE]\n\n"
        f"You are agent '{own_name}'. Respond to this inter-agent request "
        f"from '{sender}'. Be direct and concise."
    )

    prompt += _delegation_notes(own_name, sender, delegation, question_context, lane)
    if question_context and own_name == "main":
        prompt = source_header(question_context) + prompt

    ns.status = "running"
    files_block = await build_appended_files_block(
        orch.paths, orch._config.append_system_prompt_files
    )
    request = AgentRequest(
        prompt=prompt,
        received_context=current_input_context(orch._config.user_timezone),
        append_system_prompt=files_block,
        chat_id=chat_id,
        transport=transport,
        origin_chat_id=source_chat_id,
        origin_topic_id=source_topic_id,
        origin_transport=origin_transport,
        interagent_turn=True,
        delegation_id=delegation.request_id if delegation else "",
        process_label=f"interagent:{ns.name}",
        session_kind="named",
        session_name=ns.name,
        effort_override=ns.reasoning_effort or None,
        resume_session=ns.session_id or None,
        timeout_seconds=orch._config.cli_timeout,
    )

    request = await _with_snapshot_flag(orch, request)
    try:
        response = await orch._cli_service.execute(request)
    except asyncio.CancelledError:
        _settle(ns)
        raise
    except Exception:
        _settle(ns)
        logger.exception("Inter-agent message handling failed (from=%s)", sender)
        return (
            f"Error processing inter-agent message from '{sender}'",
            ns.name,
            provider_switch_notice,
        )

    # #81: Claude / Codex CLI can invalidate cached session IDs after a
    # version bump or cache clear. Detect the stale-session error and retry
    # ONCE with a fresh session so async inter-agent sends don't silently
    # fail. The recovery is visible: it emits a WARNING log AND prepends a
    # notice to provider_switch_notice so the caller sees what happened.
    if _is_invalid_session(response):
        stale_id = ns.session_id
        logger.warning(
            "Inter-agent session stale (from=%s session=%s stale_id=%s) -- "
            "retrying with fresh session",
            sender,
            ns.name,
            stale_id,
        )
        orch._named_sessions.end_session(chat_id, ns.name)
        ns, _, _ = _get_or_create_interagent_session(
            orch,
            sender,
            new_session=True,
            source_chat_id=source_chat_id,
            source_topic_id=source_topic_id,
            lane=lane,
        )
        ns.status = "running"
        files_block = await build_appended_files_block(
            orch.paths, orch._config.append_system_prompt_files
        )
        retry_request = AgentRequest(
            prompt=prompt,
            received_context=current_input_context(orch._config.user_timezone),
            append_system_prompt=files_block,
            chat_id=chat_id,
            transport=transport,
            origin_chat_id=source_chat_id,
            origin_topic_id=source_topic_id,
            origin_transport=origin_transport,
            interagent_turn=True,
            delegation_id=delegation.request_id if delegation else "",
            process_label=f"interagent:{ns.name}",
            session_kind="named",
            session_name=ns.name,
            effort_override=ns.reasoning_effort or None,
            resume_session=None,
            timeout_seconds=orch._config.cli_timeout,
        )
        retry_request = await _with_snapshot_flag(orch, retry_request)
        try:
            response = await orch._cli_service.execute(retry_request)
        except asyncio.CancelledError:
            _settle(ns)
            raise
        except Exception:
            _settle(ns)
            logger.exception("Inter-agent retry failed (from=%s)", sender)
            return (
                f"Error processing inter-agent message from '{sender}' (after stale-session retry)",
                ns.name,
                provider_switch_notice,
            )
        recovery_notice = (
            f"Inter-agent session `{ns.name}` was stale "
            f"(CLI rejected session `{stale_id}`); started a fresh session "
            f"and retried. This is normal after a CLI update."
        )
        provider_switch_notice = (
            f"{provider_switch_notice}\n{recovery_notice}".strip()
            if provider_switch_notice
            else recovery_notice
        )

    if response and response.session_id:
        orch._named_sessions.update_after_response(
            chat_id, ns.name, response.session_id, status="idle"
        )
    else:
        _settle(ns)
    return (response.result if response else ""), ns.name, provider_switch_notice
