from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from ductor_bot.messenger.slack.sender import (
    SlackSendOpts,
    _split_text,
    send_rich,
    send_rich_checked,
    send_rich_checked_progress,
)


class TestSendRich:
    async def test_sends_text_message(self) -> None:
        client = AsyncMock()
        client.chat_postMessage.return_value = {"ts": "1.0"}

        result = await send_rich(client, "C123", "Hello **world**")

        assert result == "1.0"
        client.chat_postMessage.assert_awaited_once()
        assert client.chat_postMessage.call_args.kwargs["channel"] == "C123"
        assert client.chat_postMessage.call_args.kwargs["text"] == "Hello *world*"

    async def test_sends_tagged_file(self, tmp_path: Path) -> None:
        client = AsyncMock()
        client.chat_postMessage.return_value = {"ts": "1.0"}
        client.files_upload_v2.return_value = {"ok": True}
        file_path = tmp_path / "out.txt"
        file_path.write_text("hello", encoding="utf-8")

        await send_rich(
            client,
            "C123",
            f"See this <file:{file_path}>",
            SlackSendOpts(allowed_roots=[tmp_path]),
        )

        client.files_upload_v2.assert_awaited_once()
        assert client.files_upload_v2.call_args.kwargs["channel"] == "C123"


def test_split_text_splits_long_messages() -> None:
    chunks = _split_text("x" * 40_500)
    assert len(chunks) == 2


async def test_checked_reports_partial_chunk_failure_and_legacy_returns_last_ts(
    monkeypatch,
) -> None:
    from ductor_bot.messenger.slack import sender

    monkeypatch.setattr(sender, "_split_text", lambda _text: ["one", "two", "three"])
    client = AsyncMock()
    responses = [
        {"ok": True, "ts": "1.0"},
        {"ok": False},
        {"ok": True, "ts": "3.0"},
    ]
    client.chat_postMessage.side_effect = responses
    ok, ts = await send_rich_checked(client, "C123", "three chunks")
    assert not ok
    assert ts == "3.0"

    client.chat_postMessage.side_effect = responses
    assert await send_rich(client, "C123", "three chunks") == "3.0"


async def test_ia_progress_stops_at_second_chunk_and_resumes(monkeypatch) -> None:
    from ductor_bot.messenger.slack import sender

    monkeypatch.setattr(sender, "_split_text", lambda _text: ["first", "second", "third"])
    client = AsyncMock()
    client.chat_postMessage.side_effect = [
        {"ok": True, "ts": "1.0"}, {"ok": False},
    ]
    ok, ts, progress = await send_rich_checked_progress(client, "C123", "three chunks")
    assert (ok, ts, progress) == (False, "1.0", 1)
    assert [call.kwargs["text"] for call in client.chat_postMessage.await_args_list] == [
        "first", "second",
    ]

    client.chat_postMessage.reset_mock()
    client.chat_postMessage.side_effect = [
        {"ok": True, "ts": "2.0"}, {"ok": True, "ts": "3.0"},
    ]
    ok, ts, progress = await send_rich_checked_progress(
        client, "C123", "three chunks", start_chunk=1
    )
    assert (ok, ts, progress) == (True, "3.0", 3)
    assert [call.kwargs["text"] for call in client.chat_postMessage.await_args_list] == [
        "second", "third",
    ]


async def test_ia_progress_first_chunk_failure_resends_all(monkeypatch) -> None:
    from ductor_bot.messenger.slack import sender

    monkeypatch.setattr(sender, "_split_text", lambda _text: ["first", "second", "third"])
    client = AsyncMock()
    client.chat_postMessage.side_effect = [{"ok": False}]
    assert await send_rich_checked_progress(client, "C123", "three chunks") == (
        False, None, 0,
    )
    client.chat_postMessage.reset_mock()
    client.chat_postMessage.side_effect = [
        {"ok": True, "ts": "1.0"}, {"ok": True, "ts": "2.0"},
        {"ok": True, "ts": "3.0"},
    ]
    assert (await send_rich_checked_progress(
        client, "C123", "three chunks", start_chunk=0
    ))[0] is True
    assert client.chat_postMessage.await_count == 3


async def test_ia_chunk_split_is_deterministic_with_same_options(monkeypatch) -> None:
    from ductor_bot.messenger.slack import sender

    monkeypatch.setattr(sender, "_MAX_MESSAGE_LENGTH", 7)
    text = "alpha\nbeta\ngamma\ndelta"
    opts = SlackSendOpts(thread_ts="1.0")
    attempts = []
    for _ in range(2):
        client = AsyncMock()
        client.chat_postMessage.side_effect = [
            {"ok": True, "ts": str(i)} for i in range(10)
        ]
        assert (await send_rich_checked_progress(client, "C123", text, opts))[0] is True
        attempts.append([call.kwargs for call in client.chat_postMessage.await_args_list])
    assert len(attempts[0]) >= 2
    assert attempts[0] == attempts[1]


@pytest.mark.parametrize(
    "response",
    [
        {"ok": True},
        {"ok": True, "file": {"id": "F123"}},
        {"ok": True, "files": [], "file": {"id": "F123"}},
        {"ok": True, "files": [{"id": None}]},
        {"ok": True, "files": [{"id": ""}]},
        {"ok": True, "files": [{"id": "None"}]},
        {"ok": False, "files": [{"id": "F123"}]},
    ],
)
async def test_ia_file_chunk_requires_acknowledged_file_id(
    tmp_path: Path, response: dict,
) -> None:
    file_path = tmp_path / "report.txt"
    file_path.write_text("report", encoding="utf-8")
    client = AsyncMock()
    client.files_upload_v2.return_value = response
    assert await send_rich_checked_progress(
        client, "C123", f"<file:{file_path}>",
        SlackSendOpts(allowed_roots=[tmp_path]),
    ) == (False, None, 0)


async def test_ia_file_chunk_accepts_slack_file_id(tmp_path: Path) -> None:
    file_path = tmp_path / "report.txt"
    file_path.write_text("report", encoding="utf-8")
    client = AsyncMock()
    client.files_upload_v2.return_value = {
        "ok": True, "files": [{"id": "F123"}], "file": {"id": "F123"},
    }
    assert await send_rich_checked_progress(
        client, "C123", f"<file:{file_path}>",
        SlackSendOpts(allowed_roots=[tmp_path]),
    ) == (True, None, 1)
