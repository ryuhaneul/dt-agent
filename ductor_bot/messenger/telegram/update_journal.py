"""Telegram updates that were received but not started yet, and those that were started.

One file holds both sets, so moving an update from "received" to "started" is a single
atomic save. An id is in one of them only; if a file ever shows an id in both, "started"
wins (the update is not run again).

The journal is written before polling confirms an update to the server, so a restart never
loses a message that was only waiting, and an update that was already started (a ``/restart``
included) is never run a second time.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ductor_bot.infra.atomic_io import atomic_text_save

logger = logging.getLogger(__name__)

HANDLED_RETENTION_SECONDS = 25 * 3600.0
"""How long the id of a started update is kept (the server keeps unconfirmed ones for 24 h)."""

PENDING_TTL_SECONDS = 24 * 3600.0
"""A waiting update older than this is not replayed (the server keeps unconfirmed ones as long)."""

_VERSION = 1


@dataclass(frozen=True, slots=True)
class PendingUpdate:
    """An update that was received and not started."""

    update_id: int
    update: dict[str, Any]
    received_at: float
    chat_id: int | None
    topic_id: int | None


class UpdateJournal:
    """Pending and handled update ids of one bot, persisted in one file."""

    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time) -> None:
        self._path = path
        self._clock = clock
        self._pending: dict[int, PendingUpdate] = {}
        self._handled: dict[int, float] = {}
        self._dispatched: set[int] = set()  # given to processing during this run
        self._load()

    # -- queries ----------------------------------------------------------------------------

    def is_handled(self, update_id: int) -> bool:
        return update_id in self._handled

    def pending(self) -> list[PendingUpdate]:
        """The updates received and not started, oldest id first."""
        return [self._pending[i] for i in sorted(self._pending)]

    def claim(self, update_id: int) -> bool:
        """Whether *update_id* may be given to processing now (once per run, never if started)."""
        if update_id in self._handled or update_id in self._dispatched:
            return False
        self._dispatched.add(update_id)
        return True

    # -- changes ----------------------------------------------------------------------------

    def record_pending(
        self, update_id: int, update: dict[str, Any], chat_id: int | None, topic_id: int | None
    ) -> None:
        """Remember a received update. Returns after the file is saved.

        Every change is saved first and applied to the memory only when the save worked; a failed
        save raises ``OSError`` and leaves the journal as it was.
        """
        if update_id in self._handled or update_id in self._pending:
            return
        entry = PendingUpdate(update_id, update, self._clock(), chat_id, topic_id)
        self._commit({**self._pending, update_id: entry}, self._handled)

    def record_handled(self, update_id: int) -> None:
        """Remember an update that is never replayed (a button press)."""
        if update_id in self._handled:
            return
        pending = {i: u for i, u in self._pending.items() if i != update_id}
        self._commit(pending, {**self._handled, update_id: self._clock()})

    def expire_older_than(self, seconds: float) -> dict[tuple[int | None, int | None], int]:
        """Move the pending updates received more than *seconds* ago to handled, in one save.

        Returns how many were moved per (chat, topic).
        """
        cutoff = self._clock() - seconds
        stale = [u for u in self._pending.values() if u.received_at < cutoff]
        counts: dict[tuple[int | None, int | None], int] = {}
        pending = dict(self._pending)
        handled = dict(self._handled)
        for update in stale:
            key = (update.chat_id, update.topic_id)
            counts[key] = counts.get(key, 0) + 1
            del pending[update.update_id]
            handled[update.update_id] = self._clock()
        if stale:
            self._commit(pending, handled)
        return counts

    def mark_started(self, update_id: int) -> None:
        """Move an update from pending to handled in one save (also for an unknown id).

        If the save fails (``OSError``) the update stays pending, so nothing may be started for it.
        """
        self.record_handled(update_id)
        self._dispatched.discard(update_id)

    # -- persistence ------------------------------------------------------------------------

    def _load(self) -> None:
        try:
            raw_bytes = self._path.read_bytes()
        except FileNotFoundError:
            return
        except OSError:
            logger.warning("Telegram update journal could not be read; starting empty")
            return
        try:
            data = json.loads(raw_bytes.decode("utf-8"))
            pending = {
                int(key): PendingUpdate(
                    int(key),
                    dict(value["update"]),
                    float(value["received_at"]),
                    value.get("chat_id"),
                    value.get("topic_id"),
                )
                for key, value in data.get("pending", {}).items()
            }
            handled = {int(key): float(value) for key, value in data.get("handled", {}).items()}
        except (ValueError, TypeError, KeyError, AttributeError):
            self._keep_corrupt_file()
            return
        for update_id in handled:
            pending.pop(update_id, None)  # started wins
        self._pending, self._handled = pending, handled

    def _keep_corrupt_file(self) -> None:
        stamp = datetime.fromtimestamp(self._clock(), UTC).strftime("%Y%m%dT%H%M%SZ")
        target = self._path.with_name(f"{self._path.name}.corrupt-{stamp}")
        try:
            self._path.replace(target)
        except OSError:
            target = self._path
        logger.warning(
            "Telegram update journal is unreadable; kept as %s, starting empty", target.name
        )

    def _commit(self, pending: dict[int, PendingUpdate], handled: dict[int, float]) -> None:
        """Save the new state, and make it the state of the journal only if that worked."""
        cutoff = self._clock() - HANDLED_RETENTION_SECONDS
        handled = {i: t for i, t in handled.items() if t >= cutoff}
        self._save(pending, handled)
        self._pending, self._handled = pending, handled

    def _save(self, pending: dict[int, PendingUpdate], handled: dict[int, float]) -> None:
        data = {
            "version": _VERSION,
            "pending": {
                str(u.update_id): {
                    "update": u.update,
                    "received_at": u.received_at,
                    "chat_id": u.chat_id,
                    "topic_id": u.topic_id,
                }
                for u in pending.values()
            },
            "handled": {str(i): t for i, t in handled.items()},
        }
        atomic_text_save(self._path, json.dumps(data, ensure_ascii=False))
