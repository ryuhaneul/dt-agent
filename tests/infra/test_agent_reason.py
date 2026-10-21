"""Why a sub-agent was excluded: English in logs, the configured language where read."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from ductor_bot.config import AgentConfig
from ductor_bot.i18n import init, t
from ductor_bot.infra.agent_diagnostics import (
    ItemRef,
    Reason,
    create_error,
    joined,
    name_format_error,
    safe_error,
    show_reason,
    start_error,
)
from ductor_bot.infra.feature_deps import format_missing
from ductor_bot.multiagent.supervisor import AgentSupervisor


@pytest.fixture(autouse=True)
def _english_after_each_test() -> Iterator[None]:
    yield
    init("en")


def _validation_error() -> ValidationError:
    try:
        AgentConfig(allowed_user_ids="invalid")  # type: ignore[arg-type]
    except ValidationError as exc:
        return exc
    raise AssertionError("expected a validation error")


def _reasons() -> list[Reason]:
    missing = format_missing(["nio", "nacl.public"], sub="worker")
    return [
        safe_error(3, ValueError("x")),
        safe_error(None, _validation_error()),
        name_format_error(0),
        create_error(OSError("x")),
        start_error(RuntimeError("x")),
        missing,
        format_missing(["nio"], sub=ItemRef(2)),
        format_missing(["slack_bolt"]),
        joined(safe_error(1, ValueError("x")), format_missing(["nio"], sub=ItemRef(1))),
    ]


@pytest.mark.parametrize("index", range(9))
def test_english_rendering_equals_the_log_text(index: int) -> None:
    reason = _reasons()[index]
    init("en")
    assert show_reason(reason) == str(reason)


@pytest.mark.parametrize("index", range(9))
def test_logged_text_is_english_and_korean_rendering_is_not(index: int) -> None:
    reason = _reasons()[index]
    assert not any("가" <= ch <= "힣" for ch in str(reason))
    init("ko")
    shown = show_reason(reason)
    assert any("가" <= ch <= "힣" for ch in shown)
    for token in ("loc=", "type="):  # technical detail stays as it is
        assert (token in shown) == (token in str(reason))


def test_korean_rendering_of_a_joined_reason_keeps_both_parts() -> None:
    reason = joined(safe_error(1, ValueError("x")), format_missing(["nio"], sub=ItemRef(1)))
    init("ko")
    shown = show_reason(reason)
    assert shown.startswith("항목 #1 설정 오류 (ValueError); 기능 matrix")
    assert "서브 '항목 #1'만 제외돼요" in shown


def test_plain_text_is_shown_unchanged() -> None:
    init("ko")
    assert show_reason("supervisor is shutting down") == "supervisor is shutting down"
    assert show_reason("모듈 nio 없음") == "모듈 nio 없음"  # an older or foreign text


def test_security_properties_survive_in_every_language() -> None:
    exc = _validation_error()
    reason = safe_error(0, exc)
    for language in ("en", "ko"):
        init(language)
        shown = show_reason(reason)
        assert "invalid" not in shown  # the offending value is never included
        assert "loc=allowed_user_ids" in shown


# -- where a person reads the reason --------------------------------------------------------------


@pytest.fixture
def supervisor(tmp_path: Path) -> AgentSupervisor:
    sup = AgentSupervisor(AgentConfig(ductor_home=str(tmp_path)))
    (tmp_path / "agents.json").write_text(json.dumps([{"name": "sub1", "transport": "matrix"}]))
    sup._notify_main_agent = AsyncMock()
    sup._stacks["main"] = MagicMock()
    return sup


@pytest.mark.parametrize("action", ["start", "restart"])
@pytest.mark.parametrize(
    ("language", "expected"),
    [("en", "item #0 config error (ValueError)"), ("ko", "항목 #0 설정 오류 (ValueError)")],
)
async def test_agent_start_and_restart_replies_show_the_reason(
    supervisor: AgentSupervisor, action: str, language: str, expected: str
) -> None:
    supervisor._excluded["sub1"] = safe_error(0, ValueError("x"))
    init(language)
    reply = await (
        supervisor.start_agent_by_name("sub1")
        if action == "start"
        else supervisor.restart_agent("sub1")
    )
    assert reply == t("supervisor.not_started", name="sub1", reason=expected)


@pytest.mark.parametrize(
    ("language", "label", "text"),
    [
        ("en", "item #0", "item #0 config error (ValueError)"),
        ("ko", "항목 #0", "항목 #0 설정 오류 (ValueError)"),
    ],
)
async def test_main_notice_shows_the_reason_and_the_logged_text_stays_english(
    supervisor: AgentSupervisor,
    caplog: pytest.LogCaptureFixture,
    language: str,
    label: str,
    text: str,
) -> None:
    init(language)
    with caplog.at_level(logging.ERROR):
        await supervisor._exclude_sub("synthetic:name", safe_error(0, ValueError("x")))
    notice = supervisor._notify_main_agent.await_args.args[0]
    assert notice == t("supervisor.excluded", name=label, reason=text)
    assert "Sub-agent 'item #0' excluded: item #0 config error (ValueError)" in caplog.text
    assert not any("가" <= ch <= "힣" for ch in caplog.text)


async def test_display_name_uses_the_structured_reason_not_a_text_prefix(
    supervisor: AgentSupervisor,
) -> None:
    assert supervisor._display_name("synthetic:name", safe_error(5, ValueError("x"))) == "item #5"
    joined_reason = joined(safe_error(4, ValueError("x")), format_missing(["nio"]))
    assert supervisor._display_name("synthetic:name", joined_reason) == "item #4"
    # A plain text that happens to start like an item label is not read as one.
    assert supervisor._display_name("sub1", "item #9 looks like a label") == "sub1"
    init("ko")
    assert supervisor._display_name("x", safe_error(5, ValueError("x")), shown=True) == "항목 #5"
