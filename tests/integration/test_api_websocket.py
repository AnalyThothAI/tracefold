import asyncio
import json
from contextlib import contextmanager

from fastapi.testclient import TestClient

from tests.postgres_test_utils import postgres_settings_storage, prepare_postgres_database
from tests.runtime_settings import disabled_workers_settings
from tracefold.app.http.app import create_app
from tracefold.app.http.ws import ClientSubscription, PublicWebSocketHub
from tracefold.market import Author, Content, Source, TwitterEvent
from tracefold.platform.config.settings import Settings


def make_settings(tmp_path) -> Settings:
    prepare_postgres_database()
    settings = Settings(
        ws_token="secret",
        storage=postgres_settings_storage(),
        workers=disabled_workers_settings(),
    )
    settings.set_config_dir(tmp_path / "app-home")
    return settings


PEPE = "0x6982508145454ce325ddbe47a25d4ec3d2311933"


def make_event(event_id: str, handle: str, text: str | None = None) -> TwitterEvent:
    return TwitterEvent(
        event_id=event_id,
        source=Source(
            provider="gmgn",
            transport="direct_ws",
            coverage="public_stream",
            channel="twitter_monitor_basic",
        ),
        action="tweet",
        original_action=None,
        tweet_id=event_id,
        internal_id=event_id,
        timestamp=1,
        received_at_ms=1000,
        author=Author(handle=handle, name=handle, avatar=None, followers=None, tags=[]),
        content=Content(text=text or f"{handle} text", media=[]),
        reference=None,
        unfollow_target=None,
        avatar_change=None,
        bio_change=None,
        raw=None,
    )


def test_websocket_auth_subscribe_replay_and_live_filtering(tmp_path):
    app = create_app(settings=make_settings(tmp_path), start_collector=False)

    with TestClient(app) as client:
        client.app.state.service.ingest.ingest_event(make_event("event-1", "toly", text="$PEPE replay"))

        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "auth", "token": "secret"})
            assert ws.receive_json()["type"] == "ready"

            ws.send_json({"type": "subscribe", "symbols": ["PEPE"], "replay": 5})
            replay = ws.receive_json()
            assert replay["type"] == "event"
            assert replay["event"]["event_id"] == "event-1"
            assert "entities" in replay
            assert "alerts" not in replay
            assert "token_intents" in replay
            assert "token_resolutions" in replay
            assert "harness" not in replay
            replay_event_fields = set(replay["event"])

            ignored = _ingest_payload(client, make_event("event-2", "elonmusk", text="no token"))
            matched = _ingest_payload(client, make_event("event-3", "toly", text="$PEPE live"))
            client.portal.call(client.app.state.service.hub.publish, ignored)
            client.portal.call(client.app.state.service.hub.publish, matched)
            live = ws.receive_json()
            assert live["event"]["event_id"] == "event-3"
            assert set(live["event"]) == replay_event_fields


def test_websocket_can_subscribe_by_ca_for_replay_and_live_events(tmp_path):
    app = create_app(settings=make_settings(tmp_path), start_collector=False)

    with TestClient(app) as client:
        replay_event = make_event("event-ca-replay", "toly", text=f"$PEPE replay {PEPE}")
        client.app.state.service.ingest.ingest_event(replay_event)

        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "auth", "token": "secret"})
            assert ws.receive_json()["type"] == "ready"

            ws.send_json({"type": "subscribe", "cas": [{"ca": PEPE}], "replay": 5})
            replay = ws.receive_json()
            assert replay["type"] == "event"
            assert replay["event"]["event_id"] == "event-ca-replay"
            assert replay["entities"][0]["entity_type"] in {"symbol", "ca"}

            ignored = make_event("event-ignore", "toly", text="no token")
            matched = make_event("event-ca-live", "elonmusk", text=f"$PEPE live {PEPE}")
            ignored_payload = _ingest_payload(client, ignored)
            matched_payload = _ingest_payload(client, matched)
            client.portal.call(client.app.state.service.hub.publish, ignored_payload)
            client.portal.call(client.app.state.service.hub.publish, matched_payload)
            live = ws.receive_json()
            assert live["event"]["event_id"] == "event-ca-live"


