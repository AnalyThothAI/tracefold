from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, suppress
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

from tracefold.market import EVM_QUERY_CHAINS, normalize_ca

DEFAULT_SEND_TIMEOUT_SECONDS = 0.25
MAX_REPLAY_LIMIT = 100
MAX_SUBSCRIPTION_FILTER_VALUES = 50
SUBSCRIBE_MESSAGE_KEYS = frozenset({"type", "cas", "symbols", "market_targets", "replay", "after_cursor"})
_BROADCAST_POLL_INTERVAL_SECONDS = 0.250
_BROADCAST_BATCH_SIZE = 500
_BROADCAST_CACHE_SIZE = 2_000


@dataclass(eq=False)
class ClientSubscription:
    """Client filters for replay events and material live market target updates."""

    websocket: WebSocket
    cas: set[tuple[str, str]] = field(default_factory=set)
    symbols: set[str] = field(default_factory=set)
    market_targets: set[tuple[str, str]] = field(default_factory=set)
    live_after_cursor: int = 0


class PersistedLiveBroadcaster:
    """One read-only PostgreSQL cursor reader with in-memory client fanout."""

    def __init__(
        self,
        *,
        token: str,
        repository_session: Callable[[], AbstractContextManager[Any]],
        default_replay_limit: int = 100,
        send_timeout_seconds: float = DEFAULT_SEND_TIMEOUT_SECONDS,
    ):
        self.token = token
        self.repository_session = repository_session
        self.default_replay_limit = default_replay_limit
        self.send_timeout_seconds = max(0.001, float(send_timeout_seconds))
        self._clients: set[ClientSubscription] = set()
        self._records: list[dict[str, Any]] = []
        self._cursor = 0
        self._stop_event = asyncio.Event()
        self._subscriber_event = asyncio.Event()
        self._state_lock = asyncio.Lock()
        self._poll_task: asyncio.Task[None] | None = None
        self._db_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tracefold-live-broadcaster-db")

    async def start(self) -> None:
        if self._poll_task is not None:
            raise RuntimeError("persisted_live_broadcaster_already_started")
        self._stop_event.clear()
        self._poll_task = asyncio.create_task(self._poll_loop(), name="serve:persisted_live_broadcaster")

    async def aclose(self) -> None:
        self._stop_event.set()
        self._subscriber_event.set()
        if self._poll_task is not None:
            await self._poll_task
            self._poll_task = None
        self._db_executor.shutdown(wait=True, cancel_futures=True)

    async def publish(self, payload: dict[str, Any]) -> None:
        if not self._clients:
            return

        message = _json_message(payload)
        async with self._state_lock:
            send_targets = [
                client
                for client in list(self._clients)
                if _is_new_for_client(payload, client) and self._payload_matches_subscription(payload, client)
            ]
        results = await asyncio.gather(
            *(self._send_with_timeout(client, message) for client in send_targets),
            return_exceptions=True,
        )
        stale_clients = [
            client
            for client, result in zip(send_targets, results, strict=True)
            if isinstance(result, BaseException) or result is False
        ]

        for client in stale_clients:
            self._clients.discard(client)

    async def _send_with_timeout(self, client: ClientSubscription, message: str) -> bool:
        try:
            await asyncio.wait_for(client.websocket.send_text(message), timeout=self.send_timeout_seconds)
            return True
        except (TimeoutError, WebSocketDisconnect, RuntimeError):
            return False

    async def handle(self, websocket: WebSocket) -> None:
        await websocket.accept()
        client = ClientSubscription(websocket=websocket)
        try:
            await self._authenticate(websocket)
            self._clients.add(client)
            self._subscriber_event.set()
            await websocket.send_text(_json_message({"type": "ready"}))
            while True:
                raw_message = await websocket.receive_text()
                await self._handle_client_message(client, raw_message)
        except WebSocketDisconnect:
            pass
        finally:
            self._clients.discard(client)
            if not self._clients:
                self._subscriber_event.clear()

    async def _authenticate(self, websocket: WebSocket) -> None:
        try:
            raw_message = await asyncio.wait_for(websocket.receive_text(), timeout=10)
            message = json.loads(raw_message)
        except (TimeoutError, json.JSONDecodeError) as exc:
            await _close_if_connected(websocket, code=1008, reason="authentication required")
            raise WebSocketDisconnect(code=1008) from exc

        if (
            not isinstance(message, dict)
            or set(message) != {"type", "token"}
            or message.get("type") != "auth"
            or not isinstance(message.get("token"), str)
            or message["token"] != self.token
        ):
            await _close_if_connected(websocket, code=1008, reason="authentication failed")
            raise WebSocketDisconnect(code=1008)

    async def _handle_client_message(self, client: ClientSubscription, raw_message: str) -> None:
        try:
            message = json.loads(raw_message)
        except json.JSONDecodeError:
            await client.websocket.send_text(_json_message({"type": "error", "code": "invalid_json"}))
            return

        if not isinstance(message, dict) or message.get("type") != "subscribe":
            await client.websocket.send_text(_json_message({"type": "error", "code": "unsupported_message"}))
            return
        if set(message) - SUBSCRIBE_MESSAGE_KEYS:
            await client.websocket.send_text(_json_message({"type": "error", "code": "invalid_subscription"}))
            return

        try:
            cas = _normalize_cas(_list_value(message.get("cas", []), field="cas"))
            symbols = _normalize_symbols(_string_list(message.get("symbols", []), field="symbols"))
            market_targets = _normalize_market_targets(
                _list_value(message.get("market_targets", []), field="market_targets")
            )
            replay_limit = _replay_limit(message.get("replay"), self.default_replay_limit)
            after_cursor = _after_cursor(message.get("after_cursor"))
        except ValueError:
            await client.websocket.send_text(_json_message({"type": "error", "code": "invalid_subscription"}))
            return
        if _subscription_filter_count(cas=cas, symbols=symbols, market_targets=market_targets) > (
            MAX_SUBSCRIPTION_FILTER_VALUES
        ):
            await client.websocket.send_text(
                _json_message(
                    {
                        "type": "error",
                        "code": "too_many_filters",
                        "limit": MAX_SUBSCRIPTION_FILTER_VALUES,
                    }
                )
            )
            return

        async with self._state_lock:
            client.cas = cas
            client.symbols = symbols
            client.market_targets = market_targets
            replay_events = await self._replay_events(client, replay_limit, after_cursor=after_cursor)
        for payload in replay_events:
            await client.websocket.send_text(_json_message(payload))

    async def _replay_events(
        self,
        client: ClientSubscription,
        limit: int,
        *,
        after_cursor: int | None,
    ) -> list[dict[str, Any]]:
        if limit <= 0 or not (client.cas or client.symbols or client.market_targets):
            client.live_after_cursor = self._cursor
            return []
        loop = asyncio.get_running_loop()
        rows = await loop.run_in_executor(
            self._db_executor,
            partial(
                self._read_replay,
                after_cursor=after_cursor,
                limit=MAX_REPLAY_LIMIT,
            ),
        )
        self._extend_records([row for row in rows if int(row["cursor"]) > self._cursor])
        client.live_after_cursor = max(
            (int(row["cursor"]) for row in rows),
            default=self._cursor,
        )
        matching = [
            _payload_from_row(record)
            for record in rows
            if self._payload_matches_subscription(_payload_from_row(record), client)
        ]
        return matching[-limit:]

    async def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            if not self._clients:
                await self._subscriber_event.wait()
                if self._stop_event.is_set():
                    break
                continue
            loop = asyncio.get_running_loop()
            rows = await loop.run_in_executor(
                self._db_executor,
                partial(self._read_after, cursor=self._cursor, limit=_BROADCAST_BATCH_SIZE),
            )
            records = self._extend_records(rows)
            for record in records:
                await self.publish(record["payload"])
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=_BROADCAST_POLL_INTERVAL_SECONDS,
                )

    def _read_replay(
        self,
        *,
        after_cursor: int | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        with self.repository_session() as repos:
            if after_cursor is None:
                return repos.persisted_live.latest(limit=limit)
            return repos.persisted_live.after_cursor(cursor=after_cursor, limit=limit)

    def _read_after(self, *, cursor: int, limit: int) -> list[dict[str, Any]]:
        with self.repository_session() as repos:
            return repos.persisted_live.after_cursor(cursor=cursor, limit=limit)

    def _extend_records(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        known_cursors = {int(record["cursor"]) for record in self._records}
        for row in rows:
            cursor = int(row["cursor"])
            if cursor in known_cursors:
                continue
            payload = dict(row["payload_json"])
            payload["cursor"] = cursor
            record = {"cursor": cursor, "payload": payload}
            records.append(record)
            known_cursors.add(cursor)
            self._cursor = max(self._cursor, cursor)
        if records:
            self._records.extend(records)
            del self._records[:-_BROADCAST_CACHE_SIZE]
        return records

    def _payload_matches_subscription(self, payload: dict[str, Any], client: ClientSubscription) -> bool:
        if payload.get("type") == "live_market_update":
            target = _market_target(payload)
            return bool(target and target in client.market_targets)
        if payload.get("type") != "event":
            return False
        has_token_filters = bool(client.cas or client.symbols)
        if not has_token_filters:
            return False
        for entity in payload.get("entities") or []:
            ca_key = (entity.get("chain"), entity.get("normalized_value"))
            if entity.get("entity_type") == "ca" and _ca_subscription_matches(ca_key, client.cas):
                return True
            symbol = str(entity.get("normalized_value") or "").upper()
            if entity.get("entity_type") == "symbol" and symbol in client.symbols:
                return True
        for intent in payload.get("token_intents") or []:
            symbol = str(intent.get("display_symbol") or "").strip().upper()
            if symbol and symbol in client.symbols:
                return True
            chain = intent.get("chain_hint")
            address = intent.get("address_hint")
            if address and _ca_subscription_matches((chain, str(address).lower()), client.cas):
                return True
        for resolution in payload.get("token_resolutions") or []:
            symbol = str(resolution.get("symbol") or "").strip().upper()
            if symbol and symbol in client.symbols:
                return True
        return False


def _payload_from_row(row: dict[str, Any]) -> dict[str, Any]:
    payload = dict(row["payload_json"])
    payload["cursor"] = int(row["cursor"])
    return payload


def _is_new_for_client(payload: dict[str, Any], client: ClientSubscription) -> bool:
    cursor = payload.get("cursor")
    return cursor is None or int(cursor) > client.live_after_cursor


def _json_message(message: dict[str, Any]) -> str:
    return json.dumps(message, ensure_ascii=False, separators=(",", ":"))


def _replay_limit(value: Any, default: int) -> int:
    if value is None:
        return max(0, min(int(default), MAX_REPLAY_LIMIT))
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("invalid_replay")
    return max(0, min(value, MAX_REPLAY_LIMIT))


def _after_cursor(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("invalid_after_cursor")
    return value


def _subscription_filter_count(
    *,
    cas: set[tuple[str, str]],
    symbols: set[str],
    market_targets: set[tuple[str, str]],
) -> int:
    return len(cas) + len(symbols) + len(market_targets)


def _per_filter_replay_limit(*, total_limit: int, filter_count: int) -> int:
    if total_limit <= 0 or filter_count <= 0:
        return 0
    return max(1, (int(total_limit) + int(filter_count) - 1) // int(filter_count))


def _normalize_cas(values: list[Any]) -> set[tuple[str, str]]:
    normalized: set[tuple[str, str]] = set()
    for item in values:
        if not isinstance(item, dict) or set(item) - {"ca", "chain"}:
            raise ValueError("invalid_ca")
        value = item.get("ca")
        chain = item.get("chain")
        if not isinstance(value, str) or not value.strip():
            raise ValueError("invalid_ca")
        if chain is not None and (not isinstance(chain, str) or not chain.strip()):
            raise ValueError("invalid_ca")
        normalized.add(normalize_ca(value, chain=str(chain) if chain else None))
    return normalized


def _normalize_symbols(values: list[str]) -> set[str]:
    symbols: set[str] = set()
    for item in values:
        value = item.strip().lstrip("$").upper()
        if value and not value.startswith("0X"):
            symbols.add(value)
    return symbols


def _normalize_market_targets(values: list[Any]) -> set[tuple[str, str]]:
    targets: set[tuple[str, str]] = set()
    for item in values:
        if not isinstance(item, dict) or set(item) != {"target_type", "target_id"}:
            raise ValueError("invalid_market_target")
        target = _market_target(item)
        if target is None:
            raise ValueError("invalid_market_target")
        targets.add(target)
    return targets


def _market_target(payload: dict[str, Any]) -> tuple[str, str] | None:
    raw_target_type = payload.get("target_type")
    raw_target_id = payload.get("target_id")
    if not isinstance(raw_target_type, str) or not isinstance(raw_target_id, str):
        return None
    target_type = raw_target_type.strip()
    target_id = raw_target_id.strip()
    if not target_type or not target_id:
        return None
    return (target_type, target_id)


def _list_value(value: Any, *, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"invalid_{field}")
    return value


def _string_list(value: Any, *, field: str) -> list[str]:
    values = _list_value(value, field=field)
    if any(not isinstance(item, str) for item in values):
        raise ValueError(f"invalid_{field}")
    return values


def _ca_subscription_matches(ca_key: tuple[Any, Any], subscribed: set[tuple[str, str]]) -> bool:
    chain, address = ca_key
    try:
        normalized_chain, canonical_address = normalize_ca(
            str(address or ""),
            chain=str(chain) if chain else None,
        )
    except ValueError:
        normalized_chain = str(chain or "")
        canonical_address = str(address or "")
    normalized_address = canonical_address.casefold()
    normalized_subscribed = {
        _normalized_ca_pair(subscribed_chain, subscribed_address) for subscribed_chain, subscribed_address in subscribed
    }
    if any(
        normalized_chain == subscribed_chain and normalized_address == subscribed_address
        for subscribed_chain, subscribed_address in normalized_subscribed
    ):
        return True
    return any(
        subscribed_chain == "evm_unknown"
        and normalized_address == subscribed_address
        and normalized_chain in EVM_QUERY_CHAINS
        for subscribed_chain, subscribed_address in normalized_subscribed
    )


def _normalized_ca_pair(chain: Any, address: Any) -> tuple[str, str]:
    try:
        normalized_chain, normalized_address = normalize_ca(
            str(address or ""),
            chain=str(chain) if chain else None,
        )
    except ValueError:
        return str(chain or ""), str(address or "").casefold()
    return normalized_chain, normalized_address.casefold()


async def _close_if_connected(websocket: WebSocket, *, code: int, reason: str) -> None:
    if websocket.client_state != WebSocketState.DISCONNECTED:
        try:
            await websocket.close(code=code, reason=reason)
        except (AttributeError, RuntimeError):
            return
