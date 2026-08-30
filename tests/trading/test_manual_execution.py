from __future__ import annotations

from decimal import Decimal

from tracefold.trading import (
    ManualModificationGuard,
    ManualTradeIntent,
    ManualTradeParameters,
    ManualTradeSource,
    ManualVenueInstrument,
    ModificationGuardState,
    StrategyPreset,
    TradeSide,
    create_manual_trade_intent,
)
from tracefold.trading.manual_execution import build_manual_execution_plan


def _intent(*, side: TradeSide = TradeSide.LONG) -> ManualTradeIntent:
    parameters = ManualTradeParameters(
        notional_usd=Decimal("150000"),
        leverage=15,
        stop_loss_bps=50,
        take_profit_bps=200,
    )
    return create_manual_trade_intent(
        session_id="0198f3ae-76c0-77a1-a191-0d3f16842ea0",
        source=ManualTradeSource(
            news_event_id="event-42",
            delivery_target_sha256="a" * 64,
            delivery_message_id=42,
            headline_zh="BTC ETF 净流入创纪录",
            base_symbol="BTC",
            side=side,
            source_observed_at_ms=1_900_000_000_000,
        ),
        actor_user_id=123456789,
        account_ref="binance-manual-live-1",
        venue="binance_usdm_live",
        preset=StrategyPreset.TIGHT_STOP,
        recommended=parameters,
        selected=parameters,
        reference_entry=Decimal("60000"),
        account_equity=Decimal("10000"),
        guard=ManualModificationGuard(
            state=ModificationGuardState.ACCEPTED,
            notional_deviation_bps=0,
            stop_loss_deviation_bps=0,
            take_profit_deviation_bps=0,
            original_max_loss_usd=Decimal("750"),
            modified_max_loss_usd=Decimal("750"),
            max_loss_change_bps=0,
            modified_account_risk_bps=750,
        ),
        confirmed_at_ms=1_900_000_000_100,
    )


def _instrument() -> ManualVenueInstrument:
    return ManualVenueInstrument(
        symbol="BTCUSDT",
        tick_size=Decimal("0.10"),
        quantity_step=Decimal("0.001"),
        min_quantity=Decimal("0.001"),
        min_notional=Decimal("5"),
    )


def test_execution_plan_quantizes_once_and_derives_stable_per_leg_client_ids() -> None:
    intent = _intent()

    plan = build_manual_execution_plan(intent, _instrument())

    assert plan.symbol == "BTCUSDT"
    assert plan.entry_side == "BUY" and plan.close_side == "SELL"
    assert plan.quantity == Decimal("2.500")
    assert plan.stop_loss_trigger == Decimal("59700.00")
    assert plan.take_profit_trigger == Decimal("61200.00")
    assert plan.entry_client_order_id.startswith("tfm-e-")
    assert plan.take_profit_client_order_id.startswith("tfm-t-")
    assert plan.stop_loss_client_order_id.startswith("tfm-s-")
    assert len({plan.entry_client_order_id, plan.take_profit_client_order_id, plan.stop_loss_client_order_id}) == 3
    assert all(
        len(value) <= 36
        for value in (
            plan.entry_client_order_id,
            plan.take_profit_client_order_id,
            plan.stop_loss_client_order_id,
        )
    )


def test_short_execution_plan_reverses_sides_and_trigger_directions() -> None:
    plan = build_manual_execution_plan(_intent(side=TradeSide.SHORT), _instrument())

    assert plan.entry_side == "SELL" and plan.close_side == "BUY"
    assert plan.stop_loss_trigger == Decimal("60300.00")
    assert plan.take_profit_trigger == Decimal("58800.00")
