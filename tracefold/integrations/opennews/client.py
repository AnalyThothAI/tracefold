from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from contextlib import suppress
from typing import Any

import httpx
import websockets
from websockets.exceptions import (
    ConnectionClosed,
    InvalidHandshake,
    InvalidStatus,
    PayloadTooBig,
    ProtocolError,
)

from tracefold.news import OpenNewsExpectedError
from tracefold.news.opennews import OpenNewsHistoryError, OpenNewsHistoryPayloadReason

OPENNEWS_WSS_URL = "wss://ai.6551.io/open/news_wss"
OPENNEWS_HTTP_BASE_URL = "https://ai.6551.io/open"
OPENNEWS_MAX_FRAME_BYTES = 1 * 1024 * 1024
OPENNEWS_HISTORY_MAX_BODY_BYTES = 8 * 1024 * 1024
_OPENNEWS_HISTORY_CHUNK_BYTES = 64 * 1024
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
        except InvalidStatus as exc:
            await self.close()
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            code = "opennews_authentication_failed" if status_code in {401, 403} else "opennews_handshake_failed"
            raise OpenNewsExpectedError(code, status_code=status_code) from None
        except InvalidHandshake:
            await self.close()
            raise OpenNewsExpectedError("opennews_handshake_failed") from None
        except (PayloadTooBig, ProtocolError):
            await self.close()
            raise OpenNewsExpectedError("opennews_protocol_error") from None
        except (OSError, TimeoutError, ConnectionClosed):
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
            try:
                return _json_object(raw)
            except OpenNewsExpectedError as exc:
                if exc.code != "opennews_frame_invalid":
                    raise
                return {}
        except OpenNewsExpectedError:
            raise
        except ConnectionClosed as exc:
            raise OpenNewsExpectedError(
                "opennews_receive_failed",
                status_code=getattr(exc, "code", None),
            ) from None
        except (PayloadTooBig, ProtocolError):
            raise OpenNewsExpectedError("opennews_protocol_error") from None
        except (OSError, TimeoutError, InvalidHandshake):
            raise OpenNewsExpectedError("opennews_receive_failed") from None

    async def close(self) -> None:
        websocket = self._websocket
        self._websocket = None
        if websocket is not None:
            with suppress(*_EXPECTED_WEBSOCKET_FAILURES):
                await websocket.close()


class OpenNewsStrategyHistoryClient:
    """Official authenticated Strategy list/history adapter; never News Search."""

    def __init__(
        self,
        *,
        token: str,
        base_url: str = OPENNEWS_HTTP_BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=str(base_url).rstrip("/"),
            headers={"Authorization": f"Bearer {token}", "Accept-Encoding": "identity"},
            timeout=httpx.Timeout(10.0),
            follow_redirects=False,
            transport=transport,
        )

    async def get_strategy_list(self, *, limit: int, page: int) -> Mapping[str, Any]:
        return await self._get("/strategy_list", params={"limit": int(limit), "page": int(page)})

    async def get_strategy_hits(
        self,
        *,
        strategy_id: str,
        limit: int,
        page: int,
    ) -> Mapping[str, Any]:
        return await self._get(
            "/strategy_hits",
            params={"strategyId": str(strategy_id), "limit": int(limit), "page": int(page)},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, *, params: Mapping[str, Any]) -> Mapping[str, Any]:
        try:
            async with self._client.stream("GET", path, params=params) as response:
                if response.status_code == 404:
                    raise OpenNewsHistoryError("opennews_history_unavailable")
                if response.status_code in {401, 403}:
                    raise OpenNewsHistoryError("opennews_history_authentication")
                if response.status_code == 429:
                    raise OpenNewsHistoryError("opennews_history_rate_limited")
                response.raise_for_status()
                if response.headers.get("Content-Encoding", "identity").strip().lower() != "identity":
                    raise OpenNewsHistoryError("opennews_history_content_encoding_unsupported")
                if response.is_stream_consumed:
                    if len(response.content) > OPENNEWS_HISTORY_MAX_BODY_BYTES:
                        raise OpenNewsHistoryError("opennews_history_payload_too_large")
                    body = bytearray(response.content)
                else:
                    body = bytearray()
                    async for chunk in response.aiter_raw(chunk_size=_OPENNEWS_HISTORY_CHUNK_BYTES):
                        if len(body) + len(chunk) > OPENNEWS_HISTORY_MAX_BODY_BYTES:
                            raise OpenNewsHistoryError("opennews_history_payload_too_large")
                        body.extend(chunk)
        except OpenNewsHistoryError:
            raise
        except httpx.TimeoutException:
            raise OpenNewsHistoryError("opennews_history_timeout") from None
        except httpx.HTTPError:
            raise OpenNewsHistoryError("opennews_history_http_error") from None
        try:
            payload = json.loads(body)
        except (RecursionError, ValueError):
            raise OpenNewsHistoryError.invalid_payload(OpenNewsHistoryPayloadReason.JSON_INVALID) from None
        if not isinstance(payload, Mapping):
            raise OpenNewsHistoryError.invalid_payload(OpenNewsHistoryPayloadReason.ROOT_NOT_OBJECT)
        return payload


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
    "OPENNEWS_HTTP_BASE_URL",
    "OPENNEWS_WSS_URL",
    "OpenNewsStrategyHistoryClient",
    "OpenNewsWebSocketClient",
]
