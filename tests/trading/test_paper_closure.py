"""A decision and both paper sides must use one later LIVE bar anchor."""

from decimal import Decimal

from tracefold.trading.engine.paper import BAR_MS, Bar, LegGeometry, both_legs, geometry, paper_leg


def _bar(at: int, *, high: str = "100.1", low: str = "99.9", close: str = "100") -> Bar:
    return Bar(at, Decimal(high), Decimal(low), Decimal(close))


def test_geometry_uses_closed_history_and_clips_small_atr() -> None:
    rows = tuple(_bar((index + 1) * BAR_MS) for index in range(16))
    result = geometry(rows)
    assert (result.stop_bps, result.tp_bps, result.max_hold_ms) == (100, 200, 14_400_000)


def test_both_sides_anchor_after_decision_and_stop_wins_an_ambiguous_bar() -> None:
    bars = (
        _bar(BAR_MS),
        _bar(2 * BAR_MS),  # Decision is during this bar; it cannot anchor here.
        _bar(3 * BAR_MS),
        _bar(4 * BAR_MS, high="102.5", low="97.5"),  # Both sides touch SL and TP.
        _bar(5 * BAR_MS),
        _bar(6 * BAR_MS),
    )
    long, short = both_legs(
        decided_at_ms=2 * BAR_MS + 1,
        bars=bars,
        leg_geometry=LegGeometry(100, 200, 3 * BAR_MS),
        half_spread_bps=Decimal(1),
    )
    assert long.anchor_at_ms == short.anchor_at_ms == 3 * BAR_MS
    assert long.outcome == short.outcome == "sl"
    assert long.net_r == short.net_r == Decimal("-1.11")


def test_timeout_uses_last_close_and_incomplete_horizon_stays_missing() -> None:
    bars = tuple(_bar((index + 1) * BAR_MS) for index in range(5))
    leg = paper_leg(
        side="long",
        decided_at_ms=BAR_MS,
        bars=bars,
        leg_geometry=LegGeometry(100, 200, 3 * BAR_MS),
        half_spread_bps=Decimal(0),
    )
    assert leg.status == "complete" and leg.outcome == "timeout"
    assert leg.net_r == Decimal("-0.1")
    missing = paper_leg(
        side="long",
        decided_at_ms=BAR_MS,
        bars=bars[:-1],
        leg_geometry=LegGeometry(100, 200, 3 * BAR_MS),
        half_spread_bps=Decimal(0),
    )
    assert missing.status == "missing" and missing.reason == "horizon_incomplete"
    assert missing.net_r is None


def test_gap_is_missing_rather_than_a_synthetic_exit() -> None:
    bars = (_bar(BAR_MS), _bar(2 * BAR_MS), _bar(4 * BAR_MS), _bar(5 * BAR_MS))
    result = paper_leg(
        side="short",
        decided_at_ms=BAR_MS,
        bars=bars,
        leg_geometry=LegGeometry(100, 200, 2 * BAR_MS),
        half_spread_bps=Decimal(0),
    )
    assert result.reason == "bar_gap" and result.net_r is None
