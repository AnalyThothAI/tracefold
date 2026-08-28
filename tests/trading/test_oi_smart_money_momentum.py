"""`oi_smart_money_momentum_v1`: the three-dimensional template, boundary by boundary (#265).

Every threshold here is one side of an inequality that the issue spells out in words, and each of these
tests is the pair that proves which side. `500` qualifies and `499` does not; `5001` qualifies and
`5000` does not; `1` qualifies and `0` does not; `0` qualifies and `-1` does not; `1000` qualifies and
`1001` does not. Getting one of them backwards would not fail loudly — it would quietly trade a cohort
nobody measured, or refuse the one that was.

The OI floor and the chasing ceiling moved in #273 (1000 → 500, 600 → 1000). The pairs below are the
only place those two numbers are asserted against a decision, so they are what stops the next edit
from moving a threshold without moving the boundary that proves it.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from tracefold.trading.contracts import (
    FrozenMarketContext,
    FrozenStrategyContext,
    OiRegime,
    OiTradeCandidate,
    RegimeAssessment,
)
from tracefold.trading.strategy.oi_smart_money_momentum import (
    OiSmartMoneyMomentumConfig,
    OiSmartMoneyMomentumStrategy,
)
from tracefold.trading.strategy.root import capital_strategy_id, strategies, strategy_from_manifest

NOW = 1_787_000_000_000
STRATEGY = OiSmartMoneyMomentumStrategy()


def _oi(**kwargs: Any) -> OiTradeCandidate:
    """TUT's real shape: 15.48% / 54.24% / 90.74% at $23.01M, `drop` on the reader's own 80% rule."""

    fields: dict[str, Any] = {
        "event_id": "e-tut",
        "observed_at_ms": NOW,
        "verdict_created_at_ms": NOW,
        "base_symbol": "TUT",
        "venue": "binance",
        "oi_direction": "rise",
        "oi_change_bps": 1_548,
        "oi_value_usd": 23_010_000,
        "whale_long_profit_bps": 9_074,
        "whale_oi_ratio_bps": 5_424,
        "rank_in_window": 1,
        "final_decision": "drop",
        "source_rule": "whale_ratio_below_threshold",
        "metric_version": "oi_signal_v1",
        "source_strategy_id": "1019",
        "source_contract_version": "opennews_oi_source_v1",
        "measurement_window_ms": 300_000,
        "learning_epoch": "program_v7",
        "program_version": "news_oi_signal_v1",
        "program_sha256": "a" * 64,
        "policy_version": "news_triage_policy_v10",
        "editorial_origin": "telemetry_deterministic",
        "editorial_sha256": "b" * 64,
        "scored_judgment_sha256": "c" * 64,
        "runtime_manifest_sha": "d" * 64,
    }
    fields.update(kwargs)
    return OiTradeCandidate(**fields)


def _context(*, pre_move_bps: int | None = 210, **kwargs: Any) -> FrozenStrategyContext:
    return FrozenStrategyContext(
        mode="paper",
        oi=_oi(**kwargs),
        regime=RegimeAssessment(
            # Deliberately `UNCLEAR` on some inputs: this strategy reads the pre-move itself and does
            # not consult the shared quadrant, whose band starts at a 100 bps minimum it rejects.
            regime=OiRegime.BUILDUP_UP,
            reason="quadrant",
            pre_move_bps=pre_move_bps,
            oi_direction="rise",
        ),
        market=FrozenMarketContext(
            mark_price=Decimal("0.1234"),
            observed_at_ms=NOW,
            pre_move_bps=pre_move_bps,
            pre_move_lookback_ms=3_600_000,
        ),
    )


def test_the_real_tut_frame_is_the_long_this_strategy_exists_for() -> None:
    """The frame #265's Definition of Done names, and the one the reader's 80% rule dropped."""

    outcome = STRATEGY.evaluate(_context())
    assert (outcome.decision, outcome.rule) == ("long", "smart_money_momentum_long")
    assert outcome.permission == "paper"
    assert outcome.expected_horizon == "minutes"
    # The reader dropped it. That is recorded on the candidate and decides nothing here (#264).
    assert outcome.setup.startswith("5m 持仓上升 15.48%")


