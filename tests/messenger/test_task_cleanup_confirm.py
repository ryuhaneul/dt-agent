"""Telegram and Matrix reach the same confirmation before finished tasks are deleted."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ductor_bot import i18n
from ductor_bot.i18n import t, t_plural
from ductor_bot.messenger.matrix.bot import MatrixBot
from ductor_bot.messenger.telegram.app import TelegramBot
from ductor_bot.tasks.models import TaskSubmit
from ductor_bot.tasks.registry import TaskRegistry


@pytest.fixture(autouse=True)
def _english_again() -> Iterator[None]:
    yield
    i18n.init("en")


def _hub_with_finished(tmp_path: Path, count: int = 2) -> tuple[SimpleNamespace, list[str]]:
    registry = TaskRegistry(registry_path=tmp_path / "tasks.json", tasks_dir=tmp_path / "tasks")
    ids = []
    for index in range(count):
        entry = registry.create(
            TaskSubmit(
                chat_id=5,
                prompt="p",
                message_id=1,
                thread_id=None,
                parent_agent="main",
                name=str(index),
            ),
            "claude",
            "opus",
        )
        registry.update_status(entry.task_id, "done")
        ids.append(entry.task_id)
    return SimpleNamespace(registry=registry, cancel=AsyncMock(), cancel_all=AsyncMock()), ids


@pytest.mark.parametrize("language", ["en", "ko"])
class TestTelegram:
    async def test_confirmation_then_delete(self, language: str, tmp_path: Path) -> None:
        i18n.init(language)
        hub, ids = _hub_with_finished(tmp_path)
        bot = object.__new__(TelegramBot)
        bot._orchestrator = SimpleNamespace(task_hub=hub)
        bot._bot = MagicMock()
        with patch(
            "ductor_bot.messenger.telegram.app.edit_selector_response", new_callable=AsyncMock
        ) as edit:
            await bot._handle_task_selector(5, 9, "tsc:cleanup")
            shown = edit.call_args.args[3]
            assert t_plural("tasks.cleanup_confirm", 2) in shown.text
            assert [b.callback_data for b in shown.buttons.rows[0]] == ["tsc:cleanup_yes", "tsc:r"]
            assert all(hub.registry.get(task_id) is not None for task_id in ids)

            await bot._handle_task_selector(5, 9, "tsc:cleanup_yes")
            assert t_plural("tasks.cleaned", 2) in edit.call_args.args[3].text
            assert all(hub.registry.get(task_id) is None for task_id in ids)


@pytest.mark.parametrize("language", ["en", "ko"])
class TestMatrix:
    async def test_confirmation_then_delete(self, language: str, tmp_path: Path) -> None:
        i18n.init(language)
        hub, ids = _hub_with_finished(tmp_path)
        bot = object.__new__(MatrixBot)
        bot._orchestrator = SimpleNamespace(task_hub=hub)
        bot._id_map = MagicMock()
        bot._id_map.room_to_int.return_value = 5
        bot._send_selector_response = AsyncMock()

        await bot._handle_button_callback("!room:example.test", "$event", "tsc:cleanup")
        text, buttons = bot._send_selector_response.call_args.args[1:3]
        assert t_plural("tasks.cleanup_confirm", 2) in text
        assert [b.text for b in buttons.rows[0]] == [
            t("tasks.btn_confirm_delete"),
            t("tasks.btn_cancel_delete"),
        ]
        assert all(hub.registry.get(task_id) is not None for task_id in ids)

        await bot._handle_button_callback("!room:example.test", "$event", "tsc:cleanup_yes")
        assert all(hub.registry.get(task_id) is None for task_id in ids)
