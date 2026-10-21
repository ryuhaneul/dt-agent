"""Shared test fixtures."""

from __future__ import annotations

import os
from collections.abc import Iterator
from unittest.mock import patch

import pytest

from ductor_bot.config import reset_gemini_models


@pytest.fixture(autouse=True)
def _isolate_ductor_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep inherited agent routing/configuration out of test scenarios."""
    for key in tuple(os.environ):
        if key.startswith("DUCTOR_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _no_real_process_signals() -> object:
    """Globally prevent tests from sending real signals to system processes.

    Multiple modules import process_tree helpers that send real OS signals.
    Mock processes carry arbitrary PIDs (e.g. 1, 10) that correspond to real
    system processes — sending signals to them crashes the desktop session.
    """
    with (
        patch(
            "ductor_bot.cli.process_registry.terminate_process_tree",
            return_value=None,
        ),
        patch(
            "ductor_bot.cli.process_registry.force_kill_process_tree",
            return_value=None,
        ),
        patch(
            "ductor_bot.cli.process_registry.interrupt_process",
            return_value=None,
        ),
        patch(
            "ductor_bot.cli.executor.force_kill_process_tree",
            return_value=None,
        ),
        patch(
            "ductor_bot.cli.gemini_provider.force_kill_process_tree",
            return_value=None,
        ),
        patch(
            "ductor_bot.cron.execution.force_kill_process_tree",
            return_value=None,
        ),
        patch(
            "ductor_bot.infra.pidlock.terminate_process_tree",
            return_value=None,
        ),
        patch(
            "ductor_bot.infra.pidlock.force_kill_process_tree",
            return_value=None,
        ),
        patch(
            "ductor_bot.infra.pidlock.list_process_descendants",
            return_value=[],
        ),
    ):
        yield


@pytest.fixture(autouse=True)
def _mock_cli_which() -> object:
    """Prevent tests from failing when CLI tools are not installed.

    Provider constructors call ``shutil.which`` to locate their CLI binary.
    In CI (and Docker) these binaries are typically absent, causing
    FileNotFoundError. Mock all provider ``which`` calls to return a
    plausible path so tests can exercise the logic without real CLIs.
    """
    with (
        patch("ductor_bot.cli.claude_provider.which", return_value="/usr/bin/claude"),
        patch("ductor_bot.cli.codex_provider.which", return_value="/usr/bin/codex"),
        patch("ductor_bot.cli.codex_discovery.which", return_value="/usr/bin/codex"),
        patch("ductor_bot.cli.gemini_utils.which", return_value="/usr/bin/gemini"),
    ):
        yield


@pytest.fixture(autouse=True)
def _mock_cli_auth() -> object:
    """Return all CLIs as authenticated so RulesSelector doesn't delete files.

    Without this, ``deploy_rules()`` removes CLAUDE.md/AGENTS.md/GEMINI.md
    when the corresponding CLI is not authenticated — breaking workspace
    init tests that expect these files to exist after ``init_workspace()``.
    """
    from ductor_bot.cli.auth import AuthResult, AuthStatus

    fake_auth = {
        "claude": AuthResult(provider="claude", status=AuthStatus.AUTHENTICATED),
        "codex": AuthResult(provider="codex", status=AuthStatus.AUTHENTICATED),
        "gemini": AuthResult(provider="gemini", status=AuthStatus.AUTHENTICATED),
    }
    with (
        patch("ductor_bot.cli.auth.check_all_auth", return_value=fake_auth),
        patch(
            "ductor_bot.cli.auth.check_provider_auth",
            side_effect=lambda provider: AuthResult(provider, AuthStatus.AUTHENTICATED),
        ),
    ):
        yield


@pytest.fixture(autouse=True)
def _no_claude_help_probe() -> Iterator[None]:
    """Never run `claude --help` from tests: report the CLI as lacking the snapshot flag.

    Tests that cover the flag set `claude_features._supported` themselves.
    """
    from ductor_bot.cli import claude_features

    claude_features._supported = False
    yield
    claude_features._supported = None


@pytest.fixture(autouse=True)
def _reset_runtime_model_registries() -> Iterator[None]:
    """Isolate dynamically-discovered model registries between tests.

    Booting the orchestrator runs real provider discovery (e.g. ``gemini
    models``) which populates module-level runtime sets. Reset them around
    every test so discovery in one test cannot leak into another.
    """
    reset_gemini_models()
    yield
    reset_gemini_models()
