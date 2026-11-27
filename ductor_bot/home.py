"""Agent home helpers shared with host onboarding before runtime dependencies are installed."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main_home_of(ductor_home: Path) -> Path:
    """Return the main home for any agent home (a sub-agent lives in ``<main>/agents/<name>``)."""
    return ductor_home.parent.parent if ductor_home.parent.name == "agents" else ductor_home


def require_main_home(ductor_home: Path | None = None) -> Path:
    """Resolve the home and stop a management command that targets a sub-agent."""
    if ductor_home is None:
        ductor_home = Path(os.environ.get("DUCTOR_HOME", "~/.ductor"))
    raw_home = Path(os.path.abspath(ductor_home.expanduser()))  # noqa: PTH100 -- keep symlinks
    resolved_home = ductor_home.expanduser().resolve()
    for home in (raw_home, resolved_home):
        main_home = main_home_of(home)
        if main_home != home and (main_home / "agents.json").is_file():
            print("Run this command from the main agent home.", file=sys.stderr)
            raise SystemExit(2)
    return resolved_home


def subprocess_settings_home(ductor_home: Path) -> Path:
    """Read the settings home supplied by the launcher for sub-agent CLI turns."""
    if os.environ.get("DUCTOR_AGENT_ROLE") != "sub":
        return ductor_home
    main_home = os.environ.get("DUCTOR_MAIN_HOME")
    return Path(main_home) if main_home is not None else main_home_of(ductor_home)
