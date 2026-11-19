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
import math
import os
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ductor_bot.infra.file_lock import exclusive_file_lock
from ductor_bot.infra.json_store import atomic_json_save

logger = logging.getLogger(__name__)

DEFAULT_SLOT = "claude"
SHARED_PROJECTS_DIRNAME = "_shared-projects"
STATE_FILENAME = "_claude-slots.json"
CLI_AUTH_DIRNAME = ".cli-auth"
CONTAINER_DUCTOR_MOUNT = "/ductor"
SUBSCRIPTION_RETRY_INITIAL_SECONDS = 30 * 60
SUBSCRIPTION_RETRY_MAX_SECONDS = 6 * 60 * 60

AuthFailureKind = Literal["login_expired", "subscription_unavailable"]


@dataclass(frozen=True, slots=True)
class AuthFailureDetails:
    """Reason and retry schedule accompanying the credential fingerprint."""

    kind: AuthFailureKind = "login_expired"
    failed_at: float = 0.0
    retry_delay: float = 0.0


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
    auth_failures: dict[str, dict[str, int | None]] = field(default_factory=dict)
    auth_failure_details: dict[str, AuthFailureDetails] = field(default_factory=dict)


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
    r"\bcredit balance is too low\b|"
    r"\bhit the (?:usage )?limit\b|"
    r"\bhit your (?:(?!context\b)[\w-]+ )?limit\b|"
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


# Claude Code 2.1.326 auth error text (installed CLI's embedded JavaScript).
# Match only diagnostics that require sign-in or subscription intervention, not transient refresh,
# network, disk, gateway, MCP, or external API-key errors.
# Allow CLI decorations and error prefixes without backtracking through them.
_AUTH_FAILURE_TOKENS = re.compile(
    r"^(?:[ \t]|\x1b\[[0-9;]*+m)*+"
    r"(?:(?:Error:|API Error:[ \t]++[0-9]++)(?:[ \t]|\x1b\[[0-9;]*+m)*+)?+"
    r"(?:"
    r"Failed to authenticate: this login reached its time limit"
    r"(?: and cannot be renewed\.| · Sign in again to continue)|"
    r"Failed to authenticate: OAuth token revoked\. Please log in again|"
    r"(?:Login expired|Login reached its time limit|OAuth token revoked) · Please run /login|"
    r"Login expired · Run /login to sign in again, or re-authenticate your Anthropic profile|"
    r"Anthropic profile login expired · Re-authenticate your Anthropic profile|"
    r"Authentication required · Sign in again to continue|"
    r"Not logged in · Please run /login|"
    r"Not logged in\. Run claude auth login to authenticate\.|"
    r"Session expired\. Please run /login to sign in again\.|"
    r"(?P<subscription>Claude Code can't use your Claude subscription, usually because a payment is past due\b)"
    r")",
    re.IGNORECASE | re.MULTILINE,
)


def detect_auth_failure(
    *,
    returncode: int | None,
    is_error: bool,
    stderr: str,
    result: str = "",
) -> bool:
    """Recognize Claude login expiry/revocation or unusable subscription on failures.

    As with rate-limit detection, result text is trusted only on error results;
    normal assistant prose about authentication must never disable a slot.
    """
    return (
        detect_auth_failure_kind(
            returncode=returncode, is_error=is_error, stderr=stderr, result=result
        )
        is not None
    )


def detect_auth_failure_kind(
    *, returncode: int | None, is_error: bool, stderr: str, result: str = ""
) -> AuthFailureKind | None:
    """Distinguish permanent login expiry from retryable subscription failure."""
    if returncode in (0, None):
        return None
    haystack = (stderr or "") + ("\n" + (result or "") if is_error else "")
    match = _AUTH_FAILURE_TOKENS.search(haystack)
    if match is None:
        return None
    return "subscription_unavailable" if match.group("subscription") else "login_expired"


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


def _load_auth_failure_details(raw: object) -> AuthFailureDetails | None:
    """Ignore malformed metadata; missing kinds retain legacy login quarantine."""
    if isinstance(raw, dict) and raw.get("kind") == "login_expired":
        return AuthFailureDetails()
    if not isinstance(raw, dict) or raw.get("kind") != "subscription_unavailable":
        return None
    failed_at, retry_delay = raw.get("failed_at"), raw.get("retry_delay")
    if not isinstance(failed_at, (int, float)) or not isinstance(retry_delay, (int, float)):
        return None
    try:
        failed_at, retry_delay = float(failed_at), float(retry_delay)
    except OverflowError:
        return None
    if (
        not math.isfinite(failed_at)
        or failed_at < 0
        or not SUBSCRIPTION_RETRY_INITIAL_SECONDS <= retry_delay <= SUBSCRIPTION_RETRY_MAX_SECONDS
    ):
        return None
    return AuthFailureDetails("subscription_unavailable", failed_at, retry_delay)


