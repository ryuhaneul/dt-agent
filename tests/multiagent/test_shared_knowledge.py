"""Tests for multiagent/shared_knowledge.py: marker injection and migration."""

from __future__ import annotations

import asyncio
from collections import Counter
from pathlib import Path
from stat import S_IMODE
from threading import Event
from types import SimpleNamespace

import pytest

from ductor_bot.infra.file_lock import LockTimeoutError
from ductor_bot.multiagent import shared_knowledge
from ductor_bot.multiagent.shared_knowledge import (
    _END_MARKER,
    _LEGACY_END,
    _LEGACY_START,
    _START_MARKER,
    _find_markers,
    _sync_agent_io,
)


class TestFindMarkers:
    """Test _find_markers() detection."""

    def test_finds_new_markers(self) -> None:
        text = f"before\n{_START_MARKER}\ncontent\n{_END_MARKER}\nafter"
        result = _find_markers(text)
        assert result == (_START_MARKER, _END_MARKER)

    def test_finds_legacy_markers(self) -> None:
        text = f"before\n{_LEGACY_START}\ncontent\n{_LEGACY_END}\nafter"
        result = _find_markers(text)
        assert result == (_LEGACY_START, _LEGACY_END)

    def test_prefers_new_over_legacy(self) -> None:
        """When both marker types exist, prefer new format."""
        text = f"{_START_MARKER}\n{_LEGACY_START}\ncontent\n{_LEGACY_END}\n{_END_MARKER}"
        result = _find_markers(text)
        assert result == (_START_MARKER, _END_MARKER)

    def test_returns_none_when_no_markers(self) -> None:
        assert _find_markers("plain text without markers") is None

    def test_returns_none_for_empty_string(self) -> None:
        assert _find_markers("") is None


class TestSyncAgentIO:
    """Test _sync_agent_io() file operations."""

    @pytest.fixture
    def shared_path(self, tmp_path: Path) -> Path:
        p = tmp_path / "SHAREDMEMORY.md"
        p.write_text("Shared content here", encoding="utf-8")
        return p

    @pytest.fixture
    def mainmemory_path(self, tmp_path: Path) -> Path:
        p = tmp_path / "workspace" / "memory_system" / "MAINMEMORY.md"
        p.parent.mkdir(parents=True)
        p.write_text("# Main Memory\nAgent notes.\n", encoding="utf-8")
        return p

    def test_injects_into_file_without_markers(
        self, shared_path: Path, mainmemory_path: Path
    ) -> None:
        result = _sync_agent_io(shared_path, mainmemory_path)
        assert result is True

        content = mainmemory_path.read_text()
        assert _START_MARKER in content
        assert _END_MARKER in content
        assert "Shared content here" in content
        assert "# Main Memory" in content

    def test_replaces_existing_markers(self, shared_path: Path, mainmemory_path: Path) -> None:
        # First injection
        _sync_agent_io(shared_path, mainmemory_path)

        # Update shared content
        shared_path.write_text("Updated shared content", encoding="utf-8")
        result = _sync_agent_io(shared_path, mainmemory_path)
        assert result is True

        content = mainmemory_path.read_text()
        assert "Updated shared content" in content
        assert "Shared content here" not in content
        # Only one start/end marker pair
        assert content.count(_START_MARKER) == 1
        assert content.count(_END_MARKER) == 1

    def test_migrates_legacy_markers(self, shared_path: Path, mainmemory_path: Path) -> None:
        """Legacy markers are replaced with new-format markers on sync."""
        legacy_content = f"# Main Memory\n{_LEGACY_START}\nold content\n{_LEGACY_END}\n"
        mainmemory_path.write_text(legacy_content, encoding="utf-8")

        result = _sync_agent_io(shared_path, mainmemory_path)
        assert result is True

        content = mainmemory_path.read_text()
        assert _START_MARKER in content
        assert _END_MARKER in content
        assert _LEGACY_START not in content
        assert _LEGACY_END not in content
        assert "Shared content here" in content

    def test_returns_false_when_shared_missing(self, tmp_path: Path, mainmemory_path: Path) -> None:
        missing = tmp_path / "does_not_exist.md"
        assert _sync_agent_io(missing, mainmemory_path) is False

    def test_returns_false_when_shared_empty(self, tmp_path: Path, mainmemory_path: Path) -> None:
        empty_shared = tmp_path / "empty.md"
        empty_shared.write_text("", encoding="utf-8")
        assert _sync_agent_io(empty_shared, mainmemory_path) is False

    def test_returns_false_when_mainmemory_missing(self, shared_path: Path, tmp_path: Path) -> None:
        missing = tmp_path / "missing_mainmem.md"
        assert _sync_agent_io(shared_path, missing) is False

    def test_returns_false_when_content_unchanged(
        self, shared_path: Path, mainmemory_path: Path
    ) -> None:
        """Second sync with same content returns False (no write)."""
        _sync_agent_io(shared_path, mainmemory_path)
        result = _sync_agent_io(shared_path, mainmemory_path)
        assert result is False

    def test_preserves_content_before_and_after_markers(
        self, shared_path: Path, mainmemory_path: Path
    ) -> None:
        """Content before and after markers is preserved."""
        mainmemory_path.write_text(
            f"# Before\n{_START_MARKER}\nold\n{_END_MARKER}\n# After\n",
            encoding="utf-8",
        )
        result = _sync_agent_io(shared_path, mainmemory_path)
        assert result is True

        content = mainmemory_path.read_text()
        assert content.startswith("# Before\n")
        assert "# After" in content
        assert "Shared content here" in content


    @pytest.mark.parametrize("linked", [False, True])
    def test_preserves_file_mode_and_symlink_target(
        self, shared_path: Path, mainmemory_path: Path, linked: bool
    ) -> None:
        target = mainmemory_path
        if linked:
            target = mainmemory_path.with_name("notes.md")
            mainmemory_path.rename(target)
            mainmemory_path.symlink_to(target.name)
        target.chmod(0o640)
        link_inode = mainmemory_path.lstat().st_ino

        assert _sync_agent_io(shared_path, mainmemory_path)
        assert mainmemory_path.is_symlink() is linked
        if linked:
            assert mainmemory_path.readlink() == Path(target.name)
            assert mainmemory_path.lstat().st_ino == link_inode
        assert "Shared content here" in target.read_text()
        assert S_IMODE(target.stat().st_mode) == 0o640


