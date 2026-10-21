"""Delivery outcomes reflect transport sends and never reroute IA results."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ductor_bot.bus.bus import MessageBus
from ductor_bot.bus.envelope import DeliveryMode, Envelope, LockMode, Origin
from ductor_bot.messenger.matrix.transport import MatrixTransport
from ductor_bot.messenger.slack.transport import SlackTransport
from ductor_bot.messenger.telegram.transport import TelegramTransport


def _ia_envelope(transport: str) -> Envelope:
    return Envelope(
        origin=Origin.INTERAGENT,
        chat_id=123,
        topic_id=None if transport == "mx" else 7,
        transport=transport,
        prompt="Process the delegated result",
        result_text="raw result",
        lock_mode=LockMode.REQUIRED,
        needs_injection=True,
    )


def _bus_with_injector() -> MessageBus:
    bus = MessageBus()
    injector = AsyncMock()
    injector.inject_prompt.return_value = "Main's final report"
    bus.set_injector(injector)
    return bus


async def test_telegram_checked_send_false_marks_report_undelivered() -> None:
    bot = MagicMock()
    bot.file_roots.return_value = []
    bus = _bus_with_injector()
    bus.register_transport(TelegramTransport(bot))
    with patch(
        "ductor_bot.messenger.telegram.transport.send_rich_checked_progress",
        new_callable=AsyncMock,
        return_value=(False, 0),
    ) as send:
        outcome = await bus.submit(_ia_envelope("tg"))
    send.assert_awaited_once()
    assert outcome.injected
    assert not outcome.delivered
    assert not outcome.ok


@pytest.mark.parametrize(
    ("transport_name", "transport_class", "send_path"),
    [
        (
            "mx",
            MatrixTransport,
            "ductor_bot.messenger.matrix.transport.matrix_send_rich_checked_progress",
        ),
        (
            "sl",
            SlackTransport,
            "ductor_bot.messenger.slack.transport.slack_send_rich_checked_progress",
        ),
    ],
)
async def test_checked_transport_failure_marks_report_undelivered(
    transport_name: str, transport_class: type, send_path: str
) -> None:
    bot = MagicMock()
    bot.id_map.int_to_room.return_value = "!room:test"
    bot.id_map.int_to_channel.return_value = "C123"
    bot.id_map.int_to_thread.return_value = ("C123", "1.0")
    bot.file_roots.return_value = []
    bus = _bus_with_injector()
    bus.register_transport(transport_class(bot))
    with patch(send_path, new_callable=AsyncMock, return_value=(False, "last", 0)) as send:
        outcome = await bus.submit(_ia_envelope(transport_name))
    send.assert_awaited_once()
    assert outcome.injected
    assert not outcome.delivered
    assert not outcome.ok


async def test_broadcast_exception_marks_envelope_undelivered() -> None:
    bus = MessageBus()
    transport = AsyncMock()
    transport.transport_name = "tg"
    transport.deliver_broadcast.side_effect = RuntimeError("send failed")
    bus.register_transport(transport)
    envelope = Envelope(
        origin=Origin.CRON, chat_id=0, delivery=DeliveryMode.BROADCAST, result_text="news"
    )
    outcome = await bus.submit(envelope)
    assert not envelope.delivered
    assert envelope.failed_transports == ["tg"]
    assert "tg" in envelope.delivery_error
    assert not outcome.delivered


async def test_interagent_missing_transport_does_not_fallback() -> None:
    bus = _bus_with_injector()
    matrix = AsyncMock()
    matrix.transport_name = "mx"
    bus.register_transport(matrix)
    outcome = await bus.submit(_ia_envelope("tg"))
    matrix.deliver.assert_not_awaited()
    matrix.deliver_broadcast.assert_not_awaited()
    assert not outcome.ok
    assert not outcome.delivered
    assert "unavailable" in outcome.error


async def test_non_interagent_missing_transport_keeps_fallback() -> None:
    bus = MessageBus()
    matrix = AsyncMock()
    matrix.transport_name = "mx"
    bus.register_transport(matrix)
    envelope = Envelope(origin=Origin.CRON, chat_id=123, transport="tg", result_text="news")
    await bus.submit(envelope)
    matrix.deliver.assert_not_awaited()
    matrix.deliver_broadcast.assert_awaited_once()


@pytest.mark.parametrize(
    ("transport_name", "transport_class", "send_path", "send_values"),
    [
        (
            "tg",
            TelegramTransport,
            "ductor_bot.messenger.telegram.transport.send_rich",
            (False, True),
        ),
        (
            "mx",
            MatrixTransport,
            "ductor_bot.messenger.matrix.transport.matrix_send_rich_checked",
            ((False, None), (True, "$sent")),
        ),
        (
            "sl",
            SlackTransport,
            "ductor_bot.messenger.slack.transport.slack_send_rich_checked",
            ((False, None), (True, "1.0")),
        ),
    ],
)
async def test_ia_final_report_controls_delivery_independently_of_notice(
    transport_name: str,
    transport_class: type,
    send_path: str,
    send_values: tuple[object, object],
    caplog: pytest.LogCaptureFixture,
) -> None:
    failure_value, success_value = send_values
    bot = MagicMock()
    bot.id_map.int_to_room.return_value = "!room:test"
    bot.id_map.int_to_channel.return_value = "C123"
    bot.id_map.int_to_thread.return_value = ("C123", "1.0")
    bot.file_roots.return_value = []
    transport = transport_class(bot)
    for responses, expected in (
        ([failure_value, success_value], True),
        ([success_value, failure_value], False),
    ):
        env = _ia_envelope(transport_name)
        env.needs_injection = False
        env.result_text = "Main's final report"
        env.metadata["provider_switch_notice"] = "Provider changed"
        if transport_name == "tg":
            with (
                patch(send_path, new_callable=AsyncMock, return_value=responses[0]) as notice_send,
                patch(
                    "ductor_bot.messenger.telegram.transport.send_rich_checked_progress",
                    new_callable=AsyncMock,
                    return_value=(responses[1], 1 if expected else 0),
                ) as report_send,
                caplog.at_level(logging.WARNING),
            ):
                await transport.deliver(env)
            notice_send.assert_awaited_once()
            report_send.assert_awaited_once()
        else:
            progress_path = send_path + "_progress"
            with (
                patch(send_path, new_callable=AsyncMock, return_value=responses[0]) as notice_send,
                patch(
                    progress_path, new_callable=AsyncMock,
                    return_value=(*responses[1], 1 if expected else 0),
                ) as report_send,
                caplog.at_level(logging.WARNING),
            ):
                await transport.deliver(env)
            notice_send.assert_awaited_once()
            report_send.assert_awaited_once()
        assert env.delivered is expected
        if expected:
            assert "Provider switch notice was not delivered" in caplog.text
        caplog.clear()


@pytest.mark.parametrize(
    ("transport_name", "transport_class", "send_path"),
    [
        ("tg", TelegramTransport,
         "ductor_bot.messenger.telegram.transport.send_rich_checked_progress"),
        ("mx", MatrixTransport, "ductor_bot.messenger.matrix.transport.matrix_send_rich_checked_progress"),
        ("sl", SlackTransport, "ductor_bot.messenger.slack.transport.slack_send_rich_checked_progress"),
    ],
)
@pytest.mark.parametrize("result_text", ["", "  \n  "])
async def test_ia_blank_report_is_not_sent(
    transport_name: str, transport_class: type, send_path: str, result_text: str
) -> None:
    bot = MagicMock()
    bot.id_map.int_to_room.return_value = "!room:test"
    bot.id_map.int_to_channel.return_value = "C123"
    bot.id_map.int_to_thread.return_value = ("C123", "1.0")
    bot.file_roots.return_value = []
    transport = transport_class(bot)
    env = _ia_envelope(transport_name)
    env.result_text = result_text
    with patch(send_path, new_callable=AsyncMock) as send:
        await transport.deliver(env)
    send.assert_not_awaited()
    assert not env.delivered
    assert env.delivery_error


@pytest.mark.parametrize(
    ("transport_name", "transport_class", "progress_path"),
    [
        ("mx", MatrixTransport,
         "ductor_bot.messenger.matrix.transport.matrix_send_rich_checked_progress"),
        ("sl", SlackTransport,
         "ductor_bot.messenger.slack.transport.slack_send_rich_checked_progress"),
    ],
)
async def test_ia_error_envelope_uses_saved_chunk_offset(
    transport_name: str, transport_class: type, progress_path: str
) -> None:
    bot = MagicMock()
    bot.id_map.int_to_room.return_value = "!room:test"
    bot.id_map.int_to_channel.return_value = "C123"
    bot.id_map.int_to_thread.return_value = ("C123", "1.0")
    bot.file_roots.return_value = []
    transport = transport_class(bot)
    env = _ia_envelope(transport_name)
    env.needs_injection = False
    env.injected = True
    env.is_error = True
    env.start_chunk = 1
    env.delivered_chunks = 1
    env.metadata["error"] = "timeout"
    with patch(
        progress_path, new_callable=AsyncMock, return_value=(False, None, 2)
    ) as send:
        await transport.deliver(env)
    send.assert_awaited_once()
    assert send.call_args.kwargs["start_chunk"] == 1
    assert "Inter-Agent Request Failed" in send.call_args.args[2]
    assert not env.delivered
    assert env.delivered_chunks == 2


async def test_interagent_broadcast_uses_all_registered_transports_and_tracks_failure() -> None:
    from ductor_bot.bus.adapters import from_interagent_result
    from ductor_bot.multiagent.bus import AsyncInterAgentResult

    bus = MessageBus()
    transports = []
    for name in ("tg", "mx", "sl"):
        adapter = AsyncMock()
        adapter.transport_name = name
        bus.register_transport(adapter)
        transports.append(adapter)

    async def fail(envelope: Envelope) -> None:
        envelope.delivered = False

    transports[1].deliver_broadcast.side_effect = fail
    result = AsyncInterAgentResult(
        task_id="broadcast-1", sender="main", recipient="sub",
        message_preview="work", result_text="done", chat_id=0,
        result_origin="cron-broadcast", broadcast_ref="job-1",
        main_response="final report",
    )
    envelope = from_interagent_result(result, chat_id=999, transport="tg")
    outcome = await bus.submit(envelope)

    assert not outcome.ok
    assert outcome.injected
    assert envelope.failed_transports == ["mx"]
    assert outcome.error == "broadcast failed: mx"
    for adapter in transports:
        adapter.deliver_broadcast.assert_awaited_once_with(envelope)
        adapter.deliver.assert_not_awaited()
