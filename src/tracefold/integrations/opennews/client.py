from __future__ import annotations

import json
from collections.abc import Mapping
from contextlib import suppress
from typing import Any

import httpx
import websockets

from tracefold.news import (
    OPENNEWS_REST_LIMIT,
    OpenNewsEvent,
    OpenNewsExpectedError,
    parse_opennews_rest_response,
)

OPENNEWS_REST_URL = "https://ai.6551.io/open/news_search"
OPENNEWS_WSS_URL = "wss://ai.6551.io/open/news_wss"
OPENNEWS_MAX_FRAME_BYTES = 1 * 1024 * 1024
OPENNEWS_WS_IDLE_SECONDS = 45.0


class OpenNewsRestClient:
    """One bounded synchronous recovery call; Runtime owns retry cadence."""

    def __init__(self, *, token: str, timeout_seconds: float = 20.0) -> None:
        self._client = httpx.Client(
            timeout=httpx.Timeout(timeout_seconds),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )

    def fetch_latest(self) -> tuple[OpenNewsEvent, ...]:
        try:
            response = self._client.post(
                OPENNEWS_REST_URL,
                json={"engineTypes": {"news": []}, "limit": OPENNEWS_REST_LIMIT, "page": 1},
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            status = int(exc.response.status_code)
            code = "opennews_auth_failed" if status in {401, 403} else "opennews_http_failed"
            raise OpenNewsExpectedError(code, status_code=status) from None
        except (httpx.HTTPError, ValueError):
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
                await websocket.close()
                raise OpenNewsExpectedError("opennews_subscribe_failed")
            self._websocket = websocket
        except OpenNewsExpectedError:
            raise
        except Exception:
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
        except Exception:
            raise OpenNewsExpectedError("opennews_receive_failed") from None

    async def close(self) -> None:
        websocket = self._websocket
        self._websocket = None
        if websocket is not None:
            with suppress(Exception):
                await websocket.close()


async def _bounded_recv(websocket: Any) -> Any:
    import asyncio

    while True:
        try:
            return await asyncio.wait_for(websocket.recv(), timeout=OPENNEWS_WS_IDLE_SECONDS)
        except TimeoutError:
            try:
                pong = await websocket.ping()
                await asyncio.wait_for(pong, timeout=5.0)
            except Exception:
                raise OpenNewsExpectedError("opennews_idle_timeout") from None


def _json_object(raw: object) -> Mapping[str, Any]:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        payload = json.loads(str(raw))
    except (TypeError, ValueError):
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
