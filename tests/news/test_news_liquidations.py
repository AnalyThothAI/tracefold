from __future__ import annotations

from decimal import Decimal

import pytest

from tracefold.news.liquidations import (
    SOURCE_CONTRACT_VERSION,
    parse_liquidation,
)
from tracefold.news.source_contracts import classify_source_contract, market_route


def _parse(title: str, *, venue: str = "binance", strategy_id: str = "2083"):
    return parse_liquidation(
        title,
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
        # Verbatim production title from 2026-09-15 13:41 UTC: the provider's recurrence suffix.
        ("BTC Large Long Liquidation 759.27K at $76089.00, 24 times in 24h", Decimal("759270")),
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


def test_a_frame_with_no_venue_is_still_a_liquidation() -> None:
    fact = _parse("SOL Large Short Liquidation 10K at $150", venue="")
    assert fact is not None
    assert fact.source_venue is None


def test_the_native_instrument_token_survives_beside_the_normalized_symbol() -> None:
    fact = _parse("XYZ-SOL Large Short Liquidation 10K at $150")
    assert fact is not None
    assert (fact.raw_instrument, fact.symbol) == ("XYZ-SOL", "SOL")


def test_parser_retains_the_source_contract_version() -> None:
    fact = _parse("SOL Large Short Liquidation 10K at $150")
    assert fact is not None
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
        "SOL Large Short Liquidation 10K at $150, many times in 24h",
        "SOL Large Short Liquidation 10K at $150, 2 times in 7d",
    ],
)
def test_ambiguous_or_malformed_prose_fails_closed(text: str) -> None:
    assert _parse(text) is None


def test_thousands_separators_are_read_on_the_liquidation_template_too() -> None:
    """#562 §5 row 12. One figure grammar for both provider templates.

    The account template accepted `$1,234,567.89` while this one refused the same separator outright,
    so a wide forced trade -- exactly the one a reader wants -- was the one stored raw with no numbers.
    """

    fact = _parse("SOL Large Short Liquidation 1,250.5K at $79,817.87")
    assert fact is not None
    assert fact.notional_usd == Decimal("1250500")
    assert fact.price == Decimal("79817.87")


def test_a_wide_instrument_token_is_clipped_rather_than_losing_the_forced_trade() -> None:
    """#562 §5 row 12. Both parsers clip the provider's token; neither refuses a record for its width."""

    fact = _parse(f"{'S' * 40} Large Short Liquidation 10K at $150")
    assert fact is not None
    assert fact.raw_instrument == "S" * 32
    assert fact.notional_usd == Decimal("10000")


def test_a_missing_event_stamp_fails_closed() -> None:
    assert (
        parse_liquidation(
            "SOL Large Short Liquidation 10K at $150",
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
        provider_source="binance",
        event_at_ms=1_000_250,
        received_at_ms=1_000_000,
    )


@pytest.mark.parametrize("strategy_id", ["2000", "2083"])
def test_both_liquidation_strategies_route_to_the_market_liquidation_branch(strategy_id: str) -> None:
    contract = classify_source_contract(
        {"strategies": [{"id": strategy_id, "name": "renamed by the provider", "source_type": "market"}]}
    )
    assert contract.source_contract_family == "liquidation_v1"
    assert market_route((contract,)) == ("liquidation", None)
