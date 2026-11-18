"""In-memory web secret input, separate from turns and public conversation data."""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ductor_bot.i18n.loader import TranslationStore
from ductor_bot.infra.env_secrets import load_env_secrets, main_home_of
from ductor_bot.infra.secret_runtime import (
    vault_delete_secret,
    vault_list_keys,
    vault_set_secret,
)
from ductor_bot.infra.secret_store import (
    delete_secret,
    list_keys,
    set_secret,
    validate_key,
    validate_value,
)
from ductor_bot.messenger.telegram.secret_input import MAX_VALUE_LENGTH, PENDING_SECONDS

logger = logging.getLogger(__name__)


@dataclass(slots=True, repr=False)
class _Pending:
    tid: str
    key: str
    stage: str
    expires_at: float


class WebSecrets:
    """Keep only key names and short-lived, single-use capabilities in memory."""

    def __init__(self, home: Path, path: Path, language: str) -> None:
        self._home = main_home_of(home)
        self._path = path
        self._strings = TranslationStore(language)
        self._pending: dict[str, _Pending] = {}
        self._lock = asyncio.Lock()

    def _text(self, name: str, **params: object) -> str:
        return self._strings.chat(f"secret.{name}").format_map(params)

    def _reply(
        self, stage: str, name: str, *, modal: bool = False, **params: object
    ) -> dict[str, Any]:
        return {
            "text": "" if modal else self._text(name, **params),
            "buttons": None,
            "secret": {"stage": stage, "message_key": f"secret.{name}", "params": params},
        }

    @staticmethod
    def safe_command(text: str) -> str:
        """Keep only recognized syntax and validated key names, never extra arguments."""
        parts = text.split(maxsplit=3)
        if len(parts) < 2 or parts[1] not in {"list", "set", "delete", "cancel"}:
            return "/secret"
        safe = f"/secret {parts[1]}"
        if parts[1] in {"set", "delete"} and len(parts) >= 3 and not validate_key(parts[2]):
            safe += f" {parts[2]}"
        return safe

    def _remove(self, tid: str) -> bool:
        tokens = [token for token, pending in self._pending.items() if pending.tid == tid]
        for token in tokens:
            del self._pending[token]
        return bool(tokens)

    def _issue(
        self, tid: str, key: str, stage: str, *, expires_at: float | None = None
    ) -> dict[str, Any]:
        # Replace the previous capability; expired capabilities never persist to disk.
        self._remove(tid)
        now = time.time()
        self._pending = {
            token: pending for token, pending in self._pending.items() if pending.expires_at > now
        }
        token = secrets.token_urlsafe(32)
        pending = _Pending(
            tid, key, stage, expires_at if expires_at is not None else now + PENDING_SECONDS
        )
        self._pending[token] = pending
        name = "prompt" if stage == "value" else stage
        result = self._reply(stage, name, modal=stage == "value", key=key)
        result["secret"].update(
            key=key,
            token=token,
            expires_at=pending.expires_at,
            expires_in=max(0, pending.expires_at - now),
        )
        return result

    async def command(self, tid: str, text: str) -> dict[str, Any]:
        """Match Telegram's command grammar without ever retaining raw arguments."""
        async with self._lock:
            try:
                return await self._command(tid, text)
            except Exception as exc:
                logger.warning("Web secret operation failed: error=%s", type(exc).__name__)
                return self._reply("error", "save_failed")

    async def _command(self, tid: str, text: str) -> dict[str, Any]:  # noqa: PLR0911
        parts = text.split(maxsplit=3)
        action = parts[1] if len(parts) > 1 else ""
        if len(parts) > 3 and action in {"set", "delete"}:
            return self._reply("error", "extra_args", modal=True)
        if action == "cancel" and len(parts) == 2:
            removed = self._remove(tid)
            return self._reply("cancelled", "cancelled" if removed else "nothing_pending")
        if action == "list" and len(parts) == 2:
            keys = await vault_list_keys(self._path, plain=list_keys)
            return (
                self._reply("list", "list", keys="\n".join(keys))
                if keys
                else self._reply("list", "list_empty")
            )
        if action not in {"set", "delete"} or len(parts) not in {2, 3}:
            return self._reply("usage", "usage")
        key = parts[2] if len(parts) == 3 else ""
        if validate_key(key):
            return self._reply("error", "invalid_key")
        exists = key in await vault_list_keys(self._path, plain=list_keys)
        if action == "delete":
            return self._issue(tid, key, "confirm_delete")
        return self._issue(tid, key, "confirm_overwrite" if exists else "value")

    async def complete(  # noqa: PLR0911 -- capability and operation outcomes
        self, tid: str, token: str, action: str, value: str | bool
    ) -> tuple[int, dict[str, Any]]:
        """Consume capabilities under one lock; a wrong topic cannot consume them."""
        async with self._lock:
            pending = self._pending.get(token)
            if pending is None or pending.expires_at <= time.time():
                self._pending.pop(token, None)
                return 409, self._reply("expired", "expired")
            if pending.tid != tid:
                return 403, {"error": "forbidden"}
            del self._pending[token]
            if (action == "value") != (pending.stage == "value"):
                return 409, {"error": "wrong_stage"}
            try:
                if action == "confirm":
                    if not value:
                        return 200, self._reply("cancelled", "cancelled")
                    if pending.stage == "confirm_overwrite":
                        return 200, self._issue(tid, pending.key, "value")
                    deleted = await vault_delete_secret(
                        self._path, pending.key, plain=delete_secret
                    )
                    return 200, self._reply(
                        "deleted", "deleted" if deleted else "delete_missing", key=pending.key
                    )
                assert isinstance(value, str)
                return 200, await self._value(pending, value)
            except Exception as exc:
                logger.warning("Web secret operation failed: error=%s", type(exc).__name__)
                result = self._issue(tid, pending.key, pending.stage, expires_at=pending.expires_at)
                failure = self._reply("error", "save_failed")
                result["text"] = failure["text"]
                result["secret"].update(message_key="secret.save_failed", params={})
                return 200, result

    async def _value(self, pending: _Pending, value: str) -> dict[str, Any]:
        reason = validate_value(value)
        if reason is not None:
            result = self._issue(pending.tid, pending.key, "value", expires_at=pending.expires_at)
            result["text"] = self._text(
                "rejected", reason=self._text(f"reason_{reason}", max=MAX_VALUE_LENGTH)
            )
            result["secret"].update(
                reason=reason,
                message_key="secret.rejected",
                params={
                    "reason": self._text(f"reason_{reason}", max=MAX_VALUE_LENGTH),
                    "reason_code": reason,
                    "max": MAX_VALUE_LENGTH,
                },
            )
            return result
        await vault_set_secret(self._path, pending.key, value, plain=set_secret)
        result = self._reply("saved", "saved", key=pending.key, length=len(value))
        if pending.key in load_env_secrets(self._home / ".env"):
            result["text"] += "\n" + self._text("saved_env_note", key=pending.key)
            result["secret"]["params"]["env_note"] = True
        return result
