"""Bounded API retries on later user messages; synthetic Bot/CLI only."""

from __future__ import annotations

import asyncio
import json

import pytest
from aiogram.types import ForumTopicEdited

from ductor_bot.cli.types import CLIResponse
from ductor_bot.messenger.telegram import topic_naming
from tests.messenger.telegram.test_topic_naming import drain, implicit, message
from tests.messenger.telegram.test_topic_naming import wired as wired


def saved(orch):
    return json.loads((orch.paths.ductor_home / "dm_topic_naming.json").read_text())


@pytest.mark.parametrize("failure", ["exception", "timeout"])
async def test_api_failure_retries_fresh_title_only_on_next_message(wired, monkeypatch, failure):
    tg, bot, _, orch, cli, _, delivered = wired
    await implicit(tg)
    cli.send.side_effect = [
        CLIResponse(result='{"title":"첫 요청 검토"}'),
        CLIResponse(result='{"title":"다음 요청 검토"}'),
    ]

    async def fail(**kwargs):
        if failure == "timeout":
            await asyncio.Event().wait()
        raise RuntimeError("synthetic sensitive API payload")

    bot.edit_forum_topic.side_effect = fail
    monkeypatch.setattr(topic_naming, "_API_TIMEOUT", 0.01)
    await tg._on_message(message(text="첫 요청 검토"))
    await drain(tg)
    assert delivered == ["reply"]
    assert cli.send.await_count == bot.edit_forum_topic.await_count == 1
    assert saved(orch) == {"topics": {"100:42": "eligible"}, "attempts": {"100:42": 1}}
    assert tg._topic_names.get(100, 42) == "New topic"
    await asyncio.sleep(0)
    assert bot.edit_forum_topic.await_count == 1
    bot.edit_forum_topic.side_effect = None
    await tg._on_message(message(text="다음 요청 검토"))
    await drain(tg)
    assert cli.send.await_count == bot.edit_forum_topic.await_count == 2
    assert bot.edit_forum_topic.await_args.kwargs["name"] == "다음 요청 검토"
    assert "다음 요청 검토" in cli.send.await_args.kwargs["prompt"]
    assert tg._topic_names.get(100, 42) == "다음 요청 검토"
    assert saved(orch) == {"topics": {"100:42": "attempted"}, "attempts": {"100:42": 2}}
    await tg._on_message(message(text="성공 뒤 메시지"))
    await drain(tg)
    assert cli.send.await_count == bot.edit_forum_topic.await_count == 2


@pytest.mark.parametrize("failure", ["exception", "timeout"])
async def test_three_api_failures_exhaust_existing_message_budget(wired, monkeypatch, failure):
    tg, bot, _, orch, cli, _, _ = wired
    await implicit(tg)

    async def fail(**kwargs):
        if failure == "timeout":
            await asyncio.Event().wait()
        raise RuntimeError("synthetic")

    bot.edit_forum_topic.side_effect = fail
    monkeypatch.setattr(topic_naming, "_API_TIMEOUT", 0.01)
    for count in range(1, 4):
        await tg._on_message(message())
        await drain(tg)
        assert saved(orch)["attempts"] == {"100:42": count}
        assert saved(orch)["topics"] == {"100:42": "seen" if count == 3 else "eligible"}
        assert tg._topic_names.get(100, 42) == "New topic"
    await tg._on_message(message())
    await drain(tg)
    assert cli.send.await_count == bot.edit_forum_topic.await_count == 3


