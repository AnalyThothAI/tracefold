from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from contextlib import suppress
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed, InvalidHandshake, PayloadTooBig, ProtocolError

from tracefold.news import OpenNewsExpectedError

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
    "OPENNEWS_WSS_URL",
    "OpenNewsWebSocketClient",
]
