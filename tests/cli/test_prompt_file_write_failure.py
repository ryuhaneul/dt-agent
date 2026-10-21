"""A failed write/close of a private prompt file leaves no partial file behind."""

from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from ductor_bot.cli.base import CLIConfig, write_private_temp_file
from ductor_bot.cli.claude_provider import ClaudeCodeCLI
from ductor_bot.cli.gemini_utils import create_system_prompt_file
from ductor_bot.cli.grok_provider import GrokCLI
from ductor_bot.cli.param_resolver import TaskExecutionConfig
from ductor_bot.cron.execution import build_cmd

REPO = Path(__file__).resolve().parents[2]
TEXT = "SYNTHETIC_PRIVATE_CONTENT " * 100  # far larger than the 64-byte file-size limit below

# Child process: a real OS write failure (EFBIG via RLIMIT_FSIZE), nothing mocked.
_CHILD = r"""
import json, pathlib, resource, signal, sys, tempfile
tempfile.tempdir = sys.argv[1]
resource.setrlimit(resource.RLIMIT_FSIZE, (64, 64))
signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
case = sys.argv[2]
text = "SYNTHETIC_PRIVATE_CONTENT " * 100
error = None
try:
    from ductor_bot.cli.base import CLIConfig
    if case.startswith("claude"):
        from ductor_bot.cli.claude_provider import ClaudeCodeCLI
        ClaudeCodeCLI._find_cli = staticmethod(lambda: "synthetic-cli")
        cfg = CLIConfig(
            system_prompt={"claude-system": text, "claude-both": "SHORT_SYSTEM"}.get(case),
            append_system_prompt=text if case in ("claude-append", "claude-both") else None,
        )
        ClaudeCodeCLI(cfg)._write_prompt_files()
    elif case == "gemini":
        from ductor_bot.cli.gemini_utils import create_system_prompt_file
        create_system_prompt_file(text)
    elif case == "grok":
        from ductor_bot.cli.grok_provider import GrokCLI
        GrokCLI._find_cli = staticmethod(lambda: "synthetic-cli")
        GrokCLI(CLIConfig())._build_command(text)
    else:
        from ductor_bot.cli.param_resolver import TaskExecutionConfig
        from ductor_bot.cron import execution
        execution.which = lambda _: "synthetic-cli"
        execution.build_cmd(
            TaskExecutionConfig(
                provider="grok", model="", reasoning_effort="", cli_parameters=[],
                permission_mode="bypassPermissions", working_dir=sys.argv[1], file_access="all",
            ),
            text,
        )
except OSError as exc:
    error = exc.errno
left = [p.name for p in pathlib.Path(sys.argv[1]).iterdir()]
print(json.dumps({"errno": error, "left": left}))
"""


@pytest.mark.parametrize(
    "case", ["claude-system", "claude-append", "claude-both", "gemini", "grok", "cron-grok"]
)
def test_real_os_write_failure_removes_every_partial_file(tmp_path: Path, case: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", _CHILD, str(tmp_path), case],
        capture_output=True,
        text=True,
        check=True,
        cwd=REPO,
        env={**os.environ, "PYTHONPATH": str(REPO), "PYTHONDONTWRITEBYTECODE": "1"},
    )
    evidence = json.loads(result.stdout)
    assert evidence["errno"] == errno.EFBIG
    assert evidence["left"] == []


class _FailingWriter:
    """Wraps a real temp file; the write fails with ENOSPC after 5 characters."""

    def __init__(self, real, *, fail_on: str = "write") -> None:
        self.real = real
        self.name = real.name
        self.fail_on = fail_on

    def __enter__(self):
        return self

    def __exit__(self, *exc: object) -> None:
        self.real.close()
        if self.fail_on == "close" and exc[0] is None:
            raise OSError(errno.ENOSPC, "Synthetic disk full on close")

    def write(self, text: str) -> None:
        self.real.write(text[:5])
        self.real.flush()
        if self.fail_on == "write":
            raise OSError(errno.ENOSPC, "Synthetic disk full")


