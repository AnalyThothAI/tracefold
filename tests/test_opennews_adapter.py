from __future__ import annotations

import asyncio

import pytest
from websockets.exceptions import ConcurrencyError, ProtocolError

import tracefold.news.runtime as news_runtime
from tracefold.integrations.news_feeds import NewsFeedAcquisitionError, NewsFeedWire, parse_rss_feed_wire
from tracefold.integrations.opennews import client as opennews_client
from tracefold.news import NewsAcquisition, OpenNewsExpectedError
from tracefold.news.models import NewsSourceDefinition
from tracefold.news.opennews import parse_opennews_message
from tracefold.news.sources import OPENNEWS_SOURCE_ID, opennews_source
from tracefold.platform.config.settings import NewsSettings
from tracefold.platform.resource import ResourceCapability, ResourceOperationOverrun


def test_opennews_source_is_the_production_source() -> None:
    source = opennews_source()

    assert source.source_id == OPENNEWS_SOURCE_ID
    assert source.source_kind == "opennews"
    assert source.model_dump() == {
        "source_id": "news-opennews",
        "name": "OpenNews",
        "tier": 4,
        "lang": "en",
        "source_kind": "opennews",
        "enabled": True,
        "feed_url": None,
        "memberships": (),
        "refresh_interval_seconds": 1800,
    }


def test_opennews_strategy_configuration_is_normalized_and_fails_closed() -> None:
    assert NewsSettings().rss_enabled is False
    assert NewsSettings(rss_enabled=True).rss_enabled is True
    configured = NewsSettings(
        opennews_token="  secret  ",
        opennews_strategy_ids=["1019", " 1018 ", "listing-private"],
    )
    assert configured.opennews_token == "secret"
    assert configured.opennews_strategy_ids == ("1018", "1019", "listing-private")
    assert NewsSettings(opennews_token="  ").opennews_token is None

    with pytest.raises(ValueError, match="opennews_strategy_ids_required"):
        NewsSettings(opennews_token="secret")
    with pytest.raises(ValueError, match="opennews_strategy_ids_duplicate"):
        NewsSettings(opennews_token="secret", opennews_strategy_ids=["1018", " 1018 "])


@pytest.mark.parametrize("strategy_id", [None, True, 1019, 10.19, "", "bad\x00id", "x" * 129])
def test_opennews_strategy_ids_reject_invalid_values(strategy_id: object) -> None:
    with pytest.raises(ValueError):
        NewsSettings(opennews_token="secret", opennews_strategy_ids=[strategy_id])


def test_opennews_strategy_configuration_is_bounded_to_32_ids() -> None:
    boundary = NewsSettings(
        opennews_token="secret",
        opennews_strategy_ids=[f"strategy-{index:02d}" for index in range(32)],
    )
    assert len(boundary.opennews_strategy_ids) == 32

    with pytest.raises(ValueError):
        NewsSettings(
            opennews_token="secret",
            opennews_strategy_ids=[f"strategy-{index}" for index in range(33)],
        )


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
        strategy_ids=frozenset({"1019"}),
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


@pytest.mark.parametrize(
    ("strategy_id", "engine_type"),
    [
        ("news-score", "NEWS"),
        ("storage-news", "MEME"),
        ("oi-monitor", "MARKET"),
        ("listing-monitor", "listing"),
    ],
)
def test_allowlist_is_the_only_strategy_admission_filter(strategy_id: str, engine_type: str) -> None:
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
        strategy_ids=frozenset({strategy_id}),
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


def test_raw_news_and_unconfigured_strategy_frames_are_ignored() -> None:
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
                strategy_ids=frozenset({"configured"}),
            )
            is None
        )
    assert (
        parse_opennews_message(
            {
                "method": "strategy.triggered",
                "params": {**base_params, "strategy": {"id": "not-configured"}},
            },
            strategy_ids=frozenset({"configured"}),
        )
        is None
    )


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
            strategy_ids=frozenset({"1018", "1019"}),
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


