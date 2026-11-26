"""Tests for safe tool detail extraction and formatting."""

from __future__ import annotations

from pathlib import Path
from time import perf_counter
from unittest.mock import AsyncMock

import pytest

from ductor_bot.cli.stream_events import ToolUseEvent
from ductor_bot.infra.secret_vault import SecretVault
from ductor_bot.text.tool_detail import build_detail, redact_tool_name, strip_controls, tool_detail


@pytest.mark.parametrize(
    ("kind", "parameters", "expected"),
    [
        ("shell", {"command": "first", "cmd": "second"}, "first"),
        ("shell", {"cmd": "second"}, "second"),
        ("file", {"file_path": "first", "path": "second", "notebook_path": "third"}, "first"),
        ("file", {"path": "second", "notebook_path": "third"}, "second"),
        ("file", {"notebook_path": "third"}, "third"),
        ("search", {"pattern": "first", "query": "second"}, "first"),
        ("search", {"query": "second"}, "second"),
        ("web", {"url": "first", "query": "second"}, "first"),
        ("web", {"query": "second"}, "second"),
        ("shell", {"path": "other kind", "extra": "unknown key"}, None),
        ("shell", {"command": ["not text"]}, None),
        ("shell", None, None),
        (None, {"command": "looks like a builtin"}, None),
    ],
)
def test_tool_detail_only_reads_fixed_keys(kind, parameters, expected) -> None:
    event = ToolUseEvent(type="assistant", tool_name="Bash", kind=kind, parameters=parameters)
    assert tool_detail(event) == expected


def test_strip_controls_keeps_line_feeds_and_tabs() -> None:
    text = (
        "a\x00\x01\x08\r\x7f\x85b\n\t"
        "\x1b[31mc\x1b[0m\x9b2K"
        "\x1b]8;;https://example.test\x07d\x1b]8;;\x1b\\"
        "\x1bPignored\x1b\\e"
    )
    assert strip_controls(text) == "ab\n\tcde"


def _shell_event(command: str) -> ToolUseEvent:
    return ToolUseEvent(
        type="assistant", tool_name="Bash", kind="shell", parameters={"command": command}
    )


async def test_build_detail_omits_short_secret(tmp_path: Path) -> None:
    vault = SecretVault(tmp_path / "secrets.env")
    vault.plaintext_path.write_text("TOKEN=abc\n")
    assert await build_detail(_shell_event("cmd abc"), vault) is None


async def test_build_detail_keeps_mask_when_short_secret_matches_only_mask(tmp_path: Path) -> None:
    vault = SecretVault(tmp_path / "secrets.env")
    vault.plaintext_path.write_text("SHORT=**\nLONG=synthetic-private-value\n")
    detail = await build_detail(_shell_event("cmd synthetic-private-value"), vault)
    assert detail == {"kind": "shell", "text": "cmd ***", "truncated": False}


async def test_build_detail_masks_value_joined_by_control_removal(tmp_path: Path) -> None:
    vault = SecretVault(tmp_path / "secrets.env")
    vault.plaintext_path.write_text("TOKEN=abcd\nOTHER=wx\x01yz\n")
    detail = await build_detail(_shell_event("cmd ab\x01cd wxyz"), vault)
    assert detail == {"kind": "shell", "text": "cmd *** ***", "truncated": False}


async def test_build_detail_masks_before_truncating(tmp_path: Path) -> None:
    vault = SecretVault(tmp_path / "secrets.env")
    value = "synthetic-private-boundary-value"
    vault.plaintext_path.write_text(f"TOKEN={value}\n")
    prefix = "x" * 1998
    detail = await build_detail(_shell_event(prefix + value), vault)
    assert detail == {"kind": "shell", "text": prefix + "**", "truncated": True}
    detail = await build_detail(_shell_event(prefix + "yy"), vault)
    assert detail == {"kind": "shell", "text": prefix + "yy", "truncated": False}


