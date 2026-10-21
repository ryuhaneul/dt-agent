"""Tests for the ClaudeCodeCLI provider -- send(), send_streaming(), edge cases."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ductor_bot.cli.base import CLIConfig, add_cli_opt
from ductor_bot.cli.claude_accounts import SlotSnapshot
from ductor_bot.cli.claude_provider import (
    ClaudeCodeCLI,
    _log_cmd,
    _parse_response,
    _unlink_prompt_files,
)
from ductor_bot.cli.process_registry import ProcessRegistry
from ductor_bot.cli.stream_events import (
    AssistantTextDelta,
    ResultEvent,
    StreamEvent,
    SystemInitEvent,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_EXEC_PATH = "ductor_bot.cli.executor.asyncio.create_subprocess_exec"


@pytest.fixture(autouse=True)
def _default_non_windows_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep platform-sensitive command tests stable on Windows hosts."""
    monkeypatch.setattr("ductor_bot.cli.base._IS_WINDOWS", False)
    monkeypatch.setattr("ductor_bot.cli.executor._IS_WINDOWS", False)


def _make_cli(
    monkeypatch: pytest.MonkeyPatch,
    *,
    model: str = "opus",
    process_registry: ProcessRegistry | None = None,
    chat_id: int = 1,
    **kwargs: Any,
) -> ClaudeCodeCLI:
    """Create a ClaudeCodeCLI with `which` stubbed out."""
    monkeypatch.setattr("ductor_bot.cli.claude_provider.which", lambda _: "/usr/bin/claude")
    cfg = CLIConfig(
        provider="claude",
        model=model,
        process_registry=process_registry,
        chat_id=chat_id,
        **kwargs,
    )
    return ClaudeCodeCLI(cfg)


def _fake_process(
    stdout: bytes = b"",
    stderr: bytes = b"",
    returncode: int = 0,
) -> MagicMock:
    """Build a mock asyncio.subprocess.Process."""
    proc = MagicMock(spec=asyncio.subprocess.Process)
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.wait = AsyncMock(return_value=returncode)
    proc.returncode = returncode
    proc.pid = 12345
    proc.kill = MagicMock()
    proc.stdin = MagicMock()
    return proc


def _fake_streaming_process(
    lines: list[bytes],
    stderr: bytes = b"",
    returncode: int = 0,
) -> MagicMock:
    """Build a mock process whose stdout.readline() yields lines then b""."""
    proc = MagicMock(spec=asyncio.subprocess.Process)
    proc.pid = 12345
    proc.returncode = returncode
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=returncode)
    proc.stdin = MagicMock()

    line_iter = iter([*lines, b""])
    proc.stdout = MagicMock()
    proc.stdout.readline = AsyncMock(side_effect=lambda: next(line_iter))
    proc.stderr = MagicMock()
    proc.stderr.read = AsyncMock(return_value=stderr)
    return proc


async def _collect_stream(
    cli: ClaudeCodeCLI,
    prompt: str = "hello",
    **kwargs: Any,
) -> list[StreamEvent]:
    """Exhaust send_streaming() and return all events as a list."""
    return [event async for event in cli.send_streaming(prompt, **kwargs)]


# ---------------------------------------------------------------------------
# __init__ / _find_cli
# ---------------------------------------------------------------------------


class TestInit:
    def test_find_cli_not_found_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("ductor_bot.cli.claude_provider.which", lambda _: None)
        with pytest.raises(FileNotFoundError, match="claude CLI not found"):
            ClaudeCodeCLI(CLIConfig(provider="claude"))

    def test_working_dir_resolved(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr("ductor_bot.cli.claude_provider.which", lambda _: "/usr/bin/claude")
        cfg = CLIConfig(provider="claude", working_dir=str(tmp_path / "sub" / ".."))
        cli = ClaudeCodeCLI(cfg)
        assert cli._working_dir == tmp_path.resolve()


# ---------------------------------------------------------------------------
# _build_command edge cases not covered by test_providers.py
# ---------------------------------------------------------------------------


class TestBuildCommand:
    def test_max_budget_usd(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch, max_budget_usd=2.5)
        cmd = cli._build_command()
        assert "--max-budget-usd" in cmd
        idx = cmd.index("--max-budget-usd")
        assert cmd[idx + 1] == "2.5"

    def test_disallowed_tools(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch, disallowed_tools=["Bash", "Write"])
        cmd = cli._build_command()
        assert "--disallowedTools" in cmd
        idx = cmd.index("--disallowedTools")
        assert cmd[idx + 1] == "Bash"
        assert cmd[idx + 2] == "Write"

    def test_resume_takes_precedence_over_continue(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When both resume_session and continue_session are set, --resume wins."""
        cli = _make_cli(monkeypatch)
        cmd = cli._build_command(resume_session="sess-1", continue_session=True)
        assert "--resume" in cmd
        assert "--continue" not in cmd

    def test_no_none_values_in_command(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Ensure optional None fields do not produce '--flag None' pairs."""
        monkeypatch.setattr("ductor_bot.cli.claude_provider.which", lambda _: "/usr/bin/claude")
        cfg = CLIConfig(provider="claude", model=None, max_turns=None, max_budget_usd=None)
        cli = ClaudeCodeCLI(cfg)
        cmd = cli._build_command()
        assert "None" not in cmd

    def test_effort_passed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch, reasoning_effort="high")
        cmd = cli._build_command()
        assert "--effort" in cmd
        assert cmd[cmd.index("--effort") + 1] == "high"

    def test_effort_max_passed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch, reasoning_effort="max")
        cmd = cli._build_command()
        assert cmd[cmd.index("--effort") + 1] == "max"

    def test_effort_skipped_when_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch, reasoning_effort="default")
        cmd = cli._build_command()
        assert "--effort" not in cmd

    def test_effort_skipped_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch, reasoning_effort="")
        cmd = cli._build_command()
        assert "--effort" not in cmd

    @pytest.mark.parametrize("model", ["haiku", "sonnet", "opus"])
    def test_model_variants(self, monkeypatch: pytest.MonkeyPatch, model: str) -> None:
        cli = _make_cli(monkeypatch, model=model)
        cmd = cli._build_command()
        idx = cmd.index("--model")
        assert cmd[idx + 1] == model

    def test_argv_carries_no_prompt_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(
            monkeypatch,
            allowed_tools=["Read"],
            system_prompt="Be nice",
            append_system_prompt="Be brief",
            max_turns=10,
        )
        files = cli._write_prompt_files()
        try:
            cmd = cli._build_command(prompt_files=files)
        finally:
            _unlink_prompt_files(files)
        assert "--" not in cmd
        assert not {"Be nice", "Be brief"} & set(cmd)
        assert "--system-prompt" not in cmd
        assert "--append-system-prompt" not in cmd


