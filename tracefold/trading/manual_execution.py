"""Deterministic venue-neutral execution plan for one confirmed manual intent."""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .contracts import canonical_sha256
from .manual import ManualTradeIntent, TradeSide

_BPS = Decimal(10_000)


class ManualInstrumentRules(Protocol):
    @property
    def symbol(self) -> str: ...

    @property
    def tick_size(self) -> Decimal: ...

    @property
    def quantity_step(self) -> Decimal: ...

    @property
    def min_quantity(self) -> Decimal: ...

    @property
    def min_notional(self) -> Decimal: ...


class ManualExecutionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_version: Literal["manual_execution_plan_v1"] = "manual_execution_plan_v1"
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    intent_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    symbol: str = Field(pattern=r"^[A-Z0-9]{2,40}$")
    entry_side: Literal["BUY", "SELL"]
    close_side: Literal["BUY", "SELL"]
    quantity: Decimal
    leverage: int = Field(ge=1, le=125)
    stop_loss_trigger: Decimal
    take_profit_trigger: Decimal
    entry_client_order_id: str = Field(min_length=1, max_length=36)
    take_profit_client_order_id: str = Field(min_length=1, max_length=36)
    stop_loss_client_order_id: str = Field(min_length=1, max_length=36)

    @model_validator(mode="after")
    def validate_identity(self) -> ManualExecutionPlan:
        if self.plan_sha256 != canonical_sha256(self.immutable_payload):
            raise ValueError("manual_execution_plan_identity_invalid")
        return self

    @property
    def immutable_payload(self) -> dict[str, object]:
        return {
            "plan_version": self.plan_version,
            "intent_id": self.intent_id,
            "symbol": self.symbol,
            "entry_side": self.entry_side,
            "close_side": self.close_side,
            "quantity": str(self.quantity),
            "leverage": self.leverage,
            "stop_loss_trigger": str(self.stop_loss_trigger),
            "take_profit_trigger": str(self.take_profit_trigger),
            "entry_client_order_id": self.entry_client_order_id,
            "take_profit_client_order_id": self.take_profit_client_order_id,
            "stop_loss_client_order_id": self.stop_loss_client_order_id,
        }


def build_manual_execution_plan(
    intent: ManualTradeIntent,
    instrument: ManualInstrumentRules,
) -> ManualExecutionPlan:
    expected_symbol = f"{intent.source.base_symbol}USDT"
    if instrument.symbol != expected_symbol:
        raise ValueError("manual_execution_instrument_mismatch")
    quantity = _round_down(intent.selected.notional_usd / intent.reference_entry, instrument.quantity_step)
    if quantity < instrument.min_quantity or quantity * intent.reference_entry < instrument.min_notional:
        raise ValueError("manual_execution_order_below_minimum")
    if intent.source.side is TradeSide.LONG:
        entry_side: Literal["BUY", "SELL"] = "BUY"
        close_side: Literal["BUY", "SELL"] = "SELL"
        stop = intent.reference_entry * (_BPS - Decimal(intent.selected.stop_loss_bps)) / _BPS
        take_profit = intent.reference_entry * (_BPS + Decimal(intent.selected.take_profit_bps)) / _BPS
    else:
        entry_side = "SELL"
        close_side = "BUY"
        stop = intent.reference_entry * (_BPS + Decimal(intent.selected.stop_loss_bps)) / _BPS
        take_profit = intent.reference_entry * (_BPS - Decimal(intent.selected.take_profit_bps)) / _BPS
    if take_profit <= 0:
        raise ValueError("manual_execution_take_profit_invalid")
    identity = intent.intent_id[:24]
    payload: dict[str, object] = {
        "plan_version": "manual_execution_plan_v1",
        "intent_id": intent.intent_id,
        "symbol": instrument.symbol,
        "entry_side": entry_side,
        "close_side": close_side,
        "quantity": str(quantity),
        "leverage": intent.selected.leverage,
        "stop_loss_trigger": str(_round_nearest(stop, instrument.tick_size)),
        "take_profit_trigger": str(_round_nearest(take_profit, instrument.tick_size)),
        "entry_client_order_id": f"tfm-e-{identity}",
        "take_profit_client_order_id": f"tfm-t-{identity}",
        "stop_loss_client_order_id": f"tfm-s-{identity}",
    }
    return ManualExecutionPlan(plan_sha256=canonical_sha256(payload), **payload)


def _round_down(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ValueError("manual_execution_step_invalid")
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def _round_nearest(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ValueError("manual_execution_step_invalid")
    return (value / step).to_integral_value(rounding=ROUND_HALF_UP) * step


__all__ = ["ManualExecutionPlan", "ManualInstrumentRules", "build_manual_execution_plan"]
