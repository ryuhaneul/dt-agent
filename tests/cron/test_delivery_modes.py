"""Cron ``delivery_mode``: separate (unchanged), inject and in_session."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import time_machine

from ductor_bot.bus.bus import MessageBus
from ductor_bot.bus.envelope import Envelope
from ductor_bot.cli.codex_cache import CodexModelCache
from ductor_bot.config import AgentConfig
from ductor_bot.cron.manager import CronJob, CronManager
from ductor_bot.cron.observer import CronObserver
from ductor_bot.infra.input_context import input_lock
from ductor_bot.orchestrator.injection import InjectionIncomplete
from ductor_bot.orchestrator.observers import ObserverManager
from ductor_bot.workspace.paths import DuctorPaths

CHAT = 4242
TOPIC = 7
TOOLS = (
    Path(__file__).resolve().parents[2]
    / "ductor_bot"
    / "_home_defaults"
    / "workspace"
    / "tools"
    / "cron_tools"
)


def _paths(tmp_path: Path) -> DuctorPaths:
    fw = tmp_path / "fw"
    paths = DuctorPaths(
        ductor_home=tmp_path / "home", home_defaults=fw / "workspace", framework_root=fw
    )
    paths.cron_tasks_dir.mkdir(parents=True)
    return paths


def _job(job_id: str = "daily", **overrides: Any) -> CronJob:
    base: dict[str, Any] = {
        "id": job_id,
        "title": "Daily Report",
        "description": "d",
        "schedule": "0 9 * * *",
        "task_folder": job_id,
        "agent_instruction": "Do the daily work",
        "chat_id": CHAT,
        "topic_id": TOPIC,
        "delivery_mode": "inject",
    }
    base.update(overrides)
    return CronJob(**base)


class FakeTransport:
    transport_name = "tg"

    def __init__(self) -> None:
        self.delivered: list[Envelope] = []
        self.down = False

    async def deliver(self, envelope: Envelope) -> None:
        if self.down:
            msg = "network"
            raise OSError(msg)
        self.delivered.append(envelope)

    async def deliver_broadcast(self, envelope: Envelope) -> None:
        self.delivered.append(envelope)


class FakeInjector:
    """Sessions keyed by (chat, topic): the reply is recorded with the label."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.sessions: dict[tuple[int, int | None], list[str]] = {}
        self.fail = False
        self.incomplete = False  # an error/timeout turn
        self.reply = "SESSION REPLY"
        self.gate: asyncio.Event | None = None
        self.entered = asyncio.Event()

    async def inject_prompt(self, prompt: str, chat_id: int, label: str, **kwargs: Any) -> str:
        self.calls.append({"prompt": prompt, "chat_id": chat_id, "label": label, **kwargs})
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            msg = "injection failed"
            raise RuntimeError(msg)
        if self.incomplete:
            raise InjectionIncomplete("injected turn did not complete (timeout)")
        self.sessions.setdefault((chat_id, kwargs.get("topic_id")), []).append(prompt)
        return self.reply


class World:
    def __init__(self, tmp_path: Path, **config: Any) -> None:
        self.paths = _paths(tmp_path)
        self.manager = CronManager(jobs_path=self.paths.cron_jobs_path)
        self.observer = CronObserver(
            self.paths,
            self.manager,
            config=AgentConfig(**config),
            codex_cache=MagicMock(spec=CodexModelCache, validate_model=lambda _m: True),
        )
        self.bus = MessageBus()
        self.transport = FakeTransport()
        self.injector = FakeInjector()
        self.bus.register_transport(self.transport)
        self.bus.set_injector(self.injector)  # type: ignore[arg-type]
        observers = ObserverManager(MagicMock(), MagicMock())
        observers.heartbeat = MagicMock()
        observers.cron = self.observer
        observers.wire_to_bus(self.bus)

    def add(self, job: CronJob, *, task_text: str = "TASK TEXT") -> CronJob:
        self.manager.add_job(job)
        folder = self.paths.cron_tasks_dir / job.task_folder
        folder.mkdir(exist_ok=True)
        (folder / "TASK_DESCRIPTION.md").write_text(task_text, encoding="utf-8")
        return job

    async def run(self, job: CronJob, *, stdout: bytes = b'{"result": "All done."}') -> AsyncMock:
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(stdout, b""))
        spawn = AsyncMock(return_value=proc)
        with (
            time_machine.travel(datetime(2026, 1, 15, 14, 0, tzinfo=UTC)),
            patch("ductor_bot.cron.execution.which", return_value="/usr/bin/claude"),
            patch("asyncio.create_subprocess_exec", spawn),
        ):
            await self.observer._execute_job(job.id, job.agent_instruction, job.task_folder)
        return spawn

    def stored(self, job_id: str) -> CronJob:
        job = self.manager.get_job(job_id)
        assert job is not None
        return job


# -- the stored mode ---------------------------------------------------------------------


