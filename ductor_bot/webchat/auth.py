"""Bridge-side password, TOTP and login throttling."""

from __future__ import annotations

import asyncio
import base64
import binascii
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

from ductor_bot.infra.atomic_io import atomic_text_save
from ductor_bot.infra.env_secrets import load_env_secrets
from ductor_bot.infra.secret_store import set_secret

PASSWORD_KEY = "DT_WEBCHAT_PASSWORD_HASH"  # noqa: S105
TOTP_KEY = "DT_WEBCHAT_TOTP_SEED"
_HASH = re.compile(r"scrypt:32768:8:1:([0-9a-f]{32}):([0-9a-f]{64})")
_WINDOW = 900
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


@dataclass(slots=True)
class _Preauth:
    expires: float
    client_ip: str
    credentials: bytes
    seed: str | None = None


class BridgeAuth:
    """Serialize authentication so concurrent requests cannot reuse a time step."""

    def __init__(self, store: Path, domain: str, state_path: Path) -> None:
        self.store = store
        self.state_path = state_path
        self.domain = domain
        self._lock = asyncio.Lock()
        self._preauth: dict[str, _Preauth] = {}
        self._failures: list[tuple[float, str]] = []
        self._ip_failures: dict[str, list[float]] = {}
        self._ip_locked: dict[str, float] = {}
        self._global_locked = 0.0

    def credentials(self) -> bytes:
        state = _auth_state(self.state_path)
        nonce = state.get("credential_nonce")
        if not isinstance(nonce, str) or re.fullmatch(r"[0-9a-f]{64}", nonce) is None:
            nonce = rotate_credential_nonce(self.state_path)
        values = load_env_secrets(self.store)
        return json.dumps([values.get(PASSWORD_KEY), values.get(TOTP_KEY), nonce]).encode()

    def state(self) -> dict[str, bool]:
        values = load_env_secrets(self.store)
        return {
            "totp_configured": bool(values.get(TOTP_KEY)),
            "password_configured": bool(values.get(PASSWORD_KEY)),
        }

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

    def _succeeded(self, ip: str) -> None:
        self._ip_failures.pop(ip, None)
        self._ip_locked.pop(ip, None)

    def _save_used(self, fingerprint: str, step: int, source: str) -> None:
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

    async def handle(self, action: str, data: dict[str, object]) -> dict[str, object]:
        async with self._lock:
            now = time.monotonic()
            self._preauth = {key: pre for key, pre in self._preauth.items() if pre.expires > now}
            token = data.get("preauth")
            pre = self._preauth.get(token) if isinstance(token, str) else None
            ip = data.get("client_ip", pre.client_ip if pre else "")
            ip = ip if isinstance(ip, str) else ""
            self._check_lock(ip, now)
            try:
                result = await self._dispatch(action, data, pre, ip, now)
            except AuthError as exc:
                if (
                    action in {"password", "totp", "setup/confirm"}
                    and exc.payload["error"] != "code_reused"
                ):
                    self._failed(ip, now)
                raise
            if action in {"totp", "setup/confirm"}:
                self._succeeded(ip)
            return result

    async def _dispatch(  # noqa: C901
        self, action: str, data: dict[str, object], pre: _Preauth | None, ip: str, now: float
    ) -> dict[str, object]:
        values = load_env_secrets(self.store)
        seed = values.get(TOTP_KEY)
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
            token = secrets.token_urlsafe(32)
            self._preauth[token] = _Preauth(now + 300, ip, self.credentials())
            return {"preauth": token, "next": "totp" if seed else "setup"}
        if action.startswith("setup/") and seed:
            raise AuthError("totp_configured")
        if pre is None or not hmac.compare_digest(pre.credentials, self.credentials()):
            raise AuthError
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
        if action == "setup/confirm":
            await asyncio.to_thread(set_secret, self.store, TOTP_KEY, candidate)
            rotate_credential_nonce(self.state_path)
        self._preauth.pop(str(data["preauth"]), None)
        return {"ok": True}
