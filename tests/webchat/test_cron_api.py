"""Cron API contracts, persistence failures, routing and browser authentication."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import time_machine

from ductor_bot.cron import definitions
from ductor_bot.cron.jobs_file import read_jobs_data, write_jobs_data
from ductor_bot.cron.manager import CronJob
from ductor_bot.cron.observer import CronObserver
from ductor_bot.infra import file_lock
from ductor_bot.orchestrator.core import Orchestrator
from ductor_bot.webchat.web.app import STATE, create_app
from tests.webchat.test_bridge_api import conversation as conversation, headers
from tests.webchat.test_web_app import login, site as site

PATH = "/webchat/v1/topics/-123.7/cron"
BROWSER = "/api/topics/-123.7/cron"


@pytest.fixture
async def api(conversation, monkeypatch):
    h = conversation
    monkeypatch.setattr(
        "ductor_bot.webchat.cron_api.store_path", lambda _home: h.paths.ductor_home / "secrets.env"
    )
    h.bot.config.user_timezone = "Asia/Seoul"
    h.bot.config.autocompact = 400000
    h.bot._orch = Orchestrator(h.bot.config, h.paths, lock_pool=h.pool)
    h.bot._sequential = SimpleNamespace(get_lock=h.pool.get)
    h.bot._orch._observers.cron = MagicMock()
    await h.bot._orch._sessions.resolve_session(h.key)
    await h.bot._orch._sessions.set_autocompact(h.key, 300000)
    return h


def payload(**overrides):
    return {
        "id": "daily",
        "title": "Daily report",
        "schedule": "0 9 * * *",
        "content": "Read public weather.\n",
        **overrides,
    }


async def add(api, **overrides):
    response = await api.client.post(PATH, json=payload(**overrides), headers=headers())
    assert response.status == 201, await response.text()
    return (await response.json())["job"]


async def detail(api, job="daily"):
    response = await api.client.get(PATH + "/" + job, headers=headers())
    assert response.status == 200, await response.text()
    return (await response.json())["job"]


async def test_list_create_detail_defaults_and_server_routing(api):
    api.bot._topic_names.set(-123, 7, "Morning reports")
    response = await api.client.get(PATH, headers=headers())
    state = await response.json()
    assert state == {
        "jobs": [],
        "defaults": {"timezone": "Asia/Seoul", "delivery_mode": "inject", "autocompact": 300000},
    }
    job = await add(api, chat_id=999, topic_id=99, transport="mx")
    assert job["target"] == {
        "chat_id": -123,
        "topic_id": 7,
        "transport": "tg",
        "name": "Morning reports",
    }
    assert job["enabled"] is True
    assert job["delivery_mode"] == "inject"
    assert job["autocompact"] == job["effective_autocompact"] == 300000
    assert job["timezone"] == "Asia/Seoul"
    assert job["schedule_summary"]
    folder = api.paths.cron_tasks_dir / "daily"
    (folder / "scripts/job.py").write_text('load_secrets("SCRIPT_TOKEN")\n')
    view = await detail(api)
    assert view["content"] == payload()["content"]
    assert view["sha256"] == definitions.body_sha256(payload()["content"])
    assert view["scripts"] == ["job.py"]
    assert view["secret_warning"]["keys"] == ["SCRIPT_TOKEN"]
    assert "SECRETS_LOCKED" in view["secret_warning"]["warning"]
    listed = await (await api.client.get(PATH, headers=headers())).json()
    assert len(listed["jobs"]) == 1
    assert (
        not {"content", "sha256", "scripts", "agent_instruction", "provider", "model"}
        & listed["jobs"][0].keys()
    )
    api.bot._orch._observers.cron.request_reschedule.assert_called_once()


@pytest.mark.parametrize("topic", [None, "default", "off", 500000])
async def test_copies_raw_topic_autocompact_and_honors_explicit_override(api, topic):
    await api.bot._orch._sessions.set_autocompact(api.key, topic)
    job = await add(api)
    assert job["autocompact"] == topic
    assert job["effective_autocompact"] == (400000 if topic is None else topic)
    job = await add(api, id="override", autocompact="500k")
    assert job["autocompact"] == 500000


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("title", "", "required"),
        ("schedule", "61 * * * *", "invalid_schedule"),
        ("timezone", "Mars/Olympus", "invalid_timezone"),
        ("enabled", "yes", "invalid_boolean"),
        ("delivery_mode", "invalid", "invalid_delivery_mode"),
        ("autocompact", "99k", "invalid_autocompact"),
        ("provider", "codex", "unsupported_fields"),
        ("model", "some-model", "unsupported_fields"),
        ("reasoning_effort", "high", "unsupported_fields"),
        ("id", "!!!", "invalid_id"),
    ],
)
async def test_creation_field_errors_create_nothing(api, field, value, code):
    response = await api.client.post(PATH, json=payload(**{field: value}), headers=headers())
    assert response.status == 400
    assert code in (await response.json())["fields"].values()
    assert read_jobs_data(api.paths.cron_jobs_path)["jobs"] == []
    assert not (api.paths.cron_tasks_dir / "daily").exists()


@pytest.mark.parametrize("raw", ["{", "null", "[]", '"text"'])
async def test_malformed_body(api, raw):
    response = await api.client.post(PATH, data=raw, headers=headers())
    assert response.status == 400
    assert (await response.json())["fields"] == {"request": "invalid_json"}


async def test_preview_annual_three_runs_and_validation(api):
    with time_machine.travel(datetime(2026, 1, 2, tzinfo=UTC), tick=False):
        response = await api.client.post(
            PATH + "/preview", json={"schedule": "0 9 1 1 *"}, headers=headers()
        )
    data = await response.json()
    assert response.status == 200
    assert data["valid"] is True
    assert [datetime.fromisoformat(run).year for run in data["next_runs"]] == [2027, 2028, 2029]
    for body, field in [
        ({"schedule": "bad"}, "schedule"),
        ({"schedule": "* * * * *", "timezone": "bad"}, "timezone"),
    ]:
        response = await api.client.post(PATH + "/preview", json=body, headers=headers())
        assert response.status == 400
        assert field in (await response.json())["fields"]


async def test_update_sha_history_settings_and_provider_preservation(api):
    await add(api)
    jobs = read_jobs_data(api.paths.cron_jobs_path)
    jobs["jobs"][0].update(
        provider="codex", model="existing-model", reasoning_effort="high", last_run_status="success"
    )
    write_jobs_data(api.paths.cron_jobs_path, jobs)
    view = await detail(api)
    response = await api.client.put(
        PATH + "/daily",
        json={
            "title": "Updated",
            "schedule": "30 8 * * 1-5",
            "timezone": "UTC",
            "content": 'load_secrets("SERVICE_TOKEN")',
            "base_sha256": view["sha256"],
            "autocompact": None,
            "enabled": False,
            "delivery_mode": "separate",
            "chat_id": 888,
            "topic_id": 88,
            "transport": "mx",
        },
        headers=headers(),
    )
    assert response.status == 200, await response.text()
    updated = (await response.json())["job"]
    assert updated["content"] == 'load_secrets("SERVICE_TOKEN")'
    assert updated["secret_warning"]["keys"] == ["SERVICE_TOKEN"]
    assert updated["effective_autocompact"] == 400000
    assert updated["enabled"] is False
    assert updated["target"]["chat_id"] == -123
    saved = read_jobs_data(api.paths.cron_jobs_path)["jobs"][0]
    assert (saved["provider"], saved["model"], saved["reasoning_effort"]) == (
        "codex",
        "existing-model",
        "high",
    )
    assert saved["last_run_status"] == "success"
    assert (
        next((api.paths.cron_tasks_dir / "daily/.history").glob("*.md")).read_text()
        == payload()["content"]
    )
    response = await api.client.put(
        PATH + "/daily", json={"content": "Stale", "base_sha256": view["sha256"]}, headers=headers()
    )
    assert response.status == 409
    assert (await detail(api))["content"] == updated["content"]


async def test_toggle_and_archive(api):
    await add(api)
    for body, expected in [({}, False), ({"enabled": True}, True)]:
        response = await api.client.post(PATH + "/daily/toggle", json=body, headers=headers())
        assert response.status == 200
        assert (await response.json())["job"]["enabled"] is expected
    response = await api.client.delete(PATH + "/daily", headers=headers())
    assert response.status == 200
    assert read_jobs_data(api.paths.cron_jobs_path)["jobs"] == []
    assert not (api.paths.cron_tasks_dir / "daily").exists()
    archived = next((api.paths.cron_tasks_dir / ".archive").iterdir())
    assert (archived / "TASK_DESCRIPTION.md").read_text() == payload()["content"]
    assert (await api.client.get(PATH + "/daily", headers=headers())).status == 404
    assert api.bot._orch._observers.cron.request_reschedule.call_count == 4


async def test_duplicate_and_oversized_content(api):
    await add(api)
    assert (await api.client.post(PATH, json=payload(), headers=headers())).status == 409
    for method, path, body in [
        ("post", PATH, payload(id="large", content="a" * (definitions.BODY_LIMIT + 1))),
        (
            "put",
            PATH + "/daily",
            {"content": "a" * (definitions.BODY_LIMIT + 1), "base_sha256": "bad"},
        ),
    ]:
        response = await getattr(api.client, method)(path, json=body, headers=headers())
        assert response.status == 413
    assert not (api.paths.cron_tasks_dir / "large").exists()


@pytest.mark.parametrize("action", ["create", "update", "archive"])
async def test_json_failure_rolls_back_files_and_returns_503(api, monkeypatch, action):
    if action != "create":
        await add(api)
    before = read_jobs_data(api.paths.cron_jobs_path)

    def fail(*args, **kwargs):
        if action == "create":
            assert (
                api.paths.cron_tasks_dir / "daily/TASK_DESCRIPTION.md"
            ).read_text() == payload()["content"]
            assert not list(api.paths.cron_tasks_dir.glob(".staging-*"))
        raise OSError("synthetic-private-path")

    monkeypatch.setattr(definitions, "write_jobs_data", fail)
    if action == "create":
        response = await api.client.post(PATH, json=payload(), headers=headers())
    elif action == "archive":
        response = await api.client.delete(PATH + "/daily", headers=headers())
    else:
        response = await api.client.put(
            PATH + "/daily",
            json={"content": "New", "base_sha256": definitions.body_sha256(payload()["content"])},
            headers=headers(),
        )
    assert response.status == 503
    assert "synthetic-private-path" not in await response.text()
    assert read_jobs_data(api.paths.cron_jobs_path) == before
    assert not list(api.paths.cron_tasks_dir.glob(".staging-*"))
    if action == "create":
        assert not (api.paths.cron_tasks_dir / "daily").exists()
    else:
        assert (api.paths.cron_tasks_dir / "daily/TASK_DESCRIPTION.md").read_text() == payload()[
            "content"
        ]


@pytest.mark.skipif(file_lock.fcntl is None, reason="requires flock")
@pytest.mark.parametrize("action", ["update", "archive"])
@pytest.mark.parametrize("timing", ["before_request", "after_file_change"])
async def test_registry_lock_covers_files_and_json(api, monkeypatch, action, timing):
    await add(api)
    jobs = api.paths.cron_jobs_path
    tasks = api.paths.cron_tasks_dir
    folder = tasks / "daily"
    original_json = jobs.read_bytes()
    original_body = payload()["content"]
    attempted = False

    with ExitStack() as locks:

        def contend_after_change():
            nonlocal attempted
            attempted = True
            try:
                locks.enter_context(file_lock.exclusive_file_lock(jobs, timeout=0))
            except file_lock.LockTimeoutError:
                pass  # The request already owns the lock through its JSON write.

        if timing == "before_request":
            locks.enter_context(file_lock.exclusive_file_lock(jobs))
        elif action == "update":
            save_body = definitions._save_body

            def pause_after_body(*args, **kwargs):
                saved = save_body(*args, **kwargs)
                contend_after_change()
                return saved

            monkeypatch.setattr(definitions, "_save_body", pause_after_body)
        else:
            rename = Path.rename

            def pause_after_move(source, target):
                result = rename(source, target)
                if source == folder:
                    contend_after_change()
                return result

            monkeypatch.setattr(Path, "rename", pause_after_move)

        if action == "update":
            response = await api.client.put(
                PATH + "/daily",
                json={"content": "New body", "base_sha256": definitions.body_sha256(original_body)},
                headers=headers(),
            )
        else:
            response = await api.client.delete(PATH + "/daily", headers=headers())

    archived = list((tasks / ".archive").glob("daily-*"))
    if timing == "before_request":
        assert response.status == 503
        assert jobs.read_bytes() == original_json
        assert (folder / "TASK_DESCRIPTION.md").read_text() == original_body
        assert not archived
        assert (await detail(api))["content"] == original_body
    else:
        assert attempted
        assert response.status == 200
        if action == "update":
            assert (await detail(api))["content"] == "New body"
        else:
            assert not folder.exists()
            assert read_jobs_data(jobs)["jobs"] == []
            assert len(archived) == 1


@pytest.mark.parametrize(
    ("method", "suffix"),
    [
        ("get", ""),
        ("get", "/daily"),
        ("post", "/preview"),
        ("post", ""),
        ("put", "/daily"),
        ("post", "/daily/toggle"),
        ("delete", "/daily"),
    ],
)
async def test_bridge_token_required_for_every_route(api, method, suffix):
    response = await getattr(api.client, method)(
        PATH + suffix, json={} if method in {"post", "put"} else None
    )
    assert response.status == 403
    assert "WWW-Authenticate" not in response.headers


@pytest.mark.parametrize("method", ["get", "post", "put", "delete"])
async def test_unknown_topic_and_missing_job(api, method):
    url = PATH.replace("-123.7", "-123.99") + ("/missing" if method in {"put", "delete"} else "")
    response = await getattr(api.client, method)(
        url, json={} if method in {"post", "put"} else None, headers=headers()
    )
    assert response.status == 404
    if method in {"get", "put", "delete"}:
        response = await getattr(api.client, method)(
            PATH + "/missing", json={} if method == "put" else None, headers=headers()
        )
        assert response.status == 404


@pytest.mark.parametrize(
    ("method", "suffix"),
    [
        ("get", ""),
        ("get", "/daily"),
        ("post", "/preview"),
        ("post", ""),
        ("put", "/daily"),
        ("post", "/daily/toggle"),
        ("delete", "/daily"),
    ],
)
async def test_public_relay_routes_arrive_with_method_body_and_auth(site, method, suffix):
    client, fake = site
    auth, _ = await login(client)
    body = {"schedule": "0 9 * * *"} if method in {"post", "put"} else None
    response = await getattr(client, method)(BROWSER + suffix, json=body, headers=auth)
    assert response.status == 200
    forwarded = [call for call in fake.calls if call[1].startswith("/topics/-123.7/cron")]
    assert [(call[0], call[1], call[3]) for call in forwarded] == [
        (method.upper(), "/topics/-123.7/cron" + suffix, body)
    ]


@pytest.mark.parametrize("missing", ["session", "X-Dtw-Client", "X-Dtw-Csrf", "wrong-csrf"])
async def test_public_write_auth_failures_are_403(site, missing):
    client, fake = site
    auth, _ = await login(client)
    if missing == "session":
        auth.pop("Cookie")
    elif missing == "wrong-csrf":
        auth["X-Dtw-Csrf"] = "wrong"
    else:
        auth.pop(missing)
    response = await client.post(BROWSER, json=payload(), headers=auth)
    assert response.status == 403
    assert "WWW-Authenticate" not in response.headers
    assert not any(call[1].startswith("/topics/-123.7/cron") for call in fake.calls)


async def test_startup_cleans_abandoned_staging_without_archives(api, aiohttp_client):
    from ductor_bot.webchat.bridge import create_bridge_app

    for name in (".staging-old-123", ".archive/old", ".history"):
        (api.paths.cron_tasks_dir / name).mkdir(parents=True, exist_ok=True)
    app = create_bridge_app(api.bot, api.runtime, "another-test-token")
    client = await aiohttp_client(app)
    assert not list(api.paths.cron_tasks_dir.glob(".staging-*"))
    assert (api.paths.cron_tasks_dir / ".archive/old").is_dir()
    await client.close()


async def test_create_checks_secret_warning_before_definition_save(api, monkeypatch):
    original = definitions.create_job
    observed = []
    scan = definitions.scan_secrets

    def inspect_scan(*args, **kwargs):
        assert not read_jobs_data(api.paths.cron_jobs_path)["jobs"]
        observed.append("scan")
        return scan(*args, **kwargs)

    def inspect_create(*args, **kwargs):
        assert observed == ["scan"]
        return original(*args, **kwargs)

    monkeypatch.setattr(definitions, "scan_secrets", inspect_scan)
    monkeypatch.setattr(definitions, "create_job", inspect_create)
    response = await api.client.post(
        PATH, json=payload(content='load_secrets("SERVICE_TOKEN")'), headers=headers()
    )
    assert response.status == 201
    warning = (await response.json())["secret_warning"]
    assert warning["keys"] == ["SERVICE_TOKEN"]
    assert "Static evidence only" in warning["limitations"][0]


async def test_archive_move_failure_leaves_definition_and_content(api, monkeypatch):
    from pathlib import Path as FilePath

    await add(api)
    before = api.paths.cron_jobs_path.read_bytes()

    def fail_move(*args, **kwargs):
        raise OSError("synthetic move error")

    monkeypatch.setattr(FilePath, "rename", fail_move)
    response = await api.client.delete(PATH + "/daily", headers=headers())
    assert response.status == 503
    assert api.paths.cron_jobs_path.read_bytes() == before
    assert (api.paths.cron_tasks_dir / "daily/TASK_DESCRIPTION.md").read_text() == payload()[
        "content"
    ]


async def test_archive_missing_folder_removes_definition(api):
    import shutil

    await add(api)
    shutil.rmtree(api.paths.cron_tasks_dir / "daily")
    response = await api.client.post(
        PATH + "/daily/toggle", json={"enabled": False}, headers=headers()
    )
    assert response.status == 200
    assert (await response.json())["job"]["enabled"] is False
    response = await api.client.delete(PATH + "/daily", headers=headers())
    assert response.status == 200, await response.text()
    assert not read_jobs_data(api.paths.cron_jobs_path)["jobs"]
    assert not (api.paths.cron_tasks_dir / ".archive").exists()


async def test_invalid_schedule_is_local_to_job(api):
    await add(api)
    await add(api, id="broken")
    path = api.paths.cron_jobs_path
    stored = read_jobs_data(path)
    stored["jobs"][1]["schedule"] = "every day"
    write_jobs_data(path, stored)

    response = await api.client.get(PATH, headers=headers())
    assert response.status == 200, await response.text()
    jobs = {job["id"]: job for job in (await response.json())["jobs"]}
    assert jobs["daily"]["schedule_summary"]
    assert jobs["broken"]["schedule_summary"] is None
    assert (await detail(api, "broken"))["schedule_summary"] is None


async def test_invalid_schedule_job_can_be_disabled(api):
    await add(api, id="broken")
    path = api.paths.cron_jobs_path
    stored = read_jobs_data(path)
    stored["jobs"][0]["schedule"] = "every day"
    write_jobs_data(path, stored)

    response = await api.client.post(
        PATH + "/broken/toggle", json={"enabled": False}, headers=headers()
    )
    assert response.status == 200, await response.text()
    assert (await response.json())["job"]["enabled"] is False
    response = await api.client.post(
        PATH + "/broken/toggle", json={"enabled": True}, headers=headers()
    )
    assert response.status == 503
    assert await response.json() == {"error": "save_failed"}
    saved = read_jobs_data(path)["jobs"][0]
    assert saved["enabled"] is False
    assert saved["schedule"] == "every day"


async def test_toggle_id_uses_update_and_toggle_routes(api):
    await add(api, id="toggle")
    response = await api.client.put(PATH + "/toggle", json={"title": "Updated"}, headers=headers())
    assert response.status == 200, await response.text()
    assert (await response.json())["job"]["title"] == "Updated"
    response = await api.client.post(
        PATH + "/toggle/toggle", json={"enabled": False}, headers=headers()
    )
    assert response.status == 200, await response.text()
    assert (await response.json())["job"]["enabled"] is False


@pytest.mark.parametrize("value", ["missing", "invalid", None])
async def test_stored_autocompact_matches_cron_runtime(api, value):
    await add(api)
    path = api.paths.cron_jobs_path
    stored = read_jobs_data(path)
    job = stored["jobs"][0]
    if value == "missing":
        del job["autocompact"]
    else:
        job["autocompact"] = value
    write_jobs_data(path, stored)
    before = path.read_bytes()

    runtime_value = CronJob.from_dict(job).autocompact
    shown = await detail(api)
    assert shown["autocompact"] == runtime_value
    assert shown["effective_autocompact"] == (400000 if runtime_value is None else runtime_value)
    assert path.read_bytes() == before


@pytest.fixture
async def public_api(api, aiohttp_client):
    app = create_app(
        bridge_url=str(api.client.make_url("")),
        bridge_token=headers()["X-Dtw-Bridge-Token"],
        public_origin="https://chat.example.com",
    )
    public = await aiohttp_client(app)
    state = app[STATE]
    token = state.sessions.create("test-generation")
    session = state.sessions.get(token)
    auth = {"X-Dtw-Client": "web", "X-Dtw-Csrf": session.csrf, "Cookie": f"dtw_session={token}"}
    original = state.bridge.request

    async def request(method, path, **kwargs):
        if path == "/auth/state":
            return 200, {
                "password_configured": True,
                "totp_configured": True,
                "credential_generation": "test-generation",
            }
        return await original(method, path, **kwargs)

    state.bridge.request = request
    try:
        yield public, auth
    finally:
        await public.close()


@pytest.mark.parametrize(
    ("method", "suffix", "data", "status"),
    [
        ("GET", "", None, 200),
        ("GET", "/daily", None, 200),
        ("POST", "/preview", {"schedule": "0 9 * * *"}, 200),
        ("POST", "", payload(id="second"), 201),
        ("PUT", "/daily", {"title": "Updated"}, 200),
        ("POST", "/daily/toggle", {"enabled": False}, 200),
        ("DELETE", "/daily", None, 200),
    ],
)
async def test_cron_requests_proceed_during_topic_turn(
    api, public_api, *, method, suffix, data, status
):
    await add(api)
    public, auth = public_api
    async with api.pool.get(api.key.lock_key):
        response = await public.request(method, BROWSER + suffix, json=data, headers=auth)
        assert response.status == status, await response.text()


async def test_real_public_relay_creates_updates_and_archives_on_bridge(api, public_api):
    public, auth = public_api
    response = await public.post(BROWSER, json=payload(), headers=auth)
    assert response.status == 201, await response.text()
    assert (api.paths.cron_tasks_dir / "daily/TASK_DESCRIPTION.md").read_text() == payload()[
        "content"
    ]
    view = await (await public.get(BROWSER + "/daily", headers=auth)).json()
    response = await public.put(
        BROWSER + "/daily",
        json={"content": "Changed through relay", "base_sha256": view["job"]["sha256"]},
        headers=auth,
    )
    assert response.status == 200
    response = await public.delete(BROWSER + "/daily", headers=auth)
    assert response.status == 200
    assert not read_jobs_data(api.paths.cron_jobs_path)["jobs"]


async def test_observer_runs_registered_job_at_its_scheduled_time(api, monkeypatch):
    from ductor_bot.cron.execution import OneShotExecutionResult
    from ductor_bot.infra.task_runner import TaskResult
    from tests.cron.test_observer import _make_codex_cache
    import ductor_bot.cron.observer as observer_module

    original_sleep = asyncio.sleep
    called = asyncio.Event()
    executed = []
    with time_machine.travel(datetime(2026, 1, 1, 23, 59, 59, tzinfo=UTC), tick=False) as clock:
        await add(api, delivery_mode="separate")
        manager = api.bot._orch._cron_manager
        observer = CronObserver(
            api.paths, manager, config=api.bot.config, codex_cache=_make_codex_cache()
        )
        observer._run_preflight = AsyncMock(return_value=False)
        observer.set_result_handler(AsyncMock(return_value=(True, "")))

        async def execute(owner, **kwargs):
            assert datetime.now(UTC) == datetime(2026, 1, 2, 0, 0, tzinfo=UTC)
            assert kwargs["task_folder"] == "daily"
            assert kwargs["routing_env"] == {
                "DUCTOR_TRANSPORT": "tg",
                "DUCTOR_CHAT_ID": "-123",
                "DUCTOR_TOPIC_ID": "7",
            }
            assert (
                api.paths.cron_tasks_dir / "daily/TASK_DESCRIPTION.md"
            ).read_text() == payload()["content"]
            executed.append(kwargs)
            called.set()
            return TaskResult(
                "success",
                "Done",
                OneShotExecutionResult(
                    status="success",
                    result_text="Done",
                    stdout=b'{"result":"Done"}',
                    stderr=b"",
                    returncode=0,
                    timed_out=False,
                ),
            )

        async def sleep(delay):
            if delay == 1:
                clock.shift(1)
                await original_sleep(0)
            elif delay == 0:
                await original_sleep(0)
            else:
                await asyncio.Event().wait()

        monkeypatch.setattr(observer_module, "execute_in_task_folder", execute)
        monkeypatch.setattr(observer_module.asyncio, "sleep", sleep)
        observer._watcher.start = AsyncMock()
        await observer.start()
        first_run = observer._scheduled["daily"]
        try:
            await asyncio.wait_for(asyncio.gather(first_run, return_exceptions=True), 2)
            assert called.is_set()
            assert len(executed) == 1
            assert manager.get_job("daily").last_run_status == "success"
        finally:
            await observer.stop()


async def test_secret_inventory_names_in_prose_are_warned_without_values(api, monkeypatch):
    names = AsyncMock(return_value=["PROSE_TOKEN"])
    monkeypatch.setattr("ductor_bot.webchat.cron_api.vault_list_keys", names)
    response = await api.client.post(
        PATH, json=payload(content="Use PROSE_TOKEN."), headers=headers()
    )
    assert response.status == 201
    warning = (await response.json())["secret_warning"]
    assert warning["keys"] == warning["known"] == ["PROSE_TOKEN"]
    assert warning["inventory_available"] is True
    assert "values" not in warning
    assert names.await_args.args == (api.paths.ductor_home / "secrets.env",)
    assert (await detail(api))["secret_warning"]["keys"] == ["PROSE_TOKEN"]


async def test_locked_inventory_never_blocks_saving_static_warning(api, monkeypatch):
    from ductor_bot.infra.secret_vault import VaultError

    monkeypatch.setattr(
        "ductor_bot.webchat.cron_api.vault_list_keys",
        AsyncMock(side_effect=VaultError("SECRETS_LOCKED")),
    )
    response = await api.client.post(
        PATH, json=payload(content='load_secrets("SERVICE_TOKEN")'), headers=headers()
    )
    assert response.status == 201
    warning = (await response.json())["secret_warning"]
    assert warning["keys"] == ["SERVICE_TOKEN"]
    assert warning["inventory_available"] is False
    assert "known" not in warning
    assert "SECRETS_LOCKED" in warning["warning"]


@pytest.mark.parametrize("field", ["title", "content", "schedule", "timezone", "id"])
async def test_invalid_unicode_fields_are_400_before_any_write(api, field):
    response = await api.client.post(PATH, json=payload(**{field: "\ud800"}), headers=headers())
    assert response.status == 400
    assert (await response.json())["fields"] == {field: "invalid_text"}
    assert not read_jobs_data(api.paths.cron_jobs_path)["jobs"]


async def test_pathological_numeric_cron_is_field_validation_error(api):
    response = await api.client.post(
        PATH + "/preview", json={"schedule": "9" * 5000 + " * * * *"}, headers=headers()
    )
    assert response.status == 400
    assert (await response.json())["valid"] is False


@pytest.mark.parametrize("suffix", ["", "/daily"])
async def test_unregistered_head_cannot_mutate_definitions(api, suffix):
    await add(api)
    before = api.paths.cron_jobs_path.read_bytes()
    observer = api.bot._orch._observers.cron
    observer.request_reschedule.reset_mock()
    response = await api.client.head(PATH + suffix, headers=headers())
    assert response.status == 405
    assert api.paths.cron_jobs_path.read_bytes() == before
    observer.request_reschedule.assert_not_called()


async def test_toggle_checks_static_secrets_and_invalid_enabled_is_400(api):
    await add(api, content='load_secrets("SERVICE_TOKEN")')
    response = await api.client.post(
        PATH + "/daily/toggle", json={"enabled": False}, headers=headers()
    )
    assert response.status == 200
    assert (await response.json())["secret_warning"]["keys"] == ["SERVICE_TOKEN"]
    response = await api.client.post(
        PATH + "/daily/toggle", json={"enabled": "yes"}, headers=headers()
    )
    assert response.status == 400
    assert (await response.json())["fields"] == {"enabled": "invalid_boolean"}
    assert read_jobs_data(api.paths.cron_jobs_path)["jobs"][0]["enabled"] is False


@pytest.mark.parametrize("browser", [False, True])
async def test_escaped_json_accepts_content_at_exact_utf8_limit(api, public_api, browser):
    public, auth = public_api
    client, path, auth = (public, BROWSER, auth) if browser else (api.client, PATH, headers())
    content = "\n" * definitions.BODY_LIMIT
    response = await client.post(path, json=payload(content=content), headers=auth)
    assert response.status == 201, await response.text()
    assert (api.paths.cron_tasks_dir / "daily/TASK_DESCRIPTION.md").read_bytes() == content.encode()


@pytest.mark.skipif(file_lock.fcntl is None, reason="requires flock")
@pytest.mark.parametrize("action", ["rename_same", "rename_other", "add", "remove"])
async def test_cli_workspace_change_waits_for_web_archive(api, monkeypatch, action):
    await add(api, id="archive-target")
    tasks = api.paths.cron_tasks_dir
    target = "archive-target" if action == "rename_same" else "cli-target"
    if action in {"rename_other", "remove"}:
        await add(api, id=target)
    if action != "add":
        (tasks / target / "scripts/probe.txt").write_text("preserve this")
    tools = (
        Path(__file__).resolve().parents[2] / "ductor_bot/_home_defaults/workspace/tools/cron_tools"
    )
    tool, cli_args = {
        "rename_same": ("cron_edit.py", [target, "--name", "renamed"]),
        "rename_other": ("cron_edit.py", [target, "--name", "renamed"]),
        "add": (
            "cron_add.py",
            [
                "--name",
                target,
                "--title",
                "CLI test",
                "--description",
                "Test",
                "--schedule",
                "0 9 * * *",
            ],
        ),
        "remove": ("cron_remove.py", [target]),
    }[action]
    resolve = definitions._workspace_job_dir
    result = None
    folder_intact_before_archive = False

    def pause_before_folder_move(*args, **kwargs):
        nonlocal result, folder_intact_before_archive
        folder = resolve(*args, **kwargs)
        if folder.name != "archive-target":
            return folder
        result = subprocess.run(
            [sys.executable, str(tools / tool), *cli_args],
            env={**os.environ, "DUCTOR_HOME": str(api.paths.ductor_home)},
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
        folder_intact_before_archive = (tasks / target / "scripts/probe.txt").is_file()
        return folder

    monkeypatch.setattr(definitions, "_workspace_job_dir", pause_before_folder_move)
    response = await api.client.delete(PATH + "/archive-target", headers=headers())
    assert result is not None
    assert result.returncode == 1
    assert "locked by another writer" in result.stdout
    assert response.status == 200
    assert not (tasks / "renamed").exists()
    entries = {job["id"]: job for job in read_jobs_data(api.paths.cron_jobs_path)["jobs"]}
    assert "renamed" not in entries
    for missing in ("archive-target", "renamed"):
        assert (await api.client.get(PATH + f"/{missing}", headers=headers())).status == 404
    if action == "rename_same":
        assert folder_intact_before_archive
        assert entries == {}
        assert not (tasks / target).exists()
        archived = next((tasks / ".archive").glob("archive-target-*"))
        assert (archived / "scripts/probe.txt").read_text() == "preserve this"
    elif action == "add":
        assert entries == {}
        assert not (tasks / target).exists()
    else:
        assert folder_intact_before_archive
        assert entries[target]["task_folder"] == target
        assert (await detail(api, target))["content"] == payload()["content"]
        assert (tasks / target / "scripts/probe.txt").read_text() == "preserve this"

    if action == "rename_other":
        retry = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, str(tools / tool), *cli_args],
            env={**os.environ, "DUCTOR_HOME": str(api.paths.ductor_home)},
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
        )
        assert json.loads(retry.stdout)["job_id"] == "renamed"
        assert not (tasks / target).exists()
        assert (await detail(api, "renamed"))["content"] == payload()["content"]
        assert (tasks / "renamed/scripts/probe.txt").read_text() == "preserve this"


@pytest.mark.parametrize("action", ["rename", "add", "remove"])
async def test_cli_registry_save_failure_leaves_workspace_unchanged(api, action):
    await add(api)
    root = api.paths.cron_tasks_dir
    folder = root / "daily"
    (folder / "scripts/probe.txt").write_text("preserve this")
    jobs = api.paths.cron_jobs_path
    registry_before = jobs.read_bytes()
    before = {p.relative_to(folder): p.read_bytes() for p in folder.rglob("*") if p.is_file()}
    tools = (
        Path(__file__).resolve().parents[2] / "ductor_bot/_home_defaults/workspace/tools/cron_tools"
    )
    tool, args = {
        "rename": ("cron_edit.py", ["daily", "--name", "renamed"]),
        "add": (
            "cron_add.py",
            [
                "--name",
                "new-task",
                "--title",
                "CLI test",
                "--description",
                "Test",
                "--schedule",
                "0 9 * * *",
            ],
        ),
        "remove": ("cron_remove.py", ["daily"]),
    }[action]
    probe = """
