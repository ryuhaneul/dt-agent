"""Process-owned vault and ephemeral credentials for its trusted tool children."""

from __future__ import annotations

import json
import logging
import secrets
from collections.abc import Callable
from pathlib import Path

from ductor_bot.infra.atomic_io import atomic_text_save
from ductor_bot.infra.env_secrets import main_home_of
from ductor_bot.infra.secret_store import (
    delete_secret,
    list_keys,
    set_secret,
    store_path,
)
from ductor_bot.infra.secret_vault import SecretVault, VaultError

TOKEN_ENV = "DUCTOR_SECRETS_TOKEN"  # noqa: S105
VAULT_FILE_ENV = "DUCTOR_SECRETS_VAULT_FILE"
TOKEN_HEADER = "X-Ductor-Secrets-Token"  # noqa: S105
logger = logging.getLogger(__name__)
_current: SecretRuntime | None = None


class SecretRuntime:
    """One vault and one random token per bot process lifetime; never serialized."""

    def __init__(self, home: Path, *, interagent_port: int = 8799) -> None:
        self.home = home.expanduser().absolute()
        self.interagent_port = interagent_port
        self._settings_path = home / "config" / "secret_vault.json"
        ttl = 0
        configuration_error = None
        try:
            settings = json.loads(self._settings_path.read_bytes())
            ttl = settings["ttl_seconds"]
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError):
            configuration_error = "INVALID_VAULT_CONFIG"
        if type(ttl) is not int or ttl < 0:
            configuration_error = "INVALID_VAULT_CONFIG"
        if configuration_error:
            ttl = 0
            logger.warning(
                "INVALID_VAULT_CONFIG: repair or delete config/secret_vault.json and restart"
            )
        self.token = secrets.token_urlsafe(32)
        self.vault = SecretVault(
            store_path(home),
            ttl_seconds=ttl,
            persist_ttl=self._save_ttl,
            configuration_error=configuration_error,
        )

    def _save_ttl(self, ttl_seconds: int) -> None:
        try:
            atomic_text_save(self._settings_path, json.dumps({"ttl_seconds": ttl_seconds}) + "\n")
        except OSError:
            raise VaultError("VAULT_CONFIG_SAVE_FAILED") from None


# The supervisor owns registration. Nothing is injected into the bot's os.environ.
def start_secret_runtime(home: Path, *, interagent_port: int = 8799) -> SecretRuntime:
    """Install a fresh process runtime before any agent stack starts."""
    global _current  # noqa: PLW0603
    _current = SecretRuntime(home, interagent_port=interagent_port)
    return _current


async def stop_secret_runtime(runtime: SecretRuntime) -> None:
    """Revoke child credentials and discard all unlocked material."""
    global _current  # noqa: PLW0603
    if _current is runtime:
        _current = None
    runtime.token = ""
    await runtime.vault.lock()


def secret_environment(ductor_home: Path | None = None) -> dict[str, str]:
    """Return only ephemeral token and non-secret store identity for a child env."""
    if _current is None or not _current.token:
        return {}
    if (
        ductor_home is not None
        and main_home_of(ductor_home.expanduser().absolute()) != _current.home
    ):
        return {}
    return {
        "DUCTOR_INTERAGENT_HOST": "127.0.0.1",
        "DUCTOR_INTERAGENT_PORT": str(_current.interagent_port),
        TOKEN_ENV: _current.token,
        VAULT_FILE_ENV: str(_current.vault.plaintext_path),
    }


def get_secret_vault(path: Path) -> SecretVault | None:
    """Return the process-owned vault; never open an encrypted store independently."""
    if _current is not None and path.absolute() == _current.vault.plaintext_path.absolute():
        return _current.vault
    if path.with_name(path.name + ".enc").exists():
        raise VaultError("SECRETS_LOCKED")
    return None


async def vault_list_keys(
    path: Path, *, plain: Callable[[Path], list[str]] = list_keys
) -> list[str]:
    """Use the main vault, with legacy plaintext fallback only for unencrypted stores."""
    import asyncio

    vault = get_secret_vault(path)
    return await vault.list_keys() if vault else await asyncio.to_thread(plain, path)


async def vault_set_secret(
    path: Path, key: str, value: str, *, plain: Callable[[Path, str, str], None] = set_secret
) -> None:
    """Route bot writes through the vault without creating plaintext while encrypted."""
    import asyncio

    vault = get_secret_vault(path)
    if vault:
        await vault.set(key, value)
    else:
        await asyncio.to_thread(plain, path, key, value)


async def vault_delete_secret(
    path: Path, key: str, *, plain: Callable[[Path, str], bool] = delete_secret
) -> bool:
    """Route bot deletion through the vault when that store is managed."""
    import asyncio

    vault = get_secret_vault(path)
    return await vault.delete(key) if vault else await asyncio.to_thread(plain, path, key)
