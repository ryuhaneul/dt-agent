"""MemoryLintObserver: weekly lint, report turn only when problems exist."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from ductor_bot.config import AgentConfig, MemoryLintConfig
from ductor_bot.memory_lint import MemoryLintObserver
from ductor_bot.workspace.init import init_workspace
from ductor_bot.workspace.paths import DuctorPaths

WEEK = 168 * 3600


@pytest.fixture
def paths(tmp_path: Path) -> DuctorPaths:
    p = DuctorPaths(ductor_home=tmp_path / "home")
    init_workspace(p)
    return p


@pytest.fixture
def observer(paths: DuctorPaths) -> MemoryLintObserver:
    obs = MemoryLintObserver(AgentConfig(), paths)
    obs.set_report_handler(AsyncMock())
    return obs


def _state(paths: DuctorPaths) -> dict[str, object]:
    return json.loads(paths.memory_lint_state_path.read_text(encoding="utf-8"))


def _handler(obs: MemoryLintObserver) -> AsyncMock:
    assert obs._handler is not None
    return obs._handler  # type: ignore[return-value]


def _break_wiki(paths: DuctorPaths) -> None:
    (paths.workspace / "memory_system" / "bare.md").write_text("no frontmatter [[gone]]\n")


def test_defaults() -> None:
    cfg = MemoryLintConfig()
    assert (cfg.mode, cfg.every_hours, cfg.report) == ("structure", 168, "topic")
    assert not hasattr(cfg, "enabled")
    assert AgentConfig().memory_lint == cfg
    with pytest.raises(ValueError, match="MemoryLintConfig"):
        MemoryLintConfig(report="channel")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="MemoryLintConfig"):
        MemoryLintConfig(every_hours=0)


async def test_first_tick_only_starts_the_clock(
    observer: MemoryLintObserver, paths: DuctorPaths
) -> None:
    _break_wiki(paths)
    assert await observer.tick(now=1000.0) is False
    assert _state(paths)["last_run"] == 1000.0
    _handler(observer).assert_not_called()


async def test_not_due_does_nothing(observer: MemoryLintObserver, paths: DuctorPaths) -> None:
    _break_wiki(paths)
    await observer.tick(now=1000.0)
    assert await observer.tick(now=1000.0 + WEEK - 1) is False
    _handler(observer).assert_not_called()
    assert _state(paths)["last_run"] == 1000.0


async def test_clean_wiki_runs_no_turn_but_resets_the_clock(
    observer: MemoryLintObserver, paths: DuctorPaths
) -> None:
    await observer.tick(now=1000.0)
    assert await observer.tick(now=1000.0 + WEEK) is False
    _handler(observer).assert_not_called()
    assert _state(paths)["last_run"] == 1000.0 + WEEK


async def test_problems_start_exactly_one_report_turn(
    observer: MemoryLintObserver, paths: DuctorPaths
) -> None:
    await observer.tick(now=1000.0)
    _break_wiki(paths)
    assert await observer.tick(now=1000.0 + WEEK) is True
    handler = _handler(observer)
    handler.assert_awaited_once()
    prompt = handler.await_args.args[0]
    assert prompt.startswith("[Weekly memory lint]")
    assert "found 3 problem(s)" in prompt
    assert "missing frontmatter" in prompt
    assert "bare.md" in prompt
    assert "Never delete content without asking" in prompt
    # Not due again until the next period, even though problems remain.
    assert await observer.tick(now=1000.0 + WEEK + 60) is False
    handler.assert_awaited_once()


@pytest.mark.parametrize("update", [{"mode": "off"}, {"report": "off"}])
async def test_disabled_or_off_never_runs(
    observer: MemoryLintObserver, paths: DuctorPaths, update: dict[str, object]
) -> None:
    _break_wiki(paths)
    observer._config.memory_lint = observer._config.memory_lint.model_copy(update=update)
    assert await observer.tick(now=1000.0) is False
    assert await observer.tick(now=1000.0 + 10 * WEEK) is False
    _handler(observer).assert_not_called()
    assert not paths.memory_lint_state_path.exists()


async def test_hot_reload_changes_take_effect_without_restart(
    observer: MemoryLintObserver, paths: DuctorPaths
) -> None:
    await observer.tick(now=1000.0)
    _break_wiki(paths)
    assert await observer.tick(now=1000.0 + 3600) is False  # a week is not over yet
    observer._config.memory_lint = MemoryLintConfig(every_hours=1)  # what ConfigReloader does
    assert await observer.tick(now=1000.0 + 3600) is True


async def test_no_handler_leaves_state_untouched(paths: DuctorPaths) -> None:
    obs = MemoryLintObserver(AgentConfig(), paths)
    assert await obs.tick(now=1000.0) is False
    assert not paths.memory_lint_state_path.exists()


async def test_missing_tool_or_broken_output_is_ignored(
    observer: MemoryLintObserver, paths: DuctorPaths
) -> None:
    await observer.tick(now=1000.0)
    tool = paths.workspace / "tools" / "memory_tools" / "memory_lint.py"
    tool.write_text("print('not json')\n", encoding="utf-8")
    assert await observer.tick(now=1000.0 + WEEK) is False
    tool.unlink()
    assert await observer.tick(now=1000.0 + 2 * WEEK) is False
    _handler(observer).assert_not_called()


def _slow_tool(paths: DuctorPaths, pidfile: Path) -> None:
    tool = paths.workspace / "tools" / "memory_tools" / "memory_lint.py"
    tool.write_text(
        f"import os, time\nopen({str(pidfile)!r}, 'w').write(str(os.getpid()))\ntime.sleep(300)\n",
        encoding="utf-8",
    )


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def _has_content(path: Path) -> bool:
    return path.exists() and bool(path.read_text())


async def _wait_for_file(path: Path) -> None:
    for _ in range(200):
        if await asyncio.to_thread(_has_content, path):
            return
        await asyncio.sleep(0.05)
    raise AssertionError("child never started")


async def test_timeout_kills_and_reaps_the_child(
    observer: MemoryLintObserver,
    paths: DuctorPaths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("ductor_bot.memory_lint.observer._LINT_TIMEOUT", 0.5)
    pidfile = tmp_path / "pid"
    _slow_tool(paths, pidfile)
    assert await observer.run_lint() is None
    assert _gone(int(pidfile.read_text()))  # killed and reaped (a zombie would still answer)
    _handler(observer).assert_not_called()


async def test_cancellation_kills_and_reaps_the_child_and_propagates(
    observer: MemoryLintObserver, paths: DuctorPaths, tmp_path: Path
) -> None:
    pidfile = tmp_path / "pid"
    _slow_tool(paths, pidfile)
    task = asyncio.create_task(observer.run_lint())
    await _wait_for_file(pidfile)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _gone(int(pidfile.read_text()))


async def test_clean_week_costs_no_turn_with_real_clock(
    observer: MemoryLintObserver, paths: DuctorPaths
) -> None:
    paths.memory_lint_state_path.write_text(json.dumps({"last_run": time.time() - WEEK - 5}))
    assert await observer.tick() is False
    _handler(observer).assert_not_called()


def test_report_handler_is_settable_on_the_orchestrator(paths: DuctorPaths) -> None:
    from ductor_bot.orchestrator.core import Orchestrator

    orch = Orchestrator(AgentConfig(), paths)
    handler = AsyncMock()
    orch.set_memory_lint_report_handler(handler)
    assert orch._observers.memory_lint._handler is handler


def test_setting_is_hot_reloadable() -> None:
    from ductor_bot.config_reload import _HOT_RELOADABLE

    assert "memory_lint" in _HOT_RELOADABLE


# -- mode: full / structure / off, legacy ``enabled`` ------------------------------------


def _set(observer: MemoryLintObserver, **update: object) -> None:
    observer._config.memory_lint = MemoryLintConfig(**update)  # type: ignore[arg-type]


async def _due(observer: MemoryLintObserver, now: float = 1000.0) -> float:
    """Start the clock and return a time at which the next tick is due."""
    assert await observer.tick(now=now) is False
    return now + WEEK


def _prompt(observer: MemoryLintObserver, index: int = 0) -> str:
    return str(_handler(observer).await_args_list[index].args[0])


def test_legacy_enabled_maps_to_mode() -> None:
    assert MemoryLintConfig.model_validate({"enabled": False}).mode == "off"
    assert MemoryLintConfig.model_validate({"enabled": True}).mode == "structure"
    assert MemoryLintConfig.model_validate({"enabled": False, "mode": "full"}).mode == "full"
    assert MemoryLintConfig.model_validate({"enabled": True, "mode": "off"}).mode == "off"
    assert MemoryLintConfig.model_validate({}).mode == "structure"
    with pytest.raises(ValueError, match="MemoryLintConfig"):
        MemoryLintConfig(mode="weekly")  # type: ignore[arg-type]


def test_agent_config_accepts_the_legacy_key() -> None:
    cfg = AgentConfig.model_validate({"memory_lint": {"enabled": False, "every_hours": 5}})
    assert (cfg.memory_lint.mode, cfg.memory_lint.every_hours) == ("off", 5)


async def test_structure_problem_free_wiki_starts_no_turn(observer: MemoryLintObserver) -> None:
    _set(observer, mode="structure")
    assert await observer.tick(now=await _due(observer)) is False
    _handler(observer).assert_not_called()


async def test_structure_with_problems_keeps_the_existing_prompt(
    observer: MemoryLintObserver, paths: DuctorPaths
) -> None:
    from ductor_bot.memory_lint.observer import REPORT_PROMPT
    from ductor_bot.memory_lint.render import render_report

    due = await _due(observer)
    _break_wiki(paths)
    assert await observer.tick(now=due) is True
    report = await observer.run_lint()
    assert report is not None
    expected = REPORT_PROMPT.format(count=report["problem_count"], report=render_report(report))
    _handler(observer).assert_awaited_once()
    assert _prompt(observer) == expected
    assert "[Memory lint - full check]" not in _prompt(observer)


async def test_full_with_a_clean_wiki_still_starts_exactly_one_turn(
    observer: MemoryLintObserver,
) -> None:
    _set(observer, mode="full")
    due = await _due(observer)
    assert await observer.tick(now=due) is True
    _handler(observer).assert_awaited_once()
    prompt = _prompt(observer)
    assert prompt.startswith("[Memory lint - full check]")
    assert "no structural problems" in prompt
    assert "Semantic check" in prompt
    assert await observer.tick(now=due + 60) is False  # same period: no second turn
    _handler(observer).assert_awaited_once()


async def test_full_includes_the_structural_findings(
    observer: MemoryLintObserver, paths: DuctorPaths
) -> None:
    _set(observer, mode="full")
    due = await _due(observer)
    _break_wiki(paths)
    assert await observer.tick(now=due) is True
    prompt = _prompt(observer)
    assert "problem(s):" in prompt
    assert "no structural problems" not in prompt


async def test_full_runs_the_turn_even_when_the_script_fails(
    observer: MemoryLintObserver, paths: DuctorPaths
) -> None:
    _set(observer, mode="full")
    due = await _due(observer)
    (paths.workspace / "tools" / "memory_tools" / "memory_lint.py").write_text("print('x')\n")
    assert await observer.tick(now=due) is True
    assert "structural check failed to run" in _prompt(observer)


async def test_structure_stays_silent_when_the_script_fails(
    observer: MemoryLintObserver, paths: DuctorPaths
) -> None:
    due = await _due(observer)
    (paths.workspace / "tools" / "memory_tools" / "memory_lint.py").write_text("print('x')\n")
    assert await observer.tick(now=due) is False
    _handler(observer).assert_not_called()


async def test_off_never_runs_the_script(
    observer: MemoryLintObserver, paths: DuctorPaths, tmp_path: Path
) -> None:
    marker = tmp_path / "ran"
    (paths.workspace / "tools" / "memory_tools" / "memory_lint.py").write_text(
        f"open({str(marker)!r}, 'w').write('x')\n"
    )
    _set(observer, mode="off")
    assert await observer.tick(now=1000.0) is False
    assert await observer.tick(now=1000.0 + 10 * WEEK) is False
    assert not marker.exists()
    _handler(observer).assert_not_called()
    assert not paths.memory_lint_state_path.exists()


async def test_state_is_recorded_before_the_turn_runs(
    observer: MemoryLintObserver, paths: DuctorPaths
) -> None:
    _set(observer, mode="full")
    due = await _due(observer)
    seen: dict[str, object] = {}

    async def failing_turn(_prompt: str) -> None:
        seen.update(_state(paths))
        raise RuntimeError("turn failed")

    observer.set_report_handler(failing_turn)
    with pytest.raises(RuntimeError):
        await observer.tick(now=due)
    assert seen["last_run"] == due
    assert seen["last_stale_month"] == time.strftime("%Y-%m", time.gmtime(due))
    # the failed turn is not retried within the period
    handler = AsyncMock()
    observer.set_report_handler(handler)
    assert await observer.tick(now=due + 3600) is False
    handler.assert_not_called()


def _utc(year: int, month: int, day: int, hour: int = 12) -> float:
    import calendar

    return float(calendar.timegm((year, month, day, hour, 0, 0)))


async def test_stale_check_is_due_once_per_month(
    observer: MemoryLintObserver, paths: DuctorPaths
) -> None:
    _set(observer, mode="full", every_hours=24)
    observer._config.user_timezone = "UTC"
    day = 24 * 3600
    start = _utc(2026, 5, 3)
    await observer.tick(now=start)  # starts the clock
    await observer.tick(now=start + day)  # first run: stale check due
    await observer.tick(now=start + 8 * day)  # same month: skipped
    await observer.tick(now=start + 31 * day)  # June: due again
    flags = ["Perform the stale check" in _prompt(observer, i) for i in range(3)]
    assert flags == [True, False, True]
    assert "already attempted in the first run of this month" in _prompt(observer, 1)
    assert "not performed" not in _prompt(observer, 1)
    assert _state(paths)["last_stale_month"] == "2026-06"


async def test_stale_month_follows_the_configured_timezone(
    observer: MemoryLintObserver, paths: DuctorPaths
) -> None:
    _set(observer, mode="full", every_hours=1)
    observer._config.user_timezone = "Asia/Seoul"
    now = _utc(2026, 5, 31, 16)  # still May in UTC, already June 1st 01:00 in Seoul
    await observer.tick(now=now)
    await observer.tick(now=now + 3600)
    assert _state(paths)["last_stale_month"] == "2026-06"
    observer._config.user_timezone = "UTC"
    assert observer._month(now + 3600) == "2026-05"


async def test_mode_change_applies_on_the_next_tick(observer: MemoryLintObserver) -> None:
    due = await _due(observer)
    assert await observer.tick(now=due) is False  # structure, clean wiki
    _set(observer, mode="full")
    assert await observer.tick(now=due + WEEK) is True
    _set(observer, mode="off")
    assert await observer.tick(now=due + 2 * WEEK) is False


# -- full instructions: seed, user file, fallback --------------------------------------


def _instructions_path(paths: DuctorPaths) -> Path:
    return paths.workspace / "tools" / "memory_tools" / "LINT_FULL.md"


def test_seed_file_is_created_once_and_user_edits_survive(paths: DuctorPaths) -> None:
    from ductor_bot.workspace.init import init_workspace

    seed = _instructions_path(paths)
    assert seed.is_file()
    seed.write_text("my own rules\n", encoding="utf-8")
    init_workspace(paths)
    assert seed.read_text(encoding="utf-8") == "my own rules\n"
    seed.unlink()
    init_workspace(paths)
    assert seed.is_file()
    assert seed.read_text(encoding="utf-8") != "my own rules\n"


def test_builtin_text_equals_the_seed_file(paths: DuctorPaths) -> None:
    from ductor_bot.memory_lint.instructions import DEFAULT_FULL_INSTRUCTIONS

    assert _instructions_path(paths).read_text(encoding="utf-8") == DEFAULT_FULL_INSTRUCTIONS


async def test_user_instructions_are_used(observer: MemoryLintObserver, paths: DuctorPaths) -> None:
    _set(observer, mode="full")
    _instructions_path(paths).write_text("CUSTOM RULE LINE\n", encoding="utf-8")
    assert await observer.tick(now=await _due(observer)) is True
    prompt = _prompt(observer)
    assert prompt.rstrip().endswith("CUSTOM RULE LINE")
    assert "Semantic check" not in prompt


@pytest.mark.parametrize("state", ["missing", "empty", "blank", "unreadable"])
async def test_unusable_instructions_fall_back_to_the_builtin_text(
    observer: MemoryLintObserver,
    paths: DuctorPaths,
    caplog: pytest.LogCaptureFixture,
    state: str,
) -> None:
    _set(observer, mode="full")
    path = _instructions_path(paths)
    if state == "missing":
        path.unlink()
    elif state == "empty":
        path.write_text("")
    elif state == "blank":
        path.write_text("  \n\n")
    else:
        path.write_bytes(b"\xff\xfe\x00bad")
    with caplog.at_level("WARNING"):
        assert await observer.tick(now=await _due(observer)) is True
    assert "Semantic check" in _prompt(observer)
    assert any("built-in instructions" in r.getMessage() for r in caplog.records)


def test_builtin_instructions_hold_no_personal_values() -> None:
    import re

    from ductor_bot.memory_lint import instructions

    text = instructions.DEFAULT_FULL_INSTRUCTIONS

    forbidden = (
        r"/ductor",
        r"site-packages",
        r"agents\.json",
        r"config\.json",
        r"metadata\.json",
        r"opus",
        r"sonnet",
        r"gpt-",
        r"codex",
        r"haiku",
        r"claude-",
        r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b",
        r"telegram",
    )
    for pattern in forbidden:
        assert not re.search(pattern, text, re.IGNORECASE), pattern


def test_builtin_instructions_keep_the_fix_boundary() -> None:
    from ductor_bot.memory_lint import instructions

    text = instructions.DEFAULT_FULL_INSTRUCTIONS

    assert "Never do anything else on your own" in text
    assert "Do not modify or delete any `.md` file beyond what step 4 allows" in text


# -- schedule (cron expression in the agent timezone) ---------------------------------------

MONDAY_9 = "0 9 * * 1"


def _runs(observer: MemoryLintObserver) -> int:
    return _handler(observer).await_count


async def test_schedule_waits_for_the_next_slot_and_runs_each_slot_once(
    observer: MemoryLintObserver,
) -> None:
    _set(observer, mode="full", schedule=MONDAY_9, every_hours=1)
    observer._config.user_timezone = "UTC"
    start = _utc(2026, 10, 5, 8)  # Monday 08:00, before the slot
    assert await observer.tick(now=start) is False  # first sight: clock starts
    assert await observer.tick(now=start + 1800) is False  # 08:30, slot not reached
    assert await observer.tick(now=_utc(2026, 10, 5, 9) + 30) is True  # 09:00:30
    assert await observer.tick(now=_utc(2026, 10, 5, 9) + 600) is False  # same slot
    assert await observer.tick(now=_utc(2026, 10, 6, 9)) is False  # Tuesday
    assert await observer.tick(now=_utc(2026, 10, 12, 9) + 30) is True  # next Monday
    assert _runs(observer) == 2


async def test_schedule_replaces_every_hours(observer: MemoryLintObserver) -> None:
    _set(observer, mode="full", schedule=MONDAY_9, every_hours=1)
    observer._config.user_timezone = "UTC"
    start = _utc(2026, 10, 5, 10)  # Monday after the slot
    await observer.tick(now=start)
    assert await observer.tick(now=start + 5 * 3600) is False  # every_hours would be due
    assert _runs(observer) == 0


async def test_missed_slots_run_once(observer: MemoryLintObserver) -> None:
    _set(observer, mode="full", schedule=MONDAY_9)
    observer._config.user_timezone = "UTC"
    start = _utc(2026, 10, 5, 10)
    await observer.tick(now=start)
    late = _utc(2026, 11, 3, 12)  # four Mondays later
    assert await observer.tick(now=late) is True
    assert await observer.tick(now=late + 600) is False
    assert await observer.tick(now=_utc(2026, 11, 9, 8)) is False  # before the next slot
    assert _runs(observer) == 1


async def test_schedule_uses_the_agent_timezone(observer: MemoryLintObserver) -> None:
    _set(observer, mode="full", schedule=MONDAY_9)
    observer._config.user_timezone = "Asia/Seoul"
    start = _utc(2026, 10, 5, 1)  # Monday 10:00 in Seoul, slot already passed
    await observer.tick(now=start)
    # Monday Oct 12 09:00 Seoul is 00:00 UTC
    assert await observer.tick(now=_utc(2026, 10, 11, 23) + 3599) is False
    assert await observer.tick(now=_utc(2026, 10, 12, 0) + 30) is True
    # the same expression read in UTC is due at 09:00 UTC, not at 09:00 Seoul time
    observer._config.user_timezone = "UTC"
    assert await observer.tick(now=_utc(2026, 10, 12, 3)) is False
    assert await observer.tick(now=_utc(2026, 10, 12, 9) + 30) is True


async def test_without_schedule_every_hours_is_unchanged(observer: MemoryLintObserver) -> None:
    _set(observer, mode="full", every_hours=2)
    await observer.tick(now=1000.0)
    assert await observer.tick(now=1000.0 + 7199) is False
    assert await observer.tick(now=1000.0 + 7200) is True


async def test_invalid_schedule_never_runs_and_warns_once(
    observer: MemoryLintObserver, caplog: pytest.LogCaptureFixture
) -> None:
    _set(observer, mode="full", schedule="not a cron")
    await observer.tick(now=1000.0)
    with caplog.at_level("WARNING"):
        assert await observer.tick(now=1000.0 + 10 * WEEK) is False
        assert await observer.tick(now=1000.0 + 20 * WEEK) is False
    assert _runs(observer) == 0
    assert len([r for r in caplog.records if "invalid schedule" in r.getMessage()]) == 1
    _set(observer, mode="full", schedule=MONDAY_9)  # a valid value works again after reload
    observer._config.user_timezone = "UTC"
    assert await observer.tick(now=1000.0 + 30 * WEEK) is True


def test_blank_values_mean_unset() -> None:
    cfg = MemoryLintConfig(schedule="  ", provider="", model=" ", reasoning_effort="")
    assert (cfg.schedule, cfg.provider, cfg.model, cfg.reasoning_effort) == (None,) * 4
    assert MemoryLintConfig(schedule=" 0 9 * * 1 ").schedule == "0 9 * * 1"
    defaults = MemoryLintConfig()
    assert (defaults.schedule, defaults.provider, defaults.model) == (None, None, None)


# -- a sub never inherits main's memory_lint -------------------------------------------------


async def test_main_full_does_not_make_a_plain_sub_run_a_full_lint(
    paths: DuctorPaths, tmp_path: Path
) -> None:
    from ductor_bot.multiagent.models import SubAgentConfig, merge_sub_agent_config

    main = AgentConfig(memory_lint={"mode": "full"})
    plain = merge_sub_agent_config(main, SubAgentConfig(name="plain"), tmp_path / "plain")
    full = merge_sub_agent_config(
        main,
        SubAgentConfig.model_validate({"name": "full", "memory_lint": {"mode": "full"}}),
        tmp_path / "full",
    )
    off_main = AgentConfig(memory_lint={"mode": "off"})
    sub_of_off = merge_sub_agent_config(off_main, SubAgentConfig(name="s"), tmp_path / "s")
    results = {}
    for label, config in (
        ("main", main),
        ("plain", plain),
        ("full", full),
        ("sub_of_off", sub_of_off),
    ):
        observer = MemoryLintObserver(config, paths)
        observer.set_report_handler(AsyncMock())
        paths.memory_lint_state_path.unlink(missing_ok=True)
        await observer.tick(now=1000.0)  # clean wiki, first sight starts the clock
        results[label] = await observer.tick(now=1000.0 + WEEK)
    # main and the sub with its own override run a turn on a clean wiki; the others do not
    assert results == {"main": True, "plain": False, "full": True, "sub_of_off": False}


# -- prompt rules, logging --------------------------------------------------------------------


_SCOPE_SENTENCE = "Only suggest things that belong to maintaining the memory wiki."
_REREAD_SENTENCE = "actually read the pages you judge again"


async def test_the_full_prompt_carries_both_rules_before_the_user_instructions(
    observer: MemoryLintObserver, paths: DuctorPaths
) -> None:
    _set(observer, mode="full")
    _instructions_path(paths).write_text("CUSTOM RULE LINE\n", encoding="utf-8")
    assert await observer.tick(now=await _due(observer)) is True
    prompt = _prompt(observer)
    assert _REREAD_SENTENCE in prompt
    assert "Do not reuse conclusions of earlier runs as evidence." in prompt
    assert _SCOPE_SENTENCE in prompt
    assert "restoring test or working files" in prompt
    assert prompt.index(_SCOPE_SENTENCE) < prompt.index("## Instructions")
    assert prompt.rstrip().endswith("CUSTOM RULE LINE")  # the user file stays the last block
    assert "restoring test or working files" not in _instructions_path(paths).read_text("utf-8")


async def test_the_structure_prompt_has_the_scope_rule_but_not_the_reread_rule(
    observer: MemoryLintObserver, paths: DuctorPaths
) -> None:
    due = await _due(observer)
    _break_wiki(paths)
    assert await observer.tick(now=due) is True
    prompt = _prompt(observer)
    assert _SCOPE_SENTENCE in prompt
    assert _REREAD_SENTENCE not in prompt


async def test_a_clean_structure_check_logs_zero_problems(
    observer: MemoryLintObserver, caplog: pytest.LogCaptureFixture
) -> None:
    _set(observer, mode="structure")
    due = await _due(observer)
    with caplog.at_level("INFO", logger="ductor_bot.memory_lint.observer"):
        assert await observer.tick(now=due) is False
    assert "Memory lint structure check: 0 problems" in [r.getMessage() for r in caplog.records]


async def test_a_structure_check_with_problems_does_not_log_zero(
    observer: MemoryLintObserver, paths: DuctorPaths, caplog: pytest.LogCaptureFixture
) -> None:
    due = await _due(observer)
    _break_wiki(paths)
    with caplog.at_level("INFO", logger="ductor_bot.memory_lint.observer"):
        await observer.tick(now=due)
    assert all("0 problems" not in r.getMessage() for r in caplog.records)


def test_a_legacy_disabled_key_without_a_mode_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("INFO", logger="ductor_bot.config"):
        cfg = MemoryLintConfig.model_validate({"enabled": False})
    assert cfg.mode == "off"
    messages = [r.getMessage() for r in caplog.records]
    assert sum("read as mode=off" in m for m in messages) == 1


@pytest.mark.parametrize(
    "data",
    [{"enabled": False, "mode": "full"}, {"enabled": True}, {"mode": "off"}, {}],
)
def test_no_legacy_message_when_a_mode_wins_or_nothing_is_disabled(
    data: dict[str, object], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("INFO", logger="ductor_bot.config"):
        MemoryLintConfig.model_validate(data)
    assert all("read as mode=off" not in r.getMessage() for r in caplog.records)


async def test_a_hot_reload_of_the_legacy_key_is_logged_and_an_explicit_mode_wins(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from ductor_bot.config import AgentConfig

    with caplog.at_level("INFO", logger="ductor_bot.config"):
        reloaded = AgentConfig.model_validate({"memory_lint": {"enabled": False}})
        explicit = AgentConfig.model_validate({"memory_lint": {"enabled": False, "mode": "full"}})
    assert reloaded.memory_lint.mode == "off"
    assert explicit.memory_lint.mode == "full"
    assert sum("read as mode=off" in r.getMessage() for r in caplog.records) == 1
