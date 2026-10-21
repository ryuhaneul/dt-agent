"""MemoryLintReporter: dedicated topic in DM topic mode, plain DM otherwise (fake Bot only)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ductor_bot.bus.envelope import Envelope, LockMode
from ductor_bot.bus.lock_pool import LockPool
from ductor_bot.config import AgentConfig, MemoryLintConfig
from ductor_bot.i18n import init, t
from ductor_bot.messenger.telegram.memory_lint_report import MemoryLintReporter
from ductor_bot.orchestrator.providers import ProviderManager
from ductor_bot.session.key import SessionKey
from ductor_bot.session.manager import SessionManager

CHAT = 4242


class FakeBot:
    """Just enough of TelegramBot for the reporter."""

    def __init__(self, tmp_path: Path, *, topics: bool, report: str = "topic") -> None:
        self.config = AgentConfig(allowed_user_ids=[CHAT])
        self.config.memory_lint = self.config.memory_lint.model_copy(update={"report": report})
        self.lock_pool = LockPool()
        self.state_path = tmp_path / "memory_lint_state.json"
        self.bot_instance = MagicMock()
        self.bot_instance.get_me = AsyncMock(
            return_value=SimpleNamespace(has_topics_enabled=topics)
        )
        self.created: list[int] = []
        self.names: list[str] = []
        self.bot_instance.edit_forum_topic = AsyncMock()
        self.bot_instance.create_forum_topic = AsyncMock(side_effect=self._create)
        self.sessions_path = tmp_path / "sessions.json"
        self.sessions = SessionManager(sessions_path=self.sessions_path, config=self.config)
        providers = ProviderManager(self.config)
        self.seen: list[dict[str, Any] | None] = []
        self.activity_flags: list[bool] = []
        self._orch = SimpleNamespace(
            paths=SimpleNamespace(memory_lint_state_path=self.state_path),
            handle_message=AsyncMock(side_effect=self._handle),
            _sessions=self.sessions,
            _config=self.config,
            _observers=SimpleNamespace(codex_cache=None),
            _providers=providers,
            resolve_runtime_target=providers.resolve_runtime_target,
        )
        self.submitted: list[Envelope] = []
        self.deliver_ok: list[bool] = []
        self._bus = SimpleNamespace(submit=AsyncMock(side_effect=self._submit))

    def session_target(self, key: SessionKey) -> dict[str, Any] | None:
        """Persisted provider/model/effort of *key*'s session (None when it has none)."""
        if not self.sessions_path.exists():
            return None
        entry = json.loads(self.sessions_path.read_text(encoding="utf-8")).get(key.storage_key)
        if entry is None:
            return None
        return {k: entry.get(k) for k in ("provider", "model", "reasoning_effort")}

    async def _handle(
        self, key: SessionKey, _prompt: str, *, counts_as_activity: bool = True
    ) -> SimpleNamespace:
        self.activity_flags.append(counts_as_activity)
        self.seen.append(self.session_target(key))  # what the turn would run with
        return SimpleNamespace(text="Fixed two things.")

    async def _create(self, **kwargs: Any) -> SimpleNamespace:
        assert kwargs == {"chat_id": CHAT, "name": t("memory_lint.topic_name")}
        self.names.append(kwargs["name"])
        thread = 500 + len(self.created)
        self.created.append(thread)
        return SimpleNamespace(message_thread_id=thread)

    async def _submit(self, env: Envelope) -> None:
        self.submitted.append(env)
        env.delivered = self.deliver_ok.pop(0) if self.deliver_ok else True


