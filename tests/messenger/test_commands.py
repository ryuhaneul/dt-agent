"""Tests for the shared command classification module."""

from __future__ import annotations

import pytest

from ductor_bot.commands import BOT_COMMANDS, MULTIAGENT_SUB_COMMANDS
from ductor_bot.messenger.commands import (
    DIRECT_COMMANDS,
    MULTIAGENT_COMMANDS,
    ORCHESTRATOR_COMMANDS,
    classify_command,
)


class TestClassifyCommand:
    """Tests for classify_command()."""

    def test_direct_commands(self) -> None:
        for cmd in DIRECT_COMMANDS:
            assert classify_command(cmd) == "direct", f"{cmd} should be direct"

    def test_orchestrator_commands(self) -> None:
        for cmd in ORCHESTRATOR_COMMANDS:
            assert classify_command(cmd) == "orchestrator", f"{cmd} should be orchestrator"

    def test_multiagent_commands(self) -> None:
        for cmd in MULTIAGENT_COMMANDS:
            assert classify_command(cmd) == "multiagent", f"{cmd} should be multiagent"

    def test_unknown_command(self) -> None:
        assert classify_command("nonexistent") == "unknown"
        assert classify_command("") == "unknown"
        assert classify_command("foobar") == "unknown"


class TestCommandSetIntegrity:
    """Tests for structural invariants of the command sets."""

    def test_no_overlap_direct_orchestrator(self) -> None:
        overlap = DIRECT_COMMANDS & ORCHESTRATOR_COMMANDS
        assert not overlap, f"DIRECT and ORCHESTRATOR overlap: {overlap}"

    def test_no_overlap_direct_multiagent(self) -> None:
        overlap = DIRECT_COMMANDS & MULTIAGENT_COMMANDS
        assert not overlap, f"DIRECT and MULTIAGENT overlap: {overlap}"

    def test_no_overlap_orchestrator_multiagent(self) -> None:
        overlap = ORCHESTRATOR_COMMANDS & MULTIAGENT_COMMANDS
        assert not overlap, f"ORCHESTRATOR and MULTIAGENT overlap: {overlap}"

    def test_all_bot_commands_classified(self) -> None:
        """Every command in BOT_COMMANDS must be classified (not unknown)."""
        for cmd_name, _desc in BOT_COMMANDS:
            result = classify_command(cmd_name)
            assert result != "unknown", f"BOT_COMMANDS entry {cmd_name!r} is not classified"

    def test_all_multiagent_sub_commands_classified(self) -> None:
        """Every command in MULTIAGENT_SUB_COMMANDS must be classified."""
        for cmd_name, _desc in MULTIAGENT_SUB_COMMANDS:
            result = classify_command(cmd_name)
            assert result != "unknown", (
                f"MULTIAGENT_SUB_COMMANDS entry {cmd_name!r} is not classified"
            )



@pytest.mark.parametrize("text", ["usage", "/usage"])
async def test_usage_keeps_matrix_general_message_fallback(text):
    from unittest.mock import AsyncMock

    from ductor_bot.messenger.matrix.bot import MatrixBot

    bot = MatrixBot.__new__(MatrixBot)
    bot._dispatch_with_lock = AsyncMock()
    pending = []
    bot._spawn_task = lambda coro, **kwargs: pending.append(coro)
    event = object()
    await bot._handle_command(text, "!fixture:example.test", 1, event)
    assert len(pending) == 1
    await pending[0]
    bot._dispatch_with_lock.assert_awaited_once()
    assert bot._dispatch_with_lock.call_args.args[1:] == (
        text, "!fixture:example.test", event,
    )