async def test_build_detail_omits_detail_if_vault_is_locked(tmp_path: Path) -> None:
    vault = SecretVault(tmp_path / "secrets.env")
    vault.plaintext_path.write_text("TOKEN=synthetic-value\n")
    await vault.setup("synthetic-master-key")
    await vault.lock()
    assert await build_detail(_shell_event("cmd synthetic-value"), vault) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("key=ghp_" + "A" * 20, "key=***"),
        ("key=github_pat_" + "A" * 36 + "_" + "B" * 24, "key=***"),
        ("key=sk-" + "B" * 24, "key=***"),
        ("key=xoxb-" + "1-" + "abc", "key=***"),
        ("key=AKIA" + "A" * 16, "key=***"),
        ("key=ASIA" + "A" * 16, "key=***"),
        ("authorization: Bearer sample.token=value", "authorization: Bearer ***"),
        (
            "-----BEGIN OPENSSH PRIVATE KEY-----\nsynthetic\n-----END OPENSSH PRIVATE KEY-----",
            "***",
        ),
    ],
)
async def test_build_detail_masks_token_patterns(tmp_path: Path, text: str, expected: str) -> None:
    vault = SecretVault(tmp_path / "secrets.env")
    detail = await build_detail(_shell_event(text), vault)
    assert detail == {"kind": "shell", "text": expected, "truncated": False}
    assert await redact_tool_name(text, vault) == expected


@pytest.mark.parametrize(
    "prefix", ["archive_", "archive", "123", "archive-", "archive/", "mcp__x_"]
)
@pytest.mark.parametrize(
    ("token", "replacement"),
    [
        ("ghp_" + "A" * 36 + "XYZ", "***"),
        ("github_pat_" + "A" * 36 + "_XYZ", "***"),
        ("sk-proj-" + "A" * 36 + "_XYZ", "***"),
        ("xoxb-" + "12345-" + "ABCDE-XYZ", "***"),
        ("AKIA" + "A" * 16 + "XYZ", "***"),
        ("ASIA" + "A" * 16 + "XYZ", "***"),
        ("Bearer " + "sample.token" + "=XYZ", "Bearer ***"),
        ("-----BEGIN " + "PRIVATE KEY-----\nsynthetic\n-----END " + "PRIVATE KEY-----", "***"),
    ],
    ids=["github", "github-pat", "openai", "slack", "aws", "aws-temporary", "bearer", "pem"],
)
async def test_activity_detail_masks_embedded_token_patterns(
    tmp_path: Path, prefix: str, token: str, replacement: str
) -> None:
    vault = SecretVault(tmp_path / "secrets.env")
    text = f"key={prefix}{token};tail"
    if token.startswith("sk-") and prefix in {"archive", "123"}:
        replacement = token
    expected = f"key={prefix}{replacement};tail"
    detail = await build_detail(_shell_event(text), vault)
    assert detail == {"kind": "shell", "text": expected, "truncated": False}
    assert await redact_tool_name(text, vault) == expected


async def test_redact_tool_name_masks_tokens_without_a_vault() -> None:
    token = "ghp_" + "A" * 36
    assert await redact_tool_name(f"mcp__{token}", None) == "mcp__***"


async def test_redact_tool_name_uses_default_for_matching_short_value(tmp_path: Path) -> None:
    vault = SecretVault(tmp_path / "secrets.env")
    await vault.set("SHORT", "abc")
    assert await redact_tool_name("mcp__abc", vault) == "tool"


async def test_redact_tool_name_uses_default_on_io_error(tmp_path: Path, monkeypatch) -> None:
    vault = SecretVault(tmp_path / "secrets.env")
    monkeypatch.setattr(vault, "redaction_spans", AsyncMock(side_effect=OSError))
    assert await redact_tool_name("mcp__synthetic_value", vault) == "tool"


@pytest.mark.parametrize(
    ("marker", "text", "expected"),
    [
        (
            "Bearer",
            'curl -H "Authorization: Bearer ' + "abcDEF" + '123456"',
            'curl -H "Authorization: *** ***"',
        ),
        ("Bearer", "auth=Bea\x00rer " + "synthetic" + ".credential", "auth=*** ***"),
        (
            "PRIVATE KEY",
            "-----BEGIN " + "PRIVATE KEY-----\nsynthetic-key-body\n-----END " + "PRIVATE KEY-----",
            "***",
        ),
        (
            "PRIVATE KEY",
            "-----BEGIN "
            + "PRIVATE\x1b[0m KEY-----\nsynthetic-key-body\n-----END "
            + "PRIVATE KEY-----",
            "***",
        ),
    ],
    ids=["bearer", "controlled-bearer", "pem", "controlled-pem"],
)
async def test_activity_masks_credentials_overlapping_registered_markers(
    tmp_path: Path, marker: str, text: str, expected: str
) -> None:
    vault = SecretVault(tmp_path / "secrets.env")
    await vault.set("MARKER", marker)
    detail = await build_detail(_shell_event(text), vault)
    assert detail == {"kind": "shell", "text": expected, "truncated": False}
    assert await redact_tool_name(text, vault) == expected


