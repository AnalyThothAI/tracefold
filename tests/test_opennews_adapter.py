from __future__ import annotations

import asyncio
import time

import httpx
import pytest
from websockets.exceptions import ConcurrencyError, ProtocolError

import tracefold.news.runtime as news_runtime
from tracefold.integrations.news_feeds import NewsFeedAcquisitionError, NewsFeedWire, parse_rss_feed_wire
from tracefold.integrations.opennews import client as opennews_client
from tracefold.news import NewsAcquisition, OpenNewsExpectedError
from tracefold.news.models import NewsSourceDefinition
from tracefold.news.opennews import OpenNewsOverlapPage, parse_opennews_message, parse_opennews_rest_response
from tracefold.news.sources import OPENNEWS_SOURCE_ID, opennews_source
from tracefold.platform.config.settings import NewsSettings
from tracefold.platform.resource import ResourceOperationOverrun


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


def test_opennews_token_is_trimmed_and_optional() -> None:
    assert NewsSettings().rss_enabled is False
    assert NewsSettings(rss_enabled=True).rss_enabled is True
    assert NewsSettings(opennews_token="  secret  ").opennews_token == "secret"
    assert NewsSettings(opennews_token="  ").opennews_token is None


def test_websocket_handshake_owns_and_closes_partial_connection(monkeypatch) -> None:
    class _WebSocket:
        def __init__(self) -> None:
            self.owned_during_send = False
            self.close_calls = 0

        async def send(self, _payload: str) -> None:
            self.owned_during_send = client._websocket is self

        async def recv(self) -> str:
            return "not-json"

        async def close(self) -> None:
            self.close_calls += 1

    async def connect(*_args, **_kwargs):
        return websocket

    async def scenario() -> None:
        with pytest.raises(OpenNewsExpectedError, match="opennews_frame_invalid"):
            await client.connect()

    client = opennews_client.OpenNewsWebSocketClient(token="test-token")
    websocket = _WebSocket()
    monkeypatch.setattr(opennews_client.websockets, "connect", connect)

    asyncio.run(scenario())

    assert websocket.owned_during_send
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


def test_websocket_receive_classifies_protocol_disconnect() -> None:
    class _WebSocket:
        async def recv(self):
            raise ProtocolError("invalid provider frame")

    client = opennews_client.OpenNewsWebSocketClient(token="test-token")
    client._websocket = _WebSocket()

    with pytest.raises(OpenNewsExpectedError, match="opennews_receive_failed"):
        asyncio.run(client.receive())


def test_websocket_receive_classifies_invalid_utf8_provider_frame() -> None:
    class _WebSocket:
        async def recv(self):
            return b"\xff\xfe"

    client = opennews_client.OpenNewsWebSocketClient(token="test-token")
    client._websocket = _WebSocket()

    with pytest.raises(OpenNewsExpectedError, match="opennews_frame_invalid"):
        asyncio.run(client.receive())


def test_websocket_receive_classifies_pathologically_nested_provider_frame() -> None:
    class _WebSocket:
        async def recv(self):
            return "[" * 10_000 + "]" * 10_000

    client = opennews_client.OpenNewsWebSocketClient(token="test-token")
    client._websocket = _WebSocket()

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


def test_report_normalization_keeps_only_bounded_provider_metadata() -> None:
    event = parse_opennews_message(
        {
            "method": "news.update",
            "params": {
                "id": "wire-1",
                "text": "Fed holds rates steady",
                "newsType": "Reuters",
                "engineType": "news",
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
            },
        }
    )

    assert event is not None
    assert event.observation_kind == "report"
    assert event.provider_record_id == "wire-1"
    assert event.entry is not None
    assert event.entry.link == "https://example.com/article/1?a=1&b=2"
    assert event.entry.reporting_origin == "reuters"
    assert event.entry.published_at_ms == 1_785_560_400_000
    assert event.provider_metadata == {
        "score": 99,
        "source": "jin10",
        "signal": "long",
        "grade": "A",
        "coins": [{"symbol": "BTC", "market_type": "spot", "match": "Bitcoin"}],
    }


