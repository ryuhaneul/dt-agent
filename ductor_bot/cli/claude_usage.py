"""Passive Claude usage snapshots, shared across account slots and agents."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ductor_bot.cli.claude_accounts import resolve_ductor_home, state_file_path
from ductor_bot.infra.file_lock import exclusive_file_lock
from ductor_bot.infra.json_store import atomic_json_save

logger = logging.getLogger(__name__)

UsageStatus = Literal["allowed", "allowed_warning", "rejected"]
_WINDOW_NAMES = frozenset({"five_hour", "seven_day"})


class UsageWindow(BaseModel):
    """One server-reported window, with its own capture time in epoch seconds."""

    model_config = ConfigDict(strict=True, allow_inf_nan=False)
    utilization: float = Field(ge=0)
    resets_at: int = Field(ge=0)
    captured_at: int = Field(ge=0)


class SlotUsage(BaseModel):
    """Latest status and independently captured windows for an account slot."""

    model_config = ConfigDict(strict=True)
    status: UsageStatus
    rate_limit_type: str | None
    captured_at: int = Field(ge=0)
    windows: dict[str, UsageWindow] = Field(default_factory=dict)


class UsageSnapshot(BaseModel):
    """Versioned file payload independent of the cooldown registry."""

    model_config = ConfigDict(strict=True)
    version: Literal[1] = 1
    slots: dict[str, SlotUsage] = Field(default_factory=dict)


def usage_file_path(working_dir: str | Path) -> Path:
    """Use the cooldown registry's main-home resolution and sibling directory."""
    return state_file_path(resolve_ductor_home(working_dir)).with_name("_claude-usage.json")


def extract_usage_events(output: str | bytes) -> list[object]:
    """Find usage events in either one stream line or a verbose JSON array."""
    try:
        data = json.loads(output)
    except ValueError:
        return []
    entries = data if isinstance(data, list) else [data]
    return [
        event
        for event in entries
        if isinstance(event, dict) and event.get("type") == "rate_limit_event"
    ]


def _window(raw: object, captured_at: int) -> UsageWindow | None:
    if not isinstance(raw, dict):
        return None
    try:
        return UsageWindow.model_validate(
            {
                "utilization": raw.get("utilization"),
                "resets_at": raw.get("resetsAt"),
                "captured_at": captured_at,
            }
        )
    except ValidationError:
        return None


def _windows(info: dict[str, Any], captured_at: int) -> tuple[dict[str, UsageWindow], bool]:
    unified = "unifiedWindows" in info
    raw_windows: dict[str, Any] = {}
    invalid = False
    if unified:
        raw = info["unifiedWindows"]
        if not isinstance(raw, dict):
            return {}, True
        raw_windows = {name: value for name, value in raw.items() if name in _WINDOW_NAMES}
    elif isinstance(info.get("rateLimitType"), str) and info["rateLimitType"] in _WINDOW_NAMES:
        if "utilization" in info or "resetsAt" in info:
            raw_windows = {info["rateLimitType"]: info}
    else:
        invalid = "utilization" in info or "resetsAt" in info
    windows = {}
    for name, raw_window in raw_windows.items():
        window = _window(raw_window, captured_at)
        if window is None:
            invalid = True
        else:
            windows[name] = window
    return windows, invalid


def parse_usage_event(event: object, *, captured_at: int | None = None) -> SlotUsage | None:
    """Interpret known event fields, retaining partial windows and values above one."""
    if not isinstance(event, dict) or event.get("type") != "rate_limit_event":
        return None
    info = event.get("rate_limit_info")
    if not isinstance(info, dict):
        logger.warning("Ignored malformed Claude usage event")
        return None
    status = info.get("status")
    if not isinstance(status, str) or status not in {"allowed", "allowed_warning", "rejected"}:
        logger.warning("Ignored malformed Claude usage event")
        return None
    captured = int(time.time()) if captured_at is None else captured_at
    windows, invalid = _windows(info, captured)
    rate_type = info.get("rateLimitType")
    if rate_type is not None and (not isinstance(rate_type, str) or rate_type not in _WINDOW_NAMES):
        invalid = True
        rate_type = None
    if invalid:
        logger.warning("Ignored malformed Claude usage values")
    else:
        logger.debug("Parsed Claude usage event; unused fields and windows ignored")
    return SlotUsage(
        status=cast("UsageStatus", status),
        rate_limit_type=rate_type,
        captured_at=captured,
        windows=windows,
    )


class UsageStore:
    """Read atomic snapshots; merge updates while holding a cross-process lock."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> dict[str, SlotUsage]:
        """Read without creating files or modifying snapshot timestamps."""
        try:
            return UsageSnapshot.model_validate_json(self._path.read_text(encoding="utf-8")).slots
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            logger.warning("Claude usage snapshot unavailable: %s", type(exc).__name__)
            return {}

    def record(self, slot: str, event: object, *, captured_at: int | None = None) -> None:
        """Merge only the supplied windows; never touch the cooldown state file."""
        usage = parse_usage_event(event, captured_at=captured_at)
        if usage is None:
            return
        with exclusive_file_lock(self._path):
            slots = self.load()
            previous = slots.get(slot)
            if previous is not None:
                windows = previous.windows.copy()
                for name, window in usage.windows.items():
                    if name not in windows or window.captured_at >= windows[name].captured_at:
                        windows[name] = window
                if usage.captured_at < previous.captured_at:
                    usage = previous.model_copy(update={"windows": windows})
                else:
                    usage.windows = windows
            slots[slot] = usage
            atomic_json_save(self._path, UsageSnapshot(slots=slots).model_dump(mode="json"))
