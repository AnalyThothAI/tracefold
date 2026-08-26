"""The read-only replay reports what the rules did, and proposes nothing (#265 PR-C).

Two properties matter more than any number it prints. It must drive the *production* pure functions, so
the report cannot describe a funnel the scanner no longer has; and it must not answer the two questions
it has no evidence for — the price band and the outcome both need market data this report does not
fetch, and inventing either would turn a receipt into a backtest.
"""

from __future__ import annotations

from typing import Any

from tracefold.trading.candidate.blacklist import Blacklist
from tracefold.trading.candidate.eligibility import EligibilityPolicy
from tracefold.trading.candidate.gate import GateConfig
from tracefold.trading.research.oi_replay import (
    PENDING_MARKET_CONTEXT,
    meets_target_template,
    replay_oi_facts,
)
from tracefold.trading.strategy.oi_smart_money_momentum import OiSmartMoneyMomentumStrategy

NOW = 1_787_000_000_000
GATE = GateConfig.from_policy(EligibilityPolicy(min_oi_value_usd=5_000_000), venue_priority=("binance", "hyperliquid"))
STRATEGY = OiSmartMoneyMomentumStrategy()
OPEN_DENY = Blacklist.from_rows([])


def _row(**kwargs: Any) -> dict[str, Any]:
    row = {
        "event_id": "e1",
        "final_decision": "drop",
        "source_rule": "whale_ratio_below_threshold",
        "ingest_mode": "live",
        "program_version": "news_oi_signal_v1",
        "metric_version": "oi_signal_v1",
        "source_strategy_id": "1019",
        "source_contract_version": "opennews_oi_source_v1",
        "measurement_window_ms": 300_000,
        "symbol": "TUT",
        "direction": "rise",
        "oi_change_bps": 1_548,
        "oi_value_usd": 23_010_000,
        "whale_long_profit_bps": 9_074,
        "whale_oi_ratio_bps": 5_424,
        "rank_in_window": 1,
        "observed_at_ms": NOW - 600_000,
        "verdict_created_at_ms": NOW - 599_000,
        "venue": "binance",
        "learning_epoch": "program_v7",
        "program_sha256": "a" * 64,
        "policy_version": "news_triage_policy_v10",
        "editorial_origin": "telemetry_deterministic",
        "editorial_sha256": "b" * 64,
        "scored_judgment_sha256": "c" * 64,
        "runtime_manifest_sha": "d" * 64,
    }
    row.update(kwargs)
    return row


def _replay(rows: list[dict[str, Any]]) -> Any:
    return replay_oi_facts(
        rows,  # type: ignore[arg-type]
        gate=GATE,
        strategy=STRATEGY,
        blacklist=OPEN_DENY,
        listed_symbols={},
        now_ms=NOW,
    )


def test_the_seven_day_live_shape_is_reproduced_stage_by_stage() -> None:
    """The real distribution, as measured on 2026-08-27 over the seven days the OI ledger has existed.

    405 parsed facts, all `rise`; 11 with OI change >= 10%; 7 of those with smart-money ratio > 50%; all
    7 with a positive profit metric; 4 above the 5M liquidity canary. The binding condition is the
    OI-change rule, not the reader's push rule and not the floor — which is exactly the kind of thing a
    per-rule survivor count is for and a total is not.
    """

    rows = (
        [_row(event_id=f"weak-{i}", oi_change_bps=320, symbol="AAA") for i in range(6)]
        + [_row(event_id=f"ratio-{i}", oi_change_bps=1_100, whale_oi_ratio_bps=3_200, symbol="BBB") for i in range(2)]
        + [_row(event_id="thin", oi_change_bps=1_491, oi_value_usd=3_190_000, whale_oi_ratio_bps=6_593, symbol="STORJ")]
        + [_row(event_id="tut", symbol="TUT")]
    )
    report = _replay(rows)

    assert report.facts == 10
    assert report.by_reason["strategy:smart_money_oi_change_below_floor"] == 6
    assert report.by_reason["strategy:smart_money_ratio_below_or_equal_floor"] == 2
    # STORJ is real and refused by the Candidate Gate, not by an Alpha rule: $3.19M is below the
    # 5M liquidity canary, which is a universe question and has one owner (#264).
    assert report.by_reason["eligibility:oi_value_below_floor"] == 1
    assert report.by_stage[PENDING_MARKET_CONTEXT] == 1
    assert [row.symbol for row in report.surviving] == ["TUT"]


