"""Inter-agent session names with lanes: stability of unlaned names, lane rules, limits."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

from ductor_bot.orchestrator.directives import parse_directives
from ductor_bot.session.named import (
    MAX_INTERAGENT_SESSIONS_PER_CHAT,
    NamedSession,
    NamedSessionRegistry,
    interagent_session_name,
    lane_from_session_name,
    normalize_lane,
)


def _legacy_name(sender: str, chat: int = 0, topic: int | None = None) -> str:
    """The pre-lane algorithm, copied verbatim as the reference for unlaned names."""
    if not chat:
        return f"ia-{sender}"
    slug = re.sub(r"[^a-z0-9]+", "-", sender.lower()).strip("-")[:12] or "agent"
    t = f"t{topic}" if topic is not None else "t0"
    digest = hashlib.sha256(f"{sender}\0{chat}\0{topic}".encode()).hexdigest()[:8]
    return f"{f'ia.{slug}.{t}'[:30]}.x{digest}"


@pytest.mark.parametrize("sender", ["main", "Alpha.v2", "a" * 40, "!!!", "x-y_z"])
@pytest.mark.parametrize(
    ("chat", "topic"),
    [(0, None), (0, 5), (123, None), (123, 0), (-1001234567890, 98765), (7, 12345678901234)],
)
def test_unlaned_names_are_byte_identical_to_the_legacy_algorithm(
    sender: str, chat: int, topic: int | None
) -> None:
    assert interagent_session_name(sender, chat, topic) == _legacy_name(sender, chat, topic)
    assert interagent_session_name(sender, chat, topic, "") == _legacy_name(sender, chat, topic)


@pytest.mark.parametrize(("chat", "topic"), [(0, None), (123, None), (123, 4)])
def test_lane_names_have_one_shape(chat: int, topic: int | None) -> None:
    name = interagent_session_name("Main", chat, topic, "build-1")
    assert re.fullmatch(r"ia\.main\.t\d+\.x[0-9a-f]{8}\.lbuild-1", name)
    assert lane_from_session_name(name) == "build-1"


def test_lane_without_source_counts_as_chat_zero_topic_zero() -> None:
    name = interagent_session_name("main", 0, None, "a")
    assert name.startswith("ia.main.t0.x")
    assert name != interagent_session_name("main", 0, None)  # not the legacy name
    digest = hashlib.sha256(b"main\x000\x000").hexdigest()[:8]
    assert name == f"ia.main.t0.x{digest}.la"


def test_lane_names_do_not_collide() -> None:
    """Counter-examples that a naive scheme would merge, with and without a source."""
    names = {
        interagent_session_name("a", 1, None, "x"),  # topic absent
        interagent_session_name("a", 1, 0, "x"),  # topic 0
        interagent_session_name("a", 0, None, "x"),  # no source
        interagent_session_name("a", 1, None),  # no lane
        interagent_session_name("a", 1, None, "y"),  # other lane
        interagent_session_name("b", 1, None, "x"),  # other sender
        interagent_session_name("a", 2, None, "x"),  # other chat
    }
    assert len(names) == 7
    # long sender / topic digits are cut to the same prefix: the hash still separates them
    long_a = interagent_session_name("a" * 20 + "1", 5, 1234567, "x")
    long_b = interagent_session_name("a" * 20 + "2", 5, 1234567, "x")
    assert long_a != long_b


def test_lane_is_normalized_to_lower_case_everywhere() -> None:
    assert interagent_session_name("main", 1, None, "ABC") == interagent_session_name(
        "main", 1, None, "abc"
    )
    assert normalize_lane("Build-2") == "build-2"


@pytest.mark.parametrize("lane", ["a", "ab-1", "0", "a" * 12, "9-9"])
def test_valid_lanes(lane: str) -> None:
    assert normalize_lane(lane) == lane


@pytest.mark.parametrize("lane", ["-a", "a" * 13, "a_b", "a b", "a.b", "é", "a/b", "a\n", " a"])
def test_invalid_lanes_are_rejected(lane: str) -> None:
    with pytest.raises(ValueError, match="Invalid lane"):
        normalize_lane(lane)
    with pytest.raises(ValueError, match="Invalid lane"):
        interagent_session_name("main", 1, None, lane)


def test_no_lane_values() -> None:
    assert normalize_lane(None) == ""
    assert normalize_lane("") == ""
    with pytest.raises(ValueError, match="string"):
        normalize_lane(5)  # type: ignore[arg-type]


def test_unlaned_names_have_no_lane() -> None:
    assert lane_from_session_name(_legacy_name("main", 1, 2)) == ""
    assert lane_from_session_name("ia-main") == ""
    assert lane_from_session_name("redowl") == ""
    assert lane_from_session_name("ia.main.t1.xdeadbeef.lab") == "ab"


def test_end_button_callback_fits_telegram_limit_for_the_longest_name() -> None:
    longest = interagent_session_name("a" * 40, -(10**15), 10**15, "a" * 12)
    assert len(f"nsc:end:{longest}".encode()) <= 64
    assert len(f"nsc:end:{interagent_session_name('x', 1, 1, 'b' * 12)}".encode()) <= 64


def test_follow_up_directive_keeps_the_whole_lane_name() -> None:
    name = interagent_session_name("main", 1, 2, "build-1")
    parsed = parse_directives(f"@{name} continue", frozenset())
    assert name in repr(parsed)


def _session(chat: int, name: str, created: float, status: str = "idle") -> NamedSession:
    return NamedSession(
        name=name,
        chat_id=chat,
        provider="claude",
        model="opus",
        session_id="",
        prompt_preview="",
        status=status,
        created_at=created,
    )


def test_lane_sessions_are_interagent_sessions_for_quota_and_recovery(tmp_path: Path) -> None:
    registry = NamedSessionRegistry(tmp_path / "named.json")
    lane_name = interagent_session_name("main", 1, 2, "x")
    registry.add(_session(9, lane_name, 1.0, status="running"))
    assert registry.active_names(9) == set()  # not part of the user /session quota
    registry2 = NamedSessionRegistry(tmp_path / "named.json")  # restart: running -> idle
    assert registry2.pop_recovered_running(9) == []  # never auto-resumed like user sessions
    assert registry2.get(9, lane_name) is not None


def test_eviction_hook_keeps_protected_lane_sessions(tmp_path: Path) -> None:
    registry = NamedSessionRegistry(tmp_path / "named.json")
    protected = interagent_session_name("main", 1, 1, "keep")
    registry.is_protected = lambda _chat, name: name == protected
    registry.add(_session(9, protected, 0.0))  # the oldest session
    for i in range(MAX_INTERAGENT_SESSIONS_PER_CHAT + 5):
        registry.add(_session(9, interagent_session_name("main", 1, i + 10, f"l{i}"), 1.0 + i))
    assert registry.get(9, protected) is not None
    kept = [s for s in registry.list_active(9) if s.name.startswith("ia.")]
    assert len(kept) <= MAX_INTERAGENT_SESSIONS_PER_CHAT + 1