def test_ws_only_runtime_publishes_the_first_allowlisted_strategy_frame() -> None:
    async def scenario() -> tuple[list[str], tuple[str, ...]]:
        stop_event = asyncio.Event()

        class _Database:
            def __init__(self) -> None:
                self.operations: list[str] = []
                self.provider_ids: tuple[str, ...] = ()

            async def run_business(self, operation_name, _function, /, *args, **_kwargs):
                self.operations.append(operation_name)
                if operation_name == "opennews_live_publish":
                    self.provider_ids = tuple(event.provider_record_id for event in args[0])
                    stop_event.set()
                    return {"items_inserted": len(self.provider_ids)}
                return True

        class _WebSocketClient:
            def __init__(self) -> None:
                self.receive_calls = 0

            async def connect(self) -> None:
                return None

            async def receive(self):
                self.receive_calls += 1
                if self.receive_calls == 1:
                    return {
                        "method": "strategy.triggered",
                        "params": {
                            "id": 3_568_501,
                            "engineType": "market",
                            "text": "ETH open interest crossed USD 3M",
                            "source": "hyperliquid",
                            "ts": 1_775_195_200_000,
                            "strategy": {
                                "id": 1019,
                                "name": "OI Event Monitor",
                                "sourceType": "market",
                            },
                        },
                    }
                await stop_event.wait()
                return None

            async def close(self) -> None:
                return None

        database = _Database()
        acquisition = NewsAcquisition(
            db=database,
            finite_operations=_InlineFiniteOperations(),
            rss_sources=(),
            rss_feed_reader=_FeedReader(_rss_wire()),
            rss_feed_parser=parse_rss_feed_wire,
            opennews_source=opennews_source(),
            opennews_strategy_ids=("1019",),
            opennews_ws_client=_WebSocketClient(),
        )
        await asyncio.wait_for(acquisition.run_opennews(stop_event=stop_event), timeout=1.0)
        return database.operations, database.provider_ids

    operations, provider_ids = asyncio.run(scenario())
    assert operations.count("opennews_live_publish") == 1
    assert set(operations) == {"opennews_status", "opennews_live_publish"}
    assert provider_ids == ("3568501",)


def test_websocket_receive_classifies_protocol_disconnect() -> None:
    class _WebSocket:
        async def recv(self):
            raise ProtocolError("invalid provider frame")

    client = opennews_client.OpenNewsWebSocketClient(token="test-token")
    client._websocket = _WebSocket()

    with pytest.raises(OpenNewsExpectedError, match="opennews_receive_failed"):
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
        with pytest.raises(OpenNewsExpectedError, match="opennews_frame_invalid"):
            asyncio.run(client.receive())


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
        strategy_ids=frozenset({"strategy-private"}),
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
        strategy_ids=frozenset({"strategy-test"}),
    )


class _InlineFiniteOperations:
    async def run(self, _operation_name, function, /, *args, **kwargs):
        kwargs.pop("timeout_seconds")
        kwargs.pop("allow_shutdown", None)
        return function(*args, **kwargs)


class _FeedReader:
    def __init__(self, result: NewsFeedWire | BaseException) -> None:
        self.result = result
        self.requests: list[tuple[str | None, str | None]] = []
        self.closed = False

    def fetch_wire(self, *, source, etag, last_modified):
        del source
        self.requests.append((etag, last_modified))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result

    def close(self) -> None:
        self.closed = True


def _rss_source() -> NewsSourceDefinition:
    return NewsSourceDefinition(
        source_id="news-rss-test",
        name="Public Wire",
        tier=2,
        source_kind="rss",
        feed_url="https://news.example.org/feed.xml",
        memberships=("politics",),
    )


def _rss_wire() -> NewsFeedWire:
    return NewsFeedWire(
        status_code=200,
        source_name="Public Wire",
        source_lang="en",
        body=b"""
            <rss><channel><item>
              <title>Public policy update</title>
              <link>https://news.example.org/update</link>
              <pubDate>Sat, 09 Aug 2026 00:00:00 GMT</pubDate>
            </item></channel></rss>
        """,
        etag='"new"',
        last_modified="Sat, 09 Aug 2026 00:00:00 GMT",
        not_modified=False,
    )


def _acquisition(
    *,
    db,
    reader: _FeedReader | None = None,
    finite_operations=None,
    websocket_client=None,
) -> NewsAcquisition:
    return NewsAcquisition(
        db=db,
        finite_operations=finite_operations or _InlineFiniteOperations(),
        rss_sources=(_rss_source(),),
        rss_feed_reader=reader or _FeedReader(_rss_wire()),
        rss_feed_parser=parse_rss_feed_wire,
        opennews_source=opennews_source(),
        opennews_strategy_ids=("strategy-test",) if websocket_client is not None else (),
        opennews_ws_client=websocket_client,
    )