def test_the_target_cohort_is_reported_separately_from_the_funnel() -> None:
    """Two different questions. The template asks how often the shape occurs at all.

    STORJ meets all three template conditions and is still refused by the liquidity floor, so a report
    that merged the two would make "the shape never occurs" and "the shape occurs and we cannot trade
    it" look identical — and they call for opposite responses.
    """

    rows = [
        _row(event_id="tut", symbol="TUT"),
        _row(event_id="storj", symbol="STORJ", oi_change_bps=1_491, oi_value_usd=3_190_000, whale_oi_ratio_bps=6_593),
        _row(event_id="weak", symbol="AAA", oi_change_bps=320),
    ]
    report = _replay(rows)

    assert sorted(row.symbol for row in report.target_cohort) == ["STORJ", "TUT"]
    assert [row.symbol for row in report.surviving] == ["TUT"]
    assert report.by_reason["eligibility:oi_value_below_floor"] == 1


def test_the_readers_own_decision_is_reported_and_decides_nothing() -> None:
    """Five of the seven qualifying frames in the live window were `drop`. That is the finding."""

    report = _replay([_row(event_id="tut", final_decision="drop"), _row(event_id="hbar", final_decision="push")])
    assert report.reader_decisions == {"drop": 1, "push": 1}
    assert report.by_stage[PENDING_MARKET_CONTEXT] == 2


def test_freshness_is_excluded_because_it_answers_nothing_about_a_rule() -> None:
    """Every row in a seven-day window is stale against `now`; replaying that would report one number."""

    report = _replay([_row(event_id="ancient", observed_at_ms=NOW - 7 * 86_400_000)])
    assert "eligibility:trigger_stale" not in report.by_reason
    assert report.by_stage[PENDING_MARKET_CONTEXT] == 1


def test_the_two_price_rules_are_never_reported_as_binding() -> None:
    """The report has no candle. A rule it cannot evaluate must not appear as one that refused."""

    report = _replay([_row(event_id=f"f{i}") for i in range(5)])
    assert not any(key.endswith("price_direction_not_confirmed") for key in report.by_reason)
    assert not any(key.endswith("move_above_band_chasing") for key in report.by_reason)
    assert report.by_stage[PENDING_MARKET_CONTEXT] == 5


def test_an_unprovable_measurement_window_stops_at_the_strategy_and_leaves_the_cohort() -> None:
    """The template names five minutes. Three numbers over an unknown period are not an instance of it."""

    report = _replay([_row(event_id="unproven", measurement_window_ms=None)])
    assert report.by_reason["strategy:source_window_mismatch"] == 1
    assert report.surviving == []
    assert report.target_cohort == []


def test_an_unroutable_frame_is_refused_and_still_counted_in_the_template_cohort() -> None:
    """Routing is about where an order would go, not about whether the shape occurred."""

    report = _replay([_row(event_id="okx", venue="okx")])
    assert report.by_reason["routing:venue_unresolved"] == 1
    assert [row.symbol for row in report.target_cohort] == ["TUT"]
    assert report.target_cohort[0].routable is False


def test_the_template_test_reads_only_the_three_conditions_it_names() -> None:
    from tracefold.trading.contracts import OiTradeCandidate

    def _candidate(**kwargs: Any) -> OiTradeCandidate:
        from tracefold.trading.candidate.eligibility import oi_candidate

        parsed = oi_candidate(_row(**kwargs))  # type: ignore[arg-type]
        assert isinstance(parsed, OiTradeCandidate)
        return parsed

    config = STRATEGY.config
    assert meets_target_template(_candidate(), config=config) is True
    # Liquidity, rank and venue are not template conditions and do not remove a row from the cohort.
    assert meets_target_template(_candidate(oi_value_usd=1, rank_in_window=9, venue="okx"), config=config) is True
    for kwargs in (
        {"oi_change_bps": 999},
        {"whale_oi_ratio_bps": 5_000},
        {"whale_long_profit_bps": 0},
        {"measurement_window_ms": 900_000},
    ):
        assert meets_target_template(_candidate(**kwargs), config=config) is False
