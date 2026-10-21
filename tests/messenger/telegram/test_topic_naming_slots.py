"""Topic titles share account cooldowns without consuming switch notices as data."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ductor_bot.cli.claude_accounts import SlotRegistry, SlotSnapshot
from ductor_bot.config import AgentConfig
from ductor_bot.messenger.telegram.topic_naming import DMTopicNamer
from ductor_bot.session.key import SessionKey
from tests.cli.test_claude_provider import _freeze_slot_clock, _install_slot_cli, _read_slot_calls


@pytest.mark.parametrize(("timezone", "hour", "minute"), [("Asia/Tokyo", 9, 30), ("Europe/Berlin", 16, 30)])
async def test_topic_slot_cooldown_timezone_and_structured_title(
    tmp_path, monkeypatch, timezone, hour, minute
):
    home, control, calls = _install_slot_cli(tmp_path, monkeypatch)
    _freeze_slot_clock(monkeypatch)
    control.write_text(json.dumps({"limited": ["claude-a"], "result": '{"title":"Cache audit"}'}))
    config = AgentConfig(
        ductor_home=str(home),
        provider="claude",
        model="opus",
        language="en",
        claude_accounts=["claude-a", "claude-b"],
        user_timezone=timezone,
    )
    orch = SimpleNamespace(
        paths=SimpleNamespace(ductor_home=home),
        _sessions=SimpleNamespace(get_active=AsyncMock(return_value=None)),
        _process_registry=None,
    )
    monkeypatch.setattr(
        "ductor_bot.messenger.telegram.topic_naming.check_provider_auth",
        lambda _: SimpleNamespace(is_authenticated=True),
    )
    namer = DMTopicNamer(config, MagicMock(), orch, MagicMock())
    registry = SlotRegistry(home / ".cli-auth" / "_claude-slots.json")
    assert await namer._generate(SessionKey(chat_id=1), "Audit the cache") == "Cache audit"
    assert [row["auth"] for row in _read_slot_calls(calls)] == [
        str(home / ".cli-auth" / slot) for slot in config.claude_accounts
    ]
    recorded = registry.load().cooldowns["claude-a"]
    assert datetime.fromtimestamp(recorded, UTC) == datetime(2099, 10, 16, hour, minute, tzinfo=UTC)
    calls.write_text("")
    assert await namer._generate(SessionKey(chat_id=1), "Audit the cache") == "Cache audit"
    assert [row["auth"] for row in _read_slot_calls(calls)] == [
        str(home / ".cli-auth" / "claude-b")
    ]
    control.write_text(json.dumps({"limited": [], "result": '{"title":"Cache audit"}'}))
    for expiry in (time.time() + 3600, time.time() - 1):
        registry.save(SlotSnapshot(cooldowns=dict.fromkeys(config.claude_accounts, expiry)))
        calls.write_text("")
        assert await namer._generate(SessionKey(chat_id=1), "Audit the cache") == "Cache audit"
        assert [row["auth"] for row in _read_slot_calls(calls)] == [
            str(home / ".cli-auth" / "claude-a")
        ]
    assert list(tmp_path.rglob("_claude-slots.json")) == [home / ".cli-auth" / "_claude-slots.json"]
