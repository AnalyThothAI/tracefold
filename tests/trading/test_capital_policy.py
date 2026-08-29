"""`binance_oi_smart_money_long_v2`: the one production capital policy, and its frozen evidence.

The arithmetic is carried over unchanged from `oi_smart_money_momentum_v1`, deliberately — #331 is a
product hard cut, not a threshold move — so these tests pin exactly the boundaries #273 shipped, plus
the two things the new identity adds: it cannot express a permission, and every condition it executes
is written down with its threshold, its operator and the value it measured.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from tracefold.trading.contracts import (
    FrozenMarketContext,
    FrozenPolicyContext,
    OiTradeCandidate,
)
from tracefold.trading.policy import CAPITAL_POLICY, CAPITAL_POLICY_ID, CapitalPolicy, CapitalPolicyConfig

DIGEST = "f" * 64


def _oi(**overrides: Any) -> OiTradeCandidate:
    values: dict[str, Any] = {
        "event_id": "evt-1",
        "observed_at_ms": 1_787_000_000_000,
        "verdict_created_at_ms": 1_787_000_001_000,
        "base_symbol": "TUT",
        "venue": "binance",
        "oi_direction": "rise",
        "oi_change_bps": 900,
        "oi_value_usd": 40_000_000,
        "whale_long_profit_bps": 3_000,
        "whale_oi_ratio_bps": 6_000,
        "rank_in_window": 1,
        "final_decision": "push",
        "source_rule": "oi_whale_ratio",
        "metric_version": "oi_signal_v1",
        "measurement_window_ms": 300_000,
        "source_strategy_id": "opennews_oi_v1",
        "source_contract_version": "oi_source_contract_v1",
        "learning_epoch": "epoch-1",
        "program_version": "news_oi_signal_v2",
        "program_sha256": DIGEST,
        "policy_version": "news_triage_policy_v11",
        "judgment_contract_version": "news_judgment_v2",
        "judgment_origin": "oi",
        "judgment_sha256": DIGEST,
        "runtime_manifest_sha": DIGEST,
    }
    values.update(overrides)
    return OiTradeCandidate(**values)


def _context(pre_move_bps: int | None = 200, **oi_overrides: Any) -> FrozenPolicyContext:
    return FrozenPolicyContext(
        oi=_oi(**oi_overrides),
        market=FrozenMarketContext(
            mark_price=Decimal("1.25"),
            observed_at_ms=1_787_000_000_000,
            pre_move_bps=pre_move_bps,
            pre_move_lookback_ms=3_600_000,
        ),
    )


def test_the_shipped_identity_is_the_one_name_the_lane_and_the_replay_share() -> None:
    assert CAPITAL_POLICY.policy_id == CAPITAL_POLICY_ID == "binance_oi_smart_money_long_v2"
    assert CAPITAL_POLICY.policy_version == CAPITAL_POLICY_ID
    # The digest is half a Case's identity: editing a threshold starts a new record rather than
    # rewriting what the previous threshold decided.
    assert CAPITAL_POLICY.config_digest == CapitalPolicyConfig().digest
    assert set(CAPITAL_POLICY.config_snapshot) == {
        "max_price_move_bps",
        "measurement_window_ms",
        "min_oi_change_bps",
        "min_price_move_bps",
        "min_whale_long_profit_bps",
        "min_whale_oi_ratio_bps",
    }


def test_a_qualifying_frame_is_long_and_carries_every_check_it_passed() -> None:
    decision = CAPITAL_POLICY.decide(_context())
    assert decision.decision == "long"
    assert decision.rule == "smart_money_momentum_long"
    assert [check.check for check in decision.checks] == [
        "source_measurement_window_ms",
        "oi_direction",
        "oi_change_bps",
        "whale_oi_ratio_bps",
        "whale_long_profit_bps",
        "pre_move_bps",
        "pre_move_bps",
    ]
    assert all(check.passed for check in decision.checks)
    # The measured value travels with the threshold, so a console holding only today's configuration
    # can still explain a Case frozen a week ago.
    ratio = next(check for check in decision.checks if check.check == "whale_oi_ratio_bps")
    assert (ratio.operator, ratio.threshold, ratio.measured) == (">", "5000", "6000")


def test_the_answer_is_only_long_or_no_trade_and_carries_no_permission() -> None:
    """A policy that could name a permission is a capital authority in a strategy string (#331)."""

    decision = CAPITAL_POLICY.decide(_context())
    assert decision.decision in {"long", "no_trade"}
    assert not hasattr(decision, "permission")
    assert not hasattr(decision, "mode")
    assert "permission" not in decision.evidence()
    assert "execution_environment" not in decision.evidence()


def test_the_three_template_conditions_are_inclusive_exactly_as_written() -> None:
    """`min_` reads `>=`; the two `above` conditions read `>`. 500 qualifies, 499 does not."""

    assert CAPITAL_POLICY.decide(_context(oi_change_bps=500)).decision == "long"
    assert CAPITAL_POLICY.decide(_context(oi_change_bps=499)).rule == "smart_money_oi_change_below_floor"
    assert CAPITAL_POLICY.decide(_context(whale_oi_ratio_bps=5_001)).decision == "long"
    assert CAPITAL_POLICY.decide(_context(whale_oi_ratio_bps=5_000)).rule == ("smart_money_ratio_below_or_equal_floor")
    assert CAPITAL_POLICY.decide(_context(whale_long_profit_bps=1)).decision == "long"
    assert CAPITAL_POLICY.decide(_context(whale_long_profit_bps=0)).rule == "smart_money_profit_not_positive"


def test_an_unproven_measurement_window_is_refused_before_any_number_is_read() -> None:
    decision = CAPITAL_POLICY.decide(_context(measurement_window_ms=None))
    assert decision.rule == "source_window_mismatch"
    assert [check.check for check in decision.checks] == ["source_measurement_window_ms"]
    assert decision.checks[0].measured is None


def test_a_falling_frame_never_reaches_a_side() -> None:
    assert CAPITAL_POLICY.decide(_context(oi_direction="fall")).rule == "not_oi_rise"


def test_the_price_band_is_a_band_and_a_missing_pre_move_is_no_evidence() -> None:
    assert CAPITAL_POLICY.decide(_context(pre_move_bps=0)).decision == "long"
    assert CAPITAL_POLICY.decide(_context(pre_move_bps=1_000)).decision == "long"
    assert CAPITAL_POLICY.decide(_context(pre_move_bps=1_001)).rule == "move_above_band_chasing"
    assert CAPITAL_POLICY.decide(_context(pre_move_bps=-1)).rule == "price_direction_not_confirmed"
    assert CAPITAL_POLICY.decide(_context(pre_move_bps=None)).rule == "price_direction_not_confirmed"


def test_a_refusal_carries_the_rules_that_passed_before_it() -> None:
    """The evidence is the whole executed sequence, not just the failing line."""

    decision = CAPITAL_POLICY.decide(_context(whale_long_profit_bps=0))
    assert [check.passed for check in decision.checks] == [True, True, True, True, False]
    assert decision.evidence()["decision"] == "no_trade"
    assert decision.evidence()["policy_id"] == CAPITAL_POLICY_ID


def test_editing_a_threshold_moves_the_digest_and_therefore_the_identity() -> None:
    tightened = CapitalPolicy(config=CapitalPolicyConfig(min_oi_change_bps=1_000))
    assert tightened.config_digest != CAPITAL_POLICY.config_digest
    assert tightened.decide(_context(oi_change_bps=900)).rule == "smart_money_oi_change_below_floor"
