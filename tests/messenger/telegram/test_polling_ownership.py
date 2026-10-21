"""TelegramBot owns its polling task: a cancelled or stopped bot leaves no poller behind."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiohttp import web

from ductor_bot.config import AgentConfig
from ductor_bot.messenger.multi import MultiBotAdapter
from ductor_bot.messenger.telegram import app as tg_app
from ductor_bot.messenger.telegram.app import TelegramBot


class FakeTelegramApi:
    """Loopback server standing in for the Telegram Bot API."""

    def __init__(self) -> None:
        self.get_updates = 0
        self.hold_delete_webhook = asyncio.Event()
        self.delete_webhook_calls = 0
        self.block_delete_webhook = False
        self.base_url = ""
        self._runner: web.AppRunner | None = None

    async def _handle(self, request: web.Request) -> web.Response:
        method = request.match_info["method"]
        if method == "getMe":
            me = {"id": 123, "is_bot": True, "first_name": "t", "username": "t_bot"}
            return web.json_response({"ok": True, "result": me})
        if method == "getUpdates":
            self.get_updates += 1
            await asyncio.sleep(0.1)
            return web.json_response({"ok": True, "result": []})
        if method == "deleteWebhook":
            self.delete_webhook_calls += 1
            if self.block_delete_webhook:
                await self.hold_delete_webhook.wait()
        return web.json_response({"ok": True, "result": True})

    async def start(self) -> None:
        app = web.Application()
        app.router.add_post("/bot{token}/{method}", self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr,index]
        self.base_url = f"http://127.0.0.1:{port}"

    async def stop(self) -> None:
        self.hold_delete_webhook.set()
        if self._runner is not None:
            await self._runner.cleanup()


@pytest.fixture
async def api() -> AsyncIterator[FakeTelegramApi]:
    fake = FakeTelegramApi()
    await fake.start()
    yield fake
    await fake.stop()


@pytest.fixture
async def make_bot(api: FakeTelegramApi, tmp_path: Path) -> AsyncIterator[Callable[[], TelegramBot]]:
    created: list[Bot] = []

    def factory(*_args: object, **_kwargs: object) -> Bot:
        session = AiohttpSession(api=TelegramAPIServer.from_base(api.base_url))
        bot = Bot(token="123:fake-token", session=session)
        created.append(bot)
        return bot

    def build() -> TelegramBot:
        config = AgentConfig(
            telegram_token="123:fake-token", allowed_user_ids=[100], ductor_home=str(tmp_path)
        )
        with patch("ductor_bot.messenger.telegram.app.Bot", side_effect=factory):
            return TelegramBot(config)

    with patch(
        "ductor_bot.messenger.telegram.startup.run_startup", new=AsyncMock(return_value=None)
    ):
        yield build
    for bot in created:
        await bot.session.close()


def _polling_tasks() -> list[asyncio.Task[object]]:
    return [
        t
        for t in asyncio.all_tasks()
        if not t.done() and "Dispatcher._polling" in repr(t.get_coro())
    ]


async def _until(predicate: Callable[[], bool], seconds: float = 5.0) -> None:
    async with asyncio.timeout(seconds):
        while not predicate():  # noqa: ASYNC110 - polls plain state, there is no event
            await asyncio.sleep(0.02)


async def _cancel(task: asyncio.Task[object]) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


class TestCancelledRunLeavesNoPoller:
    async def test_cancel_stops_polling_and_requests(
        self, api: FakeTelegramApi, make_bot: Callable[[], TelegramBot]
    ) -> None:
        bot = make_bot()
        run = asyncio.create_task(bot.run())
        await _until(lambda: api.get_updates >= 2 and bool(_polling_tasks()))

        await _cancel(run)

        assert _polling_tasks() == []
        requests_after_cancel = api.get_updates
        await asyncio.sleep(0.6)
        assert api.get_updates == requests_after_cancel

    async def test_cancel_before_polling_started(
        self, api: FakeTelegramApi, make_bot: Callable[[], TelegramBot]
    ) -> None:
        api.block_delete_webhook = True
        bot = make_bot()
        run = asyncio.create_task(bot.run())
        await _until(lambda: api.delete_webhook_calls >= 1)

        await _cancel(run)

        assert _polling_tasks() == []
        await asyncio.sleep(0.3)
        assert api.get_updates == 0

    async def test_forced_cancel_when_graceful_stop_stalls(
        self,
        api: FakeTelegramApi,
        make_bot: Callable[[], TelegramBot],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(tg_app, "_POLLING_STOP_SECONDS", 0.2)
        monkeypatch.setattr(tg_app, "_POLLING_FORCE_SECONDS", 0.6)
        bot = make_bot()

        async def hang(*_args: object, **_kwargs: object) -> None:
            await asyncio.sleep(3600)

        # The dispatcher's own shutdown never finishes, so the graceful stop cannot complete.
        monkeypatch.setattr(bot._dp, "emit_shutdown", hang)
        run = asyncio.create_task(bot.run())
        await _until(lambda: api.get_updates >= 1 and bool(_polling_tasks()))

        async with asyncio.timeout(5):
            await _cancel(run)

        assert _polling_tasks() == []
        assert bot._dp.polling_children() == []
        assert bot._polling_task is not None
        assert bot._polling_task.done()
        requests_after_cancel = api.get_updates
        await asyncio.sleep(0.4)
        assert api.get_updates == requests_after_cancel


class TestRequestStop:
    async def test_request_stop_ends_run_by_itself(
        self, api: FakeTelegramApi, make_bot: Callable[[], TelegramBot]
    ) -> None:
        bot = make_bot()
        run = asyncio.create_task(bot.run())
        await _until(lambda: api.get_updates >= 1 and bool(_polling_tasks()))

        assert bot.request_stop() is True

        assert await asyncio.wait_for(run, 5) == 0
        assert _polling_tasks() == []

    async def test_request_stop_before_run(
        self, api: FakeTelegramApi, make_bot: Callable[[], TelegramBot]
    ) -> None:
        bot = make_bot()
        assert bot.request_stop() is True

        assert await asyncio.wait_for(bot.run(), 5) == 0

        assert api.get_updates == 0
        assert _polling_tasks() == []

    async def test_request_stop_during_startup_is_honoured(
        self, api: FakeTelegramApi, make_bot: Callable[[], TelegramBot]
    ) -> None:
        bot = make_bot()
        run = asyncio.create_task(bot.run())
        await _until(lambda: bot._polling_task is not None)
        bot.request_stop()

        assert await asyncio.wait_for(run, 5) == 0
        assert _polling_tasks() == []

    async def test_cancel_after_run_already_returned_is_a_no_op(
        self, api: FakeTelegramApi, make_bot: Callable[[], TelegramBot]
    ) -> None:
        bot = make_bot()
        run = asyncio.create_task(bot.run())
        await _until(lambda: api.get_updates >= 1 and bool(_polling_tasks()))
        bot.request_stop()
        await asyncio.wait_for(run, 5)

        run.cancel()
        assert run.done()
        from ductor_bot.infra.shutdown_budget import ShutdownBudget

        await bot._stop_polling_owned(ShutdownBudget())
        assert _polling_tasks() == []


# -- MultiBotAdapter: cancelling it cleans up every child bot ------------------------------


def _fake_bot(
    cleaned: list[str], name: str, *, signal_ready: bool = False
) -> tuple[MagicMock, list[Callable[[], Awaitable[None]]]]:
    bot = MagicMock()
    hooks: list[Callable[[], Awaitable[None]]] = []
    bot.orchestrator = MagicMock()
    bot.register_startup_hook = MagicMock(side_effect=hooks.append)
    bot.shutdown = AsyncMock()

    async def run() -> int:
        try:
            if signal_ready:
                for hook in hooks:
                    await hook()
            await asyncio.sleep(3600)
        finally:
            await asyncio.sleep(0)  # cleanup that needs a loop turn
            cleaned.append(name)
        return 0

    bot.run = run
    return bot, hooks


def _adapter(primary: MagicMock, secondary: MagicMock) -> MultiBotAdapter:
    config = MagicMock()
    config.transports = ["telegram", "matrix"]
    config.transport = "telegram"
    config.is_multi_transport = True
    with patch(
        "ductor_bot.messenger.registry._create_single_bot", side_effect=[primary, secondary]
    ):
        return MultiBotAdapter(config, agent_name="main")


class TestMultiBotAdapterCancel:
    async def test_cancel_waits_for_every_child(self) -> None:
        cleaned: list[str] = []
        primary, _ = _fake_bot(cleaned, "primary", signal_ready=True)
        secondary, _ = _fake_bot(cleaned, "secondary")
        adapter = _adapter(primary, secondary)
        run = asyncio.create_task(adapter.run())
        await _until(lambda: secondary.register_startup_hook.call_count == 0 and not run.done())
        await asyncio.sleep(0.1)

        await _cancel(run)

        assert sorted(cleaned) == ["primary", "secondary"]

    async def test_cancel_while_waiting_for_primary_startup(self) -> None:
        cleaned: list[str] = []
        primary, _ = _fake_bot(cleaned, "primary", signal_ready=False)
        secondary, _ = _fake_bot(cleaned, "secondary")
        adapter = _adapter(primary, secondary)
        run = asyncio.create_task(adapter.run())
        await asyncio.sleep(0.1)  # primary started, never signals ready

        await _cancel(run)

        assert cleaned == ["primary"]

    async def test_request_stop_asks_every_bot(self) -> None:
        primary, _ = _fake_bot([], "primary")
        secondary, _ = _fake_bot([], "secondary")
        primary.request_stop = MagicMock(return_value=True)
        secondary.request_stop = MagicMock(return_value=False)
        adapter = _adapter(primary, secondary)

        assert adapter.request_stop() is False
        primary.request_stop.assert_called_once()
        secondary.request_stop.assert_called_once()
