"""Workspace initialization: walk home defaults, copy with zone rules, sync, merge."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import time
from pathlib import Path

from ductor_bot.infra.atomic_io import atomic_text_save
from ductor_bot.workspace.cron_tasks import ensure_task_rule_files
from ductor_bot.workspace.paths import DuctorPaths
from ductor_bot.workspace.rules_selector import RulesSelector
from ductor_bot.workspace.skill_sync import sync_bundled_skills, sync_skills

logger = logging.getLogger(__name__)


# Files that are ALWAYS overwritten on every start (Zone 2).
# Everything else is seeded only once (Zone 3).
_ZONE2_FILES = frozenset({"CLAUDE.md", "AGENTS.md", "GEMINI.md"})

# Directories where ALL .py files are Zone 2 (framework-managed).
# User-owned scripts should go in tools/user_tools/ (Zone 3).
# Paths are relative to home_defaults root (include workspace/ prefix).
_ZONE2_PY_DIRS = frozenset(
    {
        # Only ``_tool_shared.py`` (the argument parser every tool loads) and ``_secrets.py``
        # (the secrets loader) sit directly in tools/.
        "workspace/tools",
        "workspace/tools/cron_tools",
        "workspace/tools/webhook_tools",
        "workspace/tools/agent_tools",
        "workspace/tools/task_tools",
        "workspace/tools/memory_tools",
        # browser.py loads the shared argument parser in tools/, so an old copy next to a
        # new parser (or the other way round) must not happen on upgrade.
        "workspace/tools/browser_tools",
        # media_tools scripts consume framework env-vars
        # (DUCTOR_TRANSCRIBE_COMMAND, DUCTOR_VIDEO_TRANSCRIBE_COMMAND) — keep
        # them framework-managed so v0.16.0 users inherit the configurable
        # transcription hooks from #66 automatically on upgrade.
        "workspace/tools/media_tools",
    }
)

# Trees where EVERY file is framework-managed (always overwritten, stale files removed).
# Paths are relative to home_defaults root. Holds the bundled DT Agent reference docs.
_ZONE2_TREE_DIRS = frozenset({"workspace/docs/dt-agent"})

# Rule templates are deployed separately by RulesSelector
_SKIP_FILES = frozenset(
    {
        "RULES-claude-only.md",
        "RULES-codex-only.md",
        "RULES-gemini-only.md",
        "RULES-all-clis.md",
        "RULES.md",  # Static templates also handled by RulesSelector
    }
)

_SKIP_DIRS = frozenset({".venv", ".git", ".mypy_cache", "__pycache__", "node_modules"})


# ---------------------------------------------------------------------------
# Home defaults sync (replaces _ensure_dirs + _copy_framework + _seed_defaults)
# ---------------------------------------------------------------------------


def _sync_home_defaults(paths: DuctorPaths) -> None:
    """Walk the home-defaults template and copy to ``ductor_home``.

    The template at ``<repo>/workspace/`` mirrors ``~/.ductor/`` exactly.
    Zone rules per file:

    - **Zone 2** (``_ZONE2_FILES``): always overwritten so framework updates
      reach users on restart.  ``CLAUDE.md`` also produces a matching
      ``AGENTS.md`` mirror automatically.
    - **Zone 3** (everything else): seeded on first run only, never
      overwritten so user modifications persist.
    """
    if not paths.home_defaults.is_dir():
        logger.warning("Home defaults directory not found: %s", paths.home_defaults)
        return
    _walk_and_copy(paths.home_defaults, paths.ductor_home)
    _prune_zone2_trees(paths.home_defaults, paths.ductor_home)
    # Ensure logs dir exists for the main agent only.  Sub-agents share the
    # central log file and don't need their own logs directory.
    # Sub-agent homes live under <main_home>/agents/<name>/.
    if paths.ductor_home.parent.name != "agents":
        paths.logs_dir.mkdir(parents=True, exist_ok=True)


def _should_skip_entry(entry: Path) -> bool:
    """Check if entry should be skipped during workspace sync."""
    return entry.name.startswith(".") or entry.name in _SKIP_DIRS or entry.name in _SKIP_FILES


def _in_zone2_tree(src: Path, root_src: Path) -> bool:
    """True when *src* is (inside) a framework-managed tree directory."""
    try:
        rel = src.relative_to(root_src).as_posix()
    except ValueError:
        return False
    return any(rel == tree or rel.startswith(f"{tree}/") for tree in _ZONE2_TREE_DIRS)


def _has_symlink_component(root: Path, target: Path) -> bool:
    """True when *target* or any directory between *root* and it is a symlink."""
    current = root
    for part in target.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _prune_zone2_trees(home_defaults: Path, ductor_home: Path) -> None:
    """Remove files from managed trees that the template no longer ships.

    Never follows symlinks: a symlinked ancestor (``workspace``, ``workspace/docs``) or a
    symlinked tree is left alone entirely (the copy step skips those too), and links inside
    the tree are removed as links, not followed.
    """
    for tree in _ZONE2_TREE_DIRS:
        shipped = home_defaults / tree
        installed = ductor_home / tree
        if not shipped.is_dir() or _has_symlink_component(ductor_home, installed):
            continue
        if not installed.is_dir():
            continue
        keep = {p.relative_to(shipped) for p in shipped.rglob("*") if p.is_file()}
        for dirpath, dirnames, filenames in os.walk(installed, topdown=False, followlinks=False):
            base = Path(dirpath)
            for name in filenames + [d for d in dirnames if (base / d).is_symlink()]:
                path = base / name
                if path.relative_to(installed) not in keep:
                    path.unlink(missing_ok=True)
            if base != installed and not any(base.iterdir()):
                base.rmdir()


def _is_zone2_py_file(entry: Path, src: Path, root_src: Path) -> bool:
    """Check if a .py file is in a Zone 2 directory (always overwritten)."""
    if entry.suffix != ".py":
        return False
    try:
        rel_dir = src.relative_to(root_src)
        return str(rel_dir) in _ZONE2_PY_DIRS
    except ValueError:
        return False


def _copy_with_symlink_check(entry: Path, target: Path) -> None:
    """Copy file to target, removing symlink if present."""
    if target.is_symlink():
        target.unlink()
    shutil.copy2(entry, target)


def _handle_zone2_file(entry: Path, target: Path, dst: Path) -> None:
    """Handle Zone 2 file copy (always overwrite) and mirror creation."""
    _copy_with_symlink_check(entry, target)
    logger.debug("Zone 2 copy: %s", target)
    # Auto-create mirrors for every CLAUDE.md (AGENTS.md + GEMINI.md)
    if entry.name == "CLAUDE.md":
        for mirror_name in ("AGENTS.md", "GEMINI.md"):
            mirror_target = dst / mirror_name
            _copy_with_symlink_check(entry, mirror_target)
            logger.debug("Zone 2 copy: %s", mirror_target)


_ZONE2_RECORD_NAME = ".zone2_installed.json"


class _Zone2Record:
    """Hashes of the framework ``.py`` files this tool installed, kept in the home directory.

    A file whose content equals its recorded hash is the framework's own copy and may be
    replaced silently; any other content is a user edit (or, for an install made before this
    record existed, unknown) and is saved first.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._hashes: dict[str, str] = {}
        self._changed = False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(data, dict):
            self._hashes = {k: v for k, v in data.items() if isinstance(v, str)}

    def get(self, key: str) -> str | None:
        return self._hashes.get(key)

    def set(self, key: str, digest: str) -> None:
        if self._hashes.get(key) != digest:
            self._hashes[key] = digest
            self._changed = True

    def save(self) -> None:
        if not self._changed:
            return
        try:
            atomic_text_save(self._path, json.dumps(self._hashes, indent=2, sort_keys=True) + "\n")
        except OSError as exc:
            logger.warning("Could not save %s: %s", self._path, exc)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _free_backup_path(target: Path) -> Path:
    """``<name>.bak``, or a name that does not exist yet (a saved backup is never replaced)."""
    backup = target.with_suffix(target.suffix + ".bak")
    if not backup.exists() and not backup.is_symlink():
        return backup
    stamp = time.strftime("%Y%m%dT%H%M%S")
    candidate = target.with_suffix(f"{target.suffix}.bak.{stamp}")
    counter = 1
    while candidate.exists() or candidate.is_symlink():
        counter += 1
        candidate = target.with_suffix(f"{target.suffix}.bak.{stamp}-{counter}")
    return candidate


