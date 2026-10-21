"""Plain-text rendering of a memory lint JSON report (for the report turn prompt)."""

from __future__ import annotations

from typing import Any

_MAX_PER_CATEGORY = 30


def render_report(report: dict[str, Any]) -> str:
    """Compact list of findings per category (long lists are truncated)."""
    lines: list[str] = []
    issues = report.get("issues", {})
    for key, values in issues.items():
        if not values:
            continue
        lines.append(f"{key.replace('_', ' ')} ({len(values)}):")
        lines += [f"  - {value}" for value in values[:_MAX_PER_CATEGORY]]
        if len(values) > _MAX_PER_CATEGORY:
            lines.append(f"  - ... and {len(values) - _MAX_PER_CATEGORY} more")
    return "\n".join(lines)
