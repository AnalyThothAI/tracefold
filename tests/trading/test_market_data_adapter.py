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
    async def respond(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.03)
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
        assert future.status == "stale" and future.missing_reasons == ("stale_source_clock",)
