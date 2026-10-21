"""Matrix and Slack notices follow the configured language, with the keys Telegram uses."""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ductor_bot import i18n
from ductor_bot.bus.envelope import Envelope, Origin
from ductor_bot.i18n import t
from ductor_bot.text.response_format import show_crash_error
from tests.messenger.matrix.test_transport import _env as matrix_env
from tests.messenger.matrix.test_transport import _make_transport as matrix_transport
from tests.messenger.slack.test_transport import _env as slack_env
from tests.messenger.slack.test_transport import _make_transport as slack_transport

LANGUAGES = ["en", "ko"]
TRANSPORTS = ["matrix", "slack"]
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


@pytest.fixture(autouse=True)
def _english_again() -> Iterator[None]:
    yield
    i18n.init("en")


def _english(key: str, **values: object) -> str:
    i18n.init("en")
    return t(key, **values)


def _make(kind: str) -> tuple[object, object, str]:
    """(transport, bot, dotted path of its plain send function)."""
    if kind == "matrix":
        transport, bot = matrix_transport()
        return transport, bot, "ductor_bot.messenger.matrix.transport.matrix_send_rich"
    transport, bot = slack_transport()
    return transport, bot, "ductor_bot.messenger.slack.transport.slack_send_rich"


def _env(kind: str, **kwargs: object) -> Envelope:
    return (matrix_env if kind == "matrix" else slack_env)(**kwargs)


