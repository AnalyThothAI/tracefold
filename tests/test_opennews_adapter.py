from __future__ import annotations

import asyncio

import httpx
import pytest
from pydantic import ValidationError
from websockets.exceptions import ConcurrencyError, ProtocolError

from tracefold.integrations.opennews import client as opennews_client
from tracefold.news import OPENNEWS_SOURCE_ID, OpenNewsExpectedError, OpenNewsHistoryError
from tracefold.news import consumers as news_consumers
from tracefold.news.opennews import (
    parse_opennews_message,
    parse_opennews_strategy_hits,
    parse_opennews_strategy_list,
)
from tracefold.platform.config.settings import NewsSettings


def test_opennews_source_id_is_stable() -> None:
    assert OPENNEWS_SOURCE_ID == "news-opennews"


def test_opennews_token_is_normalized_and_needs_no_strategy_configuration() -> None:
    """#126: a token is the whole News configuration. The account decides which Strategies push."""

    configured = NewsSettings(opennews_token="  secret  ")
    assert configured.opennews_token == "secret"
    assert NewsSettings(opennews_token="  ").opennews_token is None

    with pytest.raises(ValidationError):
        NewsSettings(opennews_token="secret", opennews_strategy_ids=["1018"])


def test_websocket_connect_sends_nothing_and_preserves_the_first_strategy_frame(monkeypatch) -> None:
    class _WebSocket:
        def __init__(self) -> None:
            self.send_calls: list[str] = []
            self.recv_calls = 0
            self.close_calls = 0

        async def send(self, payload: str) -> None:
            self.send_calls.append(payload)

        async def recv(self) -> str:
            self.recv_calls += 1
            if self.recv_calls > 1:
                raise AssertionError("unexpected second receive")
            return '{"jsonrpc":"2.0","method":"strategy.triggered","params":{"id":3568500,"strategy":{"id":1018}}}'

        async def close(self) -> None:
            self.close_calls += 1

    async def connect(*_args, **_kwargs):
        return websocket

    async def scenario():
        await client.connect()
        assert websocket.send_calls == []
        assert websocket.recv_calls == 0
        first = await client.receive()
        await client.close()
        return first

    client = opennews_client.OpenNewsWebSocketClient(token="test-token")
    websocket = _WebSocket()
    monkeypatch.setattr(opennews_client.websockets, "connect", connect)

    assert asyncio.run(scenario()) == {
        "jsonrpc": "2.0",
        "method": "strategy.triggered",
        "params": {"id": 3_568_500, "strategy": {"id": 1018}},
    }
    assert websocket.close_calls == 1
    assert client._websocket is None


def test_websocket_connect_classifies_transport_failure(monkeypatch) -> None:
    async def connect(*_args, **_kwargs):
        raise OSError("connection refused")

    client = opennews_client.OpenNewsWebSocketClient(token="test-token")
    monkeypatch.setattr(opennews_client.websockets, "connect", connect)

    with pytest.raises(OpenNewsExpectedError, match="opennews_connect_failed"):
        asyncio.run(client.connect())


def test_websocket_connect_does_not_hide_programming_errors(monkeypatch) -> None:
    async def connect(*_args, **_kwargs):
        raise AssertionError("programming bug")

    client = opennews_client.OpenNewsWebSocketClient(token="test-token")
    monkeypatch.setattr(opennews_client.websockets, "connect", connect)

    with pytest.raises(AssertionError, match="programming bug"):
        asyncio.run(client.connect())