async def test_activity_masks_adjacent_slack_and_bearer_tokens(tmp_path: Path) -> None:
    vault = SecretVault(tmp_path / "secrets.env")
    slack = "xoxb-" + "12345-" + "ABCDE"
    bearer = "synthetic" + ".credential"
    text = f"archive_{slack}-Bearer {bearer}"
    detail = await build_detail(_shell_event(text), vault)
    assert detail == {"kind": "shell", "text": "archive_*** ***", "truncated": False}
    assert await redact_tool_name(text, vault) == "archive_*** ***"


@pytest.mark.parametrize("count", [2, 3])
async def test_activity_masks_overlapping_github_tokens(tmp_path: Path, count: int) -> None:
    vault = SecretVault(tmp_path / "secrets.env")
    text = "".join("ghp_" + chr(ord("A") + index) * 36 for index in range(count))
    assert await build_detail(_shell_event(text), vault) == {
        "kind": "shell",
        "text": "***",
        "truncated": False,
    }
    assert await redact_tool_name(text, vault) == "***"


@pytest.mark.parametrize("count", [2, 3])
async def test_activity_masks_overlapping_bearer_tokens(tmp_path: Path, count: int) -> None:
    vault = SecretVault(tmp_path / "secrets.env")
    text = "Bearer " * count + "synthetic" + ".credential"
    expected = "Bearer" + " ***" * count
    assert await build_detail(_shell_event(text), vault) == {
        "kind": "shell",
        "text": expected,
        "truncated": False,
    }
    assert await redact_tool_name(text, vault) == expected


@pytest.mark.parametrize(
    "text",
    [
        "ghp_" + "A" * 36 + "xoxb-" + "12345-ABCDE",
        "AKIA" + "A" * 16 + "ghp_" + "B" * 36,
    ],
    ids=["github-slack", "aws-github"],
)
async def test_activity_masks_touching_token_formats(tmp_path: Path, text: str) -> None:
    vault = SecretVault(tmp_path / "secrets.env")
    assert await build_detail(_shell_event(text), vault) == {
        "kind": "shell",
        "text": "***",
        "truncated": False,
    }
    assert await redact_tool_name(text, vault) == "***"