class TestBuildCommandStreaming:
    def test_replaces_json_with_stream_json(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        cmd = cli._build_command_streaming()
        assert "stream-json" in cmd
        assert "json" not in cmd

    def test_verbose_flag_added(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        cmd = cli._build_command_streaming()
        assert "--verbose" in cmd

    def test_verbose_not_duplicated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        cmd = cli._build_command_streaming()
        assert cmd.count("--verbose") == 1

    def test_json_not_in_command_defensive_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Cover the except ValueError branch when 'json' is absent from command."""
        cli = _make_cli(monkeypatch)
        with patch.object(
            cli,
            "_build_command",
            return_value=["/usr/bin/claude", "-p", "--output-format", "text", "hello"],
        ):
            cmd = cli._build_command_streaming()
        assert "text" in cmd
        assert "--verbose" in cmd

    def test_resume_carried_to_streaming(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        cmd = cli._build_command_streaming(resume_session="sess-7")
        assert "--resume" in cmd
        assert "sess-7" in cmd
        assert "stream-json" in cmd


# ---------------------------------------------------------------------------
# send() -- async execution
# ---------------------------------------------------------------------------


class TestSend:
    async def test_happy_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        data = {
            "session_id": "sess-1",
            "result": "Done!",
            "is_error": False,
            "total_cost_usd": 0.03,
            "num_turns": 2,
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }
        proc = _fake_process(stdout=json.dumps(data).encode(), returncode=0)

        with patch(_EXEC_PATH, return_value=proc):
            resp = await cli.send("hello")

        assert resp.result == "Done!"
        assert resp.session_id == "sess-1"
        assert resp.is_error is False
        assert resp.timed_out is False

    async def test_timeout_returns_timed_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        proc = _fake_process()
        proc.communicate = AsyncMock(side_effect=TimeoutError)
        proc.wait = AsyncMock()

        with patch(_EXEC_PATH, return_value=proc):
            resp = await cli.send("hello", timeout_seconds=1.0)

        assert resp.timed_out is True
        assert resp.is_error is True
        assert resp.result == ""
        proc.wait.assert_awaited_once()

    async def test_empty_stdout_is_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        proc = _fake_process(stdout=b"", returncode=1)

        with patch(_EXEC_PATH, return_value=proc):
            resp = await cli.send("hello")

        assert resp.is_error is True

    async def test_process_registry_register_unregister(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registry = ProcessRegistry()
        cli = _make_cli(monkeypatch, process_registry=registry, chat_id=42)
        data = {"result": "OK", "is_error": False}
        proc = _fake_process(stdout=json.dumps(data).encode())

        with patch(_EXEC_PATH, return_value=proc):
            resp = await cli.send("hello")

        assert resp.result == "OK"
        assert not registry.has_active(42)

    async def test_process_registry_unregister_on_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registry = ProcessRegistry()
        cli = _make_cli(monkeypatch, process_registry=registry, chat_id=42)
        proc = _fake_process()
        proc.communicate = AsyncMock(side_effect=TimeoutError)
        proc.wait = AsyncMock()

        with patch(_EXEC_PATH, return_value=proc):
            await cli.send("hello", timeout_seconds=0.1)

        assert not registry.has_active(42)

    async def test_no_registry_does_not_crash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch, process_registry=None)
        data = {"result": "OK"}
        proc = _fake_process(stdout=json.dumps(data).encode())

        with patch(_EXEC_PATH, return_value=proc):
            resp = await cli.send("hello")

        assert resp.result == "OK"

    @pytest.mark.parametrize(
        ("resume_session", "continue_session"),
        [
            ("sess-abc", False),
            (None, True),
            (None, False),
        ],
    )
    async def test_session_flags_forwarded(
        self,
        monkeypatch: pytest.MonkeyPatch,
        resume_session: str | None,
        continue_session: bool,
    ) -> None:
        cli = _make_cli(monkeypatch)
        data = {"result": "OK"}
        proc = _fake_process(stdout=json.dumps(data).encode())

        with patch(_EXEC_PATH, return_value=proc) as mock_exec:
            await cli.send(
                "hello",
                resume_session=resume_session,
                continue_session=continue_session,
            )

        called_cmd = mock_exec.call_args[0]
        if resume_session:
            assert "--resume" in called_cmd
            assert resume_session in called_cmd
        elif continue_session:
            assert "--continue" in called_cmd
        else:
            assert "--resume" not in called_cmd
            assert "--continue" not in called_cmd

    async def test_stderr_captured_in_response(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        data = {"result": "ok", "is_error": False}
        proc = _fake_process(
            stdout=json.dumps(data).encode(),
            stderr=b"some warning",
            returncode=0,
        )

        with patch(_EXEC_PATH, return_value=proc):
            resp = await cli.send("hello")

        assert resp.stderr == "some warning"

    async def test_invalid_json_stdout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        proc = _fake_process(stdout=b"not json at all!", returncode=0)

        with patch(_EXEC_PATH, return_value=proc):
            resp = await cli.send("hello")

        assert resp.is_error is True
        assert "not json at all!" in resp.result


# ---------------------------------------------------------------------------
# send_streaming() -- NDJSON stream processing
# ---------------------------------------------------------------------------


class TestSendStreaming:
    async def test_happy_path_yields_events(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        init_line = (
            json.dumps({"type": "system", "subtype": "init", "session_id": "sess-1"}).encode()
            + b"\n"
        )
        assistant_line = (
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "Hello world"}]},
                }
            ).encode()
            + b"\n"
        )
        result_line = (
            json.dumps(
                {
                    "type": "result",
                    "session_id": "sess-1",
                    "result": "Hello world",
                    "is_error": False,
                    "total_cost_usd": 0.02,
                    "usage": {"input_tokens": 50, "output_tokens": 25},
                }
            ).encode()
            + b"\n"
        )

        proc = _fake_streaming_process([init_line, assistant_line, result_line], returncode=0)

        with patch(_EXEC_PATH, return_value=proc):
            events = await _collect_stream(cli)

        assert any(isinstance(e, SystemInitEvent) for e in events)
        assert any(isinstance(e, AssistantTextDelta) and e.text == "Hello world" for e in events)
        result_events = [e for e in events if isinstance(e, ResultEvent)]
        assert len(result_events) == 1
        assert result_events[0].session_id == "sess-1"

    async def test_timeout_yields_error_result(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        proc = _fake_streaming_process([])
        proc.stdout.readline = AsyncMock(side_effect=TimeoutError)
        proc.wait = AsyncMock()

        with patch(_EXEC_PATH, return_value=proc):
            events = await _collect_stream(cli, timeout_seconds=0.1)

        assert len(events) == 1
        assert isinstance(events[0], ResultEvent)
        assert events[0].is_error is True
        proc.wait.assert_awaited_once()

    async def test_nonzero_exit_yields_error_result(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        proc = _fake_streaming_process([], stderr=b"fatal error", returncode=1)

        with patch(_EXEC_PATH, return_value=proc):
            events = await _collect_stream(cli)

        assert len(events) == 1
        assert isinstance(events[0], ResultEvent)
        assert events[0].is_error is True
        assert "fatal error" in events[0].result

    async def test_empty_stream_no_events(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        proc = _fake_streaming_process([], returncode=0)

        with patch(_EXEC_PATH, return_value=proc):
            events = await _collect_stream(cli)

        assert events == []

    async def test_malformed_json_lines_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        good_line = (
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "OK"}]},
                }
            ).encode()
            + b"\n"
        )
        proc = _fake_streaming_process([b"not-json\n", b"  \n", good_line], returncode=0)

        with patch(_EXEC_PATH, return_value=proc):
            events = await _collect_stream(cli)

        assert len(events) == 1
        assert isinstance(events[0], AssistantTextDelta)
        assert events[0].text == "OK"

    async def test_process_registry_streaming(self, monkeypatch: pytest.MonkeyPatch) -> None:
        registry = ProcessRegistry()
        cli = _make_cli(monkeypatch, process_registry=registry, chat_id=99)
        proc = _fake_streaming_process([], returncode=0)

        with patch(_EXEC_PATH, return_value=proc):
            await _collect_stream(cli)

        assert not registry.has_active(99)

    async def test_process_registry_cleanup_on_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registry = ProcessRegistry()
        cli = _make_cli(monkeypatch, process_registry=registry, chat_id=99)
        proc = _fake_streaming_process([])
        proc.stdout.readline = AsyncMock(side_effect=TimeoutError)
        proc.wait = AsyncMock()

        with patch(_EXEC_PATH, return_value=proc):
            await _collect_stream(cli, timeout_seconds=0.1)

        assert not registry.has_active(99)

    async def test_missing_pipes_raises_runtime_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cli = _make_cli(monkeypatch)
        proc = MagicMock(spec=asyncio.subprocess.Process)
        proc.stdout = None
        proc.stderr = None
        proc.pid = 12345

        with (
            patch(_EXEC_PATH, return_value=proc),
            pytest.raises(RuntimeError, match="without stdout/stderr pipes"),
        ):
            await _collect_stream(cli)

    async def test_streaming_uses_stream_json_format(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        proc = _fake_streaming_process([], returncode=0)

        with patch(_EXEC_PATH, return_value=proc) as mock_exec:
            await _collect_stream(cli)

        called_cmd = mock_exec.call_args[0]
        assert "stream-json" in called_cmd
        assert "--verbose" in called_cmd

    async def test_stderr_truncated_at_500_chars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        long_stderr = b"X" * 1000
        proc = _fake_streaming_process([], stderr=long_stderr, returncode=1)

        with patch(_EXEC_PATH, return_value=proc):
            events = await _collect_stream(cli)

        result_events = [e for e in events if isinstance(e, ResultEvent)]
        assert len(result_events) == 1
        assert len(result_events[0].result) == 500

    async def test_streaming_limit_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify the 4MB buffer limit is passed to create_subprocess_exec."""
        cli = _make_cli(monkeypatch)
        proc = _fake_streaming_process([], returncode=0)

        with patch(_EXEC_PATH, return_value=proc) as mock_exec:
            await _collect_stream(cli)

        assert mock_exec.call_args[1]["limit"] == 4 * 1024 * 1024

    async def test_multiple_text_deltas(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        lines = [
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": f"chunk{i}"}]},
                }
            ).encode()
            + b"\n"
            for i in range(3)
        ]
        proc = _fake_streaming_process(lines, returncode=0)

        with patch(_EXEC_PATH, return_value=proc):
            events = await _collect_stream(cli)

        text_events = [e for e in events if isinstance(e, AssistantTextDelta)]
        assert len(text_events) == 3
        assert [e.text for e in text_events] == ["chunk0", "chunk1", "chunk2"]

    async def test_resume_session_in_streaming(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        proc = _fake_streaming_process([], returncode=0)

        with patch(_EXEC_PATH, return_value=proc) as mock_exec:
            await _collect_stream(cli, resume_session="sess-42")

        called_cmd = mock_exec.call_args[0]
        assert "--resume" in called_cmd
        assert "sess-42" in called_cmd

    async def test_no_registry_streaming_does_not_crash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cli = _make_cli(monkeypatch, process_registry=None)
        proc = _fake_streaming_process([], returncode=0)

        with patch(_EXEC_PATH, return_value=proc):
            events = await _collect_stream(cli)

        assert events == []

    async def test_zero_exit_with_empty_stderr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A zero exit code with no output should yield nothing (no error event)."""
        cli = _make_cli(monkeypatch)
        proc = _fake_streaming_process([], stderr=b"", returncode=0)

        with patch(_EXEC_PATH, return_value=proc):
            events = await _collect_stream(cli)

        assert not any(isinstance(e, ResultEvent) and e.is_error for e in events)


# ---------------------------------------------------------------------------
# add_cli_opt helper
# ---------------------------------------------------------------------------


class TestAddOpt:
    def test_adds_flag_when_value_present(self) -> None:
        cmd: list[str] = []
        add_cli_opt(cmd, "--model", "opus")
        assert cmd == ["--model", "opus"]

    def test_skips_when_value_none(self) -> None:
        cmd: list[str] = []
        add_cli_opt(cmd, "--model", None)
        assert cmd == []

    def test_skips_when_value_empty_string(self) -> None:
        cmd: list[str] = []
        add_cli_opt(cmd, "--model", "")
        assert cmd == []


# ---------------------------------------------------------------------------
# _log_cmd helper
# ---------------------------------------------------------------------------


class TestLogCmd:
    def test_truncates_long_values_after_flags(self, caplog: pytest.LogCaptureFixture) -> None:
        long_prompt = "x" * 200
        cmd = ["claude", "--system-prompt", long_prompt, "short"]
        with caplog.at_level(logging.INFO, logger="ductor_bot.cli.claude_provider"):
            _log_cmd(cmd)
        assert "..." in caplog.text

    def test_does_not_truncate_short_values(self, caplog: pytest.LogCaptureFixture) -> None:
        cmd = ["claude", "--model", "opus", "hello"]
        with caplog.at_level(logging.INFO, logger="ductor_bot.cli.claude_provider"):
            _log_cmd(cmd)
        assert "..." not in caplog.text

    def test_streaming_prefix(self, caplog: pytest.LogCaptureFixture) -> None:
        cmd = ["claude", "-p", "hello"]
        with caplog.at_level(logging.INFO, logger="ductor_bot.cli.claude_provider"):
            _log_cmd(cmd, streaming=True)
        assert "CLI stream cmd" in caplog.text

    def test_normal_prefix(self, caplog: pytest.LogCaptureFixture) -> None:
        cmd = ["claude", "-p", "hello"]
        with caplog.at_level(logging.INFO, logger="ductor_bot.cli.claude_provider"):
            _log_cmd(cmd, streaming=False)
        assert "CLI cmd" in caplog.text


# ---------------------------------------------------------------------------
# _parse_response (additional edge cases beyond test_parse_response.py)
# ---------------------------------------------------------------------------


class TestParseResponse:
    def test_stderr_truncated_at_2000_chars(self) -> None:
        long_stderr = b"E" * 5000
        data = {"result": "OK", "is_error": False}
        resp = _parse_response(json.dumps(data).encode(), long_stderr, 0)
        assert len(resp.stderr) == 2000

    def test_returncode_none(self) -> None:
        data = {"result": "OK"}
        resp = _parse_response(json.dumps(data).encode(), b"", None)
        assert resp.returncode is None

    def test_whitespace_only_stdout_is_error(self) -> None:
        resp = _parse_response(b"   \n\t  ", b"", 0)
        assert resp.is_error is True

    def test_done_log_has_session_turns_and_duration_but_no_figures(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        data = {
            "session_id": "sess-abc-123",
            "result": "OK",
            "num_turns": 2,
            "duration_ms": 1500.0,
            "total_cost_usd": 0.05,
            "usage": {"input_tokens": 500, "output_tokens": 200},
        }
        with caplog.at_level("INFO", logger="ductor_bot.cli.claude_provider"):
            _parse_response(json.dumps(data).encode(), b"", 0)
        assert "CLI done session=sess-abc turns=2 duration_ms=1500" in caplog.text
        assert "cost" not in caplog.text
        assert "tokens" not in caplog.text

    def test_stderr_in_response_object(self) -> None:
        data = {"result": "ok"}
        resp = _parse_response(json.dumps(data).encode(), b"warning text", 0)
        assert resp.stderr == "warning text"

    def test_error_result_has_correct_fields(self) -> None:
        data = {"result": "Something broke", "is_error": True}
        resp = _parse_response(json.dumps(data).encode(), b"", 1)
        assert resp.is_error is True
        assert resp.result == "Something broke"
        assert resp.returncode == 1

    def test_json_with_surrounding_whitespace(self) -> None:
        data = {"result": "trimmed", "is_error": False}
        raw = f"\n  {json.dumps(data)}  \n"
        resp = _parse_response(raw.encode(), b"", 0)
        assert resp.result == "trimmed"
        assert resp.is_error is False

    def test_duration_fields_populated(self) -> None:
        data = {
            "result": "ok",
            "duration_ms": 1234.5,
            "duration_api_ms": 900.0,
        }
        resp = _parse_response(json.dumps(data).encode(), b"", 0)
        assert resp.duration_ms == 1234.5
        assert resp.duration_api_ms == 900.0

    def test_num_turns_captured(self) -> None:
        data = {"result": "ok", "num_turns": 5}
        resp = _parse_response(json.dumps(data).encode(), b"", 0)
        assert resp.num_turns == 5

    def test_empty_stderr_bytes_yields_empty_string(self) -> None:
        data = {"result": "ok"}
        resp = _parse_response(json.dumps(data).encode(), b"", 0)
        assert resp.stderr == ""


# ---------------------------------------------------------------------------
# Multi-slot fallback (claude_accounts configured)
# ---------------------------------------------------------------------------


class TestMultiSlotFallback:
    """End-to-end coverage for the ``claude_accounts`` fallback path."""

    def _ductor_home(self, tmp_path: Path) -> Path:
        home = tmp_path / "ductor-home"
        (home / "workspace").mkdir(parents=True)
        return home

    def _make_multi_cli(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        *,
        accounts: list[str] | None = None,
        timezone: str = "UTC",
    ) -> ClaudeCodeCLI:
        home = self._ductor_home(tmp_path)
        return _make_cli(
            monkeypatch,
            working_dir=str(home / "workspace"),
            ductor_home=home,
            claude_accounts=accounts if accounts is not None else ["claude", "claude-alt"],
            user_timezone=timezone,
        )

    @staticmethod
    def _rate_limited_proc(reset_text: str = "resets 3pm") -> MagicMock:
        data = {"result": "", "is_error": True}
        return _fake_process(
            stdout=json.dumps(data).encode(),
            stderr=f"Error: Claude rate limit reached, {reset_text}".encode(),
            returncode=1,
        )

    @staticmethod
    def _success_proc(result: str = "Hi from alt slot") -> MagicMock:
        data = {"result": result, "is_error": False}
        return _fake_process(stdout=json.dumps(data).encode(), returncode=0)

    async def test_fallback_switches_to_alt_slot_and_prefixes_result(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        cli = self._make_multi_cli(monkeypatch, tmp_path)
        procs = [self._rate_limited_proc(), self._success_proc()]

        with patch(_EXEC_PATH, side_effect=procs) as mock_exec:
            resp = await cli.send("hello")

        assert mock_exec.call_count == 2
        assert resp.is_error is False
        assert resp.result.startswith("[Claude: claude → claude-alt switched")
        assert "15:00" in resp.result.splitlines()[0]
        assert "Hi from alt slot" in resp.result

    async def test_fallback_prefix_follows_the_language(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from ductor_bot.i18n import init

        cli = self._make_multi_cli(monkeypatch, tmp_path)
        procs = [self._rate_limited_proc(), self._success_proc()]
        init("ko")
        try:
            with patch(_EXEC_PATH, side_effect=procs):
                resp = await cli.send("hello")
        finally:
            init("en")

        assert resp.result.startswith("[Claude: claude → claude-alt 전환 (할당량 소진")
        assert "Hi from alt slot" in resp.result

    async def test_unparsable_reset_fallback_follows_the_language(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from ductor_bot.i18n import init

        cli = self._make_multi_cli(monkeypatch, tmp_path)
        proc = _fake_process(
            stdout=json.dumps({"result": "", "is_error": True}).encode(),
            stderr=b"Error: rate limit exceeded, try later",
            returncode=1,
        )
        init("ko")
        try:
            with patch(_EXEC_PATH, side_effect=[proc, self._success_proc()]):
                resp = await cli.send("hello")
        finally:
            init("en")

        assert resp.result.startswith("[Claude: claude → claude-alt 전환 (할당량 소진")
        assert "Hi from alt slot" in resp.result

    async def test_fallback_records_cooldown_for_first_slot(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        cli = self._make_multi_cli(monkeypatch, tmp_path)
        procs = [self._rate_limited_proc(), self._success_proc()]

        with patch(_EXEC_PATH, side_effect=procs):
            await cli.send("hello")

        assert cli._registry is not None
        snapshot = cli._registry.load()
        assert "claude" in snapshot.cooldowns
        assert snapshot.cooldowns["claude"] > 0
        assert snapshot.last_used == "claude"

    async def test_fallback_alt_subprocess_uses_config_dir_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        cli = self._make_multi_cli(monkeypatch, tmp_path)
        procs = [self._rate_limited_proc(), self._success_proc()]

        with patch(_EXEC_PATH, side_effect=procs) as mock_exec:
            await cli.send("hello")

        # First call: default slot "claude" → explicit default config directory.
        first_env = mock_exec.call_args_list[0][1].get("env") or {}
        assert first_env.get("CLAUDE_CONFIG_DIR", "").endswith(".cli-auth/claude")
        # Second call: alt slot → CLAUDE_CONFIG_DIR points at the alt dir.
        second_env = mock_exec.call_args_list[1][1].get("env") or {}
        assert second_env.get("CLAUDE_CONFIG_DIR", "").endswith(".cli-auth/claude-alt")

    async def test_unparsable_resets_cool_all_slots_without_success_prefix(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        cli = self._make_multi_cli(monkeypatch, tmp_path)
        # Error mentions "rate limit" so detect_rate_limit fires, but no
        # parseable reset timestamp → one-hour cooldown, then the next slot.
        proc = _fake_process(
            stdout=json.dumps({"result": "", "is_error": True}).encode(),
            stderr=b"Error: rate limit exceeded, try later",
            returncode=1,
        )

        with patch(_EXEC_PATH, return_value=proc) as mock_exec:
            resp = await cli.send("hello")

        assert mock_exec.call_count == 2
        assert resp.is_error is True
        assert resp.result == ""
        # Both slots retain cooldowns even without parseable reset times.
        assert cli._registry is not None
        assert set(cli._registry.load().cooldowns) == {"claude", "claude-alt"}

    async def test_no_fallback_when_only_slot_configured(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        cli = self._make_multi_cli(monkeypatch, tmp_path, accounts=["claude"])
        proc = self._rate_limited_proc()

        with patch(_EXEC_PATH, return_value=proc) as mock_exec:
            resp = await cli.send("hello")

        assert mock_exec.call_count == 1
        assert resp.is_error is True
        # No prefix added — nothing to switch to.
        assert not resp.result.startswith("[Claude:")

    async def test_all_cooled_down_returns_first_response_unprefixed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        cli = self._make_multi_cli(monkeypatch, tmp_path)
        # Pre-cool both slots far into the future so pick_slot returns None
        # and _record_cooldown also returns None → no prefix, no retry.
        assert cli._registry is not None
        future = 9_999_999_999.0
        cli._registry.save(SlotSnapshot(cooldowns={"claude": future, "claude-alt": future}))
        proc = self._rate_limited_proc()

        with patch(_EXEC_PATH, return_value=proc) as mock_exec:
            resp = await cli.send("hello")

        assert mock_exec.call_count == 1
        assert resp.is_error is True
        assert not resp.result.startswith("[Claude: claude →")

    async def test_disabled_when_accounts_empty(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        cli = self._make_multi_cli(monkeypatch, tmp_path, accounts=[])
        data = {"result": "ok", "is_error": False}
        proc = _fake_process(stdout=json.dumps(data).encode(), returncode=0)

        with patch(_EXEC_PATH, return_value=proc) as mock_exec:
            resp = await cli.send("hello")

        assert mock_exec.call_count == 1
        assert resp.result == "ok"
        assert cli._registry is None

    async def test_happy_path_no_fallback_needed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        cli = self._make_multi_cli(monkeypatch, tmp_path)
        proc = self._success_proc("fast path")

        with patch(_EXEC_PATH, return_value=proc) as mock_exec:
            resp = await cli.send("hello")

        assert mock_exec.call_count == 1
        assert resp.result == "fast path"
        # No cooldown written on success.
        assert cli._registry is not None
        assert not cli._registry.state_path.exists()

    async def test_all_slots_rate_limited_no_misleading_prefix(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Second/third slot also 429 → no success prefix, raw 429 surfaced."""
        cli = self._make_multi_cli(
            monkeypatch,
            tmp_path,
            accounts=["claude", "claude-alt", "claude-third"],
        )
        # All three slots return a rate-limit error with a parseable reset.
        procs = [
            self._rate_limited_proc("resets 3pm"),
            self._rate_limited_proc("resets 4pm"),
            self._rate_limited_proc("resets 5pm"),
        ]

        with patch(_EXEC_PATH, side_effect=procs) as mock_exec:
            resp = await cli.send("hello")

        # Every hot slot was attempted.
        assert mock_exec.call_count == 3
        # Response is still rate-limited (no success); must NOT carry a
        # fallback prefix that would mislead the user.
        assert resp.is_error is True
        assert not resp.result.startswith("[Claude:")
        # Cooldown recorded for every slot whose reset timestamp parsed.
        assert cli._registry is not None
        snap = cli._registry.load()
        assert set(snap.cooldowns.keys()) == {"claude", "claude-alt", "claude-third"}

    async def test_third_slot_succeeds_after_two_429s(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Fallback chains through multiple rate-limited slots until success."""
        cli = self._make_multi_cli(
            monkeypatch,
            tmp_path,
            accounts=["claude", "claude-alt", "claude-third"],
        )
        procs = [
            self._rate_limited_proc("resets 3pm"),
            self._rate_limited_proc("resets 4pm"),
            self._success_proc("from third"),
        ]

        with patch(_EXEC_PATH, side_effect=procs) as mock_exec:
            resp = await cli.send("hello")

        assert mock_exec.call_count == 3
        assert resp.is_error is False
        # Prefix reflects the FIRST rate-limited slot (claude) and the
        # slot that finally succeeded (claude-third).
        assert resp.result.startswith("[Claude: claude → claude-third switched")
        assert "from third" in resp.result
        # First two slots recorded cooldowns.
        assert cli._registry is not None
        snap = cli._registry.load()
        assert "claude" in snap.cooldowns
        assert "claude-alt" in snap.cooldowns
        assert "claude-third" not in snap.cooldowns

    async def test_second_slot_parse_failure_records_fallback_cooldown(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A later unparseable reset still records its fallback cooldown."""
        cli = self._make_multi_cli(monkeypatch, tmp_path)
        procs = [
            # First slot: parseable reset → cooldown recorded.
            self._rate_limited_proc("resets 3pm"),
            # Second slot: rate-limit keyword but no parseable timestamp.
            _fake_process(
                stdout=json.dumps({"result": "", "is_error": True}).encode(),
                stderr=b"Error: rate limit, try again later",
                returncode=1,
            ),
        ]

        with patch(_EXEC_PATH, side_effect=procs) as mock_exec:
            resp = await cli.send("hello")

        assert mock_exec.call_count == 2
        assert resp.is_error is True
        assert resp.result == ""
        # Both parsed and fallback reset times are recorded.
        assert cli._registry is not None
        snap = cli._registry.load()
        assert "claude" in snap.cooldowns
        assert "claude-alt" in snap.cooldowns


# ---------------------------------------------------------------------------
# System and user prompts never appear in argv (visible in ps)
# ---------------------------------------------------------------------------

_SYSTEM = "SYSTEM-PROMPT-MARKER persona"
_APPEND = "APPEND-PROMPT-MARKER " * 3000  # ~63 KB, well under the old 96 KiB cutoff
_USER = "USER-PROMPT-MARKER hello"
_RESULT = json.dumps({"type": "result", "result": "ok", "is_error": False}).encode() + b"\n"


class _ArgvRecorder:
    """Fake subprocess factory that snapshots argv and prompt files at spawn time."""

    def __init__(self, procs: list[MagicMock]) -> None:
        self._procs = list(procs)
        self.spawns: list[dict[str, Any]] = []

    def __call__(self, *argv: str, **_kwargs: Any) -> MagicMock:
        spawn: dict[str, Any] = {"argv": list(argv), "files": {}}
        for flag in ("--system-prompt-file", "--append-system-prompt-file"):
            if flag in argv:
                path = Path(argv[argv.index(flag) + 1])
                spawn["files"][flag] = (path, path.read_text(encoding="utf-8"), path.stat().st_mode)
        self.spawns.append(spawn)
        return self._procs.pop(0)


def _assert_prompts_hidden(recorder: _ArgvRecorder, count: int) -> None:
    assert len(recorder.spawns) == count
    paths: set[Path] = set()
    for spawn in recorder.spawns:
        argv = spawn["argv"]
        assert not any(m in arg for arg in argv for m in ("MARKER",)), argv
        assert "--" not in argv
        assert "--system-prompt" not in argv
        assert "--append-system-prompt" not in argv
        system_path, system_text, system_mode = spawn["files"]["--system-prompt-file"]
        append_path, append_text, append_mode = spawn["files"]["--append-system-prompt-file"]
        assert system_text == _SYSTEM
        assert append_text == _APPEND
        assert system_mode & 0o777 == 0o600
        assert append_mode & 0o777 == 0o600
        paths.update({system_path, append_path})
    assert len(paths) == 2 * count  # fresh private files per attempt
    assert not any(path.exists() for path in paths)  # removed after the run


class TestPromptsStayOutOfArgv:
    def _cli(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **kwargs: Any) -> ClaudeCodeCLI:
        home = tmp_path / "ductor-home"
        (home / "workspace").mkdir(parents=True)
        return _make_cli(
            monkeypatch,
            working_dir=str(home / "workspace"),
            ductor_home=home,
            system_prompt=_SYSTEM,
            append_system_prompt=_APPEND,
            **kwargs,
        )

    @staticmethod
    def _ok_proc() -> MagicMock:
        return _fake_process(stdout=json.dumps({"result": "ok", "is_error": False}).encode())

    async def test_send_uses_files_and_stdin(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        cli = self._cli(monkeypatch, tmp_path)
        proc = self._ok_proc()
        recorder = _ArgvRecorder([proc])
        with patch(_EXEC_PATH, side_effect=recorder):
            resp = await cli.send(_USER, resume_session="sess-9")
        assert resp.result == "ok"
        _assert_prompts_hidden(recorder, 1)
        assert recorder.spawns[0]["argv"][-2:] == ["--resume", "sess-9"]
        proc.communicate.assert_awaited_once_with(input=_USER.encode())

    async def test_streaming_uses_files_and_stdin(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        cli = self._cli(monkeypatch, tmp_path)
        proc = _fake_streaming_process([_RESULT])
        recorder = _ArgvRecorder([proc])
        with patch(_EXEC_PATH, side_effect=recorder):
            events = await _collect_stream(cli, _USER)
        assert any(isinstance(event, ResultEvent) for event in events)
        _assert_prompts_hidden(recorder, 1)
        proc.stdin.write.assert_called_once_with(_USER.encode())
        proc.stdin.close.assert_called_once()

    async def test_slot_fallback_send_never_leaks_prompts(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        cli = self._cli(monkeypatch, tmp_path, claude_accounts=["claude", "claude-alt"])
        limited = _fake_process(
            stdout=json.dumps({"result": "", "is_error": True}).encode(),
            stderr=b"Error: Claude rate limit reached, resets 3pm",
            returncode=1,
        )
        procs = [limited, self._ok_proc()]
        recorder = _ArgvRecorder(procs)
        with patch(_EXEC_PATH, side_effect=recorder):
            resp = await cli.send(_USER)
        assert "ok" in resp.result
        _assert_prompts_hidden(recorder, 2)
        for proc in procs:
            proc.communicate.assert_awaited_once_with(input=_USER.encode())

    async def test_slot_streaming_never_leaks_prompts(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        cli = self._cli(monkeypatch, tmp_path, claude_accounts=["claude", "claude-alt"])
        proc = _fake_streaming_process([_RESULT])
        recorder = _ArgvRecorder([proc])
        with patch(_EXEC_PATH, side_effect=recorder):
            events = await _collect_stream(cli, _USER)
        assert any(isinstance(event, ResultEvent) for event in events)
        _assert_prompts_hidden(recorder, 1)
        proc.stdin.write.assert_called_once_with(_USER.encode())

    async def test_files_removed_when_spawn_fails(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        cli = self._cli(monkeypatch, tmp_path)
        seen: list[Path] = []

        def _boom(*argv: str, **_kwargs: Any) -> MagicMock:
            seen.extend(Path(argv[argv.index(f) + 1]) for f in ("--system-prompt-file",))
            raise OSError("spawn failed")

        with patch(_EXEC_PATH, side_effect=_boom), pytest.raises(OSError, match="spawn failed"):
            await cli.send(_USER)
        assert seen
        assert not any(path.exists() for path in seen)

    def test_unset_prompts_add_no_file_flags(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cli = _make_cli(monkeypatch)
        files = cli._write_prompt_files()
        assert files.system is None
        assert files.append is None
        cmd = cli._build_command(prompt_files=files)
        assert "--system-prompt-file" not in cmd
        assert "--append-system-prompt-file" not in cmd


_FAKE_CLAUDE = """#!/usr/bin/env python3
import json, os, stat, sys
argv = sys.argv[1:]
def file_arg(flag):
    return argv[argv.index(flag) + 1] if flag in argv else None
report = {"cmdline": open("/proc/self/cmdline", "rb").read().decode().split("\\0")}
for flag in ("--system-prompt-file", "--append-system-prompt-file"):
    path = file_arg(flag)
    report[flag] = path and [open(path).read(), oct(stat.S_IMODE(os.stat(path).st_mode)), path]
report["stdin"] = sys.stdin.read()
print(json.dumps({"result": json.dumps(report), "is_error": False}))
"""


def _gone(path: str) -> bool:
    return not Path(path).exists()


@pytest.mark.skipif(not Path("/proc/self/cmdline").exists(), reason="needs procfs")
class TestFakeClaudeProcess:
    """Run a real subprocess standing in for the claude binary and inspect what it saw."""

    async def test_prompts_reach_cli_only_via_files_and_stdin(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        fake = tmp_path / "claude"
        fake.write_text(_FAKE_CLAUDE, encoding="utf-8")
        fake.chmod(0o700)
        monkeypatch.setattr("ductor_bot.cli.claude_provider.which", lambda _: str(fake))
        cli = ClaudeCodeCLI(
            CLIConfig(
                provider="claude",
                model="opus",
                working_dir=str(tmp_path),
                system_prompt=_SYSTEM,
                append_system_prompt=_APPEND,
            )
        )
        resp = await cli.send(_USER)
        assert resp.is_error is False
        seen = json.loads(resp.result)
        assert not any("MARKER" in arg for arg in seen["cmdline"])
        assert seen["--system-prompt-file"][:2] == [_SYSTEM, "0o600"]
        assert seen["--append-system-prompt-file"][:2] == [_APPEND, "0o600"]
        assert seen["stdin"] == _USER
        assert _gone(seen["--system-prompt-file"][2])
        assert _gone(seen["--append-system-prompt-file"][2])

    async def test_streaming_prompts_reach_cli_only_via_files_and_stdin(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        fake = tmp_path / "claude"
        fake.write_text(
            _FAKE_CLAUDE.replace(
                'print(json.dumps({"result": json.dumps(report), "is_error": False}))',
                'print(json.dumps({"type": "result", "result": json.dumps(report), "is_error": False}))',
            ),
            encoding="utf-8",
        )
        fake.chmod(0o700)
        monkeypatch.setattr("ductor_bot.cli.claude_provider.which", lambda _: str(fake))
        cli = ClaudeCodeCLI(
            CLIConfig(
                provider="claude",
                working_dir=str(tmp_path),
                system_prompt=_SYSTEM,
                append_system_prompt=_APPEND,
            )
        )
        events = await _collect_stream(cli, _USER)
        result = next(event for event in events if isinstance(event, ResultEvent))
        seen = json.loads(result.result)
        assert not any("MARKER" in arg for arg in seen["cmdline"])
        assert seen["--system-prompt-file"][:2] == [_SYSTEM, "0o600"]
        assert seen["stdin"] == _USER
        assert _gone(seen["--append-system-prompt-file"][2])


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize("home_suffix", ["", "agents/dev", "agents/workspace"])
@pytest.mark.parametrize("request_directory", ["workspace", "projects/alpha"])
async def test_auth_slots_use_bot_home_independent_of_request_directory(
    monkeypatch, tmp_path, *, streaming, fallback, home_suffix, request_directory
):
    home = tmp_path / "volume"
    agent_home = home / home_suffix
    working_dir = agent_home / "workspace" if request_directory == "workspace" else home / request_directory
    working_dir.mkdir(parents=True)
    agent_home.mkdir(parents=True, exist_ok=True)
    slots = ["claude-alt", "claude"] if fallback else ["claude"]
    for slot in slots:
        directory = home / ".cli-auth" / slot
        directory.mkdir(parents=True)
        (directory / ".credentials.json").write_text("{}")
    cli = _make_cli(
        monkeypatch, ductor_home=str(agent_home), working_dir=str(working_dir),
        claude_accounts=slots, user_timezone="UTC",
    )
    success = {"type": "result", "result": "ok", "is_error": False}
    limited = {"type": "result", "result": "", "is_error": True}
    stderr = b"Error: Claude rate limit reached, resets 3pm"
    if streaming:
        processes = [_fake_streaming_process([json.dumps(success).encode() + b"\n"])]
        if fallback:
            processes.insert(0, _fake_streaming_process(
                [json.dumps(limited).encode() + b"\n"], stderr=stderr, returncode=1,
            ))
    else:
        processes = [_fake_process(stdout=json.dumps(success).encode())]
        if fallback:
            processes.insert(0, _fake_process(
                stdout=json.dumps(limited).encode(), stderr=stderr, returncode=1,
            ))
    with patch(_EXEC_PATH, side_effect=processes) as execute:
        if streaming:
            events = await _collect_stream(cli)
            assert any(isinstance(event, ResultEvent) for event in events)
        else:
            assert not (await cli.send("hello")).is_error
    selected = [call.kwargs["env"]["CLAUDE_CONFIG_DIR"] for call in execute.call_args_list]
    assert selected == [str(home / ".cli-auth" / slot) for slot in slots]
    assert not any(Path(path).is_relative_to(working_dir / ".cli-auth") for path in selected)
    assert [call.kwargs["cwd"] for call in execute.call_args_list] == [str(working_dir)] * len(slots)


@pytest.mark.parametrize("configured", ["", "invalid/timezone"])
def test_default_timezone_uses_host_setting(monkeypatch: pytest.MonkeyPatch, configured: str) -> None:
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    cli = _make_cli(monkeypatch, user_timezone=configured)
    assert cli._default_timezone().key == "Asia/Tokyo"



def _install_slot_cli(tmp_path, monkeypatch):
    """Install a subprocess fake that records authentication paths and slot attempts."""
    home = tmp_path / "home"
    for slot in ("claude-a", "claude-b"):
        auth = home / ".cli-auth" / slot
        auth.mkdir(parents=True)
        (auth / ".credentials.json").write_text("{}")
    control = tmp_path / "control.json"
    control.write_text(json.dumps({"limited": ["claude-a"]}))
    calls = tmp_path / "calls.jsonl"
    fake = tmp_path / "fake-claude"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys\n"
        f"control = json.loads(pathlib.Path({str(control)!r}).read_text())\n"
        "auth = pathlib.Path(os.environ['CLAUDE_CONFIG_DIR'])\n"
        f"with open({str(calls)!r}, 'a') as log:\n"
        "    log.write(json.dumps({'auth': str(auth), 'cwd': os.getcwd()}) + '\\n')\n"
        "sys.stdin.read()\n"
        "error = not (auth / '.credentials.json').exists() or auth.name in control['limited']\n"
        "text = ('Not logged in' if not (auth / '.credentials.json').exists() else "
        "control.get('error', 'Usage limit; resets at 18:30')) if error else "
        "control.get('result', 'ok')\n"
        "if error: print(text, file=sys.stderr)\n"
        "print(json.dumps({'type': 'result', 'is_error': error, 'result': text}))\n"
        "sys.exit(1 if error else 0)\n"
    )
    fake.chmod(0o700)
    monkeypatch.setattr("ductor_bot.cli.claude_provider.which", lambda _: str(fake))
    monkeypatch.setattr("ductor_bot.cron.execution.which", lambda _: str(fake))
    monkeypatch.setenv("TZ", "UTC")
    return home, control, calls


def _read_slot_calls(calls):
    return [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []


@pytest.mark.parametrize("streaming", [False, True])
async def test_shared_slot_registry_across_working_directories(tmp_path, monkeypatch, streaming):
    import time

    from ductor_bot.cli.claude_accounts import SlotRegistry

    home, control, calls = _install_slot_cli(tmp_path, monkeypatch)
    cwds = [home / "workspace", tmp_path / "project-one", tmp_path / "project-two"]
    for cwd in cwds:
        cwd.mkdir(parents=True)
    registry = SlotRegistry(home / ".cli-auth" / "_claude-slots.json")

    async def send(cwd):
        cli = ClaudeCodeCLI(
            CLIConfig(
                provider="claude",
                model="opus",
                ductor_home=home,
                working_dir=str(cwd),
                claude_accounts=["claude-a", "claude-b"],
                user_timezone="UTC",
            )
        )
        if streaming:
            events = [event async for event in cli.send_streaming("hello")]
            return next(event.result for event in events if isinstance(event, ResultEvent))
        return (await cli.send("hello")).result

    first = await send(cwds[0])
    assert first.startswith("[Claude: claude-a → claude-b ")
    assert first.endswith("\n\nok")
    assert [Path(row["auth"]).name for row in _read_slot_calls(calls)] == ["claude-a", "claude-b"]
    for cwd in cwds:
        calls.write_text("")
        assert await send(cwd) == "ok"
        assert [row["auth"] for row in _read_slot_calls(calls)] == [
            str(home / ".cli-auth" / "claude-b")
        ]
    for expiry in (time.time() + 3600, time.time() - 1):
        registry.save(SlotSnapshot(cooldowns=dict.fromkeys(("claude-a", "claude-b"), expiry)))
        control.write_text(json.dumps({"limited": []}))
        for cwd in cwds:
            calls.write_text("")
            assert await send(cwd) == "ok"
            assert [row["auth"] for row in _read_slot_calls(calls)] == [
                str(home / ".cli-auth" / "claude-a")
            ]
    assert list(tmp_path.rglob("_claude-slots.json")) == [home / ".cli-auth" / "_claude-slots.json"]


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize(
    "error", ["Connection reset by peer", "fatal: git reset failed", "429 Too Many Requests"]
)
async def test_reset_transport_errors_keep_existing_rotation_policy(
    tmp_path, monkeypatch, streaming, error
):
    home, control, calls = _install_slot_cli(tmp_path, monkeypatch)
    control.write_text(json.dumps({"limited": ["claude-a"], "error": error}))
    cli = ClaudeCodeCLI(
        CLIConfig(
            provider="claude",
            model="opus",
            ductor_home=home,
            working_dir=str(tmp_path),
            claude_accounts=["claude-a", "claude-b"],
        )
    )
    if streaming:
        events = [event async for event in cli.send_streaming("hello")]
        result = next(event.result for event in events if isinstance(event, ResultEvent))
    else:
        result = (await cli.send("hello")).result
    uses_fallback = error == "429 Too Many Requests"
    expected_slots = ["claude-a", "claude-b"] if uses_fallback else ["claude-a"]
    assert [Path(row["auth"]).name for row in _read_slot_calls(calls)] == expected_slots
    assert "cooldown time could not be parsed" not in result
    assert bool(list(home.rglob("_claude-slots.json"))) is uses_fallback


def _freeze_slot_clock(monkeypatch):
    from datetime import UTC, datetime

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2099, 10, 16, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr("ductor_bot.cli.slot_rotation.datetime", FixedDateTime)



@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("suffix", ["", "agents/dev", "agents/workspace"])
async def test_empty_accounts_pin_default_slot(tmp_path, monkeypatch, streaming, suffix):
    home, control, calls = _install_slot_cli(tmp_path, monkeypatch)
    auth = home / ".cli-auth" / "claude"
    auth.mkdir()
    (auth / ".credentials.json").write_text("{}")
    control.write_text(json.dumps({"limited": []}))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "wrong-slot"))
    cli = ClaudeCodeCLI(
        CLIConfig(
            provider="claude",
            model="opus",
            ductor_home=home / suffix,
            working_dir=str(tmp_path),
            claude_accounts=[],
        )
    )
    if streaming:
        events = [event async for event in cli.send_streaming("hello")]
        result = next(event.result for event in events if isinstance(event, ResultEvent))
    else:
        result = (await cli.send("hello")).result
    assert result == "ok"
    assert [row["auth"] for row in _read_slot_calls(calls)] == [str(auth)]
    assert not list(home.rglob("_claude-slots.json"))


@pytest.mark.parametrize("all_cooling", [False, True])
async def test_streaming_missing_model_reset_cools_for_one_hour(tmp_path, monkeypatch, all_cooling):
    import time

    home, control, calls = _install_slot_cli(tmp_path, monkeypatch)
    message = "You've reached your Fable 5 limit. Run /usage-credits to continue or switch models with /model."
    control.write_text(json.dumps({"limited": ["claude-a"], "error": message}))
    cli = ClaudeCodeCLI(
        CLIConfig(
            provider="claude",
            model="opus",
            ductor_home=home,
            working_dir=str(tmp_path),
            claude_accounts=["claude-a", "claude-b"],
            user_timezone="UTC",
        )
    )
    assert cli._registry is not None
    now = time.time()
    future = now + 7200
    if all_cooling:
        cli._registry.save(SlotSnapshot(cooldowns={"claude-a": future, "claude-b": future}))
    events = [event async for event in cli.send_streaming("hello")]
    results = [event for event in events if isinstance(event, ResultEvent)]
    assert len(results) == 1
    snapshot = cli._registry.load()
    assert snapshot.cooldowns["claude-a"] == pytest.approx(now + 3600, abs=5)
    assert snapshot.last_used == "claude-a"
    expected_calls = ["claude-a"] if all_cooling else ["claude-a", "claude-b"]
    assert [Path(row["auth"]).name for row in _read_slot_calls(calls)] == expected_calls
    if all_cooling:
        assert results[0].is_error is True
        assert results[0].result == message
        assert snapshot.cooldowns["claude-b"] == future
        assert "[Claude:" not in results[0].result
    else:
        assert results[0].is_error is False
        assert results[0].result.endswith("ok")
        assert results[0].result.count("[Claude:") == 1
        assert "claude-a → claude-b" in results[0].result
        assert "claude-b" not in snapshot.cooldowns


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("estimated", [False, True])
async def test_rotation_notice_distinguishes_estimated_reset(tmp_path, monkeypatch, streaming, estimated):
    from ductor_bot.cli.claude_provider import ClaudeCodeCLI
    from ductor_bot.i18n import init, t

    init("en")
    home, control, calls = _install_slot_cli(tmp_path, monkeypatch)
    error = "Usage limit" if estimated else "Usage limit; resets at 18:30"
    control.write_text(json.dumps({"limited": ["claude-a"], "error": error}))
    cli = ClaudeCodeCLI(CLIConfig(provider="claude", model="opus", ductor_home=home,
                        working_dir=str(tmp_path),
                        claude_accounts=["claude-a", "claude-b"], user_timezone="UTC"))
    if streaming:
        events = [event async for event in cli.send_streaming("hello")]
        text = next(event.result for event in reversed(events) if isinstance(event, ResultEvent))
    else:
        text = (await cli.send("hello")).result
    assert text.startswith("[Claude: claude-a → claude-b ")
    assert (t("slot.switched_estimated") in text) is estimated
    assert ("18:30" in text) is (not estimated)
    assert [row["auth"] for row in _read_slot_calls(calls)] == [
        str(home / ".cli-auth" / slot) for slot in ["claude-a", "claude-b"]]


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("accounts", [[], ["claude-a", "claude-b"]])
async def test_usage_events_capture_executing_slot_without_changing_result(tmp_path, monkeypatch, streaming, accounts):
    from ductor_bot.cli.claude_usage import UsageStore, usage_file_path
    from tests.cli.test_claude_usage import ALLOWED

    cli = _make_cli(monkeypatch, ductor_home=tmp_path, working_dir=str(tmp_path),
                    claude_accounts=accounts)
    final = {"type": "result", "result": "same result", "is_error": False, "session_id": "synthetic-session"}
    if streaming:
        proc = _fake_streaming_process([json.dumps(ALLOWED).encode() + b"\n",
                                       json.dumps(final).encode() + b"\n"])
    else:
        proc = _fake_process(stdout=json.dumps([{"type": "system"}, ALLOWED, final]).encode())
    with patch(_EXEC_PATH, return_value=proc):
        if streaming:
            events = await _collect_stream(cli)
            assert next(event.result for event in events if isinstance(event, ResultEvent)) == "same result"
        else:
            response = await cli.send("hello")
            assert response == _parse_response(json.dumps(final).encode(), b"", 0)
    slots = UsageStore(usage_file_path(tmp_path)).load()
    assert list(slots) == ([accounts[0]] if accounts else ["claude"])
    assert slots[next(iter(slots))].windows["five_hour"].utilization == 0.11
    assert not (tmp_path / ".cli-auth/_claude-slots.json").exists()


@pytest.mark.parametrize("streaming", [False, True])
async def test_usage_write_failure_does_not_change_execution_result(tmp_path, monkeypatch, streaming, caplog):
    from ductor_bot.cli.claude_usage import UsageStore
    from tests.cli.test_claude_usage import ALLOWED

    cli = _make_cli(monkeypatch, ductor_home=tmp_path, working_dir=str(tmp_path))
    monkeypatch.setattr(UsageStore, "record", MagicMock(side_effect=OSError("synthetic-private-value")))
    final = {"type": "result", "result": "same result", "is_error": False}
    proc = (_fake_streaming_process([json.dumps(ALLOWED).encode() + b"\n", json.dumps(final).encode() + b"\n"])
            if streaming else _fake_process(stdout=json.dumps([ALLOWED, final]).encode()))
    with patch(_EXEC_PATH, return_value=proc):
        if streaming:
            events = await _collect_stream(cli)
            assert next(event.result for event in events if isinstance(event, ResultEvent)) == "same result"
        else:
            response = await cli.send("hello")
            assert response.result == "same result"
            assert not response.is_error
    assert "usage" in caplog.text.lower()
    assert "synthetic-private-value" not in caplog.text


def test_non_streaming_command_requests_verbose_events(monkeypatch):
    assert _make_cli(monkeypatch)._build_command().count("--verbose") == 1


@pytest.mark.parametrize("streaming", [False, True])
async def test_rotated_usage_events_stay_with_each_attempted_slot(tmp_path, monkeypatch, streaming):
    from ductor_bot.cli.claude_usage import UsageStore, usage_file_path
    from tests.cli.test_claude_usage import ALLOWED, REJECTED

    cli = _make_cli(monkeypatch, ductor_home=tmp_path, working_dir=str(tmp_path),
                    claude_accounts=["claude-a", "claude-b"], user_timezone="UTC")
    limited = {"type": "result", "result": "Usage limit; resets at 18:30", "is_error": True}
    final = {"type": "result", "result": "same result", "is_error": False}
    if streaming:
        procs = [_fake_streaming_process([json.dumps(event).encode() + b"\n" for event in pair],
                                         returncode=code)
                 for pair, code in [([REJECTED, limited], 1), ([ALLOWED, final], 0)]]
    else:
        procs = [_fake_process(stdout=json.dumps(pair).encode(), returncode=code)
                 for pair, code in [([REJECTED, limited], 1), ([ALLOWED, final], 0)]]
    with patch(_EXEC_PATH, side_effect=procs):
        if streaming:
            events = await _collect_stream(cli)
            result = next(event.result for event in events if isinstance(event, ResultEvent))
        else:
            result = (await cli.send("hello")).result
    assert result.endswith("same result")
    slots = UsageStore(usage_file_path(tmp_path)).load()
    assert slots["claude-a"].status == "rejected"
    assert slots["claude-a"].windows["seven_day"].utilization == 0.36
    assert slots["claude-b"].status == "allowed"
    assert slots["claude-b"].windows["seven_day"].utilization == 0.02


@pytest.mark.parametrize("streaming", [False, True])
async def test_slow_usage_write_failure_does_not_consume_cli_timeout(tmp_path, monkeypatch, streaming):
    import time
    from ductor_bot.cli.claude_usage import UsageStore
    from tests.cli.test_claude_usage import ALLOWED

    def fail_write(*args, **kwargs):
        time.sleep(0.6)
        raise OSError("synthetic write failure")

    cli = _make_cli(monkeypatch, ductor_home=tmp_path, working_dir=str(tmp_path))
    monkeypatch.setattr(UsageStore, "record", fail_write)
    final = {"type": "result", "result": "same result", "is_error": False}
    proc = (_fake_streaming_process([json.dumps(ALLOWED).encode() + b"\n", json.dumps(final).encode() + b"\n"])
            if streaming else _fake_process(stdout=json.dumps([ALLOWED, final]).encode()))
    with patch(_EXEC_PATH, return_value=proc):
        if streaming:
            events = await _collect_stream(cli, timeout_seconds=0.5)
            result = next(event for event in events if isinstance(event, ResultEvent))
            assert result.result == "same result"
            assert not result.is_error
        else:
            result = await cli.send("hello", timeout_seconds=0.5)
            assert result.result == "same result"
            assert not result.is_error


async def test_streaming_usage_keeps_event_arrival_time_when_saved_after_result(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from ductor_bot.cli import claude_provider
    from ductor_bot.cli.claude_usage import UsageStore, usage_file_path
    from tests.cli.test_claude_usage import ALLOWED, CAPTURED

    clock = SimpleNamespace(now=CAPTURED)
    monkeypatch.setattr(claude_provider, "time", SimpleNamespace(time=lambda: clock.now))
    cli = _make_cli(monkeypatch, ductor_home=tmp_path, working_dir=str(tmp_path))
    final = {"type": "result", "result": "same result", "is_error": False}
    proc = _fake_streaming_process([])
    lines = iter([json.dumps(ALLOWED).encode() + b"\n", json.dumps(final).encode() + b"\n", b""])

    async def readline():
        line = next(lines)
        if b'"type": "result"' in line:
            clock.now += 100
        return line

    proc.stdout.readline = readline
    with patch(_EXEC_PATH, return_value=proc):
        await _collect_stream(cli)
    usage = UsageStore(usage_file_path(tmp_path)).load()["claude"]
    assert clock.now == CAPTURED + 100
    assert usage.captured_at == CAPTURED
    assert all(window.captured_at == CAPTURED for window in usage.windows.values())


async def test_usage_line_handler_skips_extraction_for_other_events(monkeypatch):
    cli = ClaudeCodeCLI(CLIConfig())
    extract = MagicMock(return_value=[])
    monkeypatch.setattr("ductor_bot.cli.claude_provider.extract_usage_events", extract)
    usage_events = []
    events = [event async for event in cli._usage_line_handler(
        '{"type":"system","subtype":"init","session_id":"synthetic"}',
        usage_events=usage_events,
    )]
    extract.assert_not_called()
    assert usage_events == []
    assert events



@pytest.mark.parametrize("driver", ["provider", "default-task", "named-task"])
async def test_buffered_usage_uses_start_before_intermediate_stream(tmp_path, monkeypatch, driver):
    from types import SimpleNamespace

    from ductor_bot.cli import claude_provider as provider, claude_usage
    from ductor_bot.cli.claude_usage import UsageStore, usage_file_path
    from ductor_bot.cli.param_resolver import resolve_cli_config
    from ductor_bot.config import AgentConfig
    from ductor_bot.cron.execution import OneShotExecutionResult
    from ductor_bot.infra import task_runner
    from ductor_bot.infra.task_runner import TaskRunOptions, run_oneshot_task

    clock = [1000]
    timing = SimpleNamespace(time=lambda: clock[0])
    monkeypatch.setattr(provider, "time", timing)
    monkeypatch.setattr(claude_usage, "time", timing)
    # The task runner may use its own clock once start-time capture is wired.
    monkeypatch.setattr(task_runner, "time", timing, raising=False)
    monkeypatch.setattr(provider.ClaudeCodeCLI, "_find_cli", lambda _: "fake-unused")
    monkeypatch.setattr("ductor_bot.cron.execution.which", lambda _: "fake-unused")
    slot = "claude-a" if driver == "named-task" else "claude"
    cli = provider.ClaudeCodeCLI(CLIConfig(ductor_home=str(tmp_path), working_dir=str(tmp_path)))
    store = UsageStore(usage_file_path(tmp_path / "workspace"))

    def event(value):
        return {"type": "rate_limit_event", "rate_limit_info": {
            "status": "allowed", "rateLimitType": "five_hour", "utilization": value,
            "resetsAt": 5000}}

    async def intermediate_stream():
        clock[0] = 1100
        events = []
        assert [item async for item in cli._usage_line_handler(
            json.dumps(event(0.3)), usage_events=events)] == []
        await cli._save_usage_events(events, slot)
        assert store.load()[slot].captured_at == 1100
        clock[0] = 1200

    final = {"type": "result", "result": "unchanged", "is_error": False}
    stdout = json.dumps([event(0.5), final]).encode()
    async def fake_provider(*, parse_output, **kwargs):
        await intermediate_stream()
        return parse_output(stdout, b"", 0)
    async def fake_task(*args, **kwargs):
        await intermediate_stream()
        return OneShotExecutionResult("success", "unchanged", stdout, b"", 0, False)
    monkeypatch.setattr(provider, "run_oneshot_subprocess", fake_provider)
    monkeypatch.setattr("ductor_bot.cron.execution.execute_one_shot", fake_task)
    if driver == "provider":
        result = await cli.send("hello")
        assert result.result == "unchanged"
    else:
        config = AgentConfig(ductor_home=str(tmp_path), provider="claude", model="opus",
                             claude_accounts=[slot] if driver == "named-task" else [])
        result = await run_oneshot_task(resolve_cli_config(config, None), "hello",
            TaskRunOptions(cwd=tmp_path, timeout_seconds=5, timeout_label="test"))
        assert result.result_text == "unchanged"
    usage = store.load()[slot]
    assert (usage.captured_at, usage.windows["five_hour"].captured_at,
            usage.windows["five_hour"].utilization) == (1100, 1100, 0.3)
