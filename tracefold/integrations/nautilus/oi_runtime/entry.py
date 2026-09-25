"""Entry identities and the pure arithmetic of admitting one: spread, size and deterministic ids."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from nautilus_trader.model.identifiers import ClientOrderId

from tracefold.trading.execution_contracts import (
    OperatorIntentV1,
    SignalEntryEnvelopeV3,
    SignalExitPlanV1,
    TradeSignalV3,
)
from tracefold.trading.trade_plan import TradePlan

from .risk import decimal_value, fixed_risk_quantity

# Sizing pads the executable side of the book by this much before it divides the risk budget by a
# price, so a market order that fills through a thin top of book is still inside the frozen risk.
_ENTRY_PRICE_PAD_BPS = Decimal(25)


def deterministic_client_order_id(*, namespace: str, entry_id: str, leg: str) -> ClientOrderId:
    """One venue order id per (account slot, mode, entry, leg); the namespace carries the first two."""

    digest = hashlib.sha256(f"{namespace}:{entry_id}:{leg}".encode()).hexdigest()
    return ClientOrderId(f"tf{digest[:30]}")


@dataclass(frozen=True, slots=True)
class RuntimeEntryRequest:
    """One request to open exposure, correlated to exactly one durable input: a Signal or a Command."""

    entry_id: str
    market_key: str
    direction: Literal["long", "short"]
    expires_at_ns: int
    source: Literal["signal", "manual"]
    case_id: str | None = None
    entry_scope_id: str = ""
    exit_plan: SignalExitPlanV1 | None = None
    entry_envelope: SignalEntryEnvelopeV3 | None = None
    native_symbol: str | None = None
    asset_id: str | None = None
    mapping_semantics_digest: str | None = None
    account_slot: str | None = None
    runtime_mode: Literal["paper", "live"] | None = None

    @classmethod
    def from_signal(cls, signal: TradeSignalV3) -> RuntimeEntryRequest:
        return cls(
            entry_id=signal.signal_id,
            market_key=signal.market_key,
            direction=signal.direction,
            expires_at_ns=signal.expires_at_ns,
            source="signal",
            case_id=signal.case_id,
            entry_scope_id=signal.entry_scope_id,
            exit_plan=signal.exit_plan,
            entry_envelope=signal.entry_envelope,
            native_symbol=signal.native_symbol,
            asset_id=signal.asset_id,
            mapping_semantics_digest=signal.mapping_semantics_digest,
            account_slot=signal.account_slot,
            runtime_mode=signal.runtime_mode,
        )

    @classmethod
    def from_manual_command(cls, command: OperatorIntentV1) -> RuntimeEntryRequest:
        if command.action != "manual_entry" or command.market_key is None or command.direction is None:
            raise ValueError("oi_runtime_manual_entry_invalid")
        return cls(
            entry_id=command.command_id,
            market_key=command.market_key,
            direction=command.direction,
            expires_at_ns=command.expires_at_ns,
            source="manual",
            entry_scope_id=f"manual:{command.command_id}",
        )

    @classmethod
    def from_plan(cls, plan: TradePlan) -> RuntimeEntryRequest:
        return cls(
            entry_id=plan.entry_id,
            market_key=plan.market_key,
            direction=plan.direction,
            expires_at_ns=plan.entry_expires_at_ns,
            source=plan.source,
            case_id=plan.case_id,
            entry_scope_id=plan.entry_scope_id,
        )


def spread_bps(quote: Any) -> Decimal | None:
    """The quoted spread over its own midpoint, in basis points; `None` for a book that is not one."""

    bid = decimal_value(quote.bid_price)
    ask = decimal_value(quote.ask_price)
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    return (ask - bid) * Decimal(10_000) / ((bid + ask) / Decimal(2))


def entry_quantity(
    *,
    direction: Literal["long", "short"],
    quote: Any,
    instrument: Any,
    stop_distance_bps: int,
    allowed_risk_usd: Decimal,
    equity_usd: Decimal,
    max_leverage: int,
    existing_notional_usd: Decimal,
) -> Any | str:
    """The one quantity this entry is sized at, or the name of why the venue could not take any.

    `fixed_risk_quantity` divides the risk budget by the padded price, clamps to the leverage headroom
    and floors to `size_increment`; rounding down cannot cross either ceiling. What remains are the
    venue's own minimums, which sizing does not know.
    """

    executable = decimal_value(quote.ask_price if direction == "long" else quote.bid_price)
    price = executable * (Decimal(1) + _ENTRY_PRICE_PAD_BPS / Decimal(10_000))
    try:
        raw_quantity = fixed_risk_quantity(
            price=price,
            stop_distance_bps=stop_distance_bps,
            allowed_risk_usd=allowed_risk_usd,
            equity_usd=equity_usd,
            max_leverage=max_leverage,
            existing_notional_usd=existing_notional_usd,
            size_increment=instrument.size_increment.as_decimal(),
        )
    except ValueError as exc:
        return str(exc)
    quantity = instrument.make_qty(raw_quantity)
    if quantity.as_decimal() <= 0:
        return "quantity_below_increment"
    if instrument.min_quantity is not None and quantity < instrument.min_quantity:
        return "quantity_below_minimum"
    if instrument.min_notional is not None and quantity.as_decimal() * price < instrument.min_notional.as_decimal():
        return "notional_below_minimum"
    return quantity


def protective_trigger(
    *,
    direction: Literal["long", "short"],
    average_entry_price: Decimal,
    distance_bps: int,
    leg: Literal["stop", "take_profit"],
) -> Decimal:
    """A stop sits `distance` against the position, a take-profit `distance` with it."""

    distance = Decimal(distance_bps) / Decimal(10_000)
    adverse = (direction == "long") == (leg == "stop")
    return average_entry_price * (Decimal(1) - distance if adverse else Decimal(1) + distance)


__all__ = [
    "RuntimeEntryRequest",
    "deterministic_client_order_id",
    "entry_quantity",
    "protective_trigger",
    "spread_bps",
]
