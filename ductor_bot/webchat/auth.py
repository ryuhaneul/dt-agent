"""Bridge-side password, TOTP and login throttling."""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hashlib
import hmac
import json
import logging
import math
import re
import secrets
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from ductor_bot.infra.atomic_io import atomic_text_save, complete_write
from ductor_bot.infra.env_secrets import load_env_secrets
from ductor_bot.infra.file_lock import exclusive_file_lock
from ductor_bot.infra.secret_store import LEGACY_WEB_AUTH_KEYS, set_secret
from ductor_bot.webchat.credentials import (
    CredentialsError,
    CredentialStore,
    WebCredentials,
    credential_bytes,
    decrypt_seed,
    derive_seed_key,
    encrypt_seed,
)

PASSWORD_KEY, TOTP_KEY = LEGACY_WEB_AUTH_KEYS
MIN_PASSWORD_LENGTH = 12
_HASH = re.compile(r"scrypt:32768:8:1:([0-9a-f]{32}):([0-9a-f]{64})")
_WINDOW = 900
_PREAUTH_SECONDS = 300
logger = logging.getLogger(__name__)


def _auth_state(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _save_auth_state(path: Path, state: dict[str, object]) -> None:
    atomic_text_save(path, json.dumps(state))
    path.chmod(0o600)


def rotate_credential_nonce(path: Path) -> str:
    state = _auth_state(path)
    nonce = secrets.token_hex(32)
    state["credential_nonce"] = nonce
    _save_auth_state(path, state)
    return nonce


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = _password_digest(password, salt)
    return f"scrypt:32768:8:1:{salt.hex()}:{digest.hex()}"


def _password_digest(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password.encode(), salt=salt, n=32768, r=8, p=1, maxmem=64 * 1024 * 1024, dklen=32
    )


def verify_password(password: str, encoded: str) -> bool:
    match = _HASH.fullmatch(encoded)
    if match is None:
        return False
    try:
        digest = _password_digest(password, bytes.fromhex(match[1])).hex()
    except UnicodeEncodeError:
        return False
    return hmac.compare_digest(digest, match[2])


def normalize_seed(value: str) -> str:
    seed = "".join(value.split()).upper()
    if len(seed) < 16 or not re.fullmatch(r"[A-Z2-7]+", seed):
        raise ValueError("bad_secret")
    try:
        base64.b32decode(seed + "=" * (-len(seed) % 8))
    except binascii.Error:
        raise ValueError("bad_secret") from None
    return seed


def totp_code(seed: str, step: int) -> str:
    key = base64.b32decode(seed + "=" * (-len(seed) % 8))
    digest = hmac.digest(key, struct.pack(">Q", step), "sha1")
    offset = digest[-1] & 15
    number = int.from_bytes(digest[offset : offset + 4], "big") & 0x7FFFFFFF
    return f"{number % 1_000_000:06d}"


class AuthError(Exception):
    def __init__(self, code: str = "invalid", *, retry_after: int | None = None) -> None:
        super().__init__(code)
        self.payload: dict[str, object] = {"error": code}
        if retry_after is not None:
            self.payload["retry_after"] = retry_after


@dataclass(slots=True, repr=False)
class _Preauth:
    expires: float
    client_ip: str
    credentials: bytes
    seed: str | None = None
    seed_key: bytes | None = None
    seed_salt: bytes | None = None


class BridgeAuth:
    """Serialize authentication so concurrent requests cannot reuse a time step."""

    def __init__(self, store: Path, domain: str, state_path: Path) -> None:
        self.store = store
        self.state_path = state_path
        self.credential_store = CredentialStore(state_path.with_name("credentials.json"))
        self.domain = domain
        self._lock = asyncio.Lock()
        self._preauth: dict[str, _Preauth] = {}
        self._preauth_timers: dict[str, asyncio.TimerHandle] = {}
        self._failures: list[tuple[float, str]] = []
        self._ip_failures: dict[str, list[float]] = {}
        self._ip_locked: dict[str, float] = {}
        self._global_locked = 0.0

    def _read_new_credentials(self) -> WebCredentials | None:
        try:
            return self.credential_store.load()
        except CredentialsError:
            raise AuthError from None

    def credentials(self) -> bytes:
        """Return hash, encrypted seed and generation, never a plaintext seed."""
        state = _auth_state(self.state_path)
        nonce = state.get("credential_nonce")
        if not isinstance(nonce, str) or re.fullmatch(r"[0-9a-f]{64}", nonce) is None:
            nonce = rotate_credential_nonce(self.state_path)
        current = self._read_new_credentials()
        if current is not None:
            document = json.loads(credential_bytes(current))
            generation = hashlib.sha256(credential_bytes(current) + nonce.encode()).hexdigest()
            return json.dumps([current.password_hash, document["totp"], generation]).encode()
        values = load_env_secrets(self.store)
        # Legacy seed changes still revoke sessions, but its plaintext never leaves here.
        fingerprint = json.dumps([values.get(PASSWORD_KEY), values.get(TOTP_KEY), nonce]).encode()
        generation = hashlib.sha256(fingerprint).hexdigest()
        return json.dumps([values.get(PASSWORD_KEY), None, generation]).encode()

    def state(self) -> dict[str, bool]:
        current = self._read_new_credentials()
        if current is not None:
            return {
                "totp_configured": current.totp is not None,
                "password_configured": bool(current.password_hash),
            }
        values = load_env_secrets(self.store)
        return {
            "totp_configured": bool(values.get(TOTP_KEY)),
            "password_configured": bool(values.get(PASSWORD_KEY)),
        }

    def _discard_preauth(self, token: str) -> None:
        pre = self._preauth.pop(token, None)
        if pre is not None:
            pre.seed = None
            pre.seed_key = None
            pre.seed_salt = None
        timer = self._preauth_timers.pop(token, None)
        if timer is not None:
            timer.cancel()

    def _discard_all_preauth(self) -> None:
        for token in list(self._preauth):
            self._discard_preauth(token)

    def _issue_preauth(self, pre: _Preauth) -> str:
        token = secrets.token_urlsafe(32)
        self._preauth[token] = pre
        self._preauth_timers[token] = asyncio.get_running_loop().call_later(
            max(0.0, pre.expires - time.monotonic()), lambda: self._discard_preauth(token)
        )
        return token

    def _validate_preauth(self, pre: _Preauth | None) -> _Preauth:
        if pre is None:
            raise AuthError
        try:
            matches = hmac.compare_digest(pre.credentials, self.credentials())
        except AuthError:
            matches = False
        if not matches:
            for token, candidate in list(self._preauth.items()):
                if candidate is pre:
                    self._discard_preauth(token)
            raise AuthError
        return pre

    async def close(self) -> None:
        """Cancel expiry timers and discard every preauth secret on bridge shutdown."""
        async with self._lock:
            self._discard_all_preauth()

    def _check_lock(self, ip: str, now: float) -> None:
        self._failures = [(ts, addr) for ts, addr in self._failures if ts > now - _WINDOW]
        self._ip_failures = {
            addr: recent
            for addr, timestamps in self._ip_failures.items()
            if (recent := [ts for ts in timestamps if ts > now - _WINDOW])
        }
        self._ip_locked = {addr: end for addr, end in self._ip_locked.items() if end > now}
        until = max(self._global_locked, self._ip_locked.get(ip, 0))
        if until > now:
            raise AuthError("locked", retry_after=math.ceil(until - now))

    def _failed(self, ip: str, now: float) -> None:
        self._failures.append((now, ip))
        self._ip_failures.setdefault(ip, []).append(now)
        if len(self._ip_failures[ip]) >= 5:
            self._ip_locked[ip] = now + _WINDOW
        if len(self._failures) >= 20:
            self._global_locked = now + _WINDOW
        if self._global_locked > now:
            self._discard_all_preauth()
        elif self._ip_locked.get(ip, 0) > now:
            for token, pre in list(self._preauth.items()):
                if pre.client_ip == ip:
                    self._discard_preauth(token)

    def _succeeded(self, ip: str) -> None:
        self._ip_failures.pop(ip, None)
        self._ip_locked.pop(ip, None)

    def _save_used(self, fingerprint: str, step: int, source: str) -> None:
        # Preserve a concurrent CLI nonce rotation while updating the TOTP high-water mark.
        with exclusive_file_lock(self.credential_store.path):
            state = _auth_state(self.state_path)
            state.update({"seed_fingerprint": fingerprint, "last_step": step, "source": source})
            _save_auth_state(self.state_path, state)

    def _load_used(self, fingerprint: str, step: int) -> tuple[int, str]:
        try:
            used = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            used = None
        if (
            isinstance(used, dict)
            and used.get("seed_fingerprint") == fingerprint
            and type(used.get("last_step")) is int
            and used["last_step"] >= 0
            and used.get("source") in ("setup", "login", "recovery")
        ):
            return used["last_step"], used["source"]
        # A missing high-water mark cannot prove that any currently valid code is unused.
        logger.warning("Webchat authentication state unavailable; blocking the live TOTP window")
        self._save_used(fingerprint, step + 1, "recovery")
        return step + 1, "recovery"

    def _verify_code(self, seed: str, code: str, *, setup: bool = False) -> bool:
        if not re.fullmatch(r"[0-9]{6}", code):
            return False
        try:
            seed = normalize_seed(seed)
        except ValueError:
            return False
        fingerprint = hashlib.sha256(seed.encode()).hexdigest()
        step = int(time.time() // 30)
        # Enrollment only runs without a stored seed, including after reset-totp.
        last_step, source = (-1, "") if setup else self._load_used(fingerprint, step)
        for candidate in (step - 1, step, step + 1):
            if candidate < 0 or not hmac.compare_digest(totp_code(seed, candidate), code):
                continue
            if candidate <= last_step:
                if candidate == last_step and source == "setup":
                    raise AuthError("code_reused")
                return False
            # Persist before granting access, so restarting the bot cannot replay a code.
            self._save_used(fingerprint, candidate, "setup" if setup else "login")
            return True
        return False

    def _expire_preauth(self, now: float) -> None:
        for token, pending in list(self._preauth.items()):
            if pending.expires <= now:
                self._discard_preauth(token)

    async def handle(self, action: str, data: dict[str, object]) -> dict[str, object]:  # noqa: C901
        async with self._lock:
            now = time.monotonic()
            self._expire_preauth(now)
            token = data.get("preauth")
            pre = self._preauth.get(token) if isinstance(token, str) else None
            ip = data.get("client_ip", pre.client_ip if pre else "")
            ip = ip if isinstance(ip, str) else ""
            if action == "logout":
                if isinstance(token, str):
                    self._discard_preauth(token)
                return {"ok": True}
            self._check_lock(ip, now)
            try:
                try:
                    if pre is not None:
                        self._validate_preauth(pre)
                    result = await self._dispatch(action, data, pre, ip, now)
                except (CredentialsError, UnicodeError):
                    raise AuthError from None
            except asyncio.CancelledError:
                if isinstance(token, str):
                    self._discard_preauth(token)
                raise
            except AuthError as exc:
                if pre is not None:
                    with contextlib.suppress(AuthError):
                        self._validate_preauth(pre)
                if action in {
                    "password",
                    "totp",
                    "setup/confirm",
                    "password-change",
                } and exc.payload["error"] not in {"code_reused", "password_too_short"}:
                    self._failed(ip, now)
                raise
            if action in {"totp", "setup/confirm", "password-change"}:
                self._succeeded(ip)
            return result

    async def _dispatch(
        self, action: str, data: dict[str, object], pre: _Preauth | None, ip: str, now: float
    ) -> dict[str, object]:
        with exclusive_file_lock(self.credential_store.path):
            snapshot = self.credentials()
            current = self._read_new_credentials()
            values = load_env_secrets(self.store) if current is None else {}
        if current is None:
            return await self._dispatch_legacy(action, data, pre, ip, now, snapshot, values)
        return await self._dispatch_encrypted(action, data, pre, ip, current, snapshot)

    def _assert_generation(self, snapshot: bytes) -> None:
        if not hmac.compare_digest(snapshot, self.credentials()):
            raise AuthError

    def _save_credentials(self, credentials: WebCredentials, snapshot: bytes) -> None:
        # CLI edits, migration and web writes share the credentials companion lock.
        with exclusive_file_lock(self.credential_store.path):
            self._assert_generation(snapshot)
            self.credential_store.save(credentials)
            rotate_credential_nonce(self.state_path)

    def _save_legacy_password(self, encoded: str, snapshot: bytes) -> None:
        with exclusive_file_lock(self.credential_store.path):
            self._assert_generation(snapshot)
            set_secret(self.store, PASSWORD_KEY, encoded)
            rotate_credential_nonce(self.state_path)

    async def _dispatch_encrypted(  # noqa: C901, PLR0912, PLR0913, PLR0917
        self,
        action: str,
        data: dict[str, object],
        pre: _Preauth | None,
        ip: str,
        current: WebCredentials,
        snapshot: bytes,
    ) -> dict[str, object]:
        if action == "password-change":
            return await self._change_encrypted_password(data, ip, current, snapshot)
        if action == "password":
            password = data.get("password")
            if (
                not isinstance(password, str)
                or not ip
                or not await asyncio.to_thread(verify_password, password, current.password_hash)
            ):
                raise AuthError
            if current.totp is not None:
                seed, key, salt = await asyncio.to_thread(decrypt_seed, current.totp, password)
            else:
                seed = None
                salt = secrets.token_bytes(16)
                key = await asyncio.to_thread(derive_seed_key, password, salt)
            with exclusive_file_lock(self.credential_store.path):
                self._assert_generation(snapshot)
                token = self._issue_preauth(
                    _Preauth(
                        time.monotonic() + _PREAUTH_SECONDS,
                        ip,
                        snapshot,
                        seed=seed,
                        seed_key=key,
                        seed_salt=salt,
                    )
                )
            return {"preauth": token, "next": "totp" if current.totp else "setup"}
        if action.startswith("setup/") and current.totp is not None:
            raise AuthError("totp_configured")
        pre = self._validate_preauth(pre)
        if action == "setup/begin":
            if data.get("mode") == "new":
                pending = base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")
            elif data.get("mode") == "paste" and isinstance(data.get("secret"), str):
                try:
                    pending = normalize_seed(str(data["secret"]))
                except ValueError:
                    raise AuthError("bad_secret") from None
            else:
                raise AuthError("bad_secret")
            pre.seed = pending
            uri = (
                f"otpauth://totp/dt-agent:{quote(self.domain, safe='')}?secret={pending}"
                "&issuer=dt-agent&algorithm=SHA1&digits=6&period=30"
            )
            return {"secret": pending, "otpauth_uri": uri}
        if action not in {"totp", "setup/confirm"} or (action == "totp" and current.totp is None):
            raise AuthError
        candidate = pre.seed
        code = data.get("code")
        if ip != pre.client_ip or not candidate or not isinstance(code, str):
            raise AuthError
        if not self._verify_code(candidate, code, setup=action == "setup/confirm"):
            raise AuthError
        if action == "setup/confirm":
            if pre.seed_key is None or pre.seed_salt is None:
                raise AuthError
            record = encrypt_seed(candidate, pre.seed_key, pre.seed_salt)
            await complete_write(
                self._save_credentials,
                WebCredentials(current.password_hash, record),
                pre.credentials,
            )
            self._discard_all_preauth()
        self._discard_preauth(str(data["preauth"]))
        return {"ok": True}

    async def _change_encrypted_password(
        self,
        data: dict[str, object],
        ip: str,
        current: WebCredentials,
        snapshot: bytes,
    ) -> dict[str, object]:
        password = data.get("current_password")
        if (
            not data.get("client_ip")
            or not ip
            or not isinstance(password, str)
            or not await asyncio.to_thread(verify_password, password, current.password_hash)
        ):
            raise AuthError
        if current.totp is None:
            raise AuthError("totp_required")
        seed, _, _ = await asyncio.to_thread(decrypt_seed, current.totp, password)
        code = data.get("code")
        if not isinstance(code, str) or not self._verify_code(seed, code):
            raise AuthError
        new = data.get("new_password")
        if not isinstance(new, str) or len(new) < MIN_PASSWORD_LENGTH:
            raise AuthError("password_too_short")
        encoded = await asyncio.to_thread(hash_password, new)
        salt = secrets.token_bytes(16)
        key = await asyncio.to_thread(derive_seed_key, new, salt)
        record = encrypt_seed(seed, key, salt)
        await complete_write(self._save_credentials, WebCredentials(encoded, record), snapshot)
        self._discard_all_preauth()
        return {"ok": True}

    async def _migrate_legacy(
        self,
        pre: _Preauth,
        password_hash: str,
        seed: str,
        expected_seed: str | None,
    ) -> bool:
        if self.store.with_name(self.store.name + ".enc").exists():
            logger.warning("Webchat credential migration skipped: encrypted legacy store")
            return False
        if pre.seed_key is None or pre.seed_salt is None:
            logger.warning("Webchat credential migration deferred")
            return False
        try:
            record = encrypt_seed(seed, pre.seed_key, pre.seed_salt)
            await complete_write(
                self.credential_store.migrate_legacy,
                self.store,
                WebCredentials(password_hash, record),
                expected_seed,
                lambda: self._assert_generation(pre.credentials),
            )
        except (CredentialsError, OSError, ValueError):
            logger.warning("Webchat credential migration deferred")
            return False
        self._discard_all_preauth()
        return True

    async def _dispatch_legacy(  # noqa: C901, PLR0912, PLR0913, PLR0915, PLR0917
        self,
        action: str,
        data: dict[str, object],
        pre: _Preauth | None,
        ip: str,
        now: float,
        snapshot: bytes,
        values: dict[str, str],
    ) -> dict[str, object]:
        seed = values.get(TOTP_KEY)
        if action == "password-change":
            current = data.get("current_password")
            if (
                not data.get("client_ip")
                or not ip
                or not isinstance(current, str)
                or not await asyncio.to_thread(
                    verify_password, current, values.get(PASSWORD_KEY, "")
                )
            ):
                raise AuthError
            if not seed:
                raise AuthError("totp_required")
            code = data.get("code")
            if not isinstance(code, str) or not self._verify_code(seed, code):
                raise AuthError
            new = data.get("new_password")
            if not isinstance(new, str) or len(new) < MIN_PASSWORD_LENGTH:
                raise AuthError("password_too_short")
            encoded = await asyncio.to_thread(hash_password, new)
            await complete_write(self._save_legacy_password, encoded, snapshot)
            self._discard_all_preauth()
            return {"ok": True}
        if action == "password":
            password = data.get("password")
            if (
                not isinstance(password, str)
                or not ip
                or not await asyncio.to_thread(
                    verify_password, password, values.get(PASSWORD_KEY, "")
                )
            ):
                raise AuthError
            salt = secrets.token_bytes(16)
            key = await asyncio.to_thread(derive_seed_key, password, salt)
            with exclusive_file_lock(self.credential_store.path):
                self._assert_generation(snapshot)
                token = self._issue_preauth(
                    _Preauth(
                        now + _PREAUTH_SECONDS,
                        ip,
                        snapshot,
                        seed=seed,
                        seed_key=key,
                        seed_salt=salt,
                    )
                )
            return {"preauth": token, "next": "totp" if seed else "setup"}
        if action.startswith("setup/") and seed:
            raise AuthError("totp_configured")
        pre = self._validate_preauth(pre)
        if action == "setup/begin":
            mode = data.get("mode")
            if mode == "new":
                pending = base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")
            elif mode == "paste" and isinstance(data.get("secret"), str):
                try:
                    pending = normalize_seed(str(data["secret"]))
                except ValueError:
                    raise AuthError("bad_secret") from None
            else:
                raise AuthError("bad_secret")
            pre.seed = pending
            uri = (
                f"otpauth://totp/dt-agent:{quote(self.domain, safe='')}?secret={pending}"
                "&issuer=dt-agent&algorithm=SHA1&digits=6&period=30"
            )
            return {"secret": pending, "otpauth_uri": uri}
        code = data.get("code")
        candidate = pre.seed if action == "setup/confirm" else seed
        if ip != pre.client_ip or not candidate or not isinstance(code, str):
            raise AuthError
        if not self._verify_code(candidate, code, setup=action == "setup/confirm"):
            raise AuthError
        migrated = await self._migrate_legacy(pre, values.get(PASSWORD_KEY, ""), candidate, seed)
        if action == "setup/confirm" and not migrated:
            raise AuthError("credentials_unavailable")
        self._discard_preauth(str(data["preauth"]))
        return {"ok": True}
