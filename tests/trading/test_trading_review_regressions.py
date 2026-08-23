"""Regressions for the `/code-review` findings. One test per defect, named after what broke.

These are the cases the first pass got wrong. Several of them were invisible to the original suite
because the suite exercised the same happy path the code was written for — a rejected close, a model
budget spent on a quadrant that cannot trade, a `news_only` case that no input could produce.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tracefold.trading import (
    Bar,
    EligibilityPolicy,
    NewsTradeCandidate,
    OiRegime,
    OiTradeCandidate,
    TradePolicy,
    blacklist_rule,
    decide,
    evaluate_paper_exit,
    pre_model_reject,
)
from tracefold.trading.pipeline import _fuse

NOW = 1_787_000_000_000


def _oi(symbol: str = "DOGE", at: int = NOW, whale: int = 9_900, value: int = 73_010_000) -> OiTradeCandidate:
    return OiTradeCandidate(
        event_id=f"e-{symbol}-{at}",
        observed_at_ms=at,
        base_symbol=symbol,
        venue="hyperliquid",
        oi_direction="rise",
        oi_change_bps=320,
        oi_value_usd=value,
        whale_long_profit_bps=whale,
        whale_oi_ratio_bps=21_097,
        rank_in_window=1,
        metric_version="oi_signal_v1",
        learning_epoch="program_v6",
        program_version="news_oi_signal_v1",
        program_sha256="a" * 64,
        policy_version="news_triage_policy_v10",
        editorial_origin="telemetry_deterministic",
        editorial_sha256="b" * 64,
        scored_judgment_sha256="c" * 64,
        runtime_manifest_sha="d" * 64,
    )


def _news(symbol: str = "DOGE", at: int = NOW) -> NewsTradeCandidate:
    return NewsTradeCandidate(
        event_id=f"n-{symbol}-{at}",
        verdict_created_at_ms=at,
        opened_at_ms=at,
        base_symbol=symbol,
        evidence_version=1,
        evidence_sha256="sha",
        focus_fact_id="f",
        comparison_fingerprint="fp",
        source_artifact_id="x:1",
        source_published_at_ms=at,
        final_decision="push",
        event_type="listing",
        risk_direction="bullish",
        scope="single_name",
        magnitude=2,
        novelty="new_fact",
        headline_zh="标题",
        why_zh="机制",
        learning_epoch="program_v6",
        program_version="news_semantic_program_v4",
        program_sha256="a" * 64,
        policy_version="news_triage_policy_v10",
        editorial_origin="model",
        editorial_sha256="b" * 64,
        scored_judgment_sha256="c" * 64,
        runtime_manifest_sha="d" * 64,
    )


# ---------------------------------------------------------------------------- 7 + 18: the News lane
def test_a_news_verdict_reached_by_a_later_oi_frame_fuses_into_news_oi() -> None:
    """The two-loop planner skipped this underlying before `attach_oi` could ever run."""

    plan = _fuse(_oi(at=NOW), _news(at=NOW - 60_000), policy=EligibilityPolicy())
    assert plan is not None
    assert plan.kind == "news_oi"
    assert plan.observed_at_ms == NOW  # the OI frame fired later, so it is the primary


def test_an_oi_frame_reached_by_a_later_news_verdict_also_fuses() -> None:
    """The reverse direction — and the only path that makes `oi_lookback_seconds` live configuration."""

    plan = _fuse(_oi(at=NOW - 60_000), _news(at=NOW), policy=EligibilityPolicy())
    assert plan is not None
    assert plan.kind == "news_oi"
    assert plan.observed_at_ms == NOW  # the verdict fired later, so it is the primary
    assert plan.oi is not None


def test_a_counterpart_outside_its_lookback_is_not_attached() -> None:
    policy = EligibilityPolicy(news_lookback_ms=60_000, oi_lookback_ms=60_000)
    assert _fuse(_oi(at=NOW), _news(at=NOW - 3_600_000), policy=policy).kind == "oi_only"  # type: ignore[union-attr]
    assert _fuse(_oi(at=NOW - 3_600_000), _news(at=NOW), policy=policy).kind == "news_only"  # type: ignore[union-attr]


def test_a_news_only_case_takes_its_side_from_the_model_not_a_quadrant() -> None:
    """There is no OI frame, so there is no quadrant. Deriving the side from one made the kind dead."""

    from tracefold.trading import TradeDecision

    outcome = decide(
        case_kind="news_only",
        mode="paper",
        regime=OiRegime.UNCLEAR,
        decision=TradeDecision(
            decision="long",
            directness="direct",
            surprise=3,
            price_in=0,
            alignment="aligned",
            horizon="hours",
            reason_code="none",
            thesis_zh="x",
            invalidation_zh="y",
        ),
        whale_long_profit_bps=None,
        oi_value_usd=None,
    )
    assert (outcome.decision, outcome.rule) == ("long", "news_only_paper_model_side")


def test_a_news_only_case_still_never_reaches_live() -> None:
    assert (
        pre_model_reject(
            case_kind="news_only",
            mode="live_bounded",
            regime=OiRegime.UNCLEAR,
            whale_long_profit_bps=None,
            oi_value_usd=None,
        ).rule
        == "news_only_never_live"
    )  # type: ignore[union-attr]


# ---------------------------------------------------------------------------- 17: gates before the model
@pytest.mark.parametrize(
    ("regime", "policy", "expected"),
    [
        (OiRegime.DELEVERAGING_UP, TradePolicy(), "regime_no_entry:deleveraging_up"),
        (OiRegime.BUILDUP_DOWN, TradePolicy(), "short_disabled_long_only"),
        (OiRegime.BUILDUP_UP, TradePolicy(min_whale_long_profit_bps=9_900), "whale_long_profit_below_floor"),
        (OiRegime.BUILDUP_UP, TradePolicy(min_oi_value_usd=10**12), "oi_value_below_floor"),
    ],
)
def test_every_gate_that_needs_no_model_answer_refuses_before_a_call_is_spent(
    regime: OiRegime, policy: TradePolicy, expected: str
) -> None:
    """Three of the four quadrants used to burn the 12/day budget and then be refused by arithmetic."""

    early = pre_model_reject(
        case_kind="news_oi",
        mode="paper",
        regime=regime,
        whale_long_profit_bps=9_800,
        oi_value_usd=30_000_000,
        policy=policy,
    )
    assert early is not None
    assert early.rule == expected


def test_a_tradeable_quadrant_is_not_pre_rejected() -> None:
    assert (
        pre_model_reject(
            case_kind="news_oi",
            mode="paper",
            regime=OiRegime.BUILDUP_UP,
            whale_long_profit_bps=9_900,
            oi_value_usd=30_000_000,
        )
        is None
    )


# ---------------------------------------------------------------------------- 6: the unclosed candle
def test_the_candle_still_forming_is_not_treated_as_closed() -> None:
    """Both adapters synthesise `close_at_ms = open + interval`, so the live bar advertises a future close."""

    opened = NOW
    deadline = opened + 1_800_000
    forming = Bar(open_at_ms=opened + 1_500_000, close_at_ms=opened + 1_800_000, close=Decimal("90"))
    closed = Bar(open_at_ms=opened, close_at_ms=opened + 300_000, close=Decimal("100"))
    # `now` sits inside the forming bar: only the genuinely closed one may be consulted.
    exit_at = evaluate_paper_exit(
        side="buy",
        entry=Decimal("100"),
        stop_price=Decimal("98"),
        take_profit_price=None,
        opened_at_ms=opened,
        must_close_at_ms=deadline,
        bars=[closed, forming],
        now_ms=opened + 1_600_000,
    )
    assert exit_at is None  # the stop on the forming bar must not fire, and the deadline has not passed


def test_no_usable_bar_returns_none_so_the_caller_owns_the_no_price_path() -> None:
    assert (
        evaluate_paper_exit(
            side="buy",
            entry=Decimal("100"),
            stop_price=Decimal("98"),
            take_profit_price=None,
            opened_at_ms=NOW,
            must_close_at_ms=NOW + 1_000,
            bars=[],
            now_ms=NOW + 10_000_000,
        )
        is None
    )


# ---------------------------------------------------------------------------- 22: closed funnel vocabulary
def test_an_operator_reason_never_becomes_a_new_funnel_key() -> None:
    assert blacklist_rule("benchmark_large_cap") == "blacklisted:benchmark_large_cap"
    assert blacklist_rule("SOL/USDT depeg incident, see #4412") == "blacklisted:operator"
    assert blacklist_rule("") == "blacklisted:operator"


# ---------------------------------------------------------------------------- one declared ceiling
def test_the_exit_ceiling_is_declared_once_in_python_and_matches_the_schema() -> None:
    """Three copies of a number is two too many, and the dead one is what gets edited."""

    from pathlib import Path

    from tracefold.trading.repository import _MAX_EXIT_ATTEMPTS

    migration = (
        Path(__file__).resolve().parents[2]
        / "src/tracefold/platform/postgres/alembic/versions/20260823_0300_trading_core.py"
    ).read_text(encoding="utf-8")
    assert f"exit_attempt_total <= {_MAX_EXIT_ATTEMPTS}" in migration
    pipeline = (Path(__file__).resolve().parents[2] / "src/tracefold/trading/pipeline.py").read_text(encoding="utf-8")
    assert "_MAX_EXIT_ATTEMPTS" not in pipeline


def test_every_order_state_the_code_can_write_is_accepted_by_the_schema() -> None:
    """`trading_cases` always had a state CHECK; `trading_orders` did not.

    A typo would have been stored, matched no reconcile branch, and left the order outside the state
    machine while still inside the active-underlying index.
    """

    from pathlib import Path

    from tracefold.trading import ACTIVE_ORDER_STATES, TERMINAL_ORDER_STATES
    from tracefold.trading.models import OrderState

    migration = (
        Path(__file__).resolve().parents[2]
        / "src/tracefold/platform/postgres/alembic/versions/20260823_0300_trading_core.py"
    ).read_text(encoding="utf-8")
    predicate = migration.split("trading_orders_state_check CHECK (state IN (", 1)[1].split("))", 1)[0]
    for state in OrderState:
        assert f"'{state.value}'" in predicate, state.value
    for state in (*ACTIVE_ORDER_STATES, *TERMINAL_ORDER_STATES):
        assert f"'{state}'" in predicate, state