def _backup_user_modified_zone2(entry: Path, target: Path, record: _Zone2Record, key: str) -> bool:
    """Save a user-edited Zone 2 ``.py`` file before it is replaced.

    Nothing happens when *target* is missing, equals the new framework file, or is exactly the
    framework file installed last time (per *record*). Otherwise it is a user edit - or an
    install without a record - and moves to a backup name that is never taken by an earlier
    backup, so a saved edit survives any number of later updates.

    Returns whether *target* may be replaced: ``False`` when it could not be compared or
    saved, so the caller leaves it unchanged.
    """
    if target.name.endswith(".bak") or not target.exists():
        return True
    try:
        current = _digest(target)
        if current == _digest(entry) or current == record.get(key):
            return True
    except OSError as exc:
        logger.warning("Could not compare %s for backup: %s", target, exc)
        return False
    try:
        backup = _free_backup_path(target)
        target.replace(backup)
        logger.warning(
            "Zone 2 overwrite: saved user-modified %s to %s before applying framework update",
            target,
            backup,
        )
    except OSError as exc:
        logger.warning("Could not create backup for %s: %s", target, exc)
        return False
    return True


def _handle_regular_file(
    entry: Path, target: Path, src: Path, root_src: Path, record: _Zone2Record
) -> None:
    """Handle regular file with Zone 2 .py or Zone 3 logic."""
    if _is_zone2_py_file(entry, src, root_src):
        # Zone 2 .py file: always overwrite (framework-controlled).
        # Back up user modifications first so upgrades never destroy edits silently.
        key = entry.relative_to(root_src).as_posix()
        if not _backup_user_modified_zone2(entry, target, record, key):
            logger.warning(
                "Zone 2 update skipped for %s: the existing file could not be saved first, "
                "so it is left unchanged",
                target,
            )
            return
        _copy_with_symlink_check(entry, target)
        record.set(key, _digest(entry))
        logger.debug("Zone 2 copy (framework tool): %s", target)
    elif _in_zone2_tree(src, root_src):
        # Framework-managed reference tree: always current, no backups.
        _copy_with_symlink_check(entry, target)
        logger.debug("Zone 2 copy (managed tree): %s", target)
    elif not target.exists():
        # Zone 3: seed only (user-owned, never overwritten)
        shutil.copy2(entry, target)
        logger.debug("Zone 3 seed: %s", target)
    else:
        logger.debug("Zone 3 skip: %s (exists)", target)


