"""Subject-only topic titles from reception, with synthetic Bot/CLI only."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from ductor_bot.cli.types import CLIResponse
from ductor_bot.messenger.telegram import topic_naming
from tests.messenger.telegram.test_topic_naming import drain, implicit, message
from tests.messenger.telegram.test_topic_naming import wired  # noqa: F401


async def test_title_starts_while_conversation_reply_is_still_blocked(wired):
    tg, bot, _, _, cli, _, delivered = wired
    started, reply_release = asyncio.Event(), asyncio.Event()

    async def reply(*args, **kwargs):
        await reply_release.wait()
        delivered.append("reply")
        return "시스템 점검 완료, 이상 없음"

    async def title(**kwargs):
        started.set()
        return CLIResponse(result='{"title":"시스템 점검"}')

    tg._handle_non_streaming.side_effect = reply
    cli.send.side_effect = title
    await implicit(tg)
    turn = asyncio.create_task(tg._on_message(message(text="시스템 점검 해봐")))
    await asyncio.wait_for(started.wait(), 1)
    assert delivered == [] and not turn.done()
    await drain(tg)
    assert bot.edit_forum_topic.await_args.kwargs["name"] == "시스템 점검"
    prompt = cli.send.await_args.kwargs["prompt"]
    assert "First reply" not in prompt and "시스템 점검 완료, 이상 없음" not in prompt
    reply_release.set()
    await turn


async def test_generation_starts_before_busy_session_lock_and_dedup_is_once(wired):
    tg, bot, _, _, cli, factory, _ = wired
    await implicit(tg)
    cli.send.return_value = CLIResponse(result='{"title":"여행 계획"}')
    lock = tg._sequential.get_lock((100, 42))
    await lock.acquire()
    bot.send_message = AsyncMock()
    bot.delete_message = AsyncMock()

    async def handler(event, data):
        await tg._on_message(event, **data)

    task = asyncio.create_task(tg._sequential(handler, message(), {}))
    for _ in range(100):
        if factory.called:
            break
        await asyncio.sleep(0.001)
    assert factory.called and not task.done()
    lock.release()
    await task
    await drain(tg)
    await tg._sequential(handler, message(), {})
    assert factory.call_count == 1 and bot.edit_forum_topic.await_count == 1


async def test_greeting_then_substantive_message_retries_without_reply_content(wired):
    tg, bot, _, orch, cli, factory, _ = wired
    await implicit(tg)
    await tg._on_message(message(text="안녕"))
    await drain(tg)
    factory.assert_not_called()
    bot.edit_forum_topic.assert_not_awaited()
    saved = json.loads((orch.paths.ductor_home / "dm_topic_naming.json").read_text())
    assert saved["attempts"]["100:42"] == 1 and saved["topics"]["100:42"] == "eligible"
    cli.send.return_value = CLIResponse(result='{"title":"시스템 점검"}')
    await tg._on_message(message(text="시스템 점검 해봐"))
    await drain(tg)
    assert bot.edit_forum_topic.await_args.kwargs["name"] == "시스템 점검"
    assert tg._topic_namer._attempts["100:42"] == 2


@pytest.mark.parametrize(
    "bad",
    [
        '{"title":null}',
        '{"title":"시스템 점검 완료, 이상 없음"}',
        "I will check your system and report the results as soon as possible.",
        '{"title":"Postgres pool 문제"}',
        '{"title":"Title: subject"}',
        '{"title":"System check successful"}',
        '{"title":"시스템 점검 완\u200b료"}',
        '{"title":"A very long explanation about what I did while checking"}',
    ],
)
async def test_rejected_titles_are_replaced_by_the_message_prefix(wired, bad):
    tg, bot, _, orch, cli, _, _ = wired
    await implicit(tg)
    cli.send.return_value = CLIResponse(result=bad)
    for _ in range(4):
        await tg._on_message(message(text="시스템 점검 해봐"))
        await drain(tg)
    # No usable model title: the start of the user's message names the topic, once.
    assert cli.send.await_count == 1
    bot.edit_forum_topic.assert_awaited_once_with(
        chat_id=100, message_thread_id=42, name="시스템 점검 해봐"
    )
    saved = json.loads((orch.paths.ductor_home / "dm_topic_naming.json").read_text())
    assert saved["topics"]["100:42"] == "attempted"
    tg._topic_namer = None
    await implicit(tg)
    await tg._on_message(message(text="시스템 점검 해봐"))
    await drain(tg)
    assert cli.send.await_count == 1


async def test_retry_count_survives_restart_without_trusting_stale_implicit_name(wired):
    tg, bot, _, orch, _, factory, _ = wired
    await implicit(tg)
    await tg._on_message(message(text="hello"))
    tg._topic_namer = None
    await implicit(tg)  # A replay cannot establish that a user did not rename while offline.
    await tg._on_message(message(text="시스템 점검 해봐"))
    await drain(tg)
    factory.assert_not_called()
    bot.edit_forum_topic.assert_not_awaited()
    assert tg._topic_namer._attempts["100:42"] == 1
    assert (
        json.loads((orch.paths.ductor_home / "dm_topic_naming.json").read_text())["attempts"][
            "100:42"
        ]
        == 1
    )


@pytest.mark.parametrize("text", ["안녕", "hello", "thanks", "감사합니다"])
async def test_three_topicless_messages_stop_without_calling_cli(wired, text):
    tg, bot, _, _, _, factory, _ = wired
    await implicit(tg)
    for _ in range(4):
        await tg._on_message(message(text=text))
    factory.assert_not_called()
    bot.edit_forum_topic.assert_not_awaited()
    assert tg._topic_namer._attempts["100:42"] == 3


@pytest.mark.parametrize(
    "output,text,expected",
    [
        ('{"title":"시스템 점검"}', "시스템 점검 해봐", "시스템 점검"),
        ('{"title":"v2.4 app.py 검토"}', "v2.4 app.py 검토해 줘", "v2.4 app.py 검토"),
        ('{"title":"complete.py 검토"}', "complete.py 검토", "complete.py 검토"),
        ('{"title":"성공.md 검토"}', "성공.md 검토", "성공.md 검토"),
        ('{"title":"Postgres pool 문제"}', "Postgres pool 문제 조사", "Postgres pool 문제"),
        ('{"title":"여행 계획"}', "여행 계획 세워줘", "여행 계획"),
        ('{"title":"파일 목록 조사"}', "파일 목록을 조사해 줘", "파일 목록 조사"),
        ('{"title":"백업 파일 점검"}', "백업 파일 점검해 줘", "백업 파일 점검"),
        ('{"title":"문서 형식 검토"}', "문서 형식을 검토해 줘", "문서 형식 검토"),
        ('{"title":"README 번역"}', "README 번역해 줘", "README 번역"),
        ('{"title":"VSC 설정"}', "여행 계획 세워줘", None),
        ('{"title":"VSC 설정"}', "VSC 설정 바꿔줘", "VSC 설정"),
        ('{"title":null}', "hello", None),
        ('{"title":"시스템 점검"}', "여행 계획 세워줘", None),
        ('{"title":12}', "request", None),
        ('```{"title":"abc"}```', "request", None),
        ('{"title":"Bearer\\u200b synthetic"}', "request", ""),
        ('{"title":"name","answer":"answer"}', "request", None),
    ],
)
def test_json_subject_validation_and_supported_example_echo(output, text, expected):
    assert topic_naming.generated_topic_title(output, text) == expected


async def test_rejected_result_title_is_replaced_by_the_message_prefix(wired):
    tg, bot, _, _, cli, _, _ = wired
    await implicit(tg)
    cli.send.side_effect = [
        CLIResponse(result='{"title":"시스템 점검 완료, 이상 없음"}'),
        CLIResponse(result='{"title":"시스템 점검"}'),
    ]
    await tg._on_message(message(text="시스템 점검 해봐"))
    await drain(tg)
    # The result-like title is replaced at once by the start of the message; naming ends.
    bot.edit_forum_topic.assert_awaited_once()
    assert bot.edit_forum_topic.await_args.kwargs["name"] == "시스템 점검 해봐"
    await tg._on_message(message(text="시스템 점검에 집중해"))
    await drain(tg)
    bot.edit_forum_topic.assert_awaited_once()
    assert cli.send.await_count == 1


async def test_pending_generation_counts_messages_without_parallel_title_calls(wired):
    tg, bot, _, _, cli, _, _ = wired
    started, release = asyncio.Event(), asyncio.Event()

    async def title(**kwargs):
        started.set()
        await release.wait()
        return CLIResponse(result='{"title":null}')

    cli.send.side_effect = title
    await implicit(tg)
    await tg._on_message(message())
    await started.wait()
    await tg._on_message(message())
    await tg._on_message(message())
    assert tg._topic_namer._attempts["100:42"] == 3
    assert cli.send.await_count == 1
    release.set()
    await drain(tg)
    await tg._on_message(message())
    assert cli.send.await_count == 1
    bot.edit_forum_topic.assert_awaited_once()  # the null result named it from the message


async def test_service_creation_is_observed_before_lock_wait(wired):
    from aiogram.types import ForumTopicCreated

    tg, bot, _, _, cli, _, _ = wired
    lock = tg._sequential.get_lock((100, 42))
    await lock.acquire()
    cli.send.return_value = CLIResponse(result='{"title":"시스템 점검"}')
    bot.send_message = AsyncMock()
    bot.delete_message = AsyncMock()

    async def handler(event, data):
        if event.forum_topic_created:
            await tg._on_forum_topic_created(event)
        else:
            await tg._on_message(event, **data)

    event = message(
        text=None, created=ForumTopicCreated(name="New", icon_color=0, is_name_implicit=True)
    )
    creation = asyncio.create_task(tg._sequential(handler, event, {}))
    await asyncio.sleep(0.01)
    assert tg._topic_namer._state["100:42"] == "eligible"
    incoming = message(text="시스템 점검 해봐").model_copy(update={"message_id": 8})
    turn = asyncio.create_task(tg._sequential(handler, incoming, {}))
    await asyncio.sleep(0.01)
    await drain(tg)
    assert bot.edit_forum_topic.await_args.kwargs["name"] == "시스템 점검"
    assert not creation.done() and not turn.done()
    lock.release()
    await asyncio.gather(creation, turn)
    assert tg._topic_namer._state["100:42"] == "attempted"


async def test_user_edit_is_observed_before_lock_wait_and_wins_pending_generation(wired):
    from aiogram.types import ForumTopicEdited

    tg, bot, _, _, cli, _, _ = wired
    entered, release = asyncio.Event(), asyncio.Event()

    async def title(**kwargs):
        entered.set()
        await release.wait()
        return CLIResponse(result='{"title":"시스템 점검"}')

    cli.send.side_effect = title
    await implicit(tg)
    await tg._on_message(message(text="시스템 점검 해봐"))
    await entered.wait()
    lock = tg._sequential.get_lock((100, 42))
    await lock.acquire()
    bot.send_message = AsyncMock()
    bot.delete_message = AsyncMock()

    async def handler(event, data):
        await tg._on_forum_topic_edited(event)

    edit = message(text=None, edited=ForumTopicEdited(name="사용자 선택"))
    task = asyncio.create_task(tg._sequential(handler, edit, {}))
    await asyncio.sleep(0.01)
    assert tg._topic_namer._state["100:42"] == "custom" and not task.done()
    release.set()
    await drain(tg)
    bot.edit_forum_topic.assert_not_awaited()
    lock.release()
    await task


# -- a null or rejected model title falls back to the start of the message ---------------------


async def test_a_short_message_with_a_null_title_names_the_topic_from_the_message(wired, caplog):
    tg, bot, _, orch, cli, _, _ = wired
    await implicit(tg)
    cli.send.return_value = CLIResponse(result='{"title":null}')

    with caplog.at_level("INFO", logger="ductor_bot.messenger.telegram.topic_naming"):
        await tg._on_message(message(text="핑"))
        await drain(tg)

    bot.edit_forum_topic.assert_awaited_once_with(chat_id=100, message_thread_id=42, name="핑")
    assert tg._topic_names.get(100, 42) == "핑"
    saved = json.loads((orch.paths.ductor_home / "dm_topic_naming.json").read_text())
    assert saved["topics"]["100:42"] == "attempted"
    logged = [r.getMessage() for r in caplog.records if "rejected" in r.getMessage()]
    assert logged == ["Topic auto-name: model title rejected reason=no_topic; using message prefix"]
    assert all("핑" not in r.getMessage() for r in caplog.records)  # no message text in logs


@pytest.mark.parametrize(
    ("result", "reason"),
    [
        ('{"title":"시스템 점검 완료"}', "outcome"),
        ("I will check it", "label_or_answer"),
        ('{"title":"Postgres pool 문제"}', "echo"),
    ],
)
async def test_each_rejection_reason_is_logged_as_a_code_only(wired, caplog, result, reason):
    tg, bot, _, _, cli, _, _ = wired
    await implicit(tg)
    cli.send.return_value = CLIResponse(result=result)

    with caplog.at_level("INFO", logger="ductor_bot.messenger.telegram.topic_naming"):
        await tg._on_message(message(text="여행 계획 세워줘"))
        await drain(tg)

    bot.edit_forum_topic.assert_awaited_once_with(
        chat_id=100, message_thread_id=42, name="여행 계획 세워줘"
    )
    texts = [r.getMessage() for r in caplog.records]
    assert f"Topic auto-name: model title rejected reason={reason}; using message prefix" in texts
    assert all("여행 계획" not in text for text in texts)


async def test_a_credential_message_keeps_the_existing_fallback(wired):
    tg, bot, _, _, cli, _, _ = wired
    await implicit(tg)
    cli.send.return_value = CLIResponse(result='{"title":null}')

    await tg._on_message(message(text="Authorization: Bearer synthetic-secret-token"))
    await drain(tg)

    # the message is never used as a title: the topic id names it, as before
    bot.edit_forum_topic.assert_awaited_once_with(chat_id=100, message_thread_id=42, name="42")


async def test_a_credential_title_from_the_model_keeps_the_existing_fallback(wired):
    tg, bot, _, _, cli, _, _ = wired
    await implicit(tg)
    cli.send.return_value = CLIResponse(result='{"title":"Bearer synthetic-secret-token"}')

    await tg._on_message(message(text="Safe request"))
    await drain(tg)

    bot.edit_forum_topic.assert_awaited_once_with(
        chat_id=100, message_thread_id=42, name="Safe request"
    )


async def test_a_topic_the_user_renamed_is_left_alone_even_with_a_null_title(wired):
    tg, bot, _, _, cli, factory, _ = wired
    await implicit(tg)
    tg._topic_namer.edited(100, 42)
    cli.send.return_value = CLIResponse(result='{"title":null}')

    await tg._on_message(message(text="핑"))
    await drain(tg)

    factory.assert_not_called()
    bot.edit_forum_topic.assert_not_awaited()


async def test_the_setting_off_leaves_the_topic_alone(wired):
    tg, bot, cfg, _, cli, factory, _ = wired
    cfg.telegram_dm_topic_auto_name = False
    await implicit(tg)
    cli.send.return_value = CLIResponse(result='{"title":null}')

    await tg._on_message(message(text="핑"))
    await drain(tg)

    factory.assert_not_called()
    bot.edit_forum_topic.assert_not_awaited()


async def test_a_greeting_still_waits_for_a_later_message(wired):
    tg, bot, _, orch, cli, factory, _ = wired
    await implicit(tg)

    await tg._on_message(message(text="안녕"))
    await drain(tg)

    factory.assert_not_called()  # no model call and no title for a plain greeting
    bot.edit_forum_topic.assert_not_awaited()
    saved = json.loads((orch.paths.ductor_home / "dm_topic_naming.json").read_text())
    assert saved["topics"]["100:42"] == "eligible"
