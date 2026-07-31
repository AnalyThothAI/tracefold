from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from collections.abc import AsyncIterator, Awaitable
from dataclasses import dataclass
from typing import Any

import websockets
from loguru import logger
from websockets.exceptions import WebSocketException

from tracefold.integrations.okx.dex_client import EVM_ADDRESS_RE
from tracefold.platform.validation import require_positive_int


class OkxDexWsClientError(RuntimeError):
    pass


class _OkxDexWsMissingPong(RuntimeError):
    pass


class _OkxDexWsReconnectFailed(RuntimeError):
    pass


OKX_DEX_WS_CONNECT_TIMEOUT_SECONDS = 10.0
OKX_DEX_WS_LOGIN_TIMEOUT_SECONDS = 5.0
OKX_DEX_WS_CLOSE_TIMEOUT_SECONDS = 5.0
OKX_DEX_WS_IDLE_PING_SECONDS = 25.0
OKX_DEX_WS_PONG_TIMEOUT_SECONDS = 5.0
OKX_DEX_WS_CIRCUIT_FAILURES = 3
OKX_DEX_WS_CIRCUIT_COOLDOWN_SECONDS = 60.0
OKX_DEX_WS_MAX_FRAME_BYTES = 1 * 1024 * 1024
OKX_DEX_WS_MAX_QUEUE = 16
OKX_DEX_WS_MAX_ITEMS_PER_FRAME = 500

WS_CONNECTION_STATES = frozenset(
    {
        "disconnected",
        "connecting",
        "authenticating",
        "subscribed",
        "streaming",
        "degraded_recoverable",
        "failed_terminal",
        "circuit_open",
    }
)

_AUTH_ERROR_CODES = frozenset({"60005", "60007", "60009", "60011", "60012", "60013", "60014"})


@dataclass(frozen=True, slots=True)
class OkxDexPriceInfoUpdate:
    chain_id: str
    address: str
    observed_at_ms: int
    price_usd: float | None = None
    market_cap_usd: float | None = None
    liquidity_usd: float | None = None
    volume_24h_usd: float | None = None
    open_interest_usd: float | None = None
    holders: int | None = None
    raw: dict[str, Any] | None = None


