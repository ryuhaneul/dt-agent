#!/usr/bin/env python3
"""Create a new sub-agent by writing to agents.json.

The AgentSupervisor watches agents.json via FileWatcher and automatically
starts the new agent within seconds.

Usage (Telegram):
    python3 create_agent.py --name NAME --token TOKEN --users ID1,ID2 [--provider P] [--model M]

Usage (Matrix):
    python3 create_agent.py --name NAME --transport matrix \
        --homeserver URL --user-id @bot:server \
        --allowed-users @user:server [--password PASS] [--provider P] [--model M]

Usage (delegation only):
    python3 create_agent.py --name NAME --transport headless [--provider P] [--model M]

Persona (any transport): --persona TEXT | --persona-file PATH writes the agent's
PERSONA.md, --behavior TEXT | --behavior-file PATH its BEHAVIOR.md (essential
always-on principles only). An existing file is never overwritten. Without --persona
the agent starts from the blank template and asks about itself on first use.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


def _agents_path() -> Path:
    """Resolve agents.json path (always in main agent home).

    Sub-agents have DUCTOR_HOME = ~/.ductor/agents/<name>/, so we navigate
    up to the main home. Main agent's DUCTOR_HOME points directly to ~/.ductor/.
    """
    import os

    home = Path(os.environ.get("DUCTOR_HOME", str(Path.home() / ".ductor")))
    direct = home / "agents.json"
    if direct.is_file():
        return direct
    # Sub-agent: navigate up from agents/<name>/ to main home
    main_home = home.parent.parent
    main_path = main_home / "agents.json"
    if main_path.is_file() or (main_home / "config").is_dir():
        return main_path
    return direct


_CLAUDE_MODELS = ("haiku", "sonnet", "sonnet[1m]", "opus", "opus[1m]", "fable")
_GROK_MODELS = ("grok-4.5", "grok-composer-2.5-fast")


def _main_home() -> Path:
    """Resolve the main agent's DUCTOR_HOME."""
    import os

    home = Path(os.environ.get("DUCTOR_HOME", str(Path.home() / ".ductor")))
    if home.name != ".ductor" and (home.parent.parent / "config").is_dir():
        return home.parent.parent
    return home


def _resolve_codex_model(model: str, home: Path) -> str:
    """Validate/resolve a Codex model against the cached model list."""
    cache_path = home / "config" / "codex_models.json"
    if not cache_path.is_file():
        return model
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        models = data.get("models", [])
        valid_ids = [m["id"] for m in models if isinstance(m, dict)]
        if model in valid_ids:
            return model
        for m in models:
            if isinstance(m, dict) and m.get("is_default"):
                print(f"Note: '{model}' is not a valid Codex model. Using default: {m['id']}")
                return m["id"]
        if valid_ids:
            print(f"Note: '{model}' is not a valid Codex model. Using: {valid_ids[0]}")
            return valid_ids[0]
    except (json.JSONDecodeError, OSError, KeyError):
        pass
    return model


def _resolve_gemini_model(model: str, home: Path) -> str:
    """Validate/resolve a Gemini model against the cached model list."""
    cache_path = home / "config" / "gemini_models.json"
    if not cache_path.is_file():
        return model
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        valid_ids = data.get("models", [])
        if model in valid_ids:
            return model
        if valid_ids:
            print(f"Note: '{model}' is not a valid Gemini model. Using: {valid_ids[0]}")
            return valid_ids[0]
    except (json.JSONDecodeError, OSError):
        pass
    return model


def _resolve_model(provider: str | None, model: str | None) -> str | None:
    """Validate model name against known models for the given provider.

    Catches common mistakes like ``--model codex`` (provider name, not a model).
    Uses cached model lists from ``config/codex_models.json`` and
    ``config/gemini_models.json`` and hardcoded Claude models.
    """
    if model is None or provider is None:
        return model

    home = _main_home()

    if provider == "claude":
        if model in _CLAUDE_MODELS:
            return model
        print(f"Note: '{model}' is not a valid Claude model. Using: sonnet")
        return "sonnet"

    if provider in ("openai", "codex"):
        return _resolve_codex_model(model, home)

    if provider == "gemini":
        return _resolve_gemini_model(model, home)

    if provider == "grok":
        if model in _GROK_MODELS or model.startswith("grok-"):
            return model
        print(f"Note: '{model}' is not a valid Grok model. Using: grok-4.5")
        return "grok-4.5"

    return model


