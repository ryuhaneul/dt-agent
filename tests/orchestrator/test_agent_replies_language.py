"""Agent replies, crash notices, provider errors and tool labels follow the configured language."""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from rich.console import Console

from ductor_bot import i18n
from ductor_bot.cli.types import AgentResponse
from ductor_bot.cli_commands import agents as agents_console
from ductor_bot.config import AgentConfig
from ductor_bot.i18n import t
from ductor_bot.infra.agent_diagnostics import name_format_error
from ductor_bot.messenger.matrix.buttons import ButtonTracker
from ductor_bot.messenger.matrix.streaming import MatrixStreamEditor
from ductor_bot.messenger.slack.streaming import SlackStreamEditor
from ductor_bot.messenger.telegram.edit_streaming import EditStreamEditor, _ToolTracker
from ductor_bot.messenger.telegram.streaming import StreamEditor
from ductor_bot.multiagent.commands import cmd_agents
from ductor_bot.multiagent.health import AgentHealth
from ductor_bot.multiagent.internal_api import InternalAgentAPI
from ductor_bot.multiagent.supervisor import _MAX_RESTART_RETRIES, AgentSupervisor
from ductor_bot.orchestrator.flows import (
    _finish_normal,
    named_session_flow,
    named_session_streaming,
)
from ductor_bot.session.key import SessionKey
from ductor_bot.text.response_format import (
    normalize_tool_name,
    session_error_text,
    show_cli_error,
)

if TYPE_CHECKING:
    from ductor_bot.orchestrator.core import Orchestrator

LANGUAGES = ["en", "ko"]
USER_ID = "@bot:example.test"
HOMESERVER = "https://matrix.example.test"
SERVER_ERROR = "LoginError: M_FORBIDDEN (Invalid password)"
NO_CREDENTIALS = (
    f"RuntimeError: Matrix AUTH FAILED for {USER_ID}\n"
    "  No access_token, device_id, or password configured.\n"
    "  Set 'password' in the matrix section of config.json."
)
LOGIN_FAILED = (
    f"RuntimeError: Matrix AUTH FAILED for {USER_ID}\n"
    f"  Homeserver: {HOMESERVER}\n"
    f"  Error: {SERVER_ERROR}\n"
    "  Check your credentials in config.json (matrix section)."
)
CRASH_FORMS = {
    "no_credentials": (NO_CREDENTIALS, "crash.matrix_no_credentials", {"user_id": USER_ID}),
    "login_failed": (
        LOGIN_FAILED,
        "crash.matrix_login_failed",
        {"user_id": USER_ID, "homeserver": HOMESERVER, "error": SERVER_ERROR},
    ),
}
CLI_FORMS = [
    ("Codex failed before producing a final response.", "cli_error.codex_no_final", {}),
    ("Unknown Gemini error", "cli_error.gemini_unknown", {}),
    ("Gemini exited with code 3", "cli_error.gemini_exited", {"code": "3"}),
    ("Grok error", "cli_error.grok_error", {}),
    ("CLI error", "task_error.cli_error", {}),
]
EXTERNAL = [
    "rate limit reached, try again later",
    "Codex failed before producing a final response. (see the log)",
    "Gemini exited with code abc",
    "unknown gemini error",
]


@pytest.fixture(autouse=True)
def _english_again() -> Iterator[None]:
    yield
    i18n.init("en")


def _english(key: str, **values: object) -> str:
    i18n.init("en")
    return t(key, **values)


@pytest.fixture
def supervisor(tmp_path: Path) -> AgentSupervisor:
    return AgentSupervisor(
        AgentConfig(
            ductor_home=str(tmp_path),
            telegram_token="main-token",
            allowed_user_ids=[1],
            interagent_port=0,
        )
    )


def _crashed(error: str) -> AgentHealth:
    health = AgentHealth(name="sub1")
    health.mark_crashed(error)
    return health


# -- supervisor replies ---------------------------------------------------------------------


