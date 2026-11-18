"""Load credentials from the shared secrets store when a tool needs them.

Before master-key setup, the store is a plain ``KEY=VALUE`` file (``secrets.env``), never injected into every
CLI run, so a tool asks for exactly the keys it uses and then reads them from ``os.environ``.

Usage::

    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _secrets import load_secrets

    load_secrets("MY_API_KEY")  # name the keys you need
    token = os.environ["MY_API_KEY"]

Rules:

* Name the keys. Legacy loading without keys is supported only for an unencrypted store.
* A value that is already set in the process environment wins over the store.
* One line is ``KEY=VALUE``. Lines that start with ``#`` and empty values are skipped, matching
  quotes around a value are removed, and ``KEY=$OTHER_KEY`` takes the value of ``OTHER_KEY``
  from the same file. An ``export`` prefix is not understood.
* The file is the one named by ``DUCTOR_SECRETS_FILE``. The framework sets it to the main home's
  ``secrets.env`` for every CLI run unless you set it yourself; without it the store is
  ``/ductor/secrets.env``.
* Unencrypted missing/unreadable files give an empty result. Encrypted stores require the bot
  RPC and raise SECRETS_LOCKED when locked or unavailable. Values are never printed.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
import os
from pathlib import Path

_PATH_VARIABLE = "DUCTOR_SECRETS_FILE"
_DEFAULT_PATH = "/ductor/secrets.env"


def _store_path() -> Path:
    return Path(os.environ.get(_PATH_VARIABLE) or _DEFAULT_PATH).expanduser()


def _parse(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
    except (OSError, UnicodeError):
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key:
            values[key] = value.strip().strip('"').strip("'")
    for key, value in list(values.items()):
        if value.startswith("$") and value[1:] in values:
            values[key] = values[value[1:]]
    return values


class SecretsError(RuntimeError):
    """Safe tool-facing failure; never includes response values or credentials."""

    def __init__(self, code: str, retry_after: int | None = None) -> None:
        message = "Secrets store is locked - ask the owner to enter the master key"
        super().__init__(f"{code}: {message}" if code == "SECRETS_LOCKED" else code)
        self.code = code
        self.retry_after = retry_after


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _encrypted() -> bool:
    path = _store_path()
    return path.with_name(path.name + ".enc").exists()


def _rpc(resource: str) -> dict:
    token = os.environ.get("DUCTOR_SECRETS_TOKEN", "")
    host = os.environ.get("DUCTOR_INTERAGENT_HOST", "127.0.0.1")
    port = os.environ.get("DUCTOR_INTERAGENT_PORT", "8799")
    if not token or host != "127.0.0.1" or not port.isdecimal():
        raise SecretsError("SECRETS_LOCKED")
    expected = os.environ.get("DUCTOR_SECRETS_VAULT_FILE", "")
    if not expected or Path(expected).expanduser().absolute() != _store_path().absolute():
        raise SecretsError("SECRETS_LOCKED")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/secrets/{resource}",
        headers={"X-Ductor-Secrets-Token": token},
    )
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect).open(
            request, timeout=5
        ) as response:
            data = json.loads(response.read())
        if not isinstance(data, dict):
            raise ValueError
        return data
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise SecretsError("SECRET_NOT_SET") from None
        if exc.code == 429:
            try:
                data = json.loads(exc.read())
                retry = data.get("retry_after") if isinstance(data, dict) else None
            except (ValueError, OSError):
                retry = None
            raise SecretsError(
                "SECRETS_RATE_LIMITED", retry if type(retry) is int else None
            ) from None
        raise SecretsError("SECRETS_LOCKED") from None
    except (OSError, ValueError, urllib.error.URLError):
        raise SecretsError("SECRETS_LOCKED") from None


def secret_status() -> dict:
    """Return only status metadata; plaintext stores need no running bot."""
    if not _encrypted():
        return {"state": "unconfigured", "ttl_seconds": 0, "remaining_seconds": None}
    result = _rpc("status")
    state = result.get("state")
    if state not in {"unconfigured", "locked", "unlocked"}:
        raise SecretsError("SECRETS_LOCKED")
    return {name: result.get(name) for name in ("state", "ttl_seconds", "remaining_seconds")}


def secret_keys() -> list[str]:
    """Return store names only, without parsing values or changing the environment."""
    if _encrypted():
        names = _rpc("keys").get("keys")
        if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
            raise SecretsError("SECRETS_LOCKED")
        return sorted(set(names))
    names: set[str] = set()
    try:
        with _store_path().open("rb") as stream:
            prefix = bytearray()
            reading_name = True
            while char := stream.read(1):
                if char in (b"\n", b"\r"):
                    prefix.clear()
                    reading_name = True
                elif reading_name and char == b"=":
                    name = prefix.decode("utf-8").strip().removeprefix("export ").strip()
                    if name:
                        names.add(name)
                    prefix.clear()
                    reading_name = False
                elif reading_name:
                    # Value bytes are skipped without constructing a value or a full line.
                    # Supported key names are at most 56 characters; allow surrounding syntax.
                    if char == b"#" or len(prefix) >= 128:
                        prefix.clear()
                        reading_name = False
                    else:
                        prefix.extend(char)
    except FileNotFoundError:
        pass
    except (OSError, UnicodeError):
        raise SecretsError("SECRETS_INVENTORY_UNAVAILABLE") from None
    return sorted(names)


def store_secrets(*keys: str) -> dict[str, str]:
    """Read the store only, without consulting or modifying process environment values."""
    if not _encrypted():
        values = _parse(_store_path())
        return {key: values[key] for key in keys or tuple(values) if key in values}
    if not keys:
        raise SecretsError("SECRETS_KEYS_REQUIRED")
    values = {}
    for key in keys:
        try:
            value = _rpc(urllib.parse.quote(key, safe="")).get("value")
        except SecretsError as exc:
            if exc.code == "SECRET_NOT_SET":
                continue
            raise
        if not isinstance(value, str):
            raise SecretsError("SECRETS_LOCKED")
        values[key] = value
    return values


def load_secrets(*keys: str) -> dict[str, str]:
    """Load requested store values into the environment, preserving existing values.

    The legacy no-key form remains available only for an unencrypted store.
    """
    missing = tuple(key for key in keys if not os.environ.get(key))
    if keys and not missing:
        return {}
    store = store_secrets(*missing) if keys else store_secrets()
    loaded: dict[str, str] = {}
    for key, value in store.items():
        if value and not os.environ.get(key):
            os.environ[key] = value
            loaded[key] = value
    return loaded


def get_secret(key: str, default: str | None = None) -> str | None:
    """Return one secret: environment first, then the store, then default."""
    value = os.environ.get(key)
    return value if value else store_secrets(key).get(key, default)
