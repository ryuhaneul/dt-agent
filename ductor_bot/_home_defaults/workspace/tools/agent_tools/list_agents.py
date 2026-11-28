#!/usr/bin/env python3
"""List all registered sub-agents and their configuration.

Usage:
    python3 list_agents.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _agents_path() -> Path:
    """Resolve agents.json path (always in main agent home).

    Sub-agents have DUCTOR_HOME = ~/.ductor/agents/<name>/, so we navigate
    up to the main home.
    """
    import os

    home = Path(os.environ.get("DUCTOR_HOME", str(Path.home() / ".ductor")))
    direct = home / "agents.json"
    if direct.exists() or direct.is_symlink():
        return direct
    main_home = home.parent.parent
    main_path = main_home / "agents.json"
    if main_path.exists() or main_path.is_symlink() or (main_home / "config").is_dir():
        return main_path
    return direct


def main() -> None:
    path = _agents_path()
    if not (path.exists() or path.is_symlink()):
        print("No agents.json found. No sub-agents configured.")
        return

    try:
        agents = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    except (OSError, RecursionError, ValueError):
        agents = None
    if not isinstance(agents, list) or any(not isinstance(row, dict) for row in agents):
        print("Error: Failed to read agents.json", file=sys.stderr)
        sys.exit(1)

    if not agents:
        print("No sub-agents configured.")
        return

    print(f"Registered sub-agents ({len(agents)}):\n")
    for agent in agents:
        name = str(agent.get("name", "?"))
        users = agent.get("allowed_user_ids", [])
        provider = agent.get("provider", "(inherited)")
        model = agent.get("model", "(inherited)")

        # Check if workspace exists
        home = Path(_agents_path().parent / "agents" / name)
        workspace_status = "exists" if home.is_dir() else "not created"

        print(f"  {name}")
        print(f"    Users:     {users}")
        print(f"    Provider:  {provider}")
        print(f"    Model:     {model}")
        print(f"    Workspace: {workspace_status}")
        print()


if __name__ == "__main__":
    main()