def test_rest_report_keeps_observed_top_level_numeric_score() -> None:
    page = parse_opennews_rest_response(
        {
            "success": True,
            "data": [
                {
                    "id": "wire-rest-score",
                    "text": "Rated recovery report",
                    "newsType": "Reuters",
                    "engineType": "news",
                    "ts": "2026-08-03T05:34:47.635316+08:00",
                    "score": 75,
                    "aiRating": {
                        "score": 75,
                        "signal": "long",
                        "grade": "A",
                        "status": "done",
                    },
                }
            ],
        }
    )

    assert page.is_last_page is True
    assert len(page.events) == 1
    assert page.events[0].provider_metadata == {
        "score": 75,
        "signal": "long",
        "grade": "A",
    }


def test_malformed_article_url_keeps_report_as_linkless() -> None:
    event = parse_opennews_message(
        {
            "method": "news.update",
            "params": {
                "id": "wire-malformed-url",
                "text": "Provider supplied an invalid URL",
                "newsType": "Reuters",
                "engineType": "news",
                "link": "https://[broken",
                "ts": 1_775_195_200_000,
            },
        }
    )

    assert event is not None and event.entry is not None
    assert event.entry.link is None


@pytest.mark.parametrize(
    ("invalid_text", "expected_title"),
    [("bad\x00text", "bad text"), ("bad\ud800text", None)],
)
def test_wire_text_strips_controls_and_rejects_non_utf8(
    invalid_text: str,
    expected_title: str | None,
) -> None:
    event = parse_opennews_message(
        {
            "method": "news.update",
            "params": {
                "id": "wire-invalid-text",
                "text": invalid_text,
                "description": invalid_text,
                "newsType": "Reuters",
                "engineType": "news",
                "link": f"https://example.com/{invalid_text}",
                "ts": 1_775_195_200_000,
                "score": 75,
                "source": invalid_text,
                "signal": invalid_text,
                "grade": invalid_text,
                "coins": [{"symbol": invalid_text, "market_type": "spot"}],
            },
        }
    )

    assert event is not None and event.entry is not None
    assert event.entry.title == expected_title
    assert event.entry.description == ""
    assert event.entry.link is None
    assert event.provider_metadata == {"score": 75}


def test_headline_clamp_uses_javascript_utf16_units_and_valid_utf8() -> None:
    event = parse_opennews_message(
        {
            "method": "news.update",
            "params": {
                "id": "wire-astral-clamp",
                "text": "a" * 499 + "𝔸" + "z",
                "newsType": "Reuters",
                "engineType": "news",
                "ts": 1_775_195_200_000,
            },
        }
    )

    assert event is not None and event.entry is not None
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
def test_description_bounds_use_javascript_utf16_units_and_valid_utf8(
    description: str,
    expected: str,
) -> None:
    event = parse_opennews_message(
        {
            "method": "news.update",
            "params": {
                "id": "wire-description-clamp",
                "text": "Canonical headline differs from the description evidence",
                "description": description,
                "newsType": "Reuters",
                "engineType": "news",
                "ts": 1_775_195_200_000,
            },
        }
    )

    assert event is not None and event.entry is not None
    assert event.entry.description == expected


def test_description_equality_uses_javascript_lowercase_not_casefold() -> None:
    title = "Straße market update with enough context for the public evidence boundary"
    description = "Strasse market update with enough context for the public evidence boundary"
    event = parse_opennews_message(
        {
            "method": "news.update",
            "params": {
                "id": "wire-description-case",
                "text": title,
                "description": description,
                "newsType": "Reuters",
                "engineType": "news",
                "ts": 1_775_195_200_000,
            },
        }
    )

    assert event is not None and event.entry is not None
    assert event.entry.description == description


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("\ufeff1234567890", "1234567890"),
        ("Alpha\ufeffBeta announces a public policy update", "Alpha Beta announces a public policy update"),
    ],
)
def test_plaintext_blocks_use_javascript_whitespace(text: str, expected: str) -> None:
    event = parse_opennews_message(
        {
            "method": "news.update",
            "params": {
                "id": "wire-js-whitespace",
                "text": text,
                "newsType": "\ufeffReuters\ufeff",
                "engineType": "\ufeffnews\ufeff",
                "ts": 1_775_195_200_000,
            },
        }
    )

    assert event is not None and event.entry is not None
    assert event.entry.title == expected
    assert event.entry.reporting_origin == "reuters"


