from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from contextlib import suppress
from typing import Any

import httpx
import websockets
from websockets.exceptions import ConnectionClosed, InvalidHandshake, PayloadTooBig, ProtocolError

from tracefold.news import OpenNewsEvent, OpenNewsExpectedError
from tracefold.news.opennews import (
    OPENNEWS_REST_LIMIT,
    parse_opennews_rest_response,
)

OPENNEWS_REST_URL = "https://ai.6551.io/open/news_search"
OPENNEWS_WSS_URL = "wss://ai.6551.io/open/news_wss"
OPENNEWS_MAX_FRAME_BYTES = 1 * 1024 * 1024
OPENNEWS_WS_IDLE_SECONDS = 45.0
_EXPECTED_WEBSOCKET_FAILURES = (
    OSError,
    TimeoutError,
    ConnectionClosed,
    InvalidHandshake,
    PayloadTooBig,
    ProtocolError,
)


class OpenNewsRestClient:
    """One bounded synchronous recovery page; Runtime owns pagination and cadence."""

    def __init__(self, *, token: str, timeout_seconds: float = 20.0) -> None:
        self._client = httpx.Client(
            timeout=httpx.Timeout(timeout_seconds),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )

    def fetch_page(self, page: int) -> tuple[OpenNewsEvent, ...]:
        page_number = int(page)
        try:
            response = self._client.post(
                OPENNEWS_REST_URL,
                json={
                    "engineTypes": {"news": []},
                    "limit": OPENNEWS_REST_LIMIT,
                    "page": page_number,
                },
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status = int(exc.response.status_code)
            code = "opennews_auth_failed" if status in {401, 403} else "opennews_http_failed"
            raise OpenNewsExpectedError(code, status_code=status) from None
        except httpx.HTTPError:
            raise OpenNewsExpectedError("opennews_rest_failed") from None
        try:
            payload = response.json()
        except (RecursionError, ValueError):
            raise OpenNewsExpectedError("opennews_rest_failed") from None
        return parse_opennews_rest_response(payload)

    def close(self) -> None:
        self._client.close()


class OpenNewsWebSocketClient:
    """One connection at a time; the News acquisition Module owns reconnects."""

    def __init__(self, *, token: str) -> None:
        self._token = token
        self._websocket: Any | None = None

    async def connect(self) -> None:
        try:
            websocket = await websockets.connect(
                f"{OPENNEWS_WSS_URL}?token={self._token}",
                ping_interval=None,
                open_timeout=10.0,
                close_timeout=5.0,
                max_size=OPENNEWS_MAX_FRAME_BYTES,
                max_queue=16,
            )
            self._websocket = websocket
            await websocket.send(
                json.dumps(
                    {
                        "method": "news.subscribe",
                        "id": "tracefold-news",
                        "params": {"engineTypes": {"news": []}},
                    },
                    separators=(",", ":"),
                )
            )
            ack = _json_object(await _bounded_recv(websocket))
            if ack.get("error"):
                raise OpenNewsExpectedError("opennews_subscribe_failed")
        except OpenNewsExpectedError:
            await self.close()
            raise
        except asyncio.CancelledError:
            await self.close()
            raise
        except _EXPECTED_WEBSOCKET_FAILURES:
            await self.close()
            raise OpenNewsExpectedError("opennews_connect_failed") from None

    async def receive(self) -> Mapping[str, Any] | str:
        websocket = self._websocket
        if websocket is None:
            raise OpenNewsExpectedError("opennews_not_connected")
        try:
            raw = await _bounded_recv(websocket)
            if raw == "ping":
                await websocket.send("pong")
                return "ping"
            return _json_object(raw)
        except OpenNewsExpectedError:
            raise
        except _EXPECTED_WEBSOCKET_FAILURES:
            raise OpenNewsExpectedError("opennews_receive_failed") from None

    async def close(self) -> None:
        websocket = self._websocket
        self._websocket = None
        if websocket is not None:
            with suppress(*_EXPECTED_WEBSOCKET_FAILURES):
                await websocket.close()


async def _bounded_recv(websocket: Any) -> Any:
    while True:
        try:
            return await asyncio.wait_for(websocket.recv(), timeout=OPENNEWS_WS_IDLE_SECONDS)
        except TimeoutError:
            try:
                pong = await websocket.ping()
                await asyncio.wait_for(pong, timeout=5.0)
            except _EXPECTED_WEBSOCKET_FAILURES:
                raise OpenNewsExpectedError("opennews_idle_timeout") from None


def _json_object(raw: object) -> Mapping[str, Any]:
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        payload = json.loads(str(raw))
    except (RecursionError, TypeError, ValueError):
        raise OpenNewsExpectedError("opennews_frame_invalid") from None
    if not isinstance(payload, Mapping):
        raise OpenNewsExpectedError("opennews_frame_invalid")
    return payload


__all__ = [
    "OPENNEWS_REST_URL",
    "OPENNEWS_WSS_URL",
    "OpenNewsRestClient",
    "OpenNewsWebSocketClient",
]