class OkxDexWebSocketMarketProvider:
    def __init__(
        self,
        *,
        url: str,
        api_key: str,
        secret_key: str,
        passphrase: str,
        subscription_limit: int,
    ) -> None:
        self.url = url
        self.api_key = api_key
        self.secret_key = secret_key
        self.passphrase = passphrase
        self.subscription_limit = require_positive_int(
            subscription_limit,
            error_code="okx_dex_ws_subscription_limit_required",
        )
        self.connection_state = "disconnected"
        self.last_state_change_at_ms = _now_ms()
        self.last_message_at_ms: int | None = None
        self.last_ping_at_ms: int | None = None
        self.last_pong_at_ms: int | None = None
        self.last_error_category: str | None = None
        self.last_error_code: str | None = None
        self.reconnect_count = 0
        self.data_frame_count = 0
        self.tick_count = 0
        self._websocket: Any | None = None
        self._desired_args: set[tuple[str, str]] = set()
        self._subscribed_args: set[tuple[str, str]] = set()
        self._recoverable_failure_count = 0
        self._circuit_opened_at_ms: int | None = None
        self._lock = asyncio.Lock()

    def connection_state_payload(self) -> dict[str, Any]:
        return {
            "provider": "okx_dex_ws",
            "state": self.connection_state,
            "last_state_change_at_ms": self.last_state_change_at_ms,
            "last_message_at_ms": self.last_message_at_ms,
            "last_ping_at_ms": self.last_ping_at_ms,
            "last_pong_at_ms": self.last_pong_at_ms,
            "last_error_category": self.last_error_category,
            "last_error_code": self.last_error_code,
            "reconnect_count": self.reconnect_count,
            "desired_subscription_count": len(self._desired_args),
            "acked_subscription_count": len(self._subscribed_args),
            "data_frame_count": self.data_frame_count,
            "tick_count": self.tick_count,
        }

    async def ensure_connected(self) -> None:
        if self._websocket is not None:
            return
        async with self._lock:
            if self._websocket is not None:
                return
            self._raise_if_circuit_open()
            websocket: Any | None = None
            try:
                self._set_connection_state("connecting")
                websocket = await _await_bounded(
                    websockets.connect(
                        self.url,
                        ping_interval=None,
                        open_timeout=OKX_DEX_WS_CONNECT_TIMEOUT_SECONDS,
                        close_timeout=OKX_DEX_WS_CLOSE_TIMEOUT_SECONDS,
                        max_size=OKX_DEX_WS_MAX_FRAME_BYTES,
                        max_queue=OKX_DEX_WS_MAX_QUEUE,
                    ),
                    operation="connect",
                    timeout_seconds=OKX_DEX_WS_CONNECT_TIMEOUT_SECONDS,
                )
                self._set_connection_state("authenticating")
                await websocket.send(json.dumps(_login_payload(self.api_key, self.secret_key, self.passphrase)))
                await self._wait_for_login(websocket)
                self._websocket = websocket
                websocket = None
                self._set_connection_state("subscribed")
                self._recoverable_failure_count = 0
                self._circuit_opened_at_ms = None
                if self._desired_args:
                    await self._send_subscription_replace(
                        to_subscribe_keys=sorted(self._desired_args),
                        to_unsubscribe_keys=[],
                    )
            except asyncio.CancelledError:
                await _close_websocket(websocket)
                await self._drop_connection(state="disconnected")
                raise
            except OkxDexWsClientError:
                await _close_websocket(websocket)
                await self._drop_connection(state="failed_terminal")
                raise
            except (WebSocketException, OSError, TimeoutError) as exc:
                await _close_websocket(websocket)
                await self._drop_connection(state="degraded_recoverable")
                self._record_recoverable_error(
                    category="connect_timeout" if isinstance(exc, TimeoutError) else "transport"
                )
                raise

    async def replace_subscriptions(self, targets: list[dict[str, str]]) -> None:
        desired_args = _subscription_args(targets, limit=self.subscription_limit)
        desired_keys = {_arg_key(arg) for arg in desired_args}
        self._desired_args = set(desired_keys)
        await self.ensure_connected()
        websocket = self._websocket
        if websocket is None:
            raise OkxDexWsClientError("OKX DEX WS connection lost before replace_subscriptions")
        to_unsubscribe_keys = sorted(self._subscribed_args - desired_keys)
        to_subscribe_keys = sorted(desired_keys - self._subscribed_args)
        try:
            await self._send_subscription_replace(
                to_subscribe_keys=to_subscribe_keys,
                to_unsubscribe_keys=to_unsubscribe_keys,
            )
        except asyncio.CancelledError:
            raise
        except (WebSocketException, OSError, TimeoutError):
            await self._drop_connection(state="degraded_recoverable")
            self._record_recoverable_error(category="transport")
            raise
        self._subscribed_args = desired_keys

    async def iter_price_info(self) -> AsyncIterator[OkxDexPriceInfoUpdate]:
        while True:
            await self.ensure_connected()
            websocket = self._websocket
            if websocket is None:
                raise OkxDexWsClientError("OKX DEX WS connection lost before iter_price_info")
            try:
                raw_message = await self._recv_application_message(websocket)
                if raw_message is None:
                    continue
                message = _decode_application_json(raw_message)
                if isinstance(message, dict):
                    event = _text(message.get("event"))
                    if event == "notice":
                        await self._reconnect_after_recoverable_error(
                            "notice_reconnect",
                            code=_text(message.get("code")),
                        )
                        continue
                    if event == "error":
                        code = _text(message.get("code"))
                        if _is_auth_error_code(code):
                            self._record_error(category="auth_error", code=code)
                            await self._drop_connection(state="failed_terminal")
                            raise OkxDexWsClientError(_error_message(message))
                        await self._reconnect_after_recoverable_error("provider_error", code=code)
                        continue
                rows = _rows_from_message(message)
                if rows:
                    self.data_frame_count += 1
                for row in rows:
                    update = _price_info_update_from_row(row)
                    if update is not None:
                        self._set_connection_state("streaming")
                        self.tick_count += 1
                        yield update
            except asyncio.CancelledError:
                raise
            except OkxDexWsClientError:
                raise
            except _OkxDexWsReconnectFailed as exc:
                cause = exc.__cause__
                if cause is not None:
                    raise cause from exc
                raise
            except _OkxDexWsMissingPong:
                await self._reconnect_after_recoverable_error("missing_pong")
            except (WebSocketException, OSError, TimeoutError) as exc:
                await self._reconnect_after_recoverable_error(
                    "recv_timeout" if isinstance(exc, TimeoutError) else "transport"
                )

    async def aclose(self) -> None:
        await self._drop_connection(state="disconnected")

    async def _drop_connection(self, *, state: str) -> None:
        websocket = self._websocket
        self._websocket = None
        self._subscribed_args = set()
        await _close_websocket(websocket)
        self._set_connection_state(state)

    async def _wait_for_login(self, websocket: Any) -> None:
        while True:
            raw_message = await _await_bounded(
                websocket.recv(),
                operation="login",
                timeout_seconds=OKX_DEX_WS_LOGIN_TIMEOUT_SECONDS,
            )
            self._record_message_seen(raw_message)
            if _is_plain_pong(raw_message):
                self.last_pong_at_ms = self.last_message_at_ms
                continue
            message = _decode_application_json(raw_message)
            if not isinstance(message, dict):
                continue
            if message.get("event") == "error":
                code = _text(message.get("code"))
                self._record_error(category="auth_error" if _is_auth_error_code(code) else "login_error", code=code)
                raise OkxDexWsClientError(_error_message(message))
            if message.get("event") == "login":
                code = str(message.get("code") or "")
                if code and code != "0":
                    self._record_error(category="auth_error", code=code)
                    raise OkxDexWsClientError(_error_message(message))
                return

    async def _send_subscription_replace(
        self,
        *,
        to_subscribe_keys: list[tuple[str, str]],
        to_unsubscribe_keys: list[tuple[str, str]],
    ) -> None:
        websocket = self._websocket
        if websocket is None:
            raise OkxDexWsClientError("OKX DEX WS connection lost before subscription replace")
        if to_unsubscribe_keys:
            await websocket.send(
                json.dumps(
                    {
                        "op": "unsubscribe",
                        "args": [_arg_from_key(key) for key in to_unsubscribe_keys],
                    }
                )
            )
        if to_subscribe_keys:
            await websocket.send(
                json.dumps(
                    {
                        "op": "subscribe",
                        "args": [_arg_from_key(key) for key in to_subscribe_keys],
                    }
                )
            )
        self._subscribed_args = set(self._desired_args)

    async def _recv_application_message(self, websocket: Any) -> str | bytes | None:
        try:
            raw_message = await asyncio.wait_for(websocket.recv(), timeout=max(0.001, OKX_DEX_WS_IDLE_PING_SECONDS))
        except TimeoutError:
            await websocket.send("ping")
            self.last_ping_at_ms = _now_ms()
            try:
                raw_message = await asyncio.wait_for(
                    websocket.recv(),
                    timeout=max(0.001, OKX_DEX_WS_PONG_TIMEOUT_SECONDS),
                )
            except TimeoutError as exc:
                raise _OkxDexWsMissingPong("OKX DEX WS missing application pong") from exc
        self._record_message_seen(raw_message)
        if _is_plain_pong(raw_message):
            self.last_pong_at_ms = self.last_message_at_ms
            return None
        return raw_message

    async def _reconnect_after_recoverable_error(self, category: str, *, code: str | None = None) -> None:
        self._record_error(category=category, code=code)
        self._recoverable_failure_count += 1
        if self._recoverable_failure_count >= _circuit_failure_limit():
            self._circuit_opened_at_ms = _now_ms()
            await self._drop_connection(state="circuit_open")
            raise OkxDexWsClientError("OKX DEX WS circuit open")
        await self._drop_connection(state="degraded_recoverable")
        self.reconnect_count += 1
        try:
            await self.ensure_connected()
        except asyncio.CancelledError:
            raise
        except (WebSocketException, OSError, TimeoutError, OkxDexWsClientError) as exc:
            raise _OkxDexWsReconnectFailed("OKX DEX WS reconnect failed") from exc

    def _record_recoverable_error(self, *, category: str) -> None:
        self._record_error(category=category, code=None)
        self._recoverable_failure_count += 1
        if self._recoverable_failure_count >= _circuit_failure_limit():
            self._circuit_opened_at_ms = _now_ms()
            self._set_connection_state("circuit_open")

    def _raise_if_circuit_open(self) -> None:
        if self.connection_state != "circuit_open":
            return
        opened_at_ms = self._circuit_opened_at_ms
        if opened_at_ms is not None:
            age_seconds = max(0.0, (_now_ms() - opened_at_ms) / 1000)
            if age_seconds >= max(0.001, OKX_DEX_WS_CIRCUIT_COOLDOWN_SECONDS):
                self._recoverable_failure_count = 0
                self._circuit_opened_at_ms = None
                self._set_connection_state("disconnected")
                return
        raise OkxDexWsClientError("OKX DEX WS circuit open")

    def _record_message_seen(self, raw_message: Any) -> None:
        self.last_message_at_ms = _now_ms()

    def _record_error(self, *, category: str, code: str | None) -> None:
        self.last_error_category = category
        self.last_error_code = _text(code)

    def _set_connection_state(self, state: str) -> None:
        if state not in WS_CONNECTION_STATES:
            raise ValueError(f"unsupported OKX DEX WS state: {state}")
        if state == self.connection_state:
            return
        self.connection_state = state
        self.last_state_change_at_ms = _now_ms()
        logger.info(
            "OKX DEX WS connection state changed | state={} last_state_change_at_ms={}",
            self.connection_state,
            self.last_state_change_at_ms,
        )


