"""Token-authenticated HTTP and a single reconnecting bridge event stream."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import aiohttp

if TYPE_CHECKING:
    from aiohttp import ClientSession


class BridgeUnavailableError(Exception):
    """The bot could not be reached; never carries upstream credentials."""


class BridgeClient:
    def __init__(self, url: str, token: str, client: ClientSession) -> None:
        self._url = url.rstrip("/") + "/webchat/v1"
        self._headers = {"X-Dtw-Bridge-Token": token}
        self._client = client
        self.up = False

    async def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
    ) -> tuple[int, dict[str, Any]]:
        resource = path.partition("?")[0]
        topic_mutation = (method == "POST" and resource == "/topics") or (
            method in {"PATCH", "DELETE"} and re.fullmatch(r"/topics/[^/]+", resource) is not None
        )
        abort = (
            method == "POST"
            and re.fullmatch(r"/topics/[^/]+/(?:abort|messages)", resource) is not None
        )
        timeout = (
            aiohttp.ClientTimeout(total=45) if topic_mutation or abort else self._client.timeout
        )
        try:
            async with self._client.request(
                method,
                self._url + path,
                data=body,
                headers={**self._headers, "Content-Type": "application/json"},
                allow_redirects=False,
                timeout=timeout,
            ) as response:
                data = await response.json()
                status = response.status
        except (aiohttp.ClientError, TimeoutError, ValueError):
            raise BridgeUnavailableError from None
        if not isinstance(data, dict):
            raise BridgeUnavailableError
        return status, data

    async def events(self, publish: Callable[[str], Awaitable[None]]) -> None:
        delay = 2
        while True:
            try:
                async with self._client.ws_connect(
                    self._url + "/events",
                    headers=self._headers,
                    heartbeat=30,
                ) as socket:
                    self.up = True
                    delay = 2
                    await publish('{"type":"bridge","up":true}')
                    async for message in socket:
                        if message.type == aiohttp.WSMsgType.TEXT:
                            await publish(message.data)
                        elif message.type == aiohttp.WSMsgType.ERROR:
                            break
            except (aiohttp.ClientError, TimeoutError):
                pass
            self.up = False
            await publish('{"type":"bridge","up":false}')
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)
