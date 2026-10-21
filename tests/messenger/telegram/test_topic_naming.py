"""DM title naming contracts with fake Bot/CLI and synthetic homes only."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from aiogram.types import Chat, Document, ForumTopicCreated, ForumTopicEdited, Message, User

from ductor_bot.cli.auth import AuthResult, AuthStatus
from ductor_bot.cli.types import CLIResponse
from ductor_bot.config import AgentConfig
from ductor_bot.config_reload import ConfigReloader, classify_changes, diff_configs
from ductor_bot.infra import input_context as ic
from ductor_bot.messenger.telegram import topic_naming
from ductor_bot.messenger.telegram.middleware import AuthMiddleware
from ductor_bot.messenger.telegram.topic_naming import clean_topic_title
from ductor_bot.multiagent.delegation import safe_preview
from ductor_bot.orchestrator.registry import OrchestratorResult
from tests.messenger.telegram.test_app import _make_tg_bot
from tests.orchestrator.test_received_inputs import clock as clock


def message(
    *, chat_type="private", topic=42, created=None, edited=None, text="여행 계획을 세워 줘"
):
    return Message(
        message_id=7,
        date=datetime.now(UTC),
        chat=Chat(id=100, type=chat_type),
        from_user=User(id=100, is_bot=False, first_name="Synthetic"),
        is_topic_message=topic is not None,
        message_thread_id=topic,
        text=text,
        forum_topic_created=created,
        forum_topic_edited=edited,
    )


@pytest.fixture
def wired(tmp_path, monkeypatch):
    cfg = AgentConfig(
        telegram_token="test:token",
        ductor_home=str(tmp_path),
        language="ko",
        allowed_user_ids=[100],
        allowed_group_ids=[100],
    )
    tg, bot = _make_tg_bot(cfg)
    bot.edit_forum_topic = AsyncMock()
    session = SimpleNamespace(provider="codex", model="gpt-5.5")
    orch = SimpleNamespace(
        paths=SimpleNamespace(ductor_home=tmp_path, workspace=tmp_path),
        _sessions=SimpleNamespace(get_active=AsyncMock(return_value=session)),
        _process_registry=Mock(),
        handle_message=AsyncMock(return_value=OrchestratorResult(text="첫 답변 요약")),
        run_pre_shutdown_hooks=AsyncMock(),
    )
    tg._orchestrator = orch
    delivered = []

    async def reply(*args, **kwargs):
        delivered.append("reply")
        return "첫 답변 요약"

    tg._handle_non_streaming = AsyncMock(side_effect=reply)
    tg._handle_streaming = AsyncMock(side_effect=reply)
    cfg.streaming.enabled = False
    monkeypatch.setattr(
        topic_naming,
        "check_provider_auth",
        Mock(return_value=AuthResult("codex", AuthStatus.AUTHENTICATED)),
    )
    cli = SimpleNamespace(send=AsyncMock(return_value=CLIResponse(result='"🚀 여행\n계획"')))

    def create(config):
        return cli

    factory = Mock(side_effect=create)
    monkeypatch.setattr(topic_naming, "create_cli", factory)
    return tg, bot, cfg, orch, cli, factory, delivered


async def drain(tg):
    if tg._topic_namer:
        await asyncio.gather(*list(tg._topic_namer._tasks))


async def implicit(tg):
    await tg._on_forum_topic_created(
        message(created=ForumTopicCreated(name="New topic", icon_color=0, is_name_implicit=True))
    )


@pytest.mark.parametrize("streaming", [False, True])
async def test_dm_message_names_once_in_parallel_and_preserves_session(wired, streaming):
    tg, bot, cfg, orch, cli, factory, _ = wired
    cfg.streaming.enabled = streaming
    await implicit(tg)
    before = vars((await orch._sessions.get_active(None))).copy()
    await tg._on_message(message())
    await drain(tg)
    bot.edit_forum_topic.assert_awaited_once_with(
        chat_id=100, message_thread_id=42, name="여행 계획"
    )
    assert tg._topic_names.get(100, 42) == "여행 계획"
    assert vars((await orch._sessions.get_active(None))) == before
    config = factory.call_args.args[0]
    assert config.provider == "codex" and config.model == "gpt-5.5"
    assert config.max_turns == 1 and config.reasoning_effort == "low"
    assert config.working_dir != str(orch.paths.workspace)
    assert config.session_id == "" and config.session_kind == "background"
    args = cli.send.call_args.kwargs
    assert args["resume_session"] is None and args["continue_session"] is False
    assert "language ko" in args["prompt"] and "첫 답변 요약" not in args["prompt"]
    await tg._on_message(message())
    await drain(tg)
    assert bot.edit_forum_topic.await_count == 1
    assert factory.call_count == 1
    saved = json.loads((orch.paths.ductor_home / "dm_topic_naming.json").read_text())
    assert saved == {"topics": {"100:42": "attempted"}, "attempts": {"100:42": 1}}
    tg._topic_namer = None
    await implicit(tg)
    await tg._on_message(message())
    await drain(tg)
    assert bot.edit_forum_topic.await_count == 1


@pytest.mark.parametrize("case", ["custom", "unknown", "edited", "icon", "group", "flat"])
async def test_user_owned_unknown_and_group_topics_are_never_renamed(wired, case):
    tg, bot, _, _, _, factory, _ = wired
    if case == "custom":
        await tg._on_forum_topic_created(
            message(
                created=ForumTopicCreated(name="My title", icon_color=0, is_name_implicit=False)
            )
        )
    elif case not in {"unknown", "group", "flat"}:
        await implicit(tg)
        await tg._on_forum_topic_edited(
            message(
                edited=ForumTopicEdited(
                    name="User title" if case == "edited" else None, icon_custom_emoji_id=""
                )
            )
        )
    elif case == "group":
        await tg._on_forum_topic_created(
            message(
                chat_type="supergroup",
                created=ForumTopicCreated(name="New", icon_color=0, is_name_implicit=True),
            )
        )
    await tg._on_message(
        message(
            chat_type="supergroup" if case == "group" else "private",
            topic=None if case == "flat" else 42,
        )
    )
    await drain(tg)
    bot.edit_forum_topic.assert_not_awaited()
    factory.assert_not_called()


@pytest.mark.parametrize("failure", ["auth", "error", "exception", "timeout", "blank"])
async def test_generation_failure_or_missing_auth_uses_safe_user_prefix(
    wired, monkeypatch, failure
):
    tg, bot, _, _, cli, factory, _ = wired
    if failure == "auth":
        monkeypatch.setattr(
            topic_naming,
            "check_provider_auth",
            Mock(return_value=AuthResult("codex", AuthStatus.INSTALLED)),
        )
    elif failure == "error":
        cli.send.return_value = CLIResponse(is_error=True, result="bad")
    elif failure == "blank":
        cli.send.return_value = CLIResponse(result="🚀")
    elif failure == "exception":
        cli.send.side_effect = RuntimeError("synthetic failure")
    else:

        async def stalled(**kwargs):
            await asyncio.sleep(5)

        cli.send.side_effect = stalled
        monkeypatch.setattr(topic_naming, "_GENERATION_TIMEOUT", 0.01)
    await implicit(tg)
    await tg._on_message(message())
    await drain(tg)
    bot.edit_forum_topic.assert_awaited_once_with(
        chat_id=100, message_thread_id=42, name="여행 계획을 세워 줘"
    )
    if failure == "auth":
        factory.assert_not_called()


async def test_edit_while_generation_pending_wins_without_delaying_reply(wired):
    tg, bot, _, _, cli, _, delivered = wired
    entered, release = asyncio.Event(), asyncio.Event()

    async def generate(**kwargs):
        entered.set()
        await release.wait()
        return CLIResponse(result="Generated")

    cli.send.side_effect = generate
    await implicit(tg)
    await asyncio.wait_for(tg._on_message(message()), 0.5)
    assert delivered == ["reply"]
    await asyncio.wait_for(entered.wait(), 0.5)
    await tg._on_forum_topic_edited(message(edited=ForumTopicEdited(name="User chose this")))
    release.set()
    await drain(tg)
    bot.edit_forum_topic.assert_not_awaited()


async def test_api_error_logs_no_payload_and_retries_only_next_message(wired, caplog):
    tg, bot, _, _, _, _, _ = wired
    bot.edit_forum_topic.side_effect = RuntimeError("synthetic sensitive API payload")
    await implicit(tg)
    with caplog.at_level("INFO", logger=topic_naming.__name__):
        await tg._on_message(message())
        await drain(tg)
    records = [r.getMessage() for r in caplog.records if r.name == topic_naming.__name__]
    assert len(records) == 1 and "chat_id=100 topic_id=42" in records[0]
    assert "sensitive" not in records[0]
    assert bot.edit_forum_topic.await_count == 1
    assert tg._topic_namer._state["100:42"] == "eligible"
    await tg._on_message(message())
    await drain(tg)
    assert bot.edit_forum_topic.await_count == 2


async def test_hot_reload_disable_skips_first_turn_and_does_not_backfill(wired):
    tg, bot, cfg, _, _, factory, _ = wired
    assert cfg.telegram_dm_topic_auto_name
    new = cfg.model_copy(update={"telegram_dm_topic_auto_name": False})
    hot, restart = classify_changes(diff_configs(cfg, new))
    assert hot == {"telegram_dm_topic_auto_name": False} and restart == []
    await implicit(tg)
    reloader = ConfigReloader(
        tg._orch.paths.ductor_home / "config.json", cfg, on_hot_reload=tg._on_auth_hot_reload
    )
    reloader._apply_hot(new, hot)
    await tg._on_message(message())
    cfg.telegram_dm_topic_auto_name = True
    await tg._on_message(message())
    await drain(tg)
    factory.assert_not_called()
    bot.edit_forum_topic.assert_not_awaited()


async def test_disable_during_pending_generation_prevents_edit(wired):
    tg, bot, cfg, _, cli, _, _ = wired
    entered, release = asyncio.Event(), asyncio.Event()

    async def generate(**kwargs):
        entered.set()
        await release.wait()
        return CLIResponse(result="Title")

    cli.send.side_effect = generate
    await implicit(tg)
    await tg._on_message(message())
    await entered.wait()
    cfg.telegram_dm_topic_auto_name = False
    release.set()
    await drain(tg)
    bot.edit_forum_topic.assert_not_awaited()


async def test_sub_bot_uses_its_own_home_and_bot(wired, tmp_path):
    tg, bot, _, orch, _, _, _ = wired
    tg._agent_name = "worker"
    orch.paths.ductor_home = tmp_path / "agents/worker"
    await implicit(tg)
    await tg._on_message(message())
    await drain(tg)
    bot.edit_forum_topic.assert_awaited_once()
    assert (tmp_path / "agents/worker/dm_topic_naming.json").is_file()
    assert not (tmp_path / "dm_topic_naming.json").exists()


async def test_credentials_are_omitted_from_generator_title_state_and_logs(wired, caplog):
    tg, bot, _, orch, cli, _, _ = wired
    secret = "xoxb-" + "1234567890-0987654321-synthetic"
    await implicit(tg)
    await tg._on_message(message(text="Inspect " + secret))
    await drain(tg)
    assert secret not in cli.send.call_args.kwargs["prompt"]
    assert secret not in (orch.paths.ductor_home / "dm_topic_naming.json").read_text()
    assert secret not in caplog.text
    assert secret not in bot.edit_forum_topic.call_args.kwargs["name"]


@pytest.mark.parametrize(
    "text, expected",
    [
        ('"🚀 여행\n계획"', "여행 계획"),
        ("“Trip” 👨‍👩‍👧‍👦 plan", "Trip plan"),
        ("A" * 180, "A" * 24),
        ("a\x00b", "ab"),
        ("token=synthetic", ""),
    ],
)
def test_title_cleanup(text, expected):
    assert clean_topic_title(text) == expected


@pytest.mark.parametrize("streaming", [False, True])
async def test_actual_dispatch_does_not_wait_for_title_call(wired, monkeypatch, streaming):
    from ductor_bot.messenger.telegram.app import TelegramBot

    tg, bot, cfg, orch, _, factory, delivered = wired
    cfg.streaming.enabled = streaming
    tg.file_roots = Mock(return_value=[])
    tg._handle_non_streaming = TelegramBot._handle_non_streaming.__get__(tg)
    tg._handle_streaming = TelegramBot._handle_streaming.__get__(tg)
    orch.handle_message_streaming = AsyncMock(return_value=OrchestratorResult(text="첫 답변 요약"))
    await implicit(tg)

    async def send(*args, **kwargs):
        delivered.append("reply")

    editor = Mock(
        has_content=False,
        append_text=AsyncMock(),
        append_tool=AsyncMock(),
        append_system=AsyncMock(),
        finalize=AsyncMock(),
    )
    with patch("ductor_bot.messenger.telegram.message_dispatch.TypingContext") as typing_ctx:
        typing_ctx.return_value.__aenter__ = AsyncMock()
        typing_ctx.return_value.__aexit__ = AsyncMock()
        monkeypatch.setattr(
            "ductor_bot.messenger.telegram.message_dispatch.send_rich", AsyncMock(side_effect=send)
        )
        monkeypatch.setattr(
            "ductor_bot.messenger.telegram.message_dispatch.create_stream_editor",
            Mock(return_value=editor),
        )
        await tg._on_message(message())
        await drain(tg)
    factory.assert_called_once()
    bot.edit_forum_topic.assert_awaited_once()


async def test_implicit_flag_absent_does_not_establish_eligibility(wired):
    tg, bot, _, _, _, factory, _ = wired
    await tg._on_forum_topic_created(
        message(created=ForumTopicCreated(name="Unknown", icon_color=0))
    )
    await tg._on_message(message())
    await drain(tg)
    factory.assert_not_called()
    bot.edit_forum_topic.assert_not_awaited()


async def test_user_edit_remains_protected_after_restart(wired):
    tg, bot, _, _, _, factory, _ = wired
    await implicit(tg)
    await tg._on_forum_topic_edited(message(edited=ForumTopicEdited(name="Saved user title")))
    tg._topic_namer = None
    await implicit(tg)
    await tg._on_message(message())
    await drain(tg)
    factory.assert_not_called()
    bot.edit_forum_topic.assert_not_awaited()


@pytest.mark.parametrize("case", ["corrupt", "unwritable"])
async def test_unavailable_state_fails_closed_without_affecting_reply(wired, monkeypatch, case):
    tg, bot, _, orch, _, factory, delivered = wired
    path = orch.paths.ductor_home / "dm_topic_naming.json"
    if case == "corrupt":
        path.write_text(json.dumps({"topics": {"100:42": {"invalid": True}}}))
    else:
        monkeypatch.setattr(
            topic_naming, "atomic_json_save", Mock(side_effect=OSError("synthetic"))
        )
    await implicit(tg)
    await tg._on_message(message())
    await drain(tg)
    assert delivered == ["reply"]
    factory.assert_not_called()
    bot.edit_forum_topic.assert_not_awaited()


async def test_sensitive_input_and_generated_secret_use_neutral_fallback(wired):
    tg, bot, _, _, cli, _, _ = wired
    cli.send.return_value = CLIResponse(result="password=synthetic")
    await implicit(tg)
    await tg._on_message(message(text="token=synthetic"))
    await drain(tg)
    bot.edit_forum_topic.assert_awaited_once_with(chat_id=100, message_thread_id=42, name="42")
    assert "token=synthetic" not in cli.send.call_args.kwargs["prompt"]


@pytest.mark.parametrize("provider", ["claude", "gemini"])
async def test_provider_settings_remain_light_and_gemini_key_auth_is_respected(wired, provider):
    tg, _, cfg, orch, _, factory, _ = wired
    session = await orch._sessions.get_active(None)
    session.provider, session.model = (
        provider,
        "sonnet" if provider == "claude" else "gemini-2.5-flash",
    )
    cfg.gemini_api_key = "synthetic-api-key"
    await implicit(tg)
    await tg._on_message(message())
    await drain(tg)
    config = factory.call_args.args[0]
    assert config.provider == provider
    assert config.model == ("haiku" if provider == "claude" else session.model)
    if provider == "gemini":
        topic_naming.check_provider_auth.assert_not_called()


async def test_shutdown_cancels_pending_title_without_retry(wired):
    tg, _, _, orch, cli, _, _ = wired
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def generate(**kwargs):
        entered.set()
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.set()

    cli.send.side_effect = generate
    await implicit(tg)
    await tg._on_message(message())
    await entered.wait()
    orch.shutdown = AsyncMock()
    await asyncio.wait_for(tg.shutdown(), 1)
    assert cancelled.is_set()
    assert not tg._topic_namer._tasks
    assert tg._topic_namer._state["100:42"] == "attempted"


async def test_restart_does_not_trust_old_unconsumed_implicit_name(wired):
    tg, bot, _, _, _, factory, _ = wired
    await implicit(tg)
    tg._topic_namer = None
    # The bot could have missed a user edit while offline; a replayed creation
    # event must not restore certainty or overwrite that user's current name.
    await implicit(tg)
    await tg._on_message(message())
    await drain(tg)
    factory.assert_not_called()
    bot.edit_forum_topic.assert_not_awaited()


def incoming(text="시스템 점검 해봐", *, topic=42, number=8, **updates):
    return message(text=text, topic=topic).model_copy(
        update={"message_id": number, **updates}
    )


@pytest.mark.parametrize(
    "value,user_text",
    [
        ("(시스템 점검)", "여행 계획 세워줘"),
        ("[시스템 점검]", "여행 계획 세워줘"),
        ("【Postgres pool 문제】", "여행 계획 세워줘"),
        ("(v2.4 app.py 검토)", "여행 계획 세워줘"),
    ],
)
async def test_wrapped_unsupported_example_is_replaced_by_the_message_prefix(
    wired, value, user_text
):
    tg, bot, _, _, cli, _, _ = wired
    await implicit(tg)
    cli.send.return_value = CLIResponse(
        result=json.dumps({"title": value}, ensure_ascii=False)
    )
    await tg._on_message(incoming(user_text))
    await drain(tg)
    # The unsupported example never reaches the API; the message prefix does instead.
    bot.edit_forum_topic.assert_awaited_once_with(
        chat_id=100, message_thread_id=42, name=user_text
    )


@pytest.mark.parametrize("total", [2, 3, 4, 5])
async def test_valid_inflight_first_title_survives_message_burst(wired, total):
    tg, bot, _, _, cli, _, _ = wired
    started, release = asyncio.Event(), asyncio.Event()

    async def generate(**kwargs):
        started.set()
        await release.wait()
        return CLIResponse(result='{"title":"시스템 점검"}')

    cli.send.side_effect = generate
    await implicit(tg)
    await tg._on_message(incoming())
    await asyncio.wait_for(started.wait(), 1)
    for i in range(1, total):
        await tg._on_message(incoming("문서도 검토해 줘", number=8 + i))
    release.set()
    await drain(tg)
    assert cli.send.await_count == 1
    assert tg._topic_namer._attempts["100:42"] <= 3
    bot.edit_forum_topic.assert_awaited_once_with(
        chat_id=100, message_thread_id=42, name="시스템 점검"
    )


async def test_delayed_creation_handler_does_not_revert_renamed_cache(wired, tmp_path):
    tg, bot, _, _, cli, _, _ = wired
    bot.send_message = AsyncMock()
    bot.delete_message = AsyncMock()
    cli.send.return_value = CLIResponse(result='{"title":"시스템 점검"}')
    lock = tg._sequential.get_lock((100, 42))
    await lock.acquire()

    async def handle(event, data):
        if event.forum_topic_created:
            await tg._on_forum_topic_created(event)
        else:
            await tg._on_message(event, **data)

    creation = incoming(
        None,
        number=1,
        forum_topic_created=ForumTopicCreated(
            name="New topic", icon_color=0, is_name_implicit=True
        ),
    )
    tasks = [asyncio.create_task(tg._sequential(handle, creation, {}))]
    try:
        for _ in range(30):
            if tg._topic_namer:
                break
            await asyncio.sleep(0)
        tasks.append(
            asyncio.create_task(tg._sequential(handle, incoming(number=2), {}))
        )
        for _ in range(200):
            if bot.edit_forum_topic.await_count:
                break
            await asyncio.sleep(0.001)
        await drain(tg)
        assert tg._topic_names.get(100, 42) == "시스템 점검"
    finally:
        lock.release()
        await asyncio.gather(*tasks)
    assert bot.edit_forum_topic.await_count == 1
    assert tg._topic_names.get(100, 42) == "시스템 점검"
    from ductor_bot.cli.types import AgentRequest
    from ductor_bot.orchestrator.core import Orchestrator

    desired, stale = tmp_path / "desired", tmp_path / "stale"
    desired.mkdir()
    stale.mkdir()
    routing = SimpleNamespace(
        _config=SimpleNamespace(
            project_roots={"시스템 점검": str(desired), "New topic": str(stale)}
        ),
        _sessions=SimpleNamespace(
            resolve_topic_name=lambda chat, topic: tg._topic_names.get(chat, topic)
        ),
    )
    assert Orchestrator._resolve_request_working_dir(
        routing, AgentRequest(prompt="Synthetic request", chat_id=100, topic_id=42)
    ) == str(desired)


@pytest.mark.parametrize("entrypoint", ["stop", "shutdown"])
async def test_shutdown_blocks_new_title_tasks_while_reaping_old_task(
    wired, entrypoint
):
    tg, bot, _, orch, cli, factory, _ = wired
    first_started, cleanup_started, cleanup_release = (
        asyncio.Event() for _ in range(3)
    )
    second_started, second_release = asyncio.Event(), asyncio.Event()

    async def generate(**kwargs):
        if cli.send.await_count == 1:
            first_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cleanup_started.set()
                await cleanup_release.wait()
                raise
        second_started.set()
        await second_release.wait()
        return CLIResponse(result='{"title":"여행 계획"}')

    cli.send.side_effect = generate
    await implicit(tg)
    await tg._on_message(incoming())
    await asyncio.wait_for(first_started.wait(), 1)
    orch.shutdown = AsyncMock()
    stop = asyncio.create_task(
        tg.shutdown() if entrypoint == "shutdown" else tg._topic_namer.stop()
    )
    try:
        await asyncio.wait_for(cleanup_started.wait(), 1)
        tg._topic_namer.created(100, 43, implicit=True)
        await tg._on_message(incoming("여행 계획 세워줘", topic=43, number=9))
        for _ in range(30):
            await asyncio.sleep(0)
        cleanup_release.set()
        await asyncio.wait_for(stop, 1)
        assert cli.send.await_count == 1, (
            "stop allowed a new provider task after its task snapshot"
        )
        assert not tg._topic_namer._tasks
        assert not Path(factory.call_args.args[0].working_dir).exists()
        bot.edit_forum_topic.assert_not_awaited()
    finally:
        cleanup_release.set()
        await asyncio.gather(stop, return_exceptions=True)
        await tg._topic_namer.stop()
        second_release.set()


@pytest.mark.parametrize(
    "value,expected",
    [
        ('{"title":"시스템 점검"}', "시스템 점검"),
        ('{"title":null}', None),
        ("null", None),
        ('{"title":true}', None),
        ('{"title":["abc"]}', None),
        ('{"title":"abc","extra":1}', None),
        ('```json\n{"title":"abc"}\n```', "abc"),
        ('{"title":"시스템 점검 완료, 이상 없음"}', None),
        ('{"title":"I will investigate"}', None),
        ('{"title":"Postgres pool 문제"}', None),
        ('{"title":"v2.4 app.py 검토"}', None),
        ('{"title":"complete.py 검토"}', "complete.py 검토"),
    ],
)
def test_subject_parser_controls(value, expected):
    assert topic_naming.generated_topic_title(value, "시스템 점검 해봐") == expected


@pytest.mark.parametrize(
    "output",
    [
        "sk-\u200bSYNTHETICKEY123",
        "Bearer\u200b synthetic-secret",
        "xoxb-\u200b1234-FAKE",
    ],
)
@pytest.mark.parametrize("path", ["generated", "fallback"])
async def test_credential_boundary_after_json_and_cleaning(wired, output, path):
    tg, bot, _, orch, cli, _, _ = wired
    await implicit(tg)
    cli.send.return_value = CLIResponse(
        result=(json.dumps({"title": output}) if path == "generated" else "")
    )
    await tg._on_message(incoming(output if path == "fallback" else "安全한 요청"))
    await drain(tg)
    title = bot.edit_forum_topic.await_args.kwargs["name"]
    assert not safe_preview(title).startswith("[request preview omitted:")
    assert title == ("42" if path == "fallback" else "安全한 요청")
    saved = (orch.paths.ductor_home / "dm_topic_naming.json").read_text()
    assert output not in saved and title not in saved.replace('"100:42"', "")


@pytest.mark.parametrize(
    "case",
    ["command", "bot", "no_topic", "group", "callback", "empty_media", "caption"],
)
async def test_reception_route_boundaries(wired, case):
    tg, bot, _, _, cli, factory, _ = wired
    await implicit(tg)
    cli.send.return_value = CLIResponse(result='{"title":"시스템 점검"}')
    event = incoming()
    if case == "command":
        event = incoming("/help")
    elif case == "bot":
        event = incoming(from_user=User(id=100, is_bot=True, first_name="Synthetic"))
    elif case == "no_topic":
        event = incoming(topic=None)
    elif case == "group":
        event = message(chat_type="supergroup", text="시스템 점검 해봐")
    elif case in {"caption", "empty_media"}:
        event = incoming(
            None,
            caption="시스템 점검 해봐" if case == "caption" else None,
            document=Document(
                file_id="synthetic",
                file_unique_id="synthetic",
                file_name="secret-report.txt",
            ),
        )
    if case == "callback":
        await tg._sequential(
            AsyncMock(), event, {"command_callback_id": "synthetic-id"}
        )
    else:
        tg._start_topic_name(event)
    await drain(tg)
    if case == "caption":
        bot.edit_forum_topic.assert_awaited_once()
        assert "secret-report.txt" not in cli.send.await_args.kwargs["prompt"]
    else:
        factory.assert_not_called()
        bot.edit_forum_topic.assert_not_awaited()
    if case == "empty_media":
        assert tg._topic_namer._attempts["100:42"] == 1


async def test_auth_prevents_received_hook_for_unauthorized_user(wired):
    tg, bot, _, _, cli, factory, _ = wired
    await implicit(tg)
    event = incoming(from_user=User(id=101, is_bot=False, first_name="Unauthorized"))

    async def after_auth(event, data):
        return await tg._sequential(AsyncMock(), event, data)

    await AuthMiddleware({100})(after_auth, event, {})
    await drain(tg)
    factory.assert_not_called()
    bot.edit_forum_topic.assert_not_awaited()


async def test_title_is_generated_before_busy_lock_without_delaying_reply(wired):
    tg, bot, _, _, cli, _, delivered = wired
    started, release = asyncio.Event(), asyncio.Event()

    async def generate(**kwargs):
        started.set()
        await release.wait()
        return CLIResponse(result='{"title":"시스템 점검"}')

    cli.send.side_effect = generate
    bot.send_message = AsyncMock()
    bot.delete_message = AsyncMock()
    await implicit(tg)
    lock = tg._sequential.get_lock((100, 42))
    await lock.acquire()

    async def handler(event, data):
        await tg._on_message(event, **data)

    turn = asyncio.create_task(tg._sequential(handler, incoming(), {}))
    try:
        await asyncio.wait_for(started.wait(), 0.5)
        assert not turn.done() and delivered == []
    finally:
        lock.release()
        await asyncio.wait_for(turn, 0.5)
        release.set()
        await drain(tg)
    assert delivered == ["reply"]


@pytest.mark.parametrize("value", ["I'll check your system", "시스템을 점검하겠습니다"])
async def test_short_explicit_answers_are_replaced_by_the_message_prefix(wired, value):
    tg, bot, _, _, cli, _, _ = wired
    await implicit(tg)
    cli.send.return_value = CLIResponse(
        result=json.dumps({"title": value}, ensure_ascii=False)
    )
    await tg._on_message(incoming())
    await drain(tg)
    # The answer never reaches the API; the start of the message does instead.
    bot.edit_forum_topic.assert_awaited_once_with(
        chat_id=100, message_thread_id=42, name="시스템 점검 해봐"
    )


@pytest.mark.parametrize("topic_count", [2, 12])
async def test_title_and_dialog_are_independent_with_multiple_topics(
    wired, topic_count
):
    tg, bot, _, _, cli, _, delivered = wired
    await implicit(tg)
    for topic in range(43, 42 + topic_count):
        tg._topic_namer.created(100, topic, implicit=True)
    started, release = asyncio.Event(), asyncio.Event()

    async def generate(**kwargs):
        if cli.send.await_count >= topic_count:
            started.set()
        await release.wait()
        return CLIResponse(result='{"title":"시스템 점검"}')

    cli.send.side_effect = generate
    try:
        await asyncio.wait_for(
            asyncio.gather(
                *(
                    tg._on_message(incoming(topic=topic, number=topic))
                    for topic in range(42, 42 + topic_count)
                )
            ),
            0.5,
        )
        await asyncio.wait_for(started.wait(), 1)
        assert delivered == ["reply"] * topic_count
        assert len(tg._topic_namer._tasks) == topic_count
    finally:
        release.set()
        await drain(tg)
    assert bot.edit_forum_topic.await_count == topic_count
    assert {
        c.kwargs["message_thread_id"] for c in bot.edit_forum_topic.await_args_list
    } == set(range(42, 42 + topic_count))


@pytest.mark.parametrize("state", ["eligible", "attempted", "custom", "seen"])
async def test_legacy_state_does_not_reopen_on_restart(wired, state):
    tg, bot, _, orch, _, factory, _ = wired
    (orch.paths.ductor_home / "dm_topic_naming.json").write_text(
        json.dumps({"topics": {"100:42": state}})
    )
    await implicit(tg)
    await tg._on_message(incoming())
    await drain(tg)
    factory.assert_not_called()
    bot.edit_forum_topic.assert_not_awaited()


async def test_a_null_result_names_the_topic_once(wired):
    tg, bot, _, orch, cli, _, _ = wired
    await implicit(tg)
    cli.send.return_value = CLIResponse(result='{"title":null}')
    for i in range(5):
        await tg._on_message(incoming(number=8 + i))
        await drain(tg)
    # The first null result names the topic from the message; no later message generates.
    assert cli.send.await_count == 1
    bot.edit_forum_topic.assert_awaited_once()
    assert tg._topic_namer._state["100:42"] == "attempted"
    saved = json.loads((orch.paths.ductor_home / "dm_topic_naming.json").read_text())
    assert saved["topics"] == {"100:42": "attempted"}


async def test_observed_edit_before_conversation_lock_suppresses_api(wired):
    tg, bot, _, _, cli, _, _ = wired
    started, release = asyncio.Event(), asyncio.Event()

    async def generate(**kwargs):
        started.set()
        await release.wait()
        return CLIResponse(result='{"title":"시스템 점검"}')

    cli.send.side_effect = generate
    await implicit(tg)
    await tg._on_message(incoming())
    await asyncio.wait_for(started.wait(), 1)
    lock = tg._sequential.get_lock((100, 42))
    await lock.acquire()
    tg._start_topic_name(
        incoming(None, forum_topic_edited=ForumTopicEdited(name="사용자 이름"))
    )
    release.set()
    await drain(tg)
    lock.release()
    bot.edit_forum_topic.assert_not_awaited()


@pytest.mark.parametrize(
    "title,user_text",
    [
        ("(시스템 점검)", "시스템 점검 해봐"),
        ("[시스템 점검]", "시스템 점검 해봐"),
        ("【Postgres pool 문제】", "Postgres pool 문제 조사"),
        ("(v2.4 app.py 검토)", "v2.4 app.py 검토해줘"),
        ("여행 계획", "여행 계획 세워줘"),
        ("check_system.py 검토", "check_system.py 검토해줘"),
        ("시스템 점검 계획", "시스템 점검 계획 세워줘"),
    ],
)
def test_noun_phrases_and_supported_wrapped_examples_remain_valid(title, user_text):
    assert (
        topic_naming.generated_topic_title(
            json.dumps({"title": title}, ensure_ascii=False), user_text
        )
        == title
    )


async def test_user_edit_wins_during_rename_api(wired):
    tg, bot, _, _, cli, _, _ = wired
    started, release = asyncio.Event(), asyncio.Event()

    async def rename(**kwargs):
        started.set()
        await release.wait()

    bot.edit_forum_topic.side_effect = rename
    cli.send.return_value = CLIResponse(result='{"title":"시스템 점검"}')
    await implicit(tg)
    await tg._on_message(incoming())
    await asyncio.wait_for(started.wait(), 1)
    await tg._on_forum_topic_edited(
        incoming(None, forum_topic_edited=ForumTopicEdited(name="User title"))
    )
    release.set()
    await drain(tg)
    assert tg._topic_names.get(100, 42) == "User title"
    assert tg._topic_namer._state["100:42"] == "custom"


async def test_shutdown_without_existing_namer_rejects_late_first_input(wired):
    tg, bot, _, orch, _, factory, _ = wired
    started, release = asyncio.Event(), asyncio.Event()

    async def shutdown(_budget=None):
        started.set()
        await release.wait()

    orch.shutdown = AsyncMock(side_effect=shutdown)
    assert tg._topic_namer is None
    task = asyncio.create_task(tg.shutdown())
    try:
        await asyncio.wait_for(started.wait(), 1)
        await implicit(tg)
        await tg._on_message(incoming())
        await drain(tg)
        factory.assert_not_called()
        bot.edit_forum_topic.assert_not_awaited()
    finally:
        release.set()
        await task


@pytest.mark.parametrize("concurrent_stop", [False, True])
async def test_repeated_stop_or_stop_cancellation_waits_for_owned_cleanup(
    wired, concurrent_stop
):
    tg, bot, _, _, cli, factory, _ = wired
    started, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def generate(**kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleaning.set()
            await release.wait()
            raise

    cli.send.side_effect = generate
    await implicit(tg)
    await tg._on_message(incoming())
    await asyncio.wait_for(started.wait(), 1)
    stopper = asyncio.create_task(tg._topic_namer.stop())
    second = None
    try:
        await asyncio.wait_for(cleaning.wait(), 1)
        if concurrent_stop:
            second = asyncio.create_task(tg._topic_namer.stop())
        else:
            stopper.cancel()
            await asyncio.sleep(0)
            stopper.cancel()
        for _ in range(5):
            await asyncio.sleep(0)
        assert not stopper.done()
        assert Path(factory.call_args.args[0].working_dir).exists()
        release.set()
        results = await asyncio.gather(
            stopper, *([second] if second else []), return_exceptions=True
        )
        if not concurrent_stop:
            assert isinstance(results[0], asyncio.CancelledError)
        assert not tg._topic_namer._tasks
        assert not Path(factory.call_args.args[0].working_dir).exists()
        bot.edit_forum_topic.assert_not_awaited()
    finally:
        release.set()
        await asyncio.gather(
            stopper, *([second] if second else []), return_exceptions=True
        )
        await tg._topic_namer.stop()


@pytest.mark.parametrize(
    "candidate",
    [
        "✅ I'll check your system",
        "시스템을 점검하겠습니다 ✅",
        "I'\u200bll check your system",
        "시스템을 점검하겠습\u200b니다",
    ],
)
async def test_known_short_answer_after_cleaning_is_rejected_at_api(wired, candidate):
    tg, bot, _, _, cli, _, _ = wired
    await implicit(tg)
    cli.send.return_value = CLIResponse(
        result=json.dumps({"title": candidate}, ensure_ascii=False)
    )
    await tg._on_message(message(text="시스템 점검 해봐"))
    await drain(tg)
    observed = [call.kwargs["name"] for call in bot.edit_forum_topic.await_args_list]
    assert observed == ["시스템 점검 해봐"], (
        f"Answer sentence reached final API: {observed!r}"
    )


@pytest.mark.parametrize(
    "title,text",
    [
        ("시스템 점검하기", "시스템 점검 해봐"),
        ("서버 설정 점검", "서버 설정을 점검해줘"),
        ("파일 검사 계획", "파일 검사 계획 세워줘"),
        ("System inspection", "Please inspect my system"),
        ("Checking the system", "Please check the system"),
        ("Testing API responses", "Test API responses"),
        ("Database migration", "Plan a database migration"),
        ("Deploying the service", "Deploy the service"),
        ("check_system.py 검토", "check_system.py를 검토해줘"),
        ("성공.md 검토", "성공.md를 검토해줘"),
        ("complete.py review", "Review complete.py"),
        ("[시스템 점검]", "시스템 점검 해봐"),
        ("《[시스템 점검]》", "시스템 점검 해봐"),
        ("Postgres pool 문제", "Postgres pool 문제 조사"),
        ("(v2.4 app.py 검토)", "v2.4 app.py 검토해줘"),
    ],
)
async def test_normal_noun_phrase_reaches_final_api(wired, title, text):
    tg, bot, _, _, cli, _, _ = wired
    await implicit(tg)
    cli.send.return_value = CLIResponse(
        result=json.dumps({"title": title}, ensure_ascii=False)
    )
    await tg._on_message(message(text=text))
    await drain(tg)
    bot.edit_forum_topic.assert_awaited_once_with(
        chat_id=100, message_thread_id=42, name=title
    )


@pytest.mark.parametrize("total", [4, 8, 24])
async def test_burst_cap_does_not_allow_later_generation_after_the_prefix_name(
    wired, total
):
    tg, bot, _, _, cli, _, _ = wired
    entered, release = asyncio.Event(), asyncio.Event()

    async def generate(**kwargs):
        entered.set()
        await release.wait()
        return CLIResponse(result='{"title":null}')

    cli.send.side_effect = generate
    await implicit(tg)
    await tg._on_message(message(text="실험 계획"))
    await entered.wait()
    for i in range(1, total):
        await tg._on_message(
            message(text="다음 메시지").model_copy(update={"message_id": 100 + i})
        )
    release.set()
    await drain(tg)
    await tg._on_message(message(text="다음 요청"))
    await drain(tg)
    assert cli.send.await_count == 1
    assert tg._topic_namer._attempts["100:42"] == 3
    # The null result named the topic from the first message; nothing generates again.
    assert tg._topic_namer._state["100:42"] == "attempted"
    bot.edit_forum_topic.assert_awaited_once_with(
        chat_id=100, message_thread_id=42, name="실험 계획"
    )


async def test_queued_edit_and_delayed_creation_cannot_restore_older_cache(wired):
    tg, bot, _, _, cli, _, _ = wired
    api_entered, api_release = asyncio.Event(), asyncio.Event()

    async def rename(**kwargs):
        api_entered.set()
        await api_release.wait()

    bot.edit_forum_topic.side_effect = rename
    cli.send.return_value = CLIResponse(result='{"title":"시스템 점검"}')
    await implicit(tg)
    await tg._on_message(message(text="시스템 점검 해봐"))
    await api_entered.wait()
    lock = tg._sequential.get_lock((100, 42))
    await lock.acquire()
    edit = message(text=None, edited=ForumTopicEdited(name="User title")).model_copy(
        update={"message_id": 20}
    )
    tg._start_topic_name(edit)
    assert tg._topic_namer._state["100:42"] == "custom"
    api_release.set()
    await drain(tg)
    assert tg._topic_names.get(100, 42) == "New topic"
    lock.release()
    await tg._on_forum_topic_edited(edit)
    await implicit(tg)
    assert tg._topic_names.get(100, 42) == "User title"
    assert tg._topic_namer._state["100:42"] == "custom"


async def test_recreated_new_id_is_separate_and_old_duplicate_never_reopens(wired):
    tg, bot, _, _, cli, _, _ = wired
    cli.send.return_value = CLIResponse(result='{"title":"여행 계획"}')
    await implicit(tg)
    await tg._on_message(message(text="여행 계획 세워줘"))
    await drain(tg)
    await tg._on_forum_topic_edited(
        message(text=None, edited=ForumTopicEdited(name="User old"))
    )
    await implicit(tg)
    await tg._on_message(message(text="이전 요청"))
    await tg._on_forum_topic_created(
        message(
            topic=43,
            text=None,
            created=ForumTopicCreated(
                name="New topic", icon_color=0, is_name_implicit=True
            ),
        )
    )
    await tg._on_message(message(topic=43, text="여행 계획 세워줘"))
    await drain(tg)
    assert cli.send.await_count == 2
    assert tg._topic_names.get(100, 42) == "User old"
    assert tg._topic_names.get(100, 43) == "여행 계획"
    assert [
        call.kwargs["message_thread_id"]
        for call in bot.edit_forum_topic.await_args_list
    ] == [
        42,
        43,
    ]


@pytest.mark.parametrize("phase", ["generation", "api"])
async def test_cancelled_and_concurrent_stop_reaps_all_owned_tasks(wired, phase):
    tg, bot, _, _, cli, factory, _ = wired
    entered = set()
    cleaning = set()
    release = asyncio.Event()

    async def wait_owned(i):
        entered.add(i)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleaning.add(i)
            await release.wait()
            raise

    async def generate(**kwargs):
        if phase == "generation":
            await wait_owned(len(entered))
        return CLIResponse(result='{"title":"여행 계획"}')

    async def rename(**kwargs):
        await wait_owned(kwargs["message_thread_id"])

    cli.send.side_effect = generate
    if phase == "api":
        bot.edit_forum_topic.side_effect = rename
    await implicit(tg)
    for topic in [43, 44]:
        tg._topic_namer.created(100, topic, implicit=True)
    for topic in [42, 43, 44]:
        await tg._on_message(message(topic=topic, text="여행 계획 세워줘"))
    while len(entered) < 3:
        await asyncio.sleep(0)
    original = asyncio.create_task(tg._topic_namer.stop())
    while len(cleaning) < 3:
        await asyncio.sleep(0)
    original.cancel()
    await asyncio.sleep(0)
    original.cancel()
    repeated = asyncio.create_task(tg._topic_namer.stop())
    for _ in range(10):
        await asyncio.sleep(0)
    assert not original.done() and not repeated.done()
    await tg._on_message(message(topic=45, text="종료 뒤 요청"))
    assert cli.send.await_count == 3
    release.set()
    values = await asyncio.gather(original, repeated, return_exceptions=True)
    assert isinstance(values[0], asyncio.CancelledError) and values[1] is None
    assert not tg._topic_namer._tasks and not tg._topic_namer._pending
    assert all(
        not Path(call.args[0].working_dir).exists() for call in factory.call_args_list
    )


async def test_receipt_and_queue_context_do_not_enter_title_prompt(wired, clock):
    tg, bot, _, _, cli, _, _ = wired
    bot.send_message = AsyncMock()
    bot.delete_message = AsyncMock()
    await implicit(tg)
    lock = tg._sequential.get_lock((100, 42))
    await lock.acquire()

    async def endpoint(event, data):
        await tg._on_message(
            event, topic_naming_started=data.get("topic_naming_started", False)
        )

    async def seq(event, data):
        return await tg._sequential(endpoint, event, data)

    event = message(text="여행 계획 세워줘")
    task = asyncio.create_task(AuthMiddleware({100})(seq, event, {}))
    while not cli.send.await_count:
        await asyncio.sleep(0)
    prompt = cli.send.await_args.kwargs["prompt"]
    assert (
        "[Received:" not in prompt
        and "[Waited:" not in prompt
        and "[Queued user message]" not in prompt
    )
    assert "여행 계획 세워줘" in prompt and "첫 답변 요약" not in prompt
    assert not task.done()
    lock.release()
    await task
    await drain(tg)
    assert ic.current_input_context("UTC") is None


@pytest.mark.parametrize(
    "candidate,user_text",
    [
        ("✅ I'll check your system", "시스템 점검 해봐"),
        ("시스템을 점검하겠습니다 ✅", "시스템 점검 해봐"),
        ("I'\u200bll check your system", "시스템 점검 해봐"),
        ("시스템을 점검하겠습\u200b니다", "시스템 점검 해봐"),
        ("[시스템\u200b 점검]", "여행 계획 세워줘"),
        ("【Postgres pool 문제】", "여행 계획 세워줘"),
        ("(v2.4 app.py 검토)", "여행 계획 세워줘"),
        ("x" * 19 + "하겠습니다" + "x" * 15, "시스템 점검 해봐"),
    ],
)
async def test_final_api_boundary_rechecks_generator_result(
    wired, candidate, user_text
):
    tg, bot, *_ = wired
    await implicit(tg)
    tg._topic_namer._generate = AsyncMock(return_value=candidate)
    await tg._on_message(message(text=user_text))
    await drain(tg)
    # The rejected generator result never reaches the API; the message prefix does instead.
    bot.edit_forum_topic.assert_awaited_once_with(
        chat_id=100, message_thread_id=42, name=user_text
    )
    assert tg._topic_namer._state["100:42"] == "attempted"


@pytest.mark.parametrize(
    "user_text",
    [
        "✅ I'll check your system",
        "시스템을 점검하겠습\u200b니다 ✅",
    ],
)
async def test_final_api_boundary_rechecks_failure_fallback(wired, user_text):
    tg, bot, *_ = wired
    await implicit(tg)
    tg._topic_namer._generate = AsyncMock(side_effect=RuntimeError("synthetic failure"))
    await tg._on_message(message(text=user_text))
    await drain(tg)
    bot.edit_forum_topic.assert_not_awaited()
    assert tg._topic_namer._state["100:42"] == "eligible"


@pytest.mark.parametrize(
    "title,language",
    [
        ("ill.py review", "en"),
        ("Ill.py 검토", "ko"),
        ("Ill health research", "en"),
        ("Ill effects analysis", "en"),
    ],
)
async def test_normal_ill_word_or_filename_is_not_an_answer(wired, title, language):
    tg, bot, config, _, cli, _, _ = wired
    config.language = language
    await implicit(tg)
    cli.send.return_value = CLIResponse(result=json.dumps({"title": title}))
    for i in range(3):
        await tg._on_message(
            message(text="Review " + title).model_copy(update={"message_id": 100 + i})
        )
        await drain(tg)
    actual = [call.kwargs["name"] for call in bot.edit_forum_topic.await_args_list]
    assert actual == [title], (
        f"Normal noun phrase blocked: {title!r}, {tg._topic_namer._state!r}"
    )


@pytest.mark.parametrize(
    "title",
    [
        "Illness research",
        "Illinois travel",
        "illness.py review",
        "i_will.md review",
        "하겠습니다.py 검토",
        "Checking ill.py",
        "System inspection",
        "시스템 점검하기",
        "Postgres pool 문제",
    ],
)
async def test_normal_controls_remain_valid(wired, title):
    tg, bot, _, _, cli, _, _ = wired
    await implicit(tg)
    cli.send.return_value = CLIResponse(result=json.dumps({"title": title}))
    await tg._on_message(message(text="Review " + title))
    await drain(tg)
    bot.edit_forum_topic.assert_awaited_once_with(
        chat_id=100, message_thread_id=42, name=title
    )


@pytest.mark.parametrize(
    "separator",
    [
        "\u200c",
        "\u200d",
        "\u2060",
        "\ufeff",
        "\u180e",
        "\ufe0f",
        "\u20e3",
        "\u0000",
    ],
)
@pytest.mark.parametrize("language", ["en", "ko"])
async def test_other_removed_invisible_characters_do_not_bypass_answer(
    wired, separator, language
):
    tg, bot, _, _, cli, _, _ = wired
    await implicit(tg)
    candidate = (
        "I'" + separator + "ll check your system"
        if language == "en"
        else "시스템을 점검하겠습" + separator + "니다"
    )
    cli.send.return_value = CLIResponse(result=json.dumps({"title": candidate}))
    await tg._on_message(message(text="시스템 점검 해봐"))
    await drain(tg)
    bot.edit_forum_topic.assert_awaited_once_with(
        chat_id=100, message_thread_id=42, name="시스템 점검 해봐"
    )  # the answer is replaced by the start of the message
    assert tg._topic_namer._state["100:42"] == "attempted"


@pytest.mark.parametrize(
    "candidate",
    [
        "I'\u034fll check your system",
        "시스템을 점검하겠습\u034f니다",
        "I＇ll check your system",
        "Ｉ’ll check your system",
        "시스템을 점검하겠습니다！",
        "［시스템 점검］",
    ],
)
async def test_same_known_rules_with_invisible_or_fullwidth_decoration(
    wired, candidate
):
    tg, bot, _, _, cli, _, _ = wired
    await implicit(tg)
    cli.send.return_value = CLIResponse(result=json.dumps({"title": candidate}))
    await tg._on_message(message(text="여행 계획 세워줘"))
    await drain(tg)
    observed = [call.kwargs["name"] for call in bot.edit_forum_topic.await_args_list]
    assert observed == ["여행 계획 세워줘"], (
        f"Known decorated answer/example reached API: {observed!r}"
    )


@pytest.mark.parametrize(
    "candidate",
    [
        "✅ I’LL check your system",
        "시스템을 점검하겠습니다\u200b ✅",
        "（I’\u200bLL check your system）",
        "【시스템을 점검하겠습니다】",
    ],
)
async def test_case_and_supported_outer_decoration_remain_rejected(wired, candidate):
    tg, bot, _, _, cli, _, _ = wired
    await implicit(tg)
    cli.send.return_value = CLIResponse(result=json.dumps({"title": candidate}))
    await tg._on_message(message(text="시스템 점검 해봐"))
    await drain(tg)
    observed = [call.kwargs["name"] for call in bot.edit_forum_topic.await_args_list]
    assert observed == ["시스템 점검 해봐"]


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("safe_prefix", "서버 설정 점검"),
        ("credential", "42"),
        ("unsafe_reply", None),
    ],
)
async def test_fallback_disposition_and_rejection_preserve_retry_budget(
    wired, mode, expected
):
    tg, bot, _, orch, cli, _, _ = wired
    text = {
        "safe_prefix": "서버 설정 점검",
        "credential": "sk-\u200bSYNTHETICKEY123",
        "unsafe_reply": "✅ I'll check your system",
    }[mode]
    await implicit(tg)
    cli.send.return_value = CLIResponse(is_error=True, result="Synthetic failure")
    await tg._on_message(message(text=text))
    await drain(tg)
    if expected is None:
        bot.edit_forum_topic.assert_not_awaited()
        assert tg._topic_namer._state["100:42"] == "eligible"
        cli.send.return_value = CLIResponse(result='{"title":"시스템 점검"}')
        await tg._on_message(message(text="시스템 점검 해봐"))
        await drain(tg)
        bot.edit_forum_topic.assert_awaited_once_with(
            chat_id=100, message_thread_id=42, name="시스템 점검"
        )
        assert tg._topic_namer._attempts["100:42"] == 2
    else:
        bot.edit_forum_topic.assert_awaited_once_with(
            chat_id=100, message_thread_id=42, name=expected
        )
    saved = (orch.paths.ductor_home / "dm_topic_naming.json").read_text()
    assert text not in saved and (
        expected is None or expected not in saved.replace('"100:42"', "")
    )


@pytest.mark.parametrize(
    "title,expected",
    [
        ("시스템 점검", "시스템 점검"),
        ("시스템\u034f 점검", "시스템 점검"),
        ("ｖ２．４ ａｐｐ．ｐｙ 검토", "v2.4 app.py 검토"),
        ("Café 연구", "Café 연구"),
        ("Cafe\u0301 연구", "Café 연구"),
    ],
)
async def test_standardization_preserves_composed_hangul_and_identifiers(
    wired, title, expected
):
    tg, bot, _, _, cli, _, _ = wired
    await implicit(tg)
    cli.send.return_value = CLIResponse(result=json.dumps({"title": title}))
    await tg._on_message(message(text="Review " + title))
    await drain(tg)
    bot.edit_forum_topic.assert_awaited_once_with(
        chat_id=100, message_thread_id=42, name=expected
    )


@pytest.mark.parametrize(
    "candidate",
    [
        "I'll check your system",
        "✅ I'll check your system",
        "시스템을 점검하겠습니다",
        "시스템을 점검하겠습니다 ✅",
    ],
)
async def test_real_output_answers_remain_rejected(wired, candidate):
    tg, bot, _, _, cli, _, _ = wired
    await implicit(tg)
    cli.send.return_value = CLIResponse(result=json.dumps({"title": candidate}))
    await tg._on_message(message(text="시스템 점검 해봐"))
    await drain(tg)
    observed = [call.kwargs["name"] for call in bot.edit_forum_topic.await_args_list]
    assert observed == ["시스템 점검 해봐"]


@pytest.mark.parametrize("mode", ["generated", "fallback"])
async def test_apostrophe_free_ill_is_not_classified_as_contraction(wired, mode):
    tg, bot, _, _, cli, _, _ = wired
    await implicit(tg)
    cli.send.return_value = CLIResponse(
        result=json.dumps({"title": "Ill health research"}), is_error=mode == "fallback"
    )
    await tg._on_message(message(text="Ill health research"))
    await drain(tg)
    bot.edit_forum_topic.assert_awaited_once_with(
        chat_id=100, message_thread_id=42, name="Ill health research"
    )