def test_websocket_rejects_retired_subscription_aliases_and_malformed_shapes():
    invalid_messages = [
        {"type": "subscribe", "ca": [{"ca": PEPE}], "replay": 0},
        {"type": "subscribe", "tokens": ["PEPE"], "replay": 0},
        {"type": "subscribe", "cas": [PEPE], "replay": 0},
        {"type": "subscribe", "cas": [{"address": PEPE}], "replay": 0},
        {"type": "subscribe", "symbols": "PEPE", "replay": 0},
        {
            "type": "subscribe",
            "market_targets": [{"target_type": "Asset", "target_id": "asset:one", "legacy": True}],
            "replay": 0,
        },
        {"type": "subscribe", "replay": "5"},
        {"type": "subscribe", "handles": ["toly"], "replay": 0},
        {"type": "subscribe", "notifications": 1, "replay": 0},
    ]

    for message in invalid_messages:
        socket = _DummyWebSocket()
        client = ClientSubscription(websocket=socket)
        hub = PublicWebSocketHub(token="secret", repository_session=_empty_repository_session)

        asyncio.run(hub._handle_client_message(client, json.dumps(message)))

        assert json.loads(socket.messages[-1]) == {"type": "error", "code": "invalid_subscription"}
        assert client.cas == set()
        assert client.symbols == set()
        assert client.market_targets == set()


def test_websocket_repeated_subscribe_replaces_market_targets():
    hub = PublicWebSocketHub(token="secret", repository_session=_empty_repository_session)
    client = ClientSubscription(websocket=_DummyWebSocket())

    asyncio.run(
        hub._handle_client_message(
            client,
            json.dumps(
                {
                    "type": "subscribe",
                    "market_targets": [
                        {
                            "target_type": "Asset",
                            "target_id": "asset:solana:token:one",
                        }
                    ],
                    "replay": 0,
                },
            ),
        ),
    )
    assert client.market_targets == {("Asset", "asset:solana:token:one")}

    asyncio.run(
        hub._handle_client_message(
            client,
            json.dumps(
                {
                    "type": "subscribe",
                    "market_targets": [
                        {
                            "target_type": "CexToken",
                            "target_id": "cex-token:binance:two",
                        }
                    ],
                    "replay": 0,
                },
            ),
        ),
    )
    assert client.market_targets == {("CexToken", "cex-token:binance:two")}


def test_websocket_publish_is_bounded_when_a_client_send_hangs():
    async def scenario() -> None:
        hub = PublicWebSocketHub(
            token="secret",
            repository_session=_empty_repository_session,
            send_timeout_seconds=0.01,
        )
        market_target = ("Asset", "asset:solana:token:one")
        slow = ClientSubscription(websocket=_HangingWebSocket(), market_targets={market_target})
        fast_socket = _DummyWebSocket()
        fast = ClientSubscription(websocket=fast_socket, market_targets={market_target})
        hub._clients.add(slow)
        hub._clients.add(fast)

        await hub.publish(
            {
                "type": "live_market_update",
                "target_type": market_target[0],
                "target_id": market_target[1],
            }
        )

        assert len(fast_socket.messages) == 1
        assert fast in hub._clients
        assert slow not in hub._clients

    asyncio.run(asyncio.wait_for(scenario(), timeout=0.1))


def test_websocket_empty_event_filter_replays_and_broadcasts_no_events():
    def unexpected_repository_session():
        raise AssertionError("empty event filters must not query replay evidence")

    hub = PublicWebSocketHub(token="secret", repository_session=unexpected_repository_session)
    client = ClientSubscription(
        websocket=None,
        market_targets={("Asset", "asset:solana:token:one")},
    )
    payload = {
        "type": "event",
        "event": {"event_id": "event-1", "author_handle": "alice"},
        "entities": [],
        "token_intents": [],
        "token_resolutions": [],
    }

    assert hub._replay_events(client, 10) == []
    assert hub._payload_matches_subscription(payload, client) is False


def test_websocket_symbol_filter_matches_token_intents_without_entities():
    hub = PublicWebSocketHub(token="secret", repository_session=lambda: None)
    client = ClientSubscription(websocket=None, symbols={"MIRROR"})
    payload = {
        "type": "event",
        "event": {"event_id": "event-1", "author_handle": "alice"},
        "entities": [],
        "token_intents": [
            {
                "intent_id": "intent:mirror",
                "display_symbol": "MIRROR",
                "chain_hint": "solana",
                "address_hint": "Mirror111111111111111111111111111111111111",
            }
        ],
    }

    assert hub._payload_matches_subscription(payload, client) is True


def test_websocket_symbol_filter_matches_projected_token_resolution_symbol_not_target_id():
    hub = PublicWebSocketHub(token="secret", repository_session=lambda: None)
    client = ClientSubscription(websocket=None, symbols={"MIRROR"})
    payload = {
        "type": "event",
        "event": {"author_handle": "random"},
        "entities": [],
        "token_intents": [],
        "token_resolutions": [
            {
                "target_type": "Asset",
                "target_id": "asset:eip155:1:erc20:0xfeedface",
                "symbol": "MIRROR",
            }
        ],
    }

    assert hub._payload_matches_subscription(payload, client) is True


