"""Task provenance survives persistence and binds only to its creating turn."""

from __future__ import annotations

import asyncio
import json
import runpy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ductor_bot.multiagent.internal_api import InternalAgentAPI
from ductor_bot.tasks.hub import TaskHub
from ductor_bot.tasks.models import TaskEntry, TaskSubmit
from ductor_bot.tasks.registry import TaskRegistry


@pytest.fixture
def hub(tmp_path):
    registry = TaskRegistry(tmp_path / "tasks.json", tmp_path / "tasks")
    instance = TaskHub(
        registry,
        MagicMock(workspace=tmp_path),
        cli_service=MagicMock(),
        config=SimpleNamespace(enabled=True, max_parallel=20),
    )
    instance._spawn = MagicMock()
    return instance


def create(hub, **overrides):
    fields = {
        "chat_id": 42,
        "prompt": "Synthetic work",
        "message_id": 0,
        "thread_id": 9,
        "parent_agent": "main",
        "origin_transport": "tg",
        "origin_session_kind": "foreground",
    }
    fields.update(overrides)
    task_id = hub.submit(TaskSubmit(**fields))
    return hub.registry.get(task_id)


def finish(hub, entry):
    hub.registry.update_status(entry.task_id, "done", session_id="task-session", provider="claude")


def test_provenance_round_trip_and_legacy_reader(hub):
    entry = create(hub, origin_session_id="parent-session")
    restored = TaskRegistry(hub.registry._path, hub.registry._tasks_dir).get(entry.task_id)
    assert restored.origin_transport == "tg"
    assert restored.origin_session_kind == "foreground"
    assert restored.origin_session_id == "parent-session"
    # Load an isolated, frozen reader from before provenance fields existed.
    old_reader = runpy.run_path(str(Path(__file__).parent / "legacy_task_entry_fixture.py"))
    payload = json.loads(hub.registry._path.read_text())["tasks"][0]
    legacy = old_reader["TaskEntry"].from_dict(payload)
    assert legacy.task_id == entry.task_id
    assert legacy.original_prompt == entry.original_prompt
    assert not hasattr(legacy, "origin_transport")


def test_legacy_records_and_submissions_have_empty_provenance():
    entry = TaskEntry.from_dict({"task_id": "legacy-task", "chat_id": 42})
    submit = TaskSubmit(42, "Work", 0, None, "main")
    for value in (entry, submit):
        assert value.origin_transport == ""
        assert value.origin_session_kind == ""
        assert value.origin_session_id == ""


@pytest.mark.parametrize("replace", [False, True])
def test_resume_replaces_provenance_including_absent_values(hub, replace):
    entry = create(hub, origin_session_id="old-parent")
    finish(hub, entry)
    provenance = (
        {
            "origin_transport": "mx",
            "origin_session_kind": "named",
            "origin_session_id": "new-parent",
        }
        if replace
        else {}
    )
    hub.resume(entry.task_id, "Follow up", chat_id=55, thread_id=12, **provenance)
    assert (entry.chat_id, entry.thread_id) == (55, 12)
    for field in ("origin_transport", "origin_session_kind", "origin_session_id"):
        assert getattr(entry, field) == provenance.get(field, "")
    stored = TaskEntry.from_dict(entry.to_dict())
    assert stored.origin_session_id == provenance.get("origin_session_id", "")


@pytest.mark.parametrize("resumed", [False, True])
def test_first_turn_binding_persists_recent_origin(hub, resumed):
    entry = create(hub, origin_transport="telegram")
    entry.created_at = 100
    entry.instruction_received_at = 90
    if resumed:
        finish(hub, entry)
        hub.resume(
            entry.task_id,
            "New work",
            origin_transport="tg",
            origin_session_kind="foreground",
        )
        entry.instruction_received_at = 210
    hub.bind_first_turn_origin("main", 42, 9, "tg", "parent-session", 200 if resumed else 100)
    restored = TaskRegistry(hub.registry._path, hub.registry._tasks_dir).get(entry.task_id)
    assert restored.origin_session_id == "parent-session"


@pytest.mark.parametrize(
    "overrides",
    [
        {"parent_agent": "other"},
        {"chat_id": 43},
        {"thread_id": 10},
        {"origin_transport": "mx"},
        {"origin_session_kind": "named"},
        {"origin_session_kind": "background"},
        {"origin_session_kind": ""},
        {"origin_session_kind": "unknown"},
        {"origin_session_id": "already-bound"},
        {"created_at": 90},
    ],
)
def test_first_turn_binding_does_not_claim_other_tasks(hub, overrides):
    entry = create(hub)
    entry.created_at = 110
    entry.instruction_received_at = 80
    for field, value in overrides.items():
        setattr(entry, field, value)
    hub.bind_first_turn_origin("main", 42, 9, "tg", "new-parent", 100)
    assert entry.origin_session_id == overrides.get("origin_session_id", "")


async def test_forward_question_uses_origin_transport_and_metadata_only(hub, caplog):
    entry = create(hub, origin_transport="mx")
    delivered = asyncio.Event()
    handler = AsyncMock(side_effect=lambda *_: delivered.set())
    hub.set_question_handler("main", handler)
    caplog.set_level("INFO")
    await hub.forward_question(entry.task_id, "private-question-sentinel")
    await asyncio.wait_for(delivered.wait(), 1)
    args = handler.await_args.args
    assert args[0] == entry.task_id
    assert "private-question-sentinel" in args[1]
    assert args[3:] == (42, 9, "mx")
    assert "private-question-sentinel" not in caplog.text


@pytest.mark.parametrize("endpoint", ["create", "resume"])
@pytest.mark.parametrize(
    ("provenance", "expected"),
    [
        ({}, ("", "", "")),
        ({"transport": "", "source_session_kind": "", "source_session_id": ""}, ("", "", "")),
        (
            {
                "transport": "telegram",
                "source_session_kind": "foreground",
                "source_session_id": "parent",
            },
            ("tg", "foreground", "parent"),
        ),
        ({"transport": "matrix", "source_session_kind": "named"}, ("mx", "named", "")),
        ({"transport": "api", "source_session_kind": "background"}, ("api", "background", "")),
        ({"transport": "slack", "source_session_kind": "unexpected"}, ("slack", "unknown", "")),
    ],
)
async def test_api_stores_normalized_origin(aiohttp_client, hub, endpoint, provenance, expected):
    api = InternalAgentAPI(bus=None, port=0)
    api.set_task_hub(hub)
    client = await aiohttp_client(api._app)
    payload = {"from": "main", "prompt": "Work", "chat_id": 42, "topic_id": 9, **provenance}
    if endpoint == "resume":
        entry = create(hub, origin_session_id="old-parent")
        finish(hub, entry)
        payload["task_id"] = entry.task_id
    response = await client.post("/tasks/" + endpoint, json=payload)
    assert response.status == 200
    result = await response.json()
    assert result["success"]
    entry = hub.registry.get(result["task_id"])
    assert (entry.origin_transport, entry.origin_session_kind, entry.origin_session_id) == expected
    restored = TaskRegistry(hub.registry._path, hub.registry._tasks_dir).get(entry.task_id)
    assert (
        restored.origin_transport,
        restored.origin_session_kind,
        restored.origin_session_id,
    ) == expected