def _subscription_args(targets: list[dict[str, str]], *, limit: int) -> list[dict[str, str]]:
    parsed_limit = require_positive_int(
        limit,
        error_code="okx_dex_ws_subscription_limit_required",
    )
    args: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for target in targets:
        chain_index = str(target.get("chainIndex") or target.get("chain_id") or "").strip()
        address = _normalize_address(target.get("tokenContractAddress") or target.get("address") or "")
        if not chain_index or not address:
            continue
        key = (chain_index, address)
        if key in seen:
            continue
        seen.add(key)
        args.append(
            {
                "channel": "price-info",
                "chainIndex": chain_index,
                "tokenContractAddress": address,
            }
        )
        if len(args) >= parsed_limit:
            break
    return args


def _circuit_failure_limit() -> int:
    return require_positive_int(
        OKX_DEX_WS_CIRCUIT_FAILURES,
        error_code="okx_dex_ws_circuit_failures_required",
    )


def _arg_key(arg: dict[str, str]) -> tuple[str, str]:
    return (str(arg.get("chainIndex") or ""), str(arg.get("tokenContractAddress") or ""))


def _arg_from_key(key: tuple[str, str]) -> dict[str, str]:
    return {
        "channel": "price-info",
        "chainIndex": key[0],
        "tokenContractAddress": key[1],
    }


