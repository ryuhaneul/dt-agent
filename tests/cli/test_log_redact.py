"""Tests for command-log environment redaction."""

from __future__ import annotations

import logging
from collections.abc import Callable

import pytest

from ductor_bot.cli._log_redact import redact_cmd_for_log
from ductor_bot.cli.claude_provider import _log_cmd as log_claude_cmd
from ductor_bot.cli.codex_provider import _log_cmd as log_codex_cmd
from ductor_bot.cli.gemini_provider import _log_cmd as log_gemini_cmd

_FAKE_SECRET = "ghp_FAKESECRET123"
_FAKE_DATABASE_VALUE = "postgres://fake-user:fake-password@example.invalid/db"
_CMD = [
    "command",
    "exec",
    "-e",
    f"GITHUB_TOKEN={_FAKE_SECRET}",
    "-e",
    "DUCTOR_CHAT_ID=42",
    "--model",
    "opus",
]


def test_redact_cmd_for_log_masks_secret_and_preserves_structure() -> None:
    redacted = redact_cmd_for_log(_CMD)
    rendered = " ".join(redacted)

    assert _FAKE_SECRET not in rendered
    assert "GITHUB_TOKEN=***" in rendered
    assert "DUCTOR_CHAT_ID=42" in rendered
    assert redacted[-2:] == ["--model", "opus"]


def test_redact_cmd_for_log_handles_inline_env_forms() -> None:
    redacted = redact_cmd_for_log(
        [
            "command",
            "--env=API_TOKEN=inline-secret",
            "-eINLINE_TOKEN=compact-secret",
            "SERVICE_URL=https://example.test",
        ]
    )

    assert redacted == [
        "command",
        "--env=API_TOKEN=***",
        "-eINLINE_TOKEN=***",
        "SERVICE_URL=***",
    ]


def test_redact_cmd_for_log_masks_env_values_without_parsing_key_syntax() -> None:
    redacted = redact_cmd_for_log(
        [
            "command",
            "-e",
            "api-key=hyphen-secret",
            "--env",
            "api.key=dot-secret",
            "-e",
            "1TOKEN=digit-secret",
            "--env=키=unicode-secret",
            "line-key=first=second\nthird",
            "DUCTOR_CHAT_ID=42",
        ]
    )

    assert redacted == [
        "command",
        "-e",
        "api-key=***",
        "--env",
        "api.key=***",
        "-e",
        "1TOKEN=***",
        "--env=키=***",
        "line-key=***",
        "DUCTOR_CHAT_ID=42",
    ]


def test_redact_cmd_for_log_preserves_non_env_arguments() -> None:
    cmd = [
        "command",
        "--flag",
        "--flag=value",
        "/tmp/file=name",
        "https://example.test/path?query=value",
        "plain prompt",
        "-e",
        "HOST_ONLY_KEY",
    ]

    assert redact_cmd_for_log(cmd) == cmd


def test_redact_cmd_for_log_masks_all_non_whitelisted_dotenv_keys() -> None:
    cmd = [
        "command",
        "exec",
        "-e",
        f"DATABASE_URL={_FAKE_DATABASE_VALUE}",
        "-e",
        "api_key=fake-lowercase-secret",
        "--env=MyToken=fake-mixed-secret",
        "-e",
        "DUCTOR_CHAT_ID=42",
        "--model",
        "opus",
    ]

    redacted = redact_cmd_for_log(cmd)

    assert redacted == [
        "command",
        "exec",
        "-e",
        "DATABASE_URL=***",
        "-e",
        "api_key=***",
        "--env=MyToken=***",
        "-e",
        "DUCTOR_CHAT_ID=42",
        "--model",
        "opus",
    ]


@pytest.mark.parametrize(
    "log_cmd",
    [log_claude_cmd, log_codex_cmd, log_gemini_cmd],
    ids=["claude", "codex", "gemini"],
)
def test_provider_command_logs_redact_secret(
    caplog: pytest.LogCaptureFixture,
    log_cmd: Callable[[list[str]], None],
) -> None:
    with caplog.at_level(logging.INFO):
        log_cmd(["command", "-e", f"api-key={_FAKE_SECRET}"])

    assert _FAKE_SECRET not in caplog.text
    assert "api-key=***" in caplog.text


def test_claude_info_log_masks_embedded_url_credentials(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO):
        log_claude_cmd(["command", "-e", f"DATABASE_URL={_FAKE_DATABASE_VALUE}"])

    assert _FAKE_DATABASE_VALUE not in caplog.text
    assert "DATABASE_URL=***" in caplog.text
