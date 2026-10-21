"""Argument handling of the workspace tools that read ``sys.argv`` directly.

Every tool answers ``-h``/``--help`` with its usage and exit 0, rejects an unknown option and
a surplus positional argument with a non-zero exit, and does none of its work in those cases.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

LOOPBACK = socket.gethostbyname("localhost")
TOOLS = (
    Path(__file__).resolve().parents[2] / "ductor_bot" / "_home_defaults" / "workspace" / "tools"
)

# (script, leading arguments, most positional arguments the tool takes)
HTTP_TOOLS = {
    "ask_agent": ("agent_tools/ask_agent.py", [], 2),
    "ask_agent_async": ("agent_tools/ask_agent_async.py", [], 2),
    "ask_parent": ("task_tools/ask_parent.py", [], 1),
    "cancel_task": ("task_tools/cancel_task.py", [], 1),
    "create_task": ("task_tools/create_task.py", [], 1),
    "delete_task": ("task_tools/delete_task.py", [], 1),
    "resume_task": ("task_tools/resume_task.py", [], 2),
    "browser_navigate": ("browser_tools/browser.py", ["navigate"], 1),
    "browser_snapshot": ("browser_tools/browser.py", ["snapshot"], 0),
    "browser_act": ("browser_tools/browser.py", ["act"], 3),
    "browser_screenshot": ("browser_tools/browser.py", ["screenshot"], 0),
    "browser_evaluate": ("browser_tools/browser.py", ["evaluate"], 1),
    "browser_health": ("browser_tools/browser.py", ["health"], 0),
}
FILE_TOOLS = {
    "remove_agent": ("agent_tools/remove_agent.py", 1),
    "cron_remove": ("cron_tools/cron_remove.py", 1),
    "webhook_remove": ("webhook_tools/webhook_remove.py", 1),
    "webhook_rotate_token": ("webhook_tools/webhook_rotate_token.py", 1),
}


class _Recorder:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, object]]] = []


@pytest.fixture
def server() -> Iterator[tuple[_Recorder, int]]:
    recorder = _Recorder()

    class Handler(BaseHTTPRequestHandler):
        def _answer(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            recorder.requests.append((method, self.path, json.loads(raw) if raw else {}))
            payload = json.dumps(
                {"success": True, "text": "ok", "task_id": "t-1", "answer": "ok"}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self) -> None:
            self._answer("POST")

        def do_GET(self) -> None:
            self._answer("GET")

        def log_message(self, *args: object) -> None:
            return

    httpd = HTTPServer((LOOPBACK, 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield recorder, httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()


def _env(tmp_path: Path, port: int = 1) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("DUCTOR_")}
    env.update(
        DUCTOR_HOME=str(tmp_path),
        DUCTOR_INTERAGENT_HOST=LOOPBACK,
        DUCTOR_INTERAGENT_PORT=str(port),
        DUCTOR_AGENT_NAME="tester",
        BROWSER_URL=f"http://{LOOPBACK}:{port}",
    )
    return env


def _run(script: str, args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(TOOLS / script), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )


def _http_args(name: str, extra: list[str]) -> tuple[str, list[str], int]:
    script, lead, count = HTTP_TOOLS[name]
    return script, [*lead, *extra], count


@pytest.mark.parametrize("flag", ["-h", "--help"])
@pytest.mark.parametrize("name", list(HTTP_TOOLS))
def test_help_prints_usage_without_any_request(
    name: str, flag: str, server: tuple[_Recorder, int], tmp_path: Path
) -> None:
    recorder, port = server
    script, args, _ = _http_args(name, [flag])
    env = _env(tmp_path, port)
    env["DUCTOR_TASK_ID"] = "task-1"

    result = _run(script, args, env)

    assert result.returncode == 0, result.stderr
    assert "python3" in result.stdout or "browser.py" in result.stdout
    assert result.stderr == ""
    assert recorder.requests == []


@pytest.mark.parametrize("name", list(HTTP_TOOLS))
def test_unknown_option_fails_without_any_request(
    name: str, server: tuple[_Recorder, int], tmp_path: Path
) -> None:
    recorder, port = server
    _, _, count = _http_args(name, [])
    script, args, _ = _http_args(name, ["--nmae", *["value"] * count])
    env = _env(tmp_path, port)
    env["DUCTOR_TASK_ID"] = "task-1"

    result = _run(script, args, env)

    assert result.returncode != 0
    assert "unknown option '--nmae'" in result.stderr
    assert "Usage" in result.stderr or "browser.py" in result.stderr
    assert recorder.requests == []


@pytest.mark.parametrize("name", list(HTTP_TOOLS))
def test_surplus_argument_fails_without_any_request(
    name: str, server: tuple[_Recorder, int], tmp_path: Path
) -> None:
    recorder, port = server
    _, _, count = _http_args(name, [])
    script, args, _ = _http_args(name, ["one", "two", "three", "four"][: count + 1])
    env = _env(tmp_path, port)
    env["DUCTOR_TASK_ID"] = "task-1"

    result = _run(script, args, env)

    assert result.returncode != 0
    assert "too many arguments" in result.stderr
    assert "quotes" in result.stderr
    assert recorder.requests == []


@pytest.mark.parametrize(
    ("script", "flag"),
    [("task_tools/create_task.py", "--name"), ("agent_tools/ask_agent.py", "--lane")],
)
def test_option_without_a_value_fails(
    script: str, flag: str, server: tuple[_Recorder, int], tmp_path: Path
) -> None:
    recorder, port = server

    result = _run(script, [flag], _env(tmp_path, port))

    assert result.returncode != 0
    assert f"{flag} requires a value" in result.stderr
    assert recorder.requests == []


# -- a dash-leading text after ``--`` is sent as is, and ordinary calls are unchanged ------------


def _only_request(recorder: _Recorder) -> dict[str, object]:
    assert len(recorder.requests) == 1
    return recorder.requests[0][2]


def test_create_task_sends_dash_leading_prompt_after_double_dash(
    server: tuple[_Recorder, int], tmp_path: Path
) -> None:
    recorder, port = server

    result = _run(
        "task_tools/create_task.py", ["--name", "n", "--", "-x do it"], _env(tmp_path, port)
    )

    assert result.returncode == 0, result.stderr
    body = _only_request(recorder)
    assert body["prompt"] == "-x do it"
    assert body["name"] == "n"


def test_create_task_ordinary_call(server: tuple[_Recorder, int], tmp_path: Path) -> None:
    recorder, port = server
    args = ["--name", "N", "--provider", "codex", "--model", "m", "--thinking", "low"]
    args += ["--priority", "batch", "the prompt with spaces"]

    result = _run("task_tools/create_task.py", args, _env(tmp_path, port))

    assert result.returncode == 0, result.stderr
    assert recorder.requests[0][1] == "/tasks/create"
    body = _only_request(recorder)
    assert body == {
        "from": "tester",
        "prompt": "the prompt with spaces",
        "name": "N",
        "provider": "codex",
        "model": "m",
        "thinking": "low",
        "priority": "batch",
    }
    assert "Background task 'N' created" in result.stdout


def test_create_task_rejects_unknown_priority_without_a_request(
    server: tuple[_Recorder, int], tmp_path: Path
) -> None:
    recorder, port = server

    result = _run("task_tools/create_task.py", ["--priority", "soon", "p"], _env(tmp_path, port))

    assert result.returncode == 1
    assert "unknown priority 'soon'" in result.stderr
    assert recorder.requests == []


@pytest.mark.parametrize("script", ["agent_tools/ask_agent.py", "agent_tools/ask_agent_async.py"])
def test_ask_agent_tools_send_dash_leading_message_after_double_dash(
    script: str, server: tuple[_Recorder, int], tmp_path: Path
) -> None:
    recorder, port = server

    result = _run(script, ["main", "--", "-x a message"], _env(tmp_path, port))

    assert result.returncode == 0, result.stderr
    body = _only_request(recorder)
    assert body["to"] == "main"
    assert body["message"] == "-x a message"


def test_ask_agent_ordinary_call(server: tuple[_Recorder, int], tmp_path: Path) -> None:
    recorder, port = server

    result = _run(
        "agent_tools/ask_agent.py",
        ["--new", "--lane", "lane-a", "main", "hello there"],
        _env(tmp_path, port),
    )

    assert result.returncode == 0, result.stderr
    assert recorder.requests[0][1] == "/interagent/send"
    assert _only_request(recorder) == {
        "from": "tester",
        "to": "main",
        "message": "hello there",
        "new_session": True,
        "lane": "lane-a",
    }


def test_ask_agent_async_ordinary_call(server: tuple[_Recorder, int], tmp_path: Path) -> None:
    recorder, port = server
    args = ["--new", "--lane", "lane-a", "--summary", "short", "--reply-to", "boss", "--silent"]
    args += ["main", "hello there"]

    result = _run("agent_tools/ask_agent_async.py", args, _env(tmp_path, port))

    assert result.returncode == 0, result.stderr
    assert recorder.requests[0][1] == "/interagent/send_async"
    assert _only_request(recorder) == {
        "from": "tester",
        "to": "main",
        "message": "hello there",
        "new_session": True,
        "lane": "lane-a",
        "summary": "short",
        "reply_to": "boss",
        "silent": True,
    }


def test_ask_parent_sends_dash_leading_question_after_double_dash(
    server: tuple[_Recorder, int], tmp_path: Path
) -> None:
    recorder, port = server
    env = _env(tmp_path, port)
    env["DUCTOR_TASK_ID"] = "task-1"

    result = _run("task_tools/ask_parent.py", ["--", "-which one?"], env)

    assert result.returncode == 0, result.stderr
    assert _only_request(recorder) == {"task_id": "task-1", "question": "-which one?"}


def test_resume_task_and_id_tools_ordinary_calls(
    server: tuple[_Recorder, int], tmp_path: Path
) -> None:
    recorder, port = server
    env = _env(tmp_path, port)

    assert _run("task_tools/resume_task.py", ["t-1", "-x follow up"], env).returncode == 0
    assert _run("task_tools/cancel_task.py", ["t-1"], env).returncode == 0
    assert _run("task_tools/delete_task.py", ["t-1"], env).returncode == 0

    assert [(path, body.get("task_id")) for _, path, body in recorder.requests] == [
        ("/tasks/resume", "t-1"),
        ("/tasks/cancel", "t-1"),
        ("/tasks/delete", "t-1"),
    ]
    assert recorder.requests[0][2]["prompt"] == "-x follow up"


def test_browser_ordinary_calls(server: tuple[_Recorder, int], tmp_path: Path) -> None:
    recorder, port = server
    env = _env(tmp_path, port)

    calls = [
        ["navigate", "--insecure", "https://example.test"],
        ["snapshot"],
        ["act", "click", "3"],
        ["act", "type", "3", "some value"],
        ["screenshot", "--full"],
        ["evaluate", "document.title"],
        ["evaluate", "--", "-1 + 2"],
        ["health"],
    ]
    for call in calls:
        assert _run("browser_tools/browser.py", call, env).returncode == 0, call

    assert [(m, p, b) for m, p, b in recorder.requests] == [
        ("POST", "/navigate", {"url": "https://example.test", "insecure": True}),
        ("POST", "/snapshot", {}),
        ("POST", "/act", {"action": "click", "ref": 3}),
        ("POST", "/act", {"action": "type", "ref": 3, "value": "some value"}),
        ("POST", "/screenshot", {"fullPage": True}),
        ("POST", "/evaluate", {"expression": "document.title"}),
        ("POST", "/evaluate", {"expression": "-1 + 2"}),
        ("GET", "/health", {}),
    ]


def test_browser_top_level_arguments(server: tuple[_Recorder, int], tmp_path: Path) -> None:
    recorder, port = server
    env = _env(tmp_path, port)

    assert _run("browser_tools/browser.py", ["--help"], env).returncode == 0
    unknown = _run("browser_tools/browser.py", ["--bogus"], env)
    assert unknown.returncode != 0
    assert "unknown option '--bogus'" in unknown.stderr
    assert _run("browser_tools/browser.py", [], env).returncode == 1
    assert _run("browser_tools/browser.py", ["frobnicate"], env).returncode == 1
    assert recorder.requests == []


# -- tools that change files ----------------------------------------------------------------------


def _file_fixture(name: str, home: Path) -> tuple[Path, list[Path]]:
    """Create the state the tool would change; return (state file, paths that must stay)."""
    if name == "remove_agent":
        state = home / "agents.json"
        state.write_text(json.dumps([{"name": "helper"}]), encoding="utf-8")
        return state, []
    if name == "cron_remove":
        state = home / "cron_jobs.json"
        state.write_text(json.dumps({"jobs": [{"id": "job1", "task_folder": "job1"}]}))
        folder = home / "workspace" / "cron_tasks" / "job1"
        folder.mkdir(parents=True)
        (folder / "TASK_DESCRIPTION.md").write_text("keep", encoding="utf-8")
        return state, [folder / "TASK_DESCRIPTION.md"]
    state = home / "webhooks.json"
    hook = {"id": "hk", "title": "t", "auth_mode": "bearer", "token": "token-0"}
    state.write_text(json.dumps({"hooks": [hook]}), encoding="utf-8")
    return state, []


def _target(name: str) -> str:
    return {"remove_agent": "helper", "cron_remove": "job1"}.get(name, "hk")


@pytest.mark.parametrize("flag", ["-h", "--help"])
@pytest.mark.parametrize("name", list(FILE_TOOLS))
def test_file_tool_help_changes_nothing(name: str, flag: str, tmp_path: Path) -> None:
    script, _ = FILE_TOOLS[name]
    state, keep = _file_fixture(name, tmp_path)
    before = state.read_bytes()

    result = _run(script, [flag], _env(tmp_path))

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() != ""
    assert state.read_bytes() == before
    assert all(path.exists() for path in keep)


@pytest.mark.parametrize("name", list(FILE_TOOLS))
def test_file_tool_unknown_option_changes_nothing(name: str, tmp_path: Path) -> None:
    script, _ = FILE_TOOLS[name]
    state, keep = _file_fixture(name, tmp_path)
    before = state.read_bytes()

    result = _run(script, ["--bogus", _target(name)], _env(tmp_path))

    assert result.returncode != 0
    assert "unknown option '--bogus'" in result.stderr
    assert state.read_bytes() == before
    assert all(path.exists() for path in keep)


@pytest.mark.parametrize("name", list(FILE_TOOLS))
def test_file_tool_surplus_argument_changes_nothing(name: str, tmp_path: Path) -> None:
    script, _ = FILE_TOOLS[name]
    state, keep = _file_fixture(name, tmp_path)
    before = state.read_bytes()

    result = _run(script, [_target(name), "other"], _env(tmp_path))

    assert result.returncode != 0
    assert "too many arguments" in result.stderr
    assert "quotes" in result.stderr
    assert state.read_bytes() == before
    assert all(path.exists() for path in keep)


def test_file_tools_still_work_with_one_id(tmp_path: Path) -> None:
    for name, expected in (
        ("remove_agent", "removed from registry"),
        ("cron_remove", '"json_entry_removed": true'),
        ("webhook_remove", '"json_entry_removed": true'),
    ):
        script, _ = FILE_TOOLS[name]
        state, _keep = _file_fixture(name, tmp_path)
        result = _run(script, [_target(name)], _env(tmp_path))
        assert result.returncode == 0, (name, result.stdout, result.stderr)
        assert expected in result.stdout

    state, _keep = _file_fixture("webhook_rotate_token", tmp_path)
    assert _run("webhook_tools/webhook_rotate_token.py", [], _env(tmp_path)).returncode == 0
    rotated = json.loads(state.read_text(encoding="utf-8"))["hooks"][0]["token"]
    assert rotated != "token-0"
    state, _keep = _file_fixture("webhook_rotate_token", tmp_path)
    assert _run("webhook_tools/webhook_rotate_token.py", ["hk"], _env(tmp_path)).returncode == 0
    assert json.loads(state.read_text(encoding="utf-8"))["hooks"][0]["token"] != "token-0"


def test_id_tools_without_an_id_keep_their_old_exit(tmp_path: Path) -> None:
    for name in ("cron_remove", "webhook_remove"):
        script, _ = FILE_TOOLS[name]
        _file_fixture(name, tmp_path)
        result = _run(script, [], _env(tmp_path))
        assert result.returncode == 1
        assert "USAGE" in result.stdout


# -- help wherever it stands, values and separators ----------------------------------------------

# (tool, arguments): ``-h``/``--help`` at a positional or an option-value position.
HELP_ANYWHERE = [
    ("ask_agent", ["main", "--help"]),
    ("ask_agent", ["--lane", "--help", "main", "message"]),
    ("ask_agent_async", ["main", "-h"]),
    ("ask_agent_async", ["--summary", "-h", "main", "message"]),
    ("ask_parent", ["a question", "--help"]),
    ("cancel_task", ["fake-task", "-h"]),
    ("delete_task", ["fake-task", "--help"]),
    ("resume_task", ["fake-task", "--help"]),
    ("create_task", ["--name", "--help", "quoted prompt"]),
    ("create_task", ["quoted prompt", "-h"]),
    ("browser_act", ["type", "7", "--help"]),
    ("browser_navigate", ["https://example.test", "-h"]),
    ("browser_evaluate", ["1 + 1", "--help"]),
]


@pytest.mark.parametrize(("name", "extra"), HELP_ANYWHERE)
def test_help_at_a_positional_or_value_position_prints_usage_only(
    name: str, extra: list[str], server: tuple[_Recorder, int], tmp_path: Path
) -> None:
    recorder, port = server
    script, args, _ = _http_args(name, extra)
    env = _env(tmp_path, port)
    env["DUCTOR_TASK_ID"] = "task-1"

    result = _run(script, args, env)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() != ""
    assert result.stderr == ""
    assert recorder.requests == []


@pytest.mark.parametrize("name", list(FILE_TOOLS))
@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_file_tool_help_after_the_id_changes_nothing(name: str, flag: str, tmp_path: Path) -> None:
    script, _ = FILE_TOOLS[name]
    state, keep = _file_fixture(name, tmp_path)
    before = state.read_bytes()

    result = _run(script, [_target(name), flag], _env(tmp_path))

    assert result.returncode == 0, result.stderr
    assert state.read_bytes() == before
    assert all(path.exists() for path in keep)


MISSING_VALUE = [
    ("create_task", ["--name", "--", "p"]),
    ("create_task", ["--name", "--nmae", "p"]),
    ("create_task", ["--priority", "-x", "p"]),
    ("ask_agent", ["--lane", "--", "main", "m"]),
    ("ask_agent", ["--lane", "-x", "main", "m"]),
    ("ask_agent_async", ["--summary", "--nmae", "main", "m"]),
    ("ask_agent_async", ["--reply-to", "--silent", "main", "m"]),
]


@pytest.mark.parametrize(("name", "extra"), MISSING_VALUE)
def test_option_followed_by_a_separator_or_option_has_no_value(
    name: str, extra: list[str], server: tuple[_Recorder, int], tmp_path: Path
) -> None:
    recorder, port = server
    script, args, _ = _http_args(name, extra)

    result = _run(script, args, _env(tmp_path, port))

    assert result.returncode != 0
    assert f"{extra[0]} requires a value" in result.stderr
    assert "Usage" in result.stderr
    assert recorder.requests == []


# (tool, arguments, expected path, expected payload fields)
AFTER_SEPARATOR = [
    ("create_task", ["--", "--help"], "/tasks/create", {"prompt": "--help"}),
    ("create_task", ["--name", "n", "--", "-x text"], "/tasks/create", {"prompt": "-x text"}),
    (
        "ask_agent",
        ["main", "--", "--help"],
        "/interagent/send",
        {"to": "main", "message": "--help"},
    ),
    ("ask_agent", ["--", "main", "-x"], "/interagent/send", {"to": "main", "message": "-x"}),
    (
        "ask_agent_async",
        ["main", "--", "-h"],
        "/interagent/send_async",
        {"to": "main", "message": "-h"},
    ),
    (
        "resume_task",
        ["t-1", "--", "--help"],
        "/tasks/resume",
        {"task_id": "t-1", "prompt": "--help"},
    ),
    ("ask_parent", ["--", "--help"], "/tasks/ask_parent", {"question": "--help"}),
    (
        "browser_act",
        ["type", "7", "--", "-x text"],
        "/act",
        {"action": "type", "ref": 7, "value": "-x text"},
    ),
    ("browser_act", ["click", "--", "7"], "/act", {"action": "click", "ref": 7}),
    ("browser_evaluate", ["--", "--help"], "/evaluate", {"expression": "--help"}),
    ("browser_evaluate", ["--", "-x"], "/evaluate", {"expression": "-x"}),
    ("browser_navigate", ["--", "-x"], "/navigate", {"url": "-x", "insecure": False}),
]


@pytest.mark.parametrize("case", AFTER_SEPARATOR)
def test_text_after_the_separator_is_sent_as_it_is(
    case: tuple[str, list[str], str, dict[str, object]],
    server: tuple[_Recorder, int],
    tmp_path: Path,
) -> None:
    name, extra, path, fields = case
    recorder, port = server
    script, args, _ = _http_args(name, extra)
    env = _env(tmp_path, port)
    env["DUCTOR_TASK_ID"] = "task-1"

    result = _run(script, args, env)

    assert result.returncode == 0, result.stderr
    assert len(recorder.requests) == 1
    assert recorder.requests[0][1] == path
    body = recorder.requests[0][2]
    assert {key: body[key] for key in fields} == fields


@pytest.mark.parametrize(
    "extra",
    [
        ["act", "click", "7", "value", "extra"],
        ["act", "click", "7", "--", "value", "extra"],
        ["act", "--", "click", "7", "value", "extra"],
    ],
)
def test_browser_surplus_after_the_separator_is_still_an_error(
    extra: list[str], server: tuple[_Recorder, int], tmp_path: Path
) -> None:
    recorder, port = server

    result = _run("browser_tools/browser.py", extra, _env(tmp_path, port))

    assert result.returncode != 0
    assert "too many arguments" in result.stderr
    assert recorder.requests == []