@pytest.mark.parametrize("language", LANGUAGES)
class TestSupervisorReplies:
    async def test_fixed_replies(self, language: str, supervisor: AgentSupervisor) -> None:
        i18n.init(language)
        supervisor._stacks["sub1"] = MagicMock()
        running = await supervisor.start_agent_by_name("sub1")
        assert running == t("supervisor.already_running", name="sub1")
        del supervisor._stacks["sub1"]
        assert await supervisor.start_agent_by_name("ghost") == t(
            "supervisor.not_found", name="ghost"
        )
        assert await supervisor.restart_agent("ghost") == t("supervisor.not_found", name="ghost")
        assert await supervisor.restart_agent("main") == t("supervisor.cannot_restart_main")
        if language == "ko":
            assert running != _english("supervisor.already_running", name="sub1")

    async def test_started_and_restarted(
        self, language: str, supervisor: AgentSupervisor, tmp_path: Path
    ) -> None:
        i18n.init(language)
        (tmp_path / "agents.json").write_text(
            json.dumps([{"name": "sub1", "telegram_token": "tok:1"}])
        )
        with patch.object(supervisor, "_start_sub_agent", new_callable=AsyncMock):
            started = await supervisor.start_agent_by_name("sub1")
            restarted = await supervisor.restart_agent("sub1")
        assert started == t("supervisor.started", name="sub1")
        assert restarted == t("supervisor.restarted", name="sub1")

    async def test_reason_and_frame_share_the_language(
        self, language: str, supervisor: AgentSupervisor
    ) -> None:
        i18n.init(language)
        reason = name_format_error(None)
        supervisor._excluded["sub1"] = reason
        supervisor._excluded["sub2"] = "config error (ValueError)"
        first = await supervisor.start_agent_by_name("sub1")
        assert first == t("supervisor.not_started", name="sub1", reason=reason.shown())
        second = await supervisor.restart_agent("sub2")
        assert second == t(
            "supervisor.not_started", name="sub2", reason="config error (ValueError)"
        )
        notify = AsyncMock()
        with patch.object(supervisor, "_notify_main_agent", notify):
            await supervisor._report_excluded("sub2", "config error (ValueError)")
        notify.assert_awaited_once_with(
            t("supervisor.excluded", name="sub2", reason="config error (ValueError)")
        )
        if language == "ko":
            assert reason.shown() != str(reason)
            assert first != _english("supervisor.not_started", name="sub1", reason=str(reason))

    @pytest.mark.parametrize("form", sorted(CRASH_FORMS))
    async def test_crash_notice(
        self, language: str, form: str, supervisor: AgentSupervisor
    ) -> None:
        full, key, params = CRASH_FORMS[form]
        i18n.init(language)
        health = AgentHealth(name="sub1")
        supervisor._health["sub1"] = health
        notify = AsyncMock()
        with patch.object(supervisor, "_notify_main_agent", notify):
            _, _, done = await supervisor._handle_crash(
                "sub1",
                MagicMock(),
                health,
                _MAX_RESTART_RETRIES + 1,
                RuntimeError(full[len("RuntimeError: ") :]),
            )
        assert done is True
        notice = notify.await_args.args[0]
        assert notice == t(
            "supervisor.stopped_after_crashes",
            name="sub1",
            max=_MAX_RESTART_RETRIES,
            error=t(key, **params),
        )
        assert health.last_crash_error == full
        if language == "ko":
            assert full not in notice

    async def test_external_crash_text_is_kept(
        self, language: str, supervisor: AgentSupervisor
    ) -> None:
        i18n.init(language)
        health = AgentHealth(name="sub1")
        supervisor._health["sub1"] = health
        notify = AsyncMock()
        with patch.object(supervisor, "_notify_main_agent", notify):
            await supervisor._handle_crash(
                "sub1", MagicMock(), health, _MAX_RESTART_RETRIES + 1, ConnectionError("boom")
            )
        assert notify.await_args.args[0] == t(
            "supervisor.stopped_after_crashes",
            name="sub1",
            max=_MAX_RESTART_RETRIES,
            error="ConnectionError: boom",
        )
        assert health.last_crash_error == "ConnectionError: boom"


# -- /agents list and the console ------------------------------------------------------------


def _orch_with(health: dict[str, AgentHealth], mains: set[str]) -> MagicMock:
    orch = MagicMock()
    orch.supervisor.health = health
    stacks = {}
    for name in health:
        stack = MagicMock()
        stack.is_main = name in mains
        stack.config.model = "opus"
        stack.config.reasoning_effort = ""
        stacks[name] = stack
    orch.supervisor.stacks = stacks
    return orch


