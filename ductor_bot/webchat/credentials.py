"""Atomic web login credentials with a password-encrypted TOTP seed."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import secrets
from dataclasses import asdict, dataclass
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ductor_bot.infra.atomic_io import atomic_text_save

_AAD = b"dt-agent:webchat:totp:v1"


class CredentialsError(Exception):
    """A value-free failure that must never expose passwords, keys, or seeds."""

    def __init__(self) -> None:
        super().__init__("credentials_unavailable")


@dataclass(frozen=True, slots=True, repr=False)
class EncryptedTotp:
    """Version-one scrypt/AES-256-GCM fields; all binary values use base64."""

    salt: str
    nonce: str
    ciphertext: str


@dataclass(frozen=True, slots=True, repr=False)
class WebCredentials:
    """Only a password hash and an optional encrypted TOTP record reach disk."""

    password_hash: str
    totp: EncryptedTotp | None


def derive_seed_key(password: str, salt: bytes) -> bytes:
    """Derive a seed key with a salt independent of the password hash salt."""
    try:
        return hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=32768,
            r=8,
            p=1,
            maxmem=64 * 1024 * 1024,
            dklen=32,
        )
    except (UnicodeError, ValueError):
        raise CredentialsError from None


def _binary(record: EncryptedTotp) -> tuple[bytes, bytes, bytes]:
    try:
        salt = base64.b64decode(record.salt, validate=True)
        nonce = base64.b64decode(record.nonce, validate=True)
        ciphertext = base64.b64decode(record.ciphertext, validate=True)
    except (ValueError, TypeError, binascii.Error):
        raise CredentialsError from None
    if len(salt) != 16 or len(nonce) != 12 or len(ciphertext) < 16:
        raise CredentialsError
    return salt, nonce, ciphertext


def encrypt_seed(seed: str, key: bytes, salt: bytes) -> EncryptedTotp:
    """Encrypt a seed with a preauth-derived key, retaining no plaintext in the record."""
    nonce = secrets.token_bytes(12)
    try:
        ciphertext = AESGCM(key).encrypt(nonce, seed.encode("utf-8"), _AAD)
    except (ValueError, UnicodeError):
        raise CredentialsError from None
    return EncryptedTotp(
        *(base64.b64encode(item).decode("ascii") for item in (salt, nonce, ciphertext))
    )


def decrypt_seed(record: EncryptedTotp, password: str) -> tuple[str, bytes, bytes]:
    """Return seed, derived key and salt for one short-lived preauth context."""
    salt, nonce, ciphertext = _binary(record)
    key = derive_seed_key(password, salt)
    try:
        seed = AESGCM(key).decrypt(nonce, ciphertext, _AAD).decode("utf-8")
    except (InvalidTag, UnicodeError, ValueError):
        raise CredentialsError from None
    return seed, key, salt


def credential_bytes(credentials: WebCredentials) -> bytes:
    """Stable serialization for content-based generation and atomic persistence."""
    return json.dumps(asdict(credentials), sort_keys=True, separators=(",", ":")).encode("ascii")


class CredentialStore:
    """An absent store selects legacy auth; a corrupt existing store fails closed."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> WebCredentials | None:
        try:
            value = json.loads(self.path.read_bytes())
        except FileNotFoundError:
            return None
        except (OSError, ValueError, UnicodeError):
            raise CredentialsError from None
        if not isinstance(value, dict) or set(value) != {"password_hash", "totp"}:
            raise CredentialsError
        if not isinstance(value["password_hash"], str):
            raise CredentialsError
        record = value["totp"]
        if record is not None:
            if not isinstance(record, dict) or set(record) != {"salt", "nonce", "ciphertext"}:
                raise CredentialsError
            if not all(isinstance(item, str) for item in record.values()):
                raise CredentialsError
            record = EncryptedTotp(record["salt"], record["nonce"], record["ciphertext"])
            _binary(record)
        return WebCredentials(value["password_hash"], record)

    def save(self, credentials: WebCredentials) -> None:
        """Write hash and encrypted seed together with private, atomic replacement."""
        try:
            atomic_text_save(self.path, credential_bytes(credentials).decode("ascii") + "\n")
        except OSError:
            raise CredentialsError from None
