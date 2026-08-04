import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlencode

from curl_cffi import requests as curl_requests
from curl_cffi.curl import CurlError
from loguru import logger

from tracefold.market import GmgnStreamExpectedError

GMGN_WS_ENDPOINT = "wss://gmgn.ai/ws"
GMGN_WS_MAX_MESSAGE_BYTES = 1 * 1024 * 1024
GMGN_WS_RECV_QUEUE_SIZE = 8
GMGN_WS_SEND_QUEUE_SIZE = 4
GMGN_WS_MAX_RECONNECT_DELAY_SECONDS = 60.0
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)
WS_CONNECTION_STATES = frozenset({"disconnected", "connecting", "authenticating", "subscribed", "streaming", "failed"})


class UpstreamIdleTimeoutError(TimeoutError):
    pass


def build_gmgn_ws_url(
    app_version: str,
    *,
    device_id: str | None = None,
    fp_did: str | None = None,
    client_uuid: str | None = None,
    app_lang: str = "zh-CN",
    timezone_name: str = "Asia/Shanghai",
    timezone_offset: int = 28800,
    worker: int = 0,
    reconnect: int = 0,
) -> str:
    device_id = device_id or str(uuid.uuid4())
    fp_did = fp_did or str(uuid.uuid4())
    client_uuid = client_uuid or str(uuid.uuid4())
    params = {
        "device_id": device_id,
        "fp_did": fp_did,
        "client_id": f"gmgn_web_{app_version}",
        "from_app": "gmgn",
        "app_ver": app_version,
        "tz_name": timezone_name,
        "tz_offset": str(timezone_offset),
        "app_lang": app_lang,
        "os": "web",
        "worker": str(worker),
        "uuid": client_uuid,
        "reconnect": str(reconnect),
    }
    return f"{GMGN_WS_ENDPOINT}?{urlencode(params)}"


def build_subscribe_message(
    channel: str,
    data: list[dict[str, Any]],
    *,
    subscription_id: str | None = None,
    access_token: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "action": "subscribe",
        "channel": channel.split(":", 1)[0],
        "f": "w",
        "id": subscription_id or str(uuid.uuid4()),
        "data": data,
    }
    if access_token:
        payload["access_token"] = access_token
    return payload


def build_heartbeat_message(*, client_ts: int | None = None) -> dict[str, int | str]:
    return {
        "action": "heartbeat",
        "client_ts": client_ts if client_ts is not None else int(time.time() * 1000),
    }


