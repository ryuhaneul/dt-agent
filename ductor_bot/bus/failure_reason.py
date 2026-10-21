"""Reasons an inter-agent request fails, and how they are shown.

The bus stores and hands out the English reason text. A person reads it in the configured
language: :func:`display_reason` maps the known reasons to translation keys and leaves every
other text (an error message from outside, for example) as it is. Earlier versions stored the
Korean wording, so :func:`display_reason` and :func:`english_reason` read it as well.
"""

from __future__ import annotations

from ductor_bot.i18n import t

QUEUE_WAIT_TIMEOUT = "wait time exceeded, not started"
RECIPIENT_GONE = "recipient agent gone"

_KEYS = {
    QUEUE_WAIT_TIMEOUT: "interagent.queue_wait_timeout",
    RECIPIENT_GONE: "interagent.recipient_gone",
}
# hangul-allowed-begin: reasons stored by earlier versions, read as the English ones
_LEGACY = {
    "대기 시간 초과 — 시작 안 함": QUEUE_WAIT_TIMEOUT,
    "수신 에이전트 없음": RECIPIENT_GONE,
}
# hangul-allowed-end


def _split(error: str) -> tuple[str, str] | None:
    """Return ``(English reason, suffix)`` for a known reason, else ``None``.

    The suffix is what follows the reason: `` (3600s)`` for a wait timeout and ``: 'name'``
    for a missing recipient.
    """
    for known, english in {**{k: k for k in _KEYS}, **_LEGACY}.items():
        if error == known:
            return english, ""
        if error.startswith((f"{known} (", f"{known}: '")):
            return english, error[len(known) :]
    return None


def english_reason(error: str) -> str:
    """The reason in English (an earlier Korean reason becomes the English one)."""
    parts = _split(error)
    return error if parts is None else parts[0] + parts[1]


def display_reason(error: str) -> str:
    """The reason in the configured language; unknown texts are returned unchanged."""
    parts = _split(error)
    if parts is None:
        return error
    return t(_KEYS[parts[0]]) + parts[1]