@pytest.mark.parametrize(
    "priority", ["edit_after", "disable_after", "edit_during", "disable_during", "closing"]
)
async def test_user_edit_disable_and_closing_win_over_failed_api_retry(wired, priority):
    tg, bot, cfg, _, cli, _, _ = wired
    await implicit(tg)

    async def fail(**kwargs):
        if priority == "edit_during":
            await tg._on_forum_topic_edited(message(edited=ForumTopicEdited(name="User title")))
        elif priority == "disable_during":
            cfg.telegram_dm_topic_auto_name = False
        elif priority == "closing":
            tg._topic_namer._closing = True
        raise RuntimeError("synthetic")

    bot.edit_forum_topic.side_effect = fail
    await tg._on_message(message())
    await drain(tg)
    if priority == "edit_after":
        await tg._on_forum_topic_edited(message(edited=ForumTopicEdited(name="User title")))
    elif priority == "disable_after":
        cfg.telegram_dm_topic_auto_name = False
    await tg._on_message(message())
    await drain(tg)
    cfg.telegram_dm_topic_auto_name = True
    await tg._on_message(message())
    await drain(tg)
    assert cli.send.await_count == bot.edit_forum_topic.await_count == 1
    assert tg._topic_namer._state["100:42"] == (
        "custom"
        if priority.startswith("edit")
        else "attempted"
        if priority == "closing"
        else "seen"
    )


@pytest.mark.parametrize("count", [1, 2, 3])
async def test_failed_api_count_survives_restart_without_restoring_stale_eligibility(wired, count):
    tg, bot, _, orch, cli, _, _ = wired
    await implicit(tg)
    bot.edit_forum_topic.side_effect = RuntimeError("synthetic")
    for _ in range(count):
        await tg._on_message(message())
        await drain(tg)
    assert saved(orch)["attempts"]["100:42"] == count
    tg._topic_namer = None
    await implicit(tg)
    await tg._on_message(message())
    await drain(tg)
    assert cli.send.await_count == bot.edit_forum_topic.await_count == count
    assert tg._topic_namer._attempts["100:42"] == count
    assert tg._topic_namer._state["100:42"] == "seen"
    # The existing constructor discards stale eligibility in memory, without
    # rewriting the old record. Saved message counts remain authoritative.
    assert saved(orch) == {
        "topics": {"100:42": "seen" if count == 3 else "eligible"},
        "attempts": {"100:42": count},
    }



async def test_a_null_result_and_failed_api_calls_share_three_message_opportunities(wired):
    tg, bot, _, orch, cli, _, _ = wired
    await implicit(tg)
    cli.send.side_effect = [
        CLIResponse(result='{"title":null}'),
        CLIResponse(result='{"title":"여행 계획"}'),
        CLIResponse(result='{"title":"여행 계획"}'),
    ]
    bot.edit_forum_topic.side_effect = RuntimeError("synthetic")
    for _ in range(4):
        await tg._on_message(message())
        await drain(tg)
    # The null result names the topic from the message; every failed rename uses an opportunity.
    assert cli.send.await_count == 3 and bot.edit_forum_topic.await_count == 3
    assert saved(orch) == {"topics": {"100:42": "seen"}, "attempts": {"100:42": 3}}


async def test_cache_changes_only_after_retry_api_success(wired):
    tg, bot, _, _, _, _, _ = wired
    await implicit(tg)
    bot.edit_forum_topic.side_effect = RuntimeError("synthetic")
    await tg._on_message(message())
    await drain(tg)
    entered, release = asyncio.Event(), asyncio.Event()

    async def rename(**kwargs):
        entered.set()
        await release.wait()

    bot.edit_forum_topic.side_effect = rename
    await tg._on_message(message())
    await entered.wait()
    assert tg._topic_names.get(100, 42) == "New topic"
    release.set()
    await drain(tg)
    assert tg._topic_names.get(100, 42) == "여행 계획"


async def test_stop_during_retry_api_wait_prevents_another_retry(wired):
    tg, bot, _, orch, cli, _, _ = wired
    await implicit(tg)
    bot.edit_forum_topic.side_effect = RuntimeError("synthetic")
    await tg._on_message(message())
    await drain(tg)
    entered = asyncio.Event()

    async def rename(**kwargs):
        entered.set()
        await asyncio.Event().wait()

    bot.edit_forum_topic.side_effect = rename
    await tg._on_message(message())
    await entered.wait()
    await tg._topic_namer.stop()
    await tg._on_message(message())
    await drain(tg)
    assert cli.send.await_count == bot.edit_forum_topic.await_count == 2
    assert saved(orch) == {"topics": {"100:42": "attempted"}, "attempts": {"100:42": 2}}
    assert tg._topic_names.get(100, 42) == "New topic"
