"""Agent-local automation controls, storage preservation and conversation isolation."""

from __future__ import annotations

import json

import pytest

from ductor_bot.cli.param_resolver import TaskOverrides, resolve_cli_config
from ductor_bot.cron.execution import apply_autocompact_env, build_cmd
from ductor_bot.cron.manager import CronJob, CronManager
from ductor_bot.orchestrator.selectors import autocompact_selector as ac
from ductor_bot.orchestrator.selectors.autocompact_automation import (
    automation_state,
    automation_token,
    save_automation,
)
from ductor_bot.session import SessionKey
from ductor_bot.webhook.models import WebhookEntry


def seed(orch, count=1):
    for i in range(count):
        orch._cron_manager.add_job(
            CronJob(
                id=f"job-{i}",
                title=f"Cron {i}",
                description="private description",
                schedule="0 6 * * *",
                task_folder=f"job-{i}",
                agent_instruction="private prompt",
            )
        )
    orch._webhook_manager.add_hook(
        WebhookEntry(
            id="hook",
            title="Hook",
            description="private description",
            mode="wake",
            prompt_template="private template",
            token="private token",
            hmac_secret="private secret",
        )
    )


def callback(kind, item_id, action):
    return f"ac:auto:{kind}:{automation_token(item_id)}:{action}"


def test_public_state_and_saving_only_changes_value(orch):
    seed(orch)
    orch._config.autocompact = 350000
    before_jobs = json.loads(orch.paths.cron_jobs_path.read_text())
    before_hooks = json.loads(orch.paths.webhooks_path.read_text())
    state = automation_state(orch)
    assert state == {
        "global_value": 350000,
        "items": [
            {
                "kind": "cron",
                "id": "job-0",
                "name": "Cron 0",
                "schedule": "0 6 * * *",
                "enabled": True,
                "value": "default",
                "effective_value": "default",
            },
            {
                "kind": "webhook",
                "id": "hook",
                "name": "Hook",
                "schedule": "",
                "enabled": True,
                "value": None,
                "effective_value": 350000,
            },
        ],
    }
    assert "private" not in json.dumps(state)
    for kind, item_id, value in [("cron", "job-0", 300000), ("webhook", "hook", "off")]:
        assert save_automation(orch, kind, item_id, value)
    after_jobs = json.loads(orch.paths.cron_jobs_path.read_text())
    after_hooks = json.loads(orch.paths.webhooks_path.read_text())
    after_jobs["jobs"][0]["autocompact"] = before_jobs["jobs"][0]["autocompact"]
    after_hooks["hooks"][0]["autocompact"] = before_hooks["hooks"][0]["autocompact"]
    assert after_jobs == before_jobs
    assert after_hooks == before_hooks
    assert save_automation(orch, "webhook", "hook", None)
    orch._config.autocompact = 600000
    assert automation_state(orch)["items"][1]["effective_value"] == 600000


def test_cron_setter_merges_concurrent_edit_without_replacing_fields(orch):
    seed(orch)
    writer = CronManager(jobs_path=orch.paths.cron_jobs_path)
    writer.set_enabled("job-0", enabled=False)
    assert save_automation(orch, "cron", "job-0", 700000)
    job = orch._cron_manager.get_job("job-0")
    assert job.enabled is False
    assert job.autocompact == 700000
    assert job.agent_instruction == "private prompt"
    assert not save_automation(orch, "cron", "missing", "off")


async def test_list_pages_and_item_options(orch):
    seed(orch, count=7)
    key = SessionKey(chat_id=1)
    response = await ac.handle_autocompact_callback(orch, key, "ac:auto:list:0", user_id=100)
    assert "Cron 0" in response.text
    assert "Cron 4" in response.text
    assert "Cron 5" not in response.text
    assert "--autocompact" in response.text
    assert "private" not in response.text
    assert (
        len([b for row in response.buttons.rows for b in row if b.callback_data.endswith(":open")])
        == 5
    )
    response = await ac.handle_autocompact_callback(orch, key, "ac:auto:list:1", user_id=100)
    assert "Cron 5" in response.text
    assert "Hook" in response.text
    assert "Follow global → Default" in response.text
    for kind, item_id in [("cron", "job-0"), ("webhook", "hook")]:
        response = await ac.handle_autocompact_callback(orch, key, callback(kind, item_id, "open"))
        buttons = [b.callback_data for row in response.buttons.rows for b in row]
        assert (callback(kind, item_id, "inherit") in buttons) == (kind == "webhook")
        assert all(len(b.encode()) <= 64 for b in buttons)
    response = await ac.handle_autocompact_callback(orch, key, callback("cron", "job-0", "inherit"))
    assert response.buttons is None