@pytest.mark.parametrize("language", LANGUAGES)
class TestAgentsList:
    @pytest.mark.parametrize("form", sorted(CRASH_FORMS))
    async def test_crash_line(self, language: str, form: str) -> None:
        full, key, params = CRASH_FORMS[form]
        i18n.init(language)
        health = _crashed(full)
        result = await cmd_agents(
            _orch_with({"sub1": health}, set()), SessionKey(chat_id=1), "/agents"
        )
        line = t("agents.error_line", error=t(key, **params)[:100])
        assert line in result.text
        assert t("agents.restarts", count=1) in result.text
        assert t("agent_status.crashed") in result.text
        assert t("agent_role.sub") in result.text
        assert health.last_crash_error == full
        if language == "ko":
            assert full not in result.text

    async def test_external_crash_text_and_running_agent(self, language: str) -> None:
        i18n.init(language)
        running = AgentHealth(name="main")
        running.mark_running()
        crashed = _crashed("ConnectionError: boom")
        orch = _orch_with({"main": running, "sub1": crashed}, {"main"})
        result = await cmd_agents(orch, SessionKey(chat_id=1), "/agents")
        assert t("agents.error_line", error="ConnectionError: boom") in result.text
        assert f"[{t('agent_role.main')}] \u2014 {t('agent_status.running')}" in result.text
        assert "**main**" in result.text
        assert "**sub1**" in result.text

    @pytest.mark.parametrize("form", sorted(CRASH_FORMS))
    def test_console_crash_line(self, language: str, form: str) -> None:
        full, key, params = CRASH_FORMS[form]
        i18n.init(language)
        live = {
            "sub1": {
                "status": "crashed",
                "uptime": "",
                "restart_count": 1,
                "last_crash_error": full,
            }
        }
        console = Console(file=io.StringIO(), width=400, record=True)
        with (
            patch.object(agents_console, "fetch_live_health", return_value=live),
            patch.object(agents_console, "_console", console),
        ):
            agents_console.print_agents_status([{"name": "sub1"}], bot_running=True)
        shown = console.export_text()
        assert t(key, **params).splitlines()[0][:80].strip() in shown
        if language == "ko":
            assert full.splitlines()[0] not in shown

    async def test_health_api_values_do_not_depend_on_language(self, language: str) -> None:
        health = {"sub1": _crashed(LOGIN_FAILED)}

        async def snapshot() -> dict[str, object]:
            api = InternalAgentAPI(MagicMock(), port=0)
            api.set_health_ref(health)
            response = await api._handle_health(MagicMock())
            return json.loads(response.body)

        i18n.init("en")
        before = await snapshot()
        i18n.init(language)
        await cmd_agents(_orch_with(health, set()), SessionKey(chat_id=1), "/agents")
        after = await snapshot()
        assert after == before
        assert after["agents"]["sub1"]["last_crash_error"] == LOGIN_FAILED
        assert after["agents"]["sub1"]["status"] == "crashed"


# -- provider fixed texts ----------------------------------------------------------------------


@pytest.mark.parametrize("language", LANGUAGES)
class TestShowCliError:
    @pytest.mark.parametrize(("text", "key", "params"), CLI_FORMS)
    def test_fixed_texts(self, language: str, text: str, key: str, params: dict[str, str]) -> None:
        i18n.init(language)
        assert show_cli_error(text) == t(key, **params)
        if language == "ko":
            assert show_cli_error(text) != text

    @pytest.mark.parametrize("text", EXTERNAL)
    def test_other_text_is_kept(self, language: str, text: str) -> None:
        i18n.init(language)
        assert show_cli_error(text) == text

    def test_session_error_text(self, language: str) -> None:
        i18n.init(language)
        fixed = session_error_text("opus", "Codex failed before producing a final response.\nmore")
        assert t("session.error_detail", detail=t("cli_error.codex_no_final")) in fixed
        external = session_error_text("opus", "segmentation fault in the tool")
        assert t("session.error_detail", detail="segmentation fault in the tool") in external

    def test_finish_normal_error_detail(self, language: str) -> None:
        i18n.init(language)
        fixed = _finish_normal(AgentResponse(result="Unknown Gemini error", is_error=True))
        assert fixed.text == t("error.generic", detail=t("cli_error.gemini_unknown"))
        external = _finish_normal(AgentResponse(result="Unknown Gemini error!", is_error=True))
        assert external.text == t("error.generic", detail="Unknown Gemini error!")

    @pytest.mark.parametrize("flow", [named_session_flow, named_session_streaming])
    async def test_named_session_error_detail(
        self, language: str, flow: object, orch: object
    ) -> None:
        i18n.init(language)
        response = AgentResponse(result="Grok error", is_error=True)
        orch._cli_service.execute.return_value = response
        orch._cli_service.execute_streaming.return_value = response
        ns = orch._named_sessions.create(1, "claude", "opus", "Setup")
        orch._named_sessions.update_after_response(1, ns.name, "sid")
        result = await flow(orch, SessionKey(chat_id=1), ns.name, "hello")
        assert t("error.generic", detail=t("cli_error.grok_error")) in result.text
        # The text the agent side keeps stays English.
        assert response.result == "Grok error"


