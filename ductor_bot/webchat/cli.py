"""Webchat credential administration and configuration checks."""

from __future__ import annotations

import getpass
import json

from rich.console import Console

from ductor_bot.chat_log.settings import validate_chat_log
from ductor_bot.config import AgentConfig
from ductor_bot.i18n import init, t_rich
from ductor_bot.infra.secret_store import delete_secret, set_secret, store_path
from ductor_bot.webchat.auth import PASSWORD_KEY, TOTP_KEY, hash_password, rotate_credential_nonce
from ductor_bot.webchat.settings import validate_webchat
from ductor_bot.workspace.paths import resolve_paths

_console = Console()


def cmd_webchat(args: list[str]) -> None:
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
    paths = resolve_paths()
    raw = (
        json.loads(paths.config_path.read_text(encoding="utf-8"))
        if paths.config_path.is_file()
        else {}
    )
    config = AgentConfig.model_validate(raw)
    init(config.language)
    if args[0] == "check-config":
        errors = list(dict.fromkeys([*validate_chat_log(config), *validate_webchat(config)]))
        if "--json" in args:
            print(
                json.dumps(
                    {
                        "errors": errors,
                        "chat_log": {"enabled": config.chat_log.enabled},
                        "webchat": config.webchat.model_dump(mode="json"),
                    }
                )
            )
        else:
            _console.print(
                t_rich("webchat.config_errors", errors=", ".join(errors))
                if errors
                else t_rich("webchat.config_ok")
            )
        if errors:
            raise SystemExit(1)
        return
    path = store_path(paths.ductor_home)
    if args[0] == "set-password":
        password = getpass.getpass(t_rich("webchat.password_prompt"))
        confirmation = getpass.getpass(t_rich("webchat.password_confirm"))
        if len(password) < 12:
            _console.print(t_rich("webchat.password_too_short"))
            raise SystemExit(1)
        if password != confirmation:
            _console.print(t_rich("webchat.password_mismatch"))
            raise SystemExit(1)
        set_secret(path, PASSWORD_KEY, hash_password(password))
        rotate_credential_nonce(paths.webchat_dir / "auth_state.json")
        _console.print(t_rich("webchat.password_saved"))
    elif args[0] == "reset-totp":
        delete_secret(path, TOTP_KEY)
        rotate_credential_nonce(paths.webchat_dir / "auth_state.json")
        _console.print(t_rich("webchat.totp_reset"))
    else:
        delete_secret(path, PASSWORD_KEY)
        delete_secret(path, TOTP_KEY)
        (paths.webchat_dir / "auth_state.json").unlink(missing_ok=True)
        rotate_credential_nonce(paths.webchat_dir / "auth_state.json")
        _console.print(t_rich("webchat.credentials_cleared"))