def test_jobs_without_the_field_are_separate_and_stay_byte_identical() -> None:
    old = {
        "id": "x",
        "title": "t",
        "description": "d",
        "schedule": "0 9 * * *",
        "task_folder": "x",
        "agent_instruction": "i",
    }
    job = CronJob.from_dict(old)
    assert job.delivery_mode == "separate"
    assert job.effective_delivery_mode() == "separate"
    assert "delivery_mode" not in job.to_dict()
    assert CronJob.from_dict(_job("y").to_dict()).delivery_mode == "inject"


def test_unusable_modes_run_as_separate_and_leave_the_file_alone(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    paths = _paths(tmp_path)
    jobs = [
        _job("broadcast", chat_id=0, topic_id=None).to_dict(),
        _job("weird", delivery_mode="sideways").to_dict(),
        _job("fine").to_dict(),
    ]
    paths.cron_jobs_path.write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
    before = paths.cron_jobs_path.read_text()
    with caplog.at_level(logging.WARNING):
        manager = CronManager(jobs_path=paths.cron_jobs_path)
    modes = {j.id: j.effective_delivery_mode() for j in manager.list_jobs()}
    assert modes == {"broadcast": "separate", "weird": "separate", "fine": "inject"}
    assert sum("runs as 'separate'" in r.getMessage() for r in caplog.records) == 2
    assert paths.cron_jobs_path.read_text() == before


# -- separate: nothing changes -------------------------------------------------------------


async def test_separate_delivers_exactly_as_before(tmp_path: Path) -> None:
    world = World(tmp_path)
    job = world.add(_job("daily", delivery_mode="separate"))
    handler = AsyncMock(return_value=(True, ""))
    world.observer.set_result_handler(handler)

    spawn = await world.run(job)

    spawn.assert_awaited_once()
    handler.assert_awaited_once_with("Daily Report", "All done.", "success", CHAT, TOPIC, "tg")
    assert world.stored("daily").last_delivery_status == "ok"


async def test_separate_delivers_the_result_as_a_message_not_into_the_session(
    tmp_path: Path,
) -> None:
    world = World(tmp_path)
    job = world.add(_job("daily", delivery_mode="separate"))

    await world.run(job)

    assert world.injector.calls == []
    assert [e.result_text for e in world.transport.delivered] == ["All done."]


# -- inject ------------------------------------------------------------------------------


async def test_inject_runs_separately_then_injects_the_result_into_the_topic_session(
    tmp_path: Path,
) -> None:
    world = World(tmp_path)
    job = world.add(_job("daily"))

    spawn = await world.run(job)

    spawn.assert_awaited_once()  # the separate run happened
    (call,) = world.injector.calls
    assert (call["chat_id"], call["topic_id"], call["transport"]) == (CHAT, TOPIC, "tg")
    assert "Daily Report" in call["prompt"]
    assert "All done." in call["prompt"]
    assert "success" in call["prompt"]
    assert "daily" in call["label"]  # the job id is in the label
    assert not call.get("isolated")  # the user's own session, not an isolated one
    # Only the session's reply reaches the topic: no raw result message in addition.
    assert [(e.chat_id, e.topic_id, e.result_text) for e in world.transport.delivered] == [
        (CHAT, TOPIC, "SESSION REPLY")
    ]
    stored = world.stored("daily")
    assert (stored.last_run_status, stored.last_delivery_status) == ("success", "ok")


async def test_inject_leaves_other_topic_sessions_alone(tmp_path: Path) -> None:
    world = World(tmp_path)
    job = world.add(_job("daily"))

    await world.run(job)

    assert list(world.injector.sessions) == [(CHAT, TOPIC)]


async def test_inject_tells_the_session_when_the_run_failed(tmp_path: Path) -> None:
    world = World(tmp_path)
    job = world.add(_job("daily"))
    proc = AsyncMock()
    proc.returncode = 1
    proc.communicate = AsyncMock(return_value=(b"", b"boom"))
    with (
        patch("ductor_bot.cron.execution.which", return_value="/usr/bin/claude"),
        patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)),
    ):
        await world.observer._execute_job(job.id, job.agent_instruction, job.task_folder)

    (call,) = world.injector.calls
    assert "FAILED" in call["prompt"]
    assert world.stored("daily").last_run_status.startswith("error")


async def test_inject_ignores_silent_on_success(tmp_path: Path) -> None:
    world = World(tmp_path)
    job = world.add(_job("daily", silent_on_success=True))

    await world.run(job)

    assert len(world.injector.calls) == 1
    assert len(world.transport.delivered) == 1


async def test_inject_keeps_a_long_result_in_a_file_and_names_it(tmp_path: Path) -> None:
    world = World(tmp_path)
    job = world.add(_job("daily"))
    long_result = "x" * 9000

    await world.run(job, stdout=json.dumps({"result": long_result}).encode())

    (call,) = world.injector.calls
    path = world.paths.cron_tasks_dir / "daily" / "LAST_RESULT.md"
    assert path.read_text() == long_result
    assert str(path) in call["prompt"]
    assert len(call["prompt"]) < 8000


