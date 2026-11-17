"""Reject rule writes outside an agent home or through symbolic links."""

from __future__ import annotations

import logging
import stat
from pathlib import Path

logger = logging.getLogger(__name__)


def rule_write_allowed(home: Path, target: Path) -> bool:
    """Check every lexical component, including a missing final file, before writing.

    This is a boundary check for framework operations, not a lock against a
    concurrent process replacing a checked path between this check and the write.
    """
    home = home.absolute()
    target = target.absolute()
    try:
        relative = target.relative_to(home)
        if ".." in relative.parts:
            logger.debug("Skipping unsafe rule path: %s", target)
            return False
        current = home
        for part in ("", *relative.parts):
            if part:
                current /= part
            try:
                mode = current.lstat().st_mode
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(mode):
                logger.debug("Skipping unsafe rule path: %s", target)
                return False
        if not target.resolve().is_relative_to(home.resolve()):
            logger.debug("Skipping unsafe rule path: %s", target)
            return False
    except (OSError, ValueError, RuntimeError):
        logger.debug("Skipping unsafe rule path: %s", target)
        return False
    return True
