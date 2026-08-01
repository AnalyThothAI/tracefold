from __future__ import annotations

import asyncio
import time

import pytest

from tracefold.integrations.opennews import client as opennews_client
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
from tracefold.platform.resource import ResourceAdmissionTimeout


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
            self.gap_version = 0

        async def run_business(self, operation_name, _function, /, *args, **_kwargs):
            if operation_name == "opennews_recovery_publish":
                self.recovery_batches.append(tuple(args[0]))
            if operation_name == "opennews_status":
                gap_unclosed = bool(args[4])
                if gap_unclosed:
                    self.gap_version += 1
                    return args[5], self.gap_version
                if args[6] != self.gap_version:
                    return None
                return None, self.gap_version
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
    monkeypatch.setattr(news_runtime, "_OPENNEWS_RECOVERY_MIN_INTERVAL_SECONDS", 0.0)
    rest_calls, connect_calls, batches = asyncio.run(scenario())

    assert rest_calls == 2
    assert connect_calls == 2
    assert [batch[0].provider_record_id for batch in batches] == ["three-hour-gap", "three-hour-gap"]


def test_recovery_has_a_five_minute_persisted_cooldown() -> None:
    five_minutes_ms = 5 * 60 * 1_000

    assert news_runtime._opennews_recovery_delay_seconds(
        last_attempt_at_ms=1_000,
        now_ms=1_001,
    ) == pytest.approx((five_minutes_ms - 1) / 1_000)
    assert news_runtime._opennews_recovery_delay_seconds(
        last_attempt_at_ms=1_000,
        now_ms=1_000 + five_minutes_ms,
    ) == 0.0


def test_recovery_only_closes_a_gap_when_the_page_contains_its_provider_boundary() -> None:
    def report(provider_record_id: str):
        event = parse_opennews_message(
            {
                "method": "news.update",
                "params": {
                    "id": provider_record_id,
                    "text": provider_record_id,
                    "newsType": "Reuters",
                    "engineType": "news",
                    "ts": 1_000_000_012_000,
                },
            }
        )
        assert event is not None
        return event

    missing_boundary = tuple(report(f"burst-{index}") for index in range(100))
    covering = (report("gap-boundary"), *missing_boundary[:99])

    assert news_runtime._opennews_recovery_covers_boundary(
        covering,
        boundary_provider_record_id="gap-boundary",
    )
    assert not news_runtime._opennews_recovery_covers_boundary(
        missing_boundary,
        boundary_provider_record_id="gap-boundary",
    )
    assert news_runtime._opennews_recovery_covers_boundary(
        missing_boundary,
        boundary_provider_record_id=None,
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


def test_recovery_admission_timeout_keeps_the_gap_scheduled(monkeypatch) -> None:
    class _Database:
        async def run_business(self, operation_name, _function, /, *_args, **_kwargs):
            if operation_name == "opennews_status":
                raise ResourceAdmissionTimeout("test_opennews_status_admission_timeout")
            return {}

    class _FiniteOperations:
        def __init__(self) -> None:
            self.calls = 0

        async def run(self, _operation_name, function, /, *args, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise ResourceAdmissionTimeout("test_opennews_admission_timeout")
            return await function(*args)

    class _RestClient:
        def __init__(self, event, stop_event) -> None:
            self.event = event
            self.stop_event = stop_event
            self.calls = 0

        async def fetch_latest(self):
            self.calls += 1
            self.stop_event.set()
            return (self.event,)

    async def scenario() -> tuple[int, int, str | None]:
        gap_started_at_ms = int(time.time() * 1_000)
        recovered = parse_opennews_message(
            {
                "method": "news.update",
                "params": {
                    "id": "recovered-after-admission",
                    "text": "Recovery survives admission pressure",
                    "newsType": "Reuters",
                    "engineType": "news",
                    "ts": gap_started_at_ms - 1,
                },
            }
        )
        assert recovered is not None
        stop_event = asyncio.Event()
        finite = _FiniteOperations()
        rest = _RestClient(recovered, stop_event)
        acquisition = NewsAcquisition(
            db=_Database(),
            finite_operations=finite,
            sources=(opennews_source(),),
            opennews_rest_client=rest,
            opennews_ws_client=object(),
        )
        acquisition._opennews_gap_boundary_provider_record_id = recovered.provider_record_id
        acquisition._opennews_recovery_requested.set()

        await asyncio.wait_for(
            acquisition._opennews_recovery_loop(stop_event),
            timeout=1.0,
        )
        return (
            finite.calls,
            rest.calls,
            acquisition._opennews_gap_boundary_provider_record_id,
        )

    monkeypatch.setattr(news_runtime, "_OPENNEWS_RECOVERY_MIN_INTERVAL_SECONDS", 0.0)

    assert asyncio.run(scenario()) == (2, 1, "recovered-after-admission")


def test_late_gap_status_restores_memory_after_an_older_close() -> None:
    async def scenario() -> tuple[bool, bool, int, str | None]:
        status_started = asyncio.Event()
        release_status = asyncio.Event()

        class _Database:
            async def run_business(self, operation_name, _function, /, *_args, **_kwargs):
                assert operation_name == "opennews_status"
                status_started.set()
                await release_status.wait()
                return "gap-boundary", 2

        acquisition = NewsAcquisition(
            db=_Database(),
            finite_operations=object(),
            sources=(opennews_source(),),
        )
        acquisition._opennews_gap_version = 1
        acquisition._opennews_gap_boundary_provider_record_id = "gap-boundary"
        status_task = asyncio.create_task(
            acquisition._update_opennews_status(
                connected=True,
                error_code="opennews_buffer_overflow",
                gap_unclosed=True,
            )
        )
        await status_started.wait()

        acquisition._opennews_gap_unclosed = False
        acquisition._opennews_recovery_requested.clear()
        release_status.set()
        await status_task
        acquisition._opennews_recovery_requested.set()

        return (
            acquisition._opennews_gap_unclosed,
            acquisition._opennews_recovery_requested.is_set(),
            acquisition._opennews_gap_version,
            acquisition._opennews_gap_boundary_provider_record_id,
        )

    assert asyncio.run(scenario()) == (True, True, 2, "gap-boundary")