import runpy, sys
from pathlib import Path
from unittest.mock import patch
script = sys.argv[1]
sys.path.insert(0, str(Path(script).parent))
sys.argv = sys.argv[1:]
with patch("ductor_bot.cron.jobs_file.write_jobs_data", side_effect=OSError("save failed")):
    runpy.run_path(script, run_name="__main__")
"""
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", probe, str(tools / tool), *args],
        env={**os.environ, "DUCTOR_HOME": str(api.paths.ductor_home)},
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 1
    assert "save failed" in result.stdout + result.stderr
    assert jobs.read_bytes() == registry_before
    assert {
        p.relative_to(folder): p.read_bytes() for p in folder.rglob("*") if p.is_file()
    } == before
    assert not (root / "renamed").exists()
    assert not (root / "new-task").exists()
    assert (await detail(api))["content"] == payload()["content"]


@pytest.mark.skipif(file_lock.fcntl is None, reason="requires flock")
@pytest.mark.parametrize(
    ("first", "cli_failure", "web_failure", "winner"),
    [
        ("web", "registry", False, "web"),
        ("cli", "registry", False, "web"),
        ("cli", "body", False, "web"),
        ("cli", "registry", True, None),
        ("cli", None, False, "cli"),
        ("cli", "cleanup", False, "cli"),
    ],
)
async def test_web_and_cli_same_id_create_keeps_winner(
    api, monkeypatch, first, cli_failure, web_failure, winner
):
    job = "collision"
    tasks = api.paths.cron_tasks_dir
    folder = tasks / job
    jobs = api.paths.cron_jobs_path
    tool = (
        Path(__file__).resolve().parents[2]
        / "ductor_bot/_home_defaults/workspace/tools/cron_tools/cron_add.py"
    )
    cli = [
        str(tool),
        "--name",
        job,
        "--title",
        "CLI collision body",
        "--description",
        "CLI assignment",
        "--schedule",
        "0 9 * * *",
    ]
    env = {
        **os.environ,
        "DUCTOR_HOME": str(api.paths.ductor_home),
        "DUCTOR_CHAT_ID": "-123",
        "DUCTOR_TOPIC_ID": "7",
        "DUCTOR_TRANSPORT": "tg",
    }
    probe = """
