"""Optional encrypted tool secrets, with unlocked material confined to process memory."""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hashlib
import json
import math
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TypeVar

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ductor_bot.infra.atomic_io import atomic_bytes_save, complete_write
from ductor_bot.infra.file_lock import exclusive_file_lock
from ductor_bot.infra.secret_store import (
    LEGACY_WEB_AUTH_KEYS,
    delete_secret,
    set_secret,
    validate_key,
    validate_value,
)

_KDF = {"name": "scrypt", "n": 32768, "r": 8, "p": 1}
_T = TypeVar("_T")


class VaultState(StrEnum):
    """Public states contain no stored names or values."""

    UNCONFIGURED = "unconfigured"
    LOCKED = "locked"
    UNLOCKED = "unlocked"


@dataclass(frozen=True, slots=True)
class VaultStatus:
    """Safe status for callers; ``remaining_seconds=None`` means no deadline."""

    state: VaultState
    ttl_seconds: int
    remaining_seconds: float | None


class VaultError(Exception):
    """Stable, value-free failure codes shared by later transport integrations."""

    def __init__(self, code: str, *, retry_after: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after


def _parse_text(text: str) -> dict[str, str]:
    """Keep the existing tool loader's quote and single-step reference semantics."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip():
            values[key.strip()] = value.strip().strip('"').strip("'")
    for key, value in list(values.items()):
        if value.startswith("$") and value[1:] in values:
            values[key] = values[value[1:]]
    return values


def _line_key(line: str) -> str | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    return stripped.partition("=")[0].strip() or None


def _read_plain(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        return ""
    except UnicodeError:
        raise VaultError("INVALID_SECRETS_FILE") from None
    except OSError:
        raise VaultError("SECRETS_IO_ERROR") from None


def _validate_master_key(master_key: str) -> None:
    try:
        valid = len(master_key) >= 8 and bool(master_key.encode("utf-8"))
    except UnicodeError:
        valid = False
    if not valid:
        raise VaultError("MASTER_KEY_TOO_SHORT")


def _derive(master_key: str, salt: bytes) -> bytes:
    try:
        encoded = master_key.encode("utf-8")
    except UnicodeError:
        raise VaultError("INVALID_MASTER_KEY") from None
    return hashlib.scrypt(encoded, salt=salt, n=32768, r=8, p=1, maxmem=64 * 1024 * 1024, dklen=32)


def _aad(header: dict[str, object]) -> bytes:
    return json.dumps(header, sort_keys=True, separators=(",", ":")).encode("ascii")


def _write_encrypted(path: Path, text: str, key: bytes, salt: bytes) -> None:
    nonce = secrets.token_bytes(12)
    header: dict[str, object] = {
        "version": 1,
        "kdf": dict(_KDF),
        "salt": base64.b64encode(salt).decode("ascii"),
        "nonce": base64.b64encode(nonce).decode("ascii"),
    }
    try:
        plaintext = text.encode("utf-8")
    except UnicodeError:
        raise VaultError("INVALID_SECRET_VALUE") from None
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, _aad(header))
    envelope = {"header": header, "ciphertext": base64.b64encode(ciphertext).decode("ascii")}
    try:
        atomic_bytes_save(path, json.dumps(envelope, sort_keys=True).encode("ascii"))
    except OSError:
        raise VaultError("SECRETS_IO_ERROR") from None


def _decode_envelope(envelope: object) -> tuple[dict[str, object], bytes, bytes, bytes]:
    if not isinstance(envelope, dict) or set(envelope) != {"header", "ciphertext"}:
        raise ValueError
    header = envelope["header"]
    if not isinstance(header, dict) or set(header) != {"version", "kdf", "salt", "nonce"}:
        raise ValueError
    if type(header["version"]) is not int or header["version"] != 1:
        raise ValueError
    kdf = header["kdf"]
    if not isinstance(kdf, dict) or kdf != _KDF:
        raise ValueError
    if any(type(kdf[name]) is not int for name in ("n", "r", "p")):
        raise ValueError
    salt = base64.b64decode(header["salt"], validate=True)
    nonce = base64.b64decode(header["nonce"], validate=True)
    ciphertext = base64.b64decode(envelope["ciphertext"], validate=True)
    if len(salt) != 16 or len(nonce) != 12 or len(ciphertext) < 16:
        raise ValueError
    return header, salt, nonce, ciphertext


def _read_encrypted(path: Path) -> tuple[dict[str, object], bytes, bytes, bytes]:
    try:
        return _decode_envelope(json.loads(path.read_bytes()))
    except OSError:
        raise VaultError("SECRETS_IO_ERROR") from None
    except (ValueError, TypeError, KeyError, UnicodeError, binascii.Error):
        raise VaultError("INVALID_SECRETS_FILE") from None


def _decrypt(path: Path, master_key: str) -> tuple[bytes, bytes, str]:
    header, salt, nonce, ciphertext = _read_encrypted(path)
    key = _derive(master_key, salt)
    try:
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, _aad(header))
    except InvalidTag:
        raise VaultError("INVALID_MASTER_KEY") from None
    try:
        text = plaintext.decode("utf-8")
    except UnicodeError:
        raise VaultError("INVALID_SECRETS_FILE") from None
    return key, salt, text


class SecretVault:
    """One per main process; all operations share one asyncio lock.

    TTL configuration is supplied by the caller and is not secret or persisted here.
    Existing plaintext access remains available until setup. The owning process must
    call ``lock`` on shutdown. Dropping Python references is not a zeroization guarantee.
    """

    def __init__(
        self,
        plaintext_path: Path,
        *,
        ttl_seconds: int = 0,
        clock: Callable[[], float] = time.monotonic,
        persist_ttl: Callable[[int], None] | None = None,
    ) -> None:
        self._validate_ttl(ttl_seconds)
        self.plaintext_path = plaintext_path
        self.encrypted_path = plaintext_path.with_name(plaintext_path.name + ".enc")
        self._configured = self.encrypted_path.exists()
        self._mutex = asyncio.Lock()
        self._clock = clock
        self._ttl_seconds = ttl_seconds
        self._persist_ttl = persist_ttl
        self._key: bytes | None = None
        self._salt: bytes | None = None
        self._text: str | None = None
        self._values: dict[str, str] = {}
        self._unlocked_at: float | None = None
        self._expiry_task: asyncio.Task[None] | None = None
        self._failures = 0
        self._retry_at = 0.0
        self._expiry_callback: Callable[[], None] | None = None

    @staticmethod
    def _validate_ttl(ttl_seconds: int) -> None:
        if type(ttl_seconds) is not int or ttl_seconds < 0:
            raise VaultError("INVALID_TTL")

    def _clear_unlocked(self) -> None:
        self._key = None
        self._salt = None
        self._text = None
        self._values.clear()
        self._unlocked_at = None
        if self._expiry_task is not None and self._expiry_task is not asyncio.current_task():
            self._expiry_task.cancel()
        self._expiry_task = None

    def _remaining(self) -> float | None:
        if self._unlocked_at is None or not self._ttl_seconds:
            return None
        return max(0.0, self._unlocked_at + self._ttl_seconds - self._clock())

    def set_expiry_callback(self, callback: Callable[[], None] | None) -> None:
        """Register a value-free notification for each transition caused by TTL expiry."""
        self._expiry_callback = callback

    def _expire(self) -> None:
        remaining = self._remaining()
        if remaining is not None and remaining <= 0:
            self._clear_unlocked()
            if self._expiry_callback is not None:
                self._expiry_callback()

    def _require_unlocked(self) -> None:
        self._expire()
        if self._key is None:
            raise VaultError("SECRETS_LOCKED" if self._configured else "MASTER_KEY_NOT_SET")

    def _schedule_expiry(self) -> None:
        if self._expiry_task is not None:
            self._expiry_task.cancel()
            self._expiry_task = None
        if self._key is not None and self._ttl_seconds:
            self._expiry_task = asyncio.create_task(self._expiry_loop(), name="secret-vault-expiry")

    async def _expiry_loop(self) -> None:
        try:
            while True:
                async with self._mutex:
                    self._expire()
                    remaining = self._remaining()
                    if remaining is None:
                        return
                await asyncio.sleep(remaining)
        except asyncio.CancelledError:
            return

    async def _worker(self, operation: Callable[..., _T], *args: object) -> _T:
        # complete_write keeps the mutex held until a cancelled thread really finishes.
        try:
            return await complete_write(operation, *args)
        except asyncio.CancelledError:
            self._configured = self._configured or self.encrypted_path.exists()
            self._clear_unlocked()
            raise

    async def _authenticate(self, master_key: str) -> tuple[bytes, bytes, str]:
        remaining = self._retry_at - self._clock()
        if remaining > 0:
            raise VaultError("SECRETS_RATE_LIMITED", retry_after=math.ceil(remaining))
        try:
            result = await self._worker(_decrypt, self.encrypted_path, master_key)
        except VaultError as exc:
            if exc.code == "INVALID_MASTER_KEY":
                self._failures = min(self._failures + 1, 7)
                self._retry_at = self._clock() + min(2 ** (self._failures - 1), 60)
            raise
        self._failures = 0
        self._retry_at = 0.0
        return result

    async def status(self) -> VaultStatus:
        """Return current status, applying any elapsed deadline first."""
        async with self._mutex:
            self._expire()
            state = VaultState.UNCONFIGURED
            if self._configured:
                state = VaultState.UNLOCKED if self._key is not None else VaultState.LOCKED
            return VaultStatus(state, self._ttl_seconds, self._remaining())

    def _setup_store(self, master_key: str) -> tuple[bytes, bytes, str]:
        with exclusive_file_lock(self.plaintext_path):
            if self.encrypted_path.exists():
                raise VaultError("MASTER_KEY_ALREADY_SET")
            text = _read_plain(self.plaintext_path)
            legacy_names = {
                line.strip().removeprefix("export ").partition("=")[0].strip()
                for line in text.splitlines()
                if "=" in line and not line.lstrip().startswith("#")
            }
            if any(key in legacy_names for key in LEGACY_WEB_AUTH_KEYS):
                raise VaultError("WEB_AUTH_MIGRATION_REQUIRED")
            salt = secrets.token_bytes(16)
            key = _derive(master_key, salt)
            _write_encrypted(self.encrypted_path, text, key, salt)
            try:
                self.plaintext_path.unlink(missing_ok=True)
            except OSError:
                # A failed migration keeps the source; never activate a partial setup.
                with contextlib.suppress(OSError):
                    self.encrypted_path.unlink()
                raise VaultError("SECRETS_IO_ERROR") from None
            return key, salt, text

    async def setup(self, master_key: str) -> None:
        """Encrypt existing plaintext before removing it; failures preserve the source."""
        async with self._mutex:
            if self._configured:
                raise VaultError("MASTER_KEY_ALREADY_SET")
            _validate_master_key(master_key)
            try:
                key, salt, text = await self._worker(self._setup_store, master_key)
            except VaultError:
                self._configured = self.encrypted_path.exists()
                raise
            self._configured = True
            self._key, self._salt, self._text = key, salt, text
            self._values = _parse_text(text)
            self._unlocked_at = self._clock()
            self._schedule_expiry()

    async def unlock(self, master_key: str) -> None:
        """Authenticate and start a fresh absolute unlock lifetime."""
        async with self._mutex:
            self._expire()
            if not self._configured:
                raise VaultError("MASTER_KEY_NOT_SET")
            key, salt, text = await self._authenticate(master_key)
            self._key, self._salt, self._text = key, salt, text
            self._values = _parse_text(text)
            self._unlocked_at = self._clock()
            self._schedule_expiry()

    async def lock(self) -> None:
        """Drop all unlocked material and cancel the expiry timer."""
        async with self._mutex:
            self._clear_unlocked()

    async def change_key(self, current_key: str, new_key: str) -> None:
        """Re-encrypt with fresh salt and nonce without extending the unlock lifetime."""
        async with self._mutex:
            self._require_unlocked()
            _validate_master_key(new_key)
            _, _, text = await self._authenticate(current_key)
            self._require_unlocked()
            salt = secrets.token_bytes(16)
            key = await self._worker(_derive, new_key, salt)
            self._require_unlocked()
            await self._worker(_write_encrypted, self.encrypted_path, text, key, salt)
            self._key, self._salt, self._text = key, salt, text
            self._values = _parse_text(text)
            self._expire()

    async def set_ttl(self, ttl_seconds: int) -> None:
        """Recalculate expiry immediately against the last successful unlock."""
        async with self._mutex:
            self._validate_ttl(ttl_seconds)
            self._expire()
            if self._persist_ttl is not None:
                await self._worker(self._persist_ttl, ttl_seconds)
            self._ttl_seconds = ttl_seconds
            self._expire()
            self._schedule_expiry()

    async def _read_values(self) -> dict[str, str]:
        if not self._configured:
            text = await self._worker(_read_plain, self.plaintext_path)
            return _parse_text(text)
        self._require_unlocked()
        return self._values

    async def get(self, key: str) -> str:
        """Return one value, distinguishing a missing value from a locked store."""
        async with self._mutex:
            values = await self._read_values()
            if not values.get(key):
                raise VaultError("SECRET_NOT_SET")
            return values[key]

    async def list_keys(self) -> list[str]:
        """Return names only; a locked encrypted store does not expose its names."""
        async with self._mutex:
            return sorted(await self._read_values())

    async def _save_text(self, text: str) -> None:
        self._require_unlocked()
        assert self._key is not None
        assert self._salt is not None
        await self._worker(_write_encrypted, self.encrypted_path, text, self._key, self._salt)
        self._text = text
        self._values = _parse_text(text)
        self._expire()

    async def set(self, key: str, value: str) -> None:
        """Persist one value atomically, retaining previous memory on a failed write."""
        async with self._mutex:
            self._expire()
            if self._configured:
                self._require_unlocked()
            reason = validate_key(key) or validate_value(value)
            if reason:
                raise VaultError(reason.upper())
            if not self._configured:
                await self._worker(set_secret, self.plaintext_path, key, value)
                return
            assert self._text is not None
            lines = [
                line for line in self._text.splitlines(keepends=True) if _line_key(line) != key
            ]
            text = "".join(lines)
            if text and not text.endswith(("\n", "\r")):
                text += "\n"
            await self._save_text(text + f"{key}={value}\n")

    async def delete(self, key: str) -> bool:
        """Remove every entry for a name, reporting whether any entry existed."""
        async with self._mutex:
            self._expire()
            if self._configured:
                self._require_unlocked()
            reason = validate_key(key)
            if reason:
                raise VaultError(reason.upper())
            if not self._configured:
                return await self._worker(delete_secret, self.plaintext_path, key)
            assert self._text is not None
            lines = self._text.splitlines(keepends=True)
            updated = [line for line in lines if _line_key(line) != key]
            if updated == lines:
                return False
            await self._save_text("".join(updated))
            return True

    async def reveal(self, key: str, master_key: str) -> str:
        """Reauthenticate for one value; revealing never refreshes the unlock time."""
        async with self._mutex:
            self._require_unlocked()
            _, _, text = await self._authenticate(master_key)
            self._require_unlocked()
            value = _parse_text(text).get(key)
            if not value:
                raise VaultError("SECRET_NOT_SET")
            return value
