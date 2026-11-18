"""Validated autocompact values and per-process provider overrides."""

from __future__ import annotations

import json
import logging
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from ductor_bot.config import AutocompactValue

logger = logging.getLogger(__name__)

_VALUE: TypeAdapter[AutocompactValue] = TypeAdapter(AutocompactValue)


def validate_autocompact(value: object) -> AutocompactValue:
    """Reject coercions, unsupported modes and token counts outside 100k-1M."""
    return _VALUE.validate_python(value)


def stored_autocompact(
    value: object, record_id: str, *, inherit: bool = False
) -> AutocompactValue | None:
    """Repair only an invalid stored setting, preserving the automation record."""
    if inherit and value is None:
        return None
    try:
        return validate_autocompact(value)
    except ValueError:
        logger.warning("Invalid stored autocompact for record %s; using fallback", record_id)
        return None if inherit else "default"


def autocompact_override_kwargs(value: AutocompactValue | None) -> dict[str, Any]:
    """Keep existing call contracts unchanged when no automation override was supplied."""
    return {"autocompact": value} if value is not None else {}


def resolve_autocompact(
    topic: AutocompactValue | None,
    global_value: AutocompactValue | None = "default",
) -> AutocompactValue:
    """An explicit topic default stops inheritance, just like any other topic value."""
    value = topic if topic is not None else global_value
    return validate_autocompact("default" if value is None else value)


def autocompact_env(provider: str, value: AutocompactValue) -> dict[str, str]:
    """Claude's process overrides; unsupported providers receive no CLI overrides."""
    if provider != "claude":
        return {}
    if isinstance(value, int):
        return {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": str(value)}
    if value == "off":
        return {"DISABLE_AUTO_COMPACT": "1"}
    return {}


def claude_autocompact_settings(value: AutocompactValue) -> dict[str, Any]:
    """The flag-settings layer overrides conflicting user/project/slot settings.

    Managed policy settings, when present, still take precedence over DT settings.
    Zeroes override lower settings' disabling env values; inherited process values
    are separately removed before launching a token-configured Claude process.
    """
    if isinstance(value, int):
        return {
            "autoCompactEnabled": True,
            "autoCompactWindow": value,
            "env": {
                "CLAUDE_CODE_AUTO_COMPACT_WINDOW": str(value),
                "DISABLE_AUTO_COMPACT": "0",
                "DISABLE_COMPACT": "0",
            },
        }
    if value == "off":
        return {"autoCompactEnabled": False, "env": {"DISABLE_AUTO_COMPACT": "1"}}
    return {}


def _merge_settings(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    env = {**base.get("env", {}), **override.get("env", {})}
    result = {**base, **override}
    if env:
        result["env"] = env
    return result


def claude_settings_use_file(parameters: list[str]) -> bool:
    """Preserve file-backed settings' confidentiality when DT merges its overrides."""
    args = iter(parameters)
    for arg in args:
        if arg == "--settings":
            raw = next(args, "")
        elif arg.startswith("--settings="):
            raw = arg.removeprefix("--settings=")
        else:
            continue
        if not raw.lstrip().startswith("{"):
            return True
    return False


def merge_claude_settings(
    parameters: list[str], value: AutocompactValue, working_dir: Path
) -> list[str]:
    """Fold existing file/JSON --settings into one inline argument, with DT keys winning."""
    override = claude_autocompact_settings(value)
    if not override:
        return list(parameters)
    result: list[str] = []
    settings: dict[str, Any] = {}
    args = iter(parameters)
    for arg in args:
        if arg == "--settings":
            raw = next(args, "")
        elif arg.startswith("--settings="):
            raw = arg.removeprefix("--settings=")
        else:
            result.append(arg)
            continue
        if not raw.lstrip().startswith("{"):
            raw = (working_dir / Path(raw).expanduser()).read_text(encoding="utf-8")
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise TypeError("Claude --settings must contain a JSON object")
        settings = _merge_settings(settings, parsed)
    settings = _merge_settings(settings, override)
    result += ["--settings", json.dumps(settings, separators=(",", ":"))]
    return result


def claude_autocompact_flag_supported(cli: str) -> bool:
    """Use env/settings when a help probe fails, allowing the next call to retry."""
    try:
        return _claude_autocompact_flag_supported(cli)
    except (OSError, subprocess.SubprocessError):
        return False


@lru_cache(maxsize=8)
def _claude_autocompact_flag_supported(cli: str) -> bool:
    """Cache completed --help probes only; never invoke a model."""
    result = subprocess.run(
        [cli, "--help"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    return "--autocompact" in result.stdout + result.stderr


def codex_autocompact_args(value: AutocompactValue) -> list[str]:
    """Codex supports token limits on both exec and exec resume; off is unsupported."""
    if isinstance(value, int):
        return ["-c", f"model_auto_compact_token_limit={value}"]
    return []
