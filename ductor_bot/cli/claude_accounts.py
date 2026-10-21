"""Multi-slot Claude Code account registry, cooldown state, and 429 detection.

Allows ductor to cycle through multiple Claude Code subscriptions when one
slot hits its 5-hour usage cap. Each slot is a directory under
``<ductor_home>/.cli-auth/`` containing its own ``credentials.json`` /
``settings.json``. The subprocess selects a slot by exporting
``CLAUDE_CONFIG_DIR`` per-call.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ductor_bot.infra.file_lock import exclusive_file_lock
from ductor_bot.infra.json_store import atomic_json_save

logger = logging.getLogger(__name__)

DEFAULT_SLOT = "claude"
SHARED_PROJECTS_DIRNAME = "_shared-projects"
STATE_FILENAME = "_claude-slots.json"
CLI_AUTH_DIRNAME = ".cli-auth"
CONTAINER_DUCTOR_MOUNT = "/ductor"


@dataclass(frozen=True, slots=True)
class SlotInfo:
    """Metadata for one Claude Code account slot."""

    name: str
    config_dir: Path


@dataclass(slots=True)
class SlotSnapshot:
    """Cooldown table plus last-used slot. Persisted as JSON."""

    cooldowns: dict[str, float] = field(default_factory=dict)
    last_used: str | None = None


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def resolve_ductor_home(working_dir: str | Path) -> Path:
    """Derive the *main* ductor home used for slot auth + cooldown state.

    Step 1: if the working dir's leaf is ``workspace`` use its parent,
    otherwise use the working dir itself.

    Step 2: if the result is a sub-agent home (``…/agents/<name>``), walk
    up past the ``agents`` subtree so the result is the main ductor home.
    Sub-agents share main's ``.cli-auth/`` for both credentials and
    cooldown state; nothing in the fork populates per-agent slot dirs,
    so resolving them locally would leave every sub-agent claude call in
    a credentials-less slot.

    Other consumers that need the per-agent ductor home (e.g.
    ``_build_subprocess_env`` setting ``DUCTOR_HOME``) compute it
    inline and are not affected by this resolution.
    """
    wd = Path(working_dir).expanduser()
    if wd.name == "workspace":
        wd = wd.parent
    # /ductor/agents/<name>/ → walk up to /ductor
    if wd.parent.name == "agents":
        wd = wd.parent.parent
    return wd


def host_cli_auth_dir(ductor_home: Path) -> Path:
    """Return the host-side ``.cli-auth`` directory."""
    return ductor_home / CLI_AUTH_DIRNAME


def host_slot_config_dir(ductor_home: Path, slot_name: str) -> Path:
    """Return the host path to a slot's config dir."""
    return host_cli_auth_dir(ductor_home) / slot_name


def container_slot_config_dir(slot_name: str) -> str:
    """Return the container path to a slot's config dir."""
    return f"{CONTAINER_DUCTOR_MOUNT}/{CLI_AUTH_DIRNAME}/{slot_name}"


def state_file_path(ductor_home: Path) -> Path:
    """Return the host path to the cooldown state file."""
    return host_cli_auth_dir(ductor_home) / STATE_FILENAME


# ---------------------------------------------------------------------------
# Rate-limit detection
# ---------------------------------------------------------------------------

_RATE_LIMIT_TOKENS = re.compile(
    r"\brate[ _-]?limit|\busage limit|(?<!context )\blimit reached\b|"
    r"\bhit the (?:usage )?limit\b|"
    r"\bhit your (?:(?!context\b)\w+ )?limit\b|"
    r"\breached your (?:(?!context\b)[\w.-]+ ){1,3}limit\b|"
    r"(?<!:)\b429\b(?!:)|(?<!disk )\bquota (?:exceeded|exhausted)\b",
    re.IGNORECASE,
)


def detect_rate_limit(
    *,
    returncode: int | None,
    is_error: bool,
    stderr: str,
    result: str = "",
) -> bool:
    """Return True when the response signals Claude rate-limit / 429.

    The return code must be non-zero. Inspect stderr, and include result text
    only when ``is_error`` is True. Match rate-limit phrases rather than
    benign stderr noise (e.g. "warning: no rate sampling").
    """
    if returncode == 0 or returncode is None:
        return False
    haystack = (stderr or "") + ("\n" + (result or "") if is_error else "")
    if not haystack.strip():
        return False
    if not is_error and not (stderr or result):
        return False
    return bool(_RATE_LIMIT_TOKENS.search(haystack))


