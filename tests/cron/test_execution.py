"""Tests for cron/execution.py: CLI command building and output parsing."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from ductor_bot.cli.param_resolver import TaskExecutionConfig
from ductor_bot.cron.execution import (
    OneShotCommand,
    build_cmd,
    enrich_instruction,
    execute_one_shot,
    parse_claude_result,
    parse_codex_result,
    parse_gemini_result,
    parse_result,
)


class TestBuildCmd:
    def test_claude_provider(self) -> None:
        exec_config = TaskExecutionConfig(
            provider="claude",
            model="opus",
            reasoning_effort="",
            cli_parameters=[],
            permission_mode="bypassPermissions",
            working_dir="/tmp",
            file_access="all",
        )
        with patch("ductor_bot.cron.execution.which", return_value="/usr/bin/claude"):
            result = build_cmd(exec_config, "hello")
        assert result is not None
        assert result.cmd[0] == "/usr/bin/claude"
        assert "--no-session-persistence" in result.cmd
        # Claude: prompt over stdin, never in argv
        assert result.stdin_input == b"hello"
        assert "hello" not in result.cmd
        assert "--" not in result.cmd

    def test_codex_provider(self) -> None:
        exec_config = TaskExecutionConfig(
            provider="codex",
            model="gpt-4",
            reasoning_effort="medium",
            cli_parameters=[],
            permission_mode="bypassPermissions",
            working_dir="/tmp",
            file_access="all",
        )
        with patch("ductor_bot.cron.execution.which", return_value="/usr/bin/codex"):
            result = build_cmd(exec_config, "hello")
        assert result is not None
        assert result.cmd[0] == "/usr/bin/codex"
        assert "--dangerously-bypass-approvals-and-sandbox" in result.cmd
        # Codex: "-" reads the prompt from stdin, never from argv
        assert result.stdin_input == b"hello"
        assert result.cmd[-2:] == ["--", "-"]
        assert "hello" not in result.cmd

    def test_codex_full_auto(self) -> None:
        exec_config = TaskExecutionConfig(
            provider="codex",
            model="gpt-4",
            reasoning_effort="medium",
            cli_parameters=[],
            permission_mode="plan",
            working_dir="/tmp",
            file_access="all",
        )
        with patch("ductor_bot.cron.execution.which", return_value="/usr/bin/codex"):
            result = build_cmd(exec_config, "hello")
        assert result is not None
        assert "--full-auto" in result.cmd

    def test_returns_none_when_cli_missing(self) -> None:
        exec_config = TaskExecutionConfig(
            provider="claude",
            model="opus",
            reasoning_effort="",
            cli_parameters=[],
            permission_mode="plan",
            working_dir="/tmp",
            file_access="all",
        )
        with patch("ductor_bot.cron.execution.which", return_value=None):
            assert build_cmd(exec_config, "hello") is None

    def test_gemini_provider(self) -> None:
        exec_config = TaskExecutionConfig(
            provider="gemini",
            model="gemini-2.5-pro",
            reasoning_effort="",
            cli_parameters=[],
            permission_mode="bypassPermissions",
            working_dir="/tmp",
            file_access="all",
        )
        with patch("ductor_bot.cron.execution.find_gemini_cli", return_value="/usr/bin/gemini"):
            result = build_cmd(exec_config, "hello")
        assert result is not None
        assert result.cmd[0] == "/usr/bin/gemini"
        assert "--approval-mode" in result.cmd
        assert "yolo" in result.cmd
        # Hybrid mode: -p "" instead of -- prompt
        assert "-p" in result.cmd
        p_idx = result.cmd.index("-p")
        assert result.cmd[p_idx + 1] == ""
        assert "--" not in result.cmd
        assert "hello" not in result.cmd
        # Prompt via stdin
        assert result.stdin_input == b"hello"

    def test_gemini_returns_none_when_cli_missing(self) -> None:
        exec_config = TaskExecutionConfig(
            provider="gemini",
            model="gemini-2.5-pro",
            reasoning_effort="",
            cli_parameters=[],
            permission_mode="plan",
            working_dir="/tmp",
            file_access="all",
        )
        with patch(
            "ductor_bot.cron.execution.find_gemini_cli",
            side_effect=FileNotFoundError("not found"),
        ):
            assert build_cmd(exec_config, "hello") is None

    def test_unknown_provider_falls_back_to_claude(self) -> None:
        exec_config = TaskExecutionConfig(
            provider="unknown",
            model="model",
            reasoning_effort="",
            cli_parameters=[],
            permission_mode="plan",
            working_dir="/tmp",
            file_access="all",
        )
        with patch("ductor_bot.cron.execution.which", return_value="/usr/bin/claude"):
            result = build_cmd(exec_config, "hello")
        assert result is not None
        assert result.cmd[0] == "/usr/bin/claude"
        assert result.stdin_input == b"hello"


class TestExecuteOneShotStdin:
    """Test execute_one_shot stdin_input parameter."""

    async def test_with_stdin_input_uses_pipe(self) -> None:
        """stdin_input is forwarded to subprocess via PIPE."""
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            proc = AsyncMock()
            proc.communicate.return_value = (b'{"result":"ok"}', b"")
            proc.returncode = 0
            mock_exec.return_value = proc

            result = await execute_one_shot(
                OneShotCommand(cmd=["/usr/bin/gemini", "-p", ""], stdin_input=b"hello"),
                cwd=Path("/tmp"),
                provider="gemini",
                timeout_seconds=60,
                timeout_label="Test",
            )

        call_kwargs = mock_exec.call_args[1]
        assert call_kwargs["stdin"] == asyncio.subprocess.PIPE
        proc.communicate.assert_called_once_with(input=b"hello")
        assert result.status == "success"

    async def test_without_stdin_input_uses_devnull(self) -> None:
        """Without stdin_input, DEVNULL is used (backward compat)."""
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            proc = AsyncMock()
            proc.communicate.return_value = (b'{"result":"ok"}', b"")
            proc.returncode = 0
            mock_exec.return_value = proc

            await execute_one_shot(
                OneShotCommand(cmd=["/usr/bin/claude", "-p", "--", "hello"]),
                cwd=Path("/tmp"),
                provider="claude",
                timeout_seconds=60,
                timeout_label="Test",
            )

        call_kwargs = mock_exec.call_args[1]
        assert call_kwargs["stdin"] == asyncio.subprocess.DEVNULL
        proc.communicate.assert_called_once_with(input=None)

    async def test_env_overrides_are_merged_into_process_environment(self) -> None:
        """Command-specific env overrides are passed without dropping host env."""
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
            proc = AsyncMock()
            proc.communicate.return_value = (b'{"result":"ok"}', b"")
            proc.returncode = 0
            mock_exec.return_value = proc

            await execute_one_shot(
                OneShotCommand(
                    cmd=["/usr/bin/claude", "-p", "--", "hello"],
                    env_overrides={"DUCTOR_HOME": "/tmp/agent-home"},
                ),
                cwd=Path("/tmp"),
                provider="claude",
                timeout_seconds=60,
                timeout_label="Test",
            )

        env = mock_exec.call_args[1]["env"]
        assert env["DUCTOR_HOME"] == "/tmp/agent-home"
        assert "PATH" in env


class TestEnrichInstruction:
    def test_appends_memory_instructions(self) -> None:
        result = enrich_instruction("Do the work", "daily-report")
        assert "daily-report_MEMORY.md" in result
        assert "Do the work" in result

    def test_preserves_original(self) -> None:
        original = "Original instruction"
        result = enrich_instruction(original, "weekly")
        assert result.startswith(original)


class TestParseClaude:
    def test_parses_json(self) -> None:
        import json

        stdout = json.dumps({"result": "Hello world"}).encode()
        assert parse_claude_result(stdout) == "Hello world"

    def test_empty_bytes(self) -> None:
        assert parse_claude_result(b"") == ""

    def test_non_json_returns_raw(self) -> None:
        raw = b"Some raw text output"
        assert parse_claude_result(raw) == "Some raw text output"


class TestParseCodex:
    def test_empty_bytes(self) -> None:
        assert parse_codex_result(b"") == ""

    def test_parsed_jsonl_with_no_text_returns_empty(self) -> None:
        """Silent-success: valid JSONL events but no assistant text -> empty."""
        raw = (
            '{"type":"thread.started","thread_id":"t1"}\n'
            '{"type":"turn.started"}\n'
            '{"type":"item.started","item":{"type":"command_execution"}}\n'
            '{"type":"item.completed","item":{"type":"command_execution"}}\n'
            '{"type":"turn.completed","usage":{"input_tokens":10}}\n'
        )
        assert parse_codex_result(raw.encode()) == ""

    def test_parsed_jsonl_with_text_returns_text(self) -> None:
        raw = (
            '{"type":"thread.started","thread_id":"t1"}\n'
            '{"type":"item.completed","item":{"type":"agent_message","text":"Hello"}}\n'
            '{"type":"turn.completed","usage":{"input_tokens":10}}\n'
        )
        assert parse_codex_result(raw.encode()) == "Hello"

    def test_non_jsonl_returns_raw(self) -> None:
        raw = b"Plain text output from codex"
        assert parse_codex_result(raw) == "Plain text output from codex"


class TestParseGemini:
    def test_empty_bytes(self) -> None:
        assert parse_gemini_result(b"") == ""

    def test_json_response(self) -> None:
        import json

        data = json.dumps([{"type": "message", "role": "model", "content": "Result text"}])
        result = parse_gemini_result(data.encode())
        assert "Result text" in result

    def test_non_json_returns_raw(self) -> None:
        raw = b"Raw gemini output"
        assert parse_gemini_result(raw) == "Raw gemini output"


class TestParseResult:
    def test_dispatches_to_gemini_parser(self) -> None:
        assert parse_result("gemini", b'{"result":"ok"}') == "ok"

    def test_unknown_provider_falls_back_to_claude(self) -> None:
        assert parse_result("unknown", b'{"result":"fallback"}') == "fallback"


# ---------------------------------------------------------------------------
# One-shot runs never put the prompt in argv (visible in ps)
# ---------------------------------------------------------------------------

_MARKER_PROMPT = "CRON-PROMPT-MARKER do the thing"
_HAS_PROCFS = Path("/proc/self/cmdline").exists()


def _gone(path: Path) -> bool:
    return not path.exists()


@pytest.mark.parametrize("provider", ["claude", "codex", "grok"])
async def test_one_shot_spawn_argv_carries_no_prompt(
    provider: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = f"/usr/bin/{provider}"
    monkeypatch.setattr("ductor_bot.cron.execution.which", lambda _: binary)
    exec_config = TaskExecutionConfig(
        provider=provider,
        model="m",
        reasoning_effort="",
        cli_parameters=[],
        permission_mode="bypassPermissions",
        working_dir="/tmp",
        file_access="all",
    )
    one_shot = build_cmd(exec_config, _MARKER_PROMPT)
    assert one_shot is not None
    seen: dict[str, object] = {}

    def _spawn(*argv: str, **kwargs: object) -> AsyncMock:
        seen["argv"] = list(argv)
        seen["stdin"] = kwargs["stdin"]
        if "--prompt-file" in argv:
            path = Path(argv[argv.index("--prompt-file") + 1])
            seen["file"] = (path, path.read_text(encoding="utf-8"), path.stat().st_mode & 0o777)
        proc = AsyncMock()
        proc.communicate.return_value = (b"{}", b"")
        proc.returncode = 0
        seen["proc"] = proc
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=_spawn):
        await execute_one_shot(
            one_shot, cwd=Path("/tmp"), provider=provider, timeout_seconds=5, timeout_label="T"
        )

    assert not any("MARKER" in arg for arg in seen["argv"])  # type: ignore[attr-defined]
    if provider == "grok":
        path, text, mode = seen["file"]  # type: ignore[misc]
        assert (text, mode) == (_MARKER_PROMPT, 0o600)
        assert _gone(path)  # removed after the run
        assert seen["stdin"] == asyncio.subprocess.DEVNULL
    else:
        assert seen["stdin"] == asyncio.subprocess.PIPE
        seen["proc"].communicate.assert_called_once_with(input=_MARKER_PROMPT.encode())  # type: ignore[attr-defined]


async def test_one_shot_real_process_sees_prompt_only_on_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real subprocess standing in for ``claude`` reads its own /proc cmdline."""
    if not _HAS_PROCFS:
        pytest.skip("needs procfs")
    fake = tmp_path / "claude"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "cmdline = open('/proc/self/cmdline', 'rb').read().decode().split('\\0')\n"
        "print(json.dumps({'result': json.dumps({'cmdline': cmdline, 'stdin': sys.stdin.read()}),"
        " 'is_error': False}))\n",
        encoding="utf-8",
    )
    fake.chmod(0o700)
    monkeypatch.setattr("ductor_bot.cron.execution.which", lambda _: str(fake))
    exec_config = TaskExecutionConfig(
        provider="claude",
        model="m",
        reasoning_effort="",
        cli_parameters=[],
        permission_mode="bypassPermissions",
        working_dir=str(tmp_path),
        file_access="all",
    )
    one_shot = build_cmd(exec_config, _MARKER_PROMPT)
    assert one_shot is not None
    result = await execute_one_shot(
        one_shot, cwd=tmp_path, provider="claude", timeout_seconds=20, timeout_label="T"
    )
    assert result.status == "success"
    seen = json.loads(result.result_text)
    assert not any("MARKER" in arg for arg in seen["cmdline"])
    assert seen["stdin"] == _MARKER_PROMPT