async def _sent(kind: str, env: Envelope, *, broadcast: bool = False) -> str:
    transport, bot, target = _make(kind)
    with patch(target, new_callable=AsyncMock) as send:
        await (transport.deliver_broadcast(env) if broadcast else transport.deliver(env))
    if broadcast and kind == "slack":
        return str(bot.broadcast.call_args[0][0])
    return str(send.call_args_list[0][0][2])


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("kind", TRANSPORTS)
class TestBackgroundAndTasks:
    async def test_background_states(self, kind: str, language: str) -> None:
        i18n.init(language)
        named = {"origin": Origin.BACKGROUND, "session_name": "debug", "elapsed_seconds": 60.0}
        assert t("notice.session_cancelled", name="debug") in await _sent(
            kind, _env(kind, **named, status="aborted", prompt_preview="p")
        )
        assert t(
            "notice.session_complete", name="debug", elapsed=t("age.s", n="60")
        ) in await _sent(kind, _env(kind, **named, status="success", result_text="done"))
        assert t("notice.no_output") in await _sent(
            kind, _env(kind, **named, status="success", result_text="")
        )
        failed = await _sent(
            kind,
            _env(
                kind,
                **named,
                status="error:cli_not_found_codex",
                is_error=True,
                result_text="[codex CLI not found]",
            ),
        )
        assert t("notice.session_failed", name="debug", elapsed=t("age.s", n="60")) in failed
        assert t("run.cli_not_found", provider="codex") in failed
        stateless = {
            "origin": Origin.BACKGROUND,
            "elapsed_seconds": 120.0,
            "prompt_preview": "run tests",
            "metadata": {"task_id": "xyz"},
        }
        assert t("notice.bg_cancelled_body", task_id="xyz", prompt="run tests") in await _sent(
            kind, _env(kind, **stateless, status="aborted")
        )
        bg_failed = await _sent(
            kind, _env(kind, **stateless, status="error:exit_2", is_error=True, result_text="boom")
        )
        assert t("notice.bg_failed", elapsed=t("age.s", n="120")) in bg_failed
        assert t("run.status_exit", code="2") in bg_failed
        assert "boom" in bg_failed
        assert t("notice.bg_complete", elapsed=t("age.s", n="120")) in await _sent(
            kind, _env(kind, **stateless, status="success", result_text="ok")
        )

    async def test_task_states_and_question(self, kind: str, language: str) -> None:
        i18n.init(language)
        done = await _sent(
            kind,
            _env(
                kind,
                origin=Origin.TASK_RESULT,
                status="done",
                elapsed_seconds=4.0,
                provider="claude",
                model="opus",
                metadata={"name": "build"},
            ),
        )
        assert done == t(
            "notice.task_done", name="build", detail=f"{t('age.s', n='4')}, claude/opus"
        )
        cancelled = await _sent(
            kind,
            _env(kind, origin=Origin.TASK_RESULT, status="cancelled", metadata={"name": "build"}),
        )
        assert cancelled == t("notice.task_cancelled", name="build")
        question = await _sent(
            kind,
            _env(kind, origin=Origin.TASK_QUESTION, prompt="which?", metadata={"task_id": "abc"}),
        )
        assert question == t("notice.task_question", task_id="abc", question="which?")

    @pytest.mark.parametrize(("reason", "key", "values"), TASK_REASONS)
    async def test_task_reason(
        self, kind: str, language: str, reason: str, key: str, values: dict
    ) -> None:
        i18n.init(language)
        note = await _sent(
            kind,
            _env(
                kind,
                origin=Origin.TASK_RESULT,
                status="failed",
                metadata={"name": "build", "error": reason},
            ),
        )
        assert note == t("notice.task_failed", name="build", reason=t(key, **values))
        if language != "en":
            assert note != _english("notice.task_failed", name="build", reason=reason)

    async def test_task_reason_with_resume_hint_and_other_reasons(
        self, kind: str, language: str
    ) -> None:
        i18n.init(language)
        note = await _sent(
            kind,
            _env(
                kind,
                origin=Origin.TASK_RESULT,
                status="failed",
                metadata={"name": "build", "error": RESUME},
            ),
        )
        assert 'python3 tools/task_tools/resume_task.py abc123 "your follow-up"' in note
        assert t("task_error.resume_hint") in note
        other = await _sent(
            kind,
            _env(
                kind,
                origin=Origin.TASK_RESULT,
                status="failed",
                metadata={"name": "build", "error": "OOM"},
            ),
        )
        assert other == t("notice.task_failed", name="build", reason="OOM")
        missing = await _sent(
            kind, _env(kind, origin=Origin.TASK_RESULT, status="failed", metadata={"name": "build"})
        )
        assert missing == t("notice.task_failed", name="build", reason=t("notice.unknown"))


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("kind", TRANSPORTS)
class TestInteragentAndRuns:
    async def test_interagent_failure_and_provider_switch(self, kind: str, language: str) -> None:
        i18n.init(language)
        transport, _bot, _target = _make(kind)
        checked = f"ductor_bot.messenger.{kind}.transport.{kind}_send_rich_checked_progress"
        failed = _env(
            kind,
            origin=Origin.INTERAGENT,
            is_error=True,
            session_name="plan",
            prompt_preview="do it",
            metadata={"recipient": "worker", "error": "boom"},
        )
        returned = (True, "id", 1)
        with patch(checked, new_callable=AsyncMock, return_value=returned) as send:
            await transport.deliver(failed)
        assert send.call_args[0][2] == t(
            "notice.ia_failed",
            agent="worker",
            session=t("notice.ia_session", name="plan"),
            error="boom",
            request="do it",
        )
        switched = _env(
            kind,
            origin=Origin.INTERAGENT,
            result_text="answer",
            metadata={"provider_switch_notice": "claude -> codex"},
        )
        one = f"ductor_bot.messenger.{kind}.transport.{kind}_send_rich_checked"
        with (
            patch(one, new_callable=AsyncMock, return_value=(True, "id")) as notice,
            patch(checked, new_callable=AsyncMock, return_value=returned),
        ):
            await transport.deliver(switched)
        assert notice.call_args[0][2] == t("notice.provider_switch", notice="claude -> codex")

    @pytest.mark.parametrize(("status", "text", "key", "values"), RUN_FORMS)
    async def test_product_run_texts(
        self, kind: str, language: str, *, status: str, text: str, key: str, values: dict
    ) -> None:
        i18n.init(language)
        shown = t(key, **values)
        header = t("notice.task_header", title="Nightly")
        cron = _env(kind, status=status, result_text=text, metadata={"title": "Nightly"})
        assert await _sent(kind, cron) == f"{header}\n\n{shown}"
        hook = _env(
            kind,
            origin=Origin.WEBHOOK_CRON,
            status=status,
            result_text=text,
            metadata={"hook_title": "Hook"},
        )
        expected_hook = f"{t('notice.webhook_header', title='Hook')}\n\n{shown}"
        transport, bot, target = _make(kind)
        with patch(target, new_callable=AsyncMock) as send:
            await transport.deliver_broadcast(hook)
        sent = bot.broadcast.call_args[0][0] if kind == "slack" else send.call_args[0][2]
        assert sent == expected_hook
        assert cron.result_text == text

    async def test_other_texts_and_empty_results(self, kind: str, language: str) -> None:
        i18n.init(language)
        external = _env(
            kind,
            status="error:exit_1",
            result_text="the script printed [claude CLI not found]",
            metadata={"title": "J"},
        )
        assert (await _sent(kind, external)).endswith("the script printed [claude CLI not found]")
        empty = _env(kind, status="error:exit_3", result_text="", metadata={"title": "J"})
        assert (await _sent(kind, empty)).endswith(f"_{t('run.status_exit', code='3')}_")
        unknown = _env(kind, status="failed", result_text="", metadata={"title": "J"})
        assert (await _sent(kind, unknown)).endswith("_failed_")