@pytest.mark.parametrize("timestamp", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_timestamp_becomes_missing_date(timestamp: float) -> None:
    event = parse_opennews_message(
        {
            "method": "news.update",
            "params": {
                "id": "wire-invalid-time",
                "text": "Provider supplied an invalid timestamp",
                "newsType": "Reuters",
                "engineType": "news",
                "ts": timestamp,
            },
        }
    )

    assert event is not None and event.entry is not None
    assert event.entry.published_at_ms is None


@pytest.mark.parametrize("score", [float("nan"), float("inf"), float("-inf"), -1, 101])
def test_non_finite_or_out_of_range_scores_are_discarded(score: float) -> None:
    event = parse_opennews_message(
        {
            "method": "news.update",
            "params": {
                "id": "wire-invalid-score",
                "text": "Provider supplied an invalid score",
                "newsType": "Reuters",
                "engineType": "news",
                "ts": 1_775_195_200_000,
                "score": score,
                "coins": [
                    {
                        "symbol": "BTC",
                        "market_type": "spot",
                        "score": score,
                    }
                ],
            },
        }
    )

    assert event is not None
    assert "score" not in event.provider_metadata
    assert event.provider_metadata["coins"] == [{"symbol": "BTC", "market_type": "spot"}]


@pytest.mark.parametrize("link", [None, "#fragment", "https://reuters.com", "https://reuters.com/"])
def test_linkless_or_homepage_wire_keeps_provider_identity(link: str | None) -> None:
    event = parse_opennews_message(
        {
            "method": "news.update",
            "params": {
                "id": "wire-2",
                "text": "Linkless wire",
                "newsType": "Reuters",
                "engineType": "news",
                "link": link,
                "ts": 1_775_195_200_000,
            },
        }
    )

    assert event is not None
    assert event.provider_record_id == "wire-2"
    assert event.entry is not None
    assert event.entry.link is None


def test_translation_is_discardable_and_ai_update_carries_current_metadata() -> None:
    translation = parse_opennews_message(
        {
            "method": "news.update",
            "params": {
                "id": "wire-3",
                "text": "翻译文本",
                "newsType": "Translation",
                "engineType": "news",
                "ts": 1_775_195_200_000,
            },
        }
    )
    annotation = parse_opennews_message(
        {
            "method": "news.ai_update",
            "params": {
                "newsId": 3_442_202,
                "engineType": "news",
                "newsType": "Reuters",
                "score": 90,
                "signal": "long",
                "grade": "A+",
                "coins": [
                    {
                        "symbol": "BTC",
                        "market_type": "spot",
                        "score": 90,
                        "signal": "long",
                        "grade": "A+",
                    }
                ],
            },
        }
    )

    assert translation is not None and translation.observation_kind == "translation"
    assert translation.entry is None
    assert annotation is not None and annotation.observation_kind == "provider_annotation"
    assert annotation.provider_record_id == "3442202"
    assert annotation.entry is None
    assert annotation.provider_metadata == {
        "score": 90,
        "signal": "long",
        "grade": "A+",
        "coins": [
            {
                "symbol": "BTC",
                "market_type": "spot",
                "score": 90,
                "signal": "long",
                "grade": "A+",
            }
        ],
    }


def test_empty_provider_coins_do_not_erase_current_metadata() -> None:
    event = parse_opennews_message(
        {
            "method": "news.update",
            "params": {
                "id": "wire-empty-coins",
                "text": "Provider sends an empty late coin list",
                "newsType": "Reuters",
                "engineType": "news",
                "ts": 1_775_195_200_000,
                "coins": [],
            },
        }
    )

    assert event is not None
    assert "coins" not in event.provider_metadata


def test_strategy_and_non_news_engine_are_ignored() -> None:
    assert parse_opennews_message({"method": "strategy.triggered", "params": {"id": "x"}}) is None
    assert (
        parse_opennews_message(
            {
                "method": "news.update",
                "params": {"id": "x", "engineType": "listing", "text": "listed"},
            }
        )
        is None
    )


def test_rest_page_uses_the_same_message_normalizer_and_is_bounded() -> None:
    rows = [
        {
            "id": f"wire-{index}",
            "text": f"headline {index}",
            "newsType": "Reuters",
            "engineType": "news",
            "ts": 1_775_195_200_000,
        }
        for index in range(105)
    ]

    page = parse_opennews_rest_response({"success": True, "data": rows})

    assert page.is_last_page is False
    assert len(page.events) == 100
    assert page.events[0].provider_record_id == "wire-0"


def test_rest_page_end_uses_provider_rows_instead_of_parsed_events() -> None:
    rows = [
        {
            "id": f"wire-{index}",
            "text": f"headline {index}",
            "newsType": "Reuters",
            "engineType": "news",
            "ts": 1_775_195_200_000,
        }
        for index in range(99)
    ]
    rows.append({"engineType": "news", "text": "missing provider id"})

    page = parse_opennews_rest_response({"success": True, "data": rows})

    assert len(page.events) == 99
    assert page.is_last_page is False


def test_rest_client_requests_the_selected_recovery_page() -> None:
    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return {
                "data": [
                    {
                        "id": "wire-page-7",
                        "text": "Page seven recovery item",
                        "newsType": "Reuters",
                        "engineType": "news",
                        "ts": 1_775_195_200_000,
                    }
                ]
            }

    class _HttpClient:
        def __init__(self) -> None:
            self.request_json = None

        def post(self, _url, *, json):
            self.request_json = json
            return _Response()

    http_client = _HttpClient()
    client = object.__new__(opennews_client.OpenNewsRestClient)
    client._client = http_client

    page = client.fetch_overlap_page(7)

    assert [event.provider_record_id for event in page.events] == ["wire-page-7"]
    assert page.is_last_page is True
    assert http_client.request_json == {
        "engineTypes": {"news": []},
        "limit": 100,
        "page": 7,
    }


def test_rest_client_classifies_transport_failure() -> None:
    class _HttpClient:
        def post(self, _url, *, json):
            del json
            request = httpx.Request("POST", "https://opennews.test/recovery")
            raise httpx.ConnectError("connection refused", request=request)

    client = object.__new__(opennews_client.OpenNewsRestClient)
    client._client = _HttpClient()

    with pytest.raises(OpenNewsExpectedError, match="opennews_rest_failed"):
        client.fetch_overlap_page(1)


def test_rest_client_does_not_hide_programming_errors() -> None:
    class _HttpClient:
        def post(self, _url, *, json):
            del json
            raise AssertionError("programming bug")

    client = object.__new__(opennews_client.OpenNewsRestClient)
    client._client = _HttpClient()

    with pytest.raises(AssertionError, match="programming bug"):
        client.fetch_overlap_page(1)


def test_rest_client_does_not_hide_request_usage_errors() -> None:
    class _HttpClient:
        def post(self, _url, *, json):
            del json
            raise ValueError("invalid request construction")

    client = object.__new__(opennews_client.OpenNewsRestClient)
    client._client = _HttpClient()

    with pytest.raises(ValueError, match="request construction"):
        client.fetch_overlap_page(1)


def test_rest_client_classifies_invalid_provider_json() -> None:
    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            raise ValueError("invalid provider JSON")

    class _HttpClient:
        def post(self, _url, *, json):
            del json
            return _Response()

    client = object.__new__(opennews_client.OpenNewsRestClient)
    client._client = _HttpClient()

    with pytest.raises(OpenNewsExpectedError, match="opennews_rest_failed"):
        client.fetch_overlap_page(1)


def test_rest_client_classifies_pathologically_nested_provider_json() -> None:
    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            raise RecursionError("provider JSON nesting exceeded")

    class _HttpClient:
        def post(self, _url, *, json):
            del json
            return _Response()

    client = object.__new__(opennews_client.OpenNewsRestClient)
    client._client = _HttpClient()

    with pytest.raises(OpenNewsExpectedError, match="opennews_rest_failed"):
        client.fetch_overlap_page(1)


def test_invalid_rest_shape_fails_closed() -> None:
    with pytest.raises(OpenNewsExpectedError, match="opennews_rest_payload_invalid"):
        parse_opennews_rest_response({"data": "not-a-list"})


class _InlineFiniteOperations:
    async def run(self, _operation_name, function, /, *args, **kwargs):
        kwargs.pop("timeout_seconds")
        kwargs.pop("allow_shutdown", None)
        return function(*args, **kwargs)


class _CountingEvent(asyncio.Event):
    def __init__(self) -> None:
        super().__init__()
        self.set_calls = 0

    def set(self) -> None:
        self.set_calls += 1
        super().set()


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
    rest_client=None,
    websocket_client=None,
) -> NewsAcquisition:
    return NewsAcquisition(
        db=db,
        finite_operations=finite_operations or _InlineFiniteOperations(),
        rss_sources=(_rss_source(),),
        rss_feed_reader=reader or _FeedReader(_rss_wire()),
        rss_feed_parser=parse_rss_feed_wire,
        opennews_source=opennews_source(),
        opennews_rest_client=rest_client,
        opennews_ws_client=websocket_client,
    )


