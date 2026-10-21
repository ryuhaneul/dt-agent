"""Inter-agent failure reasons: English storage, localized display, English agent texts."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from ductor_bot.bus.adapters import build_interagent_injection_prompt, from_interagent_result
from ductor_bot.bus.envelope import Origin
from ductor_bot.bus.failure_reason import (
    QUEUE_WAIT_TIMEOUT,
    RECIPIENT_GONE,
    display_reason,
    english_reason,
)
from ductor_bot.i18n import init, t
from ductor_bot.multiagent.persistence import AsyncRecordStore
from tests.messenger.matrix.test_transport import _env as matrix_env
from tests.messenger.matrix.test_transport import _make_transport as matrix_transport
from tests.messenger.slack.test_transport import _env as slack_env
from tests.messenger.slack.test_transport import _make_transport as slack_transport
from tests.messenger.telegram.test_transport import _env as telegram_env
from tests.messenger.telegram.test_transport import _make_transport as telegram_transport
from tests.multiagent.test_async_persistence import _result, _task
from tests.multiagent.test_lane_plumbing import _result as _lane_result

HANGUL = re.compile("[가-힣]")
# Reasons written by earlier versions (the Korean wording), as the stored records contain them.
LEGACY_TIMEOUT = "대기 시간 초과 — 시작 안 함"
LEGACY_GONE = "수신 에이전트 없음"

KNOWN = [
    (QUEUE_WAIT_TIMEOUT, "interagent.queue_wait_timeout", ""),
    (QUEUE_WAIT_TIMEOUT, "interagent.queue_wait_timeout", " (3600s)"),
    (RECIPIENT_GONE, "interagent.recipient_gone", ""),
    (RECIPIENT_GONE, "interagent.recipient_gone", ": 'worker'"),
    (LEGACY_TIMEOUT, "interagent.queue_wait_timeout", ""),
    (LEGACY_TIMEOUT, "interagent.queue_wait_timeout", " (3600s)"),
    (LEGACY_GONE, "interagent.recipient_gone", ""),
    (LEGACY_GONE, "interagent.recipient_gone", ": 'worker'"),
]
EXTERNAL = [
    "Connection reset by peer",
    "timeout",
    "interrupted by restart",
    "model said: 대기 시간 초과",  # a model text that merely contains it
    f"{QUEUE_WAIT_TIMEOUT} later",
    f"{RECIPIENT_GONE}x",
]


@pytest.fixture(autouse=True)
def _english_after_each_test() -> Iterator[None]:
    yield
    init("en")


def test_the_stored_reasons_are_english() -> None:
    assert not HANGUL.search(QUEUE_WAIT_TIMEOUT + RECIPIENT_GONE)


@pytest.mark.parametrize("language", ["en", "ko"])
@pytest.mark.parametrize(("reason", "key", "suffix"), KNOWN)
def test_known_reasons_are_translated_and_keep_their_suffix(
    language: str, reason: str, key: str, suffix: str
) -> None:
    init(language)
    assert display_reason(reason + suffix) == t(key) + suffix
    assert english_reason(reason + suffix).endswith(suffix)
    assert not HANGUL.search(english_reason(reason + suffix))


def test_korean_display_uses_the_korean_wording() -> None:
    init("ko")
    assert display_reason(QUEUE_WAIT_TIMEOUT + " (60s)") == LEGACY_TIMEOUT + " (60s)"
    assert display_reason(RECIPIENT_GONE + ": 'w'") == LEGACY_GONE + ": 'w'"


@pytest.mark.parametrize("language", ["en", "ko"])
@pytest.mark.parametrize("error", EXTERNAL)
def test_other_texts_are_left_alone(language: str, error: str) -> None:
    init(language)
    assert display_reason(error) == error
    assert english_reason(error) == error


# -- the three transports show the reason on their ``Error:`` line ------------------------------


def _label() -> str:
    """The text in front of the reason in the failure notice (the same in every transport)."""
    probe = t("notice.ia_failed", agent="a", session="", error="@@", request="r")
    return next(line for line in probe.splitlines() if "@@" in line).split("@@")[0]


async def _error_line(transport_name: str, error: str) -> str:
    metadata = {"recipient": "sub-agent", "error": error}
    if transport_name == "telegram":
        transport, _, _ = telegram_transport()
        env = telegram_env(origin=Origin.INTERAGENT, is_error=True, metadata=metadata)
        target = "ductor_bot.messenger.telegram.transport.send_rich_checked_progress"
        sent: Any = (True, 1)
    elif transport_name == "matrix":
        transport, _ = matrix_transport()
        env = matrix_env(origin=Origin.INTERAGENT, is_error=True, metadata=metadata)
        target = "ductor_bot.messenger.matrix.transport.matrix_send_rich_checked_progress"
        sent = (True, "$sent", 1)
    else:
        transport, _ = slack_transport()
        env = slack_env(origin=Origin.INTERAGENT, is_error=True, metadata=metadata)
        target = "ductor_bot.messenger.slack.transport.slack_send_rich_checked_progress"
        sent = (True, "1710000000.1", 1)
    with patch(target, new_callable=AsyncMock, return_value=sent) as send:
        await transport.deliver(env)
    text = send.call_args[0][2]
    (line,) = [ln for ln in text.splitlines() if ln.startswith(_label())]
    return line


@pytest.mark.parametrize("transport_name", ["telegram", "matrix", "slack"])
@pytest.mark.parametrize("language", ["en", "ko"])
@pytest.mark.parametrize(("reason", "key", "suffix"), KNOWN)
async def test_transports_show_the_translated_reason(
    transport_name: str, language: str, reason: str, key: str, suffix: str
) -> None:
    init(language)
    assert await _error_line(transport_name, reason + suffix) == _label() + t(key) + suffix


@pytest.mark.parametrize("transport_name", ["telegram", "matrix", "slack"])
@pytest.mark.parametrize("error", EXTERNAL)
async def test_transports_show_other_errors_as_they_are(transport_name: str, error: str) -> None:
    init("ko")
    assert await _error_line(transport_name, error) == _label() + error


# -- a durable record that holds an earlier Korean reason -------------------------------------------


@pytest.mark.parametrize("language", ["en", "ko"])
async def test_stored_korean_reason_is_shown_in_the_configured_language(
    tmp_path: Path, language: str
) -> None:
    store = AsyncRecordStore(tmp_path)
    task = _task()
    store.create(task)
    stored_error = LEGACY_TIMEOUT + " (3600s)"
    store.update("main", task.task_id, result=_result(task, success=False, error=stored_error))
    restored = store.result_from_record(store.load("main", task.task_id))
    assert restored.error == stored_error  # reading keeps the stored text

    init(language)
    envelope = from_interagent_result(restored, chat_id=42)
    assert envelope.metadata["error"] == stored_error
    line = await _error_line("telegram", str(envelope.metadata["error"]))
    expected = t("interagent.queue_wait_timeout") + " (3600s)"
    assert line == _label() + expected
    # The agent always reads the English reason.
    prompt = build_interagent_injection_prompt(
        restored, agent_name="main", transport_label="Telegram chat"
    )
    assert f"Reason: {QUEUE_WAIT_TIMEOUT} (3600s)" in prompt
    assert not HANGUL.search(prompt)


# -- the text injected into the agent --------------------------------------------------------------


def test_agent_prompt_wording_is_english_and_payload_is_kept() -> None:
    original = "안녕 하세요 요청"  # a user request in Korean
    external = "provider said: 오류 발생"  # an outside error text in Korean
    lost = build_interagent_injection_prompt(
        _lane_result(
            success=False,
            error=external,
            failure_kind="result_lost",
            original_message=original,
            session_name="ia.main.x",
        ),
        agent_name="main",
        transport_label="chat",
    )
    failed = build_interagent_injection_prompt(
        _lane_result(success=False, error=external, original_message=original),
        agent_name="main",
        transport_label="chat",
    )
    for prompt in (lost, failed):
        assert f"Original request: {original}" in prompt
        assert f"Reason: {external}" in prompt
        # Nothing but the two payload texts may contain Hangul.
        assert not HANGUL.search(prompt.replace(original, "").replace(external, ""))
    assert "Session: `ia.main.x`" in lost
    assert "Do not delegate the same request again" in lost
    assert "Decide whether to retry" in failed