def _stored(bot: FakeBot) -> dict[str, Any]:
    return json.loads(bot.state_path.read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _english_after_each_test() -> Any:
    yield
    init("en")


async def test_new_topic_is_named_in_the_current_language(tmp_path: Path) -> None:
    (tmp_path / "en").mkdir()
    english = FakeBot(tmp_path / "en", topics=True)
    await MemoryLintReporter(english).report("p")  # type: ignore[arg-type]
    init("ko")
    (tmp_path / "ko").mkdir()
    korean = FakeBot(tmp_path / "ko", topics=True)
    await MemoryLintReporter(korean).report("p")  # type: ignore[arg-type]

    assert english.names == ["Memory Lint"]
    assert korean.names == ["메모리 점검"]


async def test_language_change_renames_nothing_and_new_topics_use_the_new_language(
    tmp_path: Path,
) -> None:
    bot = FakeBot(tmp_path, topics=True)
    await MemoryLintReporter(bot).report("p")  # type: ignore[arg-type]
    init("ko")
    await MemoryLintReporter(bot).report("p")  # type: ignore[arg-type]  # stored id is reused

    assert bot.names == ["Memory Lint"]
    assert bot.created == [500]
    bot.bot_instance.edit_forum_topic.assert_not_awaited()

    (tmp_path / "state").mkdir()
    fresh = FakeBot(tmp_path / "state", topics=True)  # a chat with no stored topic yet
    await MemoryLintReporter(fresh).report("p")  # type: ignore[arg-type]
    assert fresh.names == ["메모리 점검"]


async def test_stored_topic_id_means_no_create_and_no_rename(tmp_path: Path) -> None:
    first = FakeBot(tmp_path, topics=True)
    await MemoryLintReporter(first).report("p")  # type: ignore[arg-type]
    init("ko")
    second = FakeBot(tmp_path, topics=True)
    await MemoryLintReporter(second).report("p")  # type: ignore[arg-type]

    second.bot_instance.create_forum_topic.assert_not_awaited()
    second.bot_instance.edit_forum_topic.assert_not_awaited()
    assert second.submitted[0].topic_id == 500


async def test_topic_mode_creates_the_topic_once_and_reuses_it(tmp_path: Path) -> None:
    bot = FakeBot(tmp_path, topics=True)
    reporter = MemoryLintReporter(bot)  # type: ignore[arg-type]
    await reporter.report("prompt one")
    await reporter.report("prompt two")

    assert bot.created == [500]  # createForumTopic exactly once
    assert _stored(bot)["report_topic"] == {"chat_id": CHAT, "topic_id": 500}
    keys = [call.args[0] for call in bot._orch.handle_message.await_args_list]
    assert [(k.chat_id, k.topic_id) for k in keys] == [(CHAT, 500), (CHAT, 500)]
    assert bot._orch.handle_message.await_args_list[0].args[1] == "prompt one"
    assert bot.activity_flags == [False, False]  # a lint report is not user activity
    env = bot.submitted[0]
    assert (env.chat_id, env.topic_id, env.result_text) == (CHAT, 500, "Fixed two things.")
    assert env.lock_mode is LockMode.NONE


async def test_topic_id_survives_a_restart(tmp_path: Path) -> None:
    first = FakeBot(tmp_path, topics=True)
    await MemoryLintReporter(first).report("p")  # type: ignore[arg-type]
    second = FakeBot(tmp_path, topics=True)  # new process, same state file
    await MemoryLintReporter(second).report("p")  # type: ignore[arg-type]
    assert second.created == []
    assert second.submitted[0].topic_id == 500


async def test_plain_dm_when_topic_mode_is_off(tmp_path: Path) -> None:
    bot = FakeBot(tmp_path, topics=False)
    await MemoryLintReporter(bot).report("p")  # type: ignore[arg-type]
    assert bot.created == []
    key = bot._orch.handle_message.await_args.args[0]
    assert (key.chat_id, key.topic_id) == (CHAT, None)
    assert bot.submitted[0].topic_id is None


async def test_dm_setting_never_asks_or_creates_a_topic(tmp_path: Path) -> None:
    bot = FakeBot(tmp_path, topics=True, report="dm")
    await MemoryLintReporter(bot).report("p")  # type: ignore[arg-type]
    bot.bot_instance.get_me.assert_not_called()
    assert bot.created == []
    assert bot.submitted[0].topic_id is None


async def test_deleted_topic_is_detected_and_recreated_once(tmp_path: Path) -> None:
    bot = FakeBot(tmp_path, topics=True)
    reporter = MemoryLintReporter(bot)  # type: ignore[arg-type]
    await reporter.report("first")
    bot.deliver_ok = [False, True]  # stored topic 500 is gone; delivery to the new one works
    await reporter.report("second")
    assert bot.created == [500, 501]
    assert [e.topic_id for e in bot.submitted] == [500, 500, 501]
    assert _stored(bot)["report_topic"]["topic_id"] == 501


async def test_double_failure_delivers_the_same_answer_once_as_plain_dm(tmp_path: Path) -> None:
    bot = FakeBot(tmp_path, topics=True)
    reporter = MemoryLintReporter(bot)  # type: ignore[arg-type]
    await reporter.report("first")
    bot.submitted.clear()
    bot._orch.handle_message.reset_mock()
    bot.bot_instance.create_forum_topic = AsyncMock(side_effect=RuntimeError("no rights"))
    bot.deliver_ok = [False, True]  # stored topic gone, re-creation fails, DM works
    await reporter.report("second")
    assert bot._orch.handle_message.await_count == 1  # no extra model turn
    assert bot.bot_instance.create_forum_topic.await_count == 1  # no endless re-creation
    assert [(e.topic_id, e.result_text) for e in bot.submitted] == [
        (500, "Fixed two things."),
        (None, "Fixed two things."),
    ]
    assert "report_topic" not in _stored(bot)


async def test_failure_on_fresh_topic_also_falls_back_to_dm_once(tmp_path: Path) -> None:
    bot = FakeBot(tmp_path, topics=True)
    reporter = MemoryLintReporter(bot)  # type: ignore[arg-type]
    await reporter.report("first")
    bot.submitted.clear()
    bot.deliver_ok = [False, False, True]  # old topic, fresh topic, DM
    await reporter.report("second")
    assert [e.topic_id for e in bot.submitted] == [500, 501, None]
    assert bot._orch.handle_message.await_count == 2  # one turn per report call


async def test_create_failure_falls_back_to_dm(tmp_path: Path) -> None:
    bot = FakeBot(tmp_path, topics=True)
    bot.bot_instance.create_forum_topic = AsyncMock(side_effect=RuntimeError("no rights"))
    await MemoryLintReporter(bot).report("p")  # type: ignore[arg-type]
    assert bot.submitted[0].topic_id is None
    assert not bot.state_path.exists()


async def test_get_me_failure_falls_back_to_dm(tmp_path: Path) -> None:
    bot = FakeBot(tmp_path, topics=True)
    bot.bot_instance.get_me = AsyncMock(side_effect=RuntimeError("network"))
    await MemoryLintReporter(bot).report("p")  # type: ignore[arg-type]
    assert bot.created == []
    assert bot.submitted[0].topic_id is None


async def test_no_allowed_user_means_no_turn(tmp_path: Path) -> None:
    bot = FakeBot(tmp_path, topics=True)
    bot.config.allowed_user_ids = []
    await MemoryLintReporter(bot).report("p")  # type: ignore[arg-type]
    bot._orch.handle_message.assert_not_called()


async def test_empty_answer_is_not_delivered(tmp_path: Path) -> None:
    bot = FakeBot(tmp_path, topics=False)
    bot._orch.handle_message = AsyncMock(return_value=SimpleNamespace(text=""))
    await MemoryLintReporter(bot).report("p")  # type: ignore[arg-type]
    assert bot.submitted == []


def test_startup_wires_the_reporter() -> None:
    import inspect

    from ductor_bot.messenger.telegram import startup

    source = inspect.getsource(startup._run_primary_startup)
    assert "set_memory_lint_report_handler(MemoryLintReporter(bot).report)" in source


# -- configured provider/model/effort for the lint turn ------------------------------------


def _topic_key(topic: int) -> SessionKey:
    return SessionKey(chat_id=CHAT, topic_id=topic)


def _lint_cfg(bot: FakeBot, **fields: Any) -> None:
    bot.config.memory_lint = MemoryLintConfig(**fields)


async def test_topic_mode_applies_the_configured_target_before_the_turn(tmp_path: Path) -> None:
    bot = FakeBot(tmp_path, topics=True)
    _lint_cfg(bot, model="haiku", reasoning_effort="low")
    await MemoryLintReporter(bot).report("p")  # type: ignore[arg-type]
    assert bot.seen == [{"provider": "claude", "model": "haiku", "reasoning_effort": "low"}]


async def test_new_topic_after_recreation_gets_the_target_again(tmp_path: Path) -> None:
    bot = FakeBot(tmp_path, topics=True)
    _lint_cfg(bot, model="haiku")
    reporter = MemoryLintReporter(bot)  # type: ignore[arg-type]
    await reporter.report("first")
    bot.deliver_ok = [False, True]  # topic 500 is gone, 501 is created
    await reporter.report("second")
    await reporter.report("third")  # runs in the re-created topic 501
    assert bot.created == [500, 501]
    target = bot.session_target(_topic_key(501))
    assert target is not None
    assert (target["provider"], target["model"]) == ("claude", "haiku")
    assert bot.seen[-1] == target


async def test_the_configured_target_wins_over_a_model_command(tmp_path: Path) -> None:
    bot = FakeBot(tmp_path, topics=True)
    _lint_cfg(bot, model="haiku")
    reporter = MemoryLintReporter(bot)  # type: ignore[arg-type]
    await reporter.report("first")
    session, _ = await bot.sessions.resolve_session(_topic_key(500), preserve_existing_target=True)
    await bot.sessions.sync_session_target(session, model="sonnet")  # the user's /model
    assert bot.session_target(_topic_key(500))["model"] == "sonnet"  # type: ignore[index]
    await reporter.report("second")
    assert bot.seen[-1]["model"] == "haiku"  # type: ignore[index]


async def test_nothing_configured_leaves_the_topic_session_untouched(tmp_path: Path) -> None:
    bot = FakeBot(tmp_path, topics=True)
    await bot.sessions.resolve_session_target(
        _topic_key(500), provider="claude", model="sonnet", reasoning_effort="high"
    )
    await MemoryLintReporter(bot).report("p")  # type: ignore[arg-type]
    # nothing configured: the pre-existing topic session is not touched
    assert bot.seen[-1] == {"provider": "claude", "model": "sonnet", "reasoning_effort": "high"}


async def test_effort_only_keeps_provider_and_model_of_the_session(tmp_path: Path) -> None:
    bot = FakeBot(tmp_path, topics=True)
    await bot.sessions.resolve_session_target(
        _topic_key(500), provider="claude", model="sonnet", reasoning_effort="high"
    )
    reporter = MemoryLintReporter(bot)  # type: ignore[arg-type]
    reporter._remember_topic(CHAT, 500)
    _lint_cfg(bot, reasoning_effort="low")
    await reporter.report("p")
    assert bot.seen[-1] == {"provider": "claude", "model": "sonnet", "reasoning_effort": "low"}


async def test_plain_dm_session_is_never_changed_and_warns_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    bot = FakeBot(tmp_path, topics=False)
    dm_key = SessionKey(chat_id=CHAT, topic_id=None)
    await bot.sessions.resolve_session_target(
        dm_key, provider="claude", model="sonnet", reasoning_effort="high"
    )
    before = bot.session_target(dm_key)
    _lint_cfg(bot, provider="claude", model="haiku", reasoning_effort="low")
    reporter = MemoryLintReporter(bot)  # type: ignore[arg-type]
    with caplog.at_level("WARNING"):
        await reporter.report("one")
        await reporter.report("two")
    assert bot.session_target(dm_key) == before
    assert bot.seen == [before, before]
    warnings = [r for r in caplog.records if "plain DM session is left unchanged" in r.getMessage()]
    assert len(warnings) == 1


async def test_dm_report_setting_in_topic_capable_chat_leaves_the_dm_alone(
    tmp_path: Path,
) -> None:
    bot = FakeBot(tmp_path, topics=True, report="dm")
    dm_key = SessionKey(chat_id=CHAT, topic_id=None)
    await bot.sessions.resolve_session_target(dm_key, provider="claude", model="sonnet")
    _lint_cfg(bot, report="dm", model="haiku")
    await MemoryLintReporter(bot).report("p")  # type: ignore[arg-type]
    assert bot.seen == [bot.session_target(dm_key)]
    assert bot.session_target(dm_key)["model"] == "sonnet"  # type: ignore[index]


@pytest.mark.parametrize(
    "fields",
    [
        {"model": "not-a-model"},
        {"provider": "bogus-provider", "model": "haiku"},
        {"reasoning_effort": "bogus-effort", "model": "haiku"},
        {"provider": "codex"},  # no default model known without a codex cache
    ],
)
async def test_invalid_values_leave_the_session_and_still_run_the_turn(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, fields: dict[str, Any]
) -> None:
    bot = FakeBot(tmp_path, topics=True)
    await bot.sessions.resolve_session_target(
        _topic_key(500), provider="claude", model="sonnet", reasoning_effort="high"
    )
    reporter = MemoryLintReporter(bot)  # type: ignore[arg-type]
    reporter._remember_topic(CHAT, 500)
    _lint_cfg(bot, **fields)
    with caplog.at_level("WARNING"):
        await reporter.report("p")
    assert bot.seen[-1] == {"provider": "claude", "model": "sonnet", "reasoning_effort": "high"}
    assert bot._orch.handle_message.await_count == 1
    assert any("configured model not applied" in r.getMessage() for r in caplog.records)


# -- the report session follows a re-created topic ---------------------------------------------


async def _seed_report_session(bot: FakeBot, topic_id: int, sid: str = "lint-sid") -> SessionKey:
    key = SessionKey(chat_id=CHAT, topic_id=topic_id)
    session, _ = await bot.sessions.resolve_session(key, provider="claude", model="opus")
    session.session_id = sid
    await bot.sessions.update_session(session, counts_as_activity=False)
    return key


async def test_a_recreated_topic_keeps_the_report_session(tmp_path: Path) -> None:
    bot = FakeBot(tmp_path, topics=True)
    reporter = MemoryLintReporter(bot)  # type: ignore[arg-type]
    await reporter.report("first")  # creates topic 500
    old = await _seed_report_session(bot, 500)
    bot.deliver_ok = [False, True]  # the stored topic is gone; the new one works

    await reporter.report("second")

    new = SessionKey(chat_id=CHAT, topic_id=501)
    assert bot.created == [500, 501]
    assert await bot.sessions.get_active(old) is None
    moved = await bot.sessions.get_active(new)
    assert moved is not None
    assert moved.session_id == "lint-sid"  # the next lint turn continues in the new topic
    assert (moved.activity_at, moved.activity_turns) == (None, 0)


async def test_a_resumable_session_of_the_new_topic_is_kept(tmp_path: Path) -> None:
    bot = FakeBot(tmp_path, topics=True)
    reporter = MemoryLintReporter(bot)  # type: ignore[arg-type]
    await reporter.report("first")
    old = await _seed_report_session(bot, 500, "lint-sid")
    await _seed_report_session(bot, 501, "user-sid")  # the user already talks in the new topic
    bot.deliver_ok = [False, True]

    await reporter.report("second")

    kept = await bot.sessions.get_active(SessionKey(chat_id=CHAT, topic_id=501))
    assert kept is not None
    assert kept.session_id == "user-sid"
    assert (await bot.sessions.get_active(old)) is not None  # not lost either


async def test_the_dm_fallback_moves_nothing(tmp_path: Path) -> None:
    bot = FakeBot(tmp_path, topics=True)
    reporter = MemoryLintReporter(bot)  # type: ignore[arg-type]
    await reporter.report("first")
    old = await _seed_report_session(bot, 500)
    dm_before = bot.session_target(SessionKey(chat_id=CHAT))
    bot.deliver_ok = [False, False, True]  # stored topic and the new topic fail; the DM works

    await reporter.report("second")

    assert (await bot.sessions.get_active(old)) is not None
    assert (await bot.sessions.get_active(SessionKey(chat_id=CHAT, topic_id=501))) is None
    assert bot.session_target(SessionKey(chat_id=CHAT)) == dm_before  # the DM model is untouched
    assert bot.submitted[-1].topic_id is None


async def test_the_new_topic_is_locked_while_the_session_moves(tmp_path: Path) -> None:
    bot = FakeBot(tmp_path, topics=True)
    reporter = MemoryLintReporter(bot)  # type: ignore[arg-type]
    await reporter.report("first")
    await _seed_report_session(bot, 500)
    bot.deliver_ok = [False, True]
    new = SessionKey(chat_id=CHAT, topic_id=501)
    held: list[bool] = []
    original = bot._bus.submit.side_effect

    async def spy(env: Envelope) -> None:
        held.append(bot.lock_pool.get(new.lock_key).locked() if env.topic_id == 501 else False)
        await original(env)

    bot._bus.submit = AsyncMock(side_effect=spy)

    await reporter.report("second")

    assert True in held  # the delivery to the new topic ran under its lock
    assert not bot.lock_pool.get(new.lock_key).locked()  # released afterwards


# -- the log context of the lint turn -----------------------------------------------------------


async def test_the_lint_turn_does_not_show_the_previous_session_id(tmp_path: Path) -> None:
    from ductor_bot.log_context import ctx_session_id, set_log_context

    bot = FakeBot(tmp_path, topics=False)
    seen: list[str | None] = []

    async def handle(_key: SessionKey, _prompt: str, **_kwargs: Any) -> Any:
        seen.append(ctx_session_id.get(None))
        set_log_context(session_id="lint-session-id")  # what the flow does once it knows it
        return SimpleNamespace(text="done")

    bot._orch.handle_message = AsyncMock(side_effect=handle)
    set_log_context(session_id="previous-sid")

    await MemoryLintReporter(bot).report("p")  # type: ignore[arg-type]

    assert seen == [None]  # no stale session id during the turn
    assert ctx_session_id.get(None) == "previous-sid"  # and the original context is back