@pytest.mark.parametrize(
    ("kwargs", "rule"),
    [
        ({"oi_change_bps": 499}, "smart_money_oi_change_below_floor"),
        ({"whale_oi_ratio_bps": 5_000}, "smart_money_ratio_below_or_equal_floor"),
        ({"whale_long_profit_bps": 0}, "smart_money_profit_not_positive"),
        ({"oi_direction": "fall"}, "not_oi_rise"),
        ({"measurement_window_ms": 900_000}, "source_window_mismatch"),
        ({"measurement_window_ms": None}, "source_window_mismatch"),
    ],
)
def test_each_condition_refuses_by_name_one_step_below_its_threshold(kwargs: dict[str, Any], rule: str) -> None:
    outcome = STRATEGY.evaluate(_context(**kwargs))
    assert (outcome.decision, outcome.rule) == ("no_trade", rule)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"oi_change_bps": 500},
        {"whale_oi_ratio_bps": 5_001},
        {"whale_long_profit_bps": 1},
    ],
)
def test_each_condition_passes_exactly_at_its_threshold(kwargs: dict[str, Any]) -> None:
    assert STRATEGY.evaluate(_context(**kwargs)).decision == "long"


def test_the_cohort_the_lower_oi_floor_opened_is_the_one_production_was_stopping_on() -> None:
    """#273's reason for 5%: every OI Case in production had stopped on the old 1000 bps floor.

    These are the frozen `oi_change_bps` of real Cases from 2026-08-26/27, all of which carried a
    ratio and a profit that already qualified. Under the template's 10% every one of them was a
    `smart_money_oi_change_below_floor`; the floor is the only thing this asserts has changed.
    """

    for oi_change_bps in (533, 701, 791, 662, 541):
        outcome = STRATEGY.evaluate(_context(oi_change_bps=oi_change_bps))
        assert (outcome.decision, outcome.rule) == ("long", "smart_money_momentum_long")
    # And the floor is still a floor: the 313 bps frame from the same batch stays refused.
    assert STRATEGY.evaluate(_context(oi_change_bps=313)).rule == "smart_money_oi_change_below_floor"


@pytest.mark.parametrize(
    ("pre_move_bps", "decision", "rule"),
    [
        (-1, "no_trade", "price_direction_not_confirmed"),
        (0, "long", "smart_money_momentum_long"),
        (600, "long", "smart_money_momentum_long"),
        (1_000, "long", "smart_money_momentum_long"),
        (1_001, "no_trade", "move_above_band_chasing"),
        (None, "no_trade", "price_direction_not_confirmed"),
    ],
)
def test_the_price_band_is_zero_to_one_thousand_inclusive(pre_move_bps: int | None, decision: str, rule: str) -> None:
    """The 0 bps minimum is one change, and the 1000 bps ceiling is #273's.

    The lane's shared regime band starts at 100 bps and, until #264, was applied at the freeze — so a
    frame with a 0.4% pre-move never reached a strategy at all. A hidden 1% minimum here would be a
    second, unmeasured entry condition on top of the three the template names.

    601 used to be the first refusal and is now a long: the 6-10% slice this opened is the one
    `docs/research/oi-agent-design-2026-08-22.md` §1.6 measured at -0.77% over 4 h. The strategy is
    where that decision lives, so this is where it is asserted.
    """

    outcome = STRATEGY.evaluate(_context(pre_move_bps=pre_move_bps))
    assert (outcome.decision, outcome.rule) == (decision, rule)
    assert STRATEGY.evaluate(_context(pre_move_bps=50)).decision == "long"