# -- tool names in streaming stay as the tool reports them (or as normalize_tool_name has them) ----


@pytest.mark.parametrize("language", LANGUAGES)
class TestToolNames:
    def test_normalized_names_are_english_in_every_language(self, language: str) -> None:
        i18n.init(language)
        assert normalize_tool_name("Bash") == "Shell"
        assert normalize_tool_name("powershell") == "Shell"
        assert normalize_tool_name("ToolSearch") == "Search"
        assert normalize_tool_name("WebFetch") == "Web fetch"
        assert normalize_tool_name("WebSearch") == "Web search"
        assert normalize_tool_name("mcp__files__read") == "mcp__files__read"
        source = Path(__import__("ductor_bot.api.server", fromlist=["x"]).__file__).read_text(
            encoding="utf-8"
        )
        assert "normalize_tool_name" in source

    async def test_telegram_streaming(self, language: str) -> None:
        i18n.init(language)
        editor = StreamEditor(MagicMock(), 1)
        sent: list[str] = []

        async def capture(text: str, **_kw: object) -> None:
            sent.append(text)

        editor._send = capture  # type: ignore[method-assign]
        await editor.append_tool("Bash")
        await editor.append_tool("mcp__files__read")
        assert sent == ["<b>[TOOL: Shell]</b>", "<b>[TOOL: mcp__files__read]</b>"]

    async def test_telegram_edit_streaming(self, language: str) -> None:
        i18n.init(language)
        tracker = _ToolTracker()
        tracker.add("Bash")
        tracker.add("Bash")
        tracker.add("WebSearch")
        assert tracker.render_html() == "<b>[TOOL: Shell] x2</b>\n<b>[TOOL: Web search]</b>"
        editor = EditStreamEditor(MagicMock(), 1)
        editor._s.fallen_back = True
        editor._send_new = AsyncMock()  # type: ignore[method-assign]
        await editor.append_tool("Bash")
        editor._send_new.assert_awaited_once_with("<b>[TOOL: Shell]</b>")

    async def test_slack_streaming(self, language: str) -> None:
        i18n.init(language)
        editor = SlackStreamEditor(MagicMock(), "C1", thread_ts="1.0")
        assert editor._tool_activity("WebFetch").label == "Web fetch"
        await editor.on_tool("Bash")
        assert "[TOOL: Shell]" in "".join(editor._thinking_parts)

    async def test_matrix_streaming(self, language: str) -> None:
        i18n.init(language)
        editor = MatrixStreamEditor(
            AsyncMock(), "!room:test", send_fn=AsyncMock(), button_tracker=ButtonTracker()
        )
        with patch.object(editor, "_flush_and_tag", new_callable=AsyncMock) as tag:
            await editor.on_tool("Bash")
        tag.assert_awaited_once_with("**[TOOL: Shell]**")


# -- Slack and Telegram reasoning labels, counts of further items, console uptime ---------------