@pytest.mark.parametrize("language", LANGUAGES)
class TestStreamingHelpAndStartup:
    async def test_matrix_stream_status_label_follows_the_language(self, language: str) -> None:
        pytest.importorskip("nio")
        from ductor_bot.messenger.matrix.streaming import MatrixStreamEditor

        i18n.init(language)
        editor = MatrixStreamEditor(
            MagicMock(), "!r", send_fn=AsyncMock(), button_tracker=MagicMock()
        )
        with patch.object(editor, "_flush_and_tag", new=AsyncMock()) as tag:
            await editor.on_system("compacting")
            await editor.on_system("not-a-status")
        tag.assert_awaited_once_with(f"*[{t('stream.label_compacting')}]*")

    async def test_slack_stream_titles_and_labels_follow_the_language(self, language: str) -> None:
        from ductor_bot.messenger.slack.streaming import SlackStreamEditor, _system_label

        i18n.init("ko")
        korean = SlackStreamEditor(MagicMock(), "C1", thread_ts="1.1")
        i18n.init(language)
        editor = SlackStreamEditor(MagicMock(), "C1", thread_ts="1.1")
        assert editor._plan_tasks["analyze"].title == t("stream.slack_analyze_title")
        assert editor._plan_tasks["tools"].title == t("stream.slack_tools_title")
        assert editor._plan_tasks["respond"].title == t("stream.slack_response_title")
        assert editor._analysis_detail() == t("stream.slack_understanding")
        for status in (
            "thinking",
            "compacting",
            "recovering",
            "timeout_warning",
            "timeout_extended",
        ):
            assert _system_label(status) == t(f"stream.slack_label_{status}")
        assert _system_label("other") is None
        if language == "en":
            assert korean._plan_tasks["analyze"].title != editor._plan_tasks["analyze"].title

    async def test_agent_help_lines(self, language: str) -> None:
        pytest.importorskip("nio")
        from ductor_bot.messenger.matrix.bot import MatrixBot
        from ductor_bot.messenger.slack.bot import SlackBot

        i18n.init(language)
        matrix = SimpleNamespace(_send_rich=AsyncMock())
        await MatrixBot._cmd_agent_commands(matrix, text="", room_id="!r", key=None, event=None)
        shown = str(matrix._send_rich.call_args[0][1])
        for key in ("cmd_list", "cmd_start", "cmd_stop", "cmd_restart"):
            assert t(f"agents.{key}", prefix="!") in shown
        slack = SimpleNamespace(_send_rich=AsyncMock())
        await SlackBot._cmd_agent_commands(slack, text="", channel_id="C", key=None, thread_ts=None)
        shown = str(slack._send_rich.call_args[0][1])
        assert t("agents.slack_explanation") in shown
        for key in ("cmd_list", "cmd_start", "cmd_stop", "cmd_restart"):
            assert t(f"agents.{key}", prefix="/") in shown
        await SlackBot._cmd_session(
            slack, text="/session", channel_id="C", key=None, thread_ts=None
        )
        assert t("session_help.slack_usage") in str(slack._send_rich.call_args[0][1])

    async def test_restart_marker_and_media_failure(self, language: str, tmp_path) -> None:
        from ductor_bot.messenger.matrix import media as matrix_media
        from ductor_bot.messenger.matrix.startup import _consume_restart_marker as matrix_marker
        from ductor_bot.messenger.slack.startup import _consume_restart_marker as slack_marker

        i18n.init(language)
        for marker in (matrix_marker, slack_marker):
            (tmp_path / "restart-requested").write_text("1", encoding="utf-8")
            bot = SimpleNamespace(
                _orchestrator=SimpleNamespace(paths=SimpleNamespace(ductor_home=tmp_path))
            )
            assert marker(bot) == t("startup.restart_marker")
        callback = AsyncMock()
        with patch.object(
            matrix_media, "download_matrix_media", new=AsyncMock(side_effect=OSError("x"))
        ):
            assert (
                await matrix_media.resolve_matrix_media(
                    MagicMock(), MagicMock(), tmp_path, tmp_path, error_callback=callback
                )
                is None
            )
        callback.assert_awaited_once_with(t("notice.download_failed"))


