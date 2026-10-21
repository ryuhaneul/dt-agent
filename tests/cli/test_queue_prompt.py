"""Actual CLI gateway receives queue metadata only at send time."""

import logging
from unittest.mock import AsyncMock, Mock

import pytest

from ductor_bot.cli.stream_events import ResultEvent
from ductor_bot.cli.types import AgentRequest, CLIResponse
from ductor_bot.messenger.queue_context import QUEUE_NOTICE
from tests.cli.test_service import _make_service


@pytest.mark.parametrize("mode", ["non_streaming", "streaming", "fallback"])
async def test_queue_prompt_cli_gateway_preserves_request_and_log_preview(
    monkeypatch, caplog, mode
):
    caplog.set_level(logging.DEBUG)
    header = (
        "[Queued user message]\nSent at: synthetic\nWaited: 75s\nQueue position at arrival: 2\n"
        + QUEUE_NOTICE
        + "\n[/Queued user message]"
    )
    request = AgentRequest(prompt="user media or text", queue_context=header)
    received = []

    async def send(**kwargs):
        received.append(kwargs["prompt"])
        return CLIResponse(result="reply", session_id="synthetic")

    async def stream(**kwargs):
        received.append(kwargs["prompt"])
        if mode != "fallback":
            yield ResultEvent(type="result", result="reply", session_id="synthetic")

    cli = Mock(send=AsyncMock(side_effect=send), send_streaming=stream)
    monkeypatch.setattr("ductor_bot.cli.service.create_cli", lambda config: cli)
    service = _make_service()
    if mode == "non_streaming":
        response = await service.execute(request)
    else:
        response = await service.execute_streaming(request)
    assert response.result == "reply"
    assert received
    assert all(prompt == header + "\n\nuser media or text" for prompt in received)
    assert all(prompt.count("[Queued user message]") == 1 for prompt in received)
    assert request.prompt == "user media or text"
    assert "Queued user message" not in caplog.text
