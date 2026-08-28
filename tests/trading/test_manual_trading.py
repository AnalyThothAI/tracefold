"""Public manual-trading preview and modification-guard contracts."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tracefold.trading import (
    ManualRiskConfig,
    ManualStrategyPresetConfig,
    ManualTradeIntent,
    ManualTradeParameters,
    ManualTradeSource,
    ModificationGuardState,
    StrategyPreset,
    TradeSide,
    build_manual_trade_preview,
    create_manual_trade_intent,
    guard_manual_trade_modification,
    recommend_manual_trade,
)


@pytest.fixture
def risk_config() -> ManualRiskConfig:
    return ManualRiskConfig(
        notional_deviation_limit_bps=5_000,
        tight_stop_deviation_limit_bps=5_000,
        wide_stop_deviation_limit_bps=10_000,
        max_account_risk_bps=1_000,
        high_risk_loss_multiple_bps=15_000,
        min_leverage=1,
        max_leverage=20,
    )


def test_tight_stop_long_preview_states_money_and_account_impact() -> None:
    preview = build_manual_trade_preview(
        side=TradeSide.LONG,
        venue="binance_usdm_demo",
        account_equity=Decimal("10240"),
        reference_entry=Decimal("112430"),
        parameters=ManualTradeParameters(
            notional_usd=Decimal("150000"),
            leverage=15,
            stop_loss_bps=50,
            take_profit_bps=200,
        ),
    )

    assert preview.margin_usd == Decimal("10000.00")
    assert preview.stop_loss_price == Decimal("111867.85")
    assert preview.take_profit_price == Decimal("114678.60")
    assert preview.estimated_loss_usd == Decimal("750.00")
    assert preview.estimated_profit_usd == Decimal("3000.00")
    assert preview.account_risk_bps == 732
    assert preview.potential_account_return_bps == 2_929
    assert preview.liquidation_distance_bps is None


def test_short_preview_places_stop_above_and_take_profit_below_entry() -> None:
    preview = build_manual_trade_preview(
        side=TradeSide.SHORT,
        venue="binance_usdm_demo",
        account_equity=Decimal("5000"),
        reference_entry=Decimal("100"),
        parameters=ManualTradeParameters(
            notional_usd=Decimal("1000"),
            leverage=2,
            stop_loss_bps=500,
            take_profit_bps=1500,
        ),
    )

    assert preview.stop_loss_price == Decimal("105.00")
    assert preview.take_profit_price == Decimal("85.00")
    assert preview.estimated_loss_usd == Decimal("50.00")
    assert preview.estimated_profit_usd == Decimal("150.00")


def test_combined_notional_and_stop_expansion_requires_high_risk_confirmation(
    risk_config: ManualRiskConfig,
) -> None:
    recommended = ManualTradeParameters(
        notional_usd=Decimal("1000"),
        leverage=2,
        stop_loss_bps=100,
        take_profit_bps=200,
    )
    modified = ManualTradeParameters(
        notional_usd=Decimal("1500"),
        leverage=2,
        stop_loss_bps=150,
        take_profit_bps=200,
    )

    decision = guard_manual_trade_modification(
        preset=StrategyPreset.TIGHT_STOP,
        account_equity=Decimal("1000"),
        recommended=recommended,
        modified=modified,
        config=risk_config,
    )

    assert decision.state is ModificationGuardState.HIGH_RISK_CONFIRMATION
    assert decision.notional_deviation_bps == 5_000
    assert decision.stop_loss_deviation_bps == 5_000
    assert decision.original_max_loss_usd == Decimal("10.00")
    assert decision.modified_max_loss_usd == Decimal("22.50")
    assert decision.max_loss_change_bps == 12_500
    assert decision.modified_account_risk_bps == 225
    assert "combined_max_loss" in decision.reason_codes


def test_within_limits_and_without_combined_risk_is_accepted(risk_config: ManualRiskConfig) -> None:
    recommended = ManualTradeParameters(
        notional_usd=Decimal("1000"),
        leverage=2,
        stop_loss_bps=100,
        take_profit_bps=200,
    )
    modified = ManualTradeParameters(
        notional_usd=Decimal("1100"),
        leverage=2,
        stop_loss_bps=105,
        take_profit_bps=210,
    )

    decision = guard_manual_trade_modification(
        preset=StrategyPreset.TIGHT_STOP,
        account_equity=Decimal("1000"),
        recommended=recommended,
        modified=modified,
        config=risk_config,
    )

    assert decision.state is ModificationGuardState.ACCEPTED
    assert decision.reason_codes == ()


def test_unfunded_margin_or_out_of_range_leverage_is_rejected(risk_config: ManualRiskConfig) -> None:
    recommended = ManualTradeParameters(
        notional_usd=Decimal("1000"),
        leverage=2,
        stop_loss_bps=100,
        take_profit_bps=200,
    )
    modified = ManualTradeParameters(
        notional_usd=Decimal("2000"),
        leverage=1,
        stop_loss_bps=100,
        take_profit_bps=200,
    )

    decision = guard_manual_trade_modification(
        preset=StrategyPreset.TIGHT_STOP,
        account_equity=Decimal("1000"),
        recommended=recommended,
        modified=modified,
        config=risk_config,
    )

    assert decision.state is ModificationGuardState.REJECTED
    assert "insufficient_margin" in decision.reason_codes


def test_preset_recommendation_sizes_from_account_risk_and_caps_notional() -> None:
    recommendation = recommend_manual_trade(
        account_equity=Decimal("10000"),
        config=ManualStrategyPresetConfig(
            preset=StrategyPreset.TIGHT_STOP,
            leverage=10,
            stop_loss_bps=100,
            take_profit_bps=200,
            account_risk_bps=500,
            min_notional_usd=Decimal("10"),
            max_notional_usd=Decimal("25000"),
        ),
    )

    # 5% account risk at a 1% stop implies $50k, capped by the configured $25k bound.
    assert recommendation.parameters == ManualTradeParameters(
        notional_usd=Decimal("25000.00"),
        leverage=10,
        stop_loss_bps=100,
        take_profit_bps=200,
    )
    assert recommendation.estimated_max_loss_usd == Decimal("250.00")
    assert recommendation.account_risk_bps == 250
    assert len(recommendation.recommendation_sha256) == 64


def test_manual_intent_binds_source_recommendation_final_parameters_and_confirmation(
    risk_config: ManualRiskConfig,
) -> None:
    source = ManualTradeSource(
        news_event_id="event-42",
        delivery_target_sha256="a" * 64,
        delivery_message_id=4242,
        headline_zh="BTC ETF 净流入创纪录",
        base_symbol="BTC",
        side=TradeSide.LONG,
        source_observed_at_ms=1_788_000_000_000,
    )
    recommended = ManualTradeParameters(
        notional_usd=Decimal("1000"),
        leverage=2,
        stop_loss_bps=100,
        take_profit_bps=200,
    )
    modified = ManualTradeParameters(
        notional_usd=Decimal("1500"),
        leverage=2,
        stop_loss_bps=150,
        take_profit_bps=200,
    )
    guard = guard_manual_trade_modification(
        preset=StrategyPreset.TIGHT_STOP,
        account_equity=Decimal("1000"),
        recommended=recommended,
        modified=modified,
        config=risk_config,
    )

    first = create_manual_trade_intent(
        session_id="0198f3ae-76c0-77a1-a191-0d3f16842ea0",
        source=source,
        actor_user_id=123456789,
        account_ref="binance-manual-demo-1",
        venue="binance_usdm_demo",
        preset=StrategyPreset.TIGHT_STOP,
        recommended=recommended,
        selected=modified,
        reference_entry=Decimal("100"),
        account_equity=Decimal("1000"),
        guard=guard,
        confirmed_at_ms=1_788_000_010_000,
        high_risk_confirmed_at_ms=1_788_000_009_000,
    )
    second = ManualTradeIntent.model_validate(first.model_dump(mode="json"))

    assert first == second
    assert first.intent_id == second.intent_id
    assert first.source.news_event_id == "event-42"
    assert first.recommended != first.selected
    assert first.guard.state is ModificationGuardState.HIGH_RISK_CONFIRMATION
    assert first.high_risk_confirmed_at_ms == 1_788_000_009_000
    assert len(first.intent_id) == 64
