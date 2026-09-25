"""A shared physical fetch keeps each caller's coverage and request receipt honest."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace

import httpx

from tracefold.integrations.marketdata.binance import BinanceMarketData
from tracefold.trading.engine.marketdata import MarketDataRequest


def _request(*, start: int = 0, end: int = 120_000, dataset: str = "perp_bars") -> MarketDataRequest:
    return MarketDataRequest(
        dataset=dataset,
        native_symbol="SOLUSDT",
        venue="binance.usdm",
        environment="demo",
        product="spot" if dataset == "spot_bars" else "perpetual",
        source_identity="binance_public_v1",
        unit_definition="native_quote_v1",
        start_ms=start,
        end_ms=end,
        interval_ms=60_000,
        max_age_ms=None,
        deadline_at_monotonic=time.monotonic() + 2,
    )


def _bar(open_at: int) -> list[object]:
    return [open_at, "100", "101", "99", "100", "3", open_at + 59_999, "300", 1, "1", "100", "0"]


def test_instrument_rules_fetch_and_cache_are_symbol_scoped() -> None:
    asyncio.run(_instrument_rules_fetch_and_cache())


async def _instrument_rules_fetch_and_cache() -> None:
    calls = 0

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.path == "/fapi/v1/exchangeInfo"
        return httpx.Response(
            200,
            json={
                "symbols": [
                    {
                        "symbol": "SOLUSDT",
                        "contractType": "PERPETUAL",
                        "status": "TRADING",
                        "baseAsset": "SOL",
                        "quoteAsset": "USDT",
                        "marginAsset": "USDT",
                        "filters": [
                            {"filterType": "PRICE_FILTER", "tickSize": "0.001"},
                            {"filterType": "MARKET_LOT_SIZE", "minQty": "0.01", "maxQty": "100", "stepSize": "0.01"},
                            {"filterType": "MIN_NOTIONAL", "notional": "5"},
                        ],
                    }
                ]
            },
        )

    request = replace(
        _request(dataset="perp_bars"),
        dataset="instrument_rules",
        start_ms=None,
        end_ms=None,
        interval_ms=None,
        max_age_ms=3_600_000,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = BinanceMarketData(client=client, clock_ms=lambda: 300_000)
        first = await adapter.fetch(request)
        assert first.status == "ok"
        assert first.payload[0]["price_tick_size"] == "0.001"
        assert first.payload[0]["minimum_notional"] == "5"
        assert first.received_at_ms == 300_000
        second = await adapter.fetch(request)
        assert second.status == "ok"
        assert second.request_receipts[0]["cache_hit"] is True
        assert calls == 1
        unlisted = await adapter.fetch(replace(request, native_symbol="MISSINGUSDT"))
        assert unlisted.status == "missing"
        assert unlisted.missing_reasons == ("instrument_unlisted",)
        assert calls == 1


def test_overlap_shares_request_tail_refills_and_cache_receipt() -> None:
    asyncio.run(_overlap_shares_request_tail_refills_and_cache_receipt())


async def _overlap_shares_request_tail_refills_and_cache_receipt() -> None:
    calls: list[tuple[int, int]] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params["startTime"])
        end = int(request.url.params["endTime"])
        calls.append((start, end))
        await asyncio.sleep(0.02)
        return httpx.Response(200, json=[_bar(stamp) for stamp in range(start, end + 1, 60_000)])

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = BinanceMarketData(client=client, clock_ms=lambda: 300_000)
        first, second = await asyncio.gather(adapter.fetch(_request()), adapter.fetch(_request()))
        assert len(calls) == 1
        assert first.status == second.status == "ok"
        assert len(first.request_receipts) == len(second.request_receipts) == 1
        assert not first.request_receipts[0]["cache_hit"]
        warm = await adapter.fetch(_request())
        assert len(calls) == 1
        assert warm.request_receipts[0]["cache_hit"]
        tail = await adapter.fetch(_request(end=180_000))
        assert tail.status == "ok"
        assert calls == [(0, 119_999), (120_000, 179_999)]


def test_unlisted_spot_is_distinct_from_provider_failure() -> None:
    asyncio.run(_unlisted_spot_is_distinct_from_provider_failure())


async def _unlisted_spot_is_distinct_from_provider_failure() -> None:
    async def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"code": -1121, "msg": "Invalid symbol."})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = BinanceMarketData(client=client)
        result = await adapter.fetch(_request(dataset="spot_bars"))
        assert result.status == "not_applicable"
        assert result.missing_reasons == ("spot_market_unlisted",)
        assert result.request_receipts[0]["exchange_error_code"] == -1121


def test_cancelling_one_waiter_keeps_shared_fetch_alive() -> None:
    asyncio.run(_cancel_one_waiter())


async def _cancel_one_waiter() -> None:
    calls = 0
    started = asyncio.Event()

    async def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        started.set()
        await asyncio.sleep(0.03)
        return httpx.Response(200, json=[_bar(0), _bar(60_000)])

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = BinanceMarketData(client=client, clock_ms=lambda: 300_000)
        cancelled = asyncio.create_task(adapter.fetch(_request()))
        survivor = asyncio.create_task(adapter.fetch(_request()))
        await started.wait()
        cancelled.cancel()
        try:
            await cancelled
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("first waiter should be cancelled")
        assert (await survivor).status == "ok"
        assert calls == 1


def test_latest_obeys_deadline_and_rejects_future_provider_clock() -> None:
    asyncio.run(_latest_deadline_and_clock())


def test_rate_limit_starts_bounded_backoff_without_a_request_storm() -> None:
    asyncio.run(_rate_limit_backoff())


async def _rate_limit_backoff() -> None:
    calls = 0

    async def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "1"}, json={"code": -1003, "msg": "rate limit"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = BinanceMarketData(client=client, clock_ms=lambda: 300_000)
        first = await adapter.fetch(_request())
        second = await adapter.fetch(_request())
        assert first.status == second.status == "missing"
        assert calls == 1
        assert first.request_receipts[0]["http_status"] == 429
        assert first.request_receipts[0]["exchange_error_code"] == -1003
        assert second.request_receipts[0]["error"] == "market_data_backoff"


def test_reported_weight_soft_limit_stops_the_next_physical_fetch() -> None:
    asyncio.run(_weight_soft_limit())


async def _weight_soft_limit() -> None:
    calls = 0

    async def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, headers={"X-MBX-USED-WEIGHT-1M": "1800"}, json=[_bar(0), _bar(60_000)])

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = BinanceMarketData(client=client, clock_ms=lambda: 300_000)
        first = await adapter.fetch(_request())
        assert first.status == "ok"
        assert first.request_receipts[0]["used_weight_1m"] == 1800
        other = await adapter.fetch(_request(start=120_000, end=180_000))
        assert other.status == "missing"
        assert calls == 1


async def _latest_deadline_and_clock() -> None:
    async def respond(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.03)
        if request.url.path == "/fapi/v1/time":
            return httpx.Response(200, json={"serverTime": 300_000})
        return httpx.Response(200, json={"openInterest": "100", "time": 500_000})

    request = MarketDataRequest(
        dataset="open_interest",
        native_symbol="SOLUSDT",
        venue="binance.usdm",
        environment="demo",
        product="perpetual",
        source_identity="binance_public_v1",
        unit_definition="native_quantity_v1",
        start_ms=None,
        end_ms=None,
        interval_ms=None,
        max_age_ms=90_000,
        deadline_at_monotonic=time.monotonic() + 0.01,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = BinanceMarketData(client=client, clock_ms=lambda: 300_000)
        timed = await adapter.fetch(request)
        assert timed.status == "missing" and timed.missing_reasons == ("deadline_exceeded",)
        future = await adapter.fetch(
            replace(
                request,
                deadline_at_monotonic=time.monotonic() + 1,
            )
        )
        assert future.status == "stale" and future.missing_reasons == ("source_clock_far_future",)


def test_latest_clock_distinguishes_small_future_from_expired() -> None:
    asyncio.run(_latest_clock_boundaries())


async def _latest_clock_boundaries() -> None:
    event_at = 300_500

    async def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fapi/v1/time":
            return httpx.Response(200, json={"serverTime": 300_000})
        return httpx.Response(200, json={"openInterest": "100", "time": event_at})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = BinanceMarketData(client=client, clock_ms=lambda: 300_000)
        request = MarketDataRequest(
            dataset="open_interest",
            native_symbol="SOLUSDT",
            venue="binance.usdm",
            environment="demo",
            product="perpetual",
            source_identity="binance_public_v1",
            unit_definition="native_contract_quantity_v1",
            start_ms=None,
            end_ms=None,
            interval_ms=None,
            max_age_ms=90_000,
            deadline_at_monotonic=time.monotonic() + 2,
        )
        small_future = await adapter.fetch(request)
        assert small_future.status == "ok"
        assert small_future.payload[0]["source_clock_status"] == "small_future"
        event_at = 100_000
        expired = await adapter.fetch(request)
        assert expired.status == "stale"
        assert expired.missing_reasons == ("source_clock_expired",)


def test_shadow_quote_mark_and_funding_sources_are_distinct() -> None:
    asyncio.run(_shadow_market_sources())


def test_open_interest_history_preserves_quantity_value_and_period() -> None:
    asyncio.run(_open_interest_history())


async def _open_interest_history() -> None:
    seen: list[httpx.Request] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json=[
                {"symbol": "SOLUSDT", "sumOpenInterest": "100", "sumOpenInterestValue": "10000", "timestamp": 300_000},
                {"symbol": "SOLUSDT", "sumOpenInterest": "102", "sumOpenInterestValue": "10200", "timestamp": 600_000},
            ],
        )

    request = MarketDataRequest(
        dataset="open_interest_history",
        native_symbol="SOLUSDT",
        venue="binance.usdm",
        environment="live",
        product="perpetual",
        source_identity="binance_public_v1",
        unit_definition="base_quantity_and_quote_value_v1",
        start_ms=300_000,
        end_ms=600_000,
        interval_ms=300_000,
        max_age_ms=None,
        deadline_at_monotonic=time.monotonic() + 2,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = BinanceMarketData(client=client, clock_ms=lambda: 700_000)
        result = await adapter.fetch(request)
        assert result.status == "ok"
        assert result.payload[0]["sum_open_interest_quantity"] == "100"
        assert result.payload[-1]["sum_open_interest_value"] == "10200"
        assert result.event_start_ms == 300_000 and result.event_end_ms == 600_000
    assert seen[0].url.host == "fapi.binance.com"
    assert seen[0].url.path == "/futures/data/openInterestHist"
    assert seen[0].url.params["period"] == "5m"
    assert seen[0].url.params["symbol"] == "SOLUSDT"


async def _shadow_market_sources() -> None:
    paths: list[str] = []
    hosts: set[str | None] = set()

    async def respond(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        hosts.add(request.url.host)
        if request.url.path == "/fapi/v1/time":
            return httpx.Response(200, json={"serverTime": 300_000})
        if request.url.path == "/fapi/v1/ticker/bookTicker":
            return httpx.Response(
                200,
                json={"symbol": "SOLUSDT", "bidPrice": "99", "askPrice": "101", "bidQty": "3", "askQty": "4"},
            )
        if request.url.path == "/fapi/v1/markPriceKlines":
            return httpx.Response(200, json=[_bar(0), _bar(60_000)])
        if request.url.path == "/fapi/v1/fundingRate":
            return httpx.Response(
                200, json=[{"fundingTime": 60_000, "fundingRate": "0.0001", "markPrice": "100", "rateType": "Regular"}]
            )
        raise AssertionError(request.url.path)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = BinanceMarketData(client=client, clock_ms=lambda: 300_000)
        quote = MarketDataRequest(
            dataset="book_ticker",
            native_symbol="SOLUSDT",
            venue="binance.usdm",
            environment="demo",
            product="perpetual",
            source_identity="binance_public_v1",
            unit_definition="bid_ask_quote_and_base_size_v2",
            start_ms=None,
            end_ms=None,
            interval_ms=None,
            max_age_ms=5_000,
            deadline_at_monotonic=time.monotonic() + 2,
        )
        mark = replace(_request(), dataset="mark_bars", unit_definition="mark_quote_per_base_v1")
        funding = MarketDataRequest(
            dataset="funding_history",
            native_symbol="SOLUSDT",
            venue="binance.usdm",
            environment="demo",
            product="perpetual",
            source_identity="binance_public_v1",
            unit_definition="funding_rate_and_mark_price_v2",
            start_ms=0,
            end_ms=120_000,
            interval_ms=None,
            max_age_ms=None,
            deadline_at_monotonic=time.monotonic() + 2,
        )
        quote_result, mark_result, funding_result = await asyncio.gather(
            adapter.fetch(quote), adapter.fetch(mark), adapter.fetch(funding)
        )
        assert quote_result.payload[0]["bid"] == "99"
        assert quote_result.payload[0]["ask_quantity"] == "4"
        assert mark_result.status == "ok"
        assert funding_result.payload[0]["funding_rate"] == "0.0001"
        assert funding_result.payload[0]["mark_price"] == "100"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json=[{"fundingTime": 60_000, "fundingRate": "0.0001"}])
        )
    ) as client:
        missing_mark = await BinanceMarketData(client=client, clock_ms=lambda: 300_000).fetch(
            replace(funding, deadline_at_monotonic=time.monotonic() + 2)
        )
        assert missing_mark.status == "error"
        assert "funding_history_invalid" in missing_mark.missing_reasons
    assert "/fapi/v1/ticker/bookTicker" in paths
    assert "/fapi/v1/markPriceKlines" in paths
    assert "/fapi/v1/fundingRate" in paths
    assert hosts == {"demo-fapi.binance.com"}
