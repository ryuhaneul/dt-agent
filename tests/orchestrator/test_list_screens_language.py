"""List screens, /status and /diagnose follow the configured language; stored values stay English."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from ductor_bot import i18n
from ductor_bot.cli.auth import AuthResult, AuthStatus, format_age
from ductor_bot.errors import ProductError
from ductor_bot.i18n import t, t_plural
from ductor_bot.multiagent.health import AgentHealth
from ductor_bot.orchestrator.commands import (
    _build_agent_health_block,
    _build_diagnose_health_block,
    cmd_diagnose,
    cmd_status,
)
from ductor_bot.orchestrator.core import Orchestrator
from ductor_bot.orchestrator.selectors.cron_selector import _last_run_label, cron_selector_start
from ductor_bot.orchestrator.selectors.model_selector import (
    _effort_label,
    _validate_reasoning_effort,
)
from ductor_bot.orchestrator.selectors.session_selector import (
    _format_topic_block,
    session_selector_start,
)
from ductor_bot.orchestrator.selectors.task_selector import _format_entry, handle_task_callback
from ductor_bot.orchestrator.selectors.utils import format_age as selector_age
from ductor_bot.session.key import SessionKey
from ductor_bot.session.manager import SessionData
from ductor_bot.session.named import MAX_SESSIONS_PER_CHAT, NamedSession, NamedSessionRegistry
from ductor_bot.tasks.models import TaskEntry, TaskSubmit
from ductor_bot.tasks.registry import TaskRegistry
from ductor_bot.text.response_format import (
    format_uptime,
    show_agent_role,
    show_agent_status,
    show_auth_status,
    show_session_status,
    show_task_status,
)

LANGUAGES = ["en", "ko"]
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


@pytest.fixture(autouse=True)
def _english_again() -> Iterator[None]:
    yield
    i18n.init("en")


def _english(key: str, **values: object) -> str:
    i18n.init("en")
    return t(key, **values)


# -- the labels ---------------------------------------------------------------------------------


@pytest.mark.parametrize("language", LANGUAGES)
class TestLabels:
    def test_known_values_have_a_label_and_other_values_are_shown_as_they_are(
        self, language: str
    ) -> None:
        i18n.init(language)
        for status in ("running", "waiting", "done", "failed", "cancelled"):
            assert show_task_status(status) == t(f"task_status.{status}")
        for status in ("running", "idle", "ended"):
            assert show_session_status(status) == t(f"session_status.{status}")
        for status in ("running", "starting", "crashed", "stopped"):
            assert show_agent_status(status) == t(f"agent_status.{status}")
        for status in ("authenticated", "installed", "not_found"):
            assert show_auth_status(status) == t(f"auth_status.{status}")
        assert show_agent_role("main") == t("agent_role.main")
        assert show_agent_role("worker") == t("agent_role.sub")
        for other in ("paused", "", "Running"):
            assert show_task_status(other) == other
            assert show_session_status(other) == other
            assert show_agent_status(other) == other
            assert show_auth_status(other) == other

    def test_elapsed_time_units(self, language: str) -> None:
        i18n.init(language)
        assert selector_age(30) == t("age.s", n=30)
        assert selector_age(150) == t("age.m", n=2)
        assert selector_age(7300) == t("age.h", n=2)
        assert selector_age(200000) == t("age.d", n=2)
        assert format_uptime(42) == t("age.s", n="42")
        assert format_uptime(600) == t("age.m", n="10")
        assert format_uptime(9000) == t("age.h", n="2.5")
        assert format_uptime(129600) == t("age.d", n="1.5")
        now = datetime.now(UTC)
        assert format_age(now + timedelta(seconds=5)) == t("age.ago_now")
        assert format_age(now - timedelta(seconds=30)) == t("age.ago_s", n=30)
        assert format_age(now - timedelta(minutes=5)) == t("age.ago_m", n=5)
        assert format_age(now - timedelta(hours=3)) == t("age.ago_h", n=3)
        assert format_age(now - timedelta(days=2)) == t("age.ago_d", n=2)
        if language != "en":
            assert selector_age(150) != _english("age.m", n=2)

    def test_effort_buttons_and_the_invalid_effort_message(self, language: str) -> None:
        i18n.init(language)
        for level in ("none", "minimal", "low", "medium", "high", "xhigh", "max"):
            assert _effort_label(level) == t(f"effort_label.{level}")
        assert _effort_label("custom") == "custom"
        orch = MagicMock()
        orch.models.provider_for.return_value = "claude"
        with patch(
            "ductor_bot.orchestrator.selectors.model_selector._supported_efforts",
            return_value=("low", "high"),
        ):
            message = _validate_reasoning_effort(orch, "opus", "turbo")
        assert message == t(
            "model.invalid_effort", effort="turbo", model="opus", supported="low, high"
        )
        with patch(
            "ductor_bot.orchestrator.selectors.model_selector._supported_efforts", return_value=()
        ):
            assert t("model.no_efforts") in str(_validate_reasoning_effort(orch, "opus", "turbo"))


# -- /tasks: deleting finished tasks asks first ----------------------------------------------------


def _task_hub(tmp_path: Path) -> SimpleNamespace:
    registry = TaskRegistry(registry_path=tmp_path / "tasks.json", tasks_dir=tmp_path / "tasks")
    return SimpleNamespace(registry=registry, cancel=AsyncMock(), cancel_all=AsyncMock())


def _add_task(hub: SimpleNamespace, status: str, *, chat_id: int = 42, name: str = "t") -> str:
    entry = hub.registry.create(
        TaskSubmit(
            chat_id=chat_id,
            prompt="p",
            message_id=1,
            thread_id=None,
            parent_agent="main",
            name=name,
        ),
        "claude",
        "opus",
    )
    if status != "running":
        hub.registry.update_status(entry.task_id, status)
    return entry.task_id


@pytest.mark.parametrize("language", LANGUAGES)
class TestFinishedTaskDeletion:
    async def test_confirmation_comes_first_and_changes_nothing(
        self, language: str, tmp_path: Path
    ) -> None:
        i18n.init(language)
        hub = _task_hub(tmp_path)
        done = _add_task(hub, "done", name="a")
        failed = _add_task(hub, "failed", name="b")
        running = _add_task(hub, "running", name="c")
        _add_task(hub, "done", chat_id=7, name="other chat")

        response = await handle_task_callback(hub, 42, "tsc:cleanup")

        assert t_plural("tasks.cleanup_confirm", 2) in response.text
        assert response.buttons is not None
        assert [[(b.text, b.callback_data) for b in row] for row in response.buttons.rows] == [
            [
                (t("tasks.btn_confirm_delete"), "tsc:cleanup_yes"),
                (t("tasks.btn_cancel_delete"), "tsc:r"),
            ]
        ]
        assert all(len(b.callback_data.encode()) <= 64 for b in response.buttons.rows[0])
        for task_id in (done, failed, running):
            assert hub.registry.get(task_id) is not None
            assert hub.registry.task_folder(task_id).is_dir()
        assert len(hub.registry.list_all()) == 4

    async def test_a_single_task_uses_the_singular_text(
        self, language: str, tmp_path: Path
    ) -> None:
        i18n.init(language)
        hub = _task_hub(tmp_path)
        _add_task(hub, "cancelled")
        response = await handle_task_callback(hub, 42, "tsc:cleanup")
        assert t_plural("tasks.cleanup_confirm", 1) in response.text

    async def test_confirming_deletes_entries_and_folders_of_this_chat_only(
        self, language: str, tmp_path: Path
    ) -> None:
        i18n.init(language)
        hub = _task_hub(tmp_path)
        done = _add_task(hub, "done")
        failed = _add_task(hub, "failed")
        running = _add_task(hub, "running")
        other = _add_task(hub, "done", chat_id=7)
        folders = {task_id: hub.registry.task_folder(task_id) for task_id in (done, failed)}

        response = await handle_task_callback(hub, 42, "tsc:cleanup_yes")

        assert t_plural("tasks.cleaned", 2) in response.text
        for task_id, folder in folders.items():
            assert hub.registry.get(task_id) is None
            assert not folder.exists()
        assert hub.registry.get(running) is not None
        assert hub.registry.get(other) is not None
        assert hub.registry.task_folder(other).is_dir()

    async def test_cancel_returns_to_the_list_without_changes(
        self, language: str, tmp_path: Path
    ) -> None:
        i18n.init(language)
        hub = _task_hub(tmp_path)
        done = _add_task(hub, "done", name="keep me")
        response = await handle_task_callback(hub, 42, "tsc:r")
        assert "keep me" in response.text
        assert hub.registry.get(done) is not None
        assert hub.registry.task_folder(done).is_dir()

    async def test_nothing_to_delete_is_said_without_a_confirmation(
        self, language: str, tmp_path: Path
    ) -> None:
        i18n.init(language)
        hub = _task_hub(tmp_path)
        running = _add_task(hub, "running")
        response = await handle_task_callback(hub, 42, "tsc:cleanup")
        assert t("tasks.nothing_to_clean") in response.text
        assert "tsc:cleanup_yes" not in [
            b.callback_data
            for row in (response.buttons.rows if response.buttons else [])
            for b in row
        ]
        assert hub.registry.get(running) is not None

    async def test_the_list_still_offers_the_old_delete_button(
        self, language: str, tmp_path: Path
    ) -> None:
        i18n.init(language)
        hub = _task_hub(tmp_path)
        _add_task(hub, "done")
        response = await handle_task_callback(hub, 42, "tsc:r")
        assert response.buttons is not None
        callbacks = [b.callback_data for row in response.buttons.rows for b in row]
        assert "tsc:cleanup" in callbacks
        assert "tsc:cleanup_yes" not in callbacks


# -- /tasks, /sessions, /cron ---------------------------------------------------------------------


@pytest.mark.parametrize("language", LANGUAGES)
class TestLists:
    def test_task_line(self, language: str) -> None:
        i18n.init(language)
        now = time.time()
        entry = TaskEntry(
            task_id="abc123",
            chat_id=1,
            parent_agent="main",
            name="Build site",
            prompt_preview="p",
            provider="claude",
            model="opus",
            status="failed",
            created_at=now - 200,
            error="interrupted by restart",
        )
        line = _format_entry(entry, now)
        assert "**Build site**" in line
        assert "claude/opus" in line
        assert f"{t('task_status.failed')} ({t('age.m', n=3)})" in line
        assert t("task_error.interrupted") in line
        done = TaskEntry(
            task_id="x",
            chat_id=1,
            parent_agent="main",
            name="n",
            prompt_preview="p",
            provider="",
            model="",
            status="done",
            elapsed_seconds=12.0,
        )
        assert f"{t('task_status.done')} ({t('age.s', n='12')})" in _format_entry(done, now)
        agent_error = TaskEntry(
            task_id="y",
            chat_id=1,
            parent_agent="main",
            name="n",
            prompt_preview="p",
            provider="",
            model="",
            status="paused",
            error="the agent said: CLI error",
        )
        shown = _format_entry(agent_error, now)
        assert "paused" in shown
        assert "the agent said: CLI error" in shown

    async def test_sessions_screen(self, language: str) -> None:
        i18n.init(language)
        named = NamedSession(
            name="research",
            chat_id=1,
            provider="claude",
            model="opus",
            session_id="s",
            prompt_preview="look it up",
            status="idle",
            created_at=time.time() - 7300,
            message_count=1,
        )
        many = NamedSession(
            name="plan",
            chat_id=1,
            provider="codex",
            model="gpt",
            session_id="s2",
            prompt_preview="plan it",
            status="running",
            created_at=time.time() - 30,
            message_count=3,
        )
        topic = SessionData(chat_id=1, topic_id=7, topic_name=None, provider="claude", model="opus")
        topic.message_count = 1
        orch = MagicMock()
        orch.list_named_sessions.return_value = [named, many]
        orch.list_topic_sessions = AsyncMock(return_value=[topic])
        text = (await session_selector_start(orch, 1)).text
        assert t("sessions.topic_fallback", id=7) in text
        assert t("sessions.msgs_one", count=1) in text
        assert t("sessions.msgs_other", count=3) in text
        assert (
            f"{t('session_status.idle')} ({t('sessions.msgs_one', count=1)}, {t('age.h', n=2)})"
            in text
        )
        assert t("session_status.running") in text
        assert "**research**" in text
        assert "_look it up_" in text
        named_topic = SessionData(
            chat_id=1, topic_id=8, topic_name="My plan", provider="claude", model="opus"
        )
        assert "My plan" in _format_topic_block([named_topic])

    def test_cron_last_run(self, language: str) -> None:
        i18n.init(language)
        job = SimpleNamespace(last_run_status="error:timeout", last_delivery_status="failed")
        assert _last_run_label(job) == (
            t("cron.last_run", status=t("run.status_timeout")) + t("cron.delivery_failed")
        )
        assert _last_run_label(
            SimpleNamespace(last_run_status="success", last_delivery_status="")
        ) == (t("cron.last_run", status=t("run.status_success")))
        assert _last_run_label(SimpleNamespace(last_run_status="odd", last_delivery_status="")) == (
            t("cron.last_run", status="odd")
        )
        assert _last_run_label(SimpleNamespace(last_run_status="", last_delivery_status="")) == ""


# -- /status and /diagnose ------------------------------------------------------------------------


def _health() -> dict[str, AgentHealth]:
    main = AgentHealth(name="main")
    main.mark_running()
    worker = AgentHealth(name="worker")
    worker.mark_running()
    worker.restart_count = 2
    crashed_credentials = AgentHealth(name="mx-a")
    crashed_credentials.mark_crashed(NO_CREDENTIALS)
    crashed_login = AgentHealth(name="mx-b")
    crashed_login.mark_crashed(LOGIN_FAILED)
    crashed_external = AgentHealth(name="ext")
    crashed_external.mark_crashed("ConnectionError: could not reach host")
    stopped = AgentHealth(name="idle")
    return {
        h.name: h
        for h in (main, worker, crashed_credentials, crashed_login, crashed_external, stopped)
    }


@pytest.mark.parametrize("language", LANGUAGES)
class TestStatusAndDiagnose:
    def test_agent_blocks(self, language: str, orch: Orchestrator) -> None:
        i18n.init(language)
        health = _health()
        orch._supervisor = SimpleNamespace(health=health)
        status = _build_agent_health_block(orch)
        diagnose = _build_diagnose_health_block(orch)
        for block in (status, diagnose):
            assert t("agent_status.crashed") in block
            assert t("agent_status.stopped") in block
            assert "mx-a" in block
            assert "ConnectionError: could not reach host" in block
        assert f"worker — {t('agent_status.running')}" in status
        assert "⟳2" in status
        assert f"`worker` [{t('agent_role.sub')}] — {t('agent_status.running')}" in diagnose
        assert f"`main` [{t('agent_role.main')}]" in diagnose
        assert t("diagnose.restarts", count=2) in diagnose
        for block in (status, diagnose):
            assert t("crash.matrix_no_credentials", user_id="@bot:example.test")[:60] in block
            assert (
                t(
                    "crash.matrix_login_failed",
                    user_id="@bot:example.test",
                    homeserver="https://matrix.example.test",
                    error="x",
                )[:60]
                in block
            )
        assert health["mx-a"].last_crash_error == NO_CREDENTIALS  # the stored value is English
        assert health["mx-b"].status == "crashed"  # and so is the raw status

    async def test_status_screen(self, language: str, orch: Orchestrator) -> None:
        i18n.init(language)
        auth = {
            "claude": AuthResult(
                provider="claude",
                status=AuthStatus.AUTHENTICATED,
                auth_age=datetime.now(UTC) - timedelta(hours=2),
            ),
            "codex": AuthResult(provider="codex", status=AuthStatus.NOT_FOUND),
        }
        with patch("ductor_bot.orchestrator.commands.check_all_auth", return_value=auth):
            text = (await cmd_status(orch, SessionKey(chat_id=1), "/status")).text
        assert f"[claude] {t('auth_status.authenticated')} ({t('age.ago_h', n=2)})" in text
        assert f"[codex] {t('auth_status.not_found')}" in text
        assert t("status.streaming_header") in text
        assert (
            t("status.thinking_indicator", value=t("status.on")) in text
            or t("status.thinking_indicator", value=t("status.off")) in text
        )
        if language != "en":
            assert "Streaming visibility" not in text

    async def test_diagnose_log_read_failure_text(self, language: str, orch: Orchestrator) -> None:
        i18n.init(language)
        unreadable = MagicMock()
        unreadable.is_file.return_value = True
        unreadable.read_text.side_effect = OSError("denied")
        with patch("ductor_bot.orchestrator.commands._resolve_log_path", return_value=unreadable):
            result = await cmd_diagnose(orch, SessionKey(chat_id=1), "/diagnose")
        assert t("diagnose.log_unreadable") in result.text


# -- nothing stored is changed by showing the screens ---------------------------------------------


def _tree_hash(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


async def test_rendering_the_screens_changes_no_stored_file(orch: Orchestrator) -> None:
    home = orch.paths.ductor_home
    samples = {
        "sessions.json": "{}",
        "named_sessions.json": '{"sessions": []}',
        "tasks.json": '{"tasks": []}',
        "cron_jobs.json": '{"jobs": []}',
    }
    for name, body in samples.items():
        (home / name).write_text(body, encoding="utf-8")
    orch._supervisor = SimpleNamespace(health=_health())
    before = _tree_hash(home)
    i18n.init("ko")
    with patch("ductor_bot.orchestrator.commands.check_all_auth", return_value={}):
        await cmd_status(orch, SessionKey(chat_id=1), "/status")
    await cmd_diagnose(orch, SessionKey(chat_id=1), "/diagnose")
    await session_selector_start(orch, 1)
    await cron_selector_start(orch)
    assert _tree_hash(home) == before
    health = orch._supervisor.health
    assert health["worker"].status == "running"
    assert health["mx-a"].last_crash_error == NO_CREDENTIALS


# -- fixed messages as errors ---------------------------------------------------------------------


@pytest.mark.parametrize("language", LANGUAGES)
class TestProductError:
    def test_english_text_and_translated_display(self, language: str, orch: Orchestrator) -> None:
        i18n.init(language)
        orch._observers.background = MagicMock()
        with pytest.raises(ProductError) as missing:
            orch.submit_named_followup_bg(1, "nosuch", "hi", 1, None)
        assert isinstance(missing.value, ValueError)
        assert str(missing.value) == "Session 'nosuch' not found"
        assert missing.value.shown() == t("product_error.session_not_found", name="nosuch")
        if language != "en":
            assert missing.value.shown() != str(missing.value)

    def test_other_kinds(self, language: str, orch: Orchestrator, tmp_path: Path) -> None:
        i18n.init(language)
        registry = NamedSessionRegistry(tmp_path / "named.json")
        for index in range(MAX_SESSIONS_PER_CHAT):
            registry.create(1, "claude", "opus", f"prompt {index}")
        with pytest.raises(ProductError) as too_many:
            registry.create(1, "claude", "opus", "one more")
        assert str(too_many.value).startswith("Too many sessions (")
        assert too_many.value.shown() == t(
            "product_error.too_many_sessions", max=too_many.value.params["max"]
        )
        orch._observers.background = MagicMock()
        ended = NamedSession(
            name="old",
            chat_id=2,
            provider="claude",
            model="opus",
            session_id="s",
            prompt_preview="p",
            status="ended",
            created_at=time.time(),
        )
        busy = NamedSession(
            name="busy",
            chat_id=2,
            provider="claude",
            model="opus",
            session_id="s",
            prompt_preview="p",
            status="running",
            created_at=time.time(),
        )
        orch._named_sessions.get = lambda _chat_id, name: ended if name == "old" else busy
        with pytest.raises(ProductError) as ended_error:
            orch.submit_named_followup_bg(2, "old", "hi", 1, None)
        assert ended_error.value.shown() == t("product_error.session_ended", name="old")
        with pytest.raises(ProductError) as busy_error:
            orch.submit_named_followup_bg(2, "busy", "hi", 1, None)
        assert str(busy_error.value) == "Session 'busy' is still processing"
        assert busy_error.value.shown() == t("product_error.session_busy", name="busy")


# -- replies of the orchestrator and the session start in Telegram ------------------------------


@pytest.mark.parametrize("language", LANGUAGES)
class TestReplies:
    async def test_shutdown_and_internal_error_replies(
        self, language: str, orch: Orchestrator
    ) -> None:
        i18n.init(language)
        key = SessionKey(chat_id=1)
        with patch.object(
            type(orch._inflight_tracker),
            "shutting_down",
            new_callable=PropertyMock,
            return_value=True,
        ):
            assert (await orch.handle_message(key, "hello")).text == t("error.shutting_down")
        with patch.object(orch, "_route_message", new=AsyncMock(side_effect=OSError("x"))):
            assert (await orch.handle_message(key, "hello")).text == t("error.internal")

    async def test_session_start_and_follow_up_texts(self, language: str) -> None:
        from ductor_bot.messenger.telegram.app import TelegramBot

        i18n.init(language)
        orch = MagicMock()
        orch.resolve_session_directive.return_value = None
        orch.config.provider = "claude"
        stub = SimpleNamespace(_orch=orch, _bot=MagicMock(), _build_session_help=lambda: "help")
        message = MagicMock()
        message.chat.id = 1
        message.message_id = 5
        message.message_thread_id = None
        message.is_topic_message = False
        sent: list[str] = []

        async def capture(_bot: object, _chat: int, text: str, _opts: object) -> None:
            sent.append(text)

        with patch("ductor_bot.messenger.telegram.app.send_rich", new=capture):
            message.text = "/session @research go on"
            orch.get_named_session.return_value = SimpleNamespace(provider="claude", model="opus")
            orch.submit_named_followup_bg.return_value = "t1"
            await TelegramBot._on_session(stub, message)
            assert t("session_notice.followup_sent", name="research") in sent[-1]
            assert t("session_notice.task_queued", task_id="t1") in sent[-1]
            orch.submit_named_followup_bg.side_effect = ProductError(
                "session_busy", "Session 'research' is still processing", name="research"
            )
            await TelegramBot._on_session(stub, message)
            assert sent[-1] == t("product_error.session_busy", name="research")
            orch.get_named_session.return_value = SimpleNamespace(provider="codex", model="gpt")
            orch.submit_named_session.return_value = ("t2", "calm")
            message.text = "/session start something"
            await TelegramBot._on_session(stub, message)
            assert t("session_notice.started_title", name="calm") in sent[-1]
            assert (
                t("session_notice.started_body", provider="Codex", model_info=" (gpt)", name="calm")
                in sent[-1]
            )
            orch.submit_named_session.side_effect = ValueError("a plain error")
            await TelegramBot._on_session(stub, message)
            assert sent[-1] == "a plain error"
