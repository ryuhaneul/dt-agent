"""Token-authenticated HTTP and a single reconnecting bridge event stream."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
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
        headers: Mapping[str, str] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        resource = path.partition("?")[0]
        topic_mutation = (method == "POST" and resource == "/topics") or (
            method in {"PATCH", "DELETE"} and re.fullmatch(r"/topics/[^/]+", resource) is not None
        )
        abort = (
            method == "POST"
            and re.fullmatch(r"/topics/[^/]+/(?:abort|messages|buttons|callbacks)", resource)
            is not None
        )
        history_read = method == "GET" and (
            resource == "/topics" or re.fullmatch(r"/topics/[^/]+/messages", resource) is not None
        )
        upload = re.fullmatch(r"/topics/[^/]+/uploads(?:/[^/]+(?:/complete)?)?", resource)
        timeout = (
            aiohttp.ClientTimeout(total=120, sock_connect=5, sock_read=30)
            if upload
            else (
                aiohttp.ClientTimeout(total=45)
                if topic_mutation or abort or history_read
                else self._client.timeout
            )
        )
        try:
            async with self._client.request(
                method,
                self._url + path,
                data=body,
                headers={**self._headers, **(headers or {}), "Content-Type": "application/json"},
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

    async def upload_chunk(
        self,
        path: str,
        chunks: AsyncIterator[bytes],
        *,
        owner: str,
        offset: str,
        length: int | None,
    ) -> tuple[int, dict[str, Any], str | None]:
        """Send binary chunks without buffering or forwarding browser credentials."""
        headers = {
            **self._headers,
            "X-Dtw-Owner": owner,
            "Upload-Offset": offset,
            "Content-Type": "application/octet-stream",
        }
        if length is not None:
            headers["Content-Length"] = str(length)
        try:
            async with self._client.put(
                self._url + path,
                data=chunks,
                headers=headers,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=120, sock_connect=5, sock_read=30),
            ) as response:
                data = await response.json()
                status = response.status
                received = response.headers.get("Upload-Offset")
        except (aiohttp.ClientError, TimeoutError, ValueError):
            raise BridgeUnavailableError from None
        if not isinstance(data, dict):
            raise BridgeUnavailableError
        return status, data, received

    @asynccontextmanager
    async def download(
        self, method: str, path: str, *, headers: Mapping[str, str]
    ) -> AsyncIterator[aiohttp.ClientResponse]:
        """Keep the upstream response open while the browser consumes bounded chunks."""
        forwarded = {name: headers[name] for name in ("Range",) if name in headers}
        try:
            async with self._client.request(
                method,
                self._url + path,
                headers={**self._headers, **forwarded},
                allow_redirects=False,
                auto_decompress=False,
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=5, sock_read=30),
            ) as response:
                yield response
        except (aiohttp.ClientError, TimeoutError):
            raise BridgeUnavailableError from None

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
