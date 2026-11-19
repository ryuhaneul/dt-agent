"""Claude login slot preparation and completion, matching scripts/login.sh."""

from __future__ import annotations

import json
import logging
import os
import shutil
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ductor_bot.cli.claude_accounts import (
    host_cli_auth_dir,
    host_slot_config_dir,
    slot_is_api_key,
    slot_is_authenticated,
)
from ductor_bot.cli.claude_slots import is_slot_name
from ductor_bot.cli.slot_rotation import slot_use_lock
from ductor_bot.config import transform_config_file
from ductor_bot.infra.json_store import atomic_json_save
from ductor_bot.workspace.paths import DuctorPaths

logger = logging.getLogger(__name__)

_PRIVATE_FILES = frozenset({".credentials.json", "credentials.json", ".claude.json"})


def resolve_slot_name(name: str | None, *, allow_api_key: bool = False) -> str:
    """Port login.sh's resolve_claude_slot_name, including aliases and reservation."""
    if name in {None, "", "default", "claude"}:
        slot = "claude"
    else:
        slot = name if name.startswith("claude-") else f"claude-{name}"
    if not is_slot_name(slot):
        raise ValueError("invalid_slot")
    if slot == "claude-api" and not allow_api_key:
        raise ValueError("api_key_slot")
    return slot


def ensure_slot_dir(ductor_home: Path, slot: str) -> None:
    """Port ensure_slot_dir: preserve links/files and migrate legacy projects safely."""
    slot = resolve_slot_name(slot)
    directory = host_slot_config_dir(ductor_home, slot)
    shared = host_cli_auth_dir(ductor_home) / "_shared-projects"
    directory.mkdir(parents=True, exist_ok=True)
    projects = directory / "projects"
    if projects.is_symlink():
        return
    if projects.is_dir():
        if shared.exists() and (not shared.is_dir() or any(shared.iterdir())):
            raise ValueError("projects_conflict")
        if shared.is_dir():
            shared.rmdir()
        projects.rename(shared)
    elif projects.exists():
        return
    projects.symlink_to("../_shared-projects")


def share_slot_from_default(ductor_home: Path, slot: str) -> None:  # noqa: C901 -- API settings remain local during sharing
    """Port share_slot_from_default and _link_alt_to_default, after successful auth."""
    slot = resolve_slot_name(slot, allow_api_key=True)
    default = host_slot_config_dir(ductor_home, "claude")
    root = os.fspath(host_cli_auth_dir(ductor_home))
    candidate = os.path.normpath(os.path.join(root, slot))  # noqa: PTH118 -- explicit CodeQL path normalization
    if not candidate.startswith(root + os.sep):
        raise ValueError("invalid_slot")
    alternate = Path(candidate)
    if slot == "claude" or not alternate.is_dir():
        return
    api_key = slot_is_api_key(alternate)
    if api_key:
        settings = alternate / "settings.json"
        helper = json.loads(settings.read_text(encoding="utf-8"))["apiKeyHelper"]
        try:
            data = json.loads((default / "settings.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):
            data = None
        if not isinstance(data, dict):
            data = {}
            logger.warning("Default Claude settings missing or invalid; retaining API key helper")
        data["apiKeyHelper"] = helper
        atomic_json_save(settings, data)
        settings.chmod(0o644)
    if not default.is_dir():
        return
    for entry in sorted(default.iterdir()):
        if (
            entry.name in _PRIVATE_FILES
            or entry.name == "projects"
            or (api_key and entry.name == "settings.json")
        ):
            continue
        link = alternate / entry.name
        target = f"../claude/{entry.name}"
        if link.is_symlink():
            if str(link.readlink()) == target:
                continue
            link.unlink()
        elif link.exists():
            # login.sh intentionally uses local wall-clock time for this directory.
            timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")  # noqa: DTZ005
            backup = alternate / ".pre-share-backup" / timestamp
            backup.mkdir(parents=True, exist_ok=True)
            link.rename(backup / entry.name)
        link.symlink_to(target)


@dataclass(frozen=True, slots=True)
class PreparedClaudeSlot:
    name: str
    path: Path
    created: bool


class ClaudeLoginSlots:
    """Filesystem work for one manager-owned job; callers keep its singleton reserved."""

    def __init__(self, paths: DuctorPaths) -> None:
        self._paths = paths

    def reserve(self, slot: str) -> AbstractContextManager[bool]:
        """Share the deletion interlock from job reservation through cleanup."""
        return slot_use_lock(host_cli_auth_dir(self._paths.ductor_home), slot)

    def resolve_oauth_slot(self, name: str | None) -> str:
        """Reject API key slots before reserving a web OAuth login job."""
        slot = resolve_slot_name(name)
        root = os.fspath(host_cli_auth_dir(self._paths.ductor_home))
        candidate = os.path.normpath(os.path.join(root, slot))  # noqa: PTH118 -- explicit CodeQL path normalization
        if not candidate.startswith(root + os.sep):
            raise ValueError("invalid_slot")
        if slot_is_api_key(Path(candidate)):
            raise ValueError("api_key_slot")
        return slot

    def prepare(self, name: str, *, new_slot: bool) -> PreparedClaudeSlot:
        """Create only requested new slots; existing re-logins skip preparation."""
        name = self.resolve_oauth_slot(name)
        directory = host_slot_config_dir(self._paths.ductor_home, name)
        if not new_slot:
            if not directory.is_dir():
                raise ValueError("slot_not_found")
            return PreparedClaudeSlot(name, directory, created=False)
        if directory.exists() or directory.is_symlink():
            raise ValueError("slot_exists")
        shared = host_cli_auth_dir(self._paths.ductor_home) / "_shared-projects"
        shared.mkdir(parents=True, exist_ok=True)
        directory.mkdir()
        prepared = PreparedClaudeSlot(name, directory, created=True)
        try:
            ensure_slot_dir(self._paths.ductor_home, name)
        except BaseException:
            self.cleanup(prepared)
            raise
        return prepared

    def finish(self, prepared: PreparedClaudeSlot, *, succeeded: bool) -> None:
        """Share settings and append config only on success; otherwise remove new empties."""
        if not succeeded:
            self.cleanup(prepared)
            return

        def append(config: dict[str, object]) -> None:
            current = config.get("claude_accounts")
            if current is None:
                accounts: list[object] = ["claude"]
            elif isinstance(current, list) and all(isinstance(slot, str) for slot in current):
                accounts = list(current or ["claude"])
            else:
                raise ValueError("invalid_claude_accounts")
            if prepared.name not in accounts:
                accounts.append(prepared.name)
            config["claude_accounts"] = accounts

        try:
            share_slot_from_default(self._paths.ductor_home, prepared.name)
            if prepared.created:
                transform_config_file(self._paths.config_path, append)
        except BaseException:
            self.cleanup(prepared)
            raise

    @staticmethod
    def cleanup(prepared: PreparedClaudeSlot) -> None:
        """Never remove pre-existing slots, credentials or targets of shared symlinks."""
        if not prepared.created:
            return
        if prepared.path.is_symlink():
            prepared.path.unlink()
        elif prepared.path.is_dir() and not slot_is_authenticated(prepared.path):
            shutil.rmtree(prepared.path)