def test_opennews_reconcile_waits_for_the_stream_connection_before_recovery() -> None:
    class _Database:
        async def run_business(self, operation_name, _function, /, *_args, **_kwargs):
            assert operation_name == "news_source_reconcile"

    acquisition = _acquisition(
        db=_Database(),
        rest_client=object(),
        websocket_client=object(),
    )

    asyncio.run(acquisition.reconcile())

    assert not acquisition._opennews_recovery_requested.is_set()


def test_opennews_receive_failure_does_not_duplicate_connection_recovery() -> None:
    async def scenario() -> int:
        stop_event = asyncio.Event()

        class _Database:
            async def run_business(self, operation_name, _function, /, *_args, **_kwargs):
                assert operation_name == "opennews_status"
                return True

        class _WebSocketClient:
            async def connect(self) -> None:
                return None

            async def receive(self):
                raise OpenNewsExpectedError("opennews_receive_failed")

            async def close(self) -> None:
                stop_event.set()

        acquisition = _acquisition(
            db=_Database(),
            rest_client=object(),
            websocket_client=_WebSocketClient(),
        )
        requests = _CountingEvent()
        acquisition._opennews_recovery_requested = requests

        await acquisition._opennews_receive_loop(stop_event)
        return requests.set_calls

    assert asyncio.run(scenario()) == 1