async def test_inject_falls_back_to_the_plain_result_when_the_injection_fails(
    tmp_path: Path,
) -> None:
    world = World(tmp_path)
    world.injector.fail = True
    job = world.add(_job("daily"))

    await world.run(job)

    assert [e.result_text for e in world.transport.delivered] == ["All done."]  # not lost
    assert world.stored("daily").last_delivery_status == "ok"


# -- in_session --------------------------------------------------------------------------


async def test_in_session_runs_no_separate_job_and_injects_the_instruction(
    tmp_path: Path,
) -> None:
    world = World(tmp_path)
    job = world.add(_job("daily", delivery_mode="in_session"), task_text="STEP ONE\nSTEP TWO")

    spawn = await world.run(job)

    spawn.assert_not_awaited()  # no separate run
    (call,) = world.injector.calls
    assert (call["chat_id"], call["topic_id"]) == (CHAT, TOPIC)
    prompt = call["prompt"]
    assert "Do the daily work" in prompt  # agent_instruction
    assert "STEP ONE\nSTEP TWO" in prompt  # TASK_DESCRIPTION.md body
    assert str(world.paths.cron_tasks_dir / "daily" / "TASK_DESCRIPTION.md") in prompt
    assert str(world.paths.cron_tasks_dir / "daily") in prompt  # the task folder
    assert [e.result_text for e in world.transport.delivered] == ["SESSION REPLY"]
    stored = world.stored("daily")
    assert (stored.last_run_status, stored.last_delivery_status) == ("success", "ok")


async def test_in_session_ignores_silent_on_success(tmp_path: Path) -> None:
    world = World(tmp_path)
    job = world.add(_job("daily", delivery_mode="in_session", silent_on_success=True))

    await world.run(job)

    assert len(world.transport.delivered) == 1


async def test_in_session_ignores_the_job_model_and_warns_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    world = World(tmp_path)
    job = world.add(_job("daily", delivery_mode="in_session", provider="codex", model="gpt-5.4"))

    with caplog.at_level(logging.WARNING):
        await world.run(job)
        await world.run(job)

    assert sum("are ignored with" in r.getMessage() for r in caplog.records) == 1
    assert "gpt-5.4" not in world.injector.calls[0]["prompt"]
    assert not any(k in world.injector.calls[0] for k in ("provider", "model"))


async def test_in_session_records_a_failed_injection_as_the_run_status(tmp_path: Path) -> None:
    world = World(tmp_path)
    world.injector.fail = True
    job = world.add(_job("daily", delivery_mode="in_session"))

    await world.run(job)

    stored = world.stored("daily")
    assert stored.last_run_status == "error:injection"
    assert stored.last_delivery_status == "failed"
    assert stored.last_result_text is None  # there is no result to resend


async def test_in_session_with_a_missing_folder_does_not_inject(tmp_path: Path) -> None:
    world = World(tmp_path)
    job = _job("daily", delivery_mode="in_session")
    world.manager.add_job(job)  # no task folder created

    await world.run(job)

    assert world.injector.calls == []
    assert world.stored("daily").last_run_status == "error:folder_missing"


async def test_in_session_honours_the_job_dependency(tmp_path: Path) -> None:
    world = World(tmp_path)
    job = world.add(_job("daily", delivery_mode="in_session", dependency="browser"))
    seen: list[tuple[str, str, str | None]] = []

    class Queue:
        def acquire(self, task_id: str, label: str, dependency: str | None):
            seen.append((task_id, label, dependency))
            return _NullContext()

    with patch("ductor_bot.cron.dependency_queue.get_dependency_queue", return_value=Queue()):
        await world.run(job)

    assert seen == [("daily", "Cron job", "browser")]


class _NullContext:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_args: object) -> None:
        return None


# -- both modes ----------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["inject", "in_session"])
async def test_a_user_turn_in_progress_makes_the_job_wait(tmp_path: Path, mode: str) -> None:
    world = World(tmp_path)
    job = world.add(_job("daily", delivery_mode=mode))
    lock = world.bus.lock_pool.get((CHAT, TOPIC))

    async with input_lock(lock):  # the user's turn is running
        running = asyncio.create_task(world.run(job))
        await asyncio.sleep(0.2)
        assert world.injector.calls == []
    await asyncio.wait_for(running, 5)

    assert len(world.injector.calls) == 1


@pytest.mark.parametrize("mode", ["inject", "in_session"])
async def test_a_user_message_during_the_job_turn_waits_for_it(tmp_path: Path, mode: str) -> None:
    world = World(tmp_path)
    job = world.add(_job("daily", delivery_mode=mode))
    world.injector.gate = asyncio.Event()
    lock = world.bus.lock_pool.get((CHAT, TOPIC))

    running = asyncio.create_task(world.run(job))
    await asyncio.wait_for(world.injector.entered.wait(), 5)
    user = asyncio.create_task(lock.acquire())
    await asyncio.sleep(0.1)
    assert not user.done()  # the user's message waits for the job's turn

    world.injector.gate.set()
    await asyncio.wait_for(running, 5)
    await asyncio.wait_for(user, 5)
    lock.release()