def test_ws_only_runtime_is_disabled_without_a_websocket_adapter() -> None:
    acquisition = _acquisition(db=object())

    assert acquisition.opennews_enabled is False
    assert not hasattr(acquisition, "opennews_rest_client")
    assert not any("recovery" in name for name in vars(acquisition))


@pytest.mark.parametrize(
    "strategy_ids",
    [("1018", " 1018 "), ("1018", ""), ("1018", 1019)],
)
def test_ws_runtime_rejects_noncanonical_strategy_ids(strategy_ids: tuple[object, ...]) -> None:
    with pytest.raises(ValueError, match="opennews_strategy_ids_invalid"):
        NewsAcquisition(
            db=object(),
            finite_operations=_InlineFiniteOperations(),
            rss_sources=(),
            rss_feed_reader=_FeedReader(_rss_wire()),
            rss_feed_parser=parse_rss_feed_wire,
            opennews_source=opennews_source(),
            opennews_strategy_ids=strategy_ids,
            opennews_ws_client=object(),
        )


def test_opennews_business_database_overrun_restarts_the_existing_stream_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> int:
        stop_event = asyncio.Event()

        class _Database:
            async def run_business(self, operation_name, _function, /, *_args, **_kwargs):
                assert operation_name == "opennews_status"
                return True

        acquisition = _acquisition(
            db=_Database(),
            websocket_client=object(),
        )
        attempts = 0

        async def receive(_stop_event: asyncio.Event) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ResourceOperationOverrun(
                    capability=ResourceCapability.DATABASE_BUSINESS,
                    operation_name="opennews_live_publish",
                )
            stop_event.set()

        async def idle(_stop_event: asyncio.Event) -> None:
            await stop_event.wait()

        acquisition._opennews_receive_loop = receive  # type: ignore[method-assign]
        acquisition._opennews_publish_loop = idle  # type: ignore[method-assign]

        await acquisition.run_opennews(stop_event=stop_event)
        return attempts

    monkeypatch.setattr(news_runtime, "_OPENNEWS_RECONNECT_SECONDS", 0.0)

    assert asyncio.run(scenario()) == 2


def test_opennews_database_overrun_retries_the_pending_batch_with_original_clock_and_marks_unknown_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = {"now_ms": 1_000}

    async def scenario() -> tuple[list[tuple[str, ...]], list[int], list[bool]]:
        stop_event = asyncio.Event()

        class _Database:
            def __init__(self) -> None:
                self.publish_attempts: list[tuple[str, ...]] = []
                self.publish_clocks: list[int] = []
                self.coverage_gaps: list[bool] = []

            async def run_business(self, operation_name, _function, /, *args, **_kwargs):
                if operation_name == "opennews_status":
                    self.coverage_gaps.append(bool(args[4]))
                    return True
                if operation_name == "opennews_live_publish":
                    ids = tuple(event.provider_record_id for event in args[0])
                    self.publish_attempts.append(ids)
                    self.publish_clocks.append(int(args[1]))
                    if len(self.publish_attempts) == 1:
                        clock["now_ms"] = 2_000
                        raise ResourceOperationOverrun(
                            capability=ResourceCapability.DATABASE_BUSINESS,
                            operation_name="opennews_live_publish",
                        )
                    stop_event.set()
                    return {"items_inserted": 1}
                raise AssertionError(operation_name)

        class _WebSocketClient:
            async def connect(self) -> None:
                return None

            async def receive(self):
                await stop_event.wait()

            async def close(self) -> None:
                return None

        database = _Database()
        acquisition = _acquisition(db=database, websocket_client=_WebSocketClient())
        accepted = _strategy_event(id="3568500")
        assert accepted is not None
        acquisition._opennews_queue.put_nowait(accepted)
        await acquisition.run_opennews(stop_event=stop_event)
        return database.publish_attempts, database.publish_clocks, database.coverage_gaps

    monkeypatch.setattr(news_runtime, "_OPENNEWS_RECONNECT_SECONDS", 0.0)
    monkeypatch.setattr(news_runtime, "_now_ms", lambda: clock["now_ms"])

    attempts, publish_clocks, coverage_gaps = asyncio.run(scenario())
    assert attempts == [("3568500",), ("3568500",)]
    assert publish_clocks == [1_000, 1_000]
    assert coverage_gaps[0] is False
    assert True in coverage_gaps[1:]