def test_opennews_queue_overflow_reconnects_before_requesting_overlap() -> None:
    async def scenario() -> tuple[int, list[str | None]]:
        stop_event = asyncio.Event()

        class _Database:
            def __init__(self) -> None:
                self.errors: list[str | None] = []

            async def run_business(self, operation_name, _function, /, *args, **_kwargs):
                assert operation_name == "opennews_status"
                self.errors.append(args[3])
                return True

        class _WebSocketClient:
            def __init__(self) -> None:
                self.receive_calls = 0

            async def connect(self) -> None:
                return None

            async def receive(self):
                self.receive_calls += 1
                if self.receive_calls > 1:
                    raise OpenNewsExpectedError("opennews_receive_failed")
                return {
                    "method": "news.update",
                    "params": {
                        "id": "overflowed-live-event",
                        "text": "Overflowed live event",
                        "newsType": "Reuters",
                        "engineType": "news",
                        "ts": int(time.time() * 1_000),
                    },
                }

            async def close(self) -> None:
                stop_event.set()

        database = _Database()
        acquisition = _acquisition(
            db=database,
            rest_client=object(),
            websocket_client=_WebSocketClient(),
        )
        acquisition._opennews_queue = asyncio.Queue(maxsize=1)
        acquisition._opennews_queue.put_nowait(_report("already-buffered"))
        requests = _CountingEvent()
        acquisition._opennews_recovery_requested = requests

        await acquisition._opennews_receive_loop(stop_event)
        return requests.set_calls, database.errors

    assert asyncio.run(scenario()) == (
        1,
        [None, "opennews_buffer_overflow", None],
    )


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
    )

    assert asyncio.run(acquisition.turn()) is False
    assert database.operations == ["news_rss_claim"]
    assert reader.requests == []


def _report(provider_record_id: str):
    event = parse_opennews_message(
        {
            "method": "news.update",
            "params": {
                "id": provider_record_id,
                "text": provider_record_id,
                "newsType": "Reuters",
                "engineType": "news",
                "ts": int(time.time() * 1_000) - 1_000,
            },
        }
    )
    assert event is not None
    return event


