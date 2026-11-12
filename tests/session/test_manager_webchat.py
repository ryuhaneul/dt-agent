"""Exact session mutation used by web topic management."""

from __future__ import annotations

import asyncio
from dataclasses import asdict

from ductor_bot.config import AgentConfig
from ductor_bot.session.key import SessionKey
from ductor_bot.session.manager import SessionManager


async def test_set_topic_name_preserves_other_session_fields(tmp_path):
    manager = SessionManager(tmp_path / "sessions.json", AgentConfig())
    key = SessionKey.telegram(-123, 7)
    session, _ = await manager.resolve_session(key)
    session.session_id = "provider-session"
    await manager.update_session(session)
    before = asdict(await manager.get_active(key))
    assert await manager.set_topic_name(key, "Renamed")
    after = asdict(await manager.get_active(key))
    assert after == {**before, "topic_name": "Renamed"}
    assert (
        await SessionManager(tmp_path / "sessions.json", AgentConfig()).get_active(key)
    ).topic_name == "Renamed"


async def test_delete_session_is_scoped_and_persistent(tmp_path):
    path = tmp_path / "sessions.json"
    manager = SessionManager(path, AgentConfig())
    keys = [
        SessionKey.telegram(-123, 7),
        SessionKey.telegram(-123, 8),
        SessionKey.telegram(-123),
        SessionKey("mx", -123, 7),
    ]
    for key in keys:
        await manager.resolve_session(key)
    assert await manager.delete_session(keys[0])
    assert not await manager.delete_session(keys[0])
    persisted = SessionManager(path, AgentConfig())
    assert {item.session_key for item in await persisted.list_all()} == set(keys[1:])


async def test_missing_session_mutations_do_not_create_file(tmp_path):
    path = tmp_path / "sessions.json"
    manager = SessionManager(path, AgentConfig())
    key = SessionKey.telegram(123, 7)
    assert not await manager.set_topic_name(key, "Name")
    assert not await manager.delete_session(key)
    assert not path.exists()


async def test_concurrent_mutations_preserve_unrelated_session(tmp_path):
    manager = SessionManager(tmp_path / "sessions.json", AgentConfig())
    first, second, third = (SessionKey.telegram(123, topic) for topic in (1, 2, 3))
    for key in (first, second, third):
        await manager.resolve_session(key)
    await asyncio.gather(manager.set_topic_name(first, "Updated"), manager.delete_session(second))
    assert (await manager.get_active(first)).topic_name == "Updated"
    assert await manager.get_active(second) is None
    assert await manager.get_active(third) is not None
