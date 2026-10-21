"""Track in-flight CLI turns for crash recovery.

Persists the currently running foreground turn per chat so it can be
recovered after a crash or restart.  Uses the same atomic JSON utilities
as the rest of the infra layer.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ductor_bot.infra.json_store import atomic_json_save, load_json

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class InflightTurn:
    """State of a single in-flight CLI turn."""

    chat_id: int
    provider: str
    model: str
    session_id: str
    prompt_preview: str
    started_at: str
    is_recovery: bool
    path: str  # "normal" | "background"
    # Topic of the turn (None: no topic). Files without it read as None.
    topic_id: int | None = None


def _turn_from_dict(data: dict[str, Any]) -> InflightTurn:
    """Reconstruct an InflightTurn from a JSON dict."""
    return InflightTurn(
        chat_id=int(data.get("chat_id", 0)),
        provider=str(data.get("provider", "")),
        model=str(data.get("model", "")),
        session_id=str(data.get("session_id", "")),
        prompt_preview=str(data.get("prompt_preview", "")),
        started_at=str(data.get("started_at", "")),
        is_recovery=bool(data.get("is_recovery", False)),
        path=str(data.get("path", "normal")),
        topic_id=_topic_from(data.get("topic_id")),
    )


def _topic_from(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _scope_key(chat_id: int, topic_id: int | None) -> str:
    """Record key: the chat id alone without a topic (the file's original format)."""
    return str(chat_id) if topic_id is None else f"{chat_id}:{topic_id}"


class InflightTracker:
    """Write/remove inflight state atomically for crash recovery."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._shutting_down = False
        self._stopped_before_shutdown: frozenset[tuple[int, int | None]] = frozenset()

    @property
    def shutting_down(self) -> bool:
        """Whether the owning process started shutting down (set before any cancel or kill)."""
        return self._shutting_down

    def mark_shutting_down(self, user_stopped: Iterable[tuple[int, int | None]] = ()) -> None:
        """Mark shutdown start; the first call wins.

        *user_stopped* are the (chat, topic) turns the user had already stopped (``/stop``,
        interrupt) at this point: a CLI kill after the mark is the shutdown's own and must not
        look like theirs.
        """
        if self._shutting_down:
            return
        self._stopped_before_shutdown = frozenset(user_stopped)
        self._shutting_down = True

    def active_scopes(self) -> list[tuple[int, int | None]]:
        """(chat, topic) of every recorded in-flight turn."""
        scopes = [_turn_from_dict(entry) for entry in self._load_raw().values()]
        return [(turn.chat_id, turn.topic_id) for turn in scopes]

    def keep_interrupted(self, chat_id: int, topic_id: int | None = None) -> bool:
        """Whether the record of an unfinished turn survives for the next start's notice."""
        return self._shutting_down and (chat_id, topic_id) not in self._stopped_before_shutdown

    def begin(self, turn: InflightTurn) -> None:
        """Mark a turn as in-flight (atomic write)."""
        data = self._load_raw()
        data[_scope_key(turn.chat_id, turn.topic_id)] = asdict(turn)
        atomic_json_save(self._path, {"turns": data})

    def complete(self, chat_id: int, topic_id: int | None = None) -> None:
        """Remove a completed turn (atomic write)."""
        data = self._load_raw()
        key = _scope_key(chat_id, topic_id)
        if key not in data:
            return
        del data[key]
        if data:
            atomic_json_save(self._path, {"turns": data})
        else:
            self._path.unlink(missing_ok=True)

    def load_interrupted(self, *, max_age_seconds: float) -> list[InflightTurn]:
        """Load turns that were in-flight at last shutdown.

        Filters:
        - ``is_recovery=True`` entries are never recovered (no infinite loops)
        - Entries with ``chat_id == 0`` have no chat and are dropped
        - Entries older than *max_age_seconds* are dropped
        """
        data = self._load_raw()
        now = datetime.now(UTC)
        result: list[InflightTurn] = []
        for entry in data.values():
            turn = _turn_from_dict(entry)
            if turn.is_recovery:
                continue
            # Telegram groups and forum topics have negative chat ids; 0 means no chat.
            if turn.chat_id == 0:
                continue
            try:
                started = datetime.fromisoformat(turn.started_at)
                if started.tzinfo is None:
                    started = started.replace(tzinfo=UTC)
                age = (now - started).total_seconds()
                if age > max_age_seconds:
                    continue
            except (ValueError, TypeError):
                continue
            result.append(turn)
        return result

    def clear(self) -> None:
        """Remove the inflight file entirely."""
        self._path.unlink(missing_ok=True)

    def _load_raw(self) -> dict[str, Any]:
        """Load the raw turns dict from disk."""
        raw = load_json(self._path)
        if raw is None:
            return {}
        turns = raw.get("turns")
        if not isinstance(turns, dict):
            return {}
        return dict(turns)