@pytest.mark.parametrize("mode", ["inject", "in_session"])
async def test_quiet_hours_still_stop_the_job_before_anything_is_injected(
    tmp_path: Path, mode: str
) -> None:
    world = World(tmp_path)
    job = world.add(_job("daily", delivery_mode=mode, quiet_start=13, quiet_end=15))

    spawn = await world.run(job)  # 14:00 UTC is inside the quiet window

    spawn.assert_not_awaited()
    assert world.injector.calls == []
    assert world.transport.delivered == []


@pytest.mark.parametrize("mode", ["inject", "in_session"])
async def test_a_disabled_job_does_nothing(tmp_path: Path, mode: str) -> None:
    world = World(tmp_path)
    job = world.add(_job("daily", delivery_mode=mode, enabled=False))

    await world.run(job)

    assert world.injector.calls == []


@pytest.mark.parametrize("mode", ["inject", "in_session"])
async def test_a_broadcast_job_runs_as_separate(tmp_path: Path, mode: str) -> None:
    world = World(tmp_path)
    job = world.add(_job("daily", delivery_mode=mode, chat_id=0, topic_id=None))

    spawn = await world.run(job)

    spawn.assert_awaited_once()
    assert world.injector.calls == []
    assert [e.result_text for e in world.transport.delivered] == ["All done."]


async def test_delivery_retry_resends_the_plain_result_without_injecting(
    tmp_path: Path,
) -> None:
    world = World(tmp_path)
    job = world.add(_job("daily"))
    world.manager.update_run_status(
        job.id,
        status="success",
        delivery_status="failed",
        delivery_error="net",
        result_text="kept result",
    )

    delivered, _error, _failed = await world.observer._deliver_result(
        job.id, job.title, "kept result", "success", (CHAT, TOPIC, "tg")
    )

    assert delivered
    assert world.injector.calls == []
    assert [e.result_text for e in world.transport.delivered] == ["kept result"]


# -- the tools -----------------------------------------------------------------------------


def _tool(
    tmp_path: Path, name: str, args: list[str], **env: str
) -> subprocess.CompletedProcess[str]:
    clean = {k: v for k, v in os.environ.items() if not k.startswith("DUCTOR_")}
    clean["DUCTOR_HOME"] = str(tmp_path)
    clean.update(env)
    return subprocess.run(
        [sys.executable, str(TOOLS / name), *args],
        capture_output=True,
        text=True,
        env=clean,
        check=False,
    )


_ADD = ["--name", "j", "--title", "T", "--description", "D", "--schedule", "0 9 * * *"]


def _jobs(tmp_path: Path) -> list[dict[str, Any]]:
    return json.loads((tmp_path / "cron_jobs.json").read_text())["jobs"]


def test_add_refuses_a_mode_without_a_chat_and_creates_nothing(tmp_path: Path) -> None:
    for mode in ("inject", "in_session"):
        result = _tool(tmp_path, "cron_add.py", [*_ADD, "--delivery-mode", mode])
        assert result.returncode == 1
        assert "needs a chat" in json.loads(result.stdout)["error"]
    assert not (tmp_path / "cron_jobs.json").exists()
    assert not (tmp_path / "workspace" / "cron_tasks" / "j").exists()


def test_a_new_job_in_a_chat_defaults_to_inject_and_separate_can_be_asked_for(
    tmp_path: Path,
) -> None:
    chat = {"DUCTOR_CHAT_ID": str(CHAT), "DUCTOR_TOPIC_ID": str(TOPIC)}
    assert _tool(tmp_path, "cron_add.py", _ADD, **chat).returncode == 0
    explicit = ["--name", "k", *_ADD[2:], "--delivery-mode", "separate"]
    assert _tool(tmp_path, "cron_add.py", explicit, **chat).returncode == 0
    in_session = ["--name", "m", *_ADD[2:], "--delivery-mode", "in_session"]
    assert _tool(tmp_path, "cron_add.py", in_session, **chat).returncode == 0
    jobs = {j["id"]: j for j in _jobs(tmp_path)}
    assert jobs["j"]["delivery_mode"] == "inject"  # the default, written explicitly
    assert "delivery_mode" not in jobs["k"]  # an explicit separate
    assert jobs["m"]["delivery_mode"] == "in_session"
    # The stored default is what the observer reads back.
    assert CronJob.from_dict(jobs["j"]).effective_delivery_mode() == "inject"


def test_a_new_job_without_a_chat_is_separate_with_a_note_not_an_error(tmp_path: Path) -> None:
    result = _tool(tmp_path, "cron_add.py", _ADD)
    assert result.returncode == 0
    output = json.loads(result.stdout)
    assert "delivery_mode" not in _jobs(tmp_path)[0]
    assert "broadcast" in output["delivery_mode_note"]
    summary = {row["setting"]: row for row in output["settings_summary"]}
    assert summary["delivery_mode"]["value"] == "separate"
    assert summary["target"]["value"] == "broadcast to all users"
    zero = _tool(tmp_path, "cron_add.py", ["--name", "z", *_ADD[2:]], DUCTOR_CHAT_ID="0")
    assert zero.returncode == 0  # chat 0 is no chat either
    assert "delivery_mode" not in _jobs(tmp_path)[1]


