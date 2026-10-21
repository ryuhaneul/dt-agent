"""Headless delegation routing and rejected async/task submissions."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from ductor_bot.config import AgentConfig
from ductor_bot.messenger.telegram.app import TelegramBot
from ductor_bot.multiagent.bus import (
    AsyncInterAgentResult,
    AsyncSendOptions,
    HeadlessAsyncRejected,
    InterAgentBus,
)
from ductor_bot.multiagent.internal_api import InternalAgentAPI
from ductor_bot.multiagent.models import SubAgentConfig
from ductor_bot.multiagent.supervisor import AgentSupervisor


def _stack(transport: str, result: str = "done") -> MagicMock:
    stack = MagicMock()
    stack.config = AgentConfig(transport=transport)
    stack.bot.orchestrator.handle_interagent_message = AsyncMock(
        return_value=(result, "ia-main", "")
    )
    stack.bot.notification_service.notify = AsyncMock()
    stack.bot.notification_service.notify_all = AsyncMock()
    return stack


@pytest.fixture
def bus() -> InterAgentBus:
    bus = InterAgentBus()
    bus.register("main", _stack("telegram"))
    bus.register("worker", _stack("headless"))
    return bus


@pytest.fixture
async def client(bus: InterAgentBus) -> TestClient:
    api = InternalAgentAPI(bus, port=0)
    api.set_task_hub(MagicMock())
    server = TestServer(api._app)
    client = TestClient(server)
    await client.start_server()
    yield client
    await client.close()
    await bus.cancel_all_async()


async def test_main_can_send_sync_to_headless(bus: InterAgentBus) -> None:
    response = await bus.send("main", "worker", "do work")
    assert response.success
    assert response.text == "done"


async def test_headless_can_send_sync_to_main(bus: InterAgentBus) -> None:
    response = await bus.send("worker", "main", "question")
    assert response.success
    assert response.text == "done"


async def test_main_async_result_keeps_original_route(
    bus: InterAgentBus, caplog: pytest.LogCaptureFixture
) -> None:
    received: list[AsyncInterAgentResult] = []
    done = asyncio.Event()

    async def deliver(result: AsyncInterAgentResult) -> None:
        received.append(result)
        done.set()

    bus.set_async_result_handler("main", deliver)
    opts = AsyncSendOptions(chat_id=-1001, topic_id=7, transport="tg", silent=True)
    with caplog.at_level("INFO", logger="ductor_bot.multiagent.bus"):
        task_id = bus.send_async("main", "worker", "do work", opts=opts)
        await asyncio.wait_for(done.wait(), timeout=1)

    assert task_id is not None
    assert len(received) == 1
    assert received[0].result_text == "done"
    assert (received[0].chat_id, received[0].topic_id, received[0].transport) == (
        opts.chat_id,
        opts.topic_id,
        opts.transport,
    )
    assert caplog.text.count("chat_id=-1001 topic_id=7 transport=tg") == 2


@pytest.mark.parametrize("reply_to", ["", "main"])
def test_headless_async_sender_is_rejected_even_with_reply_to(
    bus: InterAgentBus, reply_to: str
) -> None:
    with pytest.raises(HeadlessAsyncRejected, match=r"ask_agent\.py"):
        bus.send_async("worker", "main", "do work", opts=AsyncSendOptions(reply_to=reply_to))
    assert not bus._async_tasks


def test_bus_rejects_headless_result_receiver_with_handler(bus: InterAgentBus) -> None:
    bus.set_async_result_handler("worker", AsyncMock())
    with pytest.raises(HeadlessAsyncRejected, match="cannot receive async results"):
        bus.send_async("main", "main", "do work", opts=AsyncSendOptions(reply_to="worker"))
    assert not bus._async_tasks


@pytest.mark.parametrize("reply_to", ["", "main"])
async def test_http_rejects_headless_async_sender(
    client: TestClient, bus: InterAgentBus, reply_to: str
) -> None:
    response = await client.post(
        "/interagent/send_async",
        json={"from": "worker", "to": "main", "message": "do work", "reply_to": reply_to},
    )
    data = await response.json()
    assert data["success"] is False
    assert "ask_agent.py" in data["error"]
    assert not bus._async_tasks


async def test_http_rejects_headless_result_receiver_with_handler(
    client: TestClient, bus: InterAgentBus
) -> None:
    bus.set_async_result_handler("worker", AsyncMock())
    response = await client.post(
        "/interagent/send_async",
        json={
            "from": "main",
            "to": "main",
            "message": "do work",
            "reply_to": "worker",
        },
    )
    data = await response.json()
    assert data["success"] is False
    assert "headless" in data["error"]
    assert not bus._async_tasks


async def test_http_accepts_headless_target_with_main_result_receiver(
    client: TestClient, bus: InterAgentBus
) -> None:
    done = asyncio.Event()
    bus.set_async_result_handler("main", AsyncMock(side_effect=lambda _result: done.set()))
    response = await client.post(
        "/interagent/send_async",
        json={"from": "main", "to": "worker", "message": "do work"},
    )
    assert (await response.json())["success"] is True
    await asyncio.wait_for(done.wait(), timeout=1)


async def test_http_sync_to_headless_remains_allowed(client: TestClient) -> None:
    response = await client.post(
        "/interagent/send",
        json={"from": "main", "to": "worker", "message": "question"},
    )
    assert (await response.json())["success"] is True


async def test_http_rejects_headless_task_create(client: TestClient, bus: InterAgentBus) -> None:
    response = await client.post(
        "/tasks/create", json={"from": "worker", "prompt": "background work"}
    )
    data = await response.json()
    assert data["success"] is False
    assert "background tasks" in data["error"]
    assert not bus._async_tasks


async def test_http_requires_registered_async_result_receiver(
    client: TestClient, bus: InterAgentBus
) -> None:
    for sender, reply_to in [
        ("unknown", ""),
        ("main", "missing"),
        ("main", "worker"),
    ]:
        response = await client.post(
            "/interagent/send_async",
            json={"from": sender, "to": "worker", "message": "do work", "reply_to": reply_to},
        )
        data = await response.json()
        assert data["success"] is False
        assert ("headless" if reply_to == "worker" else "no async result receiver") in data["error"]
        assert not bus._async_tasks

    done = asyncio.Event()

    async def receive(_result: AsyncInterAgentResult) -> None:
        done.set()

    bus.set_async_result_handler("main", receive)
    response = await client.post(
        "/interagent/send_async",
        json={"from": "main", "to": "worker", "message": "do work"},
    )
    assert (await response.json())["success"] is True
    await asyncio.wait_for(done.wait(), timeout=1)

    bus.set_async_result_handler("worker", receive)
    response = await client.post(
        "/interagent/send_async",
        json={
            "from": "unknown",
            "to": "worker",
            "message": "do work",
            "reply_to": "main",
        },
    )
    assert (await response.json())["success"] is True


async def test_telegram_injection_log_contains_route(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bot = TelegramBot.__new__(TelegramBot)
    bot._config = AgentConfig(allowed_user_ids=[1])
    bot._agent_name = "main"
    bot._bus = MagicMock()
    bot._bus.submit = AsyncMock()
    result = AsyncInterAgentResult(
        task_id="task-1",
        sender="main",
        recipient="worker",
        message_preview="do work",
        result_text="done",
        original_message="do work",
        chat_id=-1001,
        topic_id=7,
        transport="tg",
    )
    with caplog.at_level("INFO", logger="ductor_bot.messenger.telegram.app"):
        await bot.on_async_interagent_result(result)
    assert "chat_id=-1001 topic_id=7 transport=tg" in caplog.text


@pytest.mark.parametrize("name", ["../x", "a/b", ".."])
async def test_supervisor_rejects_unsafe_sub_name(tmp_path: Path, name: str) -> None:
    supervisor = AgentSupervisor(AgentConfig(ductor_home=str(tmp_path)))
    with (
        patch.object(supervisor, "_notify_main_agent", new_callable=AsyncMock) as notify,
        patch(
            "ductor_bot.multiagent.supervisor.AgentStack.create", new_callable=AsyncMock
        ) as create,
    ):
        await supervisor._start_sub_agent(SubAgentConfig(name=name))
    create.assert_not_awaited()
    notify.assert_awaited_once()
    assert name not in supervisor.stacks


async def test_supervisor_accepts_dotted_sub_name(tmp_path: Path) -> None:
    supervisor = AgentSupervisor(AgentConfig(ductor_home=str(tmp_path)))
    stack = MagicMock()
    stack.shutdown = AsyncMock()
    with (
        patch(
            "ductor_bot.multiagent.supervisor.AgentStack.create", new_callable=AsyncMock
        ) as create,
        patch.object(supervisor, "_supervised_run", new_callable=AsyncMock, return_value=0),
    ):
        create.return_value = stack
        await supervisor._start_sub_agent(SubAgentConfig(name="alpha.v2"))
        assert "alpha.v2" in supervisor.stacks
        await supervisor.stop_agent("alpha.v2")
    create.assert_awaited_once()
