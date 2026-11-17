"""Reject rule writes outside an agent home or through links below its real path."""

from __future__ import annotations

import logging
import stat
from pathlib import Path

logger = logging.getLogger(__name__)


def rule_write_allowed(home: Path, target: Path) -> bool:
    """Check lexical components below the resolved home before writing, including a missing file.

    This is a boundary check for framework operations, not a lock against a
    concurrent process replacing a checked path between this check and the write.
    """
    home = home.absolute()
    target = target.absolute()
    try:
        real_home = home.resolve()
        relative = (
            target.relative_to(home)
            if target.is_relative_to(home)
            else target.relative_to(real_home)
        )
        if ".." in relative.parts:
            logger.debug("Skipping unsafe rule path: %s", target)
            return False
        target = real_home / relative
        current = real_home
        for part in relative.parts:
            current /= part
            try:
                mode = current.lstat().st_mode
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(mode):
                logger.debug("Skipping unsafe rule path: %s", target)
                return False
        if not target.resolve().is_relative_to(real_home):
            logger.debug("Skipping unsafe rule path: %s", target)
            return False
    except (OSError, ValueError, RuntimeError):
        logger.debug("Skipping unsafe rule path: %s", target)
        return False
    return True
