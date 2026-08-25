from __future__ import annotations

from decimal import Decimal

from tracefold.trading.contracts import Bar
from tracefold.trading.research.event_study import (
    EventStudyPolicy,
    bootstrap_mean_interval,
    measure_event,
    summarize_evaluation_rows,
)

NOW = 1_900_000_000_000
FIVE_MINUTES = 300_000


def _bars() -> tuple[Bar, ...]:
    prices = ("100", "102", "101", "104", "106", "108", "110", "112", "114", "116", "118", "120", "122")
    return tuple(
        Bar(
            open_at_ms=NOW - FIVE_MINUTES + index * FIVE_MINUTES,
            close_at_ms=NOW + index * FIVE_MINUTES,
            close=Decimal(price),
        )
        for index, price in enumerate(prices)
    )


def test_event_study_reports_every_required_horizon_and_missing_resolution() -> None:
    outcome = measure_event(
        _bars(),
        cutoff_ms=NOW,
        decision="no_trade",
        research_side="long",
        policy=EventStudyPolicy(
            stop_bps=500,
            take_profit_bps=100,
            max_holding_ms=1_800_000,
            taker_fee_bps_per_leg=5,
        ),
        gap_tolerance_ms=FIVE_MINUTES,
    )
    assert outcome is not None
    assert set(outcome["horizons"]) == {"5s", "30s", "1m", "5m", "15m", "1h"}
    assert outcome["horizons"]["5s"] == {
        "status": "missing",
        "reason": "source_bar_resolution_unsupported",
    }
    assert outcome["horizons"]["5m"]["signed_return_bps"] == 200
    assert outcome["horizons"]["1h"]["signed_return_bps"] == 2_200
    assert outcome["mfe_bps"] == 2_200
    assert outcome["mae_bps"] == 100
    assert outcome["exit_simulation"]["exit_reason"] == "take_profit"
    assert outcome["exit_simulation"]["fee_bps"] == 10
    assert outcome["exit_simulation"]["slippage_bps"] == 4
    assert outcome["exit_simulation"]["net_return_bps"] is None
    assert "cost:funding_unavailable" in outcome["missing_data"]


def test_bootstrap_and_strategy_venue_liquidity_cohorts_are_stable_and_separate() -> None:
    outcome = measure_event(
        _bars(),
        cutoff_ms=NOW,
        decision="no_trade",
        research_side="long",
        policy=EventStudyPolicy(
            stop_bps=500,
            take_profit_bps=100,
            max_holding_ms=1_800_000,
            taker_fee_bps_per_leg=5,
        ),
        gap_tolerance_ms=FIVE_MINUTES,
    )
    assert outcome is not None
    rows = [
        {
            "strategy_id": strategy,
            "underlying_key": "crypto:DOGE",
            "research_partition": "holdout",
            "manifest": {
                "instrument": {"exchange_id": "binance"},
                "contexts": {
                    "market": {"depth_notional_usd": None},
                    "liquidation": {
                        "event_at_ms": NOW - 1_000,
                        "received_at_ms": NOW,
                        "source_contract": {"complete": False},
                    },
                },
            },
            "market_outcome": outcome,
        }
        for strategy in ("liquidation_continuation_shadow_v1", "liquidation_exhaustion_shadow_v1")
    ]
    by_strategy, cohorts = summarize_evaluation_rows(rows)
    assert len(by_strategy) == len(cohorts) == 2
    assert {row["cohort_key"] for row in cohorts} == {
        "liquidation_continuation_shadow_v1|binance|unknown",
        "liquidation_exhaustion_shadow_v1|binance|unknown",
    }
    assert all(row["promotion_ready"] is False for row in cohorts)
    assert all("source_contract_incomplete" in row["promotion_reasons"] for row in cohorts)
    assert all("duplicate_rate_unavailable" in row["promotion_reasons"] for row in cohorts)
    assert all(row["missing_data"]["source:duplicate_rate_unavailable"] == 1 for row in cohorts)
    assert all(row["duplicate_rate_bps"] is None for row in cohorts)
    assert bootstrap_mean_interval([10, 20, 30], cohort_key="same") == bootstrap_mean_interval(
        [10, 20, 30], cohort_key="same"
    )