@pytest.mark.parametrize(
    ("prefix", "unit"),
    [("", "sk-"), ("ghp_", "A"), ("", "Bearer "), ("", "ghp_" + "A" * 36)],
    ids=["sk-prefixes", "long-github-token", "bearer-markers", "github-tokens"],
)
async def test_activity_large_token_input(tmp_path: Path, prefix: str, unit: str) -> None:
    vault = SecretVault(tmp_path / "secrets.env")
    size = 1024 * 1024
    text = (prefix + unit * (size // len(unit) + 1))[:size]
    event = _shell_event(text)
    start = perf_counter()
    assert await build_detail(event, vault) is not None
    assert perf_counter() - start < 2


async def test_activity_checks_short_values_before_masking_token_shapes(tmp_path: Path) -> None:
    vault = SecretVault(tmp_path / "secrets.env")
    await vault.set("SHORT", "abc")
    token = "sk-" + "A" * 20 + "abc"
    assert await build_detail(_shell_event(token), vault) is None
    assert await redact_tool_name(token, vault) == "tool"


@pytest.mark.parametrize(
    ("prefix", "value"),
    [
        ("export CREDS=", "AKIA" + "ABCDEFGHIJKLMNOP" + ":wJalrSYNTHETIC"),
        ("git clone ", "https://oauth2:" + "ghp_" + "A" * 20 + "@git.example.test/team/repo"),
        ("login --password ", "Hunter2Correct." + "sk-" + "B" * 24),
        ("", "abcdefgh" * 2),
    ],
    ids=["aws-pair", "git-url", "password", "adjacent-values"],
)
async def test_activity_masks_whole_registered_values(
    tmp_path: Path, prefix: str, value: str
) -> None:
    vault = SecretVault(tmp_path / "secrets.env")
    await vault.set("VALUE", value if prefix else "abcdefgh")
    text = prefix + value
    expected = prefix + "***"
    assert await build_detail(_shell_event(text), vault) == {
        "kind": "shell",
        "text": expected,
        "truncated": False,
    }
    assert await redact_tool_name(text, vault) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("task-2026-11-25-refresh-topic-list", "task-2026-11-25-refresh-topic-list"),
        ("risk-assessment-report-2026-final", "risk-assessment-report-2026-final"),
        ("flask-application-factory-config", "flask-application-factory-config"),
        ("desk-booking-service-backend", "desk-booking-service-backend"),
        ("laughs_at_everything", "laughs_at_everything"),
        ("coughs_loudly", "coughs_loudly"),
        ("K=sk-" + "ant-api03-" + "abcDEF_1234567890", "K=***"),
        ("archive_sk-" + "A" * 24, "archive_***"),
        ("path/sk-" + "A" * 24, "path/***"),
        ("-----BEGIN " + "PRIVATE KEY-----\nsynthetic-key-body", "***"),
    ],
)
async def test_activity_token_boundaries_and_unclosed_key(
    tmp_path: Path, text: str, expected: str
) -> None:
    vault = SecretVault(tmp_path / "secrets.env")
    assert await build_detail(_shell_event(text), vault) == {
        "kind": "shell",
        "text": expected,
        "truncated": False,
    }
    assert await redact_tool_name(text, vault) == expected


_PEM_OPENING = "-----BEGIN " + "PRIVATE KEY-----\n"
_PEM_CLOSING = "\n-----END " + "PRIVATE KEY-----"


@pytest.mark.parametrize("count", [2, 3])
async def test_activity_masks_keys_with_shared_markers(tmp_path: Path, count: int) -> None:
    vault = SecretVault(tmp_path / "secrets.env")
    blocks = [_PEM_OPENING + f"synthetic-key-body-{index}" + _PEM_CLOSING for index in range(count)]
    text = blocks[0] + "".join(block[5:] for block in blocks[1:])
    assert await build_detail(_shell_event(text), vault) == {
        "kind": "shell",
        "text": "***",
        "truncated": False,
    }
    assert await redact_tool_name(text, vault) == "***"


@pytest.mark.parametrize("closed", [True, False], ids=["closed", "unclosed"])
async def test_activity_masks_keys_separated_by_newline(tmp_path: Path, closed: bool) -> None:
    vault = SecretVault(tmp_path / "secrets.env")
    first = _PEM_OPENING + "synthetic-first-key-body" + _PEM_CLOSING
    second = _PEM_OPENING + "synthetic-second-key-body"
    if closed:
        second += _PEM_CLOSING
    text = first + "\n" + second
    assert await build_detail(_shell_event(text), vault) == {
        "kind": "shell",
        "text": "***\n***",
        "truncated": False,
    }
    assert await redact_tool_name(text, vault) == "***\n***"


@pytest.mark.parametrize(
    ("prefix", "unit"),
    [
        ("", _PEM_OPENING),
        ("", _PEM_OPENING + "synthetic-key-body" + _PEM_CLOSING),
        ("-----", _PEM_OPENING[5:] + "synthetic-key-body" + _PEM_CLOSING),
    ],
    ids=["unclosed-markers", "closed-blocks", "shared-markers"],
)
async def test_activity_large_key_input(tmp_path: Path, prefix: str, unit: str) -> None:
    vault = SecretVault(tmp_path / "secrets.env")
    text = prefix + unit * (1024 * 1024 // len(unit) + 1)
    event = _shell_event(text)
    start = perf_counter()
    assert await build_detail(event, vault) == {
        "kind": "shell",
        "text": "***",
        "truncated": False,
    }
    assert perf_counter() - start < 2


@pytest.mark.parametrize("text", ["", "\x1b[0m\x00", "  \n"])
async def test_activity_omits_empty_details(tmp_path: Path, text: str) -> None:
    vault = SecretVault(tmp_path / "secrets.env")
    assert await build_detail(_shell_event(text), vault) is None