def test_an_explicit_mode_without_a_chat_is_still_refused(tmp_path: Path) -> None:
    for mode in ("inject", "in_session"):
        result = _tool(tmp_path, "cron_add.py", [*_ADD, "--delivery-mode", mode])
        assert result.returncode == 1
        assert "needs a chat" in json.loads(result.stdout)["error"]
    assert not (tmp_path / "cron_jobs.json").exists()


def test_an_explicit_separate_needs_no_chat(tmp_path: Path) -> None:
    result = _tool(tmp_path, "cron_add.py", [*_ADD, "--delivery-mode", "separate"])
    assert result.returncode == 0
    assert "delivery_mode_note" not in json.loads(result.stdout)  # nothing to explain: asked for


def test_the_add_output_summarises_every_setting_with_value_and_meaning(tmp_path: Path) -> None:
    chat = {"DUCTOR_CHAT_ID": str(CHAT), "DUCTOR_TOPIC_ID": str(TOPIC)}
    args = [
        *_ADD,
        "--provider",
        "codex",
        "--model",
        "gpt-5.4",
        "--quiet-start",
        "22",
        "--quiet-end",
        "7",
        "--dependency",
        "browser",
        "--silent-on-success",
    ]
    output = json.loads(_tool(tmp_path, "cron_add.py", args, **chat).stdout)
    rows = {row["setting"]: row for row in output["settings_summary"]}
    assert list(rows) == [
        "schedule",
        "target",
        "delivery_mode",
        "model",
        "quiet_hours",
        "silent_on_success",
        "dependency",
    ]
    assert all(row["value"] and row["meaning"] for row in rows.values())
    assert rows["delivery_mode"]["value"] == "inject"
    assert rows["target"]["value"] == f"chat {CHAT}, topic {TOPIC} (tg)"
    assert rows["model"]["value"] == "provider=codex, model=gpt-5.4"
    assert rows["quiet_hours"]["value"] == "22:00-7:00"
    assert rows["silent_on_success"]["value"] == "true"
    assert rows["dependency"]["value"] == "browser"
    assert "summary_instruction" in output


def test_the_summary_says_the_job_model_is_ignored_in_session(tmp_path: Path) -> None:
    chat = {"DUCTOR_CHAT_ID": str(CHAT)}
    args = [*_ADD, "--model", "opus", "--delivery-mode", "in_session"]
    output = json.loads(_tool(tmp_path, "cron_add.py", args, **chat).stdout)
    rows = {row["setting"]: row for row in output["settings_summary"]}
    assert "ignored" in rows["model"]["meaning"]
    assert "own model" in rows["delivery_mode"]["meaning"]


def test_edit_changes_the_mode_and_validates_the_chat(tmp_path: Path) -> None:
    chat = {"DUCTOR_CHAT_ID": str(CHAT), "DUCTOR_TOPIC_ID": str(TOPIC)}
    _tool(tmp_path, "cron_add.py", _ADD, **chat)
    _tool(tmp_path, "cron_edit.py", ["j", "--delivery-mode", "in_session"])
    ok = _tool(tmp_path, "cron_edit.py", ["j", "--delivery-mode", "inject"])
    assert ok.returncode == 0
    assert _jobs(tmp_path)[0]["delivery_mode"] == "inject"
    to_broadcast = _tool(tmp_path, "cron_edit.py", ["j", "--chat-id", "0"])
    assert to_broadcast.returncode == 1  # an inject job cannot lose its chat
    assert "needs a chat" in json.loads(to_broadcast.stdout)["error"]
    assert _jobs(tmp_path)[0]["chat_id"] == CHAT
    summary = json.loads(ok.stdout)["settings_summary"] if "settings_summary" in ok.stdout else []
    assert {row["setting"] for row in summary} >= {"delivery_mode", "target", "model"}
    back = _tool(tmp_path, "cron_edit.py", ["j", "--delivery-mode", "separate"])
    assert back.returncode == 0
    assert "delivery_mode" not in _jobs(tmp_path)[0]
    # A chat-less (broadcast) job cannot get a mode, nor can its chat be removed afterwards.
    (tmp_path / "cron_jobs.json").write_text(
        json.dumps({"jobs": [{**_jobs(tmp_path)[0], "chat_id": 0}]})
    )
    refused = _tool(tmp_path, "cron_edit.py", ["j", "--delivery-mode", "inject"])
    assert refused.returncode == 1
    assert "needs a chat" in json.loads(refused.stdout)["error"]
    assert "delivery_mode" not in _jobs(tmp_path)[0]
    with_chat = _tool(
        tmp_path, "cron_edit.py", ["j", "--delivery-mode", "in_session", "--chat-id", str(CHAT)]
    )
    assert with_chat.returncode == 0


