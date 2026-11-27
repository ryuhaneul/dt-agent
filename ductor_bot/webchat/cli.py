"""Webchat credential administration and configuration checks."""

from __future__ import annotations

import contextlib
import getpass
import json
from pathlib import Path

from rich.console import Console

from ductor_bot.chat_log.settings import validate_chat_log
from ductor_bot.config import AgentConfig
from ductor_bot.home import require_main_home
from ductor_bot.i18n import init, t_rich
from ductor_bot.infra.atomic_io import atomic_bytes_save
from ductor_bot.infra.env_secrets import _parse_dotenv, clear_cache
from ductor_bot.infra.file_lock import exclusive_file_lock
from ductor_bot.infra.secret_store import (
    LEGACY_WEB_AUTH_KEYS,
    _delete_secrets_locked,
    list_keys,
    store_path,
)
from ductor_bot.webchat.auth import MIN_PASSWORD_LENGTH, hash_password, rotate_credential_nonce
from ductor_bot.webchat.credentials import CredentialsError, CredentialStore, WebCredentials
from ductor_bot.webchat.settings import validate_webchat
from ductor_bot.workspace.paths import resolve_paths

_console = Console()


def _credential_report(
    store: CredentialStore, legacy_path: Path
) -> tuple[dict[str, object], list[str]]:
    encrypted_legacy = legacy_path.with_name(legacy_path.name + ".enc").exists()
    legacy = {} if encrypted_legacy else _parse_dotenv(legacy_path)
    names = [] if encrypted_legacy else list_keys(legacy_path) if legacy_path.exists() else []
    migration_required = any(
        name.removeprefix("export ").strip() in LEGACY_WEB_AUTH_KEYS for name in names
    )
    errors: list[str] = []
    try:
        current = store.load()
    except CredentialsError:
        current = None
        errors.append("credentials_invalid")
    if current is not None:
        storage = "encrypted"
        # Keep public flags separate from sensitive-data name heuristics.
        login_configured = bool(current.password_hash)
        totp_configured = current.totp is not None
    else:
        storage = "encrypted" if errors else "legacy" if migration_required else "unconfigured"
        login_configured = bool(legacy.get(LEGACY_WEB_AUTH_KEYS[0]))
        totp_configured = bool(legacy.get(LEGACY_WEB_AUTH_KEYS[1]))
    return {
        "storage": storage,
        "password_configured": login_configured,
        "totp_configured": totp_configured,
        "migration_required": migration_required,
    }, errors


def _edit_credentials(
    command: str, store: CredentialStore, legacy_path: Path, encoded: str = ""
) -> None:
    with exclusive_file_lock(store.path), exclusive_file_lock(legacy_path):
        encrypted_legacy = legacy_path.with_name(legacy_path.name + ".enc").exists()
        before = store.path.read_bytes() if store.path.exists() else None
        try:
            if command == "set-password":
                store.save(WebCredentials(encoded, None))
            elif command == "reset-totp":
                current = store.load()
                if current is not None:
                    store.save(WebCredentials(current.password_hash, None))
            else:
                store.path.unlink(missing_ok=True)
            keys = LEGACY_WEB_AUTH_KEYS[-1:] if command == "reset-totp" else LEGACY_WEB_AUTH_KEYS
            # Setup refuses legacy web keys, so a configured vault needs no cleanup.
            if not encrypted_legacy:
                _delete_secrets_locked(legacy_path, keys)
            if command == "clear-credentials":
                store.path.with_name("auth_state.json").unlink(missing_ok=True)
            rotate_credential_nonce(store.path.with_name("auth_state.json"))
        except (OSError, ValueError, CredentialsError):
            if before is None:
                with contextlib.suppress(OSError):
                    store.path.unlink(missing_ok=True)
            else:
                atomic_bytes_save(store.path, before)
            raise
        clear_cache()


def cmd_webchat(args: list[str]) -> None:  # noqa: C901
    paths = resolve_paths()
    require_main_home()
    if "webchat" in args:
        args = args[args.index("webchat") + 1 :]
    if args not in (
        ["set-password"],
        ["reset-totp"],
        ["clear-credentials"],
        ["check-config"],
        ["check-config", "--json"],
    ):
        _console.print(t_rich("webchat.usage"))
        raise SystemExit(2)
    raw = (
        json.loads(paths.config_path.read_text(encoding="utf-8"))
        if paths.config_path.is_file()
        else {}
    )
    config = AgentConfig.model_validate(raw)
    init(config.language)
    path = store_path(paths.ductor_home)
    store = CredentialStore(paths.webchat_dir / "credentials.json")
    if args[0] == "check-config":
        credentials, credential_errors = _credential_report(store, path)
        errors = list(
            dict.fromkeys(
                [*validate_chat_log(config), *validate_webchat(config), *credential_errors]
            )
        )
        if "--json" in args:
            print(
                json.dumps(
                    {
                        "errors": errors,
                        "chat_log": {"enabled": config.chat_log.enabled},
                        "webchat": config.webchat.model_dump(mode="json"),
                        "credentials": credentials,
                    }
                )
            )
        else:
            _console.print(
                t_rich("webchat.config_errors", errors=", ".join(errors))
                if errors
                else t_rich("webchat.config_ok")
            )
            _console.print(t_rich("webchat.credentials_status", **credentials))
        if errors:
            raise SystemExit(1)
        return
    encoded = ""
    if args[0] == "set-password":
        password = getpass.getpass(t_rich("webchat.password_prompt"))
        confirmation = getpass.getpass(t_rich("webchat.password_confirm"))
        if len(password) < MIN_PASSWORD_LENGTH:
            _console.print(t_rich("webchat.password_too_short"))
            raise SystemExit(1)
        if password != confirmation:
            _console.print(t_rich("webchat.password_mismatch"))
            raise SystemExit(1)
        try:
            encoded = hash_password(password)
        except UnicodeError:
            _console.print(t_rich("webchat.credentials_failed"))
            raise SystemExit(1) from None
    try:
        _edit_credentials(args[0], store, path, encoded)
    except (OSError, CredentialsError, ValueError) as exc:
        locked = isinstance(exc, ValueError) and str(exc) == "SECRETS_LOCKED"
        code, reason = (
            ("SECRETS_LOCKED", "vault_locked")
            if locked
            else ("CREDENTIALS_UNAVAILABLE", "credentials_failed")
        )
        _console.print(f"{code}: " + t_rich(f"webchat.{reason}"))
        raise SystemExit(1) from None
    key = {
        "set-password": "password_saved",
        "reset-totp": "totp_reset",
        "clear-credentials": "credentials_cleared",
    }[args[0]]
    _console.print(t_rich(f"webchat.{key}"))
    if args[0] == "set-password":
        _console.print(t_rich("webchat.totp_reenroll_required"))
