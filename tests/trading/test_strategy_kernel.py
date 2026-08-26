"""#213: versioned pure strategies over one frozen point-in-time trigger context."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from tracefold.trading.contracts import (
    FrozenMarketContext,
    FrozenStrategyContext,
    InstrumentRef,
    LiquidationAggregate,
    LiquidationMarketTrigger,
    LiquidationSourceContract,
    LiquidationTradeCandidate,
    OiRegime,
    OiTradeCandidate,
    RegimeAssessment,
    TradingCaseManifest,
)
from tracefold.trading.strategy.liquidation_burst import (
    LiquidationContinuationConfig,
    LiquidationContinuationStrategy,
)
from tracefold.trading.strategy.liquidation_exhaustion import LiquidationExhaustionStrategy
from tracefold.trading.strategy.root import capital_strategy_id, strategies, strategy_from_manifest

NOW = 1_900_000_000_000


def _liquidation_context(*, complete: bool, exhaustion_features: bool = False) -> FrozenStrategyContext:
    fact = LiquidationTradeCandidate(
        source_key="a" * 64,
        item_id="item-1",
        fact_id="fact-1",
        base_symbol="DOGE",
        venue="binance",
        liquidated_position_side="short",
        forced_order_side="buy",
        notional_usd=Decimal("750000"),
        price=Decimal("0.12"),
        event_at_ms=NOW - 1_000,
        received_at_ms=NOW,
        parser_version="liquidation_parser_v1",
        source_contract=LiquidationSourceContract(
            provider_record_identity="provider-1",
            symbol_contract_identity="binance:DOGEUSDT",
            position_side_semantics="short=>forced_buy;long=>forced_sell",
            quantity_semantics="base_asset",
            notional_semantics="usd_execution_notional",
            price_semantics="execution_price",
            completeness_assumption="sequenced_complete_stream",
            throttle_assumption="none",
            source_contract_version="test_complete_v1",
            complete=complete,
        ),
    )
    market = FrozenMarketContext(
        mark_price=fact.price,
        observed_at_ms=NOW,
        pre_move_bps=250,
        pre_move_lookback_ms=3_600_000,
        price_momentum_bps=100,
        price_momentum_window_ms=60_000,
        displacement_bps=250,
        displacement_window_ms=60_000,
        spread_bps=5,
        depth_notional_usd=Decimal("2000000"),
        funding_bps=10,
    )
    return FrozenStrategyContext(
        mode="paper",
        liquidation=fact,
        liquidation_aggregate=LiquidationAggregate(
            window_ms=60_000,
            count=3,
            notional_usd=Decimal("1500000"),
            long_notional_usd=Decimal("0"),
            short_notional_usd=Decimal("1500000"),
            long_count=0,
            short_count=3,
            dominant_liquidated_side="short",
            dominant_share_bps=10_000,
            dominant_count=3,
            dominant_notional_usd=Decimal("1500000"),
            dominant_acceleration_bps=1_000,
            source_refs=("a" * 64, "b" * 64, "c" * 64),
        ),
        regime=RegimeAssessment(
            regime=OiRegime.UNCLEAR,
            reason="oi_absent",
            pre_move_bps=250,
            oi_direction=None,
        ),
        market=market,
        oi=OiTradeCandidate(
            event_id="oi-1",
            observed_at_ms=NOW - 5_000,
            verdict_created_at_ms=NOW - 4_000,
            base_symbol="DOGE",
            venue="binance",
            oi_direction="rise",
            oi_change_bps=200,
            oi_value_usd=50_000_000,
            whale_long_profit_bps=9_900,
            whale_oi_ratio_bps=5_000,
            rank_in_window=1,
            metric_version="oi_signal_v1",
            learning_epoch="program_v7",
            program_version="news_oi_signal_v1",
            program_sha256="1" * 64,
            policy_version="news_triage_policy_v10",
            editorial_origin="telemetry_deterministic",
            editorial_sha256="2" * 64,
            scored_judgment_sha256="3" * 64,
            runtime_manifest_sha="4" * 64,
        ),
        intensity_decelerating=exhaustion_features,
        oi_collapsing=exhaustion_features,
        price_stopped_extreme=exhaustion_features,
        liquidity_recovered=exhaustion_features,
    )


def test_same_manifest_context_and_strategy_config_produce_the_same_outcome() -> None:
    strategy = LiquidationContinuationStrategy()
    context = _liquidation_context(complete=True)
    assert strategy.evaluate(context) == strategy.evaluate(context)
    assert strategy.config_digest == LiquidationContinuationStrategy().config_digest


def test_strategy_config_change_moves_the_content_digest() -> None:
    baseline = LiquidationContinuationStrategy()
    stricter = LiquidationContinuationStrategy(LiquidationContinuationConfig(min_count=4))
    assert baseline.config_digest != stricter.config_digest


def test_manifest_freezes_replayable_config_values_not_only_a_digest() -> None:
    strategy = LiquidationContinuationStrategy()
    context = _liquidation_context(complete=True)
    manifest = TradingCaseManifest(
        primary_trigger=LiquidationMarketTrigger(
            source_key="a" * 64,
            observed_at_ms=NOW - 1_000,
            persisted_at_ms=NOW,
            venue="binance",
        ),
        contexts=context,
        strategy_id=strategy.strategy_id,
        strategy_version=strategy.strategy_version,
        strategy_config=strategy.config_snapshot,
        strategy_config_digest=strategy.config_digest,
        underlying_key="crypto:DOGE",
        base_symbol="DOGE",
        cutoff_ms=NOW,
        instrument=InstrumentRef(
            exchange_id="binance",
            venue="binance.perp",
            provider_symbol="DOGEUSDT",
            base_symbol="DOGE",
            instrument_class="crypto",
            observed_at_ms=NOW,
        ),
    )
    rebuilt = strategy_from_manifest(manifest)
    assert rebuilt is not None
    assert rebuilt.evaluate(context) == strategy.evaluate(context)
    assert rebuilt.config_snapshot == strategy.config_snapshot

    with pytest.raises(ValidationError, match="trading_strategy_config_digest_mismatch"):
        TradingCaseManifest.model_validate(
            {**manifest.model_dump(mode="json"), "strategy_config": {**strategy.config_snapshot, "min_count": 99}}
        )


def test_opennews_liquidation_contract_fails_closed_before_directional_hypotheses() -> None:
    context = _liquidation_context(complete=False)
    for strategy in (LiquidationContinuationStrategy(), LiquidationExhaustionStrategy()):
        outcome = strategy.evaluate(context)
        assert (outcome.decision, outcome.rule, outcome.permission) == (
            "no_trade",
            "source_contract_incomplete",
            "shadow",
        )


def test_continuation_and_exhaustion_are_opposite_named_hypotheses_when_evidence_is_complete() -> None:
    context = _liquidation_context(complete=True, exhaustion_features=True)
    continuation = LiquidationContinuationStrategy().evaluate(context)
    exhaustion = LiquidationExhaustionStrategy().evaluate(context)
    assert (continuation.decision, exhaustion.decision) == ("long", "short")
    assert continuation.rule != exhaustion.rule
    assert continuation.permission == exhaustion.permission == "shadow"


@pytest.mark.parametrize(
    ("aggregate_update", "market_update", "rule"),
    [
        ({"dominant_share_bps": 5_000}, {}, "burst_not_one_sided"),
        ({"dominant_acceleration_bps": None}, {}, "burst_acceleration_missing"),
        ({}, {"price_momentum_bps": -100}, "price_momentum_not_confirmed"),
        ({}, {"pre_move_bps": 251}, "pre_move_above_ceiling"),
    ],
)
def test_continuation_requires_the_declared_setup_features(
    aggregate_update: dict[str, object], market_update: dict[str, object], rule: str
) -> None:
    context = _liquidation_context(complete=True)
    aggregate = context.liquidation_aggregate
    assert aggregate is not None
    changed = context.model_copy(
        update={
            "liquidation_aggregate": aggregate.model_copy(update=aggregate_update),
            "market": context.market.model_copy(update=market_update),
        }
    )
    assert LiquidationContinuationStrategy().evaluate(changed).rule == rule


def test_opposite_flow_cannot_satisfy_the_dominant_one_sided_burst_floors() -> None:
    context = _liquidation_context(complete=True)
    aggregate = context.liquidation_aggregate
    assert aggregate is not None
    diluted = aggregate.model_copy(
        update={
            "count": 12,
            "notional_usd": Decimal("6000000"),
            "dominant_count": 1,
            "dominant_notional_usd": Decimal("250000"),
        }
    )
    outcome = LiquidationContinuationStrategy().evaluate(context.model_copy(update={"liquidation_aggregate": diluted}))
    assert outcome.rule == "burst_count_below_floor"


def test_exhaustion_uses_matching_displacement_not_the_one_hour_pre_move() -> None:
    context = _liquidation_context(complete=True, exhaustion_features=True)
    changed = context.model_copy(
        update={"market": context.market.model_copy(update={"pre_move_bps": 500, "displacement_bps": None})}
    )
    assert LiquidationExhaustionStrategy().evaluate(changed).rule == "extreme_displacement_missing"


def test_liquidation_trigger_can_never_select_a_capital_strategy() -> None:
    assert capital_strategy_id(trigger_kind="liquidation", has_oi=False, has_news=False) is None
    assert set(strategies()) == {
        "oi_momentum_v1",
        "news_oi_alignment_v1",
        "liquidation_continuation_shadow_v1",
        "liquidation_exhaustion_shadow_v1",
    }


def test_pure_strategy_modules_do_not_import_storage_providers_or_execution() -> None:
    strategy_dir = Path(__file__).parents[2] / "src" / "tracefold" / "trading" / "strategy"
    source = "\n".join(path.read_text(encoding="utf-8") for path in strategy_dir.glob("*.py"))
    for forbidden in ("storage", "integrations", "execution", "postgres", "requests", "httpx"):
        assert forbidden not in source