import errno, os, runpy, sys
from pathlib import Path
from unittest.mock import patch
from ductor_bot.cron import jobs_file

first, failure, script, *args = sys.argv[1:]
sys.path.insert(0, str(Path(script).parent))
sys.argv = [script, *args]
write_jobs = jobs_file.write_jobs_data
write_text = Path.write_text
rmdir = os.rmdir

def wait_before_write():
    print("ready", flush=True)
    assert sys.stdin.readline().strip() == "continue"

def save(path, data):
    folder = path.parent / "workspace/cron_tasks/collision"
    (folder / "scripts/cli-marker.txt").write_text("CLI script")
    if first == "cli":
        wait_before_write()
    if failure == "registry":
        raise OSError("CLI registry failure")
    write_jobs(path, data)

def create_body(path, *args, **kwargs):
    if failure == "body" and path.name == "TASK_DESCRIPTION.md":
        wait_before_write()
        raise OSError("CLI body failure")
    return write_text(path, *args, **kwargs)

def remove_dir(path, *args, **kwargs):
    candidate = Path(path)
    if failure == "cleanup" and candidate.name.startswith(".cli-staging-"):
        raise OSError(errno.EIO, "CLI cleanup failure", path)
    return rmdir(path, *args, **kwargs)

with patch.object(jobs_file, "write_jobs_data", save), patch.object(
    Path, "write_text", create_body
), patch.object(os, "rmdir", remove_dir):
    runpy.run_path(script, run_name="__main__")