def _walk_and_copy(
    src: Path, dst: Path, root_src: Path | None = None, record: _Zone2Record | None = None
) -> None:
    """Recursively copy *src* tree into *dst* with zone-based overwrite rules.

    Args:
        src: Source directory to copy from
        dst: Destination directory to copy to
        root_src: Root source directory (for calculating relative paths). Defaults to src.
        record: Installed-file hashes of the whole walk; the top-level call creates it in
            *dst* and saves it at the end.
    """
    if root_src is None:
        root_src = src
    if record is None:
        record = _Zone2Record(dst / _ZONE2_RECORD_NAME)
        try:
            _walk_and_copy(src, dst, root_src, record)
        finally:
            record.save()
        return

    dst.mkdir(parents=True, exist_ok=True)
    for entry in sorted(src.iterdir()):
        if _should_skip_entry(entry):
            continue
        target = dst / entry.name
        if entry.is_dir():
            if target.is_symlink():
                logger.debug("Skip symlinked target: %s", target)
                continue
            _walk_and_copy(entry, target, root_src, record)
        elif entry.name in _ZONE2_FILES:
            _handle_zone2_file(entry, target, dst)
        else:
            _handle_regular_file(entry, target, src, root_src, record)


# ---------------------------------------------------------------------------
# Rule file sync (CLAUDE.md <-> AGENTS.md)
# ---------------------------------------------------------------------------


