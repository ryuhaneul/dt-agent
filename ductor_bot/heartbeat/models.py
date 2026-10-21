"""Typed outcomes produced by heartbeat turns."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class HeartbeatStatus(StrEnum):
    """Result category for a heartbeat turn."""

    ACK = "ack"
    ALERT = "alert"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class HeartbeatOutcome:
    """Result of a heartbeat turn. Only alerts carry non-empty text.

    *marker* is the physical session state the turn ran on (provider, session id, message
    count): its session id is what a quarantine refers to. *activity* is the session's
    user-activity count at that time, the identifier that ACK dormancy is judged on.
    """

    status: HeartbeatStatus
    text: str = ""
    marker: tuple[str, str, int] | None = None
    reason: str = ""
    activity: int | None = None