def test_opennews_overlap_starts_at_newest_page_and_stops_on_repository_overlap(monkeypatch) -> None:
    async def scenario() -> tuple[list[int], list[list[str]], list[bool]]:
        stop_event = asyncio.Event()

        class _Database:
            def __init__(self) -> None:
                self.batches: list[list[str]] = []
                self.completions: list[bool] = []

            async def run_business(self, operation_name, _function, /, *args, **_kwargs):
                if operation_name == "opennews_recovery_start":
                    return None
                if operation_name == "opennews_recovery_publish":
                    ids = [event.provider_record_id for event in args[0]]
                    self.batches.append(ids)
                    return {
                        "events_seen": len(ids),
                        "items_inserted": len(ids),
                        "items_updated": 0,
                        "metadata_updated": 0,
                        "rejected": 0,
                        "overlap_complete": len(self.batches) == 3,
                    }
                if operation_name == "opennews_recovery_complete":
                    self.completions.append(bool(args[2]))
                    stop_event.set()
                    return True
                raise AssertionError(operation_name)

        class _RestClient:
            def __init__(self) -> None:
                self.pages: list[int] = []

            def fetch_overlap_page(self, page):
                self.pages.append(page)
                return OpenNewsOverlapPage(
                    events=(_report(f"page-{page}"),),
                    is_last_page=False,
                )

        database = _Database()
        rest = _RestClient()
        acquisition = _acquisition(
            db=database,
            rest_client=rest,
            websocket_client=object(),
        )
        acquisition._opennews_recovery_requested.set()

        await asyncio.wait_for(acquisition._opennews_recovery_loop(stop_event), timeout=1.0)
        return rest.pages, database.batches, database.completions

    monkeypatch.setattr(news_runtime, "_OPENNEWS_RECOVERY_MIN_INTERVAL_SECONDS", 0.0)

    assert asyncio.run(scenario()) == (
        [1, 2, 3],
        [["page-1"], ["page-2"], ["page-3"]],
        [False],
    )


@pytest.mark.parametrize("event_count", [0, 1])
def test_opennews_short_page_finishes_recovery_without_fetching_more_pages(
    monkeypatch,
    event_count: int,
) -> None:
    async def scenario() -> tuple[list[int], list[bool], bool]:
        stop_event = asyncio.Event()

        class _Database:
            def __init__(self) -> None:
                self.completions: list[bool] = []

            async def run_business(self, operation_name, _function, /, *args, **_kwargs):
                if operation_name == "opennews_recovery_start":
                    return None
                if operation_name == "opennews_recovery_publish":
                    events = args[0]
                    return {
                        "events_seen": len(events),
                        "items_inserted": len(events),
                        "items_updated": 0,
                        "metadata_updated": 0,
                        "rejected": 0,
                        "overlap_complete": False,
                    }
                if operation_name == "opennews_recovery_complete":
                    self.completions.append(bool(args[2]))
                    stop_event.set()
                    return True
                raise AssertionError(operation_name)

        class _RestClient:
            def __init__(self) -> None:
                self.pages: list[int] = []

            def fetch_overlap_page(self, page):
                self.pages.append(page)
                return OpenNewsOverlapPage(
                    events=tuple(_report(f"page-{page}-item-{index}") for index in range(event_count)),
                    is_last_page=True,
                )

        database = _Database()
        rest = _RestClient()
        acquisition = _acquisition(
            db=database,
            rest_client=rest,
            websocket_client=object(),
        )
        acquisition._opennews_recovery_requested.set()

        await asyncio.wait_for(acquisition._opennews_recovery_loop(stop_event), timeout=1.0)
        return rest.pages, database.completions, acquisition._opennews_recovery_requested.is_set()

    monkeypatch.setattr(news_runtime, "_OPENNEWS_RECOVERY_MIN_INTERVAL_SECONDS", 0.0)

    assert asyncio.run(scenario()) == ([1], [False], False)


