"""Pure tests for the Price Review domain (#88): source selection, quote parsing, candle alignment, returns.

Everything here is the metric contract `reaction_v1` freezes. A test that fails is either a bug or a decision
to publish a new metric version — never a reason to quietly change what a stored v1 row means.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal

import httpx
import pytest

from tracefold.integrations.venues.candles import (
    fetch_binance_bars,
    fetch_binance_candles,
    fetch_hyperliquid_bars,
    fetch_hyperliquid_candles,
)
from tracefold.integrations.venues.errors import VenueExpectedError
from tracefold.integrations.venues.hyperliquid import fetch_hyperliquid_instruments
from tracefold.integrations.venues.quotes import (
    fetch_binance_futures_day_quotes,
    fetch_binance_futures_quotes,
    fetch_binance_spot_day_quotes,
    fetch_binance_spot_quotes,
    fetch_hyperliquid_quotes,
)
from tracefold.news.market_review.pricing import (
    CANDLE_INTERVAL_MS,
    HORIZON_MS,
    QUOTE_FRESH_MAX_AGE_MS,
    QUOTE_MAX_FUTURE_SKEW_MS,
    QUOTE_PERIOD_SECONDS,
    REACTION_METRIC_VERSION,
    Candle,
    PriceInstrument,
    coverage_pct,
    hit_pct,
    median_bps,
    parse_change_pct,
    parse_price,
    price_kind_for,
    quote_asset_rank,
    quote_asset_rank_sql,
    quote_freshness,
    reference_freshness,
    return_bps,
    select_candle,
    source_rank,
    source_rank_sql,
)

ANCHOR = 1_787_000_123_456  # deliberately not on a 5-minute boundary


def _floor(timestamp_ms: int) -> int:
    """The grid every target is measured against; the domain reads it off the candles themselves."""

    return (timestamp_ms // CANDLE_INTERVAL_MS) * CANDLE_INTERVAL_MS


def _candles(start_ms: int, closes: list[str]) -> list[Candle]:
    return [
        Candle(
            open_at_ms=start_ms + index * CANDLE_INTERVAL_MS,
            close_at_ms=start_ms + (index + 1) * CANDLE_INTERVAL_MS,
            close=Decimal(value),
        )
        for index, value in enumerate(closes)
    ]


# ---------------------------------------------------------------------------- source selection
def test_source_order_is_code_owned_and_deterministic() -> None:
    assert source_rank("binance.perp") < source_rank("binance.spot") < source_rank("hl.perp")
    assert source_rank("hl.spot") < source_rank("hl.xyz") < source_rank("hl.brandnewdex")
    assert quote_asset_rank("USDT") < quote_asset_rank("USDC") < quote_asset_rank("FDUSD")
    assert quote_asset_rank(None) == quote_asset_rank("DAI")


def test_rank_sql_is_generated_from_the_same_order_so_the_two_cannot_drift() -> None:
    """#88 §2: the chip, the Quote planner and the Reaction planner share one precedence, not three copies."""

    sql = source_rank_sql()
    assert "WHEN 'binance.perp' THEN 0" in sql and "WHEN 'hl.xyz' THEN 4" in sql
    assert sql.endswith("ELSE 5 END")
    assert "upper(i.quote_asset)" in quote_asset_rank_sql()


def test_price_kind_declares_what_the_number_actually_is() -> None:
    assert price_kind_for("binance.spot") == "last"
    assert price_kind_for("hl.perp") == "mid"
    assert PriceInstrument(venue="hl.spot", venue_symbol="@107", base_symbol="HYPE").quote_key == (
        "@107",
        "mid",
    )


# ---------------------------------------------------------------------------- quote values
def test_a_price_is_a_positive_finite_decimal_or_nothing() -> None:
    assert parse_price("68123.4") == Decimal("68123.4")
    assert parse_price(0) is None and parse_price("-1") is None
    assert parse_price("nan") is None and parse_price("inf") is None
    assert parse_price(None) is None and parse_price(True) is None