def test_allowlisted_market_strategy_is_normalized_as_a_linkless_report() -> None:
    event = parse_opennews_message(
        {
            "jsonrpc": "2.0",
            "method": "strategy.triggered",
            "params": {
                "id": 3_568_500,
                "newsType": "strategy",
                "engineType": "market",
                "text": "BTC open interest increased 3.4% in 3 minutes",
                "link": "",
                "source": "binance",
                "description": '{"open_interest_change":{"value":3.4,"unit":"%"}}',
                "coins": [{"symbol": "BTC", "market_type": "cex"}],
                "ts": "2026-08-13T03:00:00Z",
                "strategy": {
                    "id": 1019,
                    "name": "OI Event Monitor",
                    "sourceType": "market",
                    "soundId": "alert-1",
                    "bgColor": "#FF6B35",
                    "metrics": {"open_interest_change": {"value": 3.4, "unit": "%"}},
                },
                "aiRating": {"score": 85},
            },
        },
    )

    assert event is not None
    assert event.provider_record_id == "3568500"
    assert event.observation_kind == "report"
    assert event.entry.title == "BTC open interest increased 3.4% in 3 minutes"
    assert event.entry.description == ""
    assert event.entry.link is None
    assert event.entry.reporting_origin == "binance"
    assert event.entry.published_at_ms == 1_786_590_000_000
    assert event.provider_metadata == {
        "score": 85,
        "source": "binance",
        "coins": [{"symbol": "BTC", "market_type": "cex"}],
        "strategies": [
            {
                "id": "1019",
                "name": "OI Event Monitor",
                "source_type": "market",
                "engine_type": "market",
            }
        ],
    }


def test_official_strategy_history_adapter_uses_exact_authenticated_endpoints() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/open/strategy_list":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": [
                        {"id": 1018, "name": "News Score >70", "enabled": True},
                        {"id": 1019, "name": "OI Event Monitor", "enabled": True},
                    ],
                    "page": 1,
                    "limit": 100,
                    "total": 2,
                },
            )
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": [
                    {
                        "id": 3_568_500,
                        "engineType": "market",
                        "text": "BTC open interest increased 3.4%",
                        "source": "binance",
                        "ts": "2026-08-13T03:00:00Z",
                        "strategy": {"id": 1019, "name": "OI Event Monitor"},
                    }
                ],
                "page": 2,
                "limit": 100,
                "total": 101,
            },
        )

    async def scenario() -> tuple[object, object]:
        client = opennews_client.OpenNewsStrategyHistoryClient(
            token="history-token",
            transport=httpx.MockTransport(handler),
        )
        try:
            strategy_list = await client.get_strategy_list(limit=100, page=1)
            strategy_hits = await client.get_strategy_hits(strategy_id="1019", limit=100, page=2)
            return strategy_list, strategy_hits
        finally:
            await client.close()

    strategy_list, strategy_hits = asyncio.run(scenario())

    assert parse_opennews_strategy_list(
        strategy_list,
    ) == (
        {"id": "1018", "name": "News Score >70", "enabled": True},
        {"id": "1019", "name": "OI Event Monitor", "enabled": True},
    )
    parsed_hits = parse_opennews_strategy_hits(
        strategy_hits,
    )
    assert [event.provider_record_id for event in parsed_hits.events] == ["3568500"]
    assert parsed_hits.has_more is False
    assert [request.url.path for request in requests] == [
        "/open/strategy_list",
        "/open/strategy_hits",
    ]
    assert dict(requests[1].url.params) == {
        "strategyId": "1019",
        "limit": "100",
        "page": "2",
    }
    assert all(request.headers["authorization"] == "Bearer history-token" for request in requests)


def test_official_strategy_history_adapter_classifies_unavailable_endpoint() -> None:
    async def scenario() -> None:
        client = opennews_client.OpenNewsStrategyHistoryClient(
            token="history-token",
            transport=httpx.MockTransport(lambda _request: httpx.Response(404)),
        )
        try:
            with pytest.raises(OpenNewsHistoryError, match=r"^opennews_history_unavailable$"):
                await client.get_strategy_list(limit=100, page=1)
        finally:
            await client.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("strategy_id", "engine_type"),
    [
        ("news-score", "NEWS"),
        ("storage-news", "MEME"),
        ("oi-monitor", "MARKET"),
        ("listing-monitor", "listing"),
    ],
)
def test_every_strategy_frame_is_admitted_with_its_provider_metadata(strategy_id: str, engine_type: str) -> None:
    event = parse_opennews_message(
        {
            "method": "strategy.triggered",
            "params": {
                "id": f"event-{strategy_id}",
                "engineType": engine_type,
                "text": f"Triggered {strategy_id}",
                "ts": 1_775_195_200_000,
                "strategy": {
                    "id": strategy_id,
                    "name": f"Strategy {strategy_id}",
                    "sourceType": engine_type,
                },
            },
        },
    )

    assert event is not None
    assert event.provider_metadata["strategies"] == [
        {
            "id": strategy_id,
            "name": f"Strategy {strategy_id}",
            "source_type": engine_type.lower(),
            "engine_type": engine_type.lower(),
        }
    ]