class TestStreamLabels:
    @staticmethod
    def _slack(stream: AsyncMock) -> SlackStreamEditor:
        client = MagicMock()
        client.chat_stream = AsyncMock(return_value=stream)
        return SlackStreamEditor(client, "C1", thread_ts="1.0")

    @pytest.mark.parametrize("language", LANGUAGES)
    async def test_slack_native_thinking_and_more_items(self, language: str) -> None:
        from ductor_bot.cli.stream_events import ToolUseEvent

        i18n.init("en")
        stream = AsyncMock()
        editor = self._slack(stream)
        i18n.init(language)  # the language changes after the editor was made
        await editor.on_thinking("EXTERNAL_REASONING")
        await editor.on_tool(
            ToolUseEvent(
                type="tool_use",
                tool_name="Read",
                parameters={"paths": ["RAW_PATH_A", "RAW_PATH_B"]},
            )
        )
        markdown = [
            c.kwargs["markdown_text"]
            for c in stream.append.await_args_list
            if "markdown_text" in c.kwargs
        ]
        assert markdown[0] == f"\U0001f4ad *{t('stream.slack_label_thinking')}*\nEXTERNAL_REASONING"
        more = t("stream.more_items", count=1)
        assert (
            editor._tool_activity(
                ToolUseEvent(
                    type="tool_use",
                    tool_name="Read",
                    parameters={"paths": ["RAW_PATH_A", "RAW_PATH_B"]},
                )
            ).target
            == f"RAW_PATH_A {more}"
        )
        if language == "ko":
            assert "Thinking" not in markdown[0]
            assert "more" not in more

    @pytest.mark.parametrize("language", LANGUAGES)
    async def test_slack_fallback(self, language: str) -> None:
        from ductor_bot.cli.stream_events import ToolUseEvent

        i18n.init(language)
        editor = self._slack(AsyncMock())
        editor._native_failed = True
        await editor.on_thinking("EXTERNAL_REASONING")
        await editor.on_tool(
            ToolUseEvent(
                type="tool_use",
                tool_name="Read",
                parameters={"paths": ["RAW_PATH_A", "RAW_PATH_B"]},
            )
        )
        sent: list[str] = []

        async def capture(_client: object, _channel: str, text: str, _opts: object) -> None:
            sent.append(text)

        with patch("ductor_bot.messenger.slack.streaming.send_rich", new=capture):
            await editor.finalize("answer")
        assert f"*{t('stream.slack_label_thinking')}*\nEXTERNAL_REASONING" in sent[0]
        assert f"RAW_PATH_A {t('stream.more_items', count=1)}" in sent[0]
        if language == "ko":
            assert "Thinking" not in sent[0]
            assert "more" not in sent[0]

    @pytest.mark.parametrize("language", LANGUAGES)
    def test_telegram_reasoning_chunk(self, language: str) -> None:
        from ductor_bot.messenger.telegram.message_dispatch import _format_reasoning_chunk

        i18n.init(language)
        assert _format_reasoning_chunk(" EXTERNAL_REASONING ") == (
            f"**{t('stream.slack_label_thinking')}**\n\nEXTERNAL_REASONING"
        )
        assert _format_reasoning_chunk("  ") == ""

    @pytest.mark.parametrize("language", LANGUAGES)
    def test_uptime_text_of_the_api(self, language: str) -> None:
        from ductor_bot.text.response_format import show_uptime

        i18n.init(language)
        for text, key, n in (
            ("45s", "age.s", "45"),
            ("2m", "age.m", "2"),
            ("1.5h", "age.h", "1.5"),
            ("2.0d", "age.d", "2.0"),
        ):
            assert show_uptime(text) == t(key, n=n)
        for other in ("", "soon", "5 min", "1h 2m", "1.5H", "-3s"):
            assert show_uptime(other) == other

    @pytest.mark.parametrize("language", LANGUAGES)
    def test_console_uptime(self, language: str) -> None:
        i18n.init(language)
        live = {"sub1": {"status": "running", "uptime": "1.5h", "restart_count": 0}}
        console = Console(file=io.StringIO(), width=400, record=True)
        with (
            patch.object(agents_console, "fetch_live_health", return_value=live),
            patch.object(agents_console, "_console", console),
        ):
            agents_console.print_agents_status([{"name": "sub1"}], bot_running=True)
        assert t("age.h", n="1.5") in console.export_text()


# -- fixed labels of the cron list, the supervisor notice, a topic fallback, a bus error ---------