_RULE_FILE_NAMES = ("CLAUDE.md", "AGENTS.md", "GEMINI.md")


def sync_rule_files(root: Path) -> None:
    """Recursively sync CLAUDE.md <-> AGENTS.md <-> GEMINI.md by mtime.

    For each directory under root (including root itself):
    - Find the newest rule file among the three by mtime.
    - Copy the newest to all others that exist or are missing.
    - Skip directories in _SKIP_DIRS.
    """
    if not root.is_dir():
        return
    _sync_group(root)
    try:
        entries = list(root.rglob("*"))
    except PermissionError:
        logger.debug("Permission denied walking %s, skipping rule-file sync subtree", root)
        return
    for dirpath in entries:
        try:
            is_dir = dirpath.is_dir()
        except PermissionError:
            continue
        if not is_dir:
            continue
        if any(part in _SKIP_DIRS for part in dirpath.parts):
            continue
        _sync_group(dirpath)


def _sync_group(directory: Path) -> None:
    """Sync all rule files (CLAUDE.md, AGENTS.md, GEMINI.md) in a single directory."""
    files = {name: directory / name for name in _RULE_FILE_NAMES}
    try:
        existing = {name: path for name, path in files.items() if path.exists()}
    except PermissionError:
        logger.debug("Permission denied checking rule files in %s, skipping", directory)
        return
    if not existing:
        return

    try:
        newest_name, newest_path = max(existing.items(), key=lambda item: item[1].stat().st_mtime)
        newest_mtime = newest_path.stat().st_mtime
    except PermissionError:
        logger.debug("Permission denied reading stat in %s, skipping", directory)
        return

    for name, path in files.items():
        if name == newest_name:
            continue
        try:
            if path.exists() and path.stat().st_mtime < newest_mtime:
                shutil.copy2(newest_path, path)
        except PermissionError:
            logger.debug("Permission denied syncing %s in %s, skipping", name, directory)


# ---------------------------------------------------------------------------
# Config smart-merge
# ---------------------------------------------------------------------------