def test_only_the_strategy_triggered_method_is_admitted() -> None:
    base_params = {
        "id": "provider-event-1",
        "engineType": "news",
        "text": "Provider report",
        "ts": 1_775_195_200_000,
        "strategy": {"id": "configured"},
    }

    for method in ("news.update", "news.ai_update"):
        assert (
            parse_opennews_message(
                {"method": method, "params": base_params},
            )
            is None
        )
    # #126: there is no local allowlist. A Strategy Tracefold has never heard of is admitted, because the
    # provider account decided to enable it and the socket pushed it.
    unknown = parse_opennews_message(
        {
            "method": "strategy.triggered",
            "params": {**base_params, "strategy": {"id": "never-seen-before"}},
        },
    )
    assert unknown is not None
    assert unknown.provider_metadata["strategies"][0]["id"] == "never-seen-before"


def test_same_provider_event_keeps_each_strategy_as_mergeable_provenance_input() -> None:
    events = [
        parse_opennews_message(
            {
                "method": "strategy.triggered",
                "params": {
                    "id": 3_568_500,
                    "engineType": engine_type,
                    "text": "The same underlying provider event",
                    "ts": 1_775_195_200_000,
                    "strategy": {
                        "id": strategy_id,
                        "name": strategy_name,
                        "sourceType": engine_type,
                    },
                },
            },
        )
        for strategy_id, strategy_name, engine_type in (
            (1019, "OI Event Monitor", "market"),
            (1018, "News Score >70", "news"),
        )
    ]

    assert all(event is not None for event in events)
    assert [event.provider_record_id for event in events if event is not None] == ["3568500", "3568500"]
    assert [event.provider_metadata["strategies"] for event in events if event is not None] == [
        [
            {
                "id": "1019",
                "name": "OI Event Monitor",
                "source_type": "market",
                "engine_type": "market",
            }
        ],
        [
            {
                "id": "1018",
                "name": "News Score >70",
                "source_type": "news",
                "engine_type": "news",
            }
        ],
    ]


def test_websocket_receive_classifies_protocol_disconnect() -> None:
    class _WebSocket:
        async def recv(self):
            raise ProtocolError("invalid provider frame")

    client = opennews_client.OpenNewsWebSocketClient(token="test-token")
    client._websocket = _WebSocket()

    with pytest.raises(OpenNewsExpectedError, match="opennews_protocol_error"):
        asyncio.run(client.receive())


def test_websocket_receive_classifies_invalid_provider_frames() -> None:
    class _WebSocket:
        def __init__(self, frame: object) -> None:
            self.frame = frame

        async def recv(self):
            return self.frame

    for frame in (b"\xff\xfe", "[" * 10_000 + "]" * 10_000):
        client = opennews_client.OpenNewsWebSocketClient(token="test-token")
        client._websocket = _WebSocket(frame)
        assert asyncio.run(client.receive()) == {}


def test_websocket_receive_does_not_hide_concurrent_use_errors() -> None:
    class _WebSocket:
        async def recv(self):
            raise ConcurrencyError("recv is already running")

    client = opennews_client.OpenNewsWebSocketClient(token="test-token")
    client._websocket = _WebSocket()

    with pytest.raises(ConcurrencyError, match="already running"):
        asyncio.run(client.receive())