def test_opennews_non_database_overrun_remains_fatal() -> None:
    async def scenario() -> None:
        stop_event = asyncio.Event()
        acquisition = _acquisition(
            db=object(),
            websocket_client=object(),
        )

        async def receive(_stop_event: asyncio.Event) -> None:
            raise ResourceOperationOverrun(
                capability=ResourceCapability.FINITE_OPERATION,
                operation_name="opennews_live_publish",
            )

        async def idle(_stop_event: asyncio.Event) -> None:
            await stop_event.wait()

        acquisition._opennews_receive_loop = receive  # type: ignore[method-assign]
        acquisition._opennews_publish_loop = idle  # type: ignore[method-assign]

        await acquisition.run_opennews(stop_event=stop_event)

    with pytest.raises(ExceptionGroup) as raised:
        asyncio.run(scenario())
    leaf = raised.value.exceptions[0]
    assert isinstance(leaf, ResourceOperationOverrun)
    assert leaf.operation_name == "opennews_live_publish"


def test_rss_turn_claims_fetches_parses_and_publishes_one_due_source() -> None:
    class _Database:
        def __init__(self) -> None:
            self.operations: list[str] = []
            self.published = None

        async def run_business(self, operation_name, _function, /, *args, **_kwargs):
            self.operations.append(operation_name)
            if operation_name == "news_rss_claim":
                return {
                    "source_id": "news-rss-test",
                    "etag": '"old"',
                    "last_modified": "Fri, 08 Aug 2026 00:00:00 GMT",
                }
            if operation_name == "news_rss_publish":
                self.published = args
                return {"items_inserted": 1}
            raise AssertionError(operation_name)

    database = _Database()
    reader = _FeedReader(_rss_wire())

    assert asyncio.run(_acquisition(db=database, reader=reader).turn()) is True
    assert database.operations == ["news_rss_claim", "news_rss_publish"]
    assert reader.requests == [('"old"', "Fri, 08 Aug 2026 00:00:00 GMT")]
    assert database.published is not None
    source, _claim_token, fetch, _finished_at_ms = database.published
    assert source.source_id == "news-rss-test"
    assert [entry.title for entry in fetch.entries] == ["Public policy update"]


def test_rss_turn_records_bounded_expected_failure_and_releases_the_claim() -> None:
    class _Database:
        def __init__(self) -> None:
            self.failure = None

        async def run_business(self, operation_name, _function, /, *args, **_kwargs):
            if operation_name == "news_rss_claim":
                return {"source_id": "news-rss-test", "etag": None, "last_modified": None}
            if operation_name == "news_rss_failure":
                self.failure = args
                return True
            raise AssertionError(operation_name)

    database = _Database()
    reader = _FeedReader(NewsFeedAcquisitionError("news_rss_http_503", status_code=503))

    assert asyncio.run(_acquisition(db=database, reader=reader).turn()) is True
    assert database.failure is not None
    source_id, claim_token, _finished_at_ms, error_code, status_code = database.failure
    assert source_id == "news-rss-test"
    assert claim_token
    assert (error_code, status_code) == ("news_rss_http_503", 503)


def test_rss_turn_is_idle_when_no_source_is_due() -> None:
    class _Database:
        async def run_business(self, operation_name, _function, /, *_args, **_kwargs):
            assert operation_name == "news_rss_claim"

    assert asyncio.run(_acquisition(db=_Database()).turn()) is False


def test_default_disabled_rss_turn_performs_no_feed_request() -> None:
    class _Database:
        def __init__(self) -> None:
            self.operations: list[str] = []

        async def run_business(self, operation_name, _function, /, *_args, **_kwargs):
            self.operations.append(operation_name)
            assert operation_name == "news_rss_claim"

    database = _Database()
    reader = _FeedReader(_rss_wire())
    acquisition = NewsAcquisition(
        db=database,
        finite_operations=_InlineFiniteOperations(),
        rss_sources=(),
        rss_feed_reader=reader,
        rss_feed_parser=parse_rss_feed_wire,
        opennews_source=opennews_source(),
        opennews_strategy_ids=(),
    )

    assert asyncio.run(acquisition.turn()) is False
    assert database.operations == ["news_rss_claim"]
    assert reader.requests == []


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
        task = asyncio.create_task(news_runtime._receive_or_stop(client, stop_event=asyncio.Event()))
        await asyncio.wait_for(client.started.wait(), timeout=1.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(client.cancelled.wait(), timeout=1.0)

    asyncio.run(scenario())