class DirectGmgnWebSocketClient:
    """Anonymous GMGN upstream WebSocket client.

    This mirrors GMGN web's public subscription protocol without running a
    browser in the service hot path.
    """

    def __init__(
        self,
        *,
        app_version: str,
        channels: list[str],
        chains: list[str],
        on_frame: Callable[[str], Awaitable[None]],
        proxy: str | None = None,
        reconnect_delay: float = 3,
        heartbeat_interval: float = 25,
        idle_timeout: float = 90,
        user_agent: str = DEFAULT_USER_AGENT,
        session_factory: Callable[[], Any] | None = None,
    ):
        self.app_version = app_version
        self.channels = channels
        self.chains = [item for item in chains if item]
        self.on_frame = on_frame
        self.proxy = proxy
        self.reconnect_delay = reconnect_delay
        self.heartbeat_interval = heartbeat_interval
        self.idle_timeout = idle_timeout
        self.user_agent = user_agent
        self._session_factory = session_factory or curl_requests.AsyncSession
        self.connection_state = "disconnected"
        self.last_state_change_at_ms = _now_ms()
        self._consecutive_failures = 0
        self._last_failure_key: tuple[str, str] | None = None
        self._same_failure_count = 0

    def connection_state_payload(self) -> dict[str, Any]:
        return {
            "provider": "gmgn_direct_ws",
            "state": self.connection_state,
            "last_state_change_at_ms": self.last_state_change_at_ms,
        }

    async def aclose(self) -> None:
        self._set_connection_state("disconnected")

    async def run(self) -> None:
        reconnect_count = 0
        self._reset_retry_state()
        try:
            while True:
                retry_delay = self.reconnect_delay
                try:
                    await self._run_once(reconnect_count=reconnect_count)
                    reconnect_count += 1
                    self._reset_retry_state()
                except asyncio.CancelledError:
                    raise
                except (
                    GmgnStreamExpectedError,
                    UpstreamIdleTimeoutError,
                    CurlError,
                    OSError,
                ) as exc:
                    reconnect_count += 1
                    self._consecutive_failures += 1
                    retry_delay = _bounded_reconnect_delay(
                        self.reconnect_delay,
                        consecutive_failures=self._consecutive_failures,
                    )
                    self._set_connection_state("failed")
                    self._log_retry_failure(exc, retry_delay=retry_delay)

                await asyncio.sleep(retry_delay)
        finally:
            self._set_connection_state("disconnected")

    async def _run_once(self, *, reconnect_count: int = 0) -> None:
        ws_url = build_gmgn_ws_url(
            self.app_version,
            reconnect=1 if reconnect_count else 0,
        )
        headers = {
            "Origin": "https://gmgn.ai",
            "User-Agent": self.user_agent,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        connect_kwargs = {
            "headers": headers,
            "timeout": 15,
            "impersonate": "chrome",
            "proxy": self.proxy,
            "max_message_size": GMGN_WS_MAX_MESSAGE_BYTES,
            "recv_queue_size": GMGN_WS_RECV_QUEUE_SIZE,
            "send_queue_size": GMGN_WS_SEND_QUEUE_SIZE,
            "block_on_recv_queue_full": True,
        }

        self._set_connection_state("connecting")
        async with self._session_factory() as session:
            websocket = await session.ws_connect(ws_url, **connect_kwargs)
            self._set_connection_state("authenticating")
            logger.success(f"GMGN 直连 WS 已连接，匿名订阅频道: {', '.join(self.channels)}")
            try:
                await self._subscribe_all(websocket)
                self._set_connection_state("subscribed")
                receive_task = asyncio.create_task(
                    self._receive_frames(websocket),
                    name="gmgn-receiver",
                )
                heartbeat_task = asyncio.create_task(
                    self._heartbeat_loop(websocket),
                    name="gmgn-heartbeat",
                )
                stream_tasks = (receive_task, heartbeat_task)
                try:
                    done, _ = await asyncio.wait(
                        stream_tasks,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in stream_tasks:
                        if task in done:
                            await task
                    raise RuntimeError("gmgn_stream_child_returned")
                finally:
                    for task in stream_tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*stream_tasks, return_exceptions=True)
            finally:
                await websocket.close()
                if self.connection_state != "failed":
                    self._set_connection_state("disconnected")

    async def _receive_frames(self, websocket) -> None:
        while True:
            try:
                frame = await asyncio.wait_for(websocket.recv_str(), timeout=self.idle_timeout)
            except TimeoutError as exc:
                raise UpstreamIdleTimeoutError(f"no upstream frame received for {self.idle_timeout:g}s") from exc
            if len(frame.encode("utf-8")) > GMGN_WS_MAX_MESSAGE_BYTES:
                raise GmgnStreamExpectedError("gmgn_frame_byte_limit_exceeded")
            self._reset_retry_state()
            self._set_connection_state("streaming")
            await self.on_frame(frame)
            await asyncio.sleep(0)

    async def _subscribe_all(self, websocket) -> None:
        data = [{"chain": chain} for chain in self.chains]
        for channel in self.channels:
            message = build_subscribe_message(channel, data)
            await websocket.send_str(json.dumps(message, ensure_ascii=False, separators=(",", ":")))
            logger.info(f"📡 已订阅 GMGN 匿名频道: {channel} chains={','.join(self.chains)}")

    async def _heartbeat_loop(self, websocket) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_interval)
            message = build_heartbeat_message()
            await websocket.send_str(json.dumps(message, separators=(",", ":")))

    def _set_connection_state(self, state: str) -> None:
        if state not in WS_CONNECTION_STATES:
            raise ValueError(f"unsupported GMGN WS state: {state}")
        if state == self.connection_state:
            return
        self.connection_state = state
        self.last_state_change_at_ms = _now_ms()
        log = logger.info if state in {"disconnected", "subscribed", "streaming"} else logger.debug
        log(
            "GMGN direct WS connection state changed | state={} last_state_change_at_ms={}",
            self.connection_state,
            self.last_state_change_at_ms,
        )

    def _reset_retry_state(self) -> None:
        self._consecutive_failures = 0
        self._last_failure_key = None
        self._same_failure_count = 0

    def _log_retry_failure(self, exc: BaseException, *, retry_delay: float) -> None:
        failure_key = (type(exc).__name__, str(exc))
        if failure_key != self._last_failure_key:
            self._last_failure_key = failure_key
            self._same_failure_count = 1
            logger.error(
                "GMGN direct WS unavailable | error_type={} error={} retry_in_seconds={} failures={}",
                failure_key[0],
                failure_key[1],
                retry_delay,
                self._consecutive_failures,
            )
            return

        self._same_failure_count += 1
        log = logger.warning if _is_power_of_two(self._same_failure_count) else logger.debug
        log(
            "GMGN direct WS still unavailable | error_type={} error={} retry_in_seconds={} failures={}",
            failure_key[0],
            failure_key[1],
            retry_delay,
            self._consecutive_failures,
        )


def _bounded_reconnect_delay(initial_delay: float, *, consecutive_failures: int) -> float:
    delay = min(GMGN_WS_MAX_RECONNECT_DELAY_SECONDS, max(0.0, initial_delay))
    if delay == 0:
        return 0
    remaining_doublings = max(0, consecutive_failures - 1)
    while remaining_doublings and delay < GMGN_WS_MAX_RECONNECT_DELAY_SECONDS:
        delay = min(GMGN_WS_MAX_RECONNECT_DELAY_SECONDS, delay * 2)
        remaining_doublings -= 1
    return delay


def _is_power_of_two(value: int) -> bool:
    return value > 0 and value & (value - 1) == 0


def _now_ms() -> int:
    return int(time.time() * 1000)
