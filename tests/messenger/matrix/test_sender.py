"""Tests for Matrix message sender."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from ductor_bot.messenger.matrix.sender import (
    MatrixSendOpts,
    _split_text,
    _upload_and_send_file,
    send_rich,
    send_rich_checked,
    send_rich_checked_progress,
)

# ---------------------------------------------------------------------------
# send_rich
# ---------------------------------------------------------------------------


class TestSendRich:
    async def test_sends_formatted_message(self) -> None:
        client = AsyncMock()
        resp = MagicMock()
        resp.event_id = "$ev1"
        client.room_send.return_value = resp

        event_id = await send_rich(client, "!room:test", "Hello **world**")

        client.room_send.assert_awaited_once()
        call_args = client.room_send.call_args
        assert call_args[0][0] == "!room:test"
        assert call_args[0][1] == "m.room.message"
        content = call_args[0][2]
        assert content["msgtype"] == "m.text"
        assert "world" in content["formatted_body"]
        assert event_id == "$ev1"

    async def test_returns_none_on_failure(self) -> None:
        client = AsyncMock()
        resp = MagicMock(spec=[])  # No event_id
        client.room_send.return_value = resp

        event_id = await send_rich(client, "!room:test", "Hello")

        assert event_id is None

    async def test_empty_text_sends_nothing(self) -> None:
        client = AsyncMock()
        event_id = await send_rich(client, "!room:test", "")
        # Only file tags would be sent, but there are none
        client.room_send.assert_not_awaited()
        assert event_id is None

    async def test_thread_support(self) -> None:
        client = AsyncMock()
        resp = MagicMock()
        resp.event_id = "$ev2"
        client.room_send.return_value = resp

        opts = MatrixSendOpts(thread_event_id="$thread1")
        await send_rich(client, "!room:test", "Threaded reply", opts)

        content = client.room_send.call_args[0][2]
        assert content["m.relates_to"]["rel_type"] == "m.thread"
        assert content["m.relates_to"]["event_id"] == "$thread1"

    async def test_reply_support(self) -> None:
        client = AsyncMock()
        resp = MagicMock()
        resp.event_id = "$ev3"
        client.room_send.return_value = resp

        opts = MatrixSendOpts(reply_to_event_id="$reply1")
        await send_rich(client, "!room:test", "Reply", opts)

        content = client.room_send.call_args[0][2]
        assert content["m.relates_to"]["m.in_reply_to"]["event_id"] == "$reply1"

    async def test_file_tag_extraction(self, tmp_path: Path) -> None:
        test_file = tmp_path / "doc.txt"
        test_file.write_text("content")

        client = AsyncMock()
        resp = MagicMock()
        resp.event_id = "$ev4"
        resp.content_uri = "mxc://server/file1"
        client.room_send.return_value = resp
        client.upload.return_value = (resp, None)

        opts = MatrixSendOpts(allowed_roots=[tmp_path])
        await send_rich(client, "!room:test", f"Check this <file:{test_file}>", opts)

        # Should have sent text + file
        assert client.room_send.await_count == 2

    async def test_file_outside_allowed_roots_skipped(self, tmp_path: Path) -> None:
        test_file = tmp_path / "secret.txt"
        test_file.write_text("secret")

        client = AsyncMock()
        resp = MagicMock()
        resp.event_id = "$ev5"
        client.room_send.return_value = resp

        opts = MatrixSendOpts(allowed_roots=[Path("/opt/safe")])
        await send_rich(client, "!room:test", f"<file:{test_file}>", opts)

        # File should be skipped, only empty text (also skipped)
        client.upload.assert_not_awaited()


# ---------------------------------------------------------------------------
# _upload_and_send_file
# ---------------------------------------------------------------------------


class TestUploadAndSendFile:
    async def test_image_upload(self, tmp_path: Path) -> None:
        test_file = tmp_path / "photo.jpg"
        test_file.write_bytes(b"\xff\xd8\xff\xe0")  # JPEG header

        client = AsyncMock()
        upload_resp = MagicMock()
        upload_resp.content_uri = "mxc://server/photo1"
        client.upload.return_value = (upload_resp, None)

        send_resp = MagicMock()
        send_resp.event_id = "$ev_photo"
        client.room_send.return_value = send_resp

        event_id = await _upload_and_send_file(client, "!room:test", test_file)

        assert event_id == "$ev_photo"
        content = client.room_send.call_args[0][2]
        assert content["msgtype"] == "m.image"
        assert content["url"] == "mxc://server/photo1"

    async def test_document_upload(self, tmp_path: Path) -> None:
        test_file = tmp_path / "data.csv"
        test_file.write_text("a,b,c\n1,2,3")

        client = AsyncMock()
        upload_resp = MagicMock()
        upload_resp.content_uri = "mxc://server/csv1"
        client.upload.return_value = (upload_resp, None)

        send_resp = MagicMock()
        send_resp.event_id = "$ev_csv"
        client.room_send.return_value = send_resp

        await _upload_and_send_file(client, "!room:test", test_file)

        content = client.room_send.call_args[0][2]
        assert content["msgtype"] == "m.file"

    async def test_upload_failure_returns_none(self, tmp_path: Path) -> None:
        test_file = tmp_path / "fail.txt"
        test_file.write_text("test")

        client = AsyncMock()
        upload_resp = MagicMock(spec=[])  # No content_uri
        client.upload.return_value = (upload_resp, None)

        event_id = await _upload_and_send_file(client, "!room:test", test_file)
        assert event_id is None


# ---------------------------------------------------------------------------
# _split_text
# ---------------------------------------------------------------------------


class TestSplitText:
    def test_short_text_no_split(self) -> None:
        result = _split_text("hello", "<p>hello</p>")
        assert len(result) == 1
        assert result[0][0] == "hello"

    def test_empty_text(self) -> None:
        result = _split_text("", "")
        assert result == [("", "")]

    def test_long_text_splits(self) -> None:
        # Create text that exceeds 60KB
        line = "x" * 1000
        lines = [line] * 70  # 70KB
        plain = "\n".join(lines)
        html = plain  # simplified
        result = _split_text(plain, html)
        assert len(result) >= 2


async def test_checked_reports_partial_chunk_failure_and_legacy_returns_last_id(
    monkeypatch,
) -> None:
    from ductor_bot.messenger.matrix import sender

    monkeypatch.setattr(sender, "_MAX_EVENT_SIZE", 1)
    monkeypatch.setattr(
        sender, "_split_text", lambda _plain, _html: [("one", "one"), ("two", "two"), ("three", "three")]
    )
    client = AsyncMock()
    responses = [
        SimpleNamespace(event_id="$first"),
        object(),
        SimpleNamespace(event_id="$last"),
    ]
    client.room_send.side_effect = responses
    ok, event_id = await send_rich_checked(client, "!room:test", "three chunks")
    assert not ok
    assert event_id == "$last"

    client.room_send.side_effect = responses
    assert await send_rich(client, "!room:test", "three chunks") == "$last"


async def test_ia_progress_stops_at_second_chunk_and_resumes(monkeypatch) -> None:
    from ductor_bot.messenger.matrix import sender

    monkeypatch.setattr(sender, "_MAX_EVENT_SIZE", 1)
    monkeypatch.setattr(
        sender, "_split_text",
        lambda _plain, _html: [("first", "first"), ("second", "second"), ("third", "third")],
    )
    client = AsyncMock()
    client.room_send.side_effect = [SimpleNamespace(event_id="$first"), object()]
    ok, event_id, progress = await send_rich_checked_progress(
        client, "!room:test", "three chunks"
    )
    assert (ok, event_id, progress) == (False, "$first", 1)
    assert [call.args[2]["body"] for call in client.room_send.await_args_list] == [
        "first", "second",
    ]

    client.room_send.reset_mock()
    client.room_send.side_effect = [
        SimpleNamespace(event_id="$second"), SimpleNamespace(event_id="$third"),
    ]
    ok, event_id, progress = await send_rich_checked_progress(
        client, "!room:test", "three chunks", start_chunk=1
    )
    assert (ok, event_id, progress) == (True, "$third", 3)
    assert [call.args[2]["body"] for call in client.room_send.await_args_list] == [
        "second", "third",
    ]


async def test_ia_progress_first_chunk_failure_resends_all(monkeypatch) -> None:
    from ductor_bot.messenger.matrix import sender

    monkeypatch.setattr(sender, "_MAX_EVENT_SIZE", 1)
    monkeypatch.setattr(
        sender, "_split_text",
        lambda _plain, _html: [("first", "first"), ("second", "second"), ("third", "third")],
    )
    client = AsyncMock()
    client.room_send.side_effect = [object()]
    assert await send_rich_checked_progress(client, "!room:test", "three chunks") == (
        False, None, 0,
    )
    client.room_send.reset_mock()
    client.room_send.side_effect = [
        SimpleNamespace(event_id="$1"), SimpleNamespace(event_id="$2"),
        SimpleNamespace(event_id="$3"),
    ]
    assert (await send_rich_checked_progress(
        client, "!room:test", "three chunks", start_chunk=0
    ))[0] is True
    assert client.room_send.await_count == 3


async def test_ia_chunk_split_is_deterministic_with_same_options(monkeypatch) -> None:
    from ductor_bot.messenger.matrix import sender

    monkeypatch.setattr(sender, "_MAX_EVENT_SIZE", 15)
    text = "alpha\nbeta\ngamma\ndelta\nepsilon"
    opts = MatrixSendOpts(thread_event_id="$thread")
    attempts = []
    for _ in range(2):
        client = AsyncMock()
        client.room_send.side_effect = [
            SimpleNamespace(event_id=f"$event{i}") for i in range(10)
        ]
        assert (await send_rich_checked_progress(
            client, "!room:test", text, opts
        ))[0] is True
        attempts.append([call.args[2] for call in client.room_send.await_args_list])
    assert len(attempts[0]) >= 2
    assert attempts[0] == attempts[1]


async def test_file_chunk_requires_real_matrix_event_id(tmp_path: Path) -> None:
    file_path = tmp_path / "report.txt"
    file_path.write_text("report", encoding="utf-8")
    client = AsyncMock()
    client.upload.return_value = (SimpleNamespace(content_uri="mxc://test/report"), None)
    text = f"<file:{file_path}>"
    for event_id in (None, "", "None"):
        client.room_send.return_value = SimpleNamespace(event_id=event_id)
        assert await _upload_and_send_file(client, "!room:test", file_path) is None
        assert await send_rich_checked_progress(client, "!room:test", text) == (
            False, None, 0,
        )
        assert await send_rich(client, "!room:test", text) is None

    client.room_send.return_value = SimpleNamespace(event_id="$sent")
    assert await send_rich_checked_progress(client, "!room:test", text) == (
        True, "$sent", 1,
    )
    assert await send_rich(client, "!room:test", text) == "$sent"