class TestFixedLabels:
    @staticmethod
    def _job(orch: Orchestrator, job_id: str, **routing: int) -> None:
        from ductor_bot.cron.manager import CronJob

        orch._cron_manager.add_job(
            CronJob(
                id=job_id,
                title=f"RAW_TITLE_{job_id}",
                description="d",
                schedule="0 * * * *",
                task_folder=job_id,
                agent_instruction="run",
                **routing,
            )
        )

    @pytest.mark.parametrize("language", LANGUAGES)
    async def test_cron_routing_line(self, language: str, orch: Orchestrator) -> None:
        from ductor_bot.orchestrator.commands import cmd_cron

        self._job(orch, "chat-only", chat_id=42)
        self._job(orch, "with-topic", chat_id=43, topic_id=7)
        self._job(orch, "unrouted")
        stored = (orch.paths.ductor_home / "cron_jobs.json").read_bytes()
        i18n.init(language)
        text = (await cmd_cron(orch, SessionKey(chat_id=1), "/cron")).text
        assert f"\u2192 {t('notice.target', chat=42)}" in text
        assert f"\u2192 {t('notice.target_topic', chat=43, topic=7)}" in text
        assert "RAW_TITLE_chat-only" in text
        assert "`0 * * * *`" in text
        assert text.count("\u2192") == 2
        assert (orch.paths.ductor_home / "cron_jobs.json").read_bytes() == stored
        if language == "ko":
            assert "Chat " not in text
            assert "Topic " not in text

    @pytest.mark.parametrize("language", LANGUAGES)
    @pytest.mark.parametrize("form", sorted(CRASH_FORMS))
    async def test_supervisor_notice_prefix(
        self, language: str, form: str, supervisor: AgentSupervisor
    ) -> None:
        full, key, params = CRASH_FORMS[form]
        i18n.init(language)
        notify_all = AsyncMock()
        main = MagicMock()
        main.bot.notification_service.notify_all = notify_all
        supervisor._stacks["main"] = main
        health = AgentHealth(name="sub1")
        supervisor._health["sub1"] = health
        await supervisor._handle_crash(
            "sub1",
            MagicMock(),
            health,
            _MAX_RESTART_RETRIES + 1,
            RuntimeError(full[len("RuntimeError: ") :]),
        )
        sent = notify_all.await_args.args[0]
        body = t(
            "supervisor.stopped_after_crashes",
            name="sub1",
            max=_MAX_RESTART_RETRIES,
            error=t(key, **params),
        )
        assert sent == f"**[{t('supervisor.label')}]** {body}"
        assert health.last_crash_error == full
        if language == "ko":
            assert "Supervisor" not in sent
        if language == "en":
            assert sent.startswith("**[Supervisor]** ")

    @pytest.mark.parametrize("language", LANGUAGES)
    async def test_exclusion_notice_prefix_and_external_text(
        self, language: str, supervisor: AgentSupervisor
    ) -> None:
        i18n.init(language)
        notify_all = AsyncMock()
        main = MagicMock()
        main.bot.notification_service.notify_all = notify_all
        supervisor._stacks["main"] = main
        await supervisor._report_excluded("sub2", "EXTERNAL reason (ValueError)")
        sent = notify_all.await_args.args[0]
        assert sent == (
            f"**[{t('supervisor.label')}]** "
            + t("supervisor.excluded", name="sub2", reason="EXTERNAL reason (ValueError)")
        )

    @pytest.mark.parametrize("language", LANGUAGES)
    def test_topic_reset_name_fallback(self, language: str) -> None:
        from ductor_bot.messenger.telegram.topic import TopicNameCache

        i18n.init(language)
        names = TopicNameCache()
        names.set(1, 5, "RAW_TOPIC")
        assert (names.get(1, 5) or t("sessions.topic_fallback", id=5)) == "RAW_TOPIC"
        assert (names.get(1, 6) or t("sessions.topic_fallback", id=6)) == t(
            "sessions.topic_fallback", id=6
        )
        # The cache's own English fallback is for logs and stays English.
        assert names.resolve(1, 6) == "Topic #6"

    @pytest.mark.parametrize("language", LANGUAGES)
    async def test_bus_error_without_text(self, language: str) -> None:
        from ductor_bot.bus.bus import MessageBus
        from ductor_bot.bus.envelope import DeliveryMode, Envelope, Origin

        i18n.init(language)
        bus = MessageBus()
        injector = AsyncMock()
        injector.inject_prompt.side_effect = RuntimeError("boom")
        bus.set_injector(injector)
        transport = AsyncMock()
        transport.transport_name = "tg"
        bus.register_transport(transport)
        env = Envelope(
            origin=Origin.BACKGROUND,
            chat_id=1,
            prompt="p",
            needs_injection=True,
            delivery=DeliveryMode.UNICAST,
            transport="tg",
        )
        await bus.submit(env)
        assert env.is_error
        assert env.result_text == t("notice.processing_error", origin="background")
        if language == "en":
            assert env.result_text == "Error processing background result"


# -- the one-off model hint and the duration unit of task notices ---------------------------------


