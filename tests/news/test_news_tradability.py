from __future__ import annotations

import asyncio
from collections.abc import Sequence

import httpx

from tracefold.integrations.venues.binance import fetch_binance_instruments_for_candidates
from tracefold.integrations.venues.bitget import fetch_bitget_instruments
from tracefold.integrations.venues.candles import fetch_bitget_candles, fetch_lighter_candles
from tracefold.integrations.venues.lighter import fetch_lighter_instruments
from tracefold.integrations.venues.tradability import VenueCatalogTradabilityVerifier
from tracefold.news.market_review.instruments import Instrument
from tracefold.news.tradability import REQUIRED_TRADABILITY_VENUES, tradability_candidates


def test_hong_kong_code_builds_exact_exchange_aliases() -> None:
    candidates, confident = tradability_candidates(
        event={"leader_title": "MetaLight (02605.HK) announces interim results"},
        verdict={"headline_zh": "MetaLight（02605.HK）公布中期业绩"},
        symbols=["2605"],
    )

    assert confident is True
    assert {"2605", "02605", "HK2605", "HK02605", "2605.HK", "02605.HK", "METALIGHT"} <= set(candidates)


def test_bare_numeric_code_is_not_safe_enough_to_authorize_deletion() -> None:
    calls: list[str] = []

    async def fetch(_candidates: Sequence[str]) -> Sequence[Instrument]:
        calls.append("called")
        return ()

    verifier = VenueCatalogTradabilityVerifier(fetchers={venue: fetch for venue in REQUIRED_TRADABILITY_VENUES})
    review = asyncio.run(
        verifier.review(
            event={"leader_title": "公司公布中期业绩"},
            verdict={"headline_zh": "净亏损收窄"},
            symbols=["2605"],
        )
    )

    assert review.state == "incomplete"
    assert calls == []


def test_any_exact_catalogue_match_wins_even_when_another_venue_failed() -> None:
    async def empty(_candidates: Sequence[str]) -> Sequence[Instrument]:
        return ()

    async def failed(_candidates: Sequence[str]) -> Sequence[Instrument]:
        raise TimeoutError

    async def bitget(_candidates: Sequence[str]) -> Sequence[Instrument]:
        return (
            Instrument(
                venue="bitget.perp",
                venue_symbol="METALIGHTUSDT",
                base_symbol="METALIGHT",
                instrument_class="equity",
                quote_asset="USDT",
            ),
        )

    verifier = VenueCatalogTradabilityVerifier(
        fetchers={
            "binance": failed,
            "hyperliquid": empty,
            "okx": empty,
            "lighter": empty,
            "bitget": bitget,
        }
    )
    review = asyncio.run(
        verifier.review(
            event={"leader_title": "MetaLight (02605.HK) announces interim results"},
            verdict={"headline_zh": "MetaLight 公布中期业绩"},
            symbols=["2605"],
        )
    )

    assert review.state == "matched"
    assert review.failed_venues == ("binance",)
    assert review.matches[0].venue_symbol == "METALIGHTUSDT"
    assert review.matches[0].requested_symbol == "2605"


def test_absence_requires_all_five_catalogues_to_answer_successfully() -> None:
    async def empty(_candidates: Sequence[str]) -> Sequence[Instrument]:
        return ()

    verifier = VenueCatalogTradabilityVerifier(fetchers={venue: empty for venue in REQUIRED_TRADABILITY_VENUES})
    review = asyncio.run(
        verifier.review(
            event={"leader_title": "MetaLight (02605.HK) announces interim results"},
            verdict={"headline_zh": "MetaLight 公布中期业绩"},
            symbols=["2605"],
        )
    )

    assert review.state == "absent"
    assert review.checked_venues == REQUIRED_TRADABILITY_VENUES
    assert review.failed_venues == ()


def test_one_catalogue_failure_makes_no_match_incomplete_not_absent() -> None:
    async def empty(_candidates: Sequence[str]) -> Sequence[Instrument]:
        return ()

    async def failed(_candidates: Sequence[str]) -> Sequence[Instrument]:
        raise TimeoutError

    verifier = VenueCatalogTradabilityVerifier(
        fetchers={
            "binance": failed,
            "hyperliquid": empty,
            "okx": empty,
            "lighter": empty,
            "bitget": empty,
        }
    )
    review = asyncio.run(
        verifier.review(
            event={"leader_title": "MetaLight (02605.HK) announces interim results"},
            verdict={"headline_zh": "MetaLight 公布中期业绩"},
            symbols=["2605"],
        )
    )

    assert review.state == "incomplete"
    assert review.failed_venues == ("binance",)


