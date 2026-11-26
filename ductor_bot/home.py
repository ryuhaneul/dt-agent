"""Agent home helpers shared with host onboarding before runtime dependencies are installed."""

from __future__ import annotations

import sys
from pathlib import Path


def main_home_of(ductor_home: Path) -> Path:
    """Return the main home for any agent home (a sub-agent lives in ``<main>/agents/<name>``)."""
    return ductor_home.parent.parent if ductor_home.parent.name == "agents" else ductor_home


def require_main_home(ductor_home: Path) -> Path:
    """Resolve the home and stop a management command that targets a sub-agent."""
    home = ductor_home.expanduser().resolve()
    main_home = main_home_of(home)
    if main_home != home and (main_home / "agents.json").is_file():
        print("Run this command from the main agent home.", file=sys.stderr)
        raise SystemExit(2)
    return home