async def _close_websocket(websocket: Any | None) -> None:
    if websocket is None:
        return
    close_task = asyncio.create_task(websocket.close())
    try:
        await asyncio.wait_for(asyncio.shield(close_task), timeout=OKX_DEX_WS_CLOSE_TIMEOUT_SECONDS)
    except TimeoutError:
        close_task.cancel()
        logger.warning("OKX DEX WS close timed out | timeout_seconds={}", OKX_DEX_WS_CLOSE_TIMEOUT_SECONDS)
    except (WebSocketException, OSError) as exc:
        close_task.cancel()
        logger.warning("OKX DEX WS close raised | error={}", exc)


async def _await_bounded(awaitable: Awaitable[Any], *, operation: str, timeout_seconds: float) -> Any:
    task = asyncio.ensure_future(awaitable)
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=max(0.001, float(timeout_seconds)))
    except TimeoutError as exc:
        _cancel_background_task(task, operation=operation)
        raise TimeoutError(f"OKX DEX WS {operation} timed out after {timeout_seconds:g}s") from exc
    except asyncio.CancelledError:
        _cancel_background_task(task, operation=operation)
        raise
    except Exception:
        if not task.done():
            _cancel_background_task(task, operation=operation)
        raise


def _cancel_background_task(task: asyncio.Future[Any], *, operation: str) -> None:
    if not task.done():
        task.cancel()
    task.add_done_callback(lambda done: _log_background_task_completion(done, operation=operation))


def _log_background_task_completion(task: asyncio.Future[Any], *, operation: str) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        return
    except Exception as exc:
        logger.warning("OKX DEX WS background task failed after cancel | operation={} error={}", operation, exc)


