"""Pure tests for the instrument universe (#75/#89): normalization, aliasing, classification, asset class."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from tracefold.integrations.venues.binance import fetch_binance_instruments
from tracefold.news.gate import GateInput, asset_class_of, evaluate_gate, grounded_assets
from tracefold.news.instruments import (
    ALIAS_SEEDS,
    classify,
    grounding_rollup,
    instruments_from_rows,
    is_valid_symbol,
    normalize_symbol,
    resolve_base_symbol,
    strip_quote_suffix,
)
from tracefold.news.storyline import final_storyline_key, storyline_key


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
            family="general",
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
            family="general",
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
        "strategy_ids": ("1018",),
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