def test_websocket_close_does_not_hide_concurrent_use_errors() -> None:
    class _WebSocket:
        async def close(self) -> None:
            raise ConcurrencyError("close overlaps another operation")

    client = opennews_client.OpenNewsWebSocketClient(token="test-token")
    client._websocket = _WebSocket()

    with pytest.raises(ConcurrencyError, match="overlaps"):
        asyncio.run(client.close())


def test_provider_ping_is_answered_without_entering_the_parser() -> None:
    class _WebSocket:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def recv(self):
            return "ping"

        async def send(self, payload: str) -> None:
            self.sent.append(payload)

    websocket = _WebSocket()
    client = opennews_client.OpenNewsWebSocketClient(token="test-token")
    client._websocket = websocket

    assert asyncio.run(client.receive()) == "ping"
    assert websocket.sent == ["pong"]


def test_strategy_normalization_keeps_only_bounded_provider_metadata() -> None:
    event = parse_opennews_message(
        {
            "method": "strategy.triggered",
            "params": {
                "id": "wire-1",
                "text": "Fed holds rates steady",
                "engineType": "NEWS",
                "link": "HTTPS://Example.COM/article/1/?utm_source=x&b=2&a=1#fragment",
                "ts": "2026-08-01T05:00:00Z",
                "received_at_ms": 123,
                "token": "must-not-survive",
                "source": "jin10",
                "aiRating": {"score": 99, "signal": "long", "grade": "A"},
                "coins": [
                    {
                        "symbol": "BTC",
                        "market_type": "spot",
                        "match": "Bitcoin",
                        "private": "must-not-survive",
                    }
                ],
                "strategy": {
                    "id": "strategy-private",
                    "name": "Storage News",
                    "sourceType": "NEWS",
                    "soundId": "must-not-survive",
                    "bgColor": "must-not-survive",
                    "metrics": {"must": "not-survive"},
                },
            },
        },
    )

    assert event is not None
    assert event.entry.link == "https://example.com/article/1?a=1&b=2"
    assert event.entry.reporting_origin == "jin10"
    assert event.entry.published_at_ms == 1_785_560_400_000
    assert event.provider_metadata == {
        "score": 99,
        "source": "jin10",
        "signal": "long",
        "grade": "A",
        "coins": [{"symbol": "BTC", "market_type": "spot", "match": "Bitcoin"}],
        "strategies": [
            {
                "id": "strategy-private",
                "name": "Storage News",
                "source_type": "news",
                "engine_type": "news",
            }
        ],
    }


def test_malformed_article_url_keeps_strategy_report_linkless() -> None:
    event = _strategy_event(link="https://[broken")

    assert event is not None
    assert event.entry.link is None


@pytest.mark.parametrize(
    ("invalid_text", "expected_title"),
    [("bad\x00text", "bad text"), ("bad\ud800text", None)],
)
def test_wire_text_strips_controls_and_rejects_non_utf8(
    invalid_text: str,
    expected_title: str | None,
) -> None:
    event = _strategy_event(
        text=invalid_text,
        source=invalid_text,
        signal=invalid_text,
        grade=invalid_text,
        coins=[{"symbol": invalid_text, "market_type": "spot"}],
        score=75,
    )

    assert event is not None
    assert event.entry.title == expected_title
    assert event.entry.description == ""
    assert event.entry.link is None
    assert event.provider_metadata == {
        "score": 75,
        "strategies": [{"id": "strategy-test", "name": "Test Strategy", "engine_type": "news"}],
    }