"""
    command = [sys.executable, "-c", probe, first, str(cli_failure), *cli]
    build = definitions.create_task_folder

    def add_web_marker(*args, **kwargs):
        staged = build(*args, **kwargs)
        (staged / "scripts/web-marker.txt").write_text("Web script")
        return staged

    monkeypatch.setattr(definitions, "create_task_folder", add_web_marker)
    if web_failure:

        def fail_web(*args, **kwargs):
            raise OSError("Web registry failure")

        monkeypatch.setattr(definitions, "write_jobs_data", fail_web)

    web_body = "Distinct web assignment"
    data = payload(id=job, title="Web collision body", content=web_body)
    if first == "web":
        attempted = None

        def pause_after_web_stage(*args, **kwargs):
            nonlocal attempted
            staged = add_web_marker(*args, **kwargs)
            assert not folder.exists()
            attempted = subprocess.run(
                command,
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
            assert not folder.exists()
            return staged

        monkeypatch.setattr(definitions, "create_task_folder", pause_after_web_stage)
        response = await api.client.post(PATH, json=data, headers=headers())
        assert attempted is not None
        assert attempted.returncode == 1
        assert "locked by another writer" in attempted.stdout
        duplicate = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, *cli],
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        assert duplicate.returncode == 1
        assert "already exists" in duplicate.stdout
    else:
        web_waiting = threading.Event()
        lock = definitions.exclusive_file_lock

        def observe_web_lock(target):
            if target == jobs:
                web_waiting.set()
            return lock(target)

        monkeypatch.setattr(definitions, "exclusive_file_lock", observe_web_lock)
        child = await asyncio.create_subprocess_exec(
            *command,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            assert child.stdout is not None
            assert await asyncio.wait_for(child.stdout.readline(), timeout=15) == b"ready\n"

            async def release_cli():
                assert await asyncio.to_thread(web_waiting.wait, 10)
                return await asyncio.wait_for(child.communicate(b"continue\n"), timeout=15)

            response, (stdout, stderr) = await asyncio.gather(
                api.client.post(PATH, json=data, headers=headers()), release_cli()
            )
            assert child.returncode == (1 if cli_failure in {"body", "registry"} else 0)
            if cli_failure in {"body", "registry"}:
                assert f"CLI {cli_failure} failure".encode() in stderr
            if cli_failure == "cleanup":
                assert json.loads(stdout)["json_entry_created"] is True
                (left,) = tasks.glob(".cli-staging-*")
                assert list(left.iterdir()) == []
                warnings = stderr.decode().splitlines()
                assert len(warnings) == 1
                assert "warning" in warnings[0].lower()
                assert str(left) in warnings[0]
                listed = await asyncio.to_thread(
                    subprocess.run,
                    [sys.executable, str(tool.with_name("cron_list.py"))],
                    env=env,
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=15,
                )
                assert [entry["id"] for entry in json.loads(listed.stdout)["jobs"]] == [job]
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()

    entries = read_jobs_data(jobs)["jobs"]
    assert not list(tasks.glob(".staging-*"))
    if cli_failure != "cleanup":
        assert not list(tasks.glob(".cli-staging-*"))
    if winner is None:
        assert response.status == 503
        assert entries == []
        assert not folder.exists()
        assert (await api.client.get(PATH + "/" + job, headers=headers())).status == 404
        return

    assert response.status == (201 if winner == "web" else 409)
    assert [entry["id"] for entry in entries] == [job]
    assert entries[0]["task_folder"] == job
    saved = await detail(api, job)
    if winner == "web":
        assert entries[0]["title"] == data["title"]
        assert saved["content"] == web_body
    else:
        assert entries[0]["title"] == "CLI collision body"
        assert "CLI assignment" in saved["content"]
    assert saved["scripts"] == [f"{winner}-marker.txt"]
    expected_script = "Web script" if winner == "web" else "CLI script"
    assert (folder / "scripts" / f"{winner}-marker.txt").read_text() == expected_script