class SlotRegistry:
    """File-backed cooldown registry.

    Mutations share one companion-file lock across processes. Readers see an
    atomic snapshot; read-modify-write callers use ``update``.
    """

    def __init__(self, state_path: Path, *, requires_main_config: bool = False) -> None:
        self._state_path = state_path
        # Sub-agents must verify registration against the shared main config.
        self.requires_main_config = requires_main_config

    @property
    def state_path(self) -> Path:
        return self._state_path

    def credential_mtimes(self, slot: str) -> dict[str, int | None]:
        """Fingerprint both credential filenames without reading their contents."""
        mtimes: dict[str, int | None] = {}
        for name in ("credentials.json", ".credentials.json"):
            try:
                mtimes[name] = (self._state_path.parent / slot / name).stat().st_mtime_ns
            except OSError:
                mtimes[name] = None
        return mtimes

    def _read_state(self, *, strict: bool) -> dict[str, object]:
        """Read cooldown state, optionally raising on file I/O errors."""
        if not strict and not self._state_path.is_file():
            return {}
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            if strict and self._state_path.is_symlink():
                raise
            return {}
        except (ValueError, RecursionError, OSError) as exc:
            if strict and isinstance(exc, OSError):
                raise
            logger.warning("Failed to read %s: %s", self._state_path, exc)
            return {}
        if not isinstance(raw, dict):
            return {}
        return raw

    def load(self, *, strict: bool = False) -> SlotSnapshot:
        """Load cooldown state, optionally raising on file I/O errors."""
        raw = self._read_state(strict=strict)
        cooldowns_raw = raw.get("cooldowns", {})
        cooldowns: dict[str, float] = {}
        if isinstance(cooldowns_raw, dict):
            for key, value in cooldowns_raw.items():
                if isinstance(key, str) and isinstance(value, (int, float)):
                    try:
                        cooldown = float(value)
                    except OverflowError:
                        continue
                    if math.isfinite(cooldown):
                        cooldowns[key] = cooldown
        last_used = raw.get("last_used")
        if not isinstance(last_used, str):
            last_used = None
        failures_raw = raw.get("auth_failures", {})
        auth_failures = {
            slot: mtimes
            for slot, mtimes in (failures_raw.items() if isinstance(failures_raw, dict) else [])
            if isinstance(slot, str)
            and isinstance(mtimes, dict)
            and set(mtimes) == {"credentials.json", ".credentials.json"}
            and all(value is None or type(value) is int for value in mtimes.values())
            and not slot_is_api_key(self._state_path.parent / slot)
            and mtimes == self.credential_mtimes(slot)
        }
        details_raw = raw.get("auth_failure_details", {})
        details = {
            slot: detail
            for slot, entry in (details_raw.items() if isinstance(details_raw, dict) else [])
            if slot in auth_failures and (detail := _load_auth_failure_details(entry)) is not None
        }
        return SlotSnapshot(
            cooldowns=cooldowns,
            last_used=last_used,
            auth_failures=auth_failures,
            auth_failure_details=details,
        )

    def update(self, fn: Callable[[SlotSnapshot], None], *, strict: bool = False) -> None:
        """Read, transform, and persist under one file lock."""
        with exclusive_file_lock(self._state_path):
            snapshot = self.load(strict=True) if strict else self.load()
            fn(snapshot)
            payload: dict[str, object] = {"cooldowns": snapshot.cooldowns}
            if snapshot.auth_failures:
                payload["auth_failures"] = snapshot.auth_failures
                details = {
                    slot: asdict(detail)
                    for slot, detail in snapshot.auth_failure_details.items()
                    if slot in snapshot.auth_failures
                }
                if details:
                    payload["auth_failure_details"] = details
            if snapshot.last_used is not None:
                payload["last_used"] = snapshot.last_used
            atomic_json_save(self._state_path, payload)

    def save(self, snapshot: SlotSnapshot) -> None:
        """Atomically replace the snapshot through the same mutation lock."""

        def replace(current: SlotSnapshot) -> None:
            current.cooldowns = dict(snapshot.cooldowns)
            current.last_used = snapshot.last_used
            current.auth_failures = {
                slot: dict(mtimes) for slot, mtimes in snapshot.auth_failures.items()
            }
            current.auth_failure_details = dict(snapshot.auth_failure_details)

        self.update(replace)


# ---------------------------------------------------------------------------
# Slot auth probing (used by login.sh --list equivalent)
# ---------------------------------------------------------------------------


def slot_is_api_key(config_dir: Path) -> bool:
    """Recognize a slot-local API key helper without following settings links."""
    try:
        config_dir = config_dir.resolve()
        settings_path = os.path.normpath(config_dir / "settings.json")
        if not settings_path.startswith(str(config_dir) + os.sep):
            return False
        settings = Path(settings_path)
        if settings.is_symlink() or not settings.is_file():
            return False
        data = json.loads(settings.read_text(encoding="utf-8"))
    except (ValueError, RecursionError, OSError, RuntimeError):
        return False
    helper = data.get("apiKeyHelper") if isinstance(data, dict) else None
    return isinstance(helper, str) and bool(helper)


def slot_is_authenticated(config_dir: Path) -> bool:
    """Return True when ``config_dir`` looks like a logged-in slot."""
    if not config_dir.is_dir():
        return False
    for candidate in ("credentials.json", ".credentials.json"):
        if (config_dir / candidate).is_file():
            return True
    return slot_is_api_key(config_dir)
