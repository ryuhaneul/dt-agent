"""Installed package version for status display."""

from __future__ import annotations

import importlib.metadata


def get_current_version() -> str:
    """Return the installed version of DT Agent."""
    try:
        return importlib.metadata.version("dt-agent")
    except importlib.metadata.PackageNotFoundError:
        return "0.0.0"
