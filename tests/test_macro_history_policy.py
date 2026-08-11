from __future__ import annotations

from tracefold.macro.history_policy import (
    market_history_limits,
    series_history_limits,
)


def test_series_history_policy_preserves_full_percentiles_without_universal_cap() -> None:
    limits = series_history_limits(
        (
            "fred.bamlc0a0cm",
            "fred.dgs10",
            "fred.cpiaucsl",
            "treasury.daily_nominal_curve",
            "treasury.daily_real_curve",
        )
    )
    assert limits["fred.bamlc0a0cm"] == 10_000
    assert limits["fred.dgs10"] == 500
    assert limits["fred.cpiaucsl"] == 500
    assert limits["treasury.daily_nominal_curve"] == 130
    assert limits["treasury.daily_real_curve"] == 130


def test_market_history_policy_bounds_intraday_to_the_longest_comparison_window() -> None:
    limits = market_history_limits(
        (
            "yfinance.spy.intraday",
            "nasdaq.spy.daily",
        )
    )
    assert limits == {
        "yfinance.spy.intraday": 36,
        "nasdaq.spy.daily": 260,
    }