def test_list_shows_the_mode(tmp_path: Path) -> None:
    chat = {"DUCTOR_CHAT_ID": str(CHAT)}
    _tool(tmp_path, "cron_add.py", [*_ADD, "--delivery-mode", "inject"], **chat)
    _tool(
        tmp_path, "cron_add.py", ["--name", "k", *_ADD[2:], "--delivery-mode", "separate"], **chat
    )
    listed = json.loads(_tool(tmp_path, "cron_list.py", []).stdout)
    assert {j["id"]: j["delivery_mode"] for j in listed["jobs"]} == {"j": "inject", "k": "separate"}


# -- jobs that already exist stay as they are -----------------------------------------------


def test_a_job_without_the_field_stays_separate_when_the_tools_touch_it(tmp_path: Path) -> None:
    old = {
        "id": "j",
        "title": "T",
        "description": "d",
        "schedule": "0 9 * * *",
        "task_folder": "j",
        "agent_instruction": "i",
        "chat_id": CHAT,
        "topic_id": TOPIC,
    }
    (tmp_path / "cron_jobs.json").write_text(json.dumps({"jobs": [old]}))
    edited = _tool(tmp_path, "cron_edit.py", ["j", "--title", "New"])
    assert edited.returncode == 0
    assert "delivery_mode" not in _jobs(tmp_path)[0]  # the edit does not migrate it
    rows = {r["setting"]: r for r in json.loads(edited.stdout)["settings_summary"]}
    assert rows["delivery_mode"]["value"] == "separate"
    assert CronJob.from_dict(_jobs(tmp_path)[0]).effective_delivery_mode() == "separate"


# -- the rules documents --------------------------------------------------------------------


@pytest.mark.parametrize("name", ["all-clis", "claude-only", "codex-only", "gemini-only"])
def test_the_rules_tell_the_agent_to_explain_each_setting_first(name: str) -> None:
    text = (TOOLS / f"RULES-{name}.md").read_text(encoding="utf-8")
    assert "## Explain Each Setting Before Creating or Editing a Job" in text
    section = text.split("## Explain Each Setting Before Creating or Editing a Job")[1].split(
        "\n## "
    )[0]
    for item in (
        "Schedule and timezone",
        "Target chat/topic",
        "delivery_mode",
        "provider / model",
        "Quiet hours",
        "silent_on_success",
        "Dependency",
        "confirmation",
        "settings_summary",
    ):
        assert item in section, item
    assert "`separate`" in section
    assert "`inject`" in section
    assert "`in_session`" in section
    assert "default `inject`" in text


def test_a_job_without_the_field_can_still_lose_its_chat(tmp_path: Path) -> None:
    old = {
        "id": "j",
        "title": "T",
        "description": "d",
        "schedule": "0 9 * * *",
        "task_folder": "j",
        "agent_instruction": "i",
        "chat_id": CHAT,
        "topic_id": TOPIC,
    }
    (tmp_path / "cron_jobs.json").write_text(json.dumps({"jobs": [old]}))
    assert _tool(tmp_path, "cron_edit.py", ["j", "--chat-id", "0"]).returncode == 0


# -- an injected turn that does not complete ------------------------------------------------


@pytest.mark.parametrize("failure", ["incomplete", "empty"])
async def test_inject_with_an_incomplete_turn_sends_the_result_as_a_plain_message(
    tmp_path: Path, failure: str
) -> None:
    world = World(tmp_path)
    job = world.add(_job("daily"))
    if failure == "incomplete":
        world.injector.incomplete = True
    else:
        world.injector.reply = "  "

    await world.run(job)

    assert [e.result_text for e in world.transport.delivered] == ["All done."]
    stored = world.stored("daily")
    assert (stored.last_run_status, stored.last_delivery_status) == ("success", "ok")
    assert "injection incomplete" in stored.last_delivery_error
    assert stored.last_result_text is None


async def test_inject_incomplete_and_the_plain_message_fails_keeps_the_result_for_retry(
    tmp_path: Path,
) -> None:
    world = World(tmp_path)
    job = world.add(_job("daily"))
    world.injector.incomplete = True
    world.transport.down = True

    await world.run(job)

    stored = world.stored("daily")
    assert stored.last_delivery_status == "failed"
    assert stored.last_result_text == "All done."  # the whole result is kept

    world.transport.down = False
    await world.observer._retry_failed_deliveries()  # the retry recovers the original text

    assert [e.result_text for e in world.transport.delivered] == ["All done."]
    stored = world.stored("daily")
    assert (stored.last_delivery_status, stored.last_result_text) == ("ok", None)


@pytest.mark.parametrize("failure", ["incomplete", "empty"])
async def test_in_session_with_an_incomplete_turn_is_an_injection_error(
    tmp_path: Path, failure: str
) -> None:
    world = World(tmp_path)
    job = world.add(_job("daily", delivery_mode="in_session"))
    if failure == "incomplete":
        world.injector.incomplete = True
    else:
        world.injector.reply = ""

    await world.run(job)

    stored = world.stored("daily")
    assert stored.last_run_status == "error:injection"  # the status message is no success
    assert stored.last_delivery_status == "failed"
    assert stored.last_result_text is None  # nothing to resend: the work is not run again


