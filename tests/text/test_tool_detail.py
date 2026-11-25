"""Tests for safe tool detail extraction and formatting."""

from __future__ import annotations

from pathlib import Path

import pytest

from ductor_bot.cli.stream_events import ToolUseEvent
from ductor_bot.infra.secret_vault import SecretVault
from ductor_bot.text.tool_detail import build_detail, strip_controls, tool_detail


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
        ("key=ghp_" + "A" * 36, "key=***"),
        ("key=github_pat_" + "A" * 36 + "_" + "B" * 24, "key=***"),
        ("key=sk-proj-" + "A" * 36 + "_" + "B" * 24, "key=***"),
        ("key=xoxb-12345-ABCDE", "key=***"),
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
