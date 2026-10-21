#!/usr/bin/env python3
"""Prepare a runnable DT Agent home without starting services."""

from __future__ import annotations

import argparse
import getpass
import ipaddress
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, NoReturn
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


REPO = Path(__file__).resolve().parent.parent
# One JSON file per language: the texts a person reads (questions, errors, the final notice).
MESSAGES_DIR = Path(__file__).resolve().parent / "onboard_i18n"
SSH_REMOVED = "ssh extra was removed; openssh-client is included in the image by default"

_language = "en"
_catalogs: dict[str, dict[str, str]] = {}


def _catalog(language: str) -> dict[str, str]:
    if language not in _catalogs:
        try:
            data = json.loads((MESSAGES_DIR / f"{language}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        _catalogs[language] = data if isinstance(data, dict) else {}
    return _catalogs[language]


def set_language(language: str) -> None:
    """Choose the language of every text shown from now on (English when unknown)."""
    global _language
    _language = language if (MESSAGES_DIR / f"{language}.json").is_file() else "en"
    _fit_streams(_language)


def _fit_streams(language: str) -> None:
    """Keep the output from failing on the terminal encoding.

    A stream whose encoding cannot show every text of *language* is switched to UTF-8: the
    language was asked for, so it is shown intact rather than dropped or turned into question
    marks. Other streams keep their encoding. Values that still cannot be encoded (a path, for
    example) are written as escapes instead of raising.
    """
    everything = "".join(_catalog(language).values())
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            everything.encode(stream.encoding or "ascii")
        except (UnicodeEncodeError, LookupError):
            reconfigure(encoding="utf-8", errors="backslashreplace")
        else:
            reconfigure(errors="backslashreplace")


def message(key: str, **values: object) -> str:
    """The text for *key* in the current language; a missing key falls back to English."""
    text = _catalog(_language).get(key) or _catalog("en")[key]
    return text.format(**values) if values else text


class InputError(ValueError):
    pass


def csv_items(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def selected(arg: str | None, env_name: str, default: str = "") -> str:
    return arg if arg is not None else os.environ.get(env_name, default)


def detect_system_timezone(
    tz_env: str | None = None,
    timezone_file: Path = Path("/etc/timezone"),
    localtime_file: Path = Path("/etc/localtime"),
) -> str:
    """Select the first ZoneInfo-valid host timezone, falling back to UTC."""
    candidates = [os.environ.get("TZ", "") if tz_env is None else tz_env]
    try:
        candidates.append(timezone_file.read_text(encoding="utf-8").strip())
    except (OSError, UnicodeError):
        pass
    try:
        if localtime_file.is_symlink():
            target = str(localtime_file.resolve())
            _, marker, zone = target.partition("/zoneinfo/")
            if marker:
                candidates.append(zone)
    except (OSError, RuntimeError):
        pass
    for candidate in candidates:
        try:
            ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError):
            continue
        return candidate
    return "UTC"


def supported_languages(i18n_dir: Path | None = None) -> tuple[str, ...]:
    """Discover bundled chat languages without importing runtime dependencies."""
    directory = i18n_dir if i18n_dir is not None else REPO / "ductor_bot" / "i18n"
    return tuple(sorted(path.parent.name for path in directory.glob("*/chat.toml")))


def detect_system_language(
    environ: dict[str, str] | None = None,
    i18n_dir: Path | None = None,
) -> str:
    """Respect locale precedence; unsupported system languages fall back to English."""
    environment = os.environ if environ is None else environ
    locale = next((environment[key] for key in ("LC_ALL", "LC_MESSAGES", "LANG")
                   if environment.get(key)), "")
    language = locale.split(".", 1)[0].split("@", 1)[0].replace("-", "_").split("_", 1)[0].lower()
    return language if language in supported_languages(i18n_dir) else "en"


def enabled(arg: bool | None, env_name: str, default: bool = False) -> bool:
    if arg is not None:
        return arg
    value = os.environ.get(env_name, "true" if default else "false").lower()
    if value not in {"true", "false", "yes", "no", "1", "0", "y", "n"}:
        raise InputError(message("err_bool", name=env_name))
    return value in {"true", "yes", "1", "y"}


def ask(label: str, current: str, *, default: str = "", secret: bool = False) -> str:
    if current:
        return current
    suffix = message("ask_keep") if secret and default else f" [{default}]" if default else ""
    prompt = f"{label}{suffix}: "
    answer = (getpass.getpass(prompt) if secret else input(prompt)).strip()
    return answer or default


def read_secret(path: str, label_key: str) -> str:
    label = message(label_key)
    if not path:
        raise InputError(message("err_secret_needs_file", label=label))
    try:
        value = Path(path).expanduser().read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise InputError(message("err_secret_unreadable", label=label, reason=exc.strerror)) from None
    if not value or "\n" in value or "\r" in value:
        raise InputError(message("err_secret_one_line", label=label))
    return value


def numeric_ids(value: str, label_key: str, *, required: bool) -> list[int]:
    items = csv_items(value)
    label = message(label_key)
    if required and not items:
        raise InputError(message("err_required", label=label))
    if any(not re.fullmatch(r"-?[0-9]+", item) for item in items):
        raise InputError(message("err_numeric_ids", label=label))
    return [int(item) for item in items]


def put(path: Path, body: str, *, force: bool) -> None:
    if path.is_symlink() and not force:
        raise InputError(message("err_symlink"))
    if path.exists() and not force:
        notice = message("warn_exists", path=path)
        if path.stat().st_mode & 0o777 != 0o600:
            path.chmod(0o600)
            notice += message("warn_perm")
        print(notice, file=sys.stderr)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", delete=False,
        ) as output:
            temporary = Path(output.name)
            os.fchmod(output.fileno(), 0o600)
            output.write(body)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


SECRETS_FILE_HEADER = (
    "# Credentials that tools read at run time, one KEY=VALUE per line (no export).\n"
    "# This file is not given to CLI runs. A script asks for the keys it needs:\n"
    "#   from _secrets import load_secrets   # tools/_secrets.py\n"
    '#   load_secrets("MY_API_KEY")\n'
    "# A value already set in the environment wins. Keep this file private; never print it.\n"
)


def put_if_absent(path: Path, body: str) -> None:
    """Create *path* (mode 0600) only when nothing is there yet; anything else is left alone."""
    if path.exists() or path.is_symlink():
        return
    put(path, body, force=False)


def env_line(key: str, value: str) -> str:
    if any(char in value for char in "\r\n"):
        raise InputError(message("err_env_newline", key=key))
    return f"{key}={json.dumps(value)}\n"


# argparse's own wording for each kind of command-line error, mapped to a message key. The
# option names, values and choices it quotes are passed through unchanged.
# DOTALL: the quoted values are what the user typed and may contain line breaks.
_ARGUMENT_ERRORS = tuple(
    (re.compile(pattern, re.DOTALL), key, names)
    for pattern, key, names in (
        (r"unrecognized arguments: (.*)", "err_arg_unrecognized", ("arguments",)),
        (r"argument (.+?): expected .*", "err_arg_expected", ("option",)),
        (r"argument (.+?): invalid choice: (.+) \(choose from (.*)\)",
         "err_arg_choice", ("option", "value", "choices")),
        (r"argument (.+?): not allowed with argument (.+)", "err_arg_conflict", ("option", "other")),
        (r"argument (.+?): ignored explicit argument (.+)", "err_arg_ignored", ("option", "value")),
        (r"ambiguous option: (.+?) could match (.+)", "err_arg_ambiguous", ("option", "matches")),
    )
)


def argument_error(text: str) -> str:
    """argparse's error *text* in the current language (generic wording for unknown kinds)."""
    for pattern, key, names in _ARGUMENT_ERRORS:
        found = pattern.fullmatch(text)
        if found:
            return message(key, **dict(zip(names, found.groups())))
    # An error kind not listed above: a generic sentence and what the user typed, never
    # argparse's English wording.
    return message("err_arg_invalid", arguments=" ".join(sys.argv[1:]))


class _Formatter(argparse.HelpFormatter):
    def add_usage(self, usage: str | None, actions: Any, groups: Any, prefix: str | None = None) -> None:
        super().add_usage(usage, actions, groups, prefix or message("usage_prefix"))


class _Parser(argparse.ArgumentParser):
    def error(self, message_text: str) -> NoReturn:
        self.print_usage(sys.stderr)
        print(f"onboard: {argument_error(message_text)}", file=sys.stderr)
        raise SystemExit(2)


def parser() -> argparse.ArgumentParser:
    p = _Parser(
        description=message("help_description"),
        formatter_class=_Formatter,
        add_help=False,
        allow_abbrev=False,  # option names are read in full, also by early_language()
    )
    p._optionals.title = message("help_options")
    p.add_argument("-h", "--help", action="help", help=message("help_help"))
    p.add_argument("--no-prompt", action="store_true")
    p.add_argument("--home")
    p.add_argument("--telegram-token-file")
    p.add_argument("--allowed-users")
    p.add_argument("--allowed-groups")
    p.add_argument("--tz")
    p.add_argument("--language", help=message("help_language"))
    p.add_argument("--transport", action="append", choices=["none", "matrix", "slack"])
    p.add_argument("--matrix-homeserver")
    p.add_argument("--matrix-user-id")
    p.add_argument("--matrix-password-file")
    p.add_argument("--matrix-access-token-file")
    p.add_argument("--matrix-device-id")
    p.add_argument("--matrix-allowed-rooms")
    p.add_argument("--matrix-allowed-users")
    p.add_argument("--slack-bot-token-file")
    p.add_argument("--slack-app-token-file")
    p.add_argument("--slack-allowed-users")
    p.add_argument("--slack-allowed-channels")
    api = p.add_mutually_exclusive_group()
    api.add_argument("--api", dest="api", action="store_true", default=None)
    api.add_argument("--no-api", dest="api", action="store_false")
    webhook = p.add_mutually_exclusive_group()
    webhook.add_argument("--webhook", dest="webhook", action="store_true", default=None)
    webhook.add_argument("--no-webhook", dest="webhook", action="store_false")
    p.add_argument("--bind")
    p.add_argument("--force", action="store_true", help=message("help_force"))
    return p


def fill_missing(config: dict, defaults: dict) -> None:
    """Add new defaults without replacing saved values, including nested values."""
    for key, value in defaults.items():
        if key not in config:
            config[key] = value
        elif isinstance(value, dict) and isinstance(config[key], dict):
            fill_missing(config[key], value)


def load_config(home: Path, force: bool) -> tuple[dict, dict]:
    with (REPO / "config.example.json").open(encoding="utf-8") as fh:
        defaults = json.load(fh)
    defaults.pop("_comment", None)
    defaults.pop("_comment_project_roots", None)
    path = home / "config" / "config.json"
    if path.is_symlink():
        if not force:
            raise InputError(message("err_symlink"))
        # Keep the existing force policy: replace the link, never read its target.
        saved = {}
    elif path.exists():
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(saved, dict):
                raise ValueError("config must be an object")
        except (OSError, ValueError, UnicodeError):
            raise InputError(message("err_config_unreadable")) from None
    else:
        saved = {}
    config = json.loads(json.dumps(saved))
    fill_missing(config, defaults)
    return config, saved


def decide_language(args: argparse.Namespace, saved: dict | None = None) -> str:
    """The language of the texts: --language, ONBOARD_LANGUAGE, saved config, system locale, en.

    A value that is not a bundled language is not used here; the language check in
    :func:`collect` reports it, in the language of the next entry in this order.
    """
    languages = supported_languages()
    explicit = args.language if args.language is not None else os.environ.get("ONBOARD_LANGUAGE")
    if explicit in languages:
        return explicit
    if explicit is None and saved and saved.get("language") in languages:
        return saved["language"]
    return detect_system_language()


def option_value(arguments: list[str], name: str) -> str | None:
    """The last value given as ``name value`` or ``name=value`` (None when absent or missing)."""
    found = None
    for index, argument in enumerate(arguments):
        if argument.startswith(name + "="):
            found = argument[len(name) + 1:]
        elif argument == name:
            following = arguments[index + 1] if index + 1 < len(arguments) else ""
            found = following if following and not following.startswith("-") else None
    return found


def saved_config(home_value: str) -> dict:
    """The saved config.json when it can be read safely (not a link, a JSON object), else {}."""
    try:
        path = Path(home_value).expanduser() / "config" / "config.json"
        if path.is_symlink():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RuntimeError):
        return {}
    return data if isinstance(data, dict) else {}