def test_opennews_window_exhaustion_does_not_self_schedule_another_search(monkeypatch) -> None:
    async def scenario() -> tuple[list[int], list[bool], bool]:
        stop_event = asyncio.Event()

        class _Database:
            def __init__(self) -> None:
                self.publish_calls = 0
                self.completions: list[bool] = []

            async def run_business(self, operation_name, _function, /, *args, **_kwargs):
                if operation_name == "opennews_recovery_start":
                    return None
                if operation_name == "opennews_recovery_publish":
                    self.publish_calls += 1
                    return {
                        "events_seen": 1,
                        "items_inserted": 1,
                        "items_updated": 0,
                        "metadata_updated": 0,
                        "rejected": 0,
                        "overlap_complete": False,
                    }
                if operation_name == "opennews_recovery_complete":
                    self.completions.append(bool(args[2]))
                    stop_event.set()
                    return True
                raise AssertionError(operation_name)

        class _RestClient:
            def __init__(self) -> None:
                self.pages: list[int] = []

            def fetch_overlap_page(self, page):
                self.pages.append(page)
                return OpenNewsOverlapPage(
                    events=(_report(f"attempt-{len(self.pages)}-page-{page}"),),
                    is_last_page=False,
                )

        database = _Database()
        rest = _RestClient()
        acquisition = _acquisition(
            db=database,
            rest_client=rest,
            websocket_client=object(),
        )
        acquisition._opennews_recovery_requested.set()

        await asyncio.wait_for(acquisition._opennews_recovery_loop(stop_event), timeout=1.0)
        return rest.pages, database.completions, acquisition._opennews_recovery_requested.is_set()

    monkeypatch.setattr(news_runtime, "_OPENNEWS_RECOVERY_MIN_INTERVAL_SECONDS", 0.0)

    assert asyncio.run(scenario()) == ([*range(1, 12)], [True], False)


def test_opennews_recovery_failure_stays_durable_without_a_status_clear(monkeypatch) -> None:
    async def scenario() -> list[tuple[str, str | None]]:
        stop_event = asyncio.Event()

        class _Database:
            def __init__(self) -> None:
                self.operations: list[tuple[str, str | None]] = []

            async def run_business(self, operation_name, _function, /, *args, **_kwargs):
                code = args[2].code if operation_name == "opennews_recovery_failure" else None
                self.operations.append((operation_name, code))
                if operation_name == "opennews_recovery_failure":
                    stop_event.set()

        class _OverrunFinite:
            async def run(self, *_args, **_kwargs):
                raise ResourceOperationOverrun("resource_operation_overrun:opennews_rest_recovery")

        class _RestClient:
            def fetch_overlap_page(self, _page):
                raise AssertionError("the bounded executor owns this call")

        database = _Database()
        acquisition = _acquisition(
            db=database,
            finite_operations=_OverrunFinite(),
            rest_client=_RestClient(),
            websocket_client=object(),
        )
        acquisition._opennews_recovery_requested.set()

        await asyncio.wait_for(acquisition._opennews_recovery_loop(stop_event), timeout=1.0)
        return database.operations

    monkeypatch.setattr(news_runtime, "_OPENNEWS_RECOVERY_MIN_INTERVAL_SECONDS", 0.0)

    assert asyncio.run(scenario()) == [
        ("opennews_recovery_start", None),
        ("opennews_recovery_failure", "opennews_rest_timeout"),
    ]


def test_opennews_live_publish_does_not_clear_the_overlap_outcome() -> None:
    class _Database:
        def __init__(self) -> None:
            self.operations: list[str] = []

        async def run_business(self, operation_name, _function, /, *_args, **_kwargs):
            self.operations.append(operation_name)
            return {"items_inserted": 1}

    async def scenario() -> tuple[list[str], bool]:
        database = _Database()
        acquisition = _acquisition(db=database)
        acquisition._opennews_queue.put_nowait(_report("live-1"))
        stop_event = asyncio.Event()
        stop_event.set()
        await acquisition._opennews_publish_loop(stop_event)
        return database.operations, acquisition._opennews_recovery_requested.is_set()

    assert asyncio.run(scenario()) == (["opennews_live_publish"], False)


def test_recovery_has_a_five_minute_persisted_cooldown() -> None:
    five_minutes_ms = 5 * 60 * 1_000

    assert news_runtime._opennews_recovery_delay_seconds(
        last_attempt_at_ms=1_000,
        now_ms=1_001,
    ) == pytest.approx((five_minutes_ms - 1) / 1_000)
    assert (
        news_runtime._opennews_recovery_delay_seconds(
            last_attempt_at_ms=1_000,
            now_ms=1_000 + five_minutes_ms,
        )
        == 0.0
    )


def test_news_acquisition_has_no_gap_state_machine() -> None:
    acquisition = _acquisition(db=object())

    assert not any(name.startswith("_opennews_gap") for name in vars(acquisition))
    assert not hasattr(news_runtime, "_opennews_recovery_covers_boundary")


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
