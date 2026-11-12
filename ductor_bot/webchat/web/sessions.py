"""Memory-only browser sessions and strict authentication cookies."""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aiohttp import web

SESSION_COOKIE = "dtw_session"
PREAUTH_COOKIE = "dtw_pre"
ABSOLUTE_TTL = 12 * 60 * 60
IDLE_TTL = 2 * 60 * 60


@dataclass(slots=True)
class Session:
    csrf: str
    credential_generation: str
    created: float
    last_seen: float

    def remaining(self) -> float:
        now = time.monotonic()
        return min(self.created + ABSOLUTE_TTL - now, self.last_seen + IDLE_TTL - now)


class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    def create(self, credential_generation: str = "") -> str:
        self.count()
        token = secrets.token_urlsafe(32)
        now = time.monotonic()
        self._sessions[token] = Session(secrets.token_urlsafe(32), credential_generation, now, now)
        return token

    def get(self, token: str, *, touch: bool = True) -> Session | None:
        session = self._sessions.get(token)
        if session is None:
            return None
        if session.remaining() <= 0:
            self.delete(token)
            return None
        if touch:
            session.last_seen = time.monotonic()
        return session

    def delete(self, token: str) -> None:
        self._sessions.pop(token, None)

    def count(self) -> int:
        for token in list(self._sessions):
            self.get(token, touch=False)
        return len(self._sessions)


def set_preauth(response: web.StreamResponse, value: str) -> None:
    response.set_cookie(
        PREAUTH_COOKIE,
        value,
        path="/api/auth",
        max_age=300,
        httponly=True,
        secure=True,
        samesite="Strict",
    )


def clear_preauth(response: web.StreamResponse) -> None:
    response.set_cookie(
        PREAUTH_COOKIE,
        "",
        expires="Thu, 01 Jan 1970 00:00:00 GMT",
        max_age=0,
        path="/api/auth",
        httponly=True,
        secure=True,
        samesite="Strict",
    )


def set_session(response: web.StreamResponse, value: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        value,
        path="/",
        httponly=True,
        secure=True,
        samesite="Strict",
    )


def clear_session(response: web.StreamResponse) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        "",
        expires="Thu, 01 Jan 1970 00:00:00 GMT",
        max_age=0,
        path="/",
        httponly=True,
        secure=True,
        samesite="Strict",
    )