def _smart_merge_config(paths: DuctorPaths) -> None:
    """Create config from example or merge new keys into existing."""
    if not paths.config_example_path.exists():
        return

    try:
        defaults = json.loads(paths.config_example_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("Failed to parse config example: %s", paths.config_example_path)
        return

    if not paths.config_path.exists():
        from ductor_bot.infra.json_store import atomic_json_save

        atomic_json_save(paths.config_path, defaults)
        return

    try:
        existing = json.loads(paths.config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("Failed to parse config: %s, skipping merge", paths.config_path)
        return
    merged = {**defaults, **existing}

    if merged != existing:
        from ductor_bot.infra.json_store import atomic_json_save

        atomic_json_save(paths.config_path, merged)


# ---------------------------------------------------------------------------
# Cleanup helpers
# ---------------------------------------------------------------------------


def _migrate_tasks_to_cron_tasks(paths: DuctorPaths) -> None:
    """One-time migration: rename tasks/ to cron_tasks/ if needed."""
    old_tasks = paths.workspace / "tasks"
    if old_tasks.is_dir() and not paths.cron_tasks_dir.exists():
        old_tasks.rename(paths.cron_tasks_dir)
        logger.info("Migrated workspace/tasks/ -> workspace/cron_tasks/")


def _clean_orphan_symlinks(paths: DuctorPaths) -> None:
    """Remove broken symlinks in the workspace root."""
    if not paths.workspace.is_dir():
        return
    for entry in paths.workspace.iterdir():
        if entry.is_symlink() and not entry.exists():
            entry.unlink()


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


_REQUIRED_DIRS = (
    "workspace",
    "workspace/memory_system",
    "workspace/cron_tasks",
    "workspace/tools",
    "workspace/tools/user_tools",
    "workspace/tools/cron_tools",
    "workspace/tools/media_tools",
    "workspace/tools/webhook_tools",
    "workspace/tools/agent_tools",
    "workspace/output_to_user",
    "workspace/tasks",
    "workspace/skills",
    "config",
)


def _ensure_required_dirs(paths: DuctorPaths) -> None:
    """Create any required directories that are missing."""
    for rel in _REQUIRED_DIRS:
        d = paths.ductor_home / rel
        if not d.is_dir():
            d.mkdir(parents=True, exist_ok=True)
            logger.info("Created missing directory: %s", d)


def init_workspace(paths: DuctorPaths) -> None:
    """Initialize the workspace: defaults sync, rule sync, config merge, cleanup."""
    logger.info("Workspace init started home=%s", paths.ductor_home)
    _migrate_tasks_to_cron_tasks(paths)
    sync_bundled_skills(paths)
    _sync_home_defaults(paths)
    _ensure_required_dirs(paths)

    # Deploy provider-specific rule files based on CLI auth status
    try:
        selector = RulesSelector(paths)
        selector.deploy_rules()
    except Exception:
        logger.exception("Failed to deploy rule files")

    ensure_task_rule_files(paths.cron_tasks_dir)
    sync_rule_files(paths.workspace)
    _smart_merge_config(paths)
    _clean_orphan_symlinks(paths)
    sync_skills(paths)
    logger.info("Workspace init completed")


# ---------------------------------------------------------------------------
# Runtime environment injection
# ---------------------------------------------------------------------------

_RUNTIME_NOTICE = """

---

## Runtime Environment

**IMPORTANT: YOU ARE RUNNING INSIDE THE DUCTOR CONTAINER.**

- Your filesystem is isolated. `/ductor` is the mounted host directory `~/.ductor`.
- You cannot see or access the host system outside this mount.
- Changes under `/ductor` affect the mounted host directory.
"""

# ---------------------------------------------------------------------------
# Transport-specific messenger rules
# ---------------------------------------------------------------------------

_TRANSPORT_TELEGRAM = """

---

## Messenger Rules

- Replies are Telegram messages (4096-char limit; auto-split is handled).
- Keep responses mobile-friendly and structured.
- To send files, use `<file:/absolute/path>`.
- Save generated deliverables in `output_to_user/`.
- Do not suggest GUI-only actions like `xdg-open`.

### Quick Reply Buttons

Use button syntax at the end of messages:

- `[button:Label]` markers
- same line = one row
- new line = new row

Keep labels short. Callback data is truncated to 64 bytes by the framework.
Do not place button markers inside code blocks.
"""

_TRANSPORT_MATRIX = """

---

## Messenger Rules

- Replies are Matrix messages (no hard character limit, but keep responses readable).
- Messages are formatted as HTML (Markdown is auto-converted by the framework).
- Keep responses structured and scannable.
- To send files, use `<file:/absolute/path>`.
- Save generated deliverables in `output_to_user/`.
- Do not suggest GUI-only actions like `xdg-open`.
- Commands use `!` prefix (e.g. `!help`, `!status`). \
`/` also works but may conflict with Element's built-in commands.

### Quick Reply Buttons

Use button syntax at the end of messages:

- `[button:Label]` markers
- same line = one row
- new line = new row

In Matrix, buttons are rendered as a numbered text list. The user types \
the label text (or number) to "press" a button — there are no clickable \
inline buttons. Keep labels short and distinctive.
Do not place button markers inside code blocks.
"""

_TRANSPORT_SLACK = """

---

## Messenger Rules

- Replies are Slack messages.
- Slack command keywords work as normal messages or slash-prefixed messages
  (for example `help` or `/help`).
- In channels, keep long conversations inside the existing thread when one exists.
- To send files, use `<file:/absolute/path>`.
- Save generated deliverables in `output_to_user/`.
- Do not suggest GUI-only actions like opening Finder or clicking app-only menus.
"""

_TRANSPORT_RULES: dict[str, str] = {
    "telegram": _TRANSPORT_TELEGRAM,
    "matrix": _TRANSPORT_MATRIX,
    "slack": _TRANSPORT_SLACK,
    "headless": "",
}

# ---------------------------------------------------------------------------
# Multi-Agent identity injection
# ---------------------------------------------------------------------------

_IDENTITY_MAIN = """

---

## Multi-Agent Identity

**You are the MAIN agent (`{name}`).**

- You are the primary agent and coordinator in a multi-agent system.
- You can create, manage, and communicate with sub-agents.
- Each sub-agent has its own workspace and memory. Bot-backed sub-agents
have a separate Telegram/Matrix chat; delegation-only (`headless`) sub-agents
have no bot token, messaging account, or direct user chat.

### How the user interacts with sub-agents

For a bot-backed sub-agent, the user can chat with its bot directly or ask
YOU to delegate. For a headless sub-agent, the user only asks YOU to delegate
using synchronous or asynchronous agent tools. Async results return through
you to the originating chat/topic, never to a headless chat.

Create a delegation-only worker with
`python3 tools/agent_tools/create_agent.py --name NAME --transport headless`
(optional `--provider` and `--model`). Choose this when no separate direct chat
is needed; choose Telegram/Matrix when the user wants one.

After creating a bot-backed sub-agent, explain how to open its chat. For
headless, explain delegation through you and never suggest opening its chat.
Do not suggest Python tool commands to the user — they are internal tools.

### Agent tools (for YOUR internal use)

- `python3 tools/agent_tools/ask_agent.py TARGET "message"` — sync, blocks
- `python3 tools/agent_tools/ask_agent_async.py TARGET "message"` — async
- Add `--new` before TARGET to start a fresh session (discard prior context)
- `python3 tools/agent_tools/list_agents.py`
- `python3 tools/agent_tools/edit_shared_knowledge.py`

Responses from these tools always come back to YOU, never to the sub-agent's chat.
Use async for tasks that may take more than a few seconds.

When you delegate a task asynchronously with source chat/topic context, the \
sub-agent processes it in a **Named Session** with a sender/chat/topic-scoped \
name (for example, `ia.main.t12345.xab12cd34`). Follow-up messages from the \
same chat/topic reuse that session. Calls without source context use the \
legacy `ia-<sender>` name.

The exact session name is reported with the response. To continue it in the \
bot-backed sub-agent's chat, use the reported name exactly: \
`@<session-name> <message>`. Headless agents have no direct chat: continue
through agent tools with the same source chat/topic. Do not guess session names.
"""

_IDENTITY_SUB = """

---

## Multi-Agent Identity

**You are agent `{name}` (sub-agent).**

- You are a specialized sub-agent in a multi-agent system.
- The main agent coordinates the overall system.
- You have your own workspace and memory.
{interface_notice}

### Communication Tools

- **Synchronous**: `python3 tools/agent_tools/ask_agent.py TARGET "message"` — blocks until response, answer returned to you
- **Asynchronous**: `python3 tools/agent_tools/ask_agent_async.py TARGET "message"` — returns immediately, answer returned to your calling context when ready
- **Fresh session**: Add `--new` before the target to discard prior context: `ask_agent_async.py --new TARGET "message"`

**Important**: Responses always come back to the calling agent, never directly to another agent's chat.
For headless agents, main delivers async results to the originating chat/topic.
During synchronous delegation, put questions in the final report instead of
messaging main: the server rejects those questions. Continue the work, and do
not invent missing values; leave them empty or state assumptions.

### Inter-Agent Named Sessions

When another agent sends you a message with source chat/topic context, it \
runs in a **Named Session** with a sender/chat/topic-scoped name (for \
example, `ia.main.t12345.xab12cd34`). Follow-up messages from the same \
chat/topic reuse that session. Calls without source context use the legacy \
`ia-{{sender}}` name. These sessions:

- Preserve context across multiple messages from the same sender agent
- Run in the background, independently of foreground turns
- Bot-backed agents expose them via `/sessions`; headless agents have no user chat

Async responses report the exact session name. To continue it, use the \
reported name exactly: `@<session-name> <message>`, only for bot-backed
recipients with a chat. Headless agents have no direct chat; main continues
through agent tools with the same source chat/topic. Synchronous responses
do not include the session name. Do not guess session names.

When you receive an `[INTER-AGENT MESSAGE]` marker, respond directly \
and concisely. If the user asks about running tasks or background \
sessions, check `named_sessions.json` (or `/sessions` for bot-backed agents) for active \
inter-agent sessions.
"""


def _build_identity_notice(agent_name: str, transport: str) -> str:
    """Build the identity section for rule files."""
    if agent_name == "main":
        return _IDENTITY_MAIN.format(name=agent_name)
    if transport == "headless":
        interface_notice = (
            "- You are delegation-only (`headless`), with no bot account or chat. "
            "The user cannot chat with you directly.\n"
            "- Main uses synchronous or asynchronous delegation. Async results "
            "return through main to the originating chat/topic."
        )
    else:
        interface_notice = f"- You have your own {transport.capitalize()} bot and direct chat."
    return _IDENTITY_SUB.format(name=agent_name, interface_notice=interface_notice)


def inject_runtime_environment(
    paths: DuctorPaths,
    *,
    agent_name: str = "main",
    transport: str = "telegram",
) -> None:
    """Append transport rules, agent identity, and runtime environment to rule files.

    Called once after workspace init when the transport is known.
    """
    env_notice = _RUNTIME_NOTICE
    identity_notice = _build_identity_notice(agent_name, transport)
    transport_notice = _TRANSPORT_RULES.get(transport, _TRANSPORT_TELEGRAM)

    for name in _RULE_FILE_NAMES:
        target = paths.workspace / name
        if not target.exists():
            continue
        content = target.read_text(encoding="utf-8")
        # Avoid duplicate injection on restart without workspace re-init
        if "## Multi-Agent Identity" in content or "## Runtime Environment" in content:
            continue
        atomic_text_save(target, content + transport_notice + identity_notice + env_notice)
    logger.info(
        "Runtime environment injected: %s agent=%s transport=%s",
        "container",
        agent_name,
        transport,
    )


_RULE_SYNC_INTERVAL = 10.0  # seconds


async def watch_rule_files(workspace: Path, *, interval: float = _RULE_SYNC_INTERVAL) -> None:
    """Continuously sync CLAUDE.md <-> AGENTS.md <-> GEMINI.md across the workspace.

    Every *interval* seconds:
    1. ``ensure_task_rule_files`` adds missing rule files to existing cron
       tasks (e.g. GEMINI.md after a new provider was authenticated).
    2. ``sync_rule_files`` propagates content changes across all rule files.
    """
    cron_tasks_dir = workspace / "cron_tasks"
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(ensure_task_rule_files, cron_tasks_dir)
            await asyncio.to_thread(sync_rule_files, workspace)
        except Exception:
            logger.exception("Rule file sync failed")
