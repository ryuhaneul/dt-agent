"""Agent registry: loads and manages agents.json."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ductor_bot.infra.agent_diagnostics import safe_error
from ductor_bot.infra.file_lock import exclusive_file_lock
from ductor_bot.infra.json_store import atomic_json_save
from ductor_bot.multiagent.models import SubAgentConfig

logger = logging.getLogger(__name__)


class AgentRegistry:
    """Read access to the sub-agent registry file (agents.json)."""

    def __init__(self, agents_path: Path) -> None:
        self._path = agents_path
        self._indices: dict[str, int] = {}

    def load(self) -> list[SubAgentConfig]:
        """Load sub-agent definitions. Returns empty list if file is missing."""
        self._indices = {}
        if not self._path.is_file():
            return []
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.exception("Failed to read agents.json at %s", self._path)
            return []

        if not isinstance(raw, list):
            logger.warning("agents.json must be a JSON array, got %s", type(raw).__name__)
            return []

        agents: list[SubAgentConfig] = []
        for idx, entry in enumerate(raw):
            try:
                sub = SubAgentConfig(**entry)
                agents.append(sub)
                self._indices.setdefault(sub.name, idx)
            except Exception as exc:
                # Tracebacks may include rejected credentials; log only safe metadata.
                logger.error(  # noqa: TRY400
                    "Invalid sub-agent definition: %s",
                    safe_error(idx, exc),
                )
        return agents

    def index_for(self, name: str) -> int | None:
        return self._indices.get(name)


def update_agent_fields(agents_path: Path, agent_name: str, **fields: object) -> None:
    """Update specific fields of an agent entry in agents.json.

    Holds the registry lock from read through replacement. Raises on a missing
    or invalid registry, a missing agent, or a write failure.
    A value of ``None`` removes the key from the entry.
    """
    with exclusive_file_lock(agents_path):
        raw = json.loads(agents_path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError("agents.json must be a JSON array")  # noqa: TRY004 -- malformed JSON

        entry = next(
            (row for row in raw if isinstance(row, dict) and row.get("name") == agent_name),
            None,
        )
        if entry is None:
            raise ValueError("Agent registry entry unavailable")

        for key, value in fields.items():
            if value is None:
                entry.pop(key, None)
            else:
                entry[key] = value
        atomic_json_save(agents_path, raw)

    logger.info("Updated agent '%s' in agents.json: %s", agent_name, list(fields))
