"""Typed manual-position and close-request contracts for private Telegram portfolio views."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .manual import (
    MAX_DEVELOPMENT_TEST_NOTIONAL_USD,
    ManualTradeParameters,
    ManualTradeSource,
    StrategyPreset,
    TradeSide,
    is_development_test_source,
)


class ManualPositionState(StrEnum):
    OPEN = "OPEN"
    EXPOSED = "EXPOSED"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    MANUAL_REVIEW = "MANUAL_REVIEW"


class ManualCloseState(StrEnum):
    PENDING = "PENDING"
    SUBMITTING = "SUBMITTING"
    FILLED = "FILLED"
    AMBIGUOUS = "AMBIGUOUS"
    REJECTED = "REJECTED"


class _FrozenPortfolioModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ManualCloseRequest(_FrozenPortfolioModel):
    close_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    intent_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    session_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    requested_bps: Literal[3000, 5000, 10000]
    client_order_id: str = Field(pattern=r"^[.A-Z:/a-z0-9_-]{1,36}$")
    state: ManualCloseState
    target_quantity: Decimal | None = None
    attempted_at_ms: int | None = Field(default=None, gt=0)
    receipt: dict[str, object] | None = None
    reconciled_at_ms: int | None = Field(default=None, gt=0)
    error_code: str | None = Field(default=None, min_length=1, max_length=160)
    requested_at_ms: int = Field(gt=0)
    updated_at_ms: int = Field(gt=0)

    @field_validator("target_quantity", mode="before")
    @classmethod
    def parse_target_quantity(cls, value: object) -> Decimal | None:
        if value is None:
            return None
        parsed = Decimal(str(value))
        if not parsed.is_finite() or parsed <= 0:
            raise ValueError("manual_close_quantity_invalid")
        return parsed

    @model_validator(mode="after")
    def validate_state_shape(self) -> ManualCloseRequest:
        if self.updated_at_ms < self.requested_at_ms:
            raise ValueError("manual_close_time_invalid")
        if self.reconciled_at_ms is not None and (
            self.state is not ManualCloseState.FILLED or self.reconciled_at_ms < self.updated_at_ms
        ):
            raise ValueError("manual_close_reconciliation_time_invalid")
        if self.state is ManualCloseState.PENDING:
            valid = self.target_quantity is None and self.attempted_at_ms is None and self.receipt is None
        elif self.state is ManualCloseState.SUBMITTING:
            valid = self.target_quantity is not None and self.attempted_at_ms is not None and self.receipt is None
        elif self.state is ManualCloseState.FILLED:
            valid = self.target_quantity is not None and self.attempted_at_ms is not None and self.receipt is not None
        else:
            valid = self.error_code is not None
        if not valid:
            raise ValueError("manual_close_state_shape_invalid")
        return self


class ManualPositionView(_FrozenPortfolioModel):
    intent_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    session_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    source: ManualTradeSource
    account_ref: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    symbol: str = Field(pattern=r"^[A-Z0-9]{2,40}$")
    side: TradeSide
    preset: StrategyPreset
    recommended: ManualTradeParameters
    selected: ManualTradeParameters
    state: ManualPositionState
    quantity: Decimal
    entry_price: Decimal
    mark_price: Decimal
    unrealized_pnl_usd: Decimal
    leverage: int = Field(ge=1, le=125)
    liquidation_price: Decimal | None = None
    take_profit_price: Decimal
    stop_loss_price: Decimal
    opened_at_ms: int = Field(gt=0)
    observed_at_ms: int = Field(gt=0)
    closed_at_ms: int | None = Field(default=None, gt=0)
    exit_reason: str | None = Field(default=None, min_length=1, max_length=80)
    exit_price: Decimal | None = None
    realized_pnl_usd: Decimal | None = None
    active_close: ManualCloseRequest | None = None

    @field_validator(
        "quantity",
        "entry_price",
        "mark_price",
        "unrealized_pnl_usd",
        "liquidation_price",
        "take_profit_price",
        "stop_loss_price",
        "exit_price",
        "realized_pnl_usd",
        mode="before",
    )
    @classmethod
    def parse_decimal(cls, value: object) -> Decimal | None:
        if value is None:
            return None
        parsed = Decimal(str(value))
        if not parsed.is_finite():
            raise ValueError("manual_position_decimal_invalid")
        return parsed

    @model_validator(mode="after")
    def validate_position(self) -> ManualPositionView:
        if self.quantity < 0 or self.entry_price <= 0 or self.mark_price <= 0:
            raise ValueError("manual_position_value_invalid")
        if self.liquidation_price is not None and self.liquidation_price < 0:
            raise ValueError("manual_position_value_invalid")
        if self.observed_at_ms < self.opened_at_ms:
            raise ValueError("manual_position_time_invalid")
        if self.state is ManualPositionState.CLOSED:
            if self.quantity != 0 or self.closed_at_ms is None or self.exit_reason is None:
                raise ValueError("manual_closed_position_shape_invalid")
        elif self.closed_at_ms is not None:
            raise ValueError("manual_open_position_closed_time_invalid")
        return self

    @property
    def pnl_bps(self) -> int:
        notional = self.entry_price * self.quantity
        if notional <= 0:
            return 0
        return int((self.unrealized_pnl_usd / notional * Decimal(10_000)).to_integral_value())

    @property
    def margin_return_bps(self) -> int:
        margin = self.entry_price * self.quantity / Decimal(self.leverage)
        if margin <= 0:
            return 0
        return int((self.unrealized_pnl_usd / margin * Decimal(10_000)).to_integral_value())


class ManualTradeHistoryEvent(_FrozenPortfolioModel):
    event_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    session_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    symbol: str = Field(pattern=r"^[A-Z0-9][A-Z0-9.-]{0,19}$")
    event_kind: str = Field(min_length=1, max_length=80)
    payload: dict[str, object]
    created_at_ms: int = Field(gt=0)


__all__ = [
    "MAX_DEVELOPMENT_TEST_NOTIONAL_USD",
    "ManualCloseRequest",
    "ManualCloseState",
    "ManualPositionState",
    "ManualPositionView",
    "ManualTradeHistoryEvent",
    "is_development_test_source",
]