def test_websocket_symbol_filter_does_not_match_target_id_substrings():
    hub = PublicWebSocketHub(token="secret", repository_session=lambda: None)
    client = ClientSubscription(websocket=None, symbols={"SET"})
    payload = {
        "type": "event",
        "event": {"author_handle": "random"},
        "entities": [],
        "token_intents": [],
        "token_resolutions": [
            {
                "target_type": "Asset",
                "target_id": "asset:eip155:1:erc20:0xfeedface",
                "symbol": "VOICE",
            }
        ],
    }

    assert hub._payload_matches_subscription(payload, client) is False


def test_websocket_ca_filter_matches_token_intents_without_entities():
    hub = PublicWebSocketHub(token="secret", repository_session=lambda: None)
    client = ClientSubscription(
        websocket=None,
        cas={("ethereum", "0x6982508145454ce325ddbe47a25d4ec3d2311933")},
    )
    payload = {
        "type": "event",
        "event": {"event_id": "event-1", "author_handle": "alice"},
        "entities": [],
        "token_intents": [
            {
                "intent_id": "intent:pepe",
                "display_symbol": "PEPE",
                "chain_hint": "ethereum",
                "address_hint": "0x6982508145454ce325ddbe47a25d4ec3d2311933",
            }
        ],
    }

    assert hub._payload_matches_subscription(payload, client) is True


def test_websocket_routes_live_market_update_for_explicit_market_target_subscription():
    hub = PublicWebSocketHub(token="secret", repository_session=lambda: None)
    client = ClientSubscription(
        websocket=None,
        market_targets={("Asset", "asset:solana:token:5UUH9RTDiSpq6HKS6bp4NdU9PNJpXRXuiw6ShBTBhgH2")},
    )
    payload = {
        "type": "live_market_update",
        "target_type": "Asset",
        "target_id": "asset:solana:token:5UUH9RTDiSpq6HKS6bp4NdU9PNJpXRXuiw6ShBTBhgH2",
        "live_market": {"status": "live", "price_usd": 1.23},
        "provider": "okx",
        "observed_at_ms": 1_700_086_430_000,
    }

    assert hub._payload_matches_subscription(payload, client) is True


def test_websocket_does_not_broadcast_live_market_update_without_matching_subscription():
    hub = PublicWebSocketHub(token="secret", repository_session=lambda: None)
    client = ClientSubscription(
        websocket=None,
        market_targets={("Asset", "asset:solana:token:other")},
    )
    payload = {
        "type": "live_market_update",
        "target_type": "Asset",
        "target_id": "asset:solana:token:5UUH9RTDiSpq6HKS6bp4NdU9PNJpXRXuiw6ShBTBhgH2",
        "live_market": {"status": "live", "price_usd": 1.23},
    }

    assert hub._payload_matches_subscription(payload, client) is False


def test_websocket_ignores_legacy_market_update_even_when_subscribed():
    hub = PublicWebSocketHub(token="secret", repository_session=lambda: None)
    client = ClientSubscription(
        websocket=None,
        market_targets={("Asset", "asset:solana:token:5UUH9RTDiSpq6HKS6bp4NdU9PNJpXRXuiw6ShBTBhgH2")},
    )
    payload = {
        "type": "market_update",
        "target_type": "Asset",
        "target_id": "asset:solana:token:5UUH9RTDiSpq6HKS6bp4NdU9PNJpXRXuiw6ShBTBhgH2",
    }

    assert hub._payload_matches_subscription(payload, client) is False


def _ingest_payload(client, event: TwitterEvent) -> dict:
    result = client.app.state.service.ingest.ingest_event(event)
    with client.app.state.service.repositories() as repos:
        token_resolutions = repos.event_tokens.for_event(event.event_id)
    return {
        "type": "event",
        "event": result.event,
        "entities": result.entities,
        "token_intents": result.token_intents,
        "token_resolutions": token_resolutions,
        "harness": None,
    }


class _DummyWebSocket:
    def __init__(self):
        self.messages: list[str] = []

    async def send_text(self, message: str) -> None:
        self.messages.append(message)


class _HangingWebSocket:
    async def send_text(self, _message: str) -> None:
        await asyncio.sleep(60)


@contextmanager
def _empty_repository_session():
    class Evidence:
        def recent_events(self, *args, **kwargs):
            return []

    class Repositories:
        evidence = Evidence()

    yield Repositories()
