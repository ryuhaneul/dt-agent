"""Tests for centralized logging setup."""

from __future__ import annotations

import logging
from pathlib import Path


class TestSetupLogging:
    """Test logging configuration."""

    def test_sets_root_level(self) -> None:
        from ductor_bot.logging_config import setup_logging

        setup_logging(level=logging.WARNING, log_dir=None)
        assert logging.getLogger().level == logging.WARNING

    def test_verbose_sets_debug(self) -> None:
        from ductor_bot.logging_config import setup_logging

        setup_logging(verbose=True, log_dir=None)
        assert logging.getLogger().level == logging.DEBUG

    def test_console_handler_added(self) -> None:
        from ductor_bot.logging_config import setup_logging

        setup_logging(log_dir=None)
        root = logging.getLogger()
        handler_types = [type(h).__name__ for h in root.handlers]
        assert "StreamHandler" in handler_types

    def test_file_handler_when_log_dir(self, tmp_path: Path) -> None:
        from ductor_bot.logging_config import setup_logging

        log_dir = tmp_path / "logs"
        setup_logging(log_dir=log_dir)
        assert log_dir.exists()
        # QueueHandler should be present (for async file writing)
        root = logging.getLogger()
        handler_types = [type(h).__name__ for h in root.handlers]
        assert "QueueHandler" in handler_types

    def test_noisy_loggers_quieted(self) -> None:
        from ductor_bot.logging_config import setup_logging

        setup_logging(log_dir=None)
        assert logging.getLogger("httpx").level >= logging.WARNING
        assert logging.getLogger("httpcore").level >= logging.WARNING

    def test_repeated_calls_no_duplicate_handlers(self) -> None:
        from ductor_bot.logging_config import setup_logging

        setup_logging(log_dir=None)
        setup_logging(log_dir=None)
        root = logging.getLogger()
        # Should not accumulate handlers
        stream_handlers = [h for h in root.handlers if isinstance(h, logging.StreamHandler)]
        assert len(stream_handlers) <= 2  # Console + possibly QueueHandler


class TestColorFormatter:
    """Test ANSI color formatting."""

    def test_color_applied_when_enabled(self) -> None:
        from ductor_bot.logging_config import _ColorFormatter

        fmt = _ColorFormatter("%(levelname)s: %(message)s", use_color=True)
        record = logging.LogRecord("test", logging.INFO, "", 0, "msg", (), None)
        result = fmt.format(record)
        assert "\x1b[" in result  # ANSI escape present

    def test_no_color_when_disabled(self) -> None:
        from ductor_bot.logging_config import _ColorFormatter

        fmt = _ColorFormatter("%(levelname)s: %(message)s", use_color=False)
        record = logging.LogRecord("test", logging.INFO, "", 0, "msg", (), None)
        result = fmt.format(record)
        assert "\x1b[" not in result


async def test_fetch_decode_error_hides_batch_from_console_and_file(
    tmp_path: Path, monkeypatch
) -> None:
    import asyncio
    import io
    import json
    from unittest.mock import AsyncMock

    from aiogram import Bot, Dispatcher
    from aiogram.client.session.aiohttp import AiohttpSession
    from aiogram.utils.backoff import Backoff

    from ductor_bot.logging_config import _stop_queue_listener, setup_logging

    marker = "synthetic-sensitive-batch-value"
    content = json.dumps(
        {
            "ok": True,
            "result": [
                {
                    "update_id": 1,
                    "message": {
                        "message_id": 1,
                        "date": 0,
                        "chat": {"id": 1, "type": "private"},
                        "text": marker,
                    },
                },
                {"update_id": "invalid"},
            ],
        }
    )
    console = io.StringIO()
    monkeypatch.setattr("sys.stderr", console)
    setup_logging(log_dir=tmp_path)
    setup_logging(log_dir=tmp_path)
    dispatcher = logging.getLogger("aiogram.dispatcher")
    session = AiohttpSession()
    bot = Bot(token="123456:synthetic-token", session=session)

    async def malformed_response(bot, method, **_kwargs):
        return session.check_response(bot, method, 200, content).result

    monkeypatch.setattr(session, "make_request", malformed_response)
    monkeypatch.setattr(Backoff, "asleep", AsyncMock(side_effect=asyncio.CancelledError))
    updates = Dispatcher._listen_updates(bot)
    try:
        import pytest

        with pytest.raises(asyncio.CancelledError):
            await anext(updates)
    finally:
        await updates.aclose()
        await session.close()
        _stop_queue_listener()
    for output in (console.getvalue(), (tmp_path / "agent.log").read_text()):
        assert marker not in output
        assert "Failed to fetch updates - ClientDecodeError: ClientDecodeError" in output
    assert (
        sum(type(item).__name__ == "_DispatcherExceptionFilter" for item in dispatcher.filters) == 1
    )


def test_dispatcher_exception_fields_are_sanitized(tmp_path: Path, monkeypatch) -> None:
    import io

    from ductor_bot.logging_config import _stop_queue_listener, setup_logging

    marker = "synthetic-sensitive-exception-value"
    console = io.StringIO()
    monkeypatch.setattr("sys.stderr", console)
    setup_logging(log_dir=tmp_path)
    dispatcher = logging.getLogger("aiogram.dispatcher")
    error = ValueError(marker)
    dispatcher.error("Failure: %s", error, exc_info=(ValueError, error, None))
    record = dispatcher.makeRecord(
        dispatcher.name,
        logging.ERROR,
        "",
        0,
        "Mapped: %(error)s",
        ({"error": ValueError(marker)},),
        None,
    )
    record.exc_text = "ValueError: " + marker + "\n" + marker
    dispatcher.handle(record)
    _stop_queue_listener()
    for output in (console.getvalue(), (tmp_path / "agent.log").read_text()):
        assert marker not in output
        assert "ValueError" in output
        assert "Failure:" in output
        assert "Mapped:" in output
