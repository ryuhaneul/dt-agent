"""Server-owned provenance for an in-flight inter-agent delegation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ductor_bot.session.key import SessionKey

if TYPE_CHECKING:
    from ductor_bot.session.manager import SessionData
    from ductor_bot.session.named import NamedSession


@dataclass(frozen=True, slots=True)
class DelegationContext:
    """Captured at dispatch; looked up by id, sender and recipient on questions."""

    request_id: str
    mode: str
    sender: str
    recipient: str
    chat_id: int = 0
    topic_id: int | None = None
    transport: str = "tg"
    topic_name: str = ""
    session_key: str = ""
    provider: str = ""
    session_id: str = ""
    request_preview: str = ""
    transcript_path: str = ""
    metadata_path: str = ""
    session_kind: str = ""
    session_name: str = ""


def delegation_session_key(transport: str, chat_id: int, topic_id: int | None) -> SessionKey:
    """API CLI provenance keeps its transport, but uses Telegram session storage."""
    transport = {"telegram": "tg", "matrix": "mx"}.get(transport, transport or "tg")
    return SessionKey("tg" if transport == "api" else transport, chat_id, topic_id)


def safe_preview(message: str) -> str:
    """Do not duplicate credential-looking text in a provenance header."""
    if re.search(
        r"(?i)(?:token|secret|password|api[_ -]?key|authorization)\s*[:=]|"
        r"\bsk-[a-zA-Z0-9_-]+|\b\d{5,}:[a-zA-Z0-9_-]{20,}|"
        r"\bxox[a-z]-[a-zA-Z0-9-]+|"
        r"\b[a-z][a-z0-9+.-]*://[^\s/?#@]+@|"
        r"\b(?:bearer|basic)\s+\S+|"
        r"\b(?:gh[pousr]_|github_pat_)[a-zA-Z0-9_]+|"
        r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|"
        r"\beyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+",
        message,
    ):
        return "[request preview omitted: credential-like text]"
    return " ".join(message.split())[:200]


def _legacy_source_kind(
    active: SessionData | None, named: NamedSession | None, source_session_id: str
) -> str:
    """A legacy caller can only establish kind with a matching nonempty ID."""
    if named:
        return "named"
    if active and active.session_id == source_session_id:
        return "foreground"
    return "background"


async def capture_delegation(  # noqa: C901 - explicit source-kind and metadata boundaries
    orch: object,
    context: DelegationContext,
    source_session_id: str = "",
    source_session_kind: str = "",
    source_session_name: str = "",
) -> DelegationContext:
    """Snapshot the sender's actual active (or matching named) session."""
    from dataclasses import replace

    from ductor_bot.orchestrator.core import Orchestrator

    key = delegation_session_key(context.transport, context.chat_id, context.topic_id)
    context = replace(
        context,
        session_key=key.storage_key,
        session_kind=source_session_kind
        if source_session_kind in {"foreground", "named", "background"}
        else "unknown",
        session_name=source_session_name,
    )
    if not isinstance(orch, Orchestrator) or not context.chat_id:
        return context
    try:
        active = await orch._sessions.get_active(key)
        named = next(
            (
                ns
                for ns in orch._named_sessions.list_active(context.chat_id)
                if source_session_id and ns.session_id == source_session_id
            ),
            None,
        )
    except (OSError, ValueError, TypeError):
        # Missing/damaged source metadata must not misattribute a question or
        # prevent delegation. The dedicated header marks these fields unknown.
        return context
    if source_session_kind == "named":
        named = orch._named_sessions.get(context.chat_id, source_session_name)
    elif source_session_kind in {"foreground", "background"}:
        named = None
    kind = context.session_kind
    if kind == "unknown" and source_session_id:
        kind = _legacy_source_kind(active, named, source_session_id)
    session = named if kind == "named" else active if kind == "foreground" else None
    if source_session_id and (session is None or session.session_id != source_session_id):
        session = None
        if kind == "foreground":
            kind = "background"
    provider = session.provider if session else ""
    session_id = session.session_id if session else ""
    metadata = orch.paths.named_sessions_path if named else orch.paths.sessions_path
    transcript = ""
    # login.sh shares Claude session transcripts in these projects directories.
    # Only report a concrete file that exists; other providers remain unconfirmed.
    if provider == "claude" and session_id and re.fullmatch(r"[\w-]+", session_id):
        auth = orch.paths.ductor_home / ".cli-auth"
        for projects in (auth / "_shared-projects", auth / "claude" / "projects"):
            match = next((p for p in projects.glob(f"*/{session_id}.jsonl") if p.is_file()), None)
            if match:
                transcript = str(match)
                break
    return replace(
        context,
        session_key=f"{key.storage_key} @{named.name}" if named else key.storage_key,
        session_kind=kind,
        session_name=named.name if named else source_session_name,
        provider=provider,
        session_id=session_id,
        topic_name=safe_preview(active.topic_name or "") if active else "",
        transcript_path=transcript,
        metadata_path=str(metadata) if metadata.is_file() else "",
    )


def source_header(context: DelegationContext) -> str:
    """Explicit fallback context; missing facts are marked, never invented."""
    return (
        "[DELEGATION QUESTION SOURCE]\n"
        f"Delegation mode: {context.mode or 'unknown'}\n"
        f"Original chat: {context.transport}:{context.chat_id}\n"
        f"Original topic: {context.topic_id}; name: {context.topic_name or 'unconfirmed'}\n"
        f"Original session kind: {context.session_kind or 'unconfirmed'}; "
        f"name: {context.session_name or 'unconfirmed'}\n"
        f"Original session key: {context.session_key or 'unconfirmed'}\n"
        f"Provider: {context.provider or 'unconfirmed'}; "
        f"provider session ID: {context.session_id or 'unconfirmed'}\n"
        f"Delegation task/request ID: {context.request_id or 'unconfirmed'}\n"
        f"Original request preview: {safe_preview(context.request_preview) if context.request_preview else 'unconfirmed'}\n"
        f"Conversation record: {context.transcript_path or 'unconfirmed'}\n"
        f"Session metadata (not conversation history): {context.metadata_path or 'unconfirmed'}\n"
        "This question is handled in a dedicated session. If needed, find the "
        "original conversation record to understand its context. "
        f"The answer is returned only to {context.recipient}, not posted to the chat.\n"
        "[END DELEGATION QUESTION SOURCE]\n\n"
    )
