"""Safe labels and error metadata for agents.json diagnostics.

Why a sub-agent was excluded or did not start is a :class:`Reason`: a ``str`` holding the
English text (logs, comparisons) that also knows its parts, so the places a person reads it
(:func:`show_reason`) can render it in the configured language.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Self

from pydantic import ValidationError

from ductor_bot.config import (
    AgentConfig,
    ApiConfig,
    CleanupConfig,
    CLIParametersConfig,
    HeartbeatConfig,
    MatrixConfig,
    StreamingConfig,
    WebhookConfig,
)
from ductor_bot.i18n import t

_KNOWN_FIELDS = {"name"} | set().union(
    *(
        set(model.model_fields)
        for model in (
            AgentConfig,
            ApiConfig,
            CleanupConfig,
            CLIParametersConfig,
            HeartbeatConfig,
            MatrixConfig,
            StreamingConfig,
            WebhookConfig,
        )
    )
)


def valid_sub_name(name: str) -> bool:
    return (
        name != "main"
        and re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", name) is not None
        and ".." not in name
    )


def item_label(index: int | None) -> str:
    """English label of an agents.json entry that has no usable name."""
    return f"item #{index}" if index is not None else "unnamed item"


def shown_item_label(index: int | None) -> str:
    """The same label in the configured language."""
    if index is None:
        return t("agent_reason.item_unnamed")
    return t("agent_reason.item", index=index)


@dataclass(frozen=True, slots=True)
class ItemRef:
    """An agents.json entry known only by its position."""

    index: int | None


class Reason(str):
    """English reason text plus what is needed to render it in another language."""

    __slots__ = ("kind", "params", "parts")

    kind: str
    params: dict[str, Any]
    parts: tuple[Reason, ...]

    def __new__(
        cls,
        kind: str,
        text: str,
        params: dict[str, Any] | None = None,
        parts: tuple[Reason, ...] = (),
    ) -> Self:
        obj = super().__new__(cls, text)
        obj.kind = kind
        obj.params = params or {}
        obj.parts = parts
        return obj

    def entry(self) -> tuple[bool, int | None]:
        """``(True, position)`` when the reason is about an entry that has a position to name."""
        if self.params.get("item") and self.params["index"] is not None:
            return True, self.params["index"]
        for part in self.parts:
            found = part.entry()
            if found[0]:
                return found
        return False, None

    def shown(self) -> str:
        """The reason in the configured language."""
        kind, params = self.kind, self.params
        if kind == "text":
            return str(self)
        if kind == "joined":
            return "; ".join(part.shown() for part in self.parts)
        if kind in ("config_error", "name_format"):
            return t(
                f"agent_reason.{kind}",
                **{**params, "item": shown_item_label(params["index"])},
            )
        if kind == "missing":
            details = "; ".join(
                t("agent_reason.feature_missing", feature=feature, module=module)
                for feature, module in params["pairs"]
            )
            text = f"{details}. {t('agent_reason.extras_hint', extras=params['extras'])}"
            sub = params["sub"]
            if sub:
                label = shown_item_label(sub.index) if isinstance(sub, ItemRef) else sub
                text += f"; {t('agent_reason.only_sub', sub=label)}"
            return text
        return t(f"agent_reason.{kind}", **params)


def show_reason(reason: str) -> str:
    """Render *reason* for a person; a plain string (an older or foreign text) is unchanged."""
    return reason.shown() if isinstance(reason, Reason) else str(reason)


def joined(first: str, second: str) -> Reason:
    """Two reasons in one (``first; second``)."""
    parts = tuple(p if isinstance(p, Reason) else Reason("text", str(p)) for p in (first, second))
    return Reason("joined", f"{first}; {second}", parts=parts)


def missing_modules(
    pairs: list[tuple[str, str]], extras: str, sub: str | ItemRef | None = None
) -> Reason:
    """Features that are on while their module is missing, with the image-build hint."""
    details = "; ".join(
        f"feature {feature} is on but module {module} is missing" for feature, module in pairs
    )
    text = (
        f"{details}. image build extras: '{extras}'. Add the feature to DUCTOR_EXTRAS in the "
        "root .env, then run docker compose build"
    )
    if sub:
        label = item_label(sub.index) if isinstance(sub, ItemRef) else sub
        text += f"; only sub-agent '{label}' is excluded"
    return Reason("missing", text, {"pairs": pairs, "extras": extras, "sub": sub})


def name_format_error(index: int | None) -> Reason:
    text = f"{item_label(index)} name format error (ValueError); field name"
    return Reason("name_format", text, {"index": index, "item": True})


def create_error(exc: Exception) -> Reason:
    name = type(exc).__name__
    return Reason("create_error", f"agent creation error ({name})", {"error": name})


def start_error(exc: Exception) -> Reason:
    name = type(exc).__name__
    return Reason("start_error", f"agent start error ({name})", {"error": name})


def safe_error(index: int | None, exc: Exception) -> Reason:
    """Include only exception type and Pydantic field locations/error kinds."""
    detail = []
    if isinstance(exc, ValidationError):
        for error in exc.errors(include_input=False, include_context=False):
            loc = ".".join(
                str(part)
                if isinstance(part, int) or (isinstance(part, str) and part in _KNOWN_FIELDS)
                else "<field>"
                for part in error["loc"]
            )
            detail.append(f"loc={loc or '<root>'} type={error['type']}")
    suffix = "; " + "; ".join(detail) if detail else ""
    name = type(exc).__name__
    return Reason(
        "config_error",
        f"{item_label(index)} config error ({name}){suffix}",
        {"index": index, "item": True, "error": name, "detail": suffix},
    )