def test_binance_candidate_lookup_checks_every_spot_quote_and_full_futures() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "spot.test":
            assert request.url.params["showPermissionSets"] == "false"
            return httpx.Response(
                200,
                json={
                    "symbols": [
                        {
                            "status": "TRADING",
                            "symbol": "METALIGHTBTC",
                            "baseAsset": "METALIGHT",
                            "quoteAsset": "BTC",
                        },
                        {
                            "status": "TRADING",
                            "symbol": "UNRELATEDUSDT",
                            "baseAsset": "UNRELATED",
                            "quoteAsset": "USDT",
                        },
                    ]
                },
            )
        assert request.url.host == "futures.test"
        assert "symbol" not in request.url.params
        return httpx.Response(
            200,
            json={
                "symbols": [
                    {
                        "status": "TRADING",
                        "symbol": "BTCUSDT",
                        "baseAsset": "BTC",
                        "quoteAsset": "USDT",
                        "contractType": "PERPETUAL",
                    }
                ]
            },
        )

    instruments = asyncio.run(
        fetch_binance_instruments_for_candidates(
            ("2605", "02605", "HK2605", "HK02605", "2605.HK", "METALIGHT"),
            transport=httpx.MockTransport(handler),
            spot_base_url="https://spot.test",
            futures_base_url="https://futures.test",
        )
    )

    assert [(row.venue, row.venue_symbol, row.quote_asset) for row in instruments] == [
        ("binance.spot", "METALIGHTBTC", "BTC")
    ]


def test_lighter_catalogue_keeps_numeric_price_key_and_exact_base_symbol() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "code": 200,
                "order_books": [
                    {"symbol": "IWM", "market_id": 153, "market_type": "perp", "status": "active"},
                    {"symbol": "ETH/USDC", "market_id": 2048, "market_type": "spot", "status": "active"},
                ],
            },
        )
    )

    instruments = asyncio.run(fetch_lighter_instruments(transport=transport, base_url="https://lighter.test"))

    assert [(row.venue, row.venue_symbol, row.base_symbol) for row in instruments] == [
        ("lighter.perp", "153", "IWM"),
        ("lighter.spot", "2048", "ETH"),
    ]


def test_bitget_catalogue_normalizes_reality_stock_prefix_without_changing_pair() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        category = request.url.params["category"]
        rows = (
            [
                {
                    "symbol": "TSLAUSDT",
                    "baseCoin": "TSLA",
                    "quoteCoin": "USDT",
                    "symbolType": "stock",
                    "isRwa": "YES",
                    "status": "online",
                }
            ]
            if category == "USDT-FUTURES"
            else [
                {
                    "symbol": "RPBRUSDT",
                    "baseCoin": "rPBR",
                    "quoteCoin": "USDT",
                    "symbolType": "stock",
                    "isReality": "yes",
                    "status": "online",
                }
            ]
        )
        return httpx.Response(200, json={"code": "00000", "data": rows})

    instruments = asyncio.run(
        fetch_bitget_instruments(transport=httpx.MockTransport(handler), base_url="https://bitget.test")
    )

    assert [(row.venue, row.venue_symbol, row.base_symbol) for row in instruments] == [
        ("bitget.perp", "TSLAUSDT", "TSLA"),
        ("bitget.spot", "RPBRUSDT", "PBR"),
    ]


def test_lighter_and_bitget_one_minute_candles_normalize_to_exclusive_close() -> None:
    lighter = httpx.MockTransport(
        lambda _request: httpx.Response(200, json={"code": 200, "c": [{"t": 1_000_000, "c": "12.5"}]})
    )
    bitget = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={"code": "00000", "data": [["1000000", "10", "13", "9", "12.5", "1", "12.5"]]},
        )
    )

    lighter_rows = asyncio.run(
        fetch_lighter_candles(
            "153",
            venue="lighter.perp",
            start_ms=900_000,
            end_ms=1_100_000,
            interval="1m",
            transport=lighter,
            base_url="https://lighter.test",
        )
    )
    bitget_rows = asyncio.run(
        fetch_bitget_candles(
            "TSLAUSDT",
            venue="bitget.perp",
            start_ms=900_000,
            end_ms=1_100_000,
            interval="1m",
            transport=bitget,
            base_url="https://bitget.test",
        )
    )

    assert lighter_rows[0].close_at_ms == 1_060_000
    assert bitget_rows[0].close_at_ms == 1_060_000