def _login_payload(api_key: str, secret_key: str, passphrase: str) -> dict[str, Any]:
    timestamp = _okx_timestamp()
    return {
        "op": "login",
        "args": [
            {
                "apiKey": api_key,
                "passphrase": passphrase,
                "timestamp": timestamp,
                "sign": _login_signature(secret_key=secret_key, timestamp=timestamp),
            }
        ],
    }


def _rows_from_message(message: Any) -> list[dict[str, Any]]:
    if not isinstance(message, dict):
        return []
    arg = message.get("arg")
    context = arg if isinstance(arg, dict) else {}
    data = message.get("data")
    if isinstance(data, list):
        if len(data) > OKX_DEX_WS_MAX_ITEMS_PER_FRAME:
            raise OkxDexWsClientError("OKX DEX WS frame item limit exceeded")
        return [_with_message_context(row, context) for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        return [_with_message_context(data, context)]
    return [message]


def _decode_application_json(raw_message: Any) -> Any:
    if isinstance(raw_message, bytes):
        raw_bytes = raw_message
        try:
            text = raw_message.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise OkxDexWsClientError("OKX DEX WS frame is not UTF-8 JSON") from exc
    else:
        text = str(raw_message)
        raw_bytes = text.encode("utf-8")
    if len(raw_bytes) > OKX_DEX_WS_MAX_FRAME_BYTES:
        raise OkxDexWsClientError("OKX DEX WS frame byte limit exceeded")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise OkxDexWsClientError("OKX DEX WS malformed JSON frame") from exc


def _with_message_context(row: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    if not context:
        return row
    return {**context, **row}


def _price_info_update_from_row(row: dict[str, Any]) -> OkxDexPriceInfoUpdate | None:
    if row.get("event") == "error":
        raise OkxDexWsClientError(_error_message(row))
    chain_index = _text(row.get("chainIndex") or row.get("chain_index"))
    address = _text(row.get("tokenContractAddress") or row.get("tokenAddress") or row.get("address"))
    observed_at_ms = _int(row.get("time") or row.get("timestamp") or row.get("ts") or row.get("observedAtMs"))
    if not chain_index or not address or observed_at_ms is None:
        return None
    return OkxDexPriceInfoUpdate(
        chain_id=chain_index,
        address=_normalize_address(address),
        observed_at_ms=observed_at_ms,
        price_usd=_float(row.get("price") or row.get("priceUsd") or row.get("priceUSD")),
        market_cap_usd=_float(row.get("marketCap") or row.get("marketCapUsd") or row.get("marketCapUSD")),
        liquidity_usd=_float(row.get("liquidity") or row.get("liquidityUsd") or row.get("liquidityUSD")),
        volume_24h_usd=_float(row.get("volume24H") or row.get("volume24h") or row.get("volume24hUsd")),
        open_interest_usd=_float(row.get("openInterestUsd") or row.get("openInterestUSD")),
        holders=_int(row.get("holders") or row.get("holderCount")),
        raw=dict(row),
    )


def _login_prehash(timestamp: str) -> str:
    return f"{timestamp}GET/users/self/verify"


def _login_signature(*, secret_key: str, timestamp: str) -> str:
    digest = hmac.new(
        secret_key.encode("utf-8"),
        _login_prehash(timestamp).encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


def _okx_timestamp() -> str:
    return str(int(time.time()))


def _now_ms() -> int:
    return int(time.time() * 1000)


def _is_plain_pong(raw_message: Any) -> bool:
    if isinstance(raw_message, bytes):
        try:
            raw_message = raw_message.decode("utf-8")
        except UnicodeDecodeError:
            return False
    return str(raw_message).strip().lower() == "pong"


def _is_auth_error_code(code: str | None) -> bool:
    return _text(code) in _AUTH_ERROR_CODES


def _error_message(row: dict[str, Any]) -> str:
    code = _text(row.get("code")) or "unknown"
    message = _text(row.get("msg") or row.get("message")) or "OKX DEX websocket error"
    return f"{code}: {message}"


def _normalize_address(address: Any) -> str:
    text = str(address or "").strip()
    return text.lower() if EVM_ADDRESS_RE.match(text) else text


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None