def test_the_quiet_hours_summary_describes_what_the_scheduler_really_does(tmp_path: Path) -> None:
    result = _tool(tmp_path, "cron_add.py", _ADD, DUCTOR_CHAT_ID=str(CHAT))
    rows = {r["setting"]: r for r in json.loads(result.stdout)["settings_summary"]}
    assert rows["quiet_hours"]["value"].startswith("none")
    assert "heartbeat" in rows["quiet_hours"]["value"]
    assert "do not apply" in rows["quiet_hours"]["value"]


# -- a run that ends in an exception -----------------------------------------------------------

_BOOM = "secret detail from the failing run"


async def _run_with_executor_error(world: World, job: CronJob, error: BaseException) -> None:
    with patch("ductor_bot.cron.observer.execute_in_task_folder", AsyncMock(side_effect=error)):
        await world.observer._execute_job(job.id, job.agent_instruction, job.task_folder)


@pytest.mark.parametrize(
    ("job_args", "chat"),
    [
        ({"delivery_mode": "separate"}, CHAT),
        ({"delivery_mode": "inject"}, CHAT),
        ({"delivery_mode": "separate", "chat_id": 0, "topic_id": None}, 0),
    ],
)
async def test_an_exception_becomes_one_failed_run_notice(
    tmp_path: Path, job_args: dict[str, Any], chat: int
) -> None:
    world = World(tmp_path)
    job = world.add(_job("daily", **job_args))

    await _run_with_executor_error(world, job, RuntimeError(_BOOM))

    assert world.injector.calls == []  # no model turn is made for the failure
    (envelope,) = world.transport.delivered
    assert envelope.chat_id == chat
    assert "Daily Report" in envelope.result_text
    assert "RuntimeError" in envelope.result_text
    assert _BOOM not in envelope.result_text  # only the exception type reaches the chat
    stored = world.stored("daily")
    assert stored.last_run_status == "error:exception"
    assert stored.last_run_at
    assert (stored.last_delivery_status, stored.last_result_text) == ("ok", None)


async def test_an_exception_in_in_session_mode_notifies_without_a_model_turn(
    tmp_path: Path,
) -> None:
    world = World(tmp_path)
    job = world.add(_job("daily", delivery_mode="in_session"))

    with patch("ductor_bot.bus.adapters.cron_instruction_prompt", side_effect=ValueError(_BOOM)):
        await world.observer._execute_job(job.id, job.agent_instruction, job.task_folder)

    assert world.injector.calls == []
    (envelope,) = world.transport.delivered
    assert "ValueError" in envelope.result_text
    assert _BOOM not in envelope.result_text
    assert world.stored("daily").last_run_status == "error:exception"


async def test_an_exception_is_not_hidden_by_silent_on_success(tmp_path: Path) -> None:
    world = World(tmp_path)
    job = world.add(_job("daily", delivery_mode="separate", silent_on_success=True))

    await _run_with_executor_error(world, job, RuntimeError(_BOOM))

    assert len(world.transport.delivered) == 1


async def test_a_cancelled_run_sends_no_notice(tmp_path: Path) -> None:
    world = World(tmp_path)
    job = world.add(_job("daily", delivery_mode="separate"))

    with pytest.raises(asyncio.CancelledError):
        await _run_with_executor_error(world, job, asyncio.CancelledError())

    assert world.transport.delivered == []
    assert world.stored("daily").last_run_status is None
    assert "daily" not in world.observer._executing


async def test_an_exception_after_the_delivery_makes_no_second_notice(tmp_path: Path) -> None:
    world = World(tmp_path)
    job = world.add(_job("daily", delivery_mode="separate"))

    with patch.object(world.manager, "update_run_status", side_effect=RuntimeError(_BOOM)):
        await world.run(job)

    assert [e.result_text for e in world.transport.delivered] == ["All done."]


async def test_a_failed_notice_is_kept_and_the_retry_resends_it_without_a_run(
    tmp_path: Path,
) -> None:
    world = World(tmp_path, cron_delivery_retry={"enabled": True})
    job = world.add(_job("daily", delivery_mode="inject"))
    world.transport.down = True

    await _run_with_executor_error(world, job, RuntimeError(_BOOM))

    stored = world.stored("daily")
    assert (stored.last_run_status, stored.last_delivery_status) == ("error:exception", "failed")
    assert "RuntimeError" in (stored.last_result_text or "")

    world.transport.down = False
    spawn = AsyncMock()
    with patch("asyncio.create_subprocess_exec", spawn):
        await world.observer._retry_failed_deliveries()

    spawn.assert_not_awaited()  # the model run is not repeated
    assert world.injector.calls == []
    (envelope,) = world.transport.delivered
    assert "RuntimeError" in envelope.result_text
    assert world.stored("daily").last_delivery_status == "ok"


# -- the time shown in an injected turn --------------------------------------------------------