async def test_automation_input_reuses_owner_topic_cancel_retry_and_completion(orch):
    seed(orch)
    key = SessionKey(chat_id=1, topic_id=42)
    data = callback("cron", "job-0", "input")
    await ac.handle_autocompact_callback(orch, key, data, user_id=100)
    assert "Cron 0" in (await ac.handle_autocompact_callback(orch, key, data, user_id=100)).text
    for other, user in [(key, 101), (SessionKey(chat_id=1, topic_id=43), 100)]:
        assert await ac.consume_autocompact_input(orch, other, "300000", user_id=user) is None
    for raw in ["bad", "99999", "1000001"]:
        assert "100,000" in (await ac.consume_autocompact_input(orch, key, raw, user_id=100)).text
        assert (key, 100) in orch._autocompact_inputs
    response = await ac.consume_autocompact_input(orch, key, "300000", user_id=100)
    assert "300,000" in response.text
    assert not orch._autocompact_inputs
    assert orch._cron_manager.get_job("job-0").autocompact == 300000
    assert await orch._sessions.get_active(key) is None
    await ac.handle_autocompact_callback(
        orch, key, callback("webhook", "hook", "input"), user_id=100
    )
    await ac.consume_autocompact_input(orch, key, "/cancel", user_id=100)
    assert not orch._autocompact_inputs
    assert orch._webhook_manager.get_hook("hook").autocompact is None
    orch._cli_service.execute.assert_not_called()


async def test_deleted_target_and_collision_never_change_another_item(orch, monkeypatch):
    seed(orch, count=2)
    key = SessionKey(chat_id=1)
    data = callback("cron", "job-0", "input")
    await ac.handle_autocompact_callback(orch, key, data, user_id=100)
    orch._cron_manager.remove_job("job-0")
    response = await ac.consume_autocompact_input(orch, key, "300000", user_id=100)
    assert "list changed" in response.text
    assert not orch._autocompact_inputs
    assert orch._cron_manager.get_job("job-1").autocompact == "default"
    orch._cron_manager.add_job(
        CronJob(
            id="job-2",
            title="Two",
            description="",
            schedule="* * * * *",
            task_folder="two",
            agent_instruction="",
        )
    )
    monkeypatch.setattr(
        "ductor_bot.orchestrator.selectors.autocompact_automation.automation_token",
        lambda _: "collision",
    )
    response = await ac.handle_autocompact_callback(
        orch, key, "ac:auto:cron:collision:off", user_id=100
    )
    assert "list changed" in response.text
    assert all(job.autocompact == "default" for job in orch._cron_manager.list_jobs())


@pytest.mark.parametrize(
    ("kind", "item_id", "value"), [("cron", "job-0", 300000), ("webhook", "hook", None)]
)
def test_saved_values_reach_execution_argv_env(orch, kind, item_id, value, monkeypatch):
    seed(orch)
    orch._config.autocompact = 500000
    assert save_automation(orch, kind, item_id, value)
    item = next(item for item in automation_state(orch)["items"] if item["kind"] == kind)
    cfg = resolve_cli_config(
        orch._config, None, task_overrides=TaskOverrides(autocompact=item["value"])
    )
    monkeypatch.setattr(
        "ductor_bot.cron.execution.claude_autocompact_flag_supported", lambda _: True
    )
    monkeypatch.setattr("ductor_bot.cron.execution.which", lambda _: "/fake/claude")
    command = build_cmd(cfg, "hello")
    assert command is not None
    apply_autocompact_env(command, cfg)
    expected = 300000 if kind == "cron" else 500000
    assert command.cmd[command.cmd.index("--autocompact") + 1] == str(expected)
    assert command.env_overrides["DUCTOR_AUTOCOMPACT"] == str(expected)


async def test_webhook_input_write_failure_keeps_retry_and_restores_runtime_value(
    orch, monkeypatch
):
    seed(orch)
    key = SessionKey(chat_id=1)
    await ac.handle_autocompact_callback(
        orch, key, callback("webhook", "hook", "input"), user_id=100
    )
    original = orch._webhook_manager._save

    def fail():
        raise OSError("private-path")

    monkeypatch.setattr(orch._webhook_manager, "_save", fail)
    response = await ac.consume_autocompact_input(orch, key, "300000", user_id=100)
    assert response.buttons is None
    assert (key, 100) in orch._autocompact_inputs
    assert orch._webhook_manager.get_hook("hook").autocompact is None
    monkeypatch.setattr(orch._webhook_manager, "_save", original)
    response = await ac.consume_autocompact_input(orch, key, "300000", user_id=100)
    assert response.buttons is not None
    assert not orch._autocompact_inputs
    assert orch._webhook_manager.get_hook("hook").autocompact == 300000


async def test_empty_list_and_malformed_callbacks_are_safe(orch):
    key = SessionKey(chat_id=1)
    response = ac.automation_selector_start(orch)
    assert "No cron jobs" in response.text
    assert "--autocompact" in response.text
    for data in ["ac:auto:list:x", "ac:auto", "ac:auto:task:123:off", "ac:auto:cron:123:other"]:
        response = await ac.handle_autocompact_callback(orch, key, data, user_id=100)
        assert response.buttons is None
    assert not orch._autocompact_inputs
