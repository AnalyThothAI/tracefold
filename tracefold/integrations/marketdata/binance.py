"""Bounded Binance public data client with process-level connections and bar reuse."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

from tracefold.trading.engine.marketdata import MarketDataRequest, MarketDataResult

_FUTURES = "https://fapi.binance.com"
_DEMO_FUTURES = "https://demo-fapi.binance.com"
_SPOT = "https://api.binance.com"
_BAR_INTERVALS = {60_000: "1m", 300_000: "5m"}


def _futures_base(environment: str) -> str:
    if environment == "live":
        return _FUTURES
    if environment == "demo":
        return _DEMO_FUTURES
    raise ValueError("market_data_environment_unsupported")


class BinanceMarketData:
    """One Analysis-process adapter. A cancelled waiter does not cancel a shared fetch."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        max_cached_rows: int = 50_000,
        max_connections: int = 8,
        weight_soft_limit_1m: int = 1_800,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if max_cached_rows <= 0 or max_connections <= 0 or weight_soft_limit_1m <= 0:
            raise ValueError("market_data_budget_invalid")
        self._client = client or httpx.AsyncClient(
            timeout=5.0,
            limits=httpx.Limits(max_connections=max_connections, max_keepalive_connections=max_connections),
        )
        self._owns_client = client is None
        self._slots = asyncio.Semaphore(max_connections)
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._cache: OrderedDict[tuple[str, ...], dict[int, dict[str, Any]]] = OrderedDict()
        self._inflight: dict[
            tuple[str, ...], asyncio.Task[tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]]
        ] = {}
        self._max_cached_rows = max_cached_rows
        self._weight_soft_limit_1m = weight_soft_limit_1m
        self._backoff_until = 0.0
        self._server_clocks: dict[str, tuple[int, int, int]] = {}  # offset, half RTT, sampled local ms
        self._clock_lock = asyncio.Lock()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def fetch(self, request: MarketDataRequest) -> MarketDataResult:
        if request.venue != "binance.usdm" or request.source_identity != "binance_public_v1":
            raise ValueError("market_data_source_mismatch")
        _futures_base(request.environment)
        if request.dataset == "spot_bars" and request.product != "spot":
            raise ValueError("market_data_product_mismatch")
        if request.dataset != "spot_bars" and request.product != "perpetual":
            raise ValueError("market_data_product_mismatch")
        if request.dataset in ("perp_bars", "spot_bars", "market_bars", "mark_bars"):
            return await self._bars(request)
        if request.dataset == "funding_history":
            return await self._funding_history(request)
        return await self._latest(request)

    def _result(
        self,
        request: MarketDataRequest,
        *,
        status: str,
        rows: tuple[dict[str, Any], ...],
        missing: tuple[str, ...] = (),
        receipts: tuple[dict[str, Any], ...] = (),
    ) -> MarketDataResult:
        times = [int(row["event_at_ms"]) for row in rows]
        received = [int(row["received_at_ms"]) for row in rows]
        return MarketDataResult(
            status=status,
            payload=rows,
            schema_version="binance_market_v1",
            source_version="binance_public_v1",
            unit_definition=request.unit_definition,
            source_identity=request.source_identity,
            event_start_ms=min(times) if times else None,
            event_end_ms=max(times) if times else None,
            received_at_ms=max(received) if received else None,
            missing_reasons=missing,
            request_receipts=receipts,
        )

    async def _bars(self, request: MarketDataRequest) -> MarketDataResult:
        if request.start_ms is None or request.end_ms is None or request.interval_ms is None:
            raise ValueError("market_data_window_incomplete")
        if request.interval_ms not in _BAR_INTERVALS:
            raise ValueError("market_data_interval_unsupported")
        key = (
            "binance",
            request.venue,
            request.environment,
            request.product,
            request.native_symbol,
            request.dataset,
            str(request.interval_ms),
            request.unit_definition,
            "binance_public_v1",
        )
        receipts: list[dict[str, Any]] = []
        try:
            while True:
                cached = self._cache.get(key, {})
                wanted = range(request.start_ms, request.end_ms, request.interval_ms)
                missing = [stamp for stamp in wanted if stamp not in cached]
                if not missing:
                    self._cache.move_to_end(key)
                    rows = tuple(cached[stamp] for stamp in wanted)
                    if not receipts:
                        receipts.append(
                            {
                                "endpoint": "cache",
                                "native_symbol": request.native_symbol,
                                "http_status": None,
                                "latency_ms": 0,
                                "request_weight": 0,
                                "cache_hit": True,
                            }
                        )
                    return self._result(request, status="ok", rows=rows, receipts=tuple(receipts))
                if time.monotonic() >= request.deadline_at_monotonic:
                    break
                task = self._inflight.get(key)
                if task is None:
                    # Only fill the first contiguous gap. A hot asset therefore
                    # requests the closed tail rather than the whole profile again.
                    start = missing[0]
                    end = start + request.interval_ms
                    while end in missing and end - start < 1000 * request.interval_ms:
                        end += request.interval_ms
                    task = asyncio.create_task(self._download_bars(request, start, end))
                    self._inflight[key] = task
                try:
                    new_rows, physical_receipts = await asyncio.wait_for(
                        asyncio.shield(task), timeout=max(0.01, request.deadline_at_monotonic - time.monotonic())
                    )
                finally:
                    if task.done() and self._inflight.get(key) is task:
                        self._inflight.pop(key)
                if not new_rows:
                    receipts.extend(physical_receipts)
                    break
                receipts.extend(physical_receipts)
                bucket = self._cache.setdefault(key, {})
                bucket.update({int(row["open_at_ms"]): row for row in new_rows})
                self._cache.move_to_end(key)
                self._evict()
        except (httpx.HTTPError, TimeoutError) as exc:
            receipts.append(
                {
                    "endpoint": "bar_wait",
                    "native_symbol": request.native_symbol,
                    "http_status": None,
                    "latency_ms": 0,
                    "error": type(exc).__name__,
                    "cache_hit": False,
                }
            )
        cached = self._cache.get(key, {})
        rows = tuple(
            cached[stamp] for stamp in range(request.start_ms, request.end_ms, request.interval_ms) if stamp in cached
        )
        no_spot_market = request.dataset == "spot_bars" and any(
            receipt.get("exchange_error_code") == -1121 for receipt in receipts
        )
        status = "not_applicable" if no_spot_market else "partial" if rows else "missing"
        return self._result(
            request,
            status=status,
            rows=rows,
            missing=("spot_market_unlisted",) if no_spot_market else ("coverage_incomplete",),
            receipts=tuple(receipts),
        )

    async def _download_bars(
        self,
        request: MarketDataRequest,
        start: int,
        end: int,
    ) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
        if request.interval_ms is None:
            raise ValueError("market_data_interval_unsupported")
        spot = request.dataset == "spot_bars"
        base = _SPOT if spot else _futures_base(request.environment)
        path = (
            "/api/v3/klines"
            if spot
            else "/fapi/v1/markPriceKlines"
            if request.dataset == "mark_bars"
            else "/fapi/v1/klines"
        )
        receipts: list[dict[str, Any]] = []
        try:
            response = await self._get(
                base + path,
                params={
                    "symbol": request.native_symbol,
                    "interval": _BAR_INTERVALS[request.interval_ms],
                    "startTime": start,
                    "endTime": end - 1,
                    "limit": 1000,
                },
                receipts=receipts,
            )
        except (httpx.HTTPError, TimeoutError):
            return (), tuple(receipts)
        received = self._clock_ms()
        rows: list[dict[str, Any]] = []
        for raw in response:
            open_at = int(raw[0])
            close_at = int(raw[6]) + 1
            if not start <= open_at < end or close_at > received:
                continue
            rows.append(
                {
                    "open_at_ms": open_at,
                    "event_at_ms": close_at,
                    "received_at_ms": received,
                    "open": str(raw[1]),
                    "high": str(raw[2]),
                    "low": str(raw[3]),
                    "close": str(raw[4]),
                    "base_volume": str(raw[5]),
                    "quote_volume": str(raw[7]),
                    "taker_buy_quote_volume": str(raw[10]),
                }
            )
        return tuple(rows), tuple(receipts)

    async def _latest(self, request: MarketDataRequest) -> MarketDataResult:
        receipts: list[dict[str, Any]] = []
        if request.dataset == "open_interest":
            path = "/fapi/v1/openInterest"
        elif request.dataset == "funding_basis":
            path = "/fapi/v1/premiumIndex"
        elif request.dataset == "book_ticker":
            path = "/fapi/v1/ticker/bookTicker"
        else:
            raise ValueError("market_data_dataset_unsupported")
        remaining = request.deadline_at_monotonic - time.monotonic()
        if remaining <= 0:
            return self._result(request, status="missing", rows=(), missing=("deadline_exceeded",))
        try:
            offset_ms, uncertainty_ms, sampled_at_ms = await asyncio.wait_for(
                self._calibrated_server_clock(request.environment, receipts), timeout=remaining
            )
            raw = await asyncio.wait_for(
                self._get(
                    _futures_base(request.environment) + path,
                    params={"symbol": request.native_symbol},
                    receipts=receipts,
                ),
                timeout=max(0.01, request.deadline_at_monotonic - time.monotonic()),
            )
        except ValueError:
            return self._result(
                request,
                status="error",
                rows=(),
                missing=("server_clock_unavailable",),
                receipts=tuple(receipts),
            )
        except (httpx.HTTPError, TimeoutError):
            reason = "deadline_exceeded" if time.monotonic() >= request.deadline_at_monotonic else "provider_error"
            return self._result(
                request,
                status="missing" if reason == "deadline_exceeded" else "error",
                rows=(),
                missing=(reason,),
                receipts=tuple(receipts),
            )
        received = self._clock_ms()
        if request.dataset == "book_ticker":
            if (
                not isinstance(raw, dict)
                or raw.get("bidPrice") is None
                or raw.get("askPrice") is None
                or raw.get("bidQty") is None
                or raw.get("askQty") is None
            ):
                return self._result(
                    request,
                    status="error",
                    rows=(),
                    missing=("quote_missing",),
                    receipts=tuple(receipts),
                )
            return self._result(
                request,
                status="ok",
                rows=(
                    {
                        "event_at_ms": received,
                        "received_at_ms": received,
                        "bid": str(raw["bidPrice"]),
                        "ask": str(raw["askPrice"]),
                        "bid_quantity": str(raw["bidQty"]),
                        "ask_quantity": str(raw["askQty"]),
                        "source_time_kind": "local_response_receipt",
                    },
                ),
                receipts=tuple(receipts),
            )
        if not isinstance(raw, dict) or raw.get("time") is None:
            return self._result(
                request,
                status="error",
                rows=(),
                missing=("source_time_missing",),
                receipts=tuple(receipts),
            )
        event_at = int(raw["time"])
        if request.max_age_ms is None:
            raise ValueError("market_data_age_invalid")
        server_now = received + offset_ms
        age_ms = server_now - event_at
        # Small forward readings are accepted only within the measured clock
        # uncertainty (plus one second for endpoint publication jitter).
        future_bound_ms = uncertainty_ms + 1_000
        reason = (
            "source_clock_far_future"
            if age_ms < -future_bound_ms
            else "source_clock_expired"
            if age_ms > request.max_age_ms + uncertainty_ms
            else None
        )
        status = "ok" if reason is None else "stale"
        return self._result(
            request,
            status=status,
            rows=(
                {
                    "event_at_ms": event_at,
                    "received_at_ms": received,
                    "server_clock_offset_ms": offset_ms,
                    "server_clock_uncertainty_ms": uncertainty_ms,
                    "server_clock_rtt_ms": uncertainty_ms * 2,
                    "server_clock_sampled_at_ms": sampled_at_ms,
                    "source_age_ms": age_ms,
                    "source_clock_status": "small_future"
                    if age_ms < 0 and reason is None
                    else "current"
                    if reason is None
                    else reason,
                    "open_interest_quantity": str(raw["openInterest"]) if request.dataset == "open_interest" else None,
                    "mark_price": str(raw["markPrice"]) if request.dataset == "funding_basis" else None,
                    "index_price": str(raw["indexPrice"]) if request.dataset == "funding_basis" else None,
                    "last_funding_rate": str(raw["lastFundingRate"]) if request.dataset == "funding_basis" else None,
                    "next_funding_at_ms": int(raw["nextFundingTime"]) if request.dataset == "funding_basis" else None,
                },
            ),
            missing=(reason,) if reason is not None else (),
            receipts=tuple(receipts),
        )

    async def _funding_history(self, request: MarketDataRequest) -> MarketDataResult:
        if request.start_ms is None or request.end_ms is None:
            raise ValueError("market_data_window_incomplete")
        receipts: list[dict[str, Any]] = []
        remaining = request.deadline_at_monotonic - time.monotonic()
        if remaining <= 0:
            return self._result(request, status="missing", rows=(), missing=("deadline_exceeded",))
        try:
            raw = await asyncio.wait_for(
                self._get(
                    _futures_base(request.environment) + "/fapi/v1/fundingRate",
                    params={
                        "symbol": request.native_symbol,
                        "startTime": request.start_ms,
                        "endTime": request.end_ms,
                        "limit": 1000,
                    },
                    receipts=receipts,
                ),
                timeout=remaining,
            )
        except (httpx.HTTPError, TimeoutError):
            return self._result(
                request, status="error", rows=(), missing=("funding_history_unavailable",), receipts=tuple(receipts)
            )
        if not isinstance(raw, list):
            return self._result(
                request, status="error", rows=(), missing=("funding_history_invalid",), receipts=tuple(receipts)
            )
        received = self._clock_ms()
        rows = tuple(
            {
                "event_at_ms": int(item["fundingTime"]),
                "funding_at_ms": int(item["fundingTime"]),
                "funding_rate": str(item["fundingRate"]),
                "received_at_ms": received,
            }
            for item in raw
        )
        status = "partial" if len(rows) >= 1000 else "ok"
        return self._result(
            request,
            status=status,
            rows=rows,
            missing=("funding_history_page_limit",) if status == "partial" else (),
            receipts=tuple(receipts),
        )

    async def _calibrated_server_clock(
        self,
        environment: str,
        receipts: list[dict[str, Any]],
    ) -> tuple[int, int, int]:
        sampled = self._server_clocks.get(environment)
        if sampled is not None and 0 <= self._clock_ms() - sampled[2] < 60_000:
            return sampled
        async with self._clock_lock:
            sampled = self._server_clocks.get(environment)
            if sampled is not None and 0 <= self._clock_ms() - sampled[2] < 60_000:
                return sampled
            sent_at = self._clock_ms()
            raw = await self._get(_futures_base(environment) + "/fapi/v1/time", params={}, receipts=receipts)
            received_at = self._clock_ms()
            if not isinstance(raw, dict) or raw.get("serverTime") is None:
                raise ValueError("server_clock_unavailable")
            rtt_ms = max(0, received_at - sent_at)
            offset_ms = int(raw["serverTime"]) - (sent_at + rtt_ms // 2)
            self._server_clocks[environment] = (offset_ms, rtt_ms // 2, received_at)
            return self._server_clocks[environment]

    async def _get(self, url: str, *, params: dict[str, Any], receipts: list[dict[str, Any]]) -> Any:
        async with self._slots:
            # Recheck after waiting for a slot: another request may have
            # received a rate-limit response while this one was queued.
            if time.monotonic() < self._backoff_until:
                receipts.append(
                    {
                        "endpoint": urlsplit(url).path,
                        "host": urlsplit(url).hostname,
                        "native_symbol": params.get("symbol"),
                        "http_status": None,
                        "latency_ms": 0,
                        "request_weight": 0,
                        "error": "market_data_backoff",
                        "cache_hit": False,
                    }
                )
                raise TimeoutError("market_data_backoff")
            started = time.monotonic()
            status: int | None = None
            used_weight: int | None = None
            exchange_error_code: int | None = None
            try:
                response = await self._client.get(url, params=params)
                status = response.status_code
                weight_header = response.headers.get("X-MBX-USED-WEIGHT-1M")
                used_weight = int(weight_header) if weight_header and weight_header.isdigit() else None
                if used_weight is not None and used_weight >= self._weight_soft_limit_1m:
                    self._backoff_until = max(self._backoff_until, time.monotonic() + 60)
                if status >= 400:
                    try:
                        with_error = response.json()
                        if isinstance(with_error, dict) and isinstance(with_error.get("code"), int):
                            exchange_error_code = with_error["code"]
                    except ValueError:
                        pass
                if status in (418, 429):
                    retry = response.headers.get("Retry-After")
                    seconds = min(60, max(1, int(retry))) if retry and retry.isdigit() else 10
                    self._backoff_until = time.monotonic() + seconds
                response.raise_for_status()
                return response.json()
            finally:
                receipts.append(
                    {
                        "endpoint": urlsplit(url).path,
                        "host": urlsplit(url).hostname,
                        "native_symbol": params.get("symbol"),
                        "params": {k: v for k, v in params.items() if k != "symbol"},
                        "http_status": status,
                        "latency_ms": round((time.monotonic() - started) * 1000),
                        "used_weight_1m": used_weight,
                        "exchange_error_code": exchange_error_code,
                        "cache_hit": False,
                    }
                )

    def _evict(self) -> None:
        count = sum(len(bucket) for bucket in self._cache.values())
        while count > self._max_cached_rows and self._cache:
            _, bucket = self._cache.popitem(last=False)
            count -= len(bucket)
