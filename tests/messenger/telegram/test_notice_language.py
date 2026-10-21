"""Telegram notices, the queue indicator and task reasons follow the configured language."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.exceptions import TelegramAPIError

from ductor_bot import i18n
from ductor_bot.bus.adapters import from_task_result
from ductor_bot.bus.bus import MessageBus
from ductor_bot.bus.envelope import DeliveryMode, Envelope, Origin
from ductor_bot.i18n import t
from ductor_bot.messenger.telegram.transport import TelegramTransport
from ductor_bot.tasks.hub import TaskHub
from ductor_bot.tasks.models import TaskEntry
from ductor_bot.text.response_format import (
    show_run_result,
    show_run_status,
    show_task_error,
)

LANGUAGES = ["en", "ko"]
RUN_FORMS = [
    (
        "error:cli_not_found_claude",
        "[claude CLI not found]",
        "run.cli_not_found",
        {"provider": "claude"},
    ),
    (
        "error:timeout",
        "[cron timed out after 30s]",
        "run.timed_out",
        {"label": "cron", "secs": "30"},
    ),
    (
        "error:exception",
        "Cron job 'Nightly' failed with an error (OSError).",
        "run.cron_failed",
        {"title": "Nightly", "error_type": "OSError"},
    ),
    (
        "error:injection",
        "Cron job 'Nightly' could not run in this session.",
        "run.cron_no_session",
        {"title": "Nightly"},
    ),
]
RESUME = (
    "interrupted by restart\n\nTo continue this task's conversation, use:\n"
    'python3 tools/task_tools/resume_task.py abc123 "your follow-up"'
)
TASK_REASONS = [
    ("interrupted by restart", "task_error.interrupted", {}),
    ("Bot restarted while task was running", "task_error.bot_restarted", {}),
    ("Timeout after 90s", "task_error.timeout", {"secs": "90"}),
    ("stale process killed after 12s", "task_error.stale_killed", {"secs": "12"}),
    ("CLI error", "task_error.cli_error", {}),
]


@pytest.fixture(autouse=True)
def _english_again() -> Iterator[None]:
    yield
    i18n.init("en")


def _english(key: str, **values: object) -> str:
    i18n.init("en")
    return t(key, **values)


def _transport() -> tuple[TelegramTransport, MagicMock]:
    bot = MagicMock()
    bot.bot_instance = MagicMock()
    bot.file_roots.return_value = [Path("/tmp/roots")]
    bot._orch.paths = MagicMock()
    bot._config.allowed_user_ids = [7]
    bot.broadcast = AsyncMock()
    return TelegramTransport(bot), bot


def _env(**kwargs: object) -> Envelope:
    defaults: dict[str, object] = {"origin": Origin.CRON, "chat_id": 42}
    defaults.update(kwargs)
    return Envelope(**defaults)  # type: ignore[arg-type]


async def _sent(env: Envelope, *, broadcast: bool = False) -> str:
    transport, bot = _transport()
    with patch("ductor_bot.messenger.telegram.transport.send_rich", new_callable=AsyncMock) as send:
        await (transport.deliver_broadcast(env) if broadcast else transport.deliver(env))
    if broadcast:
        return str(bot.broadcast.call_args[0][0])
    return str(send.call_args_list[0][0][2])


def _shown_differs(language: str, shown: str, english: str) -> None:
    if language != "en":
        assert shown != english


# -- background sessions and tasks ------------------------------------------------------------


@pytest.mark.parametrize("language", LANGUAGES)
class TestBackgroundNotices:
    async def test_named_session_states(self, language: str) -> None:
        i18n.init(language)
        base = {"origin": Origin.BACKGROUND, "session_name": "debug", "elapsed_seconds": 60.0}
        aborted = await _sent(_env(**base, status="aborted", prompt_preview="p"))
        assert t("notice.session_cancelled", name="debug") in aborted
        complete = await _sent(_env(**base, status="success", result_text="done text"))
        assert t("notice.session_complete", name="debug", elapsed=t("age.s", n="60")) in complete
        assert "done text" in complete
        empty = await _sent(_env(**base, status="success", result_text=""))
        assert t("notice.no_output") in empty
        failed = await _sent(_env(**base, status="error:exit_1", is_error=True, result_text=""))
        assert t("notice.session_failed", name="debug", elapsed=t("age.s", n="60")) in failed
        _shown_differs(
            language, failed, _english("notice.session_failed", name="debug", elapsed="60s")
        )

    async def test_named_session_failure_shows_product_text_translated_and_other_text_as_is(
        self, language: str
    ) -> None:
        i18n.init(language)
        base = {"origin": Origin.BACKGROUND, "session_name": "debug", "is_error": True}
        product = await _sent(
            _env(**base, status="error:cli_not_found_codex", result_text="[codex CLI not found]")
        )
        assert t("run.cli_not_found", provider="codex") in product
        external = await _sent(
            _env(**base, status="error:exit_1", result_text="[codex CLI not found] (from a script)")
        )
        assert "[codex CLI not found] (from a script)" in external

    async def test_stateless_states(self, language: str) -> None:
        i18n.init(language)
        base = {
            "origin": Origin.BACKGROUND,
            "elapsed_seconds": 120.0,
            "prompt_preview": "run tests",
            "metadata": {"task_id": "xyz"},
        }
        cancelled = await _sent(_env(**base, status="aborted"))
        assert t("notice.bg_cancelled") in cancelled
        assert t("notice.bg_cancelled_body", task_id="xyz", prompt="run tests") in cancelled
        failed = await _sent(_env(**base, status="error:exit_2", is_error=True, result_text="boom"))
        assert t("notice.bg_failed", elapsed=t("age.s", n="120")) in failed
        assert (
            t(
                "notice.bg_failed_body",
                task_id="xyz",
                status=t("run.status_exit", code="2"),
                prompt="run tests",
            )
            in failed
        )
        assert "boom" in failed
        _shown_differs(language, failed, _english("notice.bg_failed", elapsed="120s"))
        timed = await _sent(
            _env(
                **base,
                status="error:timeout",
                is_error=True,
                result_text="[cron timed out after 5s]",
            )
        )
        assert t("run.timed_out", label="cron", secs="5") in timed
        done = await _sent(_env(**base, status="success", result_text="all fine"))
        assert t("notice.bg_complete", elapsed=t("age.s", n="120")) in done


# -- delivery failures, inter-agent, provider switch -------------------------------------------


@pytest.mark.parametrize("language", LANGUAGES)
class TestFailureNotices:
    async def test_heartbeat_fallback(self, language: str) -> None:
        i18n.init(language)
        transport, _bot = _transport()
        error = TelegramAPIError(method=MagicMock(), message="x")
        with patch(
            "ductor_bot.messenger.telegram.transport.send_rich",
            new_callable=AsyncMock,
            side_effect=[error, True],
        ) as send:
            await transport.deliver(
                _env(origin=Origin.HEARTBEAT, chat_id=5, topic_id=9, result_text="alert text")
            )
        sent = send.call_args_list[1][0][2]
        assert sent == t(
            "notice.heartbeat_failed",
            target=t("notice.target_topic", chat=5, topic=9),
            text="alert text",
        )

    async def test_cron_delivery_failure_fallback(self, language: str) -> None:
        i18n.init(language)
        transport, _bot = _transport()
        error = TelegramAPIError(method=MagicMock(), message="x")
        with patch(
            "ductor_bot.messenger.telegram.transport.send_rich",
            new_callable=AsyncMock,
            side_effect=[error, True],
        ) as send:
            await transport.deliver(
                _env(
                    chat_id=5,
                    status="success",
                    result_text="the result",
                    metadata={"title": "Backup"},
                )
            )
        assert send.call_args_list[1][0][2] == t(
            "notice.cron_delivery_failed",
            title="Backup",
            target=t("notice.target", chat=5),
            text="the result",
        )

    async def test_interagent_failure_and_provider_switch(self, language: str) -> None:
        i18n.init(language)
        transport, _bot = _transport()
        failed = _env(
            origin=Origin.INTERAGENT,
            is_error=True,
            session_name="plan",
            prompt_preview="do it",
            metadata={"recipient": "worker", "error": "boom"},
        )
        with patch(
            "ductor_bot.messenger.telegram.transport.send_rich_checked_progress",
            new_callable=AsyncMock,
            return_value=(True, 1),
        ) as send:
            await transport.deliver(failed)
        assert send.call_args[0][2] == t(
            "notice.ia_failed",
            agent="worker",
            session=t("notice.ia_session", name="plan"),
            error="boom",
            request="do it",
        )
        missing = _env(origin=Origin.INTERAGENT, is_error=True, metadata={})
        with patch(
            "ductor_bot.messenger.telegram.transport.send_rich_checked_progress",
            new_callable=AsyncMock,
            return_value=(True, 1),
        ) as send:
            await transport.deliver(missing)
        assert t("notice.unknown") in send.call_args[0][2]
        switched = _env(
            origin=Origin.INTERAGENT,
            result_text="answer",
            metadata={"provider_switch_notice": "claude -> codex"},
        )
        with (
            patch(
                "ductor_bot.messenger.telegram.transport.send_rich", new_callable=AsyncMock
            ) as plain,
            patch(
                "ductor_bot.messenger.telegram.transport.send_rich_checked_progress",
                new_callable=AsyncMock,
                return_value=(True, 1),
            ),
        ):
            await transport.deliver(switched)
        assert plain.call_args[0][2] == t("notice.provider_switch", notice="claude -> codex")


# -- task results and the task reason ---------------------------------------------------------


@pytest.mark.parametrize("language", LANGUAGES)
class TestTaskNotices:
    async def test_states(self, language: str) -> None:
        i18n.init(language)
        done = await _sent(
            _env(
                origin=Origin.TASK_RESULT,
                status="done",
                elapsed_seconds=4.0,
                provider="claude",
                model="opus",
                metadata={"name": "build"},
            )
        )
        assert done == t(
            "notice.task_done", name="build", detail=f"{t('age.s', n='4')}, claude/opus"
        )
        cancelled = await _sent(
            _env(origin=Origin.TASK_RESULT, status="cancelled", metadata={"name": "build"})
        )
        assert cancelled == t("notice.task_cancelled", name="build")
        question = await _sent(
            _env(origin=Origin.TASK_QUESTION, prompt="which one?", metadata={"task_id": "abc"})
        )
        assert question == t("notice.task_question", task_id="abc", question="which one?")

    @pytest.mark.parametrize(("reason", "key", "values"), TASK_REASONS)
    async def test_reason_line(self, language: str, reason: str, key: str, values: dict) -> None:
        i18n.init(language)
        note = await _sent(
            _env(
                origin=Origin.TASK_RESULT,
                status="failed",
                metadata={"name": "build", "error": reason},
            )
        )
        assert note == t("notice.task_failed", name="build", reason=t(key, **values))
        _shown_differs(language, note, _english("notice.task_failed", name="build", reason=reason))

    async def test_reason_with_resume_hint_keeps_the_command_line(self, language: str) -> None:
        i18n.init(language)
        shown = show_task_error(RESUME)
        assert shown == (
            f"{t('task_error.interrupted')}\n\n{t('task_error.resume_hint')}\n"
            'python3 tools/task_tools/resume_task.py abc123 "your follow-up"'
        )
        note = await _sent(
            _env(
                origin=Origin.TASK_RESULT,
                status="failed",
                metadata={"name": "build", "error": RESUME},
            )
        )
        assert 'python3 tools/task_tools/resume_task.py abc123 "your follow-up"' in note

    async def test_other_reasons_are_left_alone(self, language: str) -> None:
        i18n.init(language)
        for other in (
            "OOM",
            "interrupted by restart now",
            "Timeout after 5 minutes",
            "the agent said: CLI error",
        ):
            assert show_task_error(other) == other
        no_reason = await _sent(
            _env(origin=Origin.TASK_RESULT, status="failed", metadata={"name": "build"})
        )
        assert no_reason == t("notice.task_failed", name="build", reason=t("notice.unknown"))


def test_task_result_keeps_the_english_error_for_the_parent_and_the_store() -> None:
    i18n.init("ko")
    entry = TaskEntry(
        task_id="abc123",
        chat_id=1,
        parent_agent="main",
        name="build",
        prompt_preview="p",
        provider="claude",
        model="opus",
        session_id="sid-1",
        status="failed",
    )
    result = TaskHub._interrupted_result(entry)
    assert result.error == RESUME.replace("abc123", entry.task_id)
    envelope = from_task_result(result)
    assert envelope.metadata["error"] == result.error
    assert "interrupted by restart" in envelope.prompt
    assert entry.to_dict().get("error", "") == entry.error


# -- cron, webhook and background results -------------------------------------------------------


@pytest.mark.parametrize("language", LANGUAGES)
class TestRunResults:
    @pytest.mark.parametrize(("status", "text", "key", "values"), RUN_FORMS)
    async def test_product_texts_are_translated_for_the_chat(
        self, language: str, status: str, text: str, key: str, values: dict
    ) -> None:
        i18n.init(language)
        shown = t(key, **values)
        env = _env(status=status, result_text=text, metadata={"title": "Nightly"})
        header = t("notice.task_header", title="Nightly")
        assert await _sent(env) == f"{header}\n\n{shown}"
        assert await _sent(env, broadcast=True) == f"{header}\n\n{shown}"
        hook = _env(
            origin=Origin.WEBHOOK_CRON,
            status=status,
            result_text=text,
            metadata={"hook_title": "Hook"},
        )
        assert await _sent(hook, broadcast=True) == (
            f"{t('notice.webhook_header', title='Hook')}\n\n{shown}"
        )
        assert env.result_text == text  # the stored text is not changed
        assert show_run_result(status, text) == shown

    async def test_other_texts_and_wrong_pairs_are_left_alone(self, language: str) -> None:
        i18n.init(language)
        for status, text in (
            ("success", "[claude CLI not found]"),
            ("error:timeout", "[claude CLI not found]"),
            ("error:cli_not_found_claude", "note: [claude CLI not found]"),
            ("error:exit_1", "the script printed [cron timed out after 5s]"),
        ):
            assert show_run_result(status, text) == text

    async def test_empty_result_shows_a_label_for_known_statuses_only(self, language: str) -> None:
        i18n.init(language)
        known = await _sent(_env(status="error:exit_3", result_text="", metadata={"title": "J"}))
        assert known.endswith(f"_{t('run.status_exit', code='3')}_")
        unknown = await _sent(_env(status="failed", result_text="", metadata={"title": "J"}))
        assert unknown.endswith("_failed_")
        assert show_run_status("success") == t("run.status_success")
        assert show_run_status("error:cli_not_found_gemini") == t("run.status_cli_not_found")


# -- queue indicator, files, media, callbacks, fallback, stream labels ---------------------------


@pytest.mark.parametrize("language", LANGUAGES)
class TestOtherNotices:
    async def test_queue_indicator(self, language: str) -> None:
        from ductor_bot.messenger.telegram.middleware import SequentialMiddleware, _QueueEntry

        i18n.init(language)
        mw = SequentialMiddleware()
        bot = MagicMock()
        bot.send_message = AsyncMock(return_value=MagicMock(message_id=5))
        bot.edit_message_text = AsyncMock()
        mw.set_bot(bot)
        event = MagicMock()
        event.message_id = 1
        event.message_thread_id = None
        event.is_topic_message = False
        entry = _QueueEntry(entry_id=1, chat_id=10, message_id=1, text_preview="x")
        await mw._send_indicator(10, entry, event)
        assert bot.send_message.call_args[0][1] == t("notice.queue_waiting")
        button = bot.send_message.call_args.kwargs["reply_markup"].inline_keyboard[0][0]
        assert button.text == t("notice.queue_cancel_button")
        mw._pending.setdefault(10, []).append(entry)
        await mw.cancel_entry(10, 1)
        assert bot.edit_message_text.call_args.kwargs["text"] == t("notice.queue_cancelled")
        second = _QueueEntry(entry_id=2, chat_id=10, message_id=2, text_preview="y")
        second.indicator_msg_id = 6
        mw._pending.setdefault(10, []).append(second)
        await mw.drain_pending(10)
        assert bot.edit_message_text.call_args.kwargs["text"] == t("notice.queue_discarded")

    async def test_files(self, language: str, tmp_path: Path) -> None:
        from ductor_bot.messenger.telegram.sender import send_file

        i18n.init(language)
        bot = MagicMock()
        bot.send_message = AsyncMock()
        outside = tmp_path / "outside.txt"
        outside.write_text("x")
        await send_file(bot, 1, outside, allowed_roots=[tmp_path / "root"])
        assert bot.send_message.call_args.kwargs["text"] == t(
            "notice.file_outside", name="outside.txt"
        )
        missing = tmp_path / "gone.txt"
        await send_file(bot, 1, missing, allowed_roots=[tmp_path])
        assert bot.send_message.call_args.kwargs["text"] == t(
            "notice.file_not_found", name="gone.txt", path=missing
        )

    async def test_media_download_failure_text_for_the_user_not_for_the_agent(
        self, language: str, tmp_path: Path
    ) -> None:
        from ductor_bot.messenger.telegram import media

        i18n.init(language)
        message = MagicMock()
        message.chat.id = 1
        message.caption = None
        message.answer = AsyncMock()
        with patch.object(media, "download_media", new=AsyncMock(side_effect=OSError("x"))):
            assert await media.resolve_media_text(MagicMock(), message, tmp_path, tmp_path) is None
        assert message.answer.await_args.args[0] == t("notice.download_failed")
        message.caption = "describe this"
        with patch.object(media, "download_media", new=AsyncMock(side_effect=OSError("x"))):
            prompt = await media.resolve_media_text(MagicMock(), message, tmp_path, tmp_path)
        assert prompt == "describe this\n\n[Note: an attached file could not be downloaded.]"

    async def test_answer_mark_and_task_system_notice(self, language: str) -> None:
        from ductor_bot.messenger.callback_router import route_callback
        from ductor_bot.messenger.telegram.callbacks import mark_button_choice
        from ductor_bot.session.key import SessionKey

        i18n.init(language)
        bot = MagicMock()
        bot.edit_message_text = AsyncMock()
        message = MagicMock()
        message.text = "Question"
        message.html_text = "Question"
        message.message_id = 3
        await mark_button_choice(bot, 1, message, "<Yes>")
        assert bot.edit_message_text.call_args.kwargs["text"] == (
            "Question\n\n" + t("notice.user_answer", answer="&lt;Yes&gt;")
        )
        orch = MagicMock()
        orch.task_hub = None
        result = await route_callback(orch, SessionKey(chat_id=1), "tsc:x")
        assert result.text == t("notice.task_system_unavailable")

    async def test_bus_fallback_and_stream_labels(self, language: str) -> None:
        from ductor_bot.messenger.telegram.message_dispatch import _system_label

        i18n.init(language)
        bus = MessageBus()
        telegram = AsyncMock()
        telegram.transport_name = "tg"
        bus.register_transport(telegram)
        await bus.submit(
            Envelope(
                origin=Origin.HEARTBEAT,
                chat_id=1,
                transport="mx",
                delivery=DeliveryMode.UNICAST,
                result_text="alert",
            )
        )
        fallback = telegram.deliver_broadcast.call_args[0][0]
        assert fallback.result_text == t("notice.delivery_fallback", transport="mx", text="alert")
        for status in (
            "thinking",
            "compacting",
            "recovering",
            "timeout_warning",
            "timeout_extended",
        ):
            assert _system_label(status) == t(f"stream.label_{status}")
        assert _system_label("unknown_status") is None
        assert _system_label(None) is None
