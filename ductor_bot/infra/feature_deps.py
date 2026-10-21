"""Check optional feature imports before starting configured agents."""

from __future__ import annotations

import importlib
import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from ductor_bot.infra.agent_diagnostics import (
    ItemRef,
    Reason,
    joined,
    missing_modules,
    safe_error,
    valid_sub_name,
)

if TYPE_CHECKING:
    from ductor_bot.config import AgentConfig
    from ductor_bot.workspace.paths import DuctorPaths

logger = logging.getLogger(__name__)

_REQUIRED_MODULES = {
    "matrix": ("nio",),
    "slack": ("slack_bolt", "slack_sdk"),
    "api": ("nacl.public",),
}
_MODULE_FEATURE = {
    module: feature for feature, modules in _REQUIRED_MODULES.items() for module in modules
}


def _missing_for_features(features: set[str]) -> list[str]:
    missing: list[str] = []
    for feature in sorted(features):
        for module in _REQUIRED_MODULES.get(feature, ()):
            try:
                importlib.import_module(module)
            except ImportError:
                missing.append(module)
    return missing


def missing_for(config: AgentConfig) -> list[str]:
    """Return missing import names for enabled transports and the direct API."""
    features = set(config.transports)
    if config.api.enabled:
        features.add("api")
    return _missing_for_features(features)


def format_missing(missing: list[str], *, sub: str | ItemRef | None = None) -> Reason:
    """Why features cannot start: each missing module with its feature, plus the build hint."""
    extras = os.environ.get("DUCTOR_BUILD_EXTRAS", "")
    pairs = [(_MODULE_FEATURE[module], module) for module in dict.fromkeys(missing)]
    return missing_modules(pairs, extras, sub)


def check_feature_deps(main_config: AgentConfig, paths: DuctorPaths) -> dict[str, str]:  # noqa: C901,PLR0912,PLR0915
    """Return excluded sub-agents; exit 3 only for missing main dependencies."""
    main_missing = missing_for(main_config)
    if main_missing:
        logger.error(format_missing(main_missing))
        raise SystemExit(3)

    agents_path = Path(str(paths.ductor_home)) / "agents.json"
    try:
        raw = json.loads(agents_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("Cannot read agents.json for dependency check: %s", type(exc).__name__)
        return {}
    if not isinstance(raw, list):
        logger.warning("agents.json must be a JSON array for dependency check")
        return {}

    from ductor_bot.multiagent.models import SubAgentConfig, merge_sub_agent_config

    excluded: dict[str, str] = {}
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            logger.warning("Ignoring unreadable sub-agent declaration at index %d", index)
            continue
        name = entry["name"]
        declared: set[str] = set()
        if isinstance(entry.get("transport"), str):
            declared.add(entry["transport"])
        transports = entry.get("transports")
        if isinstance(transports, list):
            declared.update(item for item in transports if isinstance(item, str))
        elif transports is not None:
            logger.warning("Unreadable sub-agent transports at index %d", index)
        if isinstance(entry.get("api"), dict) and entry["api"].get("enabled") is True:
            declared.add("api")
        try:
            sub = SubAgentConfig(**entry)
        except Exception as exc:
            # Failed model validation prevents credential-based transport resolution.
            # Preserve declared blocks as dependency hints without reimplementing it.
            for feature in ("matrix", "slack"):
                if isinstance(entry.get(feature), dict):
                    declared.add(feature)
            missing = _missing_for_features(declared)
            excluded[name] = safe_error(index, exc)
            if missing:
                excluded[name] = joined(excluded[name], format_missing(missing, sub=ItemRef(index)))
            continue
        missing = _missing_for_features(declared)
        try:
            agent_home = Path(str(paths.ductor_home)) / "agents" / name
            merged = merge_sub_agent_config(main_config, sub, agent_home)
            missing.extend(module for module in missing_for(merged) if module not in missing)
        except Exception as exc:
            excluded[name] = safe_error(index, exc)
            if missing:
                excluded[name] = joined(excluded[name], format_missing(missing, sub=ItemRef(index)))
            continue
        if missing:
            where = name if valid_sub_name(name) else ItemRef(index)
            excluded[name] = format_missing(missing, sub=where)
    return excluded