# -- the crash error of a failed Matrix login ----------------------------------------------------

NO_CREDENTIALS = (
    "RuntimeError: Matrix AUTH FAILED for @bot:example.test\n"
    "  No access_token, device_id, or password configured.\n"
    "  Set 'password' in the matrix section of config.json."
)
LOGIN_FAILED = (
    "RuntimeError: Matrix AUTH FAILED for @bot:example.test\n"
    "  Homeserver: https://matrix.example.test\n"
    "  Error: LoginError: M_FORBIDDEN (Invalid password)\n"
    "  Check your credentials in config.json (matrix section)."
)


@pytest.mark.parametrize("language", LANGUAGES)
class TestShowCrashError:
    def test_known_matrix_errors_are_translated_with_the_values_kept(self, language: str) -> None:
        i18n.init(language)
        assert show_crash_error(NO_CREDENTIALS) == t(
            "crash.matrix_no_credentials", user_id="@bot:example.test"
        )
        assert show_crash_error(LOGIN_FAILED) == t(
            "crash.matrix_login_failed",
            user_id="@bot:example.test",
            homeserver="https://matrix.example.test",
            error="LoginError: M_FORBIDDEN (Invalid password)",
        )
        if language != "en":
            assert show_crash_error(NO_CREDENTIALS) != NO_CREDENTIALS

    def test_other_errors_are_left_alone(self, language: str) -> None:
        i18n.init(language)
        for text in (
            "RuntimeError: Matrix AUTH FAILED for @bot:example.test",
            NO_CREDENTIALS + " extra",
            NO_CREDENTIALS.replace("RuntimeError: ", "ValueError: "),
            "ConnectionError: could not reach https://matrix.example.test",
            "",
        ):
            assert show_crash_error(text) == text


async def test_the_login_errors_the_product_raises_have_the_recognised_form(tmp_path) -> None:
    """``credentials.py`` raises exactly the texts ``show_crash_error`` recognises (type unchanged)."""
    from ductor_bot.messenger.matrix.credentials import login_or_restore

    config = SimpleNamespace(
        user_id="@bot:example.test",
        homeserver="https://matrix.example.test",
        password="",
        access_token="",
        device_id="",
    )
    with pytest.raises(RuntimeError) as raised:
        await login_or_restore(MagicMock(), config, tmp_path)
    assert type(raised.value) is RuntimeError
    assert show_crash_error(f"RuntimeError: {raised.value}") == t(
        "crash.matrix_no_credentials", user_id="@bot:example.test"
    )

    config.password = "secret"
    client = MagicMock()
    client.login = AsyncMock(return_value="LoginError: M_FORBIDDEN (Invalid password)")
    with pytest.raises(RuntimeError) as raised:
        await login_or_restore(client, config, tmp_path)
    assert type(raised.value) is RuntimeError
    assert show_crash_error(f"RuntimeError: {raised.value}") == t(
        "crash.matrix_login_failed",
        user_id="@bot:example.test",
        homeserver="https://matrix.example.test",
        error="LoginError: M_FORBIDDEN (Invalid password)",
    )
