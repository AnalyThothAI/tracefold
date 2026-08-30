"""Direct tests for the price kernels, because every other test reaches them by accident.

`test_capital_policy.py` passes `pre_move_bps=` in as an already-computed integer, and the replay
tests assert on admission outcomes rather than on the number. So the arithmetic that turns two
prices into the basis-point move a capital decision is made on had 88% line coverage and close to
no assertion behind it: mutating `/` to `*`, `-` to `%` and the `10_000` scale inside `move_bps`
all survived the trading suite.

These pin the three things the module promises in its own docstrings: the move is `p1/p0 - 1` in
basis points and not some other arrangement of those symbols, a price that cannot produce one reads
as missing rather than as zero, and `select_bar` never forward-fills across a gap wider than the
tolerance.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from tracefold.trading.contracts import Bar
from tracefold.trading.market_context import (
    DEFAULT_BAR_GAP_TOLERANCE_MS,
    DEFAULT_PRE_MOVE_LOOKBACK_MS,
    PriceWindow,
    move_bps,
    pre_move_bps,
    select_bar,
)

ANCHOR_MS = 1_700_000_000_000


def _bar(close_at_ms: int, close: str) -> Bar:
    return Bar(open_at_ms=close_at_ms - 60_000, close_at_ms=close_at_ms, close=Decimal(close))


@pytest.mark.parametrize(
    ("p0", "p1", "expected"),
    [
        ("100", "101", 100),
        ("100", "99", -100),
        ("100", "100", 0),
        ("100", "200", 10_000),
        ("200", "100", -5_000),
        ("1", "2", 10_000),
    ],
)
def test_move_bps_is_p1_over_p0_minus_one_in_basis_points(p0: str, p1: str, expected: int) -> None:
    assert move_bps(Decimal(p0), Decimal(p1)) == expected


def test_move_bps_is_not_symmetric_in_its_arguments() -> None:
    """A doubling is +10000 bps and a halving is -5000. Any swapped or reciprocal form breaks this pair."""

    assert move_bps(Decimal("100"), Decimal("200")) == 10_000
    assert move_bps(Decimal("200"), Decimal("100")) == -5_000


@pytest.mark.parametrize(
    ("p1", "expected"),
    [("100005", 0), ("100015", 2), ("100025", 2), ("100035", 4)],
)
def test_move_bps_rounds_half_to_even(p1: str, expected: int) -> None:
    """Exact .5 basis points. Half-up would give 1, 2, 3, 4 and drift the band edges upward."""

    assert move_bps(Decimal("100000"), Decimal(p1)) == expected


@pytest.mark.parametrize(
    ("p0", "p1"),
    [
        (None, "100"),
        ("100", None),
        (None, None),
        ("0", "100"),
        ("-5", "100"),
        ("100", "0"),
        ("100", "-5"),
    ],
)
def test_move_bps_reads_an_unpriced_end_as_missing(p0: str | None, p1: str | None) -> None:
    """Both ends, not just the denominator.

    `p0 <= 0` is a division problem and was guarded from the start; `p1 <= 0` is not, and was not.
    A halted or delisted interval reporting `close = 0` returned `-10000` — a confident −100% move
    rather than an absence — which is precisely the mark this module exists to refuse to invent.
    """

    assert move_bps(None if p0 is None else Decimal(p0), None if p1 is None else Decimal(p1)) is None


def test_select_bar_takes_the_last_bar_closed_at_or_before_the_target() -> None:
    bars = [_bar(ANCHOR_MS - 120_000, "10"), _bar(ANCHOR_MS - 60_000, "11"), _bar(ANCHOR_MS, "12")]
    chosen = select_bar(bars, target_ms=ANCHOR_MS, gap_tolerance_ms=DEFAULT_BAR_GAP_TOLERANCE_MS)
    assert chosen is not None and chosen.close == Decimal("12")


def test_select_bar_never_reads_a_bar_that_closed_after_the_target() -> None:
    """The bar after the target is both later and nearer; only the `and` keeps it out."""

    bars = [_bar(ANCHOR_MS - 60_000, "11"), _bar(ANCHOR_MS + 60_000, "99")]
    chosen = select_bar(bars, target_ms=ANCHOR_MS, gap_tolerance_ms=DEFAULT_BAR_GAP_TOLERANCE_MS)
    assert chosen is not None and chosen.close == Decimal("11")


def test_select_bar_keeps_the_first_of_two_bars_that_closed_at_the_same_millisecond() -> None:
    """A duplicated candle must not silently re-price the window by arriving second."""

    bars = [_bar(ANCHOR_MS, "12"), _bar(ANCHOR_MS, "77")]
    chosen = select_bar(bars, target_ms=ANCHOR_MS, gap_tolerance_ms=DEFAULT_BAR_GAP_TOLERANCE_MS)
    assert chosen is not None and chosen.close == Decimal("12")


def test_select_bar_accepts_a_gap_exactly_at_the_tolerance_and_refuses_one_past_it() -> None:
    tolerance = DEFAULT_BAR_GAP_TOLERANCE_MS
    at_edge = [_bar(ANCHOR_MS - tolerance, "10")]
    past_edge = [_bar(ANCHOR_MS - tolerance - 1, "10")]
    assert select_bar(at_edge, target_ms=ANCHOR_MS, gap_tolerance_ms=tolerance) is not None
    assert select_bar(past_edge, target_ms=ANCHOR_MS, gap_tolerance_ms=tolerance) is None


def test_select_bar_reads_an_empty_window_as_missing() -> None:
    assert select_bar([], target_ms=ANCHOR_MS, gap_tolerance_ms=DEFAULT_BAR_GAP_TOLERANCE_MS) is None


def test_pre_move_bps_measures_the_lookback_ending_at_the_anchor() -> None:
    bars = [_bar(ANCHOR_MS - DEFAULT_PRE_MOVE_LOOKBACK_MS, "100"), _bar(ANCHOR_MS, "103")]
    assert pre_move_bps(bars, anchor_at_ms=ANCHOR_MS) == 300


def test_pre_move_bps_honours_a_narrowed_window() -> None:
    """The start is the anchor *minus* the lookback; any other combination reads a different bar."""

    window = PriceWindow(lookback_ms=600_000, bar_gap_tolerance_ms=DEFAULT_BAR_GAP_TOLERANCE_MS)
    bars = [
        _bar(ANCHOR_MS - DEFAULT_PRE_MOVE_LOOKBACK_MS, "50"),
        _bar(ANCHOR_MS - 600_000, "100"),
        _bar(ANCHOR_MS, "103"),
    ]
    assert pre_move_bps(bars, anchor_at_ms=ANCHOR_MS, window=window) == 300


@pytest.mark.parametrize("present_at_ms", [0, DEFAULT_PRE_MOVE_LOOKBACK_MS])
def test_pre_move_bps_needs_both_ends(present_at_ms: int) -> None:
    """Either end missing on its own is enough to refuse; neither guard carries the other."""

    bars = [_bar(ANCHOR_MS - present_at_ms, "100")]
    assert pre_move_bps(bars, anchor_at_ms=ANCHOR_MS) is None


def test_select_bar_takes_the_latest_qualifying_bar_from_an_unordered_window() -> None:
    """Bars come off a provider REST page and nothing in the signature promises they are sorted.

    This is what separates `>` from `!=` in the tie-break. With `!=`, any later-iterated bar at a
    different timestamp replaces the best one — invisible for as long as every fixture happens to
    be in ascending order, and wrong the first time a page is not.
    """

    bars = [_bar(ANCHOR_MS, "12"), _bar(ANCHOR_MS - 120_000, "10"), _bar(ANCHOR_MS - 60_000, "11")]
    chosen = select_bar(bars, target_ms=ANCHOR_MS, gap_tolerance_ms=DEFAULT_BAR_GAP_TOLERANCE_MS)
    assert chosen is not None and chosen.close == Decimal("12")


def test_the_price_window_defaults_are_the_documented_values() -> None:
    """Pinned as literals.

    Every other test in this module derives its fixture from these constants, so the whole file
    keeps passing if one moves. The module docstring records why they cannot: the inverted-U band
    `policy.py` executes was measured on a 1 h lookback, and changing the window silently
    invalidates the thresholds without changing a line of `policy.py`.
    """

    assert DEFAULT_PRE_MOVE_LOOKBACK_MS == 3_600_000
    assert DEFAULT_BAR_GAP_TOLERANCE_MS == 330_000
    assert PriceWindow().lookback_ms == DEFAULT_PRE_MOVE_LOOKBACK_MS
    assert PriceWindow().bar_gap_tolerance_ms == DEFAULT_BAR_GAP_TOLERANCE_MS


def test_the_price_window_cannot_be_mutated_after_construction() -> None:
    """It is recorded as evidence alongside the decision it produced, so it may not drift after."""

    window = PriceWindow()
    with pytest.raises(FrozenInstanceError):
        window.lookback_ms = 1  # type: ignore[misc]
