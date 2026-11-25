"""Bounded account limit queries against a fake stdio app-server."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import sys
import time
from unittest.mock import AsyncMock, Mock

import pytest

from ductor_bot.cli.auth import AuthResult, AuthStatus
from ductor_bot.cli import codex_usage
from ductor_bot.cli.codex_usage import query_codex_usage, parse_codex_usage

CHATGPT_AUTH_ERROR = (
    '{"error":{"code":-32600,"message":"chatgpt authentication required to read rate limits"},"id":2}'
)
CODEX_ACCOUNT_AUTH_ERROR = (
    '{"error":{"code":-32600,'
    '"message":"codex account authentication required to read rate limits"},"id":2}'
)


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
    monkeypatch.setattr(codex_usage, "_last_successful", None)
    monkeypatch.setattr(codex_usage, "_logged_refresh_errors", set())
    monkeypatch.setattr(codex_usage, "_turn_auth_rejection", None)


@pytest.fixture
def usage_auth_file(tmp_path, monkeypatch):
    from ductor_bot.cli.auth import check_codex_auth

    monkeypatch.setenv("OPENAI_API_KEY", "fixture-api-key")
    monkeypatch.setattr(codex_usage, "check_codex_auth", check_codex_auth)
    auth_file = tmp_path / "codex" / "auth.json"
    auth_file.parent.mkdir()
    return auth_file


@pytest.fixture
def server(tmp_path, monkeypatch, usage_auth_file):
    monkeypatch.setenv("CODEX_HOME", str(usage_auth_file.parent))
    pid_file = tmp_path / "pids"
    script = tmp_path / "fake-server"

    def setup(mode="normal"):
        script.write_text(
            "#!"
            + sys.executable
            + "\n"
            + 'import json, os, sys, time\nfrom pathlib import Path\nwith Path(PID_LITERAL).open("a") as log:\n    log.write(str(os.getpid()) + chr(10))\nmode = MODE_LITERAL\nif mode == "exit":\n    sys.exit(7)\ninit = json.loads(input())\nassert init["id"] == 1 and init["method"] == "initialize"\nassert set(init["params"]["clientInfo"]) == {"name", "version"}\nprint(json.dumps({"id": 1, "result": {"codexHome": "fixture-home", "platformFamily": "unix", "platformOs": "linux", "userAgent": "fixture-agent"}}), flush=True)\nassert json.loads(input()) == {"method": "initialized"}\nrequest = json.loads(input())\nassert request == {"id": 2, "method": "account/rateLimits/read", "params": {"excludeResetCreditDetails": True}}\nfor method in ("configWarning", "remoteControl/status/changed", "account/updated"):\n    print(json.dumps({"method": method, "params": {}}), flush=True)\nif mode == "timeout":\n    time.sleep(16)\nif mode == "brief-delay":\n    time.sleep(0.1)\nresponse = RESPONSE_LITERAL\nif mode == "deep-json":\n    print("[" * 2000 + "0" + "]" * 2000, flush=True)\nelif mode == "bad-json":\n    print("not-json", flush=True)\nelif mode == "rpc-error":\n    print(json.dumps({"id": 2, "error": {"code": -32600, "message": "fixture-account"}}), flush=True)\nelif mode == "chatgpt-auth-required":\n    print(CHATGPT_AUTH_ERROR_LITERAL, flush=True)\nelif mode == "codex-account-auth-required":\n    print(CODEX_ACCOUNT_AUTH_ERROR_LITERAL, flush=True)\nelse:\n    if mode == "missing-limits":\n        del response["result"]["rateLimits"]\n    if mode == "bad-window":\n        response["result"]["rateLimits"]["primary"]["usedPercent"] = "16"\n    print(json.dumps(response), flush=True)\nsys.stdin.read()\n'.replace(
                "MODE_LITERAL", repr(mode)
            )
            .replace("PID_LITERAL", repr(str(pid_file)))
            .replace("RESPONSE_LITERAL", repr(RESPONSE))
            .replace("CHATGPT_AUTH_ERROR_LITERAL", repr(CHATGPT_AUTH_ERROR))
            .replace("CODEX_ACCOUNT_AUTH_ERROR_LITERAL", repr(CODEX_ACCOUNT_AUTH_ERROR))
        )
        script.chmod(0o700)
        monkeypatch.setattr(codex_usage, "which", lambda name: str(script))
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


async def test_last_successful_usage_is_read_only_and_survives_failures(monkeypatch):
    from types import SimpleNamespace

    clock = [50.0]
    monkeypatch.setattr(codex_usage, "time", SimpleNamespace(time=time.time, monotonic=lambda: clock[0]))
    initial = parse_codex_usage(RESPONSE["result"])
    latest = parse_codex_usage(RESPONSE["result"])
    failed = codex_usage.CodexUsage(error="timeout")
    query = AsyncMock(side_effect=[initial, latest, failed])
    monkeypatch.setattr(codex_usage, "_query", query)

    assert codex_usage.last_codex_usage() is None
    query.assert_not_awaited()
    assert await query_codex_usage() is initial
    assert codex_usage.last_codex_usage() is initial

    clock[0] += 10
    assert codex_usage.last_codex_usage() is initial
    assert query.await_count == 1
    assert await query_codex_usage() is latest
    assert codex_usage.last_codex_usage() is latest

    clock[0] += 10
    assert await query_codex_usage() is failed
    clock[0] += 3600
    assert codex_usage.last_codex_usage() is latest
    assert query.await_count == 3


@pytest.mark.parametrize("provider", ["codex", "claude"])
@pytest.mark.parametrize("mode", ["oneshot", "stream", "fallback"])
async def test_turn_completion_schedules_usage_without_waiting(monkeypatch, provider, mode):
    from ductor_bot.cli.stream_events import ResultEvent
    from ductor_bot.cli.types import AgentRequest, CLIResponse
    from tests.cli.test_service import _make_service

    service = _make_service()
    release = asyncio.Event()
    started = asyncio.Event()

    async def query():
        started.set()
        await release.wait()
        return parse_codex_usage(RESPONSE["result"])

    async def stream(**_kwargs):
        if mode != "fallback":
            yield ResultEvent(type="result", result="completed")

    usage_query = AsyncMock(wraps=codex_usage.query_codex_usage)
    monkeypatch.setattr(codex_usage, "query_codex_usage", usage_query)
    monkeypatch.setattr(codex_usage, "_query", query)
    monkeypatch.setattr(
        service, "_make_cli", lambda _request: Mock(
            send=AsyncMock(return_value=CLIResponse(result="completed")), send_streaming=stream
        )
    )
    request = AgentRequest(prompt="Example", provider_override=provider)
    execute = service.execute if mode == "oneshot" else service.execute_streaming
    try:
        response = await asyncio.wait_for(execute(request), timeout=2)
        assert response.result == "completed"
        if provider == "codex":
            await asyncio.wait_for(started.wait(), timeout=2)
            usage_query.assert_awaited_once_with(reuse_completed=False)
        else:
            usage_query.assert_not_awaited()
        assert codex_usage.last_codex_usage() is None
    finally:
        release.set()
        await asyncio.gather(*codex_usage._turn_refreshes)

    if provider == "codex":
        assert codex_usage.last_codex_usage().windows[0].used_percent == 16
    else:
        assert codex_usage.last_codex_usage() is None


async def test_turn_usage_refreshes_bypass_completed_cache_and_share_pending_queries(monkeypatch):
    from types import SimpleNamespace

    clock = [0.0]
    release = asyncio.Event()
    observations = []

    async def query():
        observations.append(clock[0])
        await release.wait()
        return parse_codex_usage(RESPONSE["result"])

    monkeypatch.setattr(codex_usage, "_query", query)
    monkeypatch.setattr(
        codex_usage, "time", SimpleNamespace(time=lambda: clock[0], monotonic=lambda: clock[0])
    )

    for timestamp in (0.0, 1.0, 11.0):
        clock[0] = timestamp
        release.clear()
        for _ in range(50):
            codex_usage.schedule_codex_usage_refresh()
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(*codex_usage._turn_refreshes)
        await query_codex_usage()
        assert observations[-1] == timestamp

    assert observations == [0.0, 1.0, 11.0]


async def test_turn_usage_refresh_logs_failure_once(monkeypatch, caplog):
    query = AsyncMock(return_value=codex_usage.CodexUsage(error="timeout"))
    monkeypatch.setattr(codex_usage, "_query", query)
    caplog.set_level(logging.DEBUG, logger=codex_usage.__name__)

    for _ in range(3):
        codex_usage.schedule_codex_usage_refresh()
        await asyncio.gather(*codex_usage._turn_refreshes)

    assert query.await_count == 3
    assert caplog.record_tuples == [
        (codex_usage.__name__, level, "Codex usage refresh failed: timeout")
        for level in (logging.WARNING, logging.DEBUG, logging.DEBUG)
    ]


async def _complete_usage_refresh():
    codex_usage.schedule_codex_usage_refresh()
    await asyncio.gather(*codex_usage._turn_refreshes)


async def test_chatgpt_required_skips_next_turn(server, usage_auth_file, caplog):
    caplog.set_level(logging.DEBUG, logger=codex_usage.__name__)
    usage_auth_file.write_text('{"OPENAI_API_KEY":"fixture-api-key"}')
    pids = server("chatgpt-auth-required")
    await _complete_usage_refresh()
    await _complete_usage_refresh()

    assert len(pids.read_text().splitlines()) == 1
    stat = usage_auth_file.stat()
    os.utime(usage_auth_file, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    await _complete_usage_refresh()
    assert [
        level for _, level, message in caplog.record_tuples
        if message == "Codex usage refresh failed: chatgpt_auth_required"
    ] == [logging.WARNING, logging.DEBUG]


@pytest.mark.parametrize("existing_file", [True, False])
async def test_auth_file_timestamp_change_resumes_turn_usage(server, usage_auth_file, existing_file):
    if existing_file:
        usage_auth_file.write_text('{"OPENAI_API_KEY":"fixture-api-key"}')
    pids = server("chatgpt-auth-required" if existing_file else "codex-account-auth-required")
    await _complete_usage_refresh()
    await _complete_usage_refresh()
    assert len(pids.read_text().splitlines()) == 1

    if existing_file:
        stat = usage_auth_file.stat()
        os.utime(usage_auth_file, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    else:
        usage_auth_file.write_text("{}")
    server()
    await _complete_usage_refresh()
    assert len(pids.read_text().splitlines()) == 2
    assert codex_usage.last_codex_usage() is not None


async def test_auth_file_one_nanosecond_change_resumes_turn_usage(server, usage_auth_file):
    initial_mtime = 1_793_000_000_000_000_000
    usage_auth_file.write_text('{"OPENAI_API_KEY":"fixture-api-key"}')
    os.utime(usage_auth_file, ns=(initial_mtime, initial_mtime))
    pids = server("chatgpt-auth-required")
    await _complete_usage_refresh()
    assert len(pids.read_text().splitlines()) == 1
    await _complete_usage_refresh()
    assert len(pids.read_text().splitlines()) == 1

    observed_initial_mtime = usage_auth_file.stat().st_mtime_ns
    replacement = usage_auth_file.with_suffix(".next.json")
    replacement.write_text('{"auth_mode":"chatgpt","tokens":{"access_token":"fixture-token"}}')
    os.utime(replacement, ns=(initial_mtime, initial_mtime + 1))
    replacement.replace(usage_auth_file)
    if usage_auth_file.stat().st_mtime_ns != observed_initial_mtime + 1:
        pytest.skip("Filesystem does not preserve a one-nanosecond timestamp change")

    server()
    await _complete_usage_refresh()
    assert len(pids.read_text().splitlines()) == 2
    assert codex_usage.last_codex_usage() is not None


async def test_manual_usage_query_bypasses_turn_skip(server):
    from ductor_bot.config import AgentConfig
    from ductor_bot.orchestrator.selectors.usage_view import render_usage

    pids = server("codex-account-auth-required")
    await _complete_usage_refresh()
    await _complete_usage_refresh()
    assert len(pids.read_text().splitlines()) == 1

    result = await query_codex_usage(reuse_completed=False)
    assert result.error == "chatgpt_auth_required"
    cfg = AgentConfig()
    assert render_usage(cfg, {}, result) == render_usage(
        cfg, {}, codex_usage.CodexUsage(error="invalid_response")
    )
    assert len(pids.read_text().splitlines()) == 2
    await _complete_usage_refresh()
    assert len(pids.read_text().splitlines()) == 2


@pytest.mark.parametrize("mode", ["rpc-error", "timeout"])
async def test_transient_usage_failure_retries_next_turn(server, monkeypatch, mode):
    monkeypatch.setattr(codex_usage, "QUERY_TIMEOUT", 1)
    pids = server(mode)
    await _complete_usage_refresh()
    assert (await query_codex_usage()).error == {"rpc-error": "invalid_response", "timeout": "timeout"}[mode]
    server()
    await _complete_usage_refresh()

    assert len(pids.read_text().splitlines()) == 2
    assert codex_usage.last_codex_usage() is not None


def test_event_loop_shutdown_reaps_turn_usage_query(server, caplog):
    pids = server("timeout")

    async def run():
        # Debug mode also warns about slow callbacks, which depends on machine load.
        asyncio.get_running_loop().slow_callback_duration = 60
        codex_usage.schedule_codex_usage_refresh()
        async with asyncio.timeout(5):
            while not pids.exists() or not pids.read_text().strip():
                await asyncio.sleep(0.01)

    asyncio.run(run(), debug=True)

    assert not codex_usage._turn_refreshes
    assert codex_usage._in_flight.cancelled()
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]
