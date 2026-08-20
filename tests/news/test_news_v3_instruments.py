"""Pure tests for the tradeable instrument universe (#75): normalization, aliasing, classification, diff."""

from __future__ import annotations

import pytest

from tracefold.news.gate import GateInput, evaluate_gate, grounded_assets
from tracefold.news.instruments import (
    Instrument,
    classify,
    diff_universe,
    grounding_rollup,
    normalize_symbol,
    resolve_base_symbol,
    strip_quote_suffix,
)
from tracefold.news.storyline import final_storyline_key, storyline_key


def _inst(venue: str, venue_symbol: str, base: str) -> Instrument:
    return Instrument(
        venue=venue,
        venue_symbol=venue_symbol,
        base_symbol=base,
        instrument_class=classify(base, venue=venue),
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


def test_classify_prefers_the_symbol_over_the_venue_default() -> None:
    assert classify("BTC", venue="binance.perp") == "crypto"
    assert classify("MSTR", venue="hl.xyz") == "equity"
    assert classify("GOLD", venue="hl.flx") == "commodity"
    assert classify("SP500", venue="hl.km") == "index"
    assert classify("EUR", venue="hl.km") == "fx"
    # Pre-IPO names are equities on their venue but priced off a private mark: policy wants them distinguishable.
    assert classify("SPACEX", venue="hl.vntl") == "pre_ipo"
    assert classify("UNITREE", venue="binance.perp") == "pre_ipo"
    assert classify("WHATEVER", venue="unknown.venue") == "unknown"


def test_alias_seeds_collapse_one_issuer_and_are_cycle_safe() -> None:
    # SKHY and SKHX are both real hl.xyz contracts; the throttle must still see one issuer.
    assert resolve_base_symbol("SKHX") == "SKHY"
    assert resolve_base_symbol("SKHYNIX") == "SKHY"
    assert resolve_base_symbol("XYZ-SKHX") == "SKHY"
    assert resolve_base_symbol("XAU") == "GOLD"
    assert resolve_base_symbol("UNKNOWNTHING") == "UNKNOWNTHING"
    assert resolve_base_symbol("A", {"A": "B", "B": "A"}) in {"A", "B"}  # terminates


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


def test_diff_reports_listings_and_delistings_by_venue_symbol() -> None:
    previous = [_inst("binance.perp", "BTCUSDT", "BTC"), _inst("hl.xyz", "xyz:MSTR", "MSTR")]
    current = [_inst("binance.perp", "BTCUSDT", "BTC"), _inst("binance.perp", "UNITREEUSDT", "UNITREE")]
    diff = diff_universe(previous, current)
    assert [i.venue_symbol for i in diff.listed] == ["UNITREEUSDT"]
    assert [i.venue_symbol for i in diff.delisted] == ["xyz:MSTR"]
    assert diff.unchanged == 1
    assert not diff.empty
    assert diff_universe(current, current).empty


def test_diff_ignores_already_delisted_rows() -> None:
    gone = Instrument("binance.perp", "OLDUSDT", "OLD", "crypto", status="delisted")
    diff = diff_universe([gone], [])
    assert diff.empty  # a row already marked delisted is not a fresh delisting


# --------------------------------------------------------------- gate integration


def _coins(*symbols: str) -> tuple[dict[str, str], ...]:
    return tuple({"symbol": s, "grade": "A"} for s in symbols)


def test_tradeable_filter_removes_symbols_that_exist_nowhere() -> None:
    coins = _coins("MSTR", "CCXI")
    assert grounded_assets("MicroStrategy files", coins) == ("CCXI", "MSTR")
    filtered = grounded_assets("MicroStrategy files", coins, tradeable_symbols=frozenset({"MSTR"}))
    assert filtered == ("MSTR",)


def test_tradeable_filter_does_not_replace_the_word_collision_stop_list() -> None:
    """`NEAR`/`BILL`/`FLOCK` are real listed tokens *and* ordinary English words.

    The venue universe cannot tell those apart, so the collision stop-list still applies and both conditions are
    independent — this is the assumption the whole whitelist rests on.
    """

    universe = frozenset({"NEAR", "BILL", "FLOCK", "MSTR"})
    grounded = grounded_assets(
        "SEC chair backs the Clarity bill as talks near zero",
        _coins("NEAR", "BILL", "FLOCK", "MSTR"),
        tradeable_symbols=universe,
    )
    assert grounded == ("MSTR",)


def test_tradeable_filter_accepts_the_provider_prefixed_form() -> None:
    grounded = grounded_assets(
        "Unitree lists in Shanghai", _coins("XYZ-UNITREE"), tradeable_symbols=frozenset({"UNITREE"})
    )
    assert grounded == ("XYZ-UNITREE",)


def test_gate_without_a_universe_is_unchanged() -> None:
    base = {
        "engine_type": "news",
        "strategy_ids": ("1018",),
        "provider_score": 80.0,
        "ingest_mode": "live",
    }
    coins = _coins("MSTR", "CCXI")
    with_none = evaluate_gate(GateInput(title="MicroStrategy buys", coins=coins, **base))  # type: ignore[arg-type]
    assert with_none.grounded_assets == ("CCXI", "MSTR")
    with_universe = evaluate_gate(
        GateInput(title="MicroStrategy buys", coins=coins, tradeable_symbols=frozenset({"MSTR"}), **base)  # type: ignore[arg-type]
    )
    assert with_universe.grounded_assets == ("MSTR",)


@pytest.mark.parametrize("symbol", ["CL", "XYZ-CL"])
def test_crude_still_needs_energy_context_under_a_universe(symbol: str) -> None:
    universe = frozenset({"CL"})
    assert grounded_assets("Fed holds rates", _coins(symbol), tradeable_symbols=universe) == ()
    assert grounded_assets("Hormuz tanker seized", _coins(symbol), tradeable_symbols=universe) == (symbol,)


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
