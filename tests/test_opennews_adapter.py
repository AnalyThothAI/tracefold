from __future__ import annotations

import asyncio
import time

import pytest

from tracefold.news import (
    NewsAcquisition,
    NewsSourceDefinition,
    OpenNewsExpectedError,
    parse_opennews_message,
    parse_opennews_rest_response,
)
from tracefold.news import runtime as news_runtime
from tracefold.news.sources import OPENNEWS_SOURCE_ID, opennews_source
from tracefold.platform.config.settings import NewsSettings


def test_opennews_source_is_the_production_source() -> None:
    source = opennews_source()

    assert source.source_id == OPENNEWS_SOURCE_ID
    assert source.source_kind == "opennews"
    assert source.memberships == ("opennews",)
    assert source.feed_url == "https://ai.6551.io/open/news_search"


def test_rss_source_definition_remains_a_dormant_adapter_utility() -> None:
    source = NewsSourceDefinition(
        source_id="rss",
        name="RSS",
        feed_url="https://example.com/rss",
        tier=2,
        memberships=("finance",),
    )

    assert source.source_kind == "rss"


def test_opennews_token_is_trimmed_and_optional() -> None:
    assert NewsSettings(opennews_token="  secret  ").opennews_token == "secret"
    assert NewsSettings(opennews_token="  ").opennews_token is None


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
    assert event.entry.raw == {}


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
            "params": {"id": "wire-1", "aiRating": {"score": 90}},
        }
    )

    assert translation is not None and translation.observation_kind == "translation"
    assert translation.entry is None
    assert annotation is not None and annotation.observation_kind == "provider_annotation"
    assert annotation.entry is None
    assert annotation.provider_metadata == {"score": 90}


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

    events = parse_opennews_rest_response({"success": True, "data": rows})

    assert len(events) == 100
    assert events[0].provider_record_id == "wire-0"


def test_invalid_rest_shape_fails_closed() -> None:
    with pytest.raises(OpenNewsExpectedError, match="opennews_rest_payload_invalid"):
        parse_opennews_rest_response({"data": "not-a-list"})


def test_recovery_is_event_driven_and_keeps_a_three_hour_disconnect_gap(monkeypatch) -> None:
    class _Database:
        def __init__(self) -> None:
            self.recovery_batches: list[tuple] = []

        async def run_business(self, operation_name, _function, /, *args, **_kwargs):
            if operation_name == "opennews_recovery_publish":
                self.recovery_batches.append(tuple(args[0]))
            return {}

    class _FiniteOperations:
        async def run(self, _operation_name, function, /, *args, **_kwargs):
            return await function(*args)

    class _RestClient:
        def __init__(self, event) -> None:
            self.event = event
            self.calls = 0

        async def fetch_latest(self):
            self.calls += 1
            return (self.event,)

    class _WebSocketClient:
        def __init__(self) -> None:
            self.connect_calls = 0
            self.disconnect = asyncio.Event()

        async def connect(self) -> None:
            self.connect_calls += 1

        async def receive(self):
            if self.connect_calls == 1:
                await self.disconnect.wait()
                raise OpenNewsExpectedError("opennews_websocket_disconnected")
            await asyncio.Future()

        async def close(self) -> None:
            return None

    async def wait_until(predicate) -> None:
        deadline = asyncio.get_running_loop().time() + 2.0
        while not predicate():
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError("OpenNews runtime condition timed out")
            await asyncio.sleep(0.001)

    async def scenario() -> tuple[int, int, list[tuple]]:
        published_at_ms = int(time.time() * 1_000) - (3 * 60 * 60 * 1_000)
        recovered = parse_opennews_message(
            {
                "method": "news.update",
                "params": {
                    "id": "three-hour-gap",
                    "text": "Recovery must keep a three-hour-old report",
                    "newsType": "Reuters",
                    "engineType": "news",
                    "ts": published_at_ms,
                },
            }
        )
        assert recovered is not None
        database = _Database()
        rest = _RestClient(recovered)
        websocket = _WebSocketClient()
        acquisition = NewsAcquisition(
            db=database,
            finite_operations=_FiniteOperations(),
            sources=(opennews_source(),),
            opennews_rest_client=rest,
            opennews_ws_client=websocket,
        )
        stop_event = asyncio.Event()
        task = asyncio.create_task(acquisition.run_opennews(stop_event=stop_event))
        try:
            await wait_until(lambda: rest.calls == 1 and websocket.connect_calls == 1)
            await asyncio.sleep(0.02)
            assert rest.calls == 1
            websocket.disconnect.set()
            await wait_until(lambda: rest.calls == 2 and websocket.connect_calls == 2)
            await asyncio.sleep(0.02)
            assert rest.calls == 2
        finally:
            stop_event.set()
            await asyncio.wait_for(task, timeout=2.0)
        return rest.calls, websocket.connect_calls, database.recovery_batches

    monkeypatch.setattr(news_runtime, "_OPENNEWS_RECONNECT_SECONDS", 0.001)
    rest_calls, connect_calls, batches = asyncio.run(scenario())

    assert rest_calls == 2
    assert connect_calls == 2
    assert [batch[0].provider_record_id for batch in batches] == ["three-hour-gap", "three-hour-gap"]