def early_language(arguments: list[str]) -> str:
    """The language before argparse runs, so that even its first error is in that language.

    Same order as :func:`decide_language`; ``--language`` and ``--home`` are looked up in the
    raw arguments (abbreviated spellings of them are not).
    """
    home = selected(option_value(arguments, "--home"), "DUCTOR_HOME", str(Path.home() / ".ductor"))
    return decide_language(
        argparse.Namespace(language=option_value(arguments, "--language")), saved_config(home)
    )


def collect(args: argparse.Namespace) -> tuple[Path, dict, str, str]:
    # The language is known before the first text is shown: --language, ONBOARD_LANGUAGE, then
    # the saved language once the home is known, then the system locale.
    set_language(decide_language(args))
    if "ONBOARD_SSH" in os.environ:
        raise InputError(f"ONBOARD_SSH: {message('err_ssh_removed')}")
    prompt = not args.no_prompt and sys.stdin.isatty() and sys.stdout.isatty()
    home_value = selected(args.home, "DUCTOR_HOME", str(Path.home() / ".ductor"))
    if prompt and args.home is None and "DUCTOR_HOME" not in os.environ:
        home_value = ask(message("q_home"), "", default=home_value)
    home = Path(home_value).expanduser().resolve()
    if not home.is_absolute():
        raise InputError(message("err_home_absolute"))

    config, saved = load_config(home, args.force)
    set_language(decide_language(args, saved))
    if not saved:
        config["matrix"] = {
            "homeserver": "", "user_id": "", "password": "", "access_token": "",
            "device_id": "", "allowed_rooms": [], "allowed_users": [], "store_path": "matrix_store",
        }
        config["slack"] = {"bot_token": "", "app_token": "", "allowed_channels": [], "allowed_users": []}
        config["heartbeat"]["group_targets"] = []

    token_path = selected(args.telegram_token_file, "ONBOARD_TELEGRAM_TOKEN_FILE")
    token = (
        read_secret(token_path, "label_telegram_token") if token_path else
        ask(message("q_token"), "", default=saved.get("telegram_token", ""), secret=True)
        if prompt else saved.get("telegram_token", "")
    )
    if not token:
        raise InputError(message("err_token_required"))

    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
        raise InputError(message("err_token_format"))

    users_value = selected(args.allowed_users, "ONBOARD_ALLOWED_USERS")
    users_default = ",".join(map(str, saved.get("allowed_user_ids", [])))
    if prompt:
        users_value = ask(message("q_users"), users_value, default=users_default)
    elif args.allowed_users is None and "ONBOARD_ALLOWED_USERS" not in os.environ:
        users_value = users_default
    users = numeric_ids(users_value, "label_allowed_users", required=True)
    groups = numeric_ids(
        selected(args.allowed_groups, "ONBOARD_ALLOWED_GROUPS", ",".join(map(str, saved.get("allowed_group_ids", [])))),
        "label_allowed_groups", required=False,
    )
    tz = selected(args.tz, "ONBOARD_TZ", saved.get("user_timezone", detect_system_timezone()))
    if prompt and args.tz is None and "ONBOARD_TZ" not in os.environ:
        tz = ask(message("q_tz"), "", default=tz)
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise InputError(message("err_tz")) from None

    languages = supported_languages()
    language = selected(args.language, "ONBOARD_LANGUAGE", saved.get("language", detect_system_language()))
    if prompt and args.language is None and "ONBOARD_LANGUAGE" not in os.environ:
        language = ask(message("q_language", languages=",".join(languages)), "", default=language)
    if language not in languages:
        raise InputError(message("err_language", languages=", ".join(languages)))
    set_language(language)

    raw_transports = args.transport if args.transport is not None else csv_items(
        os.environ.get("ONBOARD_TRANSPORTS", ",".join(t for t in saved.get("transports", [saved.get("transport", "telegram")]) if t != "telegram"))
    )
    if prompt and args.transport is None and "ONBOARD_TRANSPORTS" not in os.environ:
        raw_transports = csv_items(ask(message("q_transports"), "", default=",".join(raw_transports)))
    if any(item not in {"none", "matrix", "slack"} for item in raw_transports):
        raise InputError(message("err_transports"))
    if "none" in raw_transports and len(raw_transports) > 1:
        raise InputError(message("err_none_combined"))
    transports = list(dict.fromkeys(item for item in raw_transports if item != "none"))
    if selected(args.matrix_access_token_file, "ONBOARD_MATRIX_ACCESS_TOKEN_FILE") and not selected(
        args.matrix_device_id, "ONBOARD_MATRIX_DEVICE_ID", config["matrix"].get("device_id", "")
    ):
        raise InputError(message("err_matrix_device"))

    matrix = config["matrix"]
    if "matrix" in transports:
        homeserver = selected(args.matrix_homeserver, "ONBOARD_MATRIX_HOMESERVER", matrix.get("homeserver", ""))
        user_id = selected(args.matrix_user_id, "ONBOARD_MATRIX_USER_ID", matrix.get("user_id", ""))
        password_file = selected(args.matrix_password_file, "ONBOARD_MATRIX_PASSWORD_FILE")
        access_file = selected(args.matrix_access_token_file, "ONBOARD_MATRIX_ACCESS_TOKEN_FILE")
        device_id = selected(args.matrix_device_id, "ONBOARD_MATRIX_DEVICE_ID", matrix.get("device_id", ""))
        rooms = selected(args.matrix_allowed_rooms, "ONBOARD_MATRIX_ALLOWED_ROOMS", ",".join(matrix.get("allowed_rooms", [])))
        matrix_users = selected(args.matrix_allowed_users, "ONBOARD_MATRIX_ALLOWED_USERS", ",".join(matrix.get("allowed_users", [])))
        if prompt:
            homeserver = ask(message("q_matrix_homeserver"), selected(args.matrix_homeserver, "ONBOARD_MATRIX_HOMESERVER"), default=homeserver)
            user_id = ask(message("q_matrix_user"), selected(args.matrix_user_id, "ONBOARD_MATRIX_USER_ID"), default=user_id)
            if not password_file and not access_file and not (matrix.get("password") or matrix.get("access_token")):
                password_file = ask(message("q_matrix_password_file"), "")
                if not password_file:
                    access_file = ask(message("q_matrix_access_file"), "")
            if access_file:
                device_id = ask(message("q_matrix_device"), device_id)
            if not rooms and not matrix_users:
                rooms = ask(message("q_matrix_rooms"), "")
                if not rooms:
                    matrix_users = ask(message("q_matrix_users"), "")
        if not homeserver or not user_id:
            raise InputError(message("err_matrix_basic"))
        if password_file and access_file or not (password_file or access_file or matrix.get("password") or matrix.get("access_token")):
            raise InputError(message("err_matrix_one_secret"))
        if access_file and not device_id:
            raise InputError(message("err_matrix_device"))
        if not csv_items(rooms) and not csv_items(matrix_users):
            raise InputError(message("err_matrix_allowed"))
        matrix.update({
            "homeserver": homeserver, "user_id": user_id,
            "password": read_secret(password_file, "label_matrix_password") if password_file else "" if access_file else matrix.get("password", ""),
            "access_token": read_secret(access_file, "label_matrix_access_token") if access_file else "" if password_file else matrix.get("access_token", ""),
            "device_id": device_id, "allowed_rooms": csv_items(rooms),
            "allowed_users": csv_items(matrix_users),
        })

    slack = config["slack"]
    if "slack" in transports:
        bot_file = selected(args.slack_bot_token_file, "ONBOARD_SLACK_BOT_TOKEN_FILE")
        app_file = selected(args.slack_app_token_file, "ONBOARD_SLACK_APP_TOKEN_FILE")
        channels = selected(args.slack_allowed_channels, "ONBOARD_SLACK_ALLOWED_CHANNELS", ",".join(slack.get("allowed_channels", [])))
        slack_users = selected(args.slack_allowed_users, "ONBOARD_SLACK_ALLOWED_USERS", ",".join(slack.get("allowed_users", [])))
        if prompt:
            if not slack.get("bot_token"):
                bot_file = ask(message("q_slack_bot_file"), bot_file)
            if not slack.get("app_token"):
                app_file = ask(message("q_slack_app_file"), app_file)
            if not channels and not slack_users:
                channels = ask(message("q_slack_channels"), "")
                if not channels:
                    slack_users = ask(message("q_slack_users"), "")
        if not csv_items(channels) and not csv_items(slack_users):
            raise InputError(message("err_slack_allowed"))
        slack.update({
            "bot_token": read_secret(bot_file, "label_slack_bot_token") if bot_file else slack.get("bot_token") or read_secret("", "label_slack_bot_token"),
            "app_token": read_secret(app_file, "label_slack_app_token") if app_file else slack.get("app_token") or read_secret("", "label_slack_app_token"),
            "allowed_channels": csv_items(channels), "allowed_users": csv_items(slack_users),
        })

    api = enabled(args.api, "ONBOARD_API", config["api"]["enabled"] if saved else False)
    if prompt and args.api is None and "ONBOARD_API" not in os.environ:
        api = ask(message("q_api"), "", default="y" if api else "N").lower() == "y"
    webhook = enabled(args.webhook, "ONBOARD_WEBHOOK", config["webhooks"]["enabled"] if saved else False)
    if prompt and args.webhook is None and "ONBOARD_WEBHOOK" not in os.environ:
        webhook = ask(message("q_webhook"), "", default="y" if webhook else "N").lower() == "y"
    bind = selected(args.bind, "ONBOARD_BIND", "127.0.0.1")
    if prompt and args.bind is None and "ONBOARD_BIND" not in os.environ:
        bind = ask(message("q_bind"), "", default=bind)
    try:
        ipaddress.ip_address(bind)
    except ValueError:
        raise InputError(message("err_bind")) from None

    def received(arg: object, env_name: str) -> bool:
        return not saved or prompt or arg is not None or env_name in os.environ

    for key, value, arg, env_name in (
        ("telegram_token", token, args.telegram_token_file, "ONBOARD_TELEGRAM_TOKEN_FILE"),
        ("allowed_user_ids", users, args.allowed_users, "ONBOARD_ALLOWED_USERS"),
        ("user_timezone", tz, args.tz, "ONBOARD_TZ"),
        ("language", language, args.language, "ONBOARD_LANGUAGE"),
    ):
        if received(arg, env_name):
            config[key] = value
    if not saved:
        config.update({"ductor_home": "/ductor", "transport": "telegram"})
    saved_transports = saved.get("transports", [saved.get("transport", "telegram")])
    if (not saved or args.transport is not None or "ONBOARD_TRANSPORTS" in os.environ
            or prompt and transports != [item for item in saved_transports if item != "telegram"]):
        config["transports"] = ["telegram", *transports]
    # There is deliberately no group question, even in interactive mode.
    if not saved or args.allowed_groups is not None or "ONBOARD_ALLOWED_GROUPS" in os.environ:
        config["allowed_group_ids"] = groups
    if received(args.webhook, "ONBOARD_WEBHOOK"):
        config["webhooks"]["enabled"] = webhook
    if received(args.api, "ONBOARD_API"):
        config["api"]["enabled"] = api
    if not saved:
        config["webhooks"].update({"host": "0.0.0.0" if webhook else "127.0.0.1", "token": ""})
        config["api"].update({"token": "", "chat_id": users[0]})
    extras = ",".join([*transports, *(["api"] if api else [])])
    return home, config, extras, bind


