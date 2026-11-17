"""Contract tests use only temporary homes and the fixture CLI, never real auth."""

from __future__ import annotations

# ruff: noqa: ASYNC240 -- all filesystem operations use tiny, test-owned fixtures
import asyncio
import json
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

import pytest

from ductor_bot.cli.auth import AuthResult, AuthStatus
from ductor_bot.webchat.cli_login import (
    CliLoginError,
    CliLoginManager,
    _Job,
    _TerminalText,
    parse_output,
)

FIXTURES = Path(__file__).parent / "fixtures" / "cli_login"
OWNER = "browser-session-hmac"


@pytest.fixture
async def rig(tmp_path, monkeypatch):
    binary = tmp_path / "bin"
    binary.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    slot = home / "claude-team"
    slot.mkdir()
    monkeypatch.setattr(
        "ductor_bot.webchat.cli_login.container_slot_config_dir", lambda _: str(slot)
    )
    env = {
        "PATH": str(binary) + os.pathsep + os.environ["PATH"],
        "HOME": str(home),
        "CODEX_HOME": str(home / "codex"),
        "GROK_HOME": str(home / "grok"),
        "LANG": "C.UTF-8",
        "TERM": "xterm",
        "NO_PROXY": "localhost",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
    }
    managers = []

    def make(provider="codex", mode="wait", **options):
        config = {
            "mode": mode,
            "record": str(tmp_path / (provider + "-record.json")),
            "input": str(tmp_path / (provider + "-input.txt")),
            **options,
        }
        executable = binary / provider
        executable.write_text(
            "#!"
            + sys.executable
            + "\n"
            + FIXTURES.joinpath("fake_cli.py").read_text().split("\n", 1)[1]
        )
        executable.chmod(0o700)
        executable.with_suffix(".json").write_text(json.dumps(config))
        manager = CliLoginManager(
            startup_providers=frozenset({"codex"}),
            env=env,
            auth_checker=lambda p: AuthResult(p, AuthStatus.AUTHENTICATED),
        )
        managers.append(manager)
        return manager, config

    yield make, env, slot
    for manager in managers:
        await manager.close()


async def until(predicate, deadline_seconds=4):
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        await asyncio.sleep(0.01)
    pytest.fail("fake CLI condition timed out")


async def settled(manager, job_id):
    await until(manager._jobs[job_id].task.done, deadline_seconds=8)
    return manager.get(job_id, OWNER)


def start(manager, provider):
    return manager.start(provider, OWNER, slot="claude-team" if provider == "claude" else None)


@pytest.mark.parametrize(
    ("provider", "argv"),
    [
        ("claude", ["auth", "login"]),
        ("codex", ["login", "--device-auth"]),
        ("grok", ["login", "--device-auth"]),
    ],
)
async def test_provider_argv_env_and_controlling_pty(rig, provider, argv):
    make, env, slot = rig
    manager, config = make(provider)
    job = start(manager, provider)
    record = Path(config["record"])
    await until(record.exists)
    captured = json.loads(record.read_text())
    assert captured["argv"] == argv
    assert captured["tty"]
    assert captured["pid"] == captured["pgrp"]
    if provider == "claude":
        assert captured["env"]["CLAUDE_CONFIG_DIR"] == str(slot)
    else:
        assert captured["env"][f"{provider.upper()}_HOME"] == env[f"{provider.upper()}_HOME"]
    manager.cancel(job, OWNER)
    await settled(manager, job)


@pytest.mark.parametrize("provider", ["claude", "codex", "grok"])
async def test_success_requires_changed_credentials_and_signal(rig, provider):
    make, _, _ = rig
    manager, _ = make(provider, "success")
    job = start(manager, provider)
    result = await settled(manager, job)
    assert result["state"] == "succeeded"
    assert result["restart_required"] == (provider != "codex")
    assert manager._jobs[job].process is None


