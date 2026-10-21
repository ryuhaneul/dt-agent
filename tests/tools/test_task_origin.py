"""Task command-line tools propagate caller provenance without exposing it."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("tool", ["create_task", "resume_task"])
@pytest.mark.parametrize("with_origin", [False, True])
def test_tools_forward_origin_environment(monkeypatch, tool, with_origin):
    path = (
        Path(__file__).resolve().parents[2]
        / "ductor_bot/_home_defaults/workspace/tools/task_tools"
        / (tool + ".py")
    )
    spec = importlib.util.spec_from_file_location("task_tool_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sent = {}

    def post(_url, body, **_kwargs):
        sent.update(body)
        return {"success": True, "task_id": "sample-task"}

    monkeypatch.setattr(
        module, "_load_shared", lambda: (lambda _path: "unused", post, lambda: "main")
    )
    monkeypatch.setattr(
        module,
        "_load_parser",
        lambda: (
            lambda *_args, **_kwargs: (
                {},
                ["sample-task", "Work"] if tool == "resume_task" else ["Work"],
            )
        ),
    )
    monkeypatch.setattr(sys, "argv", [tool + ".py"])
    if with_origin:
        for key, value in {
            "DUCTOR_TRANSPORT": "api",
            "DUCTOR_SESSION_KIND": "foreground",
            "DUCTOR_SESSION_ID": "parent-session",
        }.items():
            monkeypatch.setenv(key, value)
    module.main()
    expected = (
        {
            "transport": "api",
            "source_session_kind": "foreground",
            "source_session_id": "parent-session",
        }
        if with_origin
        else {}
    )
    assert {
        key: sent[key]
        for key in ("transport", "source_session_kind", "source_session_id")
        if key in sent
    } == expected