def _read_text_option(
    parser: argparse.ArgumentParser, text: str | None, file: str | None, label: str
) -> str | None:
    """Return the text given inline or via a file (not both); None when neither is set."""
    if text is not None and file is not None:
        parser.error(f"--{label} and --{label}-file are mutually exclusive.")
    if file is not None:
        try:
            text = Path(file).read_text(encoding="utf-8")
        except OSError as exc:
            parser.error(f"cannot read --{label}-file: {exc}")
    if text is not None and not text.strip():
        parser.error(f"--{label} content is empty.")
    return text


def _workspace_boundary_error(main_home: Path, name: str, filenames: list[str]) -> str | None:
    """Return a message when the new agent's workspace (or a target file) would resolve
    outside ``<main_home>/agents/<name>/workspace`` through a symlink, else ``None``.

    Checks every existing component of the chain and each target file, including broken
    symlinks (``Path.exists`` is false for those, ``is_symlink`` is not).
    """
    base = main_home.resolve()
    relative = Path("agents") / name / "workspace"
    chain = [main_home / relative.parents[1], main_home / relative.parent, main_home / relative]
    chain += [main_home / relative / filename for filename in filenames]
    for path in chain:
        if not (path.is_symlink() or path.exists()):
            continue
        expected = base / path.relative_to(main_home)
        if path.resolve() != expected:
            return f"{path} resolves outside the expected agent workspace ({expected})"
    return None


def _write_persona_files(
    main_home: Path, name: str, persona: str | None, behavior: str | None
) -> list[str]:
    """Write PERSONA.md / BEHAVIOR.md into a new agent workspace, never overwriting."""
    notes: list[str] = []
    wanted = [f for f, text in (("PERSONA.md", persona), ("BEHAVIOR.md", behavior)) if text]
    if not wanted:
        return notes
    problem = _workspace_boundary_error(main_home, name, wanted)
    if problem:
        print(f"Error: refusing to write persona files: {problem}", file=sys.stderr)
        sys.exit(1)
    workspace = main_home / "agents" / name / "workspace"
    for filename, text in (("PERSONA.md", persona), ("BEHAVIOR.md", behavior)):
        if text is None:
            continue
        target = workspace / filename
        if target.exists():
            notes.append(f"  {filename} already exists - kept unchanged.")
            continue
        workspace.mkdir(parents=True, exist_ok=True)
        target.write_text(text.rstrip("\n") + "\n", encoding="utf-8")
        notes.append(f"  {filename} written.")
    return notes


_MATRIX_USER_RE = re.compile(r"^@[a-z0-9._=/+-]+:[a-z0-9.-]+$", re.IGNORECASE)


