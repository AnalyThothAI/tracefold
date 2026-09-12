"""Durable entry ownership and frozen risk intent; Nautilus remains the OMS."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .execution_contracts import IDENTITY_PATTERN, MARKET_KEY_PATTERN, SHA256_PATTERN

TradePlanStatus = Literal["prepared", "entry_working", "open", "closing", "closed", "unresolved"]
ExitReason = Literal[
    "stop_filled",
    "take_profit",
    "time_exit",
    "operator_flatten",
    "protection_failure",
    "recovery_safety_flatten",
    "venue_unknown",
    "not_submitted",
]


class TradePlan(BaseModel):
    """One entry identity, committed before entry and retained until private terminal proof.

    Immutable intent is separate from the few lifecycle facts required across a restart. This
    is deliberately not an order/position mirror. Prices, fills and PnL remain observations.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)

    entry_id: str = Field(pattern=SHA256_PATTERN)
    source: Literal["signal", "manual"]
    case_id: str | None = Field(default=None, min_length=1, max_length=128)
    account_slot: str = Field(pattern=IDENTITY_PATTERN)
    runtime_mode_at_creation: Literal["paper", "live"]
    market_key: str = Field(pattern=MARKET_KEY_PATTERN)
    instrument_id: str = Field(min_length=1, max_length=128)
    direction: Literal["long", "short"]
    entry_client_order_id: str = Field(pattern=r"^tf[0-9a-f]{30}$")
    created_at_ns: int = Field(gt=0)
    entry_expires_at_ns: int = Field(gt=0)
    entry_quantity: Decimal = Field(gt=0)
    stop_distance_bps: int = Field(ge=1, le=5_000)
    risk_budget_usd: Decimal = Field(gt=0)
    max_leverage_at_creation: int = Field(ge=1, le=125)
    exit_policy_id: Literal["oi_fixed_v1"] = "oi_fixed_v1"
    take_profit_bps: int = Field(ge=1, le=50_000)
    max_holding_ns: int = Field(gt=0)
    status: TradePlanStatus = "prepared"
    opened_at_ns: int | None = Field(default=None, gt=0)
    terminal_at_ns: int | None = Field(default=None, gt=0)
    exit_reason: ExitReason | None = None
    history_gap_reason: str | None = Field(default=None, min_length=1, max_length=128)
    updated_at_ns: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_lifecycle(self) -> Self:
        if self.entry_expires_at_ns <= self.created_at_ns:
            raise ValueError("trade_plan_entry_expiry_invalid")
        if (self.source == "signal") != (self.case_id is not None):
            raise ValueError("trade_plan_source_invalid")
        if (self.status == "closed") != (self.terminal_at_ns is not None):
            raise ValueError("trade_plan_terminal_invalid")
        if self.updated_at_ns < self.created_at_ns:
            raise ValueError("trade_plan_update_clock_invalid")
        return self


__all__ = ["ExitReason", "TradePlan", "TradePlanStatus"]
