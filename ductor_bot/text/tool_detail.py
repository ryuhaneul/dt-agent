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
_TOKEN_PATTERNS = (
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]+"),
    re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]+"),
    re.compile(r"(?:AKIA|ASIA)[A-Z0-9]{16,}"),
)
_BEARER = re.compile(r"""(Bearer[ \t]+)[^\s"'`;|&<>]+""", re.IGNORECASE)
_PEM_BEGIN = re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")
_PEM_END = re.compile(r"-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----")


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


def _pem_spans(text: str) -> list[tuple[int, int]]:
    spans = []
    cursor = 0
    for opening in _PEM_BEGIN.finditer(text):
        if opening.start() < cursor:
            continue
        closing = _PEM_END.search(text, opening.end())
        cursor = closing.end() if closing else len(text)
        spans.append((opening.start(), cursor))
        if closing is None:
            break
    return spans


async def _redact_activity(text: str, vault: SecretVault | None) -> str | None:
    normalized = strip_controls(text)
    spans = [] if vault is None else await vault.redaction_spans(text, strip_controls)
    if spans is None:
        return None

    for pattern in _TOKEN_PATTERNS:
        spans.extend(match.span() for match in pattern.finditer(normalized))
    spans.extend((match.end(1), match.end()) for match in _BEARER.finditer(normalized))
    spans.extend(_pem_spans(normalized))

    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            first, last = merged[-1]
            merged[-1] = (first, max(last, end))
        else:
            merged.append((start, end))

    parts: list[str] = []
    cursor = 0
    for start, end in merged:
        parts.extend((normalized[cursor:start], "***"))
        cursor = end
    parts.append(normalized[cursor:])
    return "".join(parts)


async def redact_tool_name(name: str, vault: SecretVault | None) -> str:
    """Return a safe display name for web tool activity."""
    try:
        redacted = await _redact_activity(name, vault)
    except (VaultError, OSError):
        return "tool"
    return "tool" if redacted is None else redacted


async def build_detail(event: ToolUseEvent, vault: SecretVault) -> ToolDetail | None:
    """Build a redacted and length-limited detail for an eligible tool event."""
    text = tool_detail(event)
    if text is None or event.kind is None:
        return None
    try:
        text = await _redact_activity(text, vault)
    except VaultError:
        return None
    if text is None or not text.strip():
        return None
    return {
        "kind": event.kind,
        "text": text[:_DETAIL_LIMIT],
        "truncated": len(text) > _DETAIL_LIMIT,
    }