def _validate_matrix_user_id(user_id: str) -> bool:
    """Validate Matrix user ID format (@localpart:domain)."""
    return bool(_MATRIX_USER_RE.match(user_id))


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a new sub-agent")
    parser.add_argument("--name", required=True, help="Agent name (lowercase, no spaces)")
    parser.add_argument(
        "--transport",
        choices=("telegram", "matrix", "headless"),
        default=None,
        help="Transport type (default: auto-detect from other flags)",
    )

    # Telegram-specific
    parser.add_argument("--token", default=None, help="Telegram bot token")
    parser.add_argument(
        "--users", default=None, help="Comma-separated Telegram user IDs (integers)"
    )

    # Matrix-specific
    parser.add_argument("--homeserver", default=None, help="Matrix homeserver URL (https://...)")
    parser.add_argument("--user-id", default=None, help="Matrix bot user ID (@bot:server)")
    parser.add_argument(
        "--password",
        default=None,
        help="Matrix account password (optional; needed for first login if no access_token)",
    )
    parser.add_argument(
        "--allowed-users",
        default=None,
        help="Comma-separated Matrix user IDs (@user:server,...)",
    )
    parser.add_argument(
        "--allowed-rooms",
        default=None,
        help="Comma-separated Matrix room IDs or aliases (!id:server or #alias:server)",
    )

    # Common
    parser.add_argument(
        "--provider",
        default=None,
        help="AI provider (claude/openai/gemini/grok)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Specific model name (e.g. gpt-5.5, opus, gemini-2.5-pro)",
    )
    parser.add_argument(
        "--description",
        default=None,
        help="Short agent description for the join notification (purpose, key commands)",
    )
    parser.add_argument(
        "--persona", default=None, help="PERSONA.md content (name, role, tone, ...)"
    )
    parser.add_argument("--persona-file", default=None, help="Read PERSONA.md content from a file")
    parser.add_argument(
        "--behavior",
        default=None,
        help="BEHAVIOR.md content (essential always-on principles only, kept minimal)",
    )
    parser.add_argument(
        "--behavior-file", default=None, help="Read BEHAVIOR.md content from a file"
    )
    args = parser.parse_args()

    persona_text = _read_text_option(parser, args.persona, args.persona_file, "persona")
    behavior_text = _read_text_option(parser, args.behavior, args.behavior_file, "behavior")

    # --- Detect transport ---
    transport = args.transport
    if transport is None:
        if args.homeserver or args.user_id:
            transport = "matrix"
        elif args.token:
            transport = "telegram"
        else:
            print(
                "Error: Specify --transport or provide --token (Telegram) / --homeserver (Matrix).",
                file=sys.stderr,
            )
            sys.exit(1)

    # --- Validate name ---
    name = args.name.lower().strip()
    if name == "main" or re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", name) is None or ".." in name:
        print(
            f"Error: Invalid agent name '{name}'. Use 1-64 lowercase letters, digits, "
            "dot, underscore, or hyphen; no '..' or 'main'.",
            file=sys.stderr,
        )
        sys.exit(1)

    # --- Transport-specific validation ---
    if transport == "telegram":
        # Reject Matrix-only flags
        for flag, label in [
            (args.homeserver, "--homeserver"),
            (args.user_id, "--user-id"),
            (args.password, "--password"),
            (args.allowed_users, "--allowed-users"),
            (args.allowed_rooms, "--allowed-rooms"),
        ]:
            if flag:
                print(f"Error: {label} is only valid with --transport matrix.", file=sys.stderr)
                sys.exit(1)

        if not args.token:
            print("Error: --token is required for Telegram agents.", file=sys.stderr)
            sys.exit(1)
        if not args.users:
            print("Error: --users is required for Telegram agents.", file=sys.stderr)
            sys.exit(1)

        try:
            user_ids = [int(uid.strip()) for uid in args.users.split(",") if uid.strip()]
        except ValueError:
            print("Error: Telegram user IDs must be integers.", file=sys.stderr)
            sys.exit(1)
        if not user_ids:
            print("Error: At least one user ID is required.", file=sys.stderr)
            sys.exit(1)

    elif transport == "headless":
        for flag, label in [
            (args.token, "--token"),
            (args.users, "--users"),
            (args.homeserver, "--homeserver"),
            (args.user_id, "--user-id"),
            (args.password, "--password"),
            (args.allowed_users, "--allowed-users"),
            (args.allowed_rooms, "--allowed-rooms"),
        ]:
            if flag is not None:
                print(f"Error: {label} is not valid with --transport headless.", file=sys.stderr)
                sys.exit(1)

    else:  # matrix
        # Reject Telegram-only flags
        if args.token:
            print("Error: --token is only valid with --transport telegram.", file=sys.stderr)
            sys.exit(1)
        if args.users:
            print(
                "Error: --users is for Telegram (integers). Use --allowed-users for Matrix.",
                file=sys.stderr,
            )
            sys.exit(1)

        if not args.homeserver:
            print("Error: --homeserver is required for Matrix agents.", file=sys.stderr)
            sys.exit(1)
        if not args.homeserver.startswith("https://"):
            print("Error: --homeserver must be an HTTPS URL.", file=sys.stderr)
            sys.exit(1)
        if not args.user_id:
            print("Error: --user-id is required for Matrix agents.", file=sys.stderr)
            sys.exit(1)
        if not _validate_matrix_user_id(args.user_id):
            print(
                f"Error: Invalid Matrix user ID '{args.user_id}'. Expected format: @localpart:domain",
                file=sys.stderr,
            )
            sys.exit(1)

        # Parse allowed users
        matrix_allowed_users: list[str] = []
        if args.allowed_users:
            for uid in args.allowed_users.split(","):
                uid = uid.strip()
                if not uid:
                    continue
                if not _validate_matrix_user_id(uid):
                    print(
                        f"Error: Invalid Matrix user ID '{uid}'. Expected format: @localpart:domain",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                matrix_allowed_users.append(uid)

        # Parse allowed rooms
        matrix_allowed_rooms: list[str] = []
        if args.allowed_rooms:
            for rid in args.allowed_rooms.split(","):
                rid = rid.strip()
                if not rid:
                    continue
                if not rid.startswith(("!", "#")):
                    print(
                        f"Error: Invalid room ID '{rid}'. Must start with '!' (room ID) or '#' (alias).",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                matrix_allowed_rooms.append(rid)

    # --- Load existing agents ---
    path = _agents_path()
    agents: list[dict] = []
    if path.is_file():
        try:
            agents = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            agents = []

    # Check for duplicate
    if any(a.get("name") == name for a in agents):
        print(f"Error: Agent '{name}' already exists.", file=sys.stderr)
        sys.exit(1)

    # Normalize provider name (codex -> openai)
    provider = args.provider
    if provider == "codex":
        provider = "openai"

    # Resolve model against cached model lists
    resolved_model = _resolve_model(provider, args.model)

    # --- Build agent entry ---
    if transport == "telegram":
        entry: dict = {
            "name": name,
            "telegram_token": args.token,
            "allowed_user_ids": user_ids,
        }
    elif transport == "headless":
        entry = {"name": name, "transport": "headless"}
    else:  # matrix
        matrix_cfg: dict[str, object] = {
            "homeserver": args.homeserver,
            "user_id": args.user_id,
            "allowed_rooms": matrix_allowed_rooms,
            "allowed_users": matrix_allowed_users,
            "store_path": "matrix_store",
        }
        if args.password:
            matrix_cfg["password"] = args.password
        entry = {
            "name": name,
            "transport": "matrix",
            "matrix": matrix_cfg,
        }

    if provider:
        entry["provider"] = provider
    if resolved_model:
        entry["model"] = resolved_model

    agents.append(entry)

    # Persona files go in BEFORE agents.json: the FileWatcher starts the agent within
    # seconds and its workspace init would otherwise seed the blank template first.
    if args.description:
        problem = _workspace_boundary_error(_main_home(), name, ["JOIN_NOTIFICATION.md"])
        if problem:
            print(f"Error: refusing to write JOIN_NOTIFICATION.md: {problem}", file=sys.stderr)
            sys.exit(1)
    persona_notes = _write_persona_files(_main_home(), name, persona_text, behavior_text)

    # Write
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(agents, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    # Write JOIN_NOTIFICATION.md if description provided
    if args.description:
        agent_workspace = _main_home() / "agents" / name / "workspace"
        agent_workspace.mkdir(parents=True, exist_ok=True)
        notification_path = agent_workspace / "JOIN_NOTIFICATION.md"
        notification_path.write_text(args.description + "\n", encoding="utf-8")
        print(f"  JOIN_NOTIFICATION.md written.")

    # --- Output ---
    print(f"Agent '{name}' created successfully.")
    for note in persona_notes:
        print(note)
    print(f"  Transport: {transport}")
    if transport == "telegram":
        print(f"  Users: {user_ids}")
    elif transport == "matrix":
        print(f"  Homeserver: {args.homeserver}")
        print(f"  User ID: {args.user_id}")
        if args.password:
            print(f"  Password: configured")
        else:
            print(f"  Password: NOT SET — add to agents.json before starting")
        if matrix_allowed_users:
            print(f"  Allowed users: {matrix_allowed_users}")
        if matrix_allowed_rooms:
            print(f"  Allowed rooms: {matrix_allowed_rooms}")
    if provider:
        print(f"  Provider: {provider}")
    if resolved_model:
        print(f"  Model: {resolved_model}")
    print(f"\nThe agent starts automatically within a few seconds.")
    if transport == "telegram":
        print("The user can open the sub-agent's bot chat in Telegram to talk to it directly.")
    elif transport == "headless":
        print("No chat — the main agent uses this sub-agent through synchronous delegation.")
    else:
        print(f"The user can message the bot at {args.user_id} in Matrix.")
        if not args.password:
            print(
                "\nWARNING: No --password provided. The agent needs either a password or "
                "an access_token in agents.json to log in. Add it before the agent starts."
            )


if __name__ == "__main__":
    main()
