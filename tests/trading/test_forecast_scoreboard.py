"""Six policies share one Case view and forecast scoring uses observed paper labels."""

from decimal import Decimal

from tracefold.trading.engine.forecast import (
    Forecast,
    LegProbabilities,
    PolicyConfig,
    all_policy_decisions,
    decide,
)
from tracefold.trading.engine.paper import PaperLeg
from tracefold.trading.engine.scoreboard import ScoredCase, forecast_score, policy_scores


def _leg(side: str, *, outcome: str = "tp", net_r: str = "1.9") -> PaperLeg:
    return PaperLeg(
        side, "complete", outcome, None, 1, 2, Decimal(100), Decimal(102), Decimal(200), Decimal(10), Decimal(net_r)
    )


def test_forecast_decision_accounts_for_cost_and_baselines_share_the_view() -> None:
    forecast = Forecast(
        LegProbabilities(Decimal("0.6"), Decimal("0.2"), Decimal("0.2")),
        LegProbabilities(Decimal("0.2"), Decimal("0.6"), Decimal("0.2")),
    )
    config = PolicyConfig(100, 200, Decimal(1))
    view = {"perp_return_15m_bps": "15"}
    decisions = all_policy_decisions(view, forecast, config)
    assert [item.policy_id for item in decisions] == [
        "forecast",
        "always_long",
        "always_short",
        "abstain",
        "momentum15m",
        "fade15m",
    ]
    assert [item.action for item in decisions] == ["long", "long", "short", "abstain", "long", "short"]
    assert decisions[0].expected_r == Decimal("0.89")
    assert decide(view, forecast, PolicyConfig(100, 200, Decimal(1), min_expected_r=Decimal(1))).action == "abstain"
    assert decide({}, None, config).reason == "forecast_missing"


def test_scoreboard_joins_each_action_to_its_own_side_and_clusters_by_day_asset() -> None:
    config = PolicyConfig(100, 200, Decimal(0))
    rows = tuple(
        ScoredCase(
            case_id=f"case-{index}",
            asset_id="crypto:BTC",
            day=f"2026-09-{index + 1:02d}",
            trigger_kind="oi",
            decisions=all_policy_decisions({}, None, config),
            legs=(_leg("long", net_r="1.9"), _leg("short", outcome="sl", net_r="-1.1")),
        )
        for index in range(10)
    )
    scores = {score.policy_id: score for score in policy_scores(rows)}
    assert scores["always_long"].status == "ok"
    assert scores["always_long"].average_r == Decimal("1.9")
    assert scores["always_long"].ci_low == scores["always_long"].ci_high == Decimal("1.9")
    assert scores["always_short"].average_r == Decimal("-1.1")
    assert scores["abstain"].actions == 0 and scores["abstain"].status == "insufficient_data"
    assert policy_scores(rows[:9])[1].average_r is None
    missing_assessment = ScoredCase(
        case_id="case-unassessed",
        asset_id="crypto:BTC",
        day="2026-10-01",
        trigger_kind="oi",
        decisions=(),
        legs=(_leg("long"), _leg("short", outcome="sl", net_r="-1.1")),
    )
    diluted = {item.policy_id: item for item in policy_scores((*rows, missing_assessment))}
    assert diluted["always_long"].cases == 11
    assert diluted["always_long"].coverage == Decimal(10) / 11


def test_perfect_multiclass_forecast_beats_pit_climatology_without_zero_filling() -> None:
    forecast = Forecast(
        LegProbabilities(Decimal(1), Decimal(0), Decimal(0)),
        LegProbabilities(Decimal(0), Decimal(1), Decimal(0)),
    )
    baseline = LegProbabilities(Decimal("0.333"), Decimal("0.333"), Decimal("0.334"))
    case = ScoredCase(
        case_id="case-1",
        asset_id="crypto:BTC",
        day="2026-09-29",
        trigger_kind="oi",
        decisions=(),
        legs=(_leg("long"), _leg("short", outcome="sl", net_r="-1.1")),
        forecast=forecast,
        baseline_long=baseline,
        baseline_short=baseline,
    )
    score = forecast_score((case,), min_legs=2)
    assert score.multiclass_brier == score.log_loss == Decimal(0)
    assert score.brier_skill_score == Decimal(1)
    assert forecast_score((case,), min_legs=30).status == "insufficient_data"
