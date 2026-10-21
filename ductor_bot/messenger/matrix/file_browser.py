"""Text-based file browser for Matrix.

Unlike Telegram's interactive button-based browser, Matrix uses a
flat text listing since Matrix lacks inline keyboard buttons.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ductor_bot.files.browser import BROWSER_EXCLUDED_NAMES, list_directory
from ductor_bot.i18n import t
from ductor_bot.security.paths import is_path_safe
from ductor_bot.text.response_format import SEP, fmt

if TYPE_CHECKING:
    from ductor_bot.workspace.paths import DuctorPaths

_MAX_RECENT_FILES = 5


def format_file_listing(paths: DuctorPaths, subdir: str = "") -> str:
    """Format a text listing of the workspace directory structure.

    Args:
        paths: Resolved workspace paths.
        subdir: Optional subdirectory relative to ``ductor_home`` to list.
            When empty, shows an overview of key workspace directories
            with file counts and recent files.

    Returns:
        Formatted text suitable for ``_send_rich``.
    """
    if subdir:
        return _format_subdir(paths, subdir)
    return _format_overview(paths)


def _format_overview(paths: DuctorPaths) -> str:
    """Build an overview listing of key workspace directories."""
    lines: list[str] = []

    dirs = [
        ("output_to_user", paths.output_to_user_dir),
        ("telegram_files", paths.telegram_files_dir),
        ("matrix_files", paths.matrix_files_dir),
        ("tools", paths.tools_dir),
        ("cron_tasks", paths.cron_tasks_dir),
        ("memory_system", paths.memory_system_dir),
        ("skills", paths.skills_dir),
    ]

    for name, dir_path in dirs:
        if dir_path.is_dir():
            try:
                entries = [
                    e
                    for e in dir_path.iterdir()
                    if not e.name.startswith(".") and e.name not in BROWSER_EXCLUDED_NAMES
                ]
            except PermissionError:
                lines.append(f"> `{name}/` -- {t('file_browser.matrix_permission_denied')}")
                continue
            count = len(entries)
            lines.append(f"> `{name}/` -- {t('file_browser.matrix_file_count', count=count)}")
            recent = sorted(
                [f for f in entries if f.is_file()],
                key=lambda f: f.stat().st_mtime,
                reverse=True,
            )[:_MAX_RECENT_FILES]
            lines.extend(f"  - `{f.name}`" for f in recent)
        else:
            lines.append(f"> `{name}/` -- {t('file_browser.empty')}")

    body = "\n".join(lines) if lines else t("file_browser.matrix_no_dirs")

    return fmt(
        t("file_browser.matrix_header"),
        SEP,
        body,
        t("file_browser.matrix_hint"),
    )


def _format_subdir(paths: DuctorPaths, subdir: str) -> str:
    """Build a detailed listing for a specific subdirectory."""
    base = paths.ductor_home.resolve()
    target = (base / subdir).resolve()

    if not is_path_safe(target, [base]) or not target.is_dir():
        return fmt(
            t("file_browser.matrix_header"),
            SEP,
            t("file_browser.matrix_dir_not_found", subdir=subdir),
        )

    dirs, files = list_directory(target)

    display_path = f"~/.ductor/{subdir}"
    if not display_path.endswith("/"):
        display_path += "/"

    body_lines = [f"  {d}/" for d in dirs]
    body_lines.extend(f"  {f}" for f in files)

    if not body_lines:
        body_lines.append(f"  {t('file_browser.empty')}")

    return fmt(
        t("file_browser.matrix_header"),
        SEP,
        f"`{display_path}`\n\n" + "\n".join(body_lines),
    )
