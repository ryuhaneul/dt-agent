"""Tests for CLIService gateway."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

from ductor_bot.cli.process_registry import ProcessRegistry
from ductor_bot.cli.service import CLIService, CLIServiceConfig
from ductor_bot.cli.stream_events import StreamEvent, ToolUseEvent
from ductor_bot.cli.types import AgentRequest, AgentResponse, CLIResponse
from ductor_bot.config import ModelRegistry

if TYPE_CHECKING:
    import pytest


def _make_service(**overrides: Any) -> CLIService:
    config = CLIServiceConfig(
        working_dir=overrides.pop("working_dir", "/tmp"),
        default_model=overrides.pop("default_model", "opus"),
        provider=overrides.pop("provider", "claude"),
        max_turns=overrides.pop("max_turns", None),
        max_budget_usd=overrides.pop("max_budget_usd", None),
        permission_mode=overrides.pop("permission_mode", "bypassPermissions"),
    )
    models = ModelRegistry()

    return CLIService(
        config=config,
        models=models,
        available_providers=frozenset({"claude"}),
        process_registry=ProcessRegistry(),
    )


async def test_execute_returns_agent_response() -> None:
    svc = _make_service()
    mock_response = CLIResponse(
        result="Hello!",
        session_id="sess-1",
    )
    with patch("ductor_bot.cli.service.create_cli") as mock_create:
        mock_cli = AsyncMock()
        mock_cli.send.return_value = mock_response
        mock_create.return_value = mock_cli

        resp = await svc.execute(AgentRequest(prompt="hello", chat_id=1))

    assert resp.result == "Hello!"
    assert resp.session_id == "sess-1"
    assert resp.is_error is False


async def test_execute_error_response() -> None:
    svc = _make_service()
    mock_response = CLIResponse(result="Error occurred", is_error=True)
    with patch("ductor_bot.cli.service.create_cli") as mock_create:
        mock_cli = AsyncMock()
        mock_cli.send.return_value = mock_response
        mock_create.return_value = mock_cli

        resp = await svc.execute(AgentRequest(prompt="fail", chat_id=1))

    assert resp.is_error is True
    assert resp.result == "Error occurred"


async def test_execute_streaming_success() -> None:
    svc = _make_service()

    from ductor_bot.cli.stream_events import AssistantTextDelta, ResultEvent, ThinkingEvent

    async def fake_stream(*_args: Any, **_kwargs: Any) -> AsyncGenerator[StreamEvent, None]:
        yield ThinkingEvent(type="assistant", text="considering")
        yield AssistantTextDelta(type="assistant", text="Hello ")
        yield AssistantTextDelta(type="assistant", text="world!")
        yield ResultEvent(
            type="result",
            session_id="sess-1",
            result="Hello world!",
        )

    deltas: list[str] = []
    thinking: list[str] = []

    async def on_delta(text: str) -> None:
        deltas.append(text)

    async def on_thinking(text: str) -> None:
        thinking.append(text)

    with patch("ductor_bot.cli.service.create_cli") as mock_create:
        mock_cli = MagicMock()
        mock_cli.send_streaming = fake_stream
        mock_create.return_value = mock_cli

        resp = await svc.execute_streaming(
            AgentRequest(prompt="hello", chat_id=1),
            on_text_delta=on_delta,
            on_thinking_delta=on_thinking,
        )

    assert resp.result == "Hello world!"
    assert resp.session_id == "sess-1"
    assert thinking == ["considering"]
    assert deltas == ["Hello ", "world!"]


async def test_execute_streaming_fallback_on_error() -> None:
    svc = _make_service()

    mock_response = CLIResponse(result="Fallback result", session_id="sess-2")
    with patch("ductor_bot.cli.service.create_cli") as mock_create:
        mock_cli = MagicMock()
        mock_cli.send_streaming = MagicMock(side_effect=RuntimeError("Stream broken"))
        mock_cli.send = AsyncMock(return_value=mock_response)
        mock_create.return_value = mock_cli

        resp = await svc.execute_streaming(AgentRequest(prompt="hello", chat_id=1))

    assert resp.stream_fallback is True
    assert resp.result == "Fallback result"


def test_update_default_model() -> None:
    svc = _make_service()
    svc.update_default_model("sonnet")
    assert svc._config.default_model == "sonnet"


def test_update_available_providers() -> None:
    svc = _make_service()
    svc.update_available_providers(frozenset({"claude", "codex"}))
    assert svc._available_providers == frozenset({"claude", "codex"})


async def test_stream_callbacks_dispatches_compact_boundary() -> None:
    """CompactBoundaryEvent fires on_compact_boundary and on_status(None), in order."""
    from ductor_bot.cli.service import _StreamCallbacks
    from ductor_bot.cli.stream_events import CompactBoundaryEvent

    order: list[str] = []

    async def on_boundary() -> None:
        order.append("boundary")

    async def on_status(status: str | None) -> None:
        order.append(f"status:{status}")

    cbs = _StreamCallbacks(
        on_text=None,
        on_thinking=None,
        on_tool=None,
        on_status=on_status,
        on_compact_boundary=on_boundary,
    )
    event = CompactBoundaryEvent(
        type="system", subtype="compact_boundary", trigger="auto", pre_tokens=12345
    )
    text, result = await cbs.dispatch(event)

    assert text == ""
    assert result is None
    assert order == ["boundary", "status:None"]


async def test_stream_callbacks_dispatches_thinking_text() -> None:
    from ductor_bot.cli.service import _StreamCallbacks
    from ductor_bot.cli.stream_events import ThinkingEvent

    seen: list[str] = []
    statuses: list[str | None] = []

    async def on_thinking(text: str) -> None:
        seen.append(text)

    async def on_status(status: str | None) -> None:
        statuses.append(status)

    cbs = _StreamCallbacks(
        on_text=None,
        on_thinking=on_thinking,
        on_tool=None,
        on_status=on_status,
    )
    text, result = await cbs.dispatch(ThinkingEvent(type="assistant", text="step 1"))

    assert text == ""
    assert result is None
    assert seen == ["step 1"]
    assert statuses == ["thinking"]


async def test_stream_callbacks_dispatch_tool_event() -> None:
    from ductor_bot.cli.service import _StreamCallbacks

    seen: list[ToolUseEvent] = []

    async def on_tool(event: ToolUseEvent) -> None:
        seen.append(event)

    cbs = _StreamCallbacks(
        on_text=None,
        on_thinking=None,
        on_tool=on_tool,
        on_status=None,
    )
    event = ToolUseEvent(
        type="assistant",
        tool_name="WebFetch",
        parameters={"url": "https://slack.dev/slack-thinking-steps-ai-agents/"},
    )
    text, result = await cbs.dispatch(event)

    assert text == ""
    assert result is None
    assert seen == [event]


def test_make_cli_uses_working_dir_resolver() -> None:
    svc = _make_service(working_dir="/default/workspace")
    svc.set_working_dir_resolver(lambda _req: "/projects/alpha")

    with patch("ductor_bot.cli.service.create_cli") as mock_create:
        svc._make_cli(AgentRequest(prompt="hi", chat_id=1, topic_id=5))

    cli_config = mock_create.call_args.args[0]
    assert cli_config.working_dir == "/projects/alpha"


def test_make_cli_resolver_none_keeps_default_working_dir() -> None:
    svc = _make_service(working_dir="/default/workspace")
    svc.set_working_dir_resolver(lambda _req: None)

    with patch("ductor_bot.cli.service.create_cli") as mock_create:
        svc._make_cli(AgentRequest(prompt="hi", chat_id=1, topic_id=5))

    cli_config = mock_create.call_args.args[0]
    assert cli_config.working_dir == "/default/workspace"


def test_make_cli_no_resolver_keeps_default_working_dir() -> None:
    svc = _make_service(working_dir="/default/workspace")

    with patch("ductor_bot.cli.service.create_cli") as mock_create:
        svc._make_cli(AgentRequest(prompt="hi", chat_id=1))

    cli_config = mock_create.call_args.args[0]
    assert cli_config.working_dir == "/default/workspace"


def test_make_cli_override_appends_absolute_memory_note() -> None:
    """With a cwd override the agent is told the absolute bot-memory path."""
    svc = _make_service(working_dir="/default/workspace")
    svc.set_working_dir_resolver(lambda _req: "/projects/alpha")

    with patch("ductor_bot.cli.service.create_cli") as mock_create:
        svc._make_cli(
            AgentRequest(prompt="hi", chat_id=1, topic_id=5, append_system_prompt="MEMORY DUMP")
        )

    cli_config = mock_create.call_args.args[0]
    assert cli_config.working_dir == "/projects/alpha"
    assert cli_config.append_system_prompt.startswith("MEMORY DUMP")
    assert "/default/workspace/memory_system/MAINMEMORY.md" in cli_config.append_system_prompt


def test_make_cli_memory_flush_in_project_root_keeps_workspace_memory_path() -> None:
    """Maintenance resumes in the project directory while bot memory stays in the workspace."""
    svc = _make_service(working_dir="/default/workspace")
    svc.set_working_dir_resolver(lambda _req: "/projects/alpha")

    with patch("ductor_bot.cli.service.create_cli") as mock_create:
        svc._make_cli(
            AgentRequest(prompt="flush", chat_id=1, topic_id=5, process_label="memory_flush")
        )

    cli_config = mock_create.call_args.args[0]
    assert "/default/workspace/memory_system/MAINMEMORY.md" in cli_config.append_system_prompt


def test_make_cli_no_override_leaves_append_prompt_untouched() -> None:
    svc = _make_service(working_dir="/default/workspace")
    svc.set_working_dir_resolver(lambda _req: None)

    with patch("ductor_bot.cli.service.create_cli") as mock_create:
        svc._make_cli(AgentRequest(prompt="hi", chat_id=1, append_system_prompt="MEMORY DUMP"))

    cli_config = mock_create.call_args.args[0]
    assert cli_config.append_system_prompt == "MEMORY DUMP"


async def test_interagent_turn_flag_reaches_cli_config() -> None:
    svc = _make_service()
    with patch("ductor_bot.cli.service.create_cli") as create:
        mock_cli = AsyncMock()
        mock_cli.send.return_value = CLIResponse(result="ok")
        create.return_value = mock_cli
        await svc.execute(AgentRequest(prompt="task", interagent_turn=True))
    assert create.call_args.args[0].interagent_turn is True


async def test_streaming_stale_kill_reaches_the_agent_response() -> None:
    from ductor_bot.cli.stream_events import ResultEvent

    svc = _make_service()

    async def events(**_kwargs: Any) -> AsyncGenerator[StreamEvent, None]:
        yield ResultEvent(type="result", is_error=True, returncode=143, stale_killed=True)

    with patch("ductor_bot.cli.service.create_cli") as mock_create:
        mock_cli = MagicMock()
        mock_cli.send_streaming = events
        mock_create.return_value = mock_cli

        resp = await svc.execute_streaming(AgentRequest(prompt="hello", chat_id=1))

    assert resp.stale_killed is True
    assert resp.returncode == 143


async def test_a_normal_response_is_not_marked_stale() -> None:
    svc = _make_service()
    with patch("ductor_bot.cli.service.create_cli") as mock_create:
        mock_cli = AsyncMock()
        mock_cli.send.return_value = CLIResponse(result="ok")
        mock_create.return_value = mock_cli

        resp = await svc.execute(AgentRequest(prompt="hello", chat_id=1))

    assert resp.stale_killed is False


# -- a stale kill reaches the response whichever provider built it ---------------------------------


def _stale_registry_with_process(label: str) -> tuple[ProcessRegistry, Any]:
    reg = ProcessRegistry()
    proc = MagicMock()
    proc.pid = 77
    proc.returncode = None
    proc.wait = AsyncMock(return_value=None)
    tracked = reg.register(chat_id=1, process=proc, label=label)
    tracked.registered_at = 0.0  # as old as can be
    return reg, tracked


async def _kill_stale(reg: ProcessRegistry) -> None:
    with (
        patch("ductor_bot.cli.process_registry.asyncio.sleep", new_callable=AsyncMock),
        patch("ductor_bot.cli.process_registry.terminate_process_tree"),
        patch("ductor_bot.cli.process_registry.force_kill_process_tree"),
    ):
        await reg.kill_stale(60)


async def test_stale_kill_marks_a_response_the_provider_built_itself() -> None:
    svc = _make_service()
    reg, _tracked = _stale_registry_with_process("task:ab")
    svc._process_registry = reg

    async def send(**_kwargs: Any) -> CLIResponse:
        await _kill_stale(reg)  # the cleanup kills the run while the provider waits
        return CLIResponse(result="", is_error=True, returncode=143)  # no mark of its own

    with patch("ductor_bot.cli.service.create_cli") as mock_create:
        mock_create.return_value = MagicMock(send=send)
        killed = await svc.execute(AgentRequest(prompt="x", chat_id=1, process_label="task:ab"))

    assert killed.stale_killed is True


async def test_stale_kill_marks_a_streamed_response_the_provider_built_itself() -> None:
    from ductor_bot.cli.stream_events import ResultEvent

    svc = _make_service()
    reg, _tracked = _stale_registry_with_process("task:ab")
    svc._process_registry = reg

    async def events(**_kwargs: Any) -> AsyncGenerator[StreamEvent, None]:
        await _kill_stale(reg)
        yield ResultEvent(type="result", is_error=True, returncode=143)  # no mark of its own

    with patch("ductor_bot.cli.service.create_cli") as mock_create:
        mock_create.return_value = MagicMock(send_streaming=events)
        killed = await svc.execute_streaming(
            AgentRequest(prompt="x", chat_id=1, process_label="task:ab")
        )

    assert killed.stale_killed is True


async def test_the_next_run_of_the_same_label_starts_unmarked() -> None:
    svc = _make_service()
    reg, _tracked = _stale_registry_with_process("task:ab")
    svc._process_registry = reg
    await _kill_stale(reg)  # an earlier run was killed and nobody read the mark

    with patch("ductor_bot.cli.service.create_cli") as mock_create:
        mock_cli = AsyncMock()
        mock_cli.send.return_value = CLIResponse(result="fine")
        mock_create.return_value = mock_cli
        resp = await svc.execute(AgentRequest(prompt="x", chat_id=1, process_label="task:ab"))

    assert resp.stale_killed is False


def test_call_log_has_label_status_and_duration_but_no_figures(
    caplog: pytest.LogCaptureFixture,
) -> None:
    svc = _make_service()
    with caplog.at_level("INFO", logger="ductor_bot.cli.service"):
        svc._log_call(AgentRequest(prompt="hi", chat_id=1), AgentResponse(result="r"), 1500.0)
    assert "CLI main [ok] duration_ms=1500" in caplog.text
    assert "cost" not in caplog.text
    assert "tokens" not in caplog.text
