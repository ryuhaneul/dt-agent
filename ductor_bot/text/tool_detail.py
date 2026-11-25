"""Extract and redact display text for built-in tool activity."""

from __future__ import annotations

import re
from typing import TypedDict

from ductor_bot.cli.stream_events import ToolKind, ToolUseEvent
from ductor_bot.infra.secret_vault import SecretVault, VaultError

_DETAIL_KEYS: dict[ToolKind, tuple[str, ...]] = {
    "shell": ("command", "cmd"),
    "file": ("file_path", "path", "notebook_path"),
    "search": ("pattern", "query"),
    "web": ("url", "query"),
}
_DETAIL_LIMIT = 2000
_ANSI = re.compile(
    r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]"  # CSI
    r"|(?:\x1b\]|\x9d)[\s\S]*?(?:\x07|\x1b\\|\x9c|\Z)"  # OSC
    r"|(?:\x1b[PX^_]|[\x90\x98\x9e\x9f])[\s\S]*?(?:\x1b\\|\x9c|\Z)"
    r"|\x1b[ -/]*[0-~]"
)
_CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_TOKEN = re.compile(
    r"(?:gh[pousr]_[A-Za-z0-9]+|github_pat_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]{20,}"
    r"|xox[baprs]-[A-Za-z0-9-]+|(?:AKIA|ASIA)[A-Z0-9]{16,})"
    r"|-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----[\s\S]*?"
    r"-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----"
)
_BEARER = re.compile(r"""(Bearer[ \t]+)[^\s"'`;|&<>]+""", re.IGNORECASE)


class ToolDetail(TypedDict):
    kind: ToolKind
    text: str
    truncated: bool


def tool_detail(event: ToolUseEvent) -> str | None:
    """Select only the fixed parameter keys for the tool's built-in kind."""
    if event.kind is None or event.parameters is None:
        return None
    for key in _DETAIL_KEYS[event.kind]:
        if key in event.parameters:
            value = event.parameters[key]
            return value if isinstance(value, str) else None
    return None


def strip_controls(text: str) -> str:
    """Remove ANSI escape sequences and controls except line feeds and tabs."""
    return _CONTROLS.sub("", _ANSI.sub("", text))


def _mask_tokens(text: str) -> str:
    return _BEARER.sub(r"\g<1>***", _TOKEN.sub("***", text))


async def redact_tool_name(name: str, vault: SecretVault | None) -> str:
    """Return a safe display name for web tool activity."""
    if vault is None:
        return _mask_tokens(name)
    try:
        redacted = await vault.redact(name, strip_controls)
    except (VaultError, OSError):
        return "tool"
    return "tool" if redacted is None else _mask_tokens(redacted)


async def build_detail(event: ToolUseEvent, vault: SecretVault) -> ToolDetail | None:
    """Build a redacted and length-limited detail for an eligible tool event."""
    text = tool_detail(event)
    if text is None or event.kind is None:
        return None
    try:
        text = await vault.redact(text, strip_controls)
    except VaultError:
        return None
    if text is None:
        return None
    text = _mask_tokens(text)
    return {
        "kind": event.kind,
        "text": text[:_DETAIL_LIMIT],
        "truncated": len(text) > _DETAIL_LIMIT,
    }