async def test_retry_finishes_pending_agents_when_inventory_grows(tmp_path, monkeypatch):
    source = tmp_path / "SHAREDMEMORY.md"
    source.write_text("Updated shared block")
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    third = tmp_path / "third.md"
    for target in (first, second, third):
        target.write_text("Agent notes")
    stacks = {
        name: SimpleNamespace(paths=SimpleNamespace(mainmemory_path=target))
        for name, target in (("first", first), ("second", second))
    }
    sync = shared_knowledge.SharedKnowledgeSync(source, SimpleNamespace(stacks=stacks))
    attempts = Counter()
    start_retry = asyncio.Event()
    paused = Event()
    release = Event()
    retry_sync = sync._retry_sync

    async def retry_after_initial_sync():
        await start_retry.wait()
        await retry_sync()

    def contend(shared, target):
        attempts[target] += 1
        if attempts[target] == 1:
            raise LockTimeoutError("fixture contention")
        if target == first:
            paused.set()
            assert release.wait(3)
        return _sync_agent_io(shared, target)

    monkeypatch.setattr(shared_knowledge, "_sync_agent_io", contend)
    monkeypatch.setattr(shared_knowledge, "_SYNC_INTERVAL", 0)
    monkeypatch.setattr(sync, "_retry_sync", retry_after_initial_sync)
    await sync._sync_all()
    retry = sync._retry_task
    assert retry is not None
    try:
        start_retry.set()
        assert await asyncio.to_thread(paused.wait, 3)
        stacks["third"] = SimpleNamespace(paths=SimpleNamespace(mainmemory_path=third))
        release.set()
        await asyncio.wait_for(retry, 3)
    finally:
        release.set()
        await sync.stop()

    assert attempts[first] == attempts[second] == 2
    assert "Updated shared block" in first.read_text()
    assert "Updated shared block" in second.read_text()
