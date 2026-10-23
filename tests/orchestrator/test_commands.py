"""Tests for command handlers."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from ductor_bot.cli.auth import AuthResult, AuthStatus
from ductor_bot.orchestrator.commands import (
    cmd_cron,
    cmd_diagnose,
    cmd_effort,
    cmd_memory,
    cmd_model,
    cmd_status,
)
from ductor_bot.orchestrator.core import Orchestrator
from ductor_bot.session.key import SessionKey

# -- cmd_model (wizard + direct switch) --

_AUTHED = {
    "claude": AuthResult("claude", AuthStatus.AUTHENTICATED),
    "codex": AuthResult("codex", AuthStatus.AUTHENTICATED),
}


async def test_model_list_returns_keyboard(orch: Orchestrator) -> None:
    with patch(
        "ductor_bot.orchestrator.selectors.model_selector.check_all_auth", return_value=_AUTHED
    ):
        result = await cmd_model(orch, SessionKey(chat_id=1), "/model")
    assert result.buttons is not None
    assert "Model Selector" in result.text


async def test_model_direct_switch(orch: Orchestrator) -> None:
    kill_mock = AsyncMock(return_value=0)
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", kill_mock)
    result = await cmd_model(orch, SessionKey(chat_id=1), "/model sonnet")
    assert "opus" in result.text
    assert "sonnet" in result.text
    assert orch._config.model == "sonnet"
    kill_mock.assert_called_once_with(1, None)


async def test_model_direct_claude_context_alias(orch: Orchestrator) -> None:
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", AsyncMock(return_value=0))
    await cmd_model(orch, SessionKey(chat_id=1), "/model opus[1m]")
    assert orch._config.provider == "claude"
    assert orch._config.model == "opus[1m]"


async def test_model_already_set(orch: Orchestrator) -> None:
    result = await cmd_model(orch, SessionKey(chat_id=1), "/model opus")
    assert "Already running" in result.text


async def test_model_provider_change(orch: Orchestrator) -> None:
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", AsyncMock(return_value=0))
    result = await cmd_model(orch, SessionKey(chat_id=1), "/model o3")
    assert "Provider:" in result.text


async def test_model_switch_persists_to_config(orch: Orchestrator) -> None:
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", AsyncMock(return_value=0))
    await cmd_model(orch, SessionKey(chat_id=1), "/model sonnet")
    saved = json.loads(orch.paths.config_path.read_text(encoding="utf-8"))
    assert saved["model"] == "sonnet"
    assert saved["provider"] == "claude"


async def test_model_provider_change_persists_to_config(orch: Orchestrator) -> None:
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", AsyncMock(return_value=0))
    await cmd_model(orch, SessionKey(chat_id=1), "/model o3")
    saved = json.loads(orch.paths.config_path.read_text(encoding="utf-8"))
    assert saved["model"] == "o3"
    assert saved["provider"] == "codex"


async def test_model_same_provider_does_not_show_reset(orch: Orchestrator) -> None:
    kill_mock = AsyncMock(return_value=0)
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", kill_mock)
    result = await cmd_model(orch, SessionKey(chat_id=1), "/model sonnet")
    assert "Session reset" not in result.text
    assert "Provider:" not in result.text
    kill_mock.assert_called_once_with(1, None)


# -- cmd_status --


async def test_status_no_session(orch: Orchestrator) -> None:
    with patch("ductor_bot.orchestrator.commands.check_all_auth", return_value={}):
        result = await cmd_status(orch, SessionKey(chat_id=1), "/status")
    assert "No active session" in result.text
    assert "opus" in result.text


async def test_status_with_session(orch: Orchestrator) -> None:
    await orch._sessions.resolve_session(SessionKey(chat_id=1))
    with patch("ductor_bot.orchestrator.commands.check_all_auth", return_value={}):
        result = await cmd_status(orch, SessionKey(chat_id=1), "/status")
    assert "Session:" in result.text
    assert "Messages:" in result.text


async def test_status_prefers_session_model_over_config(orch: Orchestrator) -> None:
    await orch._sessions.resolve_session(
        SessionKey(chat_id=1), provider="codex", model="gpt-5.2-codex"
    )
    with patch("ductor_bot.orchestrator.commands.check_all_auth", return_value={}):
        result = await cmd_status(orch, SessionKey(chat_id=1), "/status")
    assert "Model: gpt-5.2-codex (configured: opus)" in result.text


async def test_status_shows_streaming_visibility_flags(orch: Orchestrator) -> None:
    orch._config.streaming.show_reasoning_stream = True
    orch._config.streaming.show_tool_progress = False
    orch._config.streaming.show_thinking_indicator = False

    with patch("ductor_bot.orchestrator.commands.check_all_auth", return_value={}):
        result = await cmd_status(orch, SessionKey(chat_id=1), "/status")

    assert "Reasoning stream: on" in result.text
    assert "Tool progress: off" in result.text
    assert "Thinking indicator: off" in result.text


# -- cmd_memory --


async def test_memory_shows_content(orch: Orchestrator) -> None:
    orch.paths.mainmemory_path.write_text("# My Memories\n- Learned X")
    result = await cmd_memory(orch, SessionKey(chat_id=0), "/memory")
    assert "My Memories" in result.text


async def test_memory_empty(orch: Orchestrator) -> None:
    orch.paths.mainmemory_path.write_text("")
    result = await cmd_memory(orch, SessionKey(chat_id=0), "/memory")
    assert "empty" in result.text.lower()


# -- cmd_cron --


async def test_cron_no_jobs(orch: Orchestrator) -> None:
    result = await cmd_cron(orch, SessionKey(chat_id=0), "/cron")
    assert "No cron jobs" in result.text


async def test_cron_lists_jobs(orch: Orchestrator) -> None:
    from ductor_bot.cron.manager import CronJob

    orch._cron_manager.add_job(
        CronJob(
            id="test-job",
            title="Test Job",
            description="A test job",
            schedule="0 9 * * *",
            agent_instruction="do stuff",
            task_folder="test-task",
        ),
    )
    result = await cmd_cron(orch, SessionKey(chat_id=0), "/cron")
    assert result.buttons is not None
    assert "0 9 * * *" in result.text
    assert "Test Job" in result.text
    assert "active" in result.text


# -- cmd_diagnose --


async def test_diagnose_no_logs(orch: Orchestrator) -> None:
    result = await cmd_diagnose(orch, SessionKey(chat_id=0), "/diagnose")
    assert "Diagnostics" in result.text
    assert "No log file" in result.text


async def test_diagnose_with_logs(orch: Orchestrator) -> None:
    log_path = orch.paths.logs_dir / "agent.log"
    log_path.write_text("2024-01-01 INFO Started\n2024-01-01 ERROR Something broke\n")
    result = await cmd_diagnose(orch, SessionKey(chat_id=0), "/diagnose")
    assert "Something broke" in result.text


async def test_diagnose_shows_cache_status(orch: Orchestrator) -> None:
    """Should display Codex cache status in /diagnose output."""
    from datetime import UTC, datetime
    from unittest.mock import MagicMock

    from ductor_bot.cli.codex_cache import CodexModelCache
    from ductor_bot.cli.codex_discovery import CodexModelInfo

    # Create mock cache with test data
    mock_cache = CodexModelCache(
        last_updated=datetime.now(UTC).isoformat(),
        models=[
            CodexModelInfo(
                id="gpt-4o",
                display_name="GPT-4o",
                description="Test model",
                supported_efforts=("low", "medium", "high"),
                default_effort="medium",
                is_default=True,
            ),
        ],
    )

    # Mock the cache observer
    mock_observer = MagicMock()
    mock_observer.get_cache = MagicMock(return_value=mock_cache)
    orch._observers.codex_cache_obs = mock_observer

    result = await cmd_diagnose(orch, SessionKey(chat_id=0), "/diagnose")

    # Verify cache info is in output
    assert "Codex Model Cache" in result.text
    assert "Models cached: 1" in result.text
    assert "Default model: gpt-4o" in result.text


async def test_diagnose_shows_effective_runtime_target(orch: Orchestrator) -> None:
    orch._providers._available_providers = frozenset({"codex"})

    result = await cmd_diagnose(orch, SessionKey(chat_id=0), "/diagnose")

    assert "Configured: claude / opus" in result.text
    assert "Effective runtime: claude / opus" in result.text


# -- cmd_model (unknown model) --


async def test_model_unknown_name(orch: Orchestrator) -> None:
    """Unknown model names are treated as codex models and the switch succeeds."""
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", AsyncMock(return_value=0))
    result = await cmd_model(orch, SessionKey(chat_id=1), "/model totally_fake_model")
    assert "Model switched" in result.text
    assert "totally_fake_model" in result.text
    assert orch._config.model == "totally_fake_model"
    assert orch._config.provider == "codex"


# -- /status effort line + /effort -----------------------------------------


async def test_status_shows_effort_for_claude(orch: Orchestrator) -> None:
    orch._config.reasoning_effort = "high"
    with patch("ductor_bot.orchestrator.commands.check_all_auth", return_value={}):
        result = await cmd_status(orch, SessionKey(chat_id=1), "/status")
    assert "Reasoning effort: high" in result.text


async def test_status_hides_effort_for_non_effort_provider(orch: Orchestrator) -> None:
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", AsyncMock(return_value=0))
    from ductor_bot.orchestrator.selectors.model_selector import switch_model

    await switch_model(orch, SessionKey(chat_id=1), "gemini-2.5-pro")
    with patch("ductor_bot.orchestrator.commands.check_all_auth", return_value={}):
        result = await cmd_status(orch, SessionKey(chat_id=1), "/status")
    assert "Reasoning effort:" not in result.text


async def test_cmd_effort_returns_selector(orch: Orchestrator) -> None:
    result = await cmd_effort(orch, SessionKey(chat_id=1), "/effort")  # claude opus
    assert result.buttons is not None
    labels = [b.text for row in result.buttons.rows for b in row]
    assert "Max" in labels


async def test_cmd_effort_unsupported_provider_no_buttons(orch: Orchestrator) -> None:
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", AsyncMock(return_value=0))
    from ductor_bot.orchestrator.selectors.model_selector import switch_model

    await switch_model(orch, SessionKey(chat_id=1), "gemini-2.5-pro")
    result = await cmd_effort(orch, SessionKey(chat_id=1), "/effort")
    assert result.buttons is None


# -- /status and /sessions show no token or cost figures ------------------------------------------


@pytest.mark.parametrize(
    ("language", "labels"), [("en", ("Tokens", "Cost")), ("ko", ("\ud1a0\ud070", "\ube44\uc6a9"))]
)
async def test_status_has_no_token_or_cost_lines(
    orch: Orchestrator, language: str, labels: tuple[str, ...]
) -> None:
    from ductor_bot import i18n

    await orch._sessions.resolve_session(SessionKey(chat_id=1))
    i18n.init(language)
    try:
        with patch("ductor_bot.orchestrator.commands.check_all_auth", return_value={}):
            result = await cmd_status(orch, SessionKey(chat_id=1), "/status")
    finally:
        i18n.init("en")
    assert "$" not in result.text
    for label in labels:
        assert label not in result.text


def test_sessions_topic_block_has_no_cost() -> None:
    from ductor_bot.orchestrator.selectors.session_selector import _format_topic_block
    from ductor_bot.session.manager import SessionData

    topic = SessionData(chat_id=1, topic_id=7, topic_name="Plans", provider="claude", model="opus")
    topic.message_count = 3
    block = _format_topic_block([topic])
    assert "Plans" in block
    assert "claude/opus" in block
    assert "3 msgs" in block
    assert "$" not in block


# -- /diagnose hides the figures of old completion lines --------------------------------------

_HEAD = "2025-06-15 12:00:00 [INFO] {logger}:{file}.py:{line}: {context}"
_CONTEXT = "[main:msg:123:abcd1234] "
# (logger, source file, message with figures, message the display must show)
_OLD_LINES = [
    (
        "ductor_bot.cli.claude_provider",
        "claude_provider",
        "CLI done session=abcd1234 turns=2 cost=$0.0500 tokens=700 duration_ms=1500",
        "CLI done session=abcd1234 turns=2 duration_ms=1500",
    ),
    (
        "ductor_bot.cli.grok_provider",
        "grok_provider",
        "Grok done session=s9 turns=1 cost=$0.0042 tokens=1050",
        "Grok done session=s9 turns=1",
    ),
    (
        "ductor_bot.cli.codex_provider",
        "codex_provider",
        "Codex done session=th-42 tokens=150",
        "Codex done session=th-42",
    ),
    (
        "ductor_bot.cli.service",
        "service",
        "CLI main [ok] cost=$0.0100 tokens=500 duration_ms=1200",
        "CLI main [ok] duration_ms=1200",
    ),
    (
        "ductor_bot.cli.service",
        "service",
        "CLI task:ab12 [error] cost=$0.0000 tokens=0 duration_ms=90",
        "CLI task:ab12 [error] duration_ms=90",
    ),
]


def _log_line(logger_name: str, source: str, message: str, *, context: bool) -> str:
    head = _HEAD.format(
        logger=logger_name, file=source, line=706, context=_CONTEXT if context else ""
    )
    return head + message


def _write_log(orch: Orchestrator, lines: list[str]) -> None:
    log_path = orch.paths.logs_dir / "agent.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.mark.parametrize("context", [True, False])
@pytest.mark.parametrize(("logger_name", "source", "old", "shown"), _OLD_LINES)
async def test_diagnose_hides_figures_of_old_completion_lines(
    orch: Orchestrator, *, logger_name: str, source: str, old: str, shown: str, context: bool
) -> None:
    filler = [f"2025-06-15 11:00:{i:02d} [INFO] other.module:m.py:1: filler {i}" for i in range(10)]
    line = _log_line(logger_name, source, old, context=context)
    _write_log(orch, [*filler, line])

    via_command = await cmd_diagnose(orch, SessionKey(chat_id=1), "/diagnose")
    via_api_path = await orch.handle_message_streaming(SessionKey(chat_id=1), "/diagnose")

    for result in (via_command, via_api_path):
        assert "cost=$" not in result.text
        assert "tokens=" not in result.text
        assert _log_line(logger_name, source, shown, context=context) in result.text
    # The file itself is not rewritten.
    assert line in (orch.paths.logs_dir / "agent.log").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "line",
    [
        # Same logger, another message.
        (
            "2025-06-15 12:00:00 [ERROR] ductor_bot.cli.claude_provider:claude_provider.py:1: "
            "CLI error: cost=$5 tokens=10 rate limit"
        ),
        # Another logger with a similar message.
        (
            "2025-06-15 12:00:00 [INFO] ductor_bot.cli.gemini_provider:gemini_provider.py:1: "
            "CLI done session=ab turns=1 cost=$0.0100 tokens=5"
        ),
        # A user message that happens to contain the same text.
        (
            "2025-06-15 12:00:00 [INFO] ductor_bot.messenger.telegram.app:app.py:1: "
            "[main:msg:1:ab] Message from user: cost=$5 tokens=10"
        ),
        # A line that is not in the log format at all.
        "CLI done session=ab turns=1 cost=$0.0100 tokens=5 duration_ms=1",
        # An authentication token in a line of another logger.
        "2025-06-15 12:00:00 [INFO] ductor_bot.api.server:server.py:1: auth token=abc123 ok",
        # A completion message of a known logger without the exact prefix.
        (
            "2025-06-15 12:00:00 [INFO] ductor_bot.cli.codex_provider:codex_provider.py:1: "
            "Codex error exit=1 tokens=3"
        ),
    ],
)
async def test_diagnose_leaves_other_log_lines_alone(orch: Orchestrator, line: str) -> None:
    _write_log(orch, [line])
    result = await cmd_diagnose(orch, SessionKey(chat_id=1), "/diagnose")
    assert line in result.text