def test_a_fall_frame_can_never_produce_a_long_and_no_input_produces_a_short() -> None:
    """Long-only by construction, not by a flag (#265 §10).

    There is no `short_disabled` rule because there is no path to a short: a `fall` frame stops at
    `not_oi_rise` before a side exists, and a negative pre-move stops at the direction check. Flipping
    the lane switch changes no decision, which is what makes "short is off" a property rather than a
    setting someone can forget.
    """

    permissive = OiSmartMoneyMomentumStrategy(OiSmartMoneyMomentumConfig(allow_short=True))
    for direction in ("rise", "fall"):
        for pre_move in (-500, -1, 0, 210, 601):
            context = _context(pre_move_bps=pre_move, oi_direction=direction)
            strict_outcome = STRATEGY.evaluate(context)
            assert strict_outcome.decision in {"long", "no_trade"}
            assert permissive.evaluate(context).decision == strict_outcome.decision
            if direction == "fall":
                assert strict_outcome.rule == "not_oi_rise"


def test_the_liquidity_floor_is_not_this_strategys_and_never_was() -> None:
    """#264 gave the absolute OI floor one owner. A $1M frame is the Candidate Gate's to refuse."""

    assert "min_oi_value_usd" not in STRATEGY.config_snapshot
    assert STRATEGY.evaluate(_context(oi_value_usd=1_000_000)).decision == "long"


def test_an_oi_trigger_routes_here_whether_or_not_news_attached() -> None:
    for has_news in (False, True):
        assert capital_strategy_id(trigger_kind="oi", has_oi=True, has_news=has_news) == "oi_smart_money_momentum_v1"
    # And the old identity is still decodable, so historical Cases stay replayable (#265 §5.1).
    assert "oi_momentum_v1" in strategies()


def test_a_config_edit_moves_the_digest_and_only_affects_new_cases() -> None:
    baseline = OiSmartMoneyMomentumStrategy()
    stricter = OiSmartMoneyMomentumStrategy(OiSmartMoneyMomentumConfig(min_oi_change_bps=1_500))
    assert baseline.config_digest != stricter.config_digest
    assert set(baseline.config_snapshot) == {
        "allow_short",
        "max_price_move_bps",
        "measurement_window_ms",
        "min_oi_change_bps",
        "min_price_move_bps",
        "min_whale_long_profit_bps",
        "min_whale_oi_ratio_bps",
    }


def test_the_frozen_config_rebuilds_the_exact_strategy_that_decided_the_case() -> None:
    """A Case decided under one set of numbers must never be replayed under today's."""

    from tracefold.trading.contracts import InstrumentRef, OiMarketTrigger, TradingCaseManifest

    strategy = OiSmartMoneyMomentumStrategy(OiSmartMoneyMomentumConfig(min_oi_change_bps=1_500))
    context = _context()
    manifest = TradingCaseManifest(
        primary_trigger=OiMarketTrigger(
            source_key="oi:e-tut:oi_signal_v1",
            observed_at_ms=NOW,
            persisted_at_ms=NOW,
            venue="binance",
        ),
        contexts=context,
        strategy_id=strategy.strategy_id,
        strategy_version=strategy.strategy_version,
        strategy_config=strategy.config_snapshot,
        strategy_config_digest=strategy.config_digest,
        underlying_key="crypto:TUT",
        base_symbol="TUT",
        cutoff_ms=NOW,
        instrument=InstrumentRef(
            exchange_id="binance",
            venue="binance.perp",
            provider_symbol="TUTUSDT",
            base_symbol="TUT",
            instrument_class="crypto",
            observed_at_ms=NOW,
        ),
    )
    rebuilt = strategy_from_manifest(manifest)
    assert rebuilt is not None
    assert rebuilt.config_snapshot == strategy.config_snapshot
    assert rebuilt.evaluate(context).decision == "long"  # 15.48% clears this Case's own 15% floor
    # A frame between the two thresholds separates them, which is the whole point of freezing the config.
    between = _context(oi_change_bps=1_200)
    assert STRATEGY.evaluate(between).decision == "long"
    assert rebuilt.evaluate(between).rule == "smart_money_oi_change_below_floor"
