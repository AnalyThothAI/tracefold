from __future__ import annotations

from decimal import Decimal

import pytest

from tracefold.news.liquidations import (
    PARSER_VERSION,
    SOURCE_CONTRACT_VERSION,
    parse_liquidation,
    source_key,
)
from tracefold.news.source_contracts import classify_source_contract, market_route


def _parse(title: str, *, venue: str = "binance", strategy_id: str = "2083"):
    return parse_liquidation(
        title,
        item_id="a" * 64,
        fact_id="whole",
        source_strategy_id=strategy_id,
        provider_source=venue,
        event_at_ms=1_000,
        received_at_ms=2_000,
    )


@pytest.mark.parametrize(
    ("text", "notional"),
    [
        ("SPCX Large Short Liquidation 202.71K at $137.01", Decimal("202710")),
        ("BTC Large Long Liquidation 1.25M at $123.45", Decimal("1250000")),
        ("ETH Large Short Liquidation 2B at $4.50", Decimal("2000000000")),
        ("SOL Large Long Liquidation 99 at $1", Decimal("99")),
    ],
)
def test_exact_template_and_decimal_units(text: str, notional: Decimal) -> None:
    fact = _parse(text)
    assert fact is not None
    assert fact.notional_usd == notional
    assert fact.price > 0


def test_provider_position_side_is_normalized_to_the_forced_order_side() -> None:
    short = _parse("SOL Large Short Liquidation 10K at $150")
    long = _parse("SOL Large Long Liquidation 10K at $150", venue="hyperliquid")
    assert short is not None and (short.liquidated_position_side, short.forced_order_side) == ("short", "buy")
    assert long is not None and (long.liquidated_position_side, long.forced_order_side) == ("long", "sell")


@pytest.mark.parametrize("venue", ["binance", "hyperliquid", "okx", "bybit"])
def test_every_measured_production_venue_parses_and_keeps_its_own_string(venue: str) -> None:
    """#553. `okx` and `bybit` were refused for naming a venue the allowlist had not been told about.

    Twelve OKX reports and one Bybit report in the retained window were discarded that way. Displaying a
    venue's liquidations is not a claim that Tracefold trades there, and the two were never the same
    statement; the venue is recorded as the provider spelled it.
    """

    fact = _parse("SOL Large Short Liquidation 10K at $150", venue=venue)
    assert fact is not None
    assert fact.source_venue == venue
    assert fact.symbol_contract_identity == f"unresolved:{venue}:SOL"


def test_a_frame_with_no_venue_is_still_a_liquidation() -> None:
    fact = _parse("SOL Large Short Liquidation 10K at $150", venue="")
    assert fact is not None
    assert fact.source_venue is None
    assert fact.symbol_contract_identity == "unresolved:unknown:SOL"


def test_the_reporting_strategy_is_recorded_and_never_merges_two_sources() -> None:
    large = _parse("SOL Large Short Liquidation 10K at $150", strategy_id="2083")
    realtime = _parse("SOL Large Short Liquidation 10K at $150", strategy_id="2000")
    assert large is not None and realtime is not None
    assert (large.source_strategy_id, realtime.source_strategy_id) == ("2083", "2000")


def test_the_native_instrument_token_survives_beside_the_normalized_symbol() -> None:
    fact = _parse("XYZ-SOL Large Short Liquidation 10K at $150")
    assert fact is not None
    assert (fact.raw_instrument, fact.symbol) == ("XYZ-SOL", "SOL")


def test_source_contract_records_every_semantic_gap_and_stays_incomplete() -> None:
    fact = _parse("SOL Large Short Liquidation 10K at $150")
    assert fact is not None
    assert fact.provider_record_identity
    assert fact.position_side_semantics
    assert fact.quantity_semantics == "not_provided"
    assert fact.notional_semantics == "provider_reported_usd_notional"
    assert fact.price_semantics == "provider_reported_unspecified_price"
    assert fact.completeness_assumption and fact.throttle_assumption
    assert fact.source_contract_complete is False
    assert fact.source_contract_version == SOURCE_CONTRACT_VERSION == "opennews_liquidation_source_v2"


@pytest.mark.parametrize(
    "text",
    [
        "SOL Liquidation 10K at $150",
        "SOL Large Buy Liquidation 10K at $150",
        "SOL Large Short Liquidation about 10K at $150",
        "SOL Large Short Liquidation 10T at $150",
        "SOL Large Short Liquidation 10K at mark $150",
        "SOL Large Short Liquidation -10K at $150",
        "XYZ- Large Short Liquidation 10K at $150",
    ],
)
def test_ambiguous_or_malformed_prose_fails_closed(text: str) -> None:
    assert _parse(text) is None


def test_a_missing_event_stamp_fails_closed() -> None:
    assert (
        parse_liquidation(
            "SOL Large Short Liquidation 10K at $150",
            item_id="a" * 64,
            fact_id="whole",
            source_strategy_id="2083",
            provider_source="binance",
            event_at_ms=0,
            received_at_ms=1_000,
        )
        is None
    )


def test_venue_clock_ahead_of_this_host_still_parses(fact_ahead) -> None:
    """#544. The forced trade happened; the venue simply stamped it 250 ms ahead of our clock."""

    assert fact_ahead is not None
    assert fact_ahead.event_at_ms - fact_ahead.received_at_ms == 250
    assert fact_ahead.symbol == "SOL"
    assert fact_ahead.forced_order_side == "buy"


@pytest.fixture()
def fact_ahead():
    return parse_liquidation(
        "SOL Large Short Liquidation 10K at $150",
        item_id="a" * 64,
        fact_id="whole",
        source_strategy_id="2083",
        provider_source="binance",
        event_at_ms=1_000_250,
        received_at_ms=1_000_000,
    )


def test_the_source_key_is_the_record_the_fact_and_the_parser_generation() -> None:
    assert source_key(item_id="a" * 64, fact_id="whole") == source_key(item_id="a" * 64, fact_id="whole")
    assert source_key(item_id="a" * 64, fact_id="whole") != source_key(item_id="b" * 64, fact_id="whole")
    assert PARSER_VERSION == "liquidation_parser_v1"


@pytest.mark.parametrize("strategy_id", ["2000", "2083"])
def test_both_liquidation_strategies_route_to_the_market_liquidation_branch(strategy_id: str) -> None:
    contract = classify_source_contract(
        {"strategies": [{"id": strategy_id, "name": "renamed by the provider", "source_type": "market"}]}
    )
    assert contract.source_contract_family == "liquidation_v1"
    assert market_route((contract,)) == ("liquidation", None)
