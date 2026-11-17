"""Cron task rule file backfill (create/list/delete lives in the deployed cron_tools scripts)."""

from __future__ import annotations

import logging
from pathlib import Path

from ductor_bot.workspace.rules_boundary import rule_write_allowed

logger = logging.getLogger(__name__)

# Provider rule files — created per task only for authenticated providers.
_RULE_FILENAMES = ("CLAUDE.md", "AGENTS.md", "GEMINI.md")


def _detect_rule_filenames(cron_tasks_dir: Path) -> list[str]:
    """Determine which rule files to create based on parent directory contents.

    Checks which provider rule files (CLAUDE.md, AGENTS.md, GEMINI.md) exist
    in the ``cron_tasks/`` root — these are deployed by ``RulesSelector``
    based on CLI authentication status.  New task folders mirror only the
    providers that are currently authenticated.

    Falls back to ``["CLAUDE.md"]`` when no rule files are found (e.g. in tests
    or before workspace init has run).
    """
    found = [name for name in _RULE_FILENAMES if (cron_tasks_dir / name).is_file()]
    return found or ["CLAUDE.md"]


def ensure_task_rule_files(cron_tasks_dir: Path) -> int:
    """Add missing rule files to existing cron task folders.

    Checks which provider rule files exist in the ``cron_tasks/`` root
    (deployed by ``RulesSelector``) and creates any that are missing in
    task subdirectories.  Content is copied from an existing rule file in
    the same task folder so the agent instructions stay consistent.

    Only adds files — never removes.  Safe to call repeatedly (idempotent).

    Returns the number of files created.
    """
    home = cron_tasks_dir.parent.parent
    if not rule_write_allowed(home, cron_tasks_dir) or not cron_tasks_dir.is_dir():
        return 0

    expected = _detect_rule_filenames(cron_tasks_dir)
    created = 0

    try:
        task_dirs = sorted(cron_tasks_dir.iterdir())
    except FileNotFoundError:
        return 0

    for task_dir in task_dirs:
        if not rule_write_allowed(home, task_dir) or not task_dir.is_dir():
            continue

        # A linked source or destination makes the whole task group unsafe.
        if not all(rule_write_allowed(home, task_dir / name) for name in _RULE_FILENAMES):
            continue

        # Identify existing rule files — skip dirs that have none (not a task).
        existing = [name for name in _RULE_FILENAMES if (task_dir / name).is_file()]
        if not existing:
            continue

        missing = [name for name in expected if not (task_dir / name).is_file()]
        if not missing:
            continue

        # Copy content from the first existing rule file (they're identical).
        try:
            source_content = (task_dir / existing[0]).read_text(encoding="utf-8")
            for name in missing:
                target = task_dir / name
                target.write_text(source_content, encoding="utf-8")
                created += 1
                logger.info("Created missing rule file %s in task %s", name, task_dir.name)
        except FileNotFoundError:
            logger.debug("Task disappeared during rule backfill: %s", task_dir)

    return created