def test_day_change_is_derived_from_the_providers_own_previous_close() -> None:
    assert parse_change_pct(Decimal("110"), "100") == pytest.approx(10.0)
    assert parse_change_pct(Decimal("110"), None) is None
    assert parse_change_pct(None, "100") is None


def test_quote_freshness_uses_the_oldest_applicable_clock_at_read_time() -> None:
    measured = 100_000
    fresh = quote_freshness(measured_at_ms=measured, received_at_ms=60_000, source_at_ms=70_000)
    assert fresh.received_age_ms == 40_000
    assert fresh.source_age_ms == 30_000
    assert fresh.effective_age_ms == 40_000
    assert fresh.freshness_basis == "source_and_received"
    assert fresh.state == "fresh"

    source_stale = quote_freshness(measured_at_ms=measured, received_at_ms=99_000, source_at_ms=54_999)
    assert source_stale.state == "stale"
    assert source_stale.effective_age_ms == QUOTE_FRESH_MAX_AGE_MS + 1

    receipt_stale = quote_freshness(measured_at_ms=measured, received_at_ms=54_999, source_at_ms=99_000)
    assert receipt_stale.state == "stale"


def test_quote_freshness_missing_source_and_future_skew_boundaries_are_explicit() -> None:
    measured = 100_000
    received_only = quote_freshness(measured_at_ms=measured, received_at_ms=60_000, source_at_ms=None)
    assert received_only.freshness_basis == "received_only"
    assert received_only.source_age_ms is None
    assert received_only.state == "fresh"

    future_boundary = quote_freshness(
        measured_at_ms=measured,
        received_at_ms=measured + QUOTE_MAX_FUTURE_SKEW_MS,
        source_at_ms=measured + QUOTE_MAX_FUTURE_SKEW_MS,
    )
    assert future_boundary.state == "fresh"
    assert future_boundary.effective_age_ms == 0

    invalid_future = quote_freshness(
        measured_at_ms=measured,
        received_at_ms=measured,
        source_at_ms=measured + QUOTE_MAX_FUTURE_SKEW_MS + 1,
    )
    assert invalid_future.state == "stale"
    assert invalid_future.source_age_ms == 0  # exposed age is clamped; validity used the raw age


def test_current_and_reference_freshness_ceilings_are_the_frozen_304_contract() -> None:
    assert QUOTE_FRESH_MAX_AGE_MS == 45_000
    assert QUOTE_PERIOD_SECONDS == 20.0
    assert reference_freshness(measured_at_ms=360_000, reference_at_ms=0) == (360_000, True)
    assert reference_freshness(measured_at_ms=360_001, reference_at_ms=0) == (360_001, False)
    assert reference_freshness(
        measured_at_ms=0,
        reference_at_ms=QUOTE_MAX_FUTURE_SKEW_MS,
    ) == (0, True)
    assert reference_freshness(
        measured_at_ms=0,
        reference_at_ms=QUOTE_MAX_FUTURE_SKEW_MS + 1,
    ) == (0, False)
    assert reference_freshness(measured_at_ms=0, reference_at_ms=None) == (None, False)


# ---------------------------------------------------------------------------- candle alignment
def test_p0_is_the_last_closed_candle_at_or_before_the_anchor() -> None:
    floor = _floor(ANCHOR)
    bars = _candles(floor - 4 * CANDLE_INTERVAL_MS, ["10", "11", "12", "13"])
    picked = select_candle(bars, target_ms=ANCHOR)
    assert picked is not None
    assert picked.close_at_ms == floor
    assert picked.close_at_ms <= ANCHOR  # never a candle that closes after the news


