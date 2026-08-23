"""Deterministic sizing, the frozen payload, and the durable one-attempt commit contract.

Nothing in this module is chosen by a model. The side arrives from the pure policy; everything else —
account, venue, exact provider symbol, quantity, stop, protection, holding deadline — is arithmetic
over configuration and fresh market truth.

The commit contract is written for a provider with **no client idempotency key**. OpenTrade's pinned
CEX skill declares none, so nothing downstream can deduplicate a retry. That single fact produces all
three rules here:

* the ledger row moves to `SUBMITTING` and the transaction **commits before** the network call, so a
  crash between the two leaves evidence that an attempt may exist;
* `provider_attempt_count` is CHECKed at one in the schema, so a second write is not merely discouraged
  but impossible to record;
* a timeout, a reset, a malformed response or a restart terminalises as `AMBIGUOUS`, whose only legal
  successor is a read. Never a resend, and never a resend routed at the other venue.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Protocol

from .models import (
    ExecutionReceipt,
    MarketContext,
    OrderSide,
    OrderState,
    PreparedOrder,
    RiskRejection,
    TradingMode,
)

# Binance USDⓈ-M and Hyperliquid taker fees are both 4.5-5 bps at the base tier. The paper adapter
# charges both legs so a paper receipt is never flattered by a cost the live lane would pay.
DEFAULT_TAKER_FEE_BPS = 5


@dataclass(frozen=True, slots=True)
class OrderPolicy:
    """Every order parameter, fixed. Sizing is notional-first, never stop-distance-first.

    Stop-distance sizing (`quantity = max_loss / stop_distance`) is what made the previous revision's
    three risk numbers mutually unreachable: a tight stop demanded a notional the cap rejected, and the
    caller learned that only by being refused. Fixed notional plus a fixed stop makes the worst case a
    multiplication the operator can read off the config — `notional x stop_bps x orders_per_day`.
    """

    fixed_notional_usd: Decimal = Decimal("50")
    fixed_stop_bps: int = 200
    take_profit_bps: int = 0
    max_holding_ms: int = 1_800_000
    max_spread_bps: int = 30
    max_open_underlyings: int = 2
    max_orders_per_day: int = 4
    taker_fee_bps: int = DEFAULT_TAKER_FEE_BPS
    # Paper has no order book and the public catalogue carries no lot size. Sizing still has to be
    # exact, so paper declares its step instead of pretending to know the venue's.
    paper_quantity_step: Decimal = Decimal("0.0001")

    def as_dict(self) -> dict[str, object]:
        return {
            "fixed_notional_usd": str(self.fixed_notional_usd),
            "fixed_stop_bps": self.fixed_stop_bps,
            "take_profit_bps": self.take_profit_bps,
            "max_holding_ms": self.max_holding_ms,
            "max_spread_bps": self.max_spread_bps,
            "max_open_underlyings": self.max_open_underlyings,
            "max_orders_per_day": self.max_orders_per_day,
            "taker_fee_bps": self.taker_fee_bps,
        }

    @property
    def worst_case_daily_loss_usd(self) -> Decimal:
        per_order = self.fixed_notional_usd * Decimal(self.fixed_stop_bps) / Decimal(10_000)
        return per_order * Decimal(self.max_orders_per_day)


DEFAULT_ORDER_POLICY = OrderPolicy()


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).quantize(Decimal("1"), rounding=ROUND_DOWN) * step


def stop_price_for(*, side: OrderSide, entry: Decimal, stop_bps: int) -> Decimal:
    offset = entry * Decimal(stop_bps) / Decimal(10_000)
    return entry - offset if side == "buy" else entry + offset


def take_profit_for(*, side: OrderSide, entry: Decimal, take_profit_bps: int) -> Decimal | None:
    if take_profit_bps <= 0:
        # Measured: fixed take-profits were negative across the whole stop/TP grid because the return
        # distribution is right-tailed. Time and the protective stop own the exit instead.
        return None
    offset = entry * Decimal(take_profit_bps) / Decimal(10_000)
    return entry + offset if side == "buy" else entry - offset


@dataclass(frozen=True, slots=True)
class SizedOrder:
    quantity: Decimal
    notional_usd: Decimal
    entry_reference: Decimal
    stop_price: Decimal
    take_profit_price: Decimal | None


def size_order(
    *,
    side: OrderSide,
    market: MarketContext,
    mode: TradingMode,
    policy: OrderPolicy = DEFAULT_ORDER_POLICY,
) -> SizedOrder | RiskRejection:
    """Fixed notional at the fresh mark, floored to the venue's step. Fails closed, never clamps silently."""

    entry = Decimal(market.mark_price)
    if entry <= 0:
        return RiskRejection(rule="mark_price_invalid", detail=str(entry))

    live = mode in ("live_reviewed", "live_bounded")
    step = market.quantity_step
    if step is None:
        if live:
            # Exact precision is a provider fact. Guessing it is how an order gets rejected at the venue
            # or, worse, accepted at a size nobody chose.
            return RiskRejection(rule="quantity_step_unknown")
        step = policy.paper_quantity_step
    if step <= 0:
        return RiskRejection(rule="quantity_step_invalid", detail=str(step))

    if live and not market.spread_available:
        return RiskRejection(rule="spread_unknown_fail_closed")
    if market.spread_bps is not None and market.spread_bps > policy.max_spread_bps:
        return RiskRejection(rule="spread_above_max", detail=str(market.spread_bps))

    contract_size = Decimal(market.contract_size or 1)
    if contract_size <= 0:
        return RiskRejection(rule="contract_size_invalid", detail=str(contract_size))

    raw_quantity = policy.fixed_notional_usd / (entry * contract_size)
    quantity = floor_to_step(raw_quantity, step)
    if quantity <= 0:
        return RiskRejection(rule="quantity_below_step", detail=f"{raw_quantity} < {step}")
    if market.min_quantity is not None and quantity < market.min_quantity:
        return RiskRejection(rule="quantity_below_venue_minimum", detail=f"{quantity} < {market.min_quantity}")

    notional = quantity * entry * contract_size
    return SizedOrder(
        quantity=quantity,
        notional_usd=notional,
        entry_reference=entry,
        stop_price=stop_price_for(side=side, entry=entry, stop_bps=policy.fixed_stop_bps),
        take_profit_price=take_profit_for(side=side, entry=entry, take_profit_bps=policy.take_profit_bps),
    )


