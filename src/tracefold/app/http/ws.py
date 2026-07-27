from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

from tracefold.market import EVM_QUERY_CHAINS, EventRead, normalize_ca

DEFAULT_SEND_TIMEOUT_SECONDS = 0.25
MAX_REPLAY_LIMIT = 1000
MAX_SUBSCRIPTION_FILTER_VALUES = 50
SUBSCRIBE_MESSAGE_KEYS = frozenset({"type", "cas", "symbols", "market_targets", "replay"})


@dataclass(eq=False)
class ClientSubscription:
    """Client filters for replay events and material live market target updates."""

    websocket: WebSocket
    cas: set[tuple[str, str]] = field(default_factory=set)
    symbols: set[str] = field(default_factory=set)
    market_targets: set[tuple[str, str]] = field(default_factory=set)


class PublicWebSocketHub:
    """Publishes event/replay payloads and material live_market_update messages."""

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

    async def publish(self, payload: dict[str, Any]) -> None:
        if not self._clients:
            return

        message = _json_message(payload)
        send_targets = [client for client in list(self._clients) if self._payload_matches_subscription(payload, client)]
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
            await websocket.send_text(_json_message({"type": "ready"}))
            while True:
                raw_message = await websocket.receive_text()
                await self._handle_client_message(client, raw_message)
        except WebSocketDisconnect:
            pass
        finally:
            self._clients.discard(client)

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

        client.cas = cas
        client.symbols = symbols
        client.market_targets = market_targets
        replay_events = self._replay_events(client, replay_limit)
        for payload in reversed(replay_events):
            await client.websocket.send_text(_json_message(payload))

    def _replay_events(self, client: ClientSubscription, limit: int) -> list[dict[str, Any]]:
        if limit <= 0 or not (client.cas or client.symbols):
            return []
        collected: dict[str, dict[str, Any]] = {}
        with self.repository_session() as repos:
            per_filter_limit = _per_filter_replay_limit(
                total_limit=limit,
                filter_count=len(client.cas) + len(client.symbols),
            )
            for event in repos.evidence.recent_events_for_token_filters(
                limit=limit,
                per_filter_limit=per_filter_limit,
                cas=client.cas,
                symbols=client.symbols,
            ):
                collected[str(event["event_id"])] = event
            events = list(collected.values())
            events.sort(key=lambda item: item.get("received_at_ms") or 0, reverse=True)
            return self._payloads_for_events(repos, events[:limit])

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

    @staticmethod
    def _payloads_for_events(repos: Any, events: list[EventRead]) -> list[dict[str, Any]]:
        event_ids = tuple(str(event["event_id"]) for event in events)
        entities_by_event = repos.entities.entities_for_events(event_ids)
        intents_by_event = repos.token_intents.intents_for_events(event_ids)
        token_resolutions_by_event = repos.event_tokens.for_events(event_ids)
        return [
            {
                "type": "event",
                "event": event,
                "entities": entities_by_event.get(str(event["event_id"]), []),
                "token_intents": intents_by_event.get(str(event["event_id"]), []),
                "token_resolutions": token_resolutions_by_event.get(str(event["event_id"]), []),
            }
            for event in events
        ]


def _json_message(message: dict[str, Any]) -> str:
    return json.dumps(message, ensure_ascii=False, separators=(",", ":"))


def _replay_limit(value: Any, default: int) -> int:
    if value is None:
        return max(0, min(int(default), MAX_REPLAY_LIMIT))
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("invalid_replay")
    return max(0, min(value, MAX_REPLAY_LIMIT))


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
    if (chain, address) in subscribed:
        return True
    return any(
        subscribed_chain == "evm_unknown" and address == subscribed_address and chain in EVM_QUERY_CHAINS
        for subscribed_chain, subscribed_address in subscribed
    )


async def _close_if_connected(websocket: WebSocket, *, code: int, reason: str) -> None:
    if websocket.client_state != WebSocketState.DISCONNECTED:
        try:
            await websocket.close(code=code, reason=reason)
        except (AttributeError, RuntimeError):
            return
