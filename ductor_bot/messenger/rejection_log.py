"""Bounded, rate-limited metadata logs for allowlist rejections."""

from __future__ import annotations

import logging
import time


class RejectionLog:
    """Log a rejected (chat, user) at most once every ten minutes."""

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger
        self._last_logged: dict[tuple[int | str | None, int | str | None], float] = {}

    def log(
        self,
        chat_type: str,
        chat_id: int | str | None,
        user_id: int | str | None,
        topic_id: int | str | None = None,
    ) -> None:
        now = time.monotonic()
        key = (chat_id, user_id)
        previous = self._last_logged.get(key)
        if previous is not None and now - previous < 600:
            return
        self._last_logged = {
            pair: timestamp
            for pair, timestamp in self._last_logged.items()
            if now - timestamp < 600
        }
        if len(self._last_logged) >= 1024:
            del self._last_logged[next(iter(self._last_logged))]
        self._last_logged[key] = now
        self._logger.info(
            "Allowlist rejection chat_type=%s chat_id=%s user_id=%s topic_id=%s",
            chat_type,
            chat_id,
            user_id,
            topic_id,
        )
