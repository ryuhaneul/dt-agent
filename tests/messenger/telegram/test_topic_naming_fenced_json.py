"""Fenced JSON title outputs pass through synthetic CLI and Bot boundaries."""

from __future__ import annotations

import pytest

from ductor_bot.cli.types import CLIResponse
from ductor_bot.messenger.telegram.topic_naming import clean_topic_title, generated_topic_title
from tests.messenger.telegram.test_topic_naming import drain, implicit, message
from tests.messenger.telegram.test_topic_naming import wired as wired


FENCES = [
    "```json\n%s\n```",
    "```\n%s\n```",
    "```JSON\n%s\n```",
    "```json\r\n%s\r\n```",
    " \n\t```json \t\n%s\n\t``` \n ",
]


@pytest.mark.parametrize("fence", FENCES)
async def test_fenced_system_check_reaches_exact_api_name(wired, fence):
    tg, bot, _, _, cli, _, _ = wired
    await implicit(tg)
    cli.send.return_value = CLIResponse(result=fence % '{"title":"시스템 점검"}')
    await tg._on_message(message(text="시스템 점검 해봐"))
    await drain(tg)
    bot.edit_forum_topic.assert_awaited_once_with(
        chat_id=100, message_thread_id=42, name="시스템 점검"
    )
    assert tg._topic_names.get(100, 42) == "시스템 점검"


async def test_fenced_null_names_the_topic_from_the_message(wired):
    tg, bot, _, _, cli, _, _ = wired
    await implicit(tg)
    cli.send.side_effect = [
        CLIResponse(result='```json\n{"title":null}\n```'),
        CLIResponse(result='```json\n{"title":"시스템 점검"}\n```'),
    ]
    text = "제목 변경 했는데 안된거야? 아니면 안한거야?"
    await tg._on_message(message(text=text))
    await drain(tg)
    # A fenced null is a null: the start of the message names the topic and naming ends.
    bot.edit_forum_topic.assert_awaited_once_with(
        chat_id=100, message_thread_id=42, name=clean_topic_title(text)
    )
    assert tg._topic_namer._state["100:42"] == "attempted"
    await tg._on_message(message(text="시스템 점검 해봐"))
    await drain(tg)
    bot.edit_forum_topic.assert_awaited_once()
    assert cli.send.await_count == 1


@pytest.mark.parametrize("fence", FENCES)
def test_fenced_null_keeps_existing_null_contract(fence):
    assert generated_topic_title(fence % '{"title":null}', "request") is None


@pytest.mark.parametrize(
    "payload,expected_api",
    [
        ('{"title":"I\'ll check your system"}', "Safe request"),
        ('{"title":"시스템을 점검하겠습니다"}', "Safe request"),
        ('{"title":"Postgres pool 문제"}', "Safe request"),
        ('{"title":"Bearer synthetic-secret"}', "Safe request"),
        ('{"title":"Bearer\\u200b synthetic-secret"}', "Safe request"),
    ],
)
async def test_fence_does_not_bypass_existing_rejection_or_safe_fallback(
    wired, payload, expected_api
):
    tg, bot, _, _, cli, _, _ = wired
    await implicit(tg)
    output = f"```json\n{payload}\n```"
    assert generated_topic_title(output, "Safe request") == generated_topic_title(
        payload, "Safe request"
    )
    cli.send.return_value = CLIResponse(result=output)
    await tg._on_message(message(text="Safe request"))
    await drain(tg)
    # Rejected and credential-looking titles both end in the safe start of the message.
    bot.edit_forum_topic.assert_awaited_once_with(
        chat_id=100, message_thread_id=42, name=expected_api
    )


@pytest.mark.parametrize(
    "output",
    [
        '```{"title":"abc"}```',
        '```python\n{"title":"abc"}\n```',
        '```json\n{"title":"abc"}\n```\nOutside prose',
        '```json\n{"title":"abc"}\n```\n```json\n{"title":null}\n```',
        '```json\n{"title":"abc"}',
        "```json\nTravel plan\n```",
        '```json\n{"title":"abc"}\n``` trailing',
        '```json\n{"title":"abc",}\n```',
    ],
)
def test_other_fence_formats_keep_existing_rejection(output):
    assert generated_topic_title(output, "request") is None