class TestParseClaudeEvents:
    """``--verbose`` makes the Claude CLI print a JSON array of events."""

    def test_array_takes_the_last_result_event(self) -> None:
        import json

        events = [
            {"type": "system", "subtype": "init"},
            {"type": "result", "result": "first"},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "x"}]}},
            {"type": "result", "result": "Final answer"},
        ]
        assert parse_claude_result(json.dumps(events).encode()) == "Final answer"

    def test_array_without_result_uses_the_last_assistant_text(self) -> None:
        import json

        events = [
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "early"}]}},
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "Bash"},
                        {"type": "text", "text": "last "},
                        {"type": "text", "text": "words"},
                    ]
                },
            },
            {"type": "user"},
        ]
        assert parse_claude_result(json.dumps(events).encode()) == "last words"

    @pytest.mark.parametrize(
        "payload",
        [
            b"[]",
            b'[{"type": "system"}, "text", 3, null]',
            b'[{"type": "assistant", "message": {"content": [{"type": "tool_use"}]}}]',
        ],
    )
    def test_array_without_any_text_is_empty(self, payload: bytes) -> None:
        assert parse_claude_result(payload) == ""

    @pytest.mark.parametrize("payload", [b'"just a string"', b"42", b"null", b"true"])
    def test_other_json_values_are_empty(self, payload: bytes) -> None:
        assert parse_claude_result(payload) == ""

    def test_the_provider_dispatch_uses_the_same_parser(self) -> None:
        import json

        stdout = json.dumps([{"type": "result", "result": "via dispatch"}]).encode()
        assert parse_result("claude", stdout) == "via dispatch"


class TestParseCodexWithoutFigures:
    """The cron reading of Codex output keeps telling protocol output from text."""

    def test_usage_only_completion_is_empty_and_hides_the_events(self) -> None:
        raw = '{"type":"turn.completed","usage":{"input_tokens":10}}\n'
        assert parse_codex_result(raw.encode()) == ""

    def test_top_level_usage_only_is_empty(self) -> None:
        assert parse_codex_result(b'{"usage":{"total_tokens":3}}') == ""

    def test_normal_text_is_returned(self) -> None:
        raw = (
            '{"type":"item.completed","item":{"type":"agent_message","text":"Hello"}}\n'
            '{"type":"turn.completed","usage":{"input_tokens":10}}\n'
        )
        assert parse_codex_result(raw.encode()) == "Hello"

    def test_events_without_thread_or_usage_fall_back_to_the_raw_output(self) -> None:
        raw = '{"type":"turn.started"}\n'
        assert parse_codex_result(raw.encode()) == raw.strip()

    def test_unparseable_output_falls_back_to_the_raw_output(self) -> None:
        assert parse_codex_result(b"not json at all") == "not json at all"
