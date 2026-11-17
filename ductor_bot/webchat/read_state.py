"""Small, atomic webchat read cursors, serialized by the bridge's read-state lock."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from ductor_bot.chat_log.ids import from_tid, to_tid
from ductor_bot.infra.atomic_io import complete_write
from ductor_bot.infra.json_store import atomic_json_save

logger = logging.getLogger(__name__)


class ReadState:
    def __init__(self, path: Path) -> None:
        self._path = path
        # Admission, persistence and topic deletion share this lock.
        self.lock = asyncio.Lock()

    def _load(self) -> dict[str, int]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            logger.warning("Unable to load webchat read state")
            return {}
        if not isinstance(data, dict):
            return {}
        result: dict[str, int] = {}
        for tid, seq in data.items():
            if type(seq) is not int:
                continue
            try:
                valid = tid == "unknown" or to_tid(from_tid(tid)) == tid
            except ValueError:
                continue
            if valid:
                result[tid] = seq
        return result

    async def load(self) -> dict[str, int]:
        return await asyncio.to_thread(self._load)

    async def merge(self, updates: dict[str, int]) -> dict[str, int]:
        """Merge cursors while the caller holds lock, preserving unset versus negative."""
        values = await self.load()
        merged = dict(values)
        for tid, seq in updates.items():
            merged[tid] = max(values[tid], seq) if tid in values else seq
        if merged != values:
            await complete_write(atomic_json_save, self._path, merged)
        return merged

    async def remove(self, tid: str) -> None:
        """Remove a deleted topic while the caller holds lock."""
        values = await self.load()
        if tid in values:
            del values[tid]
            await complete_write(atomic_json_save, self._path, values)