def write_port_override(config_file: Path, override_file: Path) -> None:
    """Publish only enabled main-agent listeners from the saved configuration."""
    try:
        config = json.loads(config_file.read_text(encoding="utf-8"))
        listeners = (
            ("webhooks", "DUCTOR_WEBHOOK_BIND"),
            ("api", "DUCTOR_API_BIND"),
        )
        ports = []
        for section, bind_var in listeners:
            if config[section]["enabled"] is True:
                port = int(config[section]["port"])
                if not 1 <= port <= 65535:
                    raise ValueError("invalid port")
                ports.append(
                    f'      - "${{{bind_var}:-127.0.0.1}}:{port}:{port}"\n'
                )
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise InputError(message("err_ports", error=type(exc).__name__)) from None

    if not ports:
        override_file.unlink(missing_ok=True)
        return
    put(
        override_file,
        "services:\n  ductor-bot:\n    ports:\n" + "".join(ports),
        force=True,
    )


def generated_token() -> str:
    if shutil.which("openssl"):
        result = subprocess.run(
            ["openssl", "rand", "-hex", "32"], capture_output=True, text=True, check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    return secrets.token_hex(32)


def main() -> int:
    arguments = sys.argv[1:]
    # Before argparse reads the arguments: the language of its errors and of --help.
    set_language(early_language(arguments))
    if any(argument.split("=", 1)[0] in {"--ssh", "--no-ssh"} for argument in arguments):
        print(f"onboard: {message('err_ssh_removed')}", file=sys.stderr)
        return 2
    # argparse treats a comma-separated list beginning with a negative group ID
    # as another option unless the value is attached with "=".
    for index in range(len(arguments) - 1):
        if arguments[index] == "--allowed-groups" and re.fullmatch(
            r"-[0-9]+(?:,-?[0-9]+)*", arguments[index + 1]
        ):
            arguments[index] += "=" + arguments[index + 1]
            arguments[index + 1] = ""
    args = parser().parse_args([argument for argument in arguments if argument])
    try:
        home, config, extras, bind = collect(args)
        root_env = REPO / ".env"
        home_env = home / ".env"
        shared_env = home / ".env.shared"
        config_file = home / "config" / "config.json"
        auth_dir = home / ".cli-auth"
        os.umask(0o077)
        auth_dir.mkdir(parents=True, exist_ok=True)
        auth_dir.chmod(0o700)
        (home / "logs").mkdir(parents=True, exist_ok=True)
        root_body = "".join([
            env_line("DUCTOR_HOME", str(home)),
            env_line("DUCTOR_UID", str(os.getuid())),
            env_line("DUCTOR_GID", str(os.getgid())),
            env_line("DUCTOR_EXTRAS", extras),
            env_line("DUCTOR_WEBHOOK_BIND", bind),
            env_line("DUCTOR_API_BIND", bind),
        ])
        put(root_env, root_body, force=args.force)
        put(
            home_env,
            "# Optional provider secrets. Uncomment only the keys you use.\n"
            "# ANTHROPIC_API_KEY=\n# OPENAI_API_KEY=\n# GEMINI_API_KEY=\n",
            force=args.force,
        )
        put_if_absent(home / "secrets.env", SECRETS_FILE_HEADER)
        put(
            shared_env,
            env_line("BROWSER_TOKEN", generated_token())
            + env_line("TZ", config["user_timezone"]),
            force=args.force,
        )
        put(
            config_file,
            json.dumps(config, indent=4, ensure_ascii=False) + "\n",
            force=args.force,
        )
        write_port_override(config_file, REPO / "compose.override.yml")
    except InputError as exc:
        print(f"onboard: {exc}", file=sys.stderr)
        return 2
    saved_config = json.loads(config_file.read_text(encoding="utf-8"))
    provider = saved_config.get("provider", "claude")
    print(message("out_language", language=saved_config.get("language", "en")))
    print(message("out_ready"))
    print("  docker compose up -d --build")
    print("  docker compose logs --tail=100 ductor-bot")
    print(message("out_check_online"))
    print(f"  ./scripts/login.sh {provider}")
    print("  ./scripts/status.sh")
    print(message("out_reset"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
