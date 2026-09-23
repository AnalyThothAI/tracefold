"""Durable entry intent: the one Trading fact a Runtime writes before it may send an entry order.

Nautilus owns every order and position (#680). A plan is not a mirror of either: it records what was
intended when the entry was admitted -- instrument, direction, sized quantity, stop and take-profit
distance, maximum holding time -- and the three lifecycle facts a restart or a watchdog needs from
PostgreSQL rather than from process memory: when the position opened, when the plan ended, and why.
Prices, fills, fees and realized PnL are observations; the read models fold them from the fill journal.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .execution_contracts import IDENTITY_PATTERN, MARKET_KEY_PATTERN, SHA256_PATTERN

TradePlanStatus = Literal["prepared", "open", "closed"]
# Why a plan ended. `external` is a close this Runtime observed but did not originate (a venue-side
# close, a liquidation, a manual order on the venue); `venue_unknown` is a plan whose end this Runtime
# never observed because the account was already flat for it when it looked; `not_submitted` is an
# entry the venue (or the pre-submit risk check) refused.
ExitReason = Literal[
    "stop_filled",
    "take_profit",
    "time_exit",
    "operator_flatten",
    "external",
    "venue_unknown",
    "not_submitted",
]


class TradePlan(BaseModel):
    """One entry identity, committed before its entry order exists and closed once, never reopened."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)

    entry_id: str = Field(pattern=SHA256_PATTERN)
    entry_scope_id: str = Field(min_length=1, max_length=128)
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
    exit_policy_id: Literal["oi_fixed_v1", "analysis_dynamic_v1"] = "oi_fixed_v1"
    take_profit_bps: int = Field(ge=1, le=50_000)
    max_holding_ns: int = Field(gt=0)
    status: TradePlanStatus = "prepared"
    opened_at_ns: int | None = Field(default=None, gt=0)
    terminal_at_ns: int | None = Field(default=None, gt=0)
    exit_reason: ExitReason | None = None
    updated_at_ns: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_lifecycle(self) -> Self:
        if self.entry_expires_at_ns <= self.created_at_ns:
            raise ValueError("trade_plan_entry_expiry_invalid")
        if (self.source == "signal") != (self.case_id is not None):
            raise ValueError("trade_plan_source_invalid")
        if (self.status == "closed") != (self.terminal_at_ns is not None):
            raise ValueError("trade_plan_terminal_invalid")
        if (self.status == "closed") != (self.exit_reason is not None):
            raise ValueError("trade_plan_exit_reason_invalid")
        if self.status == "open" and self.opened_at_ns is None:
            raise ValueError("trade_plan_open_clock_invalid")
        if self.updated_at_ns < self.created_at_ns:
            raise ValueError("trade_plan_update_clock_invalid")
        return self

    def opened(self, *, opened_at_ns: int, now_ns: int) -> TradePlan:
        """This plan's position exists; the open clock is written once and never moves."""

        return self.model_copy(
            update={
                "status": "open",
                "opened_at_ns": self.opened_at_ns or opened_at_ns,
                "updated_at_ns": max(self.updated_at_ns, now_ns),
            }
        )

    def closed(self, *, reason: ExitReason, terminal_at_ns: int, now_ns: int) -> TradePlan:
        return self.model_copy(
            update={
                "status": "closed",
                "exit_reason": reason,
                "terminal_at_ns": max(terminal_at_ns, self.created_at_ns),
                "updated_at_ns": max(self.updated_at_ns, now_ns, terminal_at_ns),
            }
        )


__all__ = ["ExitReason", "TradePlan", "TradePlanStatus"]
