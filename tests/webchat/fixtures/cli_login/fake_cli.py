#!/usr/bin/env python3
"""Fake PTY CLI; all paths and scenarios are test-owned temporary files."""

import contextlib
import json
import os
import signal
import sys
import time
import tty
from pathlib import Path

executable = Path(sys.argv[0])
scenario = json.loads(executable.with_suffix(".json").read_text())
provider = executable.name


def capture(path, text):
    temporary = path.with_suffix(".capture-tmp")
    temporary.write_text(text)
    temporary.replace(path)


record = Path(scenario["record"])
capture(
    record,
    json.dumps(
        {
            "argv": sys.argv[1:],
            "env": dict(os.environ),
            "cwd": str(Path.cwd()),
            "tty": os.isatty(0),
            "pid": os.getpid(),
            "pgrp": os.getpgrp(),
        }
    ),
)
mode = scenario.get("mode", "wait")
if provider == "claude":
    credentials = Path(os.environ["CLAUDE_CONFIG_DIR"]) / ".credentials.json"
else:
    credentials = Path(os.environ[f"{provider.upper()}_HOME"]) / "auth.json"


def write_credentials():
    credentials.parent.mkdir(parents=True, exist_ok=True)
    credentials.write_text('{"test":"new-credential"}')


if scenario.get("fixture"):
    print(
        Path(scenario["fixture"]).read_text(),
        file=sys.stderr if provider == "grok" else sys.stdout,
        flush=True,
    )
if mode == "success":
    write_credentials()
    print(
        {"claude": "Login successful", "codex": "Successfully logged in"}.get(provider, ""),
        flush=True,
    )
    sys.exit(0)
if mode in {"touch", "rewrite", "same_stat"}:
    before = credentials.stat()
    if mode == "touch":
        os.utime(credentials, ns=(before.st_atime_ns, before.st_mtime_ns + 1000000))
    elif mode == "rewrite":
        credentials.write_bytes(credentials.read_bytes())
    else:
        credentials.write_text("new-credential")
        os.utime(credentials, ns=(before.st_atime_ns, before.st_mtime_ns))
    print("Login successful Successfully logged in", flush=True)
    sys.exit(0)
if mode == "echo_marker":
    print("Paste code:", flush=True)
    value = sys.stdin.readline().strip()
    if scenario["echo"]:
        for char in value:
            print(char, end="", flush=True)
            time.sleep(0.001)
        print(flush=True)
    write_credentials()
    if scenario["cli_success"]:
        print("Login successful Successfully logged in", flush=True)
    sys.exit(0)
if mode == "file_only":
    write_credentials()
    sys.exit(0)
if mode == "signal0":
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    write_credentials()
    print("Signal ready", flush=True)
if mode == "deleted":
    credentials.unlink(missing_ok=True)
    print("Login successful Successfully logged in", flush=True)
    sys.exit(0)
if mode == "existing":
    print("Login successful Successfully logged in", flush=True)
    sys.exit(0)
if mode == "fail_after_file":
    write_credentials()
    sys.exit(3)
if mode == "stubborn":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)


def wait_for_child(child):
    def cleanup(*_):
        with contextlib.suppress(ProcessLookupError):
            os.kill(child, signal.SIGTERM)
        with contextlib.suppress(ChildProcessError):
            os.waitpid(child, 0)
        sys.exit(0)

    signal.signal(signal.SIGTERM, cleanup)
    capture(Path(scenario["child_pid"]), str(child))
    os.waitpid(child, 0)
    sys.exit(0)


if mode == "child":
    child = os.fork()
    if child == 0:
        while True:
            time.sleep(1)

    wait_for_child(child)
if mode == "input":
    tty.setraw(0)
    print("Unrecognized prompt", flush=True)
    received = os.read(0, scenario.get("read_size", 4096))
    capture(Path(scenario["input"]), received.hex())
if mode == "flood":
    print("x" * 80000, flush=True)
while True:
    time.sleep(0.05)