@pytest.mark.parametrize("mode", ["inject", "in_session"])
async def test_the_injected_turn_shows_the_time_in_the_user_timezone(
    tmp_path: Path, mode: str
) -> None:
    world = World(tmp_path, user_timezone="Asia/Seoul")
    job = world.add(_job("daily", delivery_mode=mode, timezone="UTC"))

    await world.run(job)  # 14:00 UTC

    (call,) = world.injector.calls
    assert "2026-01-15T23:00:00+09:00 (Asia/Seoul)" in call["prompt"]
    assert "14:00:00" not in call["prompt"]


@pytest.mark.parametrize("name", ["all-clis", "claude-only", "codex-only", "gemini-only"])
def test_the_rules_follow_the_real_model_and_effort_behaviour(name: str) -> None:
    text = (TOOLS / f"RULES-{name}.md").read_text(encoding="utf-8")
    for stale in ("gpt-5", "gemini-2.5", "gemini-3", "sonnet[1m]", "`fable`", "21:00-08:00"):
        assert stale not in text, stale
    assert "Claude only" not in text  # `max` is not an allowed value of the tool
    ask = text.split("Ask Before Creating Jobs")[1].split("\n## ")[0]
    assert "seven settings" in ask
    assert "Do not offer a fixed model list" in ask
    assert "`--reasoning-effort low|medium|high|xhigh`" in ask or "ignore `--reasoning" in ask
    if name in ("all-clis", "codex-only"):
        assert "codex_models.json" in ask
    if name in ("all-clis", "gemini-only"):
        assert "gemini_models.json" in ask
    assert "defaults to `medium`" not in text


# -- the destination of an exception notice is the one the run started with ---------------------

_OTHER_CHAT = 999
_OTHER_TOPIC = 99


async def _run_and_change_the_job(world: World, job: CronJob, change: str) -> None:
    async def fail_after_the_change(*_args: Any, **_kwargs: Any) -> None:
        if change == "delete":
            world.manager.remove_job(job.id)
        else:
            world.manager.remove_job(job.id)
            world.manager.add_job(
                _job(job.id, chat_id=_OTHER_CHAT, topic_id=_OTHER_TOPIC, delivery_mode="separate")
            )
        raise RuntimeError(_BOOM)

    with patch(
        "ductor_bot.cron.observer.execute_in_task_folder",
        AsyncMock(side_effect=fail_after_the_change),
    ):
        await world.observer._execute_job(job.id, job.agent_instruction, job.task_folder)


@pytest.mark.parametrize("mode", ["separate", "inject", "in_session"])
@pytest.mark.parametrize("change", ["delete", "reregister"])
async def test_the_notice_goes_to_the_original_destination_when_the_job_changes(
    tmp_path: Path, mode: str, change: str
) -> None:
    world = World(tmp_path)
    job = world.add(_job("daily", delivery_mode=mode))
    if mode == "in_session":
        # in_session runs no separate job: fail where it prepares the injected turn instead
        def fail_after_the_change(*_a: Any, **_k: Any) -> str:
            world.manager.remove_job(job.id)
            if change == "reregister":
                world.manager.add_job(
                    _job(
                        job.id, chat_id=_OTHER_CHAT, topic_id=_OTHER_TOPIC, delivery_mode="separate"
                    )
                )
            raise ValueError(_BOOM)

        with patch(
            "ductor_bot.bus.adapters.cron_instruction_prompt", side_effect=fail_after_the_change
        ):
            await world.observer._execute_job(job.id, job.agent_instruction, job.task_folder)
    else:
        await _run_and_change_the_job(world, job, change)

    assert world.injector.calls == []  # no session turn, no model turn
    assert [(e.chat_id, e.topic_id) for e in world.transport.delivered] == [(CHAT, TOPIC)]
    assert "Daily Report" in world.transport.delivered[0].result_text
    assert _BOOM not in world.transport.delivered[0].result_text
    if change == "reregister":
        replaced = world.stored("daily")
        assert replaced.chat_id == _OTHER_CHAT
        assert replaced.last_run_status is None  # the new definition did not run
        assert replaced.last_result_text is None


async def test_a_failed_notice_is_not_kept_for_a_new_destination(tmp_path: Path) -> None:
    world = World(tmp_path, cron_delivery_retry={"enabled": True})
    job = world.add(_job("daily", delivery_mode="separate"))
    world.transport.down = True

    await _run_and_change_the_job(world, job, "reregister")

    replaced = world.stored("daily")
    assert replaced.last_result_text is None  # nothing is resent to the new destination
    world.transport.down = False
    await world.observer._retry_failed_deliveries()
    assert world.transport.delivered == []


async def test_a_broadcast_job_that_is_deleted_during_the_run_still_broadcasts(
    tmp_path: Path,
) -> None:
    world = World(tmp_path)
    job = world.add(_job("daily", delivery_mode="separate", chat_id=0, topic_id=None))

    await _run_and_change_the_job(world, job, "delete")

    assert [(e.chat_id, e.topic_id) for e in world.transport.delivered] == [(0, None)]