# ---------------------------------------------------------------------------
# Reset-time parsing
# ---------------------------------------------------------------------------

_PATTERN_HHMM_TZ = re.compile(
    r"resets?\s+(?:at\s+)?(\d{1,2}):(\d{2})\s*\(([^)]+)\)",
    re.IGNORECASE,
)
_PATTERN_AMPM = re.compile(
    r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*(?:\(([^)]+)\))?",
    re.IGNORECASE,
)
_PATTERN_HHMM = re.compile(
    r"resets?\s+at\s+(\d{1,2}):(\d{2})",
    re.IGNORECASE,
)
_PATTERN_HHMM_BARE = re.compile(
    r"resets?\s+(\d{1,2}):(\d{2})(?!\s*\()",
    re.IGNORECASE,
)
_PATTERN_DATE_AMPM = re.compile(
    r"resets?\s+"
    r"([A-Za-z]{3,9})\s+"  # month name (Apr, January, ...)
    r"(\d{1,2}),?\s+"  # day of month, optional comma
    r"(\d{1,2})"  # hour
    r"(?::(\d{2}))?\s*"  # optional :MM
    r"(am|pm)"  # required am/pm marker
    r"\s*(?:\(([^)]+)\))?",  # optional (TZ)
    re.IGNORECASE,
)

_MONTH_MAP: dict[str, int] = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}


def parse_reset_time(  # noqa: C901
    text: str,
    *,
    now: datetime,
    default_tz: ZoneInfo,
) -> datetime | None:
    """Parse a ``"resets ..."`` timestamp and return an absolute wall-clock.

    Returns ``None`` when no pattern matches or the parsed hour/minute is
    out of range; callers must then surface the raw stderr to the user
    (do not silently fall back to a fixed window).
    """
    date_match = _PATTERN_DATE_AMPM.search(text)
    if date_match:
        month_name = date_match.group(1).lower()
        month = _MONTH_MAP.get(month_name)
        if month is not None:
            day = int(date_match.group(2))
            hour = int(date_match.group(3))
            minute = int(date_match.group(4) or 0)
            marker = date_match.group(5).lower()
            if marker == "pm" and hour != 12:
                hour += 12
            elif marker == "am" and hour == 12:
                hour = 0
            tz_name = date_match.group(6)
            tz = _safe_zone(tz_name.strip(), default_tz) if tz_name else default_tz
            candidate = _next_wall_clock_date(now, month, day, hour, minute, tz)
            if candidate is not None:
                return candidate

    tz_match = _PATTERN_HHMM_TZ.search(text)
    if tz_match:
        hour = int(tz_match.group(1))
        minute = int(tz_match.group(2))
        tz_name = tz_match.group(3).strip()
        tz = _safe_zone(tz_name, default_tz)
        return _next_wall_clock(now, hour, minute, tz)

    ampm_match = _PATTERN_AMPM.search(text)
    if ampm_match:
        hour = int(ampm_match.group(1))
        minute = int(ampm_match.group(2) or 0)
        marker = ampm_match.group(3).lower()
        if marker == "pm" and hour != 12:
            hour += 12
        elif marker == "am" and hour == 12:
            hour = 0
        tz_name = ampm_match.group(4)
        tz = _safe_zone(tz_name.strip(), default_tz) if tz_name else default_tz
        return _next_wall_clock(now, hour, minute, tz)

    hhmm_match = _PATTERN_HHMM.search(text) or _PATTERN_HHMM_BARE.search(text)
    if hhmm_match:
        hour = int(hhmm_match.group(1))
        minute = int(hhmm_match.group(2))
        return _next_wall_clock(now, hour, minute, default_tz)

    return None


def _safe_zone(name: str, fallback: ZoneInfo) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, KeyError):
        return fallback


