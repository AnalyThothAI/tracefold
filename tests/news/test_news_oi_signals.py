"""Open-interest telemetry: parsing, basis points, and the measurement definition a group merges on.

The frames in these fixtures are verbatim production titles from 2026-08-22, including the two
single-character tickers (`S`, `4`) and the prose sentence about open interest that must NOT parse.

#553 removed this lane's judge. What used to be tested here -- a pseudo `TriageVerdict`, a reader
headline, an unconditional `drop` decision and a program identity for all of it -- no longer exists.
The four numbers and the contract they were measured under are what a frame is, and they are what
remains.
"""

from __future__ import annotations

import pytest

from tracefold.news.oi_contracts import OI_METRIC_VERSION
from tracefold.news.oi_signals import (
    PARSER_VERSION,
    SOURCE_CONTRACT_VERSION,
    OiSignal,
    measurement_definition,
    oi_source_contract,
    parse_oi_signal,
)

FRAME = "TRUMP OI Rise 4.55%, OI Value 32.17M, Whale Long Profit 80.21%, Whale/OI Ratio 100.71%"


def _signal(**over: object) -> OiSignal:
    base = dict(
        symbol="TRUMP",
        raw_instrument="TRUMP",
        direction="rise",
        oi_change_bps=455,
        oi_value_usd=32_170_000,
        whale_long_profit_bps=8021,
        whale_oi_ratio_bps=10_071,
    )
    base.update(over)
    return OiSignal(**base)  # type: ignore[arg-type]


def test_parses_the_production_template_into_exact_basis_points() -> None:
    signal = parse_oi_signal(FRAME)
    assert signal == _signal()


@pytest.mark.parametrize(("wire", "direction"), [("Rise", "rise"), ("Fall", "fall"), ("Drop", "fall")])
def test_production_direction_words_keep_their_domain_direction(wire: str, direction: str) -> None:
    signal = parse_oi_signal(FRAME.replace("Rise", wire))
    assert signal is not None and signal.direction == direction


@pytest.mark.parametrize(
    ("percent", "bps"),
    [("4.55", 455), ("100.71", 10_071), ("0.5", 50), ("1438.2", 143_820), ("-3.5", -350)],
)
def test_percentages_are_exact_integers_not_floats(percent: str, bps: int) -> None:
    """These numbers key a stored read model and a group comparison; 0.1 + 0.2 must not appear."""

    frame = f"BTC OI Rise {percent}%, OI Value 1M, Whale Long Profit 1%, Whale/OI Ratio 1%"
    signal = parse_oi_signal(frame)
    assert signal is not None and signal.oi_change_bps == bps


def test_the_symbol_is_the_titles_own_subject_normalized_and_the_native_token_is_kept() -> None:
    """The title's leading token decides, and both spellings of it are stored (#553).

    `symbol` strips the `XYZ-` prefix the way every other consumer of provider coin tags strips it, so
    one instrument keys one measurement group. `raw_instrument` keeps what the provider actually sent,
    because a display short name is not an identity and a reader looking at an ambiguous ticker needs
    the token the provider used.
    """

    assert parse_oi_signal(FRAME) == _signal()
    prefixed = parse_oi_signal("XYZ-UNITREE OI Rise 1%, OI Value 1M, Whale Long Profit 1%, Whale/OI Ratio 1%")
    assert prefixed is not None
    assert (prefixed.symbol, prefixed.raw_instrument) == ("UNITREE", "XYZ-UNITREE")
    single = parse_oi_signal("S OI Rise 3.04%, OI Value 3.86M, Whale Long Profit 92.31%, Whale/OI Ratio 31.42%")
    assert single is not None and single.symbol == "S"


@pytest.mark.parametrize(
    "title",
    [
        "HIP-3 has lost $820M in open interest over the past 5 days, down from its peak of $4.57B.",
        "Bitcoin open interest hits a record",
        "TRUMP OI Rise 4.55%, OI Value 32.17M",
        "",
    ],
)
def test_anything_that_is_not_the_template_is_not_a_signal(title: str) -> None:
    """Prose *about* open interest carries no numbers this lane can act on, and must reach nothing."""

    assert parse_oi_signal(title) is None


def test_units_scale_to_whole_dollars() -> None:
    for unit, expected in (("K", 3_860), ("M", 3_860_000), ("B", 3_860_000_000)):
        frame = f"BTC OI Rise 1%, OI Value 3.86{unit}, Whale Long Profit 1%, Whale/OI Ratio 1%"
        signal = parse_oi_signal(frame)
        assert signal is not None and signal.oi_value_usd == expected


def test_a_provider_number_that_cannot_fit_the_ledger_is_not_a_signal() -> None:
    too_large = "9" * 100
    frame = f"BTC OI Rise {too_large}%, OI Value 1M, Whale Long Profit 1%, Whale/OI Ratio 1%"
    assert parse_oi_signal(frame) is None


def test_the_measurement_window_is_proven_from_the_strategy_identity_or_stays_unknown() -> None:
    """#265. An unproven window is a first-class answer, and it is never defaulted to five minutes.

    The title carries no interval and no provider field holds one, so the only honest source is the
    bound Strategy identity. A frame from anything else keeps `None`, and the definition below spells
    that out rather than leaving a blank a consumer could read as "the usual window".
    """

    proven = oi_source_contract({"strategies": [{"id": "1019", "name": "renamed by the provider"}]})
    assert proven is not None
    assert (proven.strategy_id, proven.measurement_window_ms) == ("1019", 300_000)
    assert oi_source_contract({"strategies": [{"id": "9999", "source_type": "market"}]}) is None
    assert oi_source_contract({}) is None


def test_the_measurement_definition_separates_proven_from_unproven_groups() -> None:
    """Two frames merge into one notification group only when the same thing was measured."""

    proven = oi_source_contract({"strategies": [{"id": "1019"}]})
    assert measurement_definition(proven) == f"{OI_METRIC_VERSION}|{SOURCE_CONTRACT_VERSION}|300000"
    assert measurement_definition(None) == f"{OI_METRIC_VERSION}|unproven|unproven"
    assert measurement_definition(proven) != measurement_definition(None)


def test_the_parser_and_metric_identities_are_the_ones_the_ledger_stamps() -> None:
    assert (PARSER_VERSION, OI_METRIC_VERSION) == ("oi_signal_parser_v1", "oi_signal_v1")
    assert SOURCE_CONTRACT_VERSION == "opennews_oi_source_v1"