@pytest.fixture
def created(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Route every temp file into tmp_path, record them, make writes/closes fail."""
    made: list[Path] = []
    real_factory = tempfile.NamedTemporaryFile
    mode = {"fail_on": "write"}

    def factory(*args, **kwargs):
        kwargs["dir"] = str(tmp_path)
        real = real_factory(*args, **kwargs)
        made.append(Path(real.name))
        return _FailingWriter(real, fail_on=mode["fail_on"])

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", factory)
    monkeypatch.setattr(GrokCLI, "_find_cli", staticmethod(lambda: "synthetic-grok"))
    monkeypatch.setattr("ductor_bot.cron.execution.which", lambda _: "synthetic-grok")
    monkeypatch.setattr("ductor_bot.cli.claude_provider.which", lambda _: "synthetic-claude")
    made_mode = mode
    yield made, made_mode
    for path in made:
        path.unlink(missing_ok=True)


def _cron_grok() -> None:
    build_cmd(
        TaskExecutionConfig(
            provider="grok",
            model="",
            reasoning_effort="",
            cli_parameters=[],
            permission_mode="bypassPermissions",
            working_dir="/tmp",
            file_access="all",
        ),
        TEXT,
    )


_ENTRYPOINTS = {
    "claude-system": lambda: ClaudeCodeCLI(CLIConfig(system_prompt=TEXT))._write_prompt_files(),
    "claude-both": lambda: ClaudeCodeCLI(
        CLIConfig(system_prompt="SHORT", append_system_prompt=TEXT)
    )._write_prompt_files(),
    "gemini": lambda: create_system_prompt_file(TEXT),
    "grok": lambda: GrokCLI(CLIConfig())._build_command(TEXT),
    "cron-grok": _cron_grok,
}


@pytest.mark.parametrize("case", sorted(_ENTRYPOINTS))
@pytest.mark.parametrize("fail_on", ["write", "close"])
def test_synthetic_enospc_removes_files_and_keeps_the_original_error(
    created, case: str, fail_on: str
) -> None:
    made, mode = created
    mode["fail_on"] = fail_on
    with pytest.raises(OSError, match="Synthetic disk full") as caught:
        _ENTRYPOINTS[case]()
    assert caught.value.errno == errno.ENOSPC
    assert made, "the failing path must have created a file"
    assert not any(path.exists() for path in made), "partial prompt file persists"


def test_claude_removes_the_first_file_when_the_second_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ductor_bot.cli.claude_provider.which", lambda _: "synthetic-claude")
    real_factory = tempfile.NamedTemporaryFile
    calls = {"n": 0}
    made: list[Path] = []

    def factory(*args, **kwargs):
        kwargs["dir"] = str(tmp_path)
        real = real_factory(*args, **kwargs)
        made.append(Path(real.name))
        calls["n"] += 1
        return _FailingWriter(real) if calls["n"] == 2 else real

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", factory)
    cli = ClaudeCodeCLI(CLIConfig(system_prompt="SYSTEM", append_system_prompt=TEXT))
    with pytest.raises(OSError, match="Synthetic disk full"):
        cli._write_prompt_files()
    assert len(made) == 2
    assert not any(path.exists() for path in made)


def test_cleanup_failure_does_not_hide_the_write_error(
    created, monkeypatch: pytest.MonkeyPatch
) -> None:
    made, _ = created
    monkeypatch.setattr(Path, "unlink", lambda *_a, **_k: (_ for _ in ()).throw(PermissionError()))
    with pytest.raises(OSError, match="Synthetic disk full") as caught:
        write_private_temp_file(TEXT, prefix="x-", suffix=".md")
    assert caught.value.errno == errno.ENOSPC
    monkeypatch.undo()
    for path in made:
        path.unlink(missing_ok=True)


def test_successful_write_keeps_a_private_complete_file(tmp_path: Path) -> None:
    with patch("tempfile.tempdir", str(tmp_path)):
        path = Path(write_private_temp_file("hello\n", prefix="p-", suffix=".txt"))
    try:
        assert path.read_text(encoding="utf-8") == "hello\n"
        assert path.stat().st_mode & 0o777 == 0o600
    finally:
        path.unlink(missing_ok=True)