def _next_wall_clock(now: datetime, hour: int, minute: int, tz: ZoneInfo) -> datetime | None:
    """Return the next wall-clock occurrence of ``hour:minute`` in ``tz``.

    Returns ``None`` when ``hour`` or ``minute`` is out of range so the
    caller treats it as a parse failure (never as an instant cooldown).
    """
    if not (0 <= hour < 24 and 0 <= minute < 60):
        return None
    local_now = now.astimezone(tz)
    candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local_now:
        candidate = candidate + timedelta(days=1)
    return candidate


def _next_wall_clock_date(  # noqa: PLR0913, PLR0917 -- keeps the existing positional call contract
    now: datetime,
    month: int,
    day: int,
    hour: int,
    minute: int,
    tz: ZoneInfo,
) -> datetime | None:
    """Next occurrence of ``month/day hour:minute`` in ``tz`` after ``now``.

    Used for weekly/monthly reset strings like ``resets Apr 24, 8am (UTC)``.
    Returns ``None`` for out-of-range values so the caller can surface the
    raw stderr instead of silently picking a garbage cooldown.
    """
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    if not (0 <= hour < 24 and 0 <= minute < 60):
        return None
    local_now = now.astimezone(tz)
    for year in (local_now.year, local_now.year + 1):
        try:
            candidate = datetime(year, month, day, hour, minute, tzinfo=tz)
        except ValueError:
            return None
        if candidate > local_now:
            return candidate
    return None


# ---------------------------------------------------------------------------
# Slot picker
# ---------------------------------------------------------------------------


def pick_slot(
    priority: list[str],
    cooldowns: dict[str, float],
    *,
    now: float | None = None,
) -> str | None:
    """Return the first priority slot whose cooldown has expired.

    Returns ``None`` when every configured slot is still cooling down.
    """
    current = now if now is not None else time.time()
    for name in priority:
        expiry = cooldowns.get(name, 0.0)
        if expiry <= current:
            return name
    return None


# ---------------------------------------------------------------------------
# State file
# ---------------------------------------------------------------------------


class SlotRegistry:
    """File-backed cooldown registry.

    Mutations share one companion-file lock across processes. Readers see an
    atomic snapshot; read-modify-write callers use ``update``.
    """

    def __init__(self, state_path: Path) -> None:
        self._state_path = state_path

    @property
    def state_path(self) -> Path:
        return self._state_path

    def load(self) -> SlotSnapshot:
        """Load cooldown state; return empty snapshot on any error."""
        if not self._state_path.is_file():
            return SlotSnapshot()
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to read %s: %s", self._state_path, exc)
            return SlotSnapshot()
        if not isinstance(raw, dict):
            return SlotSnapshot()
        cooldowns_raw = raw.get("cooldowns", {})
        cooldowns: dict[str, float] = {}
        if isinstance(cooldowns_raw, dict):
            for key, value in cooldowns_raw.items():
                if isinstance(key, str) and isinstance(value, (int, float)):
                    cooldowns[key] = float(value)
        last_used = raw.get("last_used")
        if not isinstance(last_used, str):
            last_used = None
        return SlotSnapshot(cooldowns=cooldowns, last_used=last_used)

    def update(self, fn: Callable[[SlotSnapshot], None]) -> None:
        """Read, transform, and persist under one file lock."""
        with exclusive_file_lock(self._state_path):
            snapshot = self.load()
            fn(snapshot)
            payload: dict[str, object] = {"cooldowns": snapshot.cooldowns}
            if snapshot.last_used is not None:
                payload["last_used"] = snapshot.last_used
            atomic_json_save(self._state_path, payload)

    def save(self, snapshot: SlotSnapshot) -> None:
        """Atomically replace the snapshot through the same mutation lock."""

        def replace(current: SlotSnapshot) -> None:
            current.cooldowns = dict(snapshot.cooldowns)
            current.last_used = snapshot.last_used

        self.update(replace)


# ---------------------------------------------------------------------------
# Slot auth probing (used by login.sh --list equivalent)
# ---------------------------------------------------------------------------


def slot_is_authenticated(config_dir: Path) -> bool:
    """Return True when ``config_dir`` looks like a logged-in slot."""
    if not config_dir.is_dir():
        return False
    for candidate in ("credentials.json", ".credentials.json"):
        if (config_dir / candidate).is_file():
            return True
    return False
