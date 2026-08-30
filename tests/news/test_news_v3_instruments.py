"""Pure tests for the instrument universe (#75/#89): normalization, aliasing, classification, asset class."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from tracefold.integrations.venues.binance import fetch_binance_instruments
from tracefold.integrations.venues.errors import VenueExpectedError
from tracefold.integrations.venues.okx import fetch_okx_instruments
from tracefold.integrations.venues.us_reference import fetch_us_reference_instruments
from tracefold.news.events.gate import GateInput, asset_class_of, evaluate_gate, grounded_assets
from tracefold.news.events.storyline import final_storyline_key, storyline_key
from tracefold.news.market_review.instruments import (
    ALIAS_SEEDS,
    classify,
    grounding_rollup,
    instruments_from_rows,
    is_valid_symbol,
    normalize_symbol,
    resolve_base_symbol,
    strip_quote_suffix,
)


def test_normalize_strips_provider_prefix_and_dex_namespace() -> None:
    # OpenNews mirrors Hyperliquid's `xyz` builder DEX in its coin tags, so both forms name the same instrument.
    assert normalize_symbol("XYZ-UNITREE") == "UNITREE"
    assert normalize_symbol("xyz:UNITREE") == "UNITREE"
    assert normalize_symbol(" mstr ") == "MSTR"
    assert normalize_symbol("") == ""


def test_strip_quote_suffix_prefers_the_declared_quote() -> None:
    assert strip_quote_suffix("UNITREEUSDT", quote_asset="USDT") == "UNITREE"
    assert strip_quote_suffix("BTCUSDT") == "BTC"
    # Longest-suffix-first: `USD` must not win over `USDT` and leave a stray `T`.
    assert strip_quote_suffix("ETHUSDT") == "ETH"
    # A symbol that *is* its quote is left alone rather than emptied.
    assert strip_quote_suffix("USDT") == "USDT"


def test_symbol_validation_accepts_non_ascii_listings() -> None:
    """Binance really does list `币安人生USDT`, and the provider tags names like `牛来` (#89)."""

    assert is_valid_symbol("币安人生")
    assert is_valid_symbol("BRK.B") and is_valid_symbol("BTC")
    assert not is_valid_symbol("A B")
    assert not is_valid_symbol("")
    assert not is_valid_symbol("X" * 33)


def test_classify_prefers_the_symbol_over_the_venue_default() -> None:
    assert classify("BTC", venue="binance.perp") == "crypto"
    assert classify("MSTR", venue="hl.xyz") == "equity"
    assert classify("GOLD", venue="hl.flx") == "commodity"
    assert classify("SP500", venue="hl.km") == "index"
    assert classify("EUR", venue="hl.km") == "fx"
    # Pre-IPO names are equities on their venue but priced off a private mark: policy wants them distinguishable.
    assert classify("SPACEX", venue="hl.vntl") == "pre_ipo"
    assert classify("WHATEVER", venue="unknown.venue") == "unknown"


def test_crypto_gauges_are_not_equities_even_on_an_equity_dex() -> None:
    """`hl.para` lists equities *and* crypto-market gauges; classifying TOTAL2 as an index would make the Gate
    read "total2 breaks out" as a stock headline."""

    assert classify("TOTAL2", venue="hl.para") == "crypto"
    assert classify("BTCD", venue="hl.para") == "crypto"
    assert classify("AVGO", venue="hl.para") == "equity"


def test_commodity_names_never_collide_with_a_listed_coin() -> None:
    """`GAS` is Neo's gas token on three crypto venues; natural gas trades as `NATGAS`. Since the class now
    reaches the Gate, calling the token a commodity would label its headlines as stock news."""

    assert classify("GAS", venue="binance.spot") == "crypto"
    assert classify("GAS", venue="hl.perp") == "crypto"
    assert classify("NATGAS", venue="hl.xyz") == "commodity"
    assert resolve_base_symbol("NATGAS") == "NATGAS"  # never merged into the token's storyline


def test_stored_class_survives_the_round_trip_through_rows() -> None:
    """`classify()` cannot re-derive what the venue declared, so a read must not recompute it (#89)."""

    rows = [
        {"venue": "binance.perp", "venue_symbol": "JPMUSDT", "base_symbol": "JPM", "instrument_class": "equity"},
        {"venue": "binance.perp", "venue_symbol": "BTCUSDT", "base_symbol": "BTC", "instrument_class": ""},
        {"venue": "binance.perp", "venue_symbol": "XUSDT", "base_symbol": "X", "instrument_class": "nonsense"},
    ]
    built = {i.base_symbol: i.instrument_class for i in instruments_from_rows(rows)}
    assert built["JPM"] == "equity"  # declared by the venue, preserved
    assert built["BTC"] == "crypto"  # nothing stored: fall back to the classifier
    assert built["X"] == "crypto"  # stored garbage is not a class


def test_alias_seeds_collapse_one_issuer_and_are_cycle_safe() -> None:
    # SKHY and SKHX are both real hl.xyz contracts; the throttle must still see one issuer.
    assert resolve_base_symbol("SKHX") == "SKHY"
    assert resolve_base_symbol("SKHYNIX") == "SKHY"
    assert resolve_base_symbol("XYZ-SKHX") == "SKHY"
    assert resolve_base_symbol("XAU") == "GOLD"
    assert resolve_base_symbol("UNKNOWNTHING") == "UNKNOWNTHING"
    assert resolve_base_symbol("A", {"A": "B", "B": "A"}) in {"A", "B"}  # terminates


def test_alias_seeds_point_at_symbols_a_venue_actually_lists() -> None:
    """The `1810.HK -> XIAOMI` bug: Binance lists Xiaomi as `HK1810`, so the old seed resolved to nothing (#89)."""

    assert resolve_base_symbol("XIAOMI") == "HK1810"
    assert resolve_base_symbol("1810.HK") == "HK1810"
    assert resolve_base_symbol("NOKIA") == "NOK"
    assert resolve_base_symbol("RAYDIUM") == "RAY"
    assert resolve_base_symbol("BTT") == "BTTC"
    # A seed must never point at another seed's alias, or one hop is not enough to resolve it.
    assert not set(ALIAS_SEEDS.values()) & set(ALIAS_SEEDS)


def test_storyline_key_buckets_one_issuer_together() -> None:
    def key(symbol: str) -> str:
        return storyline_key(
            title="SK Hynix approves buyback",
            headline_zh="SK海力士回购",
            scope="single_name",
            primary_assets=[symbol],
            dedupe_family="general",
        )

    # The 2026-08-19 failure: one 40T KRW buyback shipped nine cards because the symbol alternated.
    assert key("SKHY") == key("SKHX") == key("SKHYNIX") == "asset:SKHY"
    assert key("XAU") == "asset:GOLD"


def test_final_storyline_key_resolves_aliases_on_both_sides() -> None:
    # The verdict says SKHX, the Gate grounded XYZ-SKHY: they must still match and bucket together.
    assert (
        final_storyline_key(
            title="SK Hynix buyback",
            headline_zh="SK海力士回购",
            scope="single_name",
            verdict_primaries=["SKHX"],
            grounded_assets=["XYZ-SKHY"],
            dedupe_family="general",
        )
        == "asset:SKHY"
    )


# --------------------------------------------------------- venue-declared classes


def _binance_transport(spot: dict[str, object], futures: dict[str, object]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = futures if "fapi" in str(request.url) else spot
        return httpx.Response(200, content=json.dumps(payload))

    return httpx.MockTransport(handler)


def test_binance_declared_class_beats_the_venue_default() -> None:
    """Binance labels its TradFi perps itself; ignoring that put 81 of 169 in the universe as crypto (#89)."""

    futures = {
        "symbols": [
            {
                "symbol": "TENCENTUSDT",
                "status": "TRADING",
                "baseAsset": "TENCENT",
                "quoteAsset": "USDT",
                "contractType": "TRADIFI_PERPETUAL",
                "underlyingType": "HK_EQUITY",
            },
            {
                "symbol": "CLUSDT",
                "status": "TRADING",
                "baseAsset": "CL",
                "quoteAsset": "USDT",
                "contractType": "TRADIFI_PERPETUAL",
                "underlyingType": "COMMODITY",
            },
            {
                "symbol": "OPENAIUSDT",
                "status": "TRADING",
                "baseAsset": "OPENAI",
                "quoteAsset": "USDT",
                "contractType": "TRADIFI_PERPETUAL",
                "underlyingType": "PREMARKET",
            },
            {
                "symbol": "BTCUSDT",
                "status": "TRADING",
                "baseAsset": "BTC",
                "quoteAsset": "USDT",
                "contractType": "PERPETUAL",
                "underlyingType": "COIN",
            },
            {
                "symbol": "BTCDOMUSDT",
                "status": "TRADING",
                "baseAsset": "BTCDOM",
                "quoteAsset": "USDT",
                "contractType": "PERPETUAL",
                "underlyingType": "INDEX",
            },
            {"symbol": "DEADUSDT", "status": "BREAK", "baseAsset": "DEAD", "quoteAsset": "USDT"},
        ]
    }
    spot = {"symbols": [{"symbol": "BTCUSDT", "status": "TRADING", "baseAsset": "BTC", "quoteAsset": "USDT"}]}
    fetched = asyncio.run(fetch_binance_instruments(transport=_binance_transport(spot, futures)))
    classes = {i.venue_symbol: i.instrument_class for i in fetched if i.venue == "binance.perp"}
    assert classes == {
        "TENCENTUSDT": "equity",
        "CLUSDT": "commodity",
        "OPENAIUSDT": "pre_ipo",
        "BTCUSDT": "crypto",
        # Binance's only two INDEX perps are crypto gauges, so INDEX is deliberately not mapped.
        "BTCDOMUSDT": "crypto",
    }


def test_an_unmapped_tradfi_underlying_does_not_read_as_crypto() -> None:
    """Binance adding `JP_EQUITY` must not put the new listings back in the universe as coins (#89)."""

    futures = {
        "symbols": [
            {
                "symbol": "SONYUSDT",
                "status": "TRADING",
                "baseAsset": "SONY",
                "quoteAsset": "USDT",
                "contractType": "TRADIFI_PERPETUAL",
                "underlyingType": "JP_EQUITY",
            }
        ]
    }
    spot = {"symbols": [{"symbol": "BTCUSDT", "status": "TRADING", "baseAsset": "BTC", "quoteAsset": "USDT"}]}
    fetched = asyncio.run(fetch_binance_instruments(transport=_binance_transport(spot, futures)))
    assert [i.instrument_class for i in fetched if i.venue == "binance.perp"] == ["equity"]


def test_okx_catalogue_includes_live_tradfi_swap_and_spot_without_guessing_contract_names() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["instType"] == "SWAP":
            data = [
                {
                    "instId": "MSFT-USDT-SWAP",
                    "state": "live",
                    "ctValCcy": "MSFT",
                    "settleCcy": "USDT",
                    "instCategory": "3",
                },
                {
                    "instId": "BTC-USD-SWAP",
                    "state": "live",
                    "ctValCcy": "BTC",
                    "settleCcy": "USD",
                    "instCategory": "1",
                },
            ]
        else:
            data = [
                {
                    "instId": "BTC-USDT",
                    "state": "live",
                    "baseCcy": "BTC",
                    "quoteCcy": "USDT",
                }
            ]
        return httpx.Response(200, json={"code": "0", "data": data})

    fetched = asyncio.run(fetch_okx_instruments(transport=httpx.MockTransport(handler)))
    assert [(row.venue, row.venue_symbol, row.instrument_class) for row in fetched] == [
        ("okx.perp", "MSFT-USDT-SWAP", "equity"),
        ("okx.spot", "BTC-USDT", "crypto"),
    ]


# ------------------------------------------------------------------ asset class


def _coins(*symbols: str) -> tuple[dict[str, str], ...]:
    return tuple({"symbol": s, "grade": "A"} for s in symbols)


def test_asset_class_reads_the_universe_when_the_provider_gives_no_xyz_twin() -> None:
    """A week of live traffic had 47 events tagged only `MRNA` / `CIEN` / `PANW`, all read as crypto (#89)."""

    classes = {"MRNA": "equity", "BTC": "crypto"}
    assert asset_class_of(("MRNA",), False, instrument_classes=classes) == "equity_or_commodity"
    assert asset_class_of(("BTC",), False, instrument_classes=classes) == "crypto"
    # A mixed headline is a stock headline: the equity is the specific fact.
    assert asset_class_of(("BTC", "MRNA"), False, instrument_classes=classes) == "equity_or_commodity"


def test_asset_class_falls_back_to_the_provider_prefix_without_a_universe() -> None:
    assert asset_class_of(("XYZ-MU",), False) == "equity_or_commodity"
    assert asset_class_of(("BTC",), False) == "crypto"
    assert asset_class_of((), True) == "macro"
    assert asset_class_of((), False) == "none"


def test_asset_class_keeps_the_old_reading_for_a_symbol_the_universe_does_not_know() -> None:
    """`UWMC` is a real NYSE listing with no crypto perp — no venue we poll can say so, and the Gate must not
    invent an answer. Closing this gap is #91."""

    assert asset_class_of(("UWMC",), False, instrument_classes={"BTC": "crypto"}) == "crypto"
    # ...but a known equity in the same tag set still decides it.
    assert asset_class_of(("UWMC", "MU"), False, instrument_classes={"MU": "equity"}) == "equity_or_commodity"


def test_gate_labels_the_event_from_the_universe() -> None:
    base = {
        "engine_type": "news",
        "provider_score": 80.0,
        "ingest_mode": "live",
    }
    verdict = evaluate_gate(
        GateInput(  # type: ignore[arg-type]
            title="Moderna upgraded to Neutral at BofA",
            coins=_coins("MRNA"),
            instrument_classes={"MRNA": "equity"},
            **base,
        )
    )
    assert verdict.asset_class == "equity_or_commodity"
    assert verdict.grounded_assets == ("MRNA",)


def test_the_universe_never_removes_a_grounded_tag() -> None:
    """#75 shipped an existence filter behind a flag; the dry-run showed it only ever removed real equities."""

    grounded = grounded_assets("Telix reports half-year results", _coins("TLX", "MSTR"))
    assert grounded == ("MSTR", "TLX")


@pytest.mark.parametrize("symbol", ["CL", "XYZ-CL"])
def test_crude_still_needs_energy_context(symbol: str) -> None:
    assert grounded_assets("Fed holds rates", _coins(symbol)) == ()
    assert grounded_assets("Hormuz tanker seized", _coins(symbol)) == (symbol,)


def _refs(**listed: str | None) -> dict[str, dict[str, object]]:
    return {
        symbol: {"symbol": symbol, "base_symbol": symbol, "venue": venue, "listed": venue is not None}
        for symbol, venue in listed.items()
    }


def test_grounding_counts_an_event_once_however_many_tags_land() -> None:
    """An Event is grounded when *any* tag names something — the same condition the Gate admits on (#87)."""

    rollup = grounding_rollup(
        {"ev-1": ["BTC", "ETH"], "ev-2": ["BTC"], "ev-3": ["SPOT"]},
        _refs(BTC="binance.perp", ETH="binance.perp", SPOT=None),
    )

    assert rollup["grounded_24h"] == 2
    assert rollup["ungrounded_by_symbol_24h"] == {"SPOT": 1}


def test_grounding_counts_a_failing_tag_on_every_event_it_cost() -> None:
    """One bad provider tag is one row for the operator, carrying how many Events it took down."""

    rollup = grounding_rollup(
        {f"ev-{i}": ["SPOT"] for i in range(38)} | {"ev-x": ["NEAR", "BTC"]},
        _refs(BTC="binance.perp", SPOT=None, NEAR=None),
    )

    assert rollup["ungrounded_by_symbol_24h"] == {"SPOT": 38, "NEAR": 1}
    # `ev-x` still grounds on BTC even though NEAR failed beside it.
    assert rollup["grounded_24h"] == 1


def test_grounding_reports_a_tag_the_universe_has_never_heard_of() -> None:
    """A tag missing from `refs` entirely is ungrounded, not skipped — a lookup gap must not read as success."""

    rollup = grounding_rollup({"ev-1": ["WHOKNOWS"]}, _refs(BTC="binance.perp"))

    assert rollup == {"tagged_24h": 1, "grounded_24h": 0, "ungrounded_by_symbol_24h": {"WHOKNOWS": 1}}


def test_grounding_is_empty_without_any_tagged_event() -> None:
    assert grounding_rollup({}, {}) == {
        "tagged_24h": 0,
        "grounded_24h": 0,
        "ungrounded_by_symbol_24h": {},
    }


def test_grounding_counts_only_events_that_offered_a_tag() -> None:
    """`tagged` is the honest denominator: an Event with no coin tag never offered a symbol to resolve.

    Subtracting `grounded` from the triaged total instead reported every macro headline as a symbol that
    failed to land, and permanently displaced the delivery line on the feed header (#87 review).
    """

    rollup = grounding_rollup(
        {"ev-macro-1": [], "ev-macro-2": [], "ev-btc": ["BTC"], "ev-spot": ["SPOT"]},
        _refs(BTC="binance.perp", SPOT=None),
    )

    # Two Events carried tags; one of them landed. The two tagless macro Events are in neither number.
    assert rollup["tagged_24h"] == 4
    assert rollup["grounded_24h"] == 1
    assert rollup["ungrounded_by_symbol_24h"] == {"SPOT": 1}


# ------------------------------------------------------- US reference directory

_NASDAQ_HEADER = "Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares"
_OTHER_HEADER = "ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol"
_TRAILER = "File Creation Time: 0820202603:02|||||||"


def _padding(prefix: str, count: int, *, other: bool = False) -> list[str]:
    """Filler so the fixtures clear the per-file floor that stops a truncated answer mass-delisting the tier."""

    if other:
        return [f"{prefix}{i}|Filler Corp|N|{prefix}{i}|N|100|N|{prefix}{i}" for i in range(count)]
    return [f"{prefix}{i}|Filler Inc. - Common Stock|Q|N|N|100|N|N" for i in range(count)]


_NASDAQ_LISTED = "\n".join(
    [
        _NASDAQ_HEADER,
        "AAPL|Apple Inc. - Common Stock|Q|N|N|100|N|N",
        "QQQ|Invesco QQQ Trust, Series 1|Q|N|N|100|Y|N",
        "ZZZT|Nasdaq Test Stock|G|Y|N|100|N|N",
        *_padding("NDQ", 120),
        _TRAILER,
    ]
)

_OTHER_LISTED = "\n".join(
    [
        _OTHER_HEADER,
        "UWMC|UWM Holdings Corporation Class A Common Stock|N|UWMC|N|100|N|UWMC",
        "BRK.A|Berkshire Hathaway Inc.|N|BRK A|N|10|N|BRK/A",
        "AAPL|Duplicate across both files|N|AAPL|N|100|N|AAPL",
        "truncated row",
        *_padding("NYS", 120, other=True),
        _TRAILER,
    ]
)


def _us_reference_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        body = _OTHER_LISTED if "otherlisted" in str(request.url) else _NASDAQ_LISTED
        return httpx.Response(200, content=body.encode())

    return httpx.MockTransport(handler)


def test_us_reference_reads_the_directory_the_way_the_file_declares_it() -> None:
    """Every ticker is a stock, the file's own ETF flag says which are funds, and nothing else gets through."""

    fetched = asyncio.run(fetch_us_reference_instruments(transport=_us_reference_transport()))

    assert {i.venue for i in fetched} == {"us.listed"}
    named = {i.base_symbol: i.instrument_class for i in fetched if not i.base_symbol.startswith(("NDQ", "NYS"))}
    assert named == {
        "AAPL": "equity",  # the first file wins; the duplicate row in the second is dropped
        "QQQ": "index",  # ETF = Y
        "UWMC": "equity",
        "BRK.A": "equity",  # dots are part of a US ticker
    }
    # A test issue, a truncated line, and a trailer padded to the full field count all have to be dropped.
    assert "ZZZT" not in {i.base_symbol for i in fetched}
    assert not any(" " in i.base_symbol for i in fetched)


def test_a_word_collision_never_reaches_the_class_map() -> None:
    """`SPOT` is Spotify in the US directory and "spot gold" in a headline — 84 tags in one week, all noise.

    The order saves it: the collision stop-list runs inside `grounded_assets`, so a stop-listed tag is gone
    before `asset_class_of` can ask the directory what it is. Adding thousands of three-letter tickers (#91)
    does not widen that hole, and this pins the ordering that keeps it shut.
    """

    verdict = evaluate_gate(
        GateInput(  # type: ignore[arg-type]
            title="SPOT GOLD climbs to a record",
            coins=_coins("SPOT"),
            instrument_classes={"SPOT": "equity"},
            engine_type="news",
            provider_score=85.0,
            ingest_mode="live",
        )
    )
    assert verdict.grounded_assets == ()
    assert verdict.asset_class != "equity_or_commodity"


def test_us_reference_refuses_a_file_that_came_back_almost_empty() -> None:
    """A header-and-trailer answer is a broken file. Accepting it would mark ~6.5k reference rows delisted in one
    turn and switch the whole tier off until a good snapshot lands."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = _NASDAQ_LISTED if "nasdaqlisted" in str(request.url) else f"{_OTHER_HEADER}\n{_TRAILER}\n"
        return httpx.Response(200, content=body.encode())

    with pytest.raises(VenueExpectedError) as caught:
        asyncio.run(fetch_us_reference_instruments(transport=httpx.MockTransport(handler)))
    assert caught.value.code == "venue_payload_empty"


def test_us_reference_reports_a_redirect_as_a_redirect() -> None:
    """Redirects are not followed, so without this the short HTML body reads as a corrupt directory."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(301, headers={"location": "/moved"}, content=b"<html>moved</html>")

    with pytest.raises(VenueExpectedError) as caught:
        asyncio.run(fetch_us_reference_instruments(transport=httpx.MockTransport(handler)))
    assert caught.value.code == "venue_http_error" and caught.value.status_code == 301


def test_us_reference_refuses_a_payload_that_is_not_the_directory() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>maintenance</html>")

    with pytest.raises(VenueExpectedError) as caught:
        asyncio.run(fetch_us_reference_instruments(transport=httpx.MockTransport(handler)))
    assert caught.value.code == "venue_payload_invalid"
