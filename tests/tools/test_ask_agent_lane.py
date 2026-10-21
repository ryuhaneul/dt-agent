"""``--lane`` in ask_agent.py / ask_agent_async.py: sent as ``lane``, never invented."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

TOOLS = (
    Path(__file__).resolve().parents[2] / "ductor_bot/_home_defaults/workspace/tools/agent_tools"
)
_ENV = (
    "DUCTOR_AGENT_NAME",
    "DUCTOR_CHAT_ID",
    "DUCTOR_TOPIC_ID",
    "DUCTOR_TRANSPORT",
    "DUCTOR_IA_TURN",
    "DUCTOR_ORIGIN_CHAT_ID",
    "DUCTOR_ORIGIN_TOPIC_ID",
    "DUCTOR_ORIGIN_TRANSPORT",
    "DUCTOR_DELEGATION_ID",
    "DUCTOR_SESSION_NAME",
    "DUCTOR_SESSION_KIND",
    "DUCTOR_SESSION_ID",
)


def _run(
    monkeypatch: pytest.MonkeyPatch, tool: str, argv: list[str]
) -> tuple[dict[str, object], str]:
    for key in _ENV:
        monkeypatch.delenv(key, raising=False)
    spec = importlib.util.spec_from_file_location(f"lane_test_{tool}", TOOLS / f"{tool}.py")
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(
        {"success": True, "task_id": "t1", "text": "ok"}
    ).encode()
    body: dict[str, object] = {}
    urls: list[str] = []

    def urlopen(request: object, timeout: int) -> MagicMock:  # noqa: ARG001
        body.update(json.loads(request.data.decode()))  # type: ignore[attr-defined]
        urls.append(request.full_url)  # type: ignore[attr-defined]
        return response

    monkeypatch.setattr(mod.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(sys, "argv", [f"{tool}.py", *argv])
    mod.main()
    return body, urls[0]


@pytest.mark.parametrize("tool", ["ask_agent", "ask_agent_async"])
def test_lane_flag_is_sent(monkeypatch: pytest.MonkeyPatch, tool: str) -> None:
    body, url = _run(monkeypatch, tool, ["--lane", "build-1", "worker", "do it"])
    assert body["lane"] == "build-1"
    assert (body["to"], body["message"]) == ("worker", "do it")
    assert url.endswith("/interagent/send" if tool == "ask_agent" else "/interagent/send_async")


@pytest.mark.parametrize("tool", ["ask_agent", "ask_agent_async"])
def test_no_lane_flag_sends_no_lane(monkeypatch: pytest.MonkeyPatch, tool: str) -> None:
    body, _ = _run(monkeypatch, tool, ["worker", "do it"])
    assert "lane" not in body


@pytest.mark.parametrize("tool", ["ask_agent", "ask_agent_async"])
def test_lane_combines_with_new_in_any_order(monkeypatch: pytest.MonkeyPatch, tool: str) -> None:
    for argv in (["--new", "--lane", "a", "w", "m"], ["--lane", "a", "--new", "w", "m"]):
        body, _ = _run(monkeypatch, tool, argv)
        assert (body["lane"], body["new_session"], body["to"]) == ("a", True, "w")


def test_async_lane_combines_with_the_other_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    body, _ = _run(
        monkeypatch,
        "ask_agent_async",
        ["--summary", "s", "--lane", "x1", "--silent", "--reply-to", "main", "w", "m"],
    )
    assert (body["lane"], body["summary"], body["silent"], body["reply_to"]) == (
        "x1",
        "s",
        True,
        "main",
    )


@pytest.mark.parametrize("tool", ["ask_agent", "ask_agent_async"])
def test_lane_without_value_is_a_usage_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tool: str
) -> None:
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, tool, ["--lane"])
    assert exc.value.code == 1
    assert "--lane requires a value" in capsys.readouterr().err
