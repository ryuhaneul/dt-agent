"""Bounded account limit queries against a fake stdio app-server."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import sys
import time
from unittest.mock import Mock

import pytest

from ductor_bot.cli.auth import AuthResult, AuthStatus
from ductor_bot.cli import codex_usage
from ductor_bot.cli.codex_usage import query_codex_usage, parse_codex_usage

RESPONSE = {
    "id": 2,
    "result": {
        "ordinaryUsageAllowed": True,
        "rateLimits": {
            "limitId": "fixture-limit",
            "limitName": None,
            "normalModelSlug": None,
            "primary": {"usedPercent": 16, "windowDurationMins": 10080, "resetsAt": 1793082712},
            "secondary": None,
            "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
            "individualLimit": None,
            "spendControlReached": False,
            "planType": "pro",
            "rateLimitReachedType": None,
        },
        "rateLimitsByLimitId": {
            "codex": {
                "limitId": "fixture-limit",
                "limitName": None,
                "normalModelSlug": None,
                "primary": {"usedPercent": 16, "windowDurationMins": 10080, "resetsAt": 1793082712},
                "secondary": None,
                "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
                "individualLimit": None,
                "spendControlReached": False,
                "planType": "pro",
                "rateLimitReachedType": None,
            }
        },
        "rateLimitResetCredits": {"availableCount": 0, "credits": None},
        "accountId": "fixture-account",
        "rateLimitUpsell": None,
    },
}


@pytest.fixture(autouse=True)
def isolate_query_state(monkeypatch):
    monkeypatch.setattr(codex_usage, "_in_flight", None)
    monkeypatch.setattr(codex_usage, "_completed_at", None)


@pytest.fixture
def server(tmp_path, monkeypatch):
    pid_file = tmp_path / "pids"
    script = tmp_path / "fake-server"

    def setup(mode="normal"):
        script.write_text(
            "#!"
            + sys.executable
            + "\n"
            + 'import json, os, sys, time\nfrom pathlib import Path\nwith Path(PID_LITERAL).open("a") as log:\n    log.write(str(os.getpid()) + chr(10))\nmode = MODE_LITERAL\nif mode == "exit":\n    sys.exit(7)\ninit = json.loads(input())\nassert init["id"] == 1 and init["method"] == "initialize"\nassert set(init["params"]["clientInfo"]) == {"name", "version"}\nprint(json.dumps({"id": 1, "result": {"codexHome": "fixture-home", "platformFamily": "unix", "platformOs": "linux", "userAgent": "fixture-agent"}}), flush=True)\nassert json.loads(input()) == {"method": "initialized"}\nrequest = json.loads(input())\nassert request == {"id": 2, "method": "account/rateLimits/read", "params": {"excludeResetCreditDetails": True}}\nfor method in ("configWarning", "remoteControl/status/changed", "account/updated"):\n    print(json.dumps({"method": method, "params": {}}), flush=True)\nif mode == "timeout":\n    time.sleep(16)\nif mode == "brief-delay":\n    time.sleep(0.1)\nresponse = RESPONSE_LITERAL\nif mode == "deep-json":\n    print("[" * 2000 + "0" + "]" * 2000, flush=True)\nelif mode == "bad-json":\n    print("not-json", flush=True)\nelif mode == "rpc-error":\n    print(json.dumps({"id": 2, "error": {"message": "fixture-account"}}), flush=True)\nelse:\n    if mode == "missing-limits":\n        del response["result"]["rateLimits"]\n    if mode == "bad-window":\n        response["result"]["rateLimits"]["primary"]["usedPercent"] = "16"\n    print(json.dumps(response), flush=True)\nsys.stdin.read()\n'.replace(
                "MODE_LITERAL", repr(mode)
            )
            .replace("PID_LITERAL", repr(str(pid_file)))
            .replace("RESPONSE_LITERAL", repr(RESPONSE))
        )
        script.chmod(0o700)
        monkeypatch.setattr(codex_usage, "which", lambda name: str(script))
        monkeypatch.setattr(
            codex_usage, "check_codex_auth", lambda: AuthResult("codex", AuthStatus.AUTHENTICATED)
        )
        return pid_file

    yield setup
    if pid_file.exists():
        for pid in pid_file.read_text().splitlines():
            with pytest.raises(ProcessLookupError):
                os.kill(int(pid), 0)


async def test_measured_response_notifications_cleanup_and_no_identifiers(server, caplog):
    caplog.set_level(logging.DEBUG, logger=codex_usage.__name__)
    pids = server()
    result = await query_codex_usage()
    assert result.error is None
    assert len(result.windows) == 1
    window = result.windows[0]
    assert (window.used_percent, window.window_minutes, window.resets_at) == (16, 10080, 1793082712)
    assert result.captured_at is not None
    assert "fixture-account" not in repr(result) + caplog.text
    assert "fixture-limit" not in repr(result) + caplog.text
    assert len(pids.read_text().splitlines()) == 1


async def test_concurrent_queries_share_one_process_then_refresh(server, monkeypatch):
    from types import SimpleNamespace
    clock = [50.0]
    monkeypatch.setattr(codex_usage, "time", SimpleNamespace(time=time.time, monotonic=lambda: clock[0]))
    pids = server("brief-delay")
    results = await asyncio.gather(*(query_codex_usage() for _ in range(8)))
    assert all(result is results[0] for result in results)
    assert len(pids.read_text().splitlines()) == 1
    assert (await query_codex_usage()).error is None
    assert len(pids.read_text().splitlines()) == 1
    clock[0] += 10
    assert (await query_codex_usage()).error is None
    assert len(pids.read_text().splitlines()) == 2


@pytest.mark.parametrize("mode", ["bad-json", "rpc-error", "exit", "missing-limits", "bad-window"])
async def test_query_failures_cleanup(server, mode, caplog):
    caplog.set_level(logging.DEBUG, logger=codex_usage.__name__)
    server(mode)
    assert (await query_codex_usage()).error == "invalid_response"
    assert "fixture-account" not in caplog.text


async def test_default_timeout_is_fifteen_seconds_and_reaps_process(server):
    server("timeout")
    start = time.monotonic()
    assert (await query_codex_usage()).error == "timeout"
    assert 14.5 <= time.monotonic() - start < 20


@pytest.mark.parametrize("status", [AuthStatus.INSTALLED, AuthStatus.NOT_FOUND])
async def test_not_authenticated_does_not_spawn(monkeypatch, status):
    monkeypatch.setattr(codex_usage, "which", lambda _: "fake")
    monkeypatch.setattr(codex_usage, "check_codex_auth", lambda: AuthResult("codex", status))
    spawn = Mock(side_effect=AssertionError("must not spawn"))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    assert (await query_codex_usage()).error == "login_required"
    spawn.assert_not_called()


async def test_missing_cli_does_not_check_auth_or_spawn(monkeypatch):
    monkeypatch.setattr(codex_usage, "which", lambda _: None)
    monkeypatch.setattr(codex_usage, "check_codex_auth", Mock(side_effect=AssertionError))
    assert (await query_codex_usage()).error == "login_required"


@pytest.mark.parametrize(
    ("key", "bad"),
    [
        ("usedPercent", -1),
        ("usedPercent", True),
        ("usedPercent", 1.5),
        ("usedPercent", "16"),
        ("windowDurationMins", "300"),
        ("resetsAt", False),
    ],
)
def test_invalid_used_values_rejected(key, bad):
    data = copy.deepcopy(RESPONSE)
    data["result"]["rateLimits"]["primary"][key] = bad
    with pytest.raises(ValueError):
        parse_codex_usage(data["result"])


def test_nullable_windows_and_times():
    assert parse_codex_usage({"rateLimits": {"primary": None, "secondary": None}}).windows == ()
    result = parse_codex_usage(
        {
            "rateLimits": {
                "primary": {"usedPercent": 16, "windowDurationMins": None, "resetsAt": None},
                "secondary": None,
            }
        }
    )
    assert result.windows[0].resets_at is None


def test_measured_response_renders_only_weekly_window():
    from dataclasses import replace
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from ductor_bot.config import AgentConfig
    from ductor_bot.i18n import init, t
    from ductor_bot.orchestrator.selectors.usage_view import render_usage

    now = int(datetime(2026, 10, 21, 13, 30, tzinfo=ZoneInfo("Asia/Seoul")).timestamp())
    result = replace(parse_codex_usage(RESPONSE["result"]), captured_at=now)
    init("ko")
    try:
        text = render_usage(AgentConfig(user_timezone="Asia/Seoul"), {}, result, now=now)
        codex_lines = text[text.index("Codex") :].splitlines()
        assert codex_lines == [
            ("Codex · " + t("usage.queried", time="13:30")),
            " 7d ██░░░░░░░░  16%  10/27 15:31",
        ]
    finally:
        init("en")


def test_rpc_window_requires_camel_case_fields():
    with pytest.raises(ValueError):
        parse_codex_usage(
            {
                "rateLimits": {
                    "primary": {
                        "used_percent": 16,
                        "window_minutes": 10080,
                        "resets_at": 1793082712,
                    }
                }
            }
        )


async def test_query_above_hundred_preserves_percent_and_clips_bar(server, monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from ductor_bot.config import AgentConfig
    from ductor_bot.orchestrator.selectors.usage_view import render_usage

    monkeypatch.setitem(RESPONSE["result"]["rateLimits"]["primary"], "usedPercent", 120)
    server()
    result = await query_codex_usage()
    assert result.error is None
    assert result.windows[0].used_percent == 120
    now = int(datetime(2026, 10, 21, 13, 30, tzinfo=ZoneInfo("Asia/Seoul")).timestamp())
    text = render_usage(AgentConfig(user_timezone="Asia/Seoul"), {}, result, now=now)
    assert [line for line in text.splitlines() if line.startswith(" 7d")] == [
        " 7d ██████████ 120%  10/27 15:31"
    ]


@pytest.fixture
def launcher_server(tmp_path, monkeypatch):
    import ctypes
    import signal
    import subprocess
    from pathlib import Path

    if sys.platform != "linux":
        pytest.skip("Process adoption probe requires Linux")
    libc = ctypes.CDLL(None)
    previous = ctypes.c_int()
    assert libc.prctl(37, ctypes.byref(previous), 0, 0, 0) == 0
    assert libc.prctl(36, 1, 0, 0, 0) == 0
    pids = tmp_path / "native-pids"
    launches = tmp_path / "launcher-pids"
    native = tmp_path / "native.py"
    native.write_text(
        "import json,os,sys,time\nfrom pathlib import Path\n" +
        f"with Path({str(pids)!r}).open('a') as out: out.write(str(os.getpid())+'\\n')\n" +
        "assert json.loads(input())['method']=='initialize'\n" +
        "print(json.dumps({'id':1,'result':{}}),flush=True)\n" +
        "assert json.loads(input())['method']=='initialized'\n" +
        "assert json.loads(input())['method']=='account/rateLimits/read'\n" +
        "mode=os.environ['FAKE_USAGE_MODE']\n" +
        "if mode=='timeout': time.sleep(30)\n" +
        "if mode=='bad-json': print('not JSON',flush=True)\n" +
        f"else: print(json.dumps({RESPONSE!r}),flush=True)\n" +
        "time.sleep(30)\n")
    launcher = tmp_path / "fake-launcher"
    launcher.write_text("#!" + sys.executable + "\n" +
        "import os,subprocess,sys\nfrom pathlib import Path\n" +
        f"with Path({str(launches)!r}).open('a') as out: out.write(str(os.getpid())+'\\n')\n" +
        f"child=subprocess.Popen([sys.executable,{str(native)!r}]+sys.argv[1:])\n" +
        "sys.exit(child.wait())\n")
    launcher.chmod(0o700)
    monkeypatch.setattr(codex_usage, "which", lambda _: str(launcher))
    monkeypatch.setattr(codex_usage, "check_codex_auth",
                        lambda: AuthResult("codex", AuthStatus.AUTHENTICATED))
    unrelated = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(90)"])

    def collected(path):
        return [int(value) for value in path.read_text().splitlines()] if path.exists() else []

    def running(pid):
        path = Path(f"/proc/{pid}/stat")
        return path.exists() and path.read_text().split(") ", 1)[1][0] != "Z"

    try:
        yield pids, launches, collected, running, unrelated
    finally:
        for pid in collected(pids) + collected(launches):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for pid in collected(pids):
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass
        unrelated.kill()
        unrelated.wait(timeout=2)
        assert libc.prctl(36, previous.value, 0, 0, 0) == 0


async def wait_until_tree_stops(pids, running):
    async with asyncio.timeout(2):
        while any(running(pid) for pid in pids):
            await asyncio.sleep(0.05)


@pytest.mark.parametrize("mode", ["success", "bad-json", "timeout"])
async def test_launcher_native_tree_reclaimed_after_query(launcher_server, monkeypatch, mode):
    pids, launches, collected, running, unrelated = launcher_server
    monkeypatch.setenv("FAKE_USAGE_MODE", mode)
    started = time.monotonic()
    result = await query_codex_usage()
    assert result.error == {"success": None, "bad-json": "invalid_response", "timeout": "timeout"}[mode]
    if mode == "timeout":
        assert 14.5 <= time.monotonic() - started < 20
    assert len(collected(pids)) == len(collected(launches)) == 1
    await wait_until_tree_stops(collected(pids) + collected(launches), running)
    assert unrelated.poll() is None


async def test_launcher_queries_share_pending_then_reclaim_each_tree(launcher_server, monkeypatch):
    from types import SimpleNamespace
    clock = [50.0]
    monkeypatch.setattr(codex_usage, "time", SimpleNamespace(time=time.time, monotonic=lambda: clock[0]))
    pids, launches, collected, running, unrelated = launcher_server
    monkeypatch.setenv("FAKE_USAGE_MODE", "success")
    results = await asyncio.gather(*(query_codex_usage() for _ in range(8)))
    assert all(result is results[0] for result in results)
    assert results[0].error is None
    assert len(collected(pids)) == len(collected(launches)) == 1
    await wait_until_tree_stops(collected(pids) + collected(launches), running)
    assert (await query_codex_usage()).error is None
    assert len(collected(pids)) == len(collected(launches)) == 1
    clock[0] += 10
    assert (await query_codex_usage()).error is None
    assert len(collected(pids)) == len(collected(launches)) == 2
    await wait_until_tree_stops(collected(pids) + collected(launches), running)
    assert unrelated.poll() is None


async def test_deep_json_becomes_invalid_response(server):
    server("deep-json")
    assert (await query_codex_usage()).error == "invalid_response"


async def test_finished_queries_reused_for_ten_seconds(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    clock = [50.0]
    monkeypatch.setattr(codex_usage, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    query = AsyncMock(return_value=codex_usage.CodexUsage())
    monkeypatch.setattr(codex_usage, "_query", query)
    monkeypatch.setattr(codex_usage, "_in_flight", None)
    first = await query_codex_usage()
    for _ in range(10):
        assert await query_codex_usage() is first
    assert query.await_count == 1
    clock[0] += 9.99
    assert await query_codex_usage() is first
    assert query.await_count == 1
    clock[0] += 0.01
    await query_codex_usage()
    assert query.await_count == 2


async def test_cleanup_wait_has_five_second_deadline(monkeypatch, caplog):
    from unittest.mock import AsyncMock, MagicMock

    process = MagicMock(returncode=0)
    async def delayed_wait():
        await asyncio.sleep(30)
    process.wait = AsyncMock(side_effect=delayed_wait)
    monkeypatch.setattr(codex_usage, "which", lambda _: "fake-unused")
    monkeypatch.setattr(codex_usage, "check_codex_auth",
                        lambda: AuthResult("codex", AuthStatus.AUTHENTICATED))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    monkeypatch.setattr(codex_usage, "_send", AsyncMock())
    monkeypatch.setattr(codex_usage, "_read_result", AsyncMock(side_effect=[{}, RESPONSE["result"]]))
    started = time.monotonic()
    result = await asyncio.wait_for(codex_usage._query(), 7)
    assert result.error is None
    assert 4.5 <= time.monotonic() - started < 7
    assert len(caplog.records) == 1


async def test_query_cancellation_remains_visible(monkeypatch):
    from unittest.mock import AsyncMock

    monkeypatch.setattr(codex_usage, "which", lambda _: "fake-unused")
    monkeypatch.setattr(codex_usage, "check_codex_auth", Mock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await codex_usage._query()