@pytest.mark.parametrize("language", LANGUAGES)
async def test_model_only_directive_hint_says_it_is_for_one_message(
    language: str, orch: Orchestrator
) -> None:
    i18n.init(language)
    result = await orch.handle_message(SessionKey(chat_id=1), "@opus")
    assert result.text == t("model.next_message", model="opus")
    if language == "en":
        assert result.text == "Send @opus <text> to use opus for that message only."
    else:
        assert "<" in result.text
        assert "@opus" in result.text


class TestTaskDurations:
    @staticmethod
    def _envs() -> tuple[object, object]:
        from ductor_bot.bus.envelope import Envelope, Origin

        session = Envelope(
            origin=Origin.BACKGROUND,
            chat_id=42,
            session_name="research",
            session_id="sid",
            result_text="Found results",
            status="success",
            elapsed_seconds=8.4,
        )
        task = Envelope(
            origin=Origin.TASK_RESULT,
            chat_id=42,
            status="done",
            provider="claude",
            model="sonnet",
            elapsed_seconds=8.4,
            metadata={"name": "RAW_NAME", "task_id": "t1"},
        )
        return session, task

    @pytest.mark.parametrize("language", LANGUAGES)
    async def test_telegram(self, language: str) -> None:
        from ductor_bot.messenger.telegram.transport import TelegramTransport

        i18n.init(language)
        bot = MagicMock()
        bot.file_roots.return_value = [Path("/tmp/roots")]
        transport = TelegramTransport(bot)
        session, task = self._envs()
        sent: list[str] = []

        async def capture(_bot: object, _chat: int, text: str, *_a: object, **_k: object) -> None:
            sent.append(text)

        with patch("ductor_bot.messenger.telegram.transport.send_rich", new=capture):
            await transport.deliver(session)
            await transport.deliver(task)
        unit = t("age.s", n="8")
        assert unit in sent[0]
        assert t("notice.task_done", name="RAW_NAME", detail=f"{unit}, claude/sonnet") in sent[1]
        if language == "en":
            assert "8s" in sent[0]
            assert "(8s, claude/sonnet)" in sent[1]
        else:
            assert "8s" not in sent[0]
            assert "8s," not in sent[1]

    @pytest.mark.parametrize("language", LANGUAGES)
    async def test_matrix_and_slack(self, language: str) -> None:
        from ductor_bot.messenger.matrix.transport import MatrixTransport
        from ductor_bot.messenger.slack.transport import SlackTransport

        i18n.init(language)
        unit = t("age.s", n="8")
        for transport_class, module, resolver, target in (
            (MatrixTransport, "matrix", "_resolve_room", "!room:test"),
            (SlackTransport, "slack", "_resolve_target", ("C1", None)),
        ):
            bot = MagicMock()
            bot.file_roots.return_value = [Path("/tmp/roots")]
            transport = transport_class(bot)
            session, task = self._envs()
            sent: list[str] = []

            async def capture(
                _client: object, _target: object, text: str, *_a: object, out: list[str] = sent
            ) -> None:
                out.append(text)

            with (
                patch.object(transport, resolver, return_value=target),
                patch(f"ductor_bot.messenger.{module}.transport.{module}_send_rich", new=capture),
            ):
                await transport.deliver(session)
                await transport.deliver(task)
            assert unit in sent[0], module
            assert (
                t("notice.task_done", name="RAW_NAME", detail=f"{unit}, claude/sonnet") in sent[1]
            )
            if language == "en":
                assert "(8s, claude/sonnet)" in sent[1]
            else:
                assert "8s" not in sent[0] + sent[1]

    @pytest.mark.parametrize("language", LANGUAGES)
    @pytest.mark.parametrize("status", ["done", "failed"])
    def test_the_prompt_for_the_agent_keeps_the_english_duration(
        self, language: str, status: str
    ) -> None:
        from ductor_bot.bus.adapters import _build_task_injection_prompt
        from ductor_bot.tasks.models import TaskResult

        i18n.init(language)
        result = TaskResult(
            task_id="t1",
            chat_id=1,
            parent_agent="main",
            name="RAW_NAME",
            prompt_preview="p",
            result_text="r",
            status=status,
            elapsed_seconds=8.4,
            provider="claude",
            model="sonnet",
            error="boom",
            original_prompt="do it",
        )
        assert "Duration: 8s" in _build_task_injection_prompt(result)