@pytest.mark.parametrize("provider", ["claude", "codex", "grok"])
async def test_existing_credentials_and_exit_zero_are_not_success(rig, provider):
    make, env, slot = rig
    path = (
        slot / ".credentials.json"
        if provider == "claude"
        else Path(env[f"{provider.upper()}_HOME"]) / "auth.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("existing")
    manager, _ = make(provider, "existing")
    job = start(manager, provider)
    assert (await settled(manager, job))["state"] == "failed"
    assert path.read_text() == "existing"


@pytest.mark.parametrize("provider", ["claude", "codex"])
async def test_changed_credentials_without_success_marker_fail(rig, provider):
    make, _, _ = rig
    manager, _ = make(provider, "file_only")
    job = start(manager, provider)
    assert (await settled(manager, job))["state"] == "failed"


async def test_claude_sigint_exit_zero_is_failure(rig):
    make, _, _ = rig
    manager, config = make("claude", "signal0")
    job = start(manager, "claude")
    await until(lambda: "Signal ready" in str(manager.get(job, OWNER)["output"]))
    process = manager._jobs[job].process
    pid = json.loads(Path(config["record"]).read_text())["pid"]
    os.killpg(pid, signal.SIGINT)
    assert (await settled(manager, job))["state"] == "failed"
    assert process.returncode == 0


@pytest.mark.parametrize("provider", ["claude", "codex", "grok"])
def test_masked_probe_fixtures(provider):
    text = FIXTURES.joinpath(provider + ".txt").read_text()
    parsed = parse_output(provider, text)
    if provider == "claude":
        assert parsed["needs_input"]
    if provider in {"codex", "grok"}:
        assert "MASKED_CODE" in parsed["code"]


def test_split_ansi_osc_utf8_and_control_sequences():
    raw = "\x1b[31m日本語\x1b[0m\r\n\x1b]8;;https://hidden.invalid\x1b\\visible\x1b]8;;\x1b\\\x1b[?25h\x00".encode()
    cleaner = _TerminalText()
    assert "".join(cleaner.feed(bytes([byte])) for byte in raw) == "日本語\nvisible"


async def test_owner_mismatch_and_singleton(rig):
    make, _, _ = rig
    manager, _ = make()
    job = start(manager, "codex")
    with pytest.raises(CliLoginError) as error:
        manager.start("grok", "other-owner")
    assert (error.value.status, error.value.code) == (409, "login_running")
    assert manager.status("other-owner")["job_id"] is None
    assert manager.status(OWNER)["job_id"] == job
    for operation in [
        lambda: manager.get(job, "other-owner"),
        lambda: manager.send_input(job, "other-owner", "line", "1"),
        lambda: manager.cancel(job, "other-owner"),
    ]:
        with pytest.raises(CliLoginError) as error:
            operation()
        assert error.value.status == 404
    manager.cancel(job, OWNER)
    assert (await settled(manager, job))["state"] == "cancelled"


@pytest.mark.parametrize(
    ("kind", "value"),
    [
        ("line", ""),
        ("line", "\x00\r\n"),
        ("line", "x" * 4097),
        ("key", "0"),
        ("key", "12"),
        ("key", "1\r"),
        ("shell", "x"),
    ],
)
async def test_invalid_input(rig, kind, value):
    make, _, _ = rig
    manager, _ = make()
    job = start(manager, "codex")
    with pytest.raises(CliLoginError) as error:
        manager.send_input(job, OWNER, kind, value)
    assert error.value.status == 400


@pytest.mark.parametrize(
    ("kind", "value", "expected"),
    [("line", "a\x00b\n", b"ab\r".hex())],
)
async def test_exact_line_input_bytes(rig, kind, value, expected):
    make, _, _ = rig
    manager, config = make("codex", "input")
    job = start(manager, "codex")
    await until(lambda: "Unrecognized prompt" in str(manager.get(job, OWNER)["output"]))
    manager.send_input(job, OWNER, kind, value)
    received = Path(config["input"])
    await until(received.exists)
    assert received.read_text() == expected
    manager.cancel(job, OWNER)
    await settled(manager, job)


@pytest.mark.parametrize("action", ["cancel", "timeout", "close"])
async def test_process_group_cleanup_and_nonblocking_cancel(rig, tmp_path, action):
    make, _, _ = rig
    child = tmp_path / "child.pid"
    manager, config = make("grok", "child", child_pid=str(child))
    now = [10.0]
    manager._clock = lambda: now[0]
    job = start(manager, "grok")
    await until(child.exists)
    pids = [int(child.read_text()), json.loads(Path(config["record"]).read_text())["pid"]]
    if action == "timeout":
        now[0] += 601
    elif action == "cancel":
        began = time.monotonic()
        manager.cancel(job, OWNER)
        assert time.monotonic() - began < 0.1
    else:
        await manager.close()
    if action != "close":
        result = await settled(manager, job)
        assert result["state"] == ("timed_out" if action == "timeout" else "cancelled")
    for pid in pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


async def test_sigkill_after_grace_keeps_api_immediate(rig):
    make, _, _ = rig
    manager, config = make("codex", "stubborn")
    job = start(manager, "codex")
    await until(lambda: Path(config["record"]).exists())
    await asyncio.sleep(0.05)
    began = time.monotonic()
    manager.cancel(job, OWNER)
    assert manager.get(job, OWNER)["job_id"] == job
    assert time.monotonic() - began < 0.1
    await settled(manager, job)
    assert 4.9 <= time.monotonic() - began < 7


async def test_auth_refresh_does_not_block_status_or_completion(rig):
    make, _, _ = rig
    manager, _ = make("codex", "success")
    calls = []

    def slow_check(provider):
        calls.append(provider)
        time.sleep(0.2)
        return AuthResult(provider, AuthStatus.AUTHENTICATED)

    manager._auth_checker = slow_check
    began = time.monotonic()
    manager.status(OWNER)
    assert time.monotonic() - began < 0.1
    job = start(manager, "codex")
    assert (await settled(manager, job))["state"] == "succeeded"
    assert not manager._refresh_task.done()
    await manager._refresh_task
    assert calls == ["claude", "codex", "grok"]
    assert manager.status(OWNER)["providers"]["grok"] == "authenticated"


async def test_no_terminal_input_output_in_debug_logs(rig, caplog):
    make, _, _ = rig
    caplog.set_level(logging.DEBUG)
    manager, config = make("codex", "input")
    job = start(manager, "codex")
    await until(lambda: "Unrecognized prompt" in str(manager.get(job, OWNER)["output"]))
    manager.send_input(job, OWNER, "line", "never-log-this-code")
    await until(lambda: Path(config["input"]).exists())
    manager.cancel(job, OWNER)
    await settled(manager, job)
    assert "never-log-this-code" not in caplog.text
    assert "Unrecognized prompt" not in caplog.text


def test_ring_is_bounded_and_sequence_advances():
    job = _Job("test", "codex", OWNER)
    for _ in range(200):
        job.append(("日本語" * 200).encode())
    assert job.output_bytes <= 64 * 1024
    assert job.seq == 200
    assert sum(len(chunk.encode()) for _, chunk in job.output) == job.output_bytes


async def test_expiry_and_incremental_output(rig):
    make, _, _ = rig
    manager, _ = make("codex", "success")
    now = [1.0]
    manager._clock = lambda: now[0]
    job = start(manager, "codex")
    result = await settled(manager, job)
    assert result["output"]
    assert manager.get(job, OWNER, after=result["seq"])["output"] == []
    now[0] += 601
    with pytest.raises(CliLoginError) as error:
        manager.get(job, OWNER)
    assert error.value.status == 404


@pytest.mark.parametrize(
    ("provider", "slot"),
    [
        ("shell", None),
        ("claude", None),
        ("claude", "../../escape"),
        ("codex", "claude-team"),
        ("claude", "claude-slots"),
    ],
)
async def test_invalid_provider_slot(rig, provider, slot):
    make, _, _ = rig
    manager, _ = make()
    with pytest.raises(CliLoginError) as error:
        manager.start(provider, OWNER, slot=slot)
    assert error.value.status == 400


@pytest.mark.parametrize("provider", ["claude", "codex", "grok"])
async def test_removed_credentials_are_not_a_successful_change(rig, provider):
    make, env, slot = rig
    path = (
        slot / ".credentials.json"
        if provider == "claude"
        else Path(env[f"{provider.upper()}_HOME"]) / "auth.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("old")
    manager, _ = make(provider, "deleted")
    job = start(manager, provider)
    assert (await settled(manager, job))["state"] == "failed"


async def test_completion_requeues_auth_when_same_provider_probe_is_in_flight(rig):
    make, _, _ = rig
    manager, _ = make("codex", "success")
    ready = threading.Event()
    release = threading.Event()
    calls = []

    def checker(provider):
        calls.append(provider)
        if len(calls) == 1:
            ready.set()
            assert release.wait(4)
        return AuthResult(provider, AuthStatus.AUTHENTICATED)

    manager._auth_checker = checker
    manager.refresh_auth("codex")
    await until(ready.is_set)
    try:
        job = start(manager, "codex")
        assert (await settled(manager, job))["state"] == "succeeded"
    finally:
        release.set()
    await manager._refresh_task
    assert calls == ["codex", "codex"]


@pytest.mark.parametrize("provider", ["claude", "codex", "grok"])
async def test_probe_fixture_replayed_through_pty(rig, provider):
    make, _, _ = rig
    manager, _ = make(provider, fixture=str(FIXTURES / (provider + ".txt")))
    job = start(manager, provider)
    await until(lambda: "MASKED_URL" in str(manager.get(job, OWNER)["output"]))
    result = manager.get(job, OWNER)
    expected = parse_output(provider, (FIXTURES / (provider + ".txt")).read_text())
    for key in ("urls", "code", "needs_input"):
        assert result[key] == expected[key]
    manager.cancel(job, OWNER)
    await settled(manager, job)


async def test_cancel_invalidates_cached_auth_until_probe_finishes(rig):
    make, _, _ = rig
    manager, _ = make("codex")
    manager._auth["codex"] = AuthResult("codex", AuthStatus.AUTHENTICATED)
    ready = threading.Event()
    release = threading.Event()

    def checker(provider):
        ready.set()
        assert release.wait(4)
        return AuthResult(provider, AuthStatus.INSTALLED)

    manager._auth_checker = checker
    job = start(manager, "codex")
    await until(lambda: manager._jobs[job].master is not None)
    manager.cancel(job, OWNER)
    await settled(manager, job)
    try:
        await until(ready.is_set)
        began = time.monotonic()
        assert manager.status(OWNER)["providers"]["codex"] is None
        assert time.monotonic() - began < 0.1
    finally:
        release.set()
    await manager._refresh_task
    assert manager.status(OWNER)["providers"]["codex"] == "installed"


async def test_web_login_excludes_gemini(rig):
    make, _, _ = rig
    manager, _ = make()
    assert tuple(manager.status(OWNER)["providers"]) == ("claude", "codex", "grok")
    with pytest.raises(CliLoginError) as error:
        manager.start("gemini", OWNER)
    assert (error.value.status, error.value.code) == (400, "invalid_login")


async def test_web_login_rejects_single_key_input(rig):
    make, _, _ = rig
    manager, _ = make("codex", "input")
    job = start(manager, "codex")
    await until(lambda: "Unrecognized prompt" in str(manager.get(job, OWNER)["output"]))
    with pytest.raises(CliLoginError) as error:
        manager.send_input(job, OWNER, "key", "1")
    assert (error.value.status, error.value.code) == (400, "invalid_input")


@pytest.mark.parametrize(
    "value",
    ["1", "fake-onetime-code", "Successfully logged in", "https://auth.openai.com/echo-only"],
)
async def test_readline_echo_stays_in_owner_output_without_logging(
    tmp_path, monkeypatch, caplog, value
):
    caplog.set_level(logging.DEBUG)
    url = "https://example.invalid/auth?client_id=fake123&state=retry123&code_challenge=challenge1"
    executable = tmp_path / "echo.mjs"
    executable.write_text(
        "import readline from 'node:readline';\n"
        "const rl=readline.createInterface({input:process.stdin,output:process.stdout,terminal:true});\n"
        "rl.question('Paste code: ',code=>{console.log("
        + json.dumps(url)
        + ");setInterval(()=>{},1000);});\n"
    )
    monkeypatch.setattr(
        "ductor_bot.webchat.cli_login._COMMANDS", {"codex": ("node", str(executable))}
    )
    manager = CliLoginManager(
        startup_providers=frozenset(),
        env={"PATH": os.environ["PATH"], "HOME": str(tmp_path)},
        auth_checker=lambda p: AuthResult(p, AuthStatus.INSTALLED),
    )
    try:
        job = manager.start("codex", OWNER)
        await until(lambda: manager.get(job, OWNER)["needs_input"])
        before = manager.get(job, OWNER)["seq"]
        manager.send_input(job, OWNER, "line", value)
        await until(lambda: url in str(manager.get(job, OWNER)["output"]))
        result = manager.get(job, OWNER, after=before)
        assert not manager._jobs[job].success_signal
        text = "".join(chunk["text"] for chunk in result["output"])
        assert text.splitlines()[0] in {value, "Paste code: " + value}
        assert url in text
        assert result["urls"] == []
        with pytest.raises(CliLoginError) as error:
            manager.get(job, "other-owner")
        assert error.value.status == 404
        assert value not in caplog.text
        assert url not in caplog.text
    finally:
        await manager.close()


@pytest.mark.parametrize("provider", ["claude", "codex", "grok"])
@pytest.mark.parametrize("mode", ["touch", "rewrite", "same_stat"])
async def test_credential_content_decides_success_even_with_identical_metadata(rig, provider, mode):
    make, env, slot = rig
    path = (
        slot / ".credentials.json"
        if provider == "claude"
        else Path(env[f"{provider.upper()}_HOME"]) / "auth.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("old-credential")
    manager, _ = make(provider, mode)
    job = start(manager, provider)
    assert (await settled(manager, job))["state"] == (
        "succeeded" if mode == "same_stat" else "failed"
    )


@pytest.mark.parametrize(
    ("mode", "expected"), [("file_only", "succeeded"), ("fail_after_file", "failed")]
)
async def test_grok_requires_zero_exit_and_changed_content(rig, mode, expected):
    make, _, _ = rig
    manager, _ = make("grok", mode)
    assert (await settled(manager, start(manager, "grok")))["state"] == expected


@pytest.mark.parametrize(
    ("provider", "marker"), [("claude", "Login successful"), ("codex", "Successfully logged in")]
)
@pytest.mark.parametrize(("echo", "cli_success"), [(False, False), (True, False), (True, True)])
async def test_input_marker_cannot_supply_success_signal(rig, provider, marker, echo, cli_success):
    make, _, _ = rig
    manager, _ = make(provider, "echo_marker", echo=echo, cli_success=cli_success)
    job = start(manager, provider)
    await until(lambda: manager.get(job, OWNER)["needs_input"])
    manager.send_input(job, OWNER, "line", marker + "-input")
    result = await settled(manager, job)
    assert result["state"] == ("succeeded" if cli_success else "failed")
    if echo:
        assert marker + "-input" in "".join(chunk["text"] for chunk in result["output"])


@pytest.mark.parametrize(
    ("provider", "host"),
    [("claude", "claude.ai"), ("codex", "auth.openai.com"), ("grok", "accounts.x.ai")],
)
def test_login_urls_require_exact_provider_host(provider, host):
    allowed = f"https://{host}/device?state=test"
    text = "\n".join(
        [
            allowed,
            f"https://sub.{host}/login",
            f"https://{host}.example.com/login",
            f"https://{host}@example.com/login",
            f"https://example.com\\@{host}/login",
            f"https://user@{host}/login",
            "https://example.com/login",
            "https://[malformed]/login",
            *[
                f"https://{other}/login"
                for other in ("claude.ai", "auth.openai.com", "accounts.x.ai")
                if other != host
            ],
        ]
    )
    assert parse_output(provider, text)["urls"] == [allowed]
    assert parse_output(provider, allowed.replace("/device", "/oauth/long\ncontinuation"))[
        "urls"
    ] == [allowed.replace("/device", "/oauth/longcontinuation")]


async def test_orphaned_job_is_claimed_only_after_owner_session_disappears(rig):
    make, _, _ = rig
    manager, _ = make()
    job = start(manager, "codex")
    assert manager.status("next", active_owners=frozenset({OWNER, "next"}))["job_id"] is None
    with pytest.raises(CliLoginError):
        manager.cancel(job, "next")
    state = manager.status("next", active_owners=frozenset({"next"}))
    assert state["running"]
    assert state["job_id"] == job
    assert manager.get(job, "next")["state"] == "running"
    manager.cancel(job, "next")
    await until(manager._jobs[job].task.done)
    assert manager.get(job, "next")["state"] == "cancelled"
    with pytest.raises(CliLoginError):
        manager.get(job, OWNER)


async def test_close_cancels_pending_probes_without_waiting_for_checker(rig):
    make, _, _ = rig
    manager, _ = make()
    ready, release = threading.Event(), threading.Event()
    calls = []

    def checker(provider):
        calls.append(provider)
        ready.set()
        release.wait(4)
        return AuthResult(provider, AuthStatus.INSTALLED)

    manager._auth_checker = checker
    manager.status(OWNER)
    await until(ready.is_set)
    try:
        await asyncio.wait_for(manager.close(), 0.5)
        assert manager._refresh_task.cancelled()
        assert calls == ["claude"]
        assert manager._auth_pending == []
    finally:
        release.set()


async def test_close_kills_running_native_probe_group(rig, tmp_path, monkeypatch):
    from ductor_bot.webchat.cli_login import check_provider_auth

    make, _, _ = rig
    manager, _ = make()
    manager._auth_checker = check_provider_auth
    pid_file = tmp_path / "probe-pids"
    monkeypatch.setattr(
        "ductor_bot.webchat.cli_login._PROBE",
        (
            "import os,time; from pathlib import Path; "
            f"child=os.fork(); Path({str(pid_file)!r}+str(child==0)).write_text(str(os.getpid())); "
            "time.sleep(30)"
        ),
    )
    manager.status(OWNER)
    await until(lambda: all(Path(str(pid_file) + suffix).exists() for suffix in ("True", "False")))
    process = manager._probe_process
    pids = [int(Path(str(pid_file) + suffix).read_text()) for suffix in ("True", "False")]
    await asyncio.wait_for(manager.close(), 0.5)
    assert process.returncode is not None
    for pid in pids:
        stat = Path(f"/proc/{pid}/stat")
        assert not stat.exists() or stat.read_text().split()[2] == "Z"


@pytest.mark.parametrize(
    "script", ["print('broken-json')", "raise SystemExit(3)", 'print(\'{"status":"wrong"}\')']
)
async def test_failed_native_probe_does_not_preserve_authenticated_cache(rig, monkeypatch, script):
    from ductor_bot.webchat.cli_login import check_provider_auth

    make, _, _ = rig
    manager, _ = make()
    manager._auth_checker = check_provider_auth
    manager._auth["codex"] = AuthResult("codex", AuthStatus.AUTHENTICATED)
    monkeypatch.setattr("ductor_bot.webchat.cli_login._PROBE", script)
    manager.refresh_auth("codex")
    await manager._refresh_task
    assert manager.status(OWNER)["providers"]["codex"] is None


@pytest.mark.parametrize("status", list(AuthStatus))
async def test_native_probe_caches_only_reported_status(rig, monkeypatch, status):
    from ductor_bot.webchat.cli_login import check_provider_auth

    make, _, _ = rig
    manager, _ = make()
    manager._auth_checker = check_provider_auth
    monkeypatch.setattr(
        "ductor_bot.webchat.cli_login._PROBE",
        f"print({json.dumps({'status': status.value})!r})",
    )
    manager.refresh_auth("codex")
    await manager._refresh_task
    assert manager._auth["codex"].status == status


async def test_close_reaps_probe_cancelled_during_spawn(rig, monkeypatch):
    from ductor_bot.webchat.cli_login import check_provider_auth

    make, _, _ = rig
    manager, _ = make()
    manager._auth_checker = check_provider_auth
    monkeypatch.setattr("ductor_bot.webchat.cli_login._PROBE", "import time; time.sleep(30)")
    original = asyncio.create_subprocess_exec
    ready, release = asyncio.Event(), asyncio.Event()
    processes = []

    async def spawn(*args, **kwargs):
        process = await original(*args, **kwargs)
        processes.append(process)
        ready.set()
        await release.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    manager.refresh_auth("codex")
    await asyncio.wait_for(ready.wait(), 4)
    closing = asyncio.create_task(manager.close())
    await asyncio.sleep(0)
    release.set()
    await asyncio.wait_for(closing, 0.5)
    assert processes[0].returncode is not None
    with pytest.raises(ProcessLookupError):
        os.killpg(processes[0].pid, 0)


@pytest.mark.parametrize(
    ("provider", "marker"), [("claude", "Login successful"), ("codex", "Successfully logged in")]
)
def test_unechoed_earlier_input_cannot_hide_later_marker_echo(provider, marker):
    job = _Job("test", provider, OWNER)
    job.input_echoes.extend(["raw-choice", marker + "-input"])
    for byte in (marker + "-input\n").encode():
        job.append(bytes([byte]))
    assert not job.success_signal
    job.append((marker + "\n").encode())
    assert job.success_signal


@pytest.mark.parametrize(
    ("provider", "host"),
    [("claude", "claude.ai"), ("codex", "auth.openai.com"), ("grok", "accounts.x.ai")],
)
@pytest.mark.parametrize("cli_url", [False, True])
async def test_input_echo_urls_never_become_login_buttons(rig, tmp_path, provider, host, cli_url):
    make, _, _ = rig
    direct = f"https://{host}/oauth/cli"
    echoed = f"https://{host}/echo-only"
    fixture = tmp_path / "login-output.txt"
    fixture.write_text(
        "\n".join(
            [
                *([direct] if cli_url else []),
                f"https://{host}.evil.example/login",
                f"https://sub.{host}/login",
                *[
                    f"https://{other}/login"
                    for other in ("claude.ai", "auth.openai.com", "accounts.x.ai")
                    if other != host
                ],
            ]
        )
    )
    manager, _ = make(provider, "echo_marker", echo=True, cli_success=False, fixture=str(fixture))
    job = start(manager, provider)
    await until(lambda: manager.get(job, OWNER)["needs_input"])
    manager.send_input(job, OWNER, "line", f"code {echoed} end")
    result = await settled(manager, job)
    assert echoed in "".join(chunk["text"] for chunk in result["output"])
    assert not manager._jobs[job].input_echoes
    assert result["urls"] == ([direct] if cli_url else [])
    assert manager.get(job, OWNER, after=result["seq"])["urls"] == result["urls"]