def build_payload(
    *,
    instrument_exchange_id: str,
    provider_symbol: str,
    side: OrderSide,
    quantity: Decimal,
    stop_price: Decimal,
    take_profit_price: Decimal | None,
    hedged: bool,
) -> dict[str, object]:
    """The canonical provider payload, frozen on the order row before any network call exists.

    `hedged` is mandatory at the provider and getting it wrong opens the opposite position, so it is a
    required argument rather than a default. The protective stop rides along with the entry so it lives
    at the venue: this process dying must not leave an unprotected position.
    """

    payload: dict[str, object] = {
        "symbol": provider_symbol,
        "side": side,
        "type": "market",
        "quantity": str(quantity),
        "exchangeId": instrument_exchange_id,
        "hedged": bool(hedged),
        "stopLossPrice": str(stop_price),
    }
    if take_profit_price is not None:
        payload["takeProfitPrice"] = str(take_profit_price)
    return payload


class ExecutionAdapter(Protocol):
    """One attempt, one answer. An adapter never retries and never routes to a second venue."""

    name: str

    async def submit(self, order: PreparedOrder) -> ExecutionReceipt: ...

    async def close(self, order: PreparedOrder, *, quantity: Decimal) -> ExecutionReceipt: ...


ClockMs = Callable[[], int]
Submit = Callable[[PreparedOrder], Awaitable[ExecutionReceipt]]


def next_state_for(receipt: ExecutionReceipt) -> OrderState:
    """Map one adapter answer onto the ledger. An ACK is an acknowledgement, never a fill."""

    if receipt.state == "REJECTED":
        return OrderState.REJECTED
    if receipt.state == "AMBIGUOUS":
        return OrderState.AMBIGUOUS
    return OrderState.ACKNOWLEDGED


def protection_verified(*, stop_price: Decimal | None, remote_stop_price: Decimal | None) -> bool:
    """An open position without a venue-side stop is `UNPROTECTED`, whatever the submit payload said."""

    return stop_price is not None and remote_stop_price is not None and remote_stop_price > 0


def must_close_at(*, opened_at_ms: int, policy: OrderPolicy = DEFAULT_ORDER_POLICY) -> int:
    """The deterministic lifecycle end. Reached without another News event and without a model."""

    return int(opened_at_ms) + int(policy.max_holding_ms)


def realized_bps(*, side: OrderSide, entry: Decimal, exit_price: Decimal, fee_bps: int) -> int:
    """Signed return in basis points, both taker legs charged. Paper must not flatter itself."""

    if entry <= 0:
        return 0
    gross = (exit_price / entry - Decimal(1)) * Decimal(10_000)
    signed = gross if side == "buy" else -gross
    return int((signed - Decimal(2 * fee_bps)).quantize(Decimal("1")))


__all__ = [
    "DEFAULT_ORDER_POLICY",
    "DEFAULT_TAKER_FEE_BPS",
    "ExecutionAdapter",
    "OrderPolicy",
    "SizedOrder",
    "build_payload",
    "floor_to_step",
    "must_close_at",
    "next_state_for",
    "protection_verified",
    "realized_bps",
    "size_order",
    "stop_price_for",
    "take_profit_for",
]