def test_selected_endpoints_stay_exactly_one_horizon_apart() -> None:
    span = 5 * 3_600_000
    bars = _candles(_floor(ANCHOR) - CANDLE_INTERVAL_MS, ["1"] * (span // CANDLE_INTERVAL_MS))
    p0 = select_candle(bars, target_ms=ANCHOR)
    p1 = select_candle(bars, target_ms=ANCHOR + HORIZON_MS["1h"])
    p4 = select_candle(bars, target_ms=ANCHOR + HORIZON_MS["4h"])
    assert p0 and p1 and p4
    assert p1.close_at_ms - p0.close_at_ms == HORIZON_MS["1h"]
    assert p4.close_at_ms - p0.close_at_ms == HORIZON_MS["4h"]


def test_an_anchor_exactly_on_a_boundary_takes_the_candle_that_closed_on_it() -> None:
    anchor = _floor(ANCHOR)
    bars = _candles(anchor - 2 * CANDLE_INTERVAL_MS, ["9", "10"])
    picked = select_candle(bars, target_ms=anchor)
    assert picked is not None and picked.close == Decimal("10")


def test_a_gap_is_missing_data_and_never_forward_filled() -> None:
    """A halt, a closed session or an illiquid hole must read as missing, not as an unchanged price."""

    target = _floor(ANCHOR) + HORIZON_MS["1h"]
    stale = _candles(target - 20 * CANDLE_INTERVAL_MS, ["10"])
    assert select_candle(stale, target_ms=target) is None
    assert select_candle([], target_ms=target) is None


def test_a_candle_inside_the_tolerance_still_counts() -> None:
    target = _floor(ANCHOR)
    bars = _candles(target - 2 * CANDLE_INTERVAL_MS, ["10"])  # closes exactly one interval early
    assert select_candle(bars, target_ms=target) is not None


# ---------------------------------------------------------------------------- returns
def test_return_is_decimal_basis_points_with_half_even_rounding() -> None:
    assert return_bps(Decimal("100"), Decimal("101")) == 100
    assert return_bps(Decimal("100"), Decimal("99")) == -100
    assert return_bps(Decimal("100"), Decimal("100")) == 0
    # 0.005% -> 0.5 bp, half-even rounds to 0; 0.015% -> 1.5 bp rounds to 2.
    assert return_bps(Decimal("100"), Decimal("100.005")) == 0
    assert return_bps(Decimal("100"), Decimal("100.015")) == 2
    assert return_bps(Decimal("0"), Decimal("1")) is None


def test_event_level_aggregate_is_the_discrete_median_of_its_primaries() -> None:
    assert median_bps([]) is None
    assert median_bps([120]) == 120
    # Even count takes the lower middle — a return one contract actually printed, and exactly
    # PostgreSQL's percentile_disc(0.5), so the feed and the review page agree.
    assert median_bps([100, 200]) == 100
    assert median_bps([-300, 100, 200]) == 100


def test_a_percentage_without_a_denominator_is_not_reported() -> None:
    assert hit_pct(0, 0) is None
    assert coverage_pct(0, 0) is None
    assert hit_pct(56, 100) == 56.0
    assert coverage_pct(3, 4) == 75.0


def test_metric_version_is_pinned() -> None:
    """Changing this string is a new version and a replay, never an edit to what v1 rows mean."""

    assert REACTION_METRIC_VERSION == "reaction_v1"


# ---------------------------------------------------------------------------- venue adapters
def _json_transport(payloads: dict[str, object], *, seen: list[str] | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request.url.path)
        for path, payload in payloads.items():
            if path in request.url.path:
                return httpx.Response(200, content=json.dumps(payload))
        return httpx.Response(404, content=b"{}")

    return httpx.MockTransport(handler)


def test_binance_price_read_uses_the_narrow_endpoint_and_filters_to_the_target_set() -> None:
    """#109: `ticker/price` is 45.5 kB where `ticker/24hr` is 270 kB for the same USD-M market."""

    payload = [
        {"symbol": "BTCUSDT", "price": "68123.4", "time": 10},
        {"symbol": "ETHUSDT", "price": "3200.0", "time": 11},
        {"symbol": "NOTWANTED", "price": "1.0"},
    ]
    seen: list[str] = []
    quotes = asyncio.run(
        fetch_binance_futures_quotes(
            ["BTCUSDT"], transport=_json_transport({"/fapi/v1/ticker/price": payload}, seen=seen)
        )
    )
    assert seen == ["/fapi/v1/ticker/price"]
    assert [quote.venue_symbol for quote in quotes] == ["BTCUSDT"]
    assert quotes[0].price == Decimal("68123.4")
    assert quotes[0].source_at_ms == 10
    # The window is named; the number is not guessed. The loop derives it from the cached reference.
    assert quotes[0].change_basis == "rolling_24h"
    assert quotes[0].reference_price is None


def test_binance_day_read_carries_the_window_open_the_change_is_measured_against() -> None:
    """Caching the reference, not the percentage, is what keeps the cheap turns truthful (#109)."""

    payload = [
        {
            "symbol": "BTCUSDT",
            "lastPrice": "68123.4",
            "openPrice": "67000.0",
            "priceChangePercent": "1.676",
            "closeTime": 99,
        },
        {"symbol": "NOTWANTED", "lastPrice": "1.0", "openPrice": "1.0"},
    ]
    seen: list[str] = []
    quotes = asyncio.run(
        fetch_binance_futures_day_quotes(
            ["BTCUSDT"], transport=_json_transport({"/fapi/v1/ticker/24hr": payload}, seen=seen)
        )
    )
    assert seen == ["/fapi/v1/ticker/24hr"]
    assert [quote.venue_symbol for quote in quotes] == ["BTCUSDT"]
    assert quotes[0].price == Decimal("68123.4")
    assert quotes[0].reference_price == Decimal("67000.0")
    assert quotes[0].source_at_ms == 99


def test_binance_spot_asks_by_name_on_the_price_endpoint_at_any_list_length() -> None:
    """`ticker/price` is a flat weight 4 with `symbols=`; dropping the list costs 171 kB against 5 kB."""

    urls: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(request.url)
        return httpx.Response(200, content=json.dumps([{"symbol": "BTCUSDT", "price": "68123.4"}]))

    wanted = [f"SYM{index}USDT" for index in range(150)]
    asyncio.run(fetch_binance_spot_quotes(wanted, transport=httpx.MockTransport(handler)))

    assert urls[0].path == "/api/v3/ticker/price"
    assert urls[0].params.get("symbols")  # 150 symbols and still asked by name


def test_binance_spot_day_read_stops_asking_by_name_where_the_list_costs_more() -> None:
    """`ticker/24hr` really is tiered (weight 2/40/80), so past 100 symbols the market is the cheaper ask."""

    urls: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(request.url)
        return httpx.Response(200, content=json.dumps([{"symbol": "BTCUSDT", "lastPrice": "1", "openPrice": "1"}]))

    transport = httpx.MockTransport(handler)
    asyncio.run(fetch_binance_spot_day_quotes(["BTCUSDT"], transport=transport))
    asyncio.run(fetch_binance_spot_day_quotes([f"SYM{index}USDT" for index in range(150)], transport=transport))

    assert [url.path for url in urls] == ["/api/v3/ticker/24hr"] * 2
    assert urls[0].params.get("symbols") == '["BTCUSDT"]'
    assert urls[1].params.get("symbols") is None


def test_binance_quote_adapter_drops_a_nonsense_price_instead_of_publishing_zero() -> None:
    payload = [{"symbol": "BTCUSDT", "price": "0"}]
    quotes = asyncio.run(
        fetch_binance_spot_quotes(["BTCUSDT"], transport=_json_transport({"/api/v3/ticker/price": payload}))
    )
    assert quotes == ()


def test_a_day_read_missing_its_open_still_publishes_the_price_without_a_percentage() -> None:
    payload = [{"symbol": "BTCUSDT", "lastPrice": "68123.4", "openPrice": "0"}]
    quotes = asyncio.run(
        fetch_binance_futures_day_quotes(["BTCUSDT"], transport=_json_transport({"/fapi/v1/ticker/24hr": payload}))
    )
    assert quotes[0].price == Decimal("68123.4")
    assert quotes[0].reference_price is None


def test_a_json_string_body_is_an_invalid_payload_not_an_empty_one() -> None:
    """A proxy answering 200 with a JSON-encoded error body must not read as `venue_payload_empty`."""

    for fetch in (fetch_binance_futures_quotes, fetch_binance_futures_day_quotes):
        with pytest.raises(VenueExpectedError) as caught:
            asyncio.run(fetch(["BTCUSDT"], transport=_json_transport({"/fapi/v1/ticker": "nonsense"})))
        assert caught.value.code == "venue_payload_invalid"


def test_the_day_read_classifies_a_failure_like_every_other_venue_call() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(429, content=b"{}"))
    with pytest.raises(VenueExpectedError) as caught:
        asyncio.run(fetch_binance_spot_day_quotes(["BTCUSDT"], transport=transport))
    assert caught.value.code == "venue_rate_limited"


def test_hyperliquid_perp_quotes_are_index_aligned_with_the_universe() -> None:
    payload = [
        {"universe": [{"name": "BTC"}, {"name": "HYPE"}]},
        [
            {"midPx": "72261.5", "prevDayPx": "68663.0"},
            {"midPx": "40.5", "prevDayPx": "45.0"},
        ],
    ]
    quotes = asyncio.run(
        fetch_hyperliquid_quotes(["HYPE"], venue="hl.perp", transport=_json_transport({"/info": payload}))
    )
    assert [quote.venue_symbol for quote in quotes] == ["HYPE"]
    assert quotes[0].change_basis == "provider_day"
    assert quotes[0].reference_price == Decimal("45.0")


def test_hyperliquid_spot_quotes_key_on_the_market_coin_not_the_row_index() -> None:
    """`spotMetaAndAssetCtxs` ships more contexts than universe rows, so only `coin` identifies a market."""

    payload = [
        {"universe": [{"name": "PURR/USDC", "tokens": [1, 0]}]},
        [
            {"coin": "@1", "midPx": "1.0", "prevDayPx": "1.0"},
            {"coin": "PURR/USDC", "midPx": "0.0915", "prevDayPx": "0.0771"},
        ],
    ]
    quotes = asyncio.run(
        fetch_hyperliquid_quotes(["PURR/USDC"], venue="hl.spot", transport=_json_transport({"/info": payload}))
    )
    assert [quote.venue_symbol for quote in quotes] == ["PURR/USDC"]
    assert quotes[0].price == Decimal("0.0915")
    assert quotes[0].reference_price == Decimal("0.0771")


def test_quote_adapters_classify_every_anticipated_failure() -> None:
    for status, code in ((429, "venue_rate_limited"), (451, "venue_blocked"), (500, "venue_http_error")):
        transport = httpx.MockTransport(lambda _request, status=status: httpx.Response(status, content=b"{}"))
        with pytest.raises(VenueExpectedError) as caught:
            asyncio.run(fetch_binance_futures_quotes(["BTCUSDT"], transport=transport))
        assert caught.value.code == code

    def timeout(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow")

    with pytest.raises(VenueExpectedError) as caught:
        asyncio.run(fetch_binance_futures_quotes(["BTCUSDT"], transport=httpx.MockTransport(timeout)))
    assert caught.value.code == "venue_timeout"

    with pytest.raises(VenueExpectedError):
        asyncio.run(
            fetch_hyperliquid_quotes(["BTC"], venue="hl.perp", transport=_json_transport({"/info": {"bad": 1}}))
        )


def test_candle_adapters_normalize_both_providers_to_one_interval_convention() -> None:
    binance_rows = [[1_787_000_000_000, "1", "2", "0.5", "1.5", "10", 1_787_000_299_999]]
    bars = asyncio.run(
        fetch_binance_candles(
            "BTCUSDT",
            venue="binance.perp",
            start_ms=1_787_000_000_000,
            end_ms=1_787_003_600_000,
            transport=_json_transport({"/fapi/v1/klines": binance_rows}),
        )
    )
    assert bars[0].close == Decimal("1.5")
    # Exclusive end, so "closed at or before" never has to know whose off-by-one it is reading.
    assert bars[0].close_at_ms == bars[0].open_at_ms + CANDLE_INTERVAL_MS

    hl_rows = [{"t": 1_787_000_000_000, "T": 1_787_000_299_999, "c": "64349.0"}]
    bars = asyncio.run(
        fetch_hyperliquid_candles(
            "@107",
            venue="hl.spot",
            start_ms=1_787_000_000_000,
            end_ms=1_787_003_600_000,
            transport=_json_transport({"/info": hl_rows}),
        )
    )
    assert bars[0].close == Decimal("64349.0")
    assert bars[0].close_at_ms == bars[0].open_at_ms + CANDLE_INTERVAL_MS


def test_replay_bar_adapters_require_and_preserve_source_native_ohlcv() -> None:
    binance_rows = [[1_787_000_000_000, "1", "2", "0.5", "1.5", "10", 1_787_000_299_999]]
    binance = asyncio.run(
        fetch_binance_bars(
            "BTCUSDT",
            venue="binance.perp",
            start_ms=1_787_000_000_000,
            end_ms=1_787_003_600_000,
            transport=_json_transport({"/fapi/v1/klines": binance_rows}),
        )
    )
    hyperliquid = asyncio.run(
        fetch_hyperliquid_bars(
            "BTC",
            venue="hl.perp",
            start_ms=1_787_000_000_000,
            end_ms=1_787_003_600_000,
            transport=_json_transport(
                {
                    "/info": [
                        {
                            "t": 1_787_000_000_000,
                            "T": 1_787_000_299_999,
                            "o": "1",
                            "h": "2",
                            "l": "0.5",
                            "c": "1.5",
                            "v": "10",
                        }
                    ]
                }
            ),
        )
    )

    assert binance == hyperliquid
    assert (binance[0].open, binance[0].high, binance[0].low, binance[0].close, binance[0].volume) == (
        Decimal("1"),
        Decimal("2"),
        Decimal("0.5"),
        Decimal("1.5"),
        Decimal("10"),
    )


def test_hyperliquid_spot_catalogue_stores_queryable_markets_not_token_names() -> None:
    """#88 §3: `spotMeta.tokens` is a registry — `@107` and `PURR/USDC` are what quotes and candles accept."""

    payloads = {
        "/info": {
            "universe": [{"name": "PURR/USDC", "tokens": [1, 0], "index": 0}, {"name": "@1", "tokens": [2, 0]}],
            "tokens": [
                {"name": "USDC", "index": 0},
                {"name": "PURR", "index": 1},
                {"name": "HYPE", "index": 2},
            ],
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if body.get("type") == "spotMeta":
            return httpx.Response(200, content=json.dumps(payloads["/info"]))
        if body.get("type") == "meta":
            return httpx.Response(200, content=json.dumps({"universe": []}))
        return httpx.Response(200, content=json.dumps([]))

    instruments = asyncio.run(fetch_hyperliquid_instruments(transport=httpx.MockTransport(handler)))
    spot = [instrument for instrument in instruments if instrument.venue == "hl.spot"]
    assert {instrument.venue_symbol for instrument in spot} == {"PURR/USDC", "@1"}
    assert {instrument.base_symbol for instrument in spot} == {"PURR", "HYPE"}
    # The quote asset is the market's second token, and `USDC` itself is not a tradeable row.
    assert {instrument.quote_asset for instrument in spot} == {"USDC"}
    assert "USDC" not in {instrument.base_symbol for instrument in spot}
