"""Master-key state transitions behind the shared pre-journal secret input guard."""

from __future__ import annotations

import asyncio
import hmac
from dataclasses import dataclass
from typing import TYPE_CHECKING

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from ductor_bot.i18n import t
from ductor_bot.infra.secret_runtime import get_secret_vault
from ductor_bot.infra.secret_vault import SecretVault, VaultError, VaultState

if TYPE_CHECKING:
    from aiogram.types import Message

    from ductor_bot.messenger.telegram.secret_input import SecretInput, _Pending

PREFIX = "_masterkey:"
STAGES = frozenset(
    PREFIX + stage
    for stage in (
        "setup",
        "setup-confirm",
        "unlock",
        "current",
        "new",
        "new-confirm",
        "ttl",
        "ttl-key",
    )
)


def text(name: str, **kwargs: object) -> str:
    return t(f"masterkey.{name}").format_map(kwargs)


@dataclass(slots=True, repr=False)
class _Keys:
    current: str = ""
    new: str = ""
    ttl_seconds: int | None = None


class MasterKeyInput:
    """Only non-secret stages are persisted by SecretInput; keys expire in memory."""

    def __init__(self, owner: SecretInput) -> None:
        self._owner = owner
        self._keys: dict[tuple[int, int], _Keys] = {}
        self._timers: dict[tuple[int, int], asyncio.TimerHandle] = {}
        self._notifications: set[asyncio.Task[None]] = set()
        self._vault: SecretVault | None = None
        self._started = False

    def clear(self, identity: tuple[int, int]) -> None:
        self._keys.pop(identity, None)
        timer = self._timers.pop(identity, None)
        if timer is not None:
            timer.cancel()

    async def start(self) -> None:
        if self._started or not self._owner._is_main:
            return
        self._started = True
        try:
            self._vault = get_secret_vault(self._owner._store_path)
        except VaultError:
            return
        if self._vault is not None:
            status = await self._vault.status()
            self._vault.set_expiry_callback(self._expired)
            if status.state is VaultState.LOCKED:
                await self._notify("startup_locked")

    def _expired(self) -> None:
        task = asyncio.create_task(self._notify("expired_locked"), name="masterkey-lock-notify")
        self._notifications.add(task)
        task.add_done_callback(self._notifications.discard)

    async def _notify(self, name: str) -> None:
        # Security-state alerts deliberately bypass startup_targets lifecycle opt-outs.
        for user_id in tuple(self._owner._allowed_users):
            message = text(name)
            if self._vault is not None and self._vault.configuration_error:
                message += "\n" + text("invalid_config")
            await self._owner._send(user_id, message)

    async def close(self, budget_seconds: float) -> None:
        if self._vault is not None:
            self._vault.set_expiry_callback(None)
        for identity in tuple(self._keys):
            self.clear(identity)
        tasks = list(self._notifications)
        if tasks:
            _, remaining = await asyncio.wait(tasks, timeout=max(0, budget_seconds))
            for task in remaining:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        self._vault = None

    async def _stage(
        self, chat_id: int, user_id: int, stage: str, keys: _Keys | None = None
    ) -> None:
        from ductor_bot.messenger.telegram.secret_input import PENDING_SECONDS

        await self._owner._request(
            chat_id, user_id, PREFIX + stage, prompt_text=text("prompt_" + stage.replace("-", "_"))
        )
        identity = (chat_id, user_id)
        if identity in self._owner._pending:
            self._keys[identity] = keys or _Keys()
            self._timers[identity] = asyncio.get_running_loop().call_later(
                PENDING_SECONDS, self.clear, identity
            )

    async def error(self, chat_id: int, exc: VaultError) -> None:
        name = {
            "MASTER_KEY_TOO_SHORT": "too_short",
            "SECRETS_RATE_LIMITED": "rate_limited",
            "WEB_AUTH_MIGRATION_REQUIRED": "migration_required",
            "SECRETS_FILE_SYMLINK": "file_symlink",
            "INVALID_MASTER_KEY": "wrong_key",
            "MASTER_KEY_REQUIRED": "prompt_ttl_key",
            "INVALID_VAULT_CONFIG": "invalid_config",
            "SECRETS_LOCKED": "locked",
            "MASTER_KEY_NOT_SET": "not_set",
            "MASTER_KEY_ALREADY_SET": "already_set",
            "INVALID_TTL": "invalid_ttl",
        }.get(exc.code, "failed")
        await self._owner._send(chat_id, text(name, retry_after=exc.retry_after or 0))

    async def command(self, message: Message, parts: list[str], user_id: int) -> None:
        chat_id = message.chat.id
        self._owner._remove_pending(chat_id, user_id)
        action = parts[1].lower() if len(parts) > 1 else "status"
        if action == "cancel" and len(parts) == 2:
            await self._owner._send(chat_id, text("cancelled"))
            return
        if len(parts) > 2 and not (action == "ttl" and len(parts) == 3):
            await self._owner._delete(chat_id, message.message_id)
            await self._owner._send(chat_id, text("usage"))
            return
        try:
            vault = get_secret_vault(self._owner._store_path)
            if vault is None:
                await self._owner._send(chat_id, text("unavailable"))
                return
            await self._command(vault, chat_id, user_id, action, parts)
        except VaultError as exc:
            await self.error(chat_id, exc)

    async def _command(  # noqa: C901, PLR0912
        self, vault: SecretVault, chat_id: int, user_id: int, action: str, parts: list[str]
    ) -> None:
        status = await vault.status()
        if action == "status":
            remaining = (
                "—"
                if status.state is not VaultState.UNLOCKED
                else text("unlimited")
                if status.remaining_seconds is None
                else text("seconds", seconds=int(status.remaining_seconds))
            )
            ttl = (
                text("unlimited")
                if not status.ttl_seconds
                else text("seconds", seconds=status.ttl_seconds)
            )
            await self._owner._send(
                chat_id,
                text(
                    "status",
                    state=text(
                        "locked_state" if status.state is VaultState.LOCKED else status.state.value
                    ),
                    ttl=ttl,
                    remaining=remaining,
                ),
            )
        elif action == "lock":
            if status.state is VaultState.UNCONFIGURED:
                raise VaultError("MASTER_KEY_NOT_SET")
            await vault.lock()
            await self._owner._send(chat_id, text("locked"))
        elif action == "ttl":
            if len(parts) == 3:
                await self.ttl(chat_id, user_id, parts[2])
            else:
                await self._owner._send(chat_id, text("choose_ttl"), keyboard=self.keyboard())
        elif action == "setup":
            if status.state is not VaultState.UNCONFIGURED:
                raise VaultError("MASTER_KEY_ALREADY_SET")
            await self._stage(chat_id, user_id, "setup")
        elif action == "unlock":
            if status.state is VaultState.UNCONFIGURED:
                raise VaultError("MASTER_KEY_NOT_SET")
            await self._stage(chat_id, user_id, "unlock")
        elif action == "change":
            if status.state is not VaultState.UNLOCKED:
                raise VaultError(
                    "SECRETS_LOCKED" if status.state is VaultState.LOCKED else "MASTER_KEY_NOT_SET"
                )
            await self._stage(chat_id, user_id, "current")
        else:
            await self._owner._send(chat_id, text("usage"))

    async def value(self, message: Message, pending: _Pending) -> None:
        identity = (pending.chat_id, pending.user_id)
        keys = self._keys.get(identity)
        self._owner._remove_pending(*identity)
        chat_id, user_id = identity
        value = message.text
        if keys is None:
            await self._owner._send(chat_id, text("expired"))
            return
        stage = pending.key.removeprefix(PREFIX)
        if stage == "ttl":
            await self.ttl(chat_id, user_id, value or "")
            return
        if value is None:
            await self._owner._send(chat_id, text("not_text"))
            return
        try:
            await self._key_value(chat_id, user_id, stage, keys, value)
        except UnicodeError:
            await self._owner._send(chat_id, text("not_text"))
        except VaultError as exc:
            await self.error(chat_id, exc)

    async def _key_value(  # noqa: C901
        self, chat_id: int, user_id: int, stage: str, keys: _Keys, value: str
    ) -> None:
        if not value.encode("utf-8"):
            raise VaultError("MASTER_KEY_TOO_SHORT")
        vault = get_secret_vault(self._owner._store_path)
        if vault is None:
            await self._owner._send(chat_id, text("unavailable"))
            return
        if stage in {"setup", "new"}:
            if len(value) < 8:
                raise VaultError("MASTER_KEY_TOO_SHORT")
            keys.new = value
            await self._stage(
                chat_id, user_id, "setup-confirm" if stage == "setup" else "new-confirm", keys
            )
        elif stage == "current":
            keys.current = value
            await self._stage(chat_id, user_id, "new", keys)
        elif stage in {"setup-confirm", "new-confirm"}:
            if not hmac.compare_digest(keys.new.encode("utf-8"), value.encode("utf-8")):
                await self._owner._send(chat_id, text("mismatch"))
                return
            if stage == "setup-confirm":
                await vault.setup(value)
            else:
                await vault.change_key(keys.current, value)
            await self._owner._send(
                chat_id, text("setup_done" if stage == "setup-confirm" else "changed")
            )
        elif stage == "ttl-key" and keys.ttl_seconds is not None:
            await vault.set_ttl(keys.ttl_seconds, value)
            await self._owner._send(chat_id, text("ttl_saved"))
        elif stage == "unlock":
            await vault.unlock(value)
            await self._owner._send(chat_id, text("unlocked"))

    async def ttl(self, chat_id: int, user_id: int, value: str) -> None:
        if not value.isascii() or not value.isdecimal() or len(value) > 8:
            await self._owner._send(chat_id, text("invalid_ttl"))
            return
        try:
            vault = get_secret_vault(self._owner._store_path)
            if vault is None:
                await self._owner._send(chat_id, text("unavailable"))
                return
            await vault.set_ttl(int(value) * 60)
        except VaultError as exc:
            if exc.code == "MASTER_KEY_REQUIRED":
                await self._stage(chat_id, user_id, "ttl-key", _Keys(ttl_seconds=int(value) * 60))
            else:
                await self.error(chat_id, exc)
        else:
            await self._owner._send(chat_id, text("ttl_saved"))

    async def callback(self, chat_id: int, user_id: int, data: str) -> None:
        self._owner._remove_pending(chat_id, user_id)
        value = data.removeprefix("mk:ttl:")
        if value == "custom":
            await self._stage(chat_id, user_id, "ttl")
        else:
            await self.ttl(chat_id, user_id, value)

    @staticmethod
    def keyboard() -> InlineKeyboardMarkup:
        choices = [(15, "15m"), (60, "1h"), (480, "8h"), (1440, "24h"), (0, "unlimited")]
        buttons = [
            InlineKeyboardButton(
                text=text("ttl_" + label) if minutes else text("unlimited"),
                callback_data=f"mk:ttl:{minutes}",
            )
            for minutes, label in choices
        ]
        return InlineKeyboardMarkup(
            inline_keyboard=[
                buttons[:2],
                buttons[2:4],
                [
                    buttons[4],
                    InlineKeyboardButton(text=text("custom"), callback_data="mk:ttl:custom"),
                ],
            ]
        )