def test_headline_clamp_uses_javascript_utf16_units_and_valid_utf8() -> None:
    event = _strategy_event(text="a" * 499 + "𝔸" + "z")

    assert event is not None
    assert event.entry.title == "a" * 499 + "\ufffd"
    assert len(event.entry.title.encode("utf-16-le")) // 2 == 500


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("😀" * 20, "😀" * 20),
        ("😀" * 250, "😀" * 200),
        ("a" * 399 + "𝔸", "a" * 399 + "\ufffd"),
    ],
)
def test_multiline_strategy_text_keeps_a_bounded_description(description: str, expected: str) -> None:
    event = _strategy_event(
        text=f"Canonical headline differs from the description evidence\n{description}",
        description='{"provider_control":"must-not-survive"}',
    )

    assert event is not None
    assert event.entry.description == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("\ufeff1234567890", "1234567890"),
        ("Alpha\ufeffBeta announces a public policy update", "Alpha Beta announces a public policy update"),
    ],
)
def test_plaintext_blocks_use_javascript_whitespace(text: str, expected: str) -> None:
    event = _strategy_event(text=text, source="\ufeffReuters\ufeff", engineType="\ufeffNEWS\ufeff")

    assert event is not None
    assert event.entry.title == expected
    assert event.entry.reporting_origin == "reuters"
    assert event.provider_metadata["strategies"][0]["engine_type"] == "news"


@pytest.mark.parametrize("timestamp", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_timestamp_becomes_missing_date(timestamp: float) -> None:
    event = _strategy_event(ts=timestamp)

    assert event is not None
    assert event.entry.published_at_ms is None


@pytest.mark.parametrize("score", [float("nan"), float("inf"), float("-inf"), -1, 101])
def test_non_finite_or_out_of_range_scores_are_discarded(score: float) -> None:
    event = _strategy_event(
        score=score,
        coins=[{"symbol": "BTC", "market_type": "spot", "score": score}],
    )

    assert event is not None
    assert "score" not in event.provider_metadata
    assert event.provider_metadata["coins"] == [{"symbol": "BTC", "market_type": "spot"}]


def test_empty_provider_coins_do_not_create_strategy_asset_metadata() -> None:
    event = _strategy_event(coins=[])

    assert event is not None
    assert "coins" not in event.provider_metadata


@pytest.mark.parametrize("wire_id", [None, True, 10.19, "", "bad\x00id", "x" * 129])
def test_invalid_wire_event_or_strategy_ids_are_ignored(wire_id: object) -> None:
    assert _strategy_event(id=wire_id) is None
    assert _strategy_event(strategy={"id": wire_id}) is None


def _strategy_event(**overrides):
    strategy = overrides.pop("strategy", {"id": "strategy-test", "name": "Test Strategy"})
    params = {
        "id": "wire-test",
        "engineType": "news",
        "text": "Strategy report",
        "link": None,
        "ts": 1_775_195_200_000,
        "strategy": strategy,
        **overrides,
    }
    return parse_opennews_message(
        {"method": "strategy.triggered", "params": params},
    )


def test_healthy_opennews_idle_keeps_the_same_websocket(monkeypatch) -> None:
    class _WebSocket:
        def __init__(self) -> None:
            self.receive_calls = 0
            self.ping_calls = 0

        async def recv(self):
            self.receive_calls += 1
            if self.receive_calls == 1:
                await asyncio.Future()
            return "next-frame"

        async def ping(self):
            self.ping_calls += 1
            pong = asyncio.get_running_loop().create_future()
            pong.set_result(None)
            return pong

    websocket = _WebSocket()
    monkeypatch.setattr(opennews_client, "OPENNEWS_WS_IDLE_SECONDS", 0.001)

    assert asyncio.run(opennews_client._bounded_recv(websocket)) == "next-frame"
    assert websocket.receive_calls == 2
    assert websocket.ping_calls == 1


def test_opennews_receive_race_owns_child_tasks_during_cancellation() -> None:
    class _Client:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()

        async def receive(self):
            self.started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise

    async def scenario() -> None:
        client = _Client()
        task = asyncio.create_task(news_consumers._receive_or_stop(client, stop_event=asyncio.Event()))
        await asyncio.wait_for(client.started.wait(), timeout=1.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(client.cancelled.wait(), timeout=1.0)

    asyncio.run(scenario())
