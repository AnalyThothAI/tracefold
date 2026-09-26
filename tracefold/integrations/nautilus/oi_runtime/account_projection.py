"""Bounded account and finding projection from one Strategy-owned observation."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import ROUND_FLOOR, Decimal
from typing import Any, Literal

from nautilus_trader.model.enums import OrderSide, OrderType, PositionSide, TriggerType

from tracefold.trading.storage.execution_stream import (
    ExecutionAccountOrder,
    ExecutionAccountPosition,
    ExecutionAccountSnapshot,
    ExecutionExposureFinding,
)
from tracefold.trading.trade_plan import PlanOrderBinding, TradePlan

from .risk import DayStartBaseline, account_equity_usd, decimal_value, quote_mid

OrderLeg = Literal["entry", "stop", "take_profit", "exit", "unknown"]
_MAX_POSITION_ROWS = 100
_MAX_ORDER_ROWS = 200
_MAX_FINDING_ROWS = 100


def _text(value: Any) -> str:
    return format(decimal_value(value).normalize(), "f")


def open_and_inflight_orders(cache: Any) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
    """Every working order once: an order listed as both open and in flight is open."""

    open_orders = tuple(cache.orders_open())
    open_ids = {order.client_order_id for order in open_orders}
    inflight = tuple(order for order in cache.orders_inflight() if order.client_order_id not in open_ids)
    return open_orders, inflight


def position_claimed(position: Any, plan: TradePlan, strategy_id: Any) -> bool:
    """The same position identity rule used by convergence and the public account view."""

    expected = PositionSide.LONG if plan.direction == "long" else PositionSide.SHORT
    return (
        position.instrument_id.value == plan.instrument_id
        and position.strategy_id == strategy_id
        and position.side == expected
        and position.opening_order_id.value == plan.entry_client_order_id
    )


def _order_claimed(order: Any, plan: TradePlan, strategy_id: Any, binding: PlanOrderBinding | None) -> bool:
    if order.instrument_id.value != plan.instrument_id or order.strategy_id != strategy_id:
        return False
    if binding is None or binding.entry_id != plan.entry_id or binding.instrument_id != plan.instrument_id:
        return False
    entry_side = OrderSide.BUY if plan.direction == "long" else OrderSide.SELL
    if order.client_order_id.value == plan.entry_client_order_id:
        return not order.is_reduce_only and order.side == entry_side
    return order.is_reduce_only and order.side != entry_side and binding.leg != "entry"


def _protection(
    position: Any, orders: tuple[Any, ...], strategy_id: Any
) -> tuple[Literal["protected", "pending", "unprotected"], Any | None, Any | None]:
    closing_side = OrderSide.SELL if position.is_long else OrderSide.BUY
    candidates = [
        order
        for order in orders
        if order.instrument_id == position.instrument_id
        and order.strategy_id == strategy_id
        and order.is_reduce_only
        and order.side == closing_side
    ]

    def valid(order: Any | None) -> bool:
        return bool(
            order is not None
            and order.is_open
            and not order.is_pending_cancel
            and not order.is_pending_update
            and order.trigger_price is not None
            and order.trigger_type == TriggerType.MARK_PRICE
            and decimal_value(order.quantity) >= decimal_value(position.quantity)
        )

    stops = [order for order in candidates if order.order_type == OrderType.STOP_MARKET]
    take_profits = [order for order in candidates if order.order_type == OrderType.MARKET_IF_TOUCHED]
    stop = next((order for order in stops if valid(order)), stops[0] if stops else None)
    take_profit = next((order for order in take_profits if valid(order)), take_profits[0] if take_profits else None)
    if valid(stop) and valid(take_profit):
        status: Literal["protected", "pending", "unprotected"] = "protected"
    elif any(order.is_inflight or order.is_pending_cancel or order.is_pending_update for order in candidates):
        status = "pending"
    else:
        status = "unprotected"
    return status, stop, take_profit


def _finding(
    code: str,
    *,
    positions: Mapping[str, Any],
    orders: Mapping[str, Any],
    plans: Mapping[Any, tuple[TradePlan, ...]],
    venue_instruments: Mapping[str, Any],
    observed_at_ns: int,
) -> ExecutionExposureFinding:
    prefix, _, identity = code.partition(":")
    position = positions.get(identity)
    order = orders.get(identity)
    if prefix in {"position", "ownership"} and position is not None:
        instrument_id = position.instrument_id
        candidates = plans.get(instrument_id, ())
        return ExecutionExposureFinding(
            kind="unclaimed_position" if not candidates else "ownership_mismatch",
            object_id=identity,
            instrument_id=instrument_id.value,
            plan_entry_id=candidates[0].entry_id if len(candidates) == 1 else None,
            cache_quantity=_text(position.signed_decimal_qty()),
            venue_quantity=None,
            observed_at_ns=observed_at_ns,
        )
    if prefix == "order" and order is not None:
        candidates = plans.get(order.instrument_id, ())
        return ExecutionExposureFinding(
            kind="unexpected_order",
            object_id=identity,
            instrument_id=order.instrument_id.value,
            plan_entry_id=candidates[0].entry_id if len(candidates) == 1 else None,
            cache_quantity=_text(order.quantity),
            venue_quantity=None,
            observed_at_ns=observed_at_ns,
        )
    if prefix == "venue":
        symbol, _, rest = identity.partition(":")
        fields = dict(part.split("=", 1) for part in rest.split(":"))
        instrument = venue_instruments.get(symbol)
        instrument_id = instrument.value if instrument is not None else f"{symbol}-PERP.BINANCE"
        candidates = plans.get(instrument, ()) if instrument is not None else ()
        return ExecutionExposureFinding(
            kind="venue_cache_mismatch",
            object_id=symbol,
            instrument_id=instrument_id,
            plan_entry_id=candidates[0].entry_id if len(candidates) == 1 else None,
            cache_quantity=fields["cache"],
            venue_quantity=fields["venue"],
            observed_at_ns=observed_at_ns,
        )
    if prefix in {"unconfirmed_close", "ambiguous"}:
        candidates = next((values for instrument, values in plans.items() if instrument.value == identity), ())
        return ExecutionExposureFinding(
            kind="close_unconfirmed" if prefix == "unconfirmed_close" else "ambiguous",
            object_id=identity,
            instrument_id=identity,
            plan_entry_id=candidates[0].entry_id if len(candidates) == 1 else None,
            cache_quantity=None,
            venue_quantity=None,
            observed_at_ns=observed_at_ns,
        )
    if prefix == "submission_unknown":
        matches = (
            (instrument, plan)
            for instrument, values in plans.items()
            for plan in values
            if plan.entry_client_order_id == identity
        )
        instrument, plan = next(matches)
        return ExecutionExposureFinding(
            kind="submission_unknown",
            object_id=identity,
            instrument_id=instrument.value,
            plan_entry_id=plan.entry_id,
            cache_quantity=None,
            venue_quantity=None,
            observed_at_ns=observed_at_ns,
        )
    raise ValueError(f"execution_finding_unknown:{prefix}")


def exposure_findings(
    codes: tuple[str, ...],
    *,
    positions: Mapping[str, Any],
    orders: Mapping[str, Any],
    plans: Mapping[Any, tuple[TradePlan, ...]],
    venue_instruments: Mapping[str, Any],
    observed_at_ns: int,
) -> tuple[ExecutionExposureFinding, ...]:
    """Interpret the convergence verdict once, before later Cache events can erase its evidence."""

    return tuple(
        sorted(
            (
                _finding(
                    code,
                    positions=positions,
                    orders=orders,
                    plans=plans,
                    venue_instruments=venue_instruments,
                    observed_at_ns=observed_at_ns,
                )
                for code in codes
            ),
            key=lambda value: (value.kind, value.instrument_id, value.object_id),
        )
    )


def account_snapshot(
    *,
    cache: Any,
    account_id: Any,
    plans: Mapping[Any, tuple[TradePlan, ...]],
    strategy_id: Any,
    order_bindings: Mapping[str, PlanOrderBinding],
    venue_positions: Mapping[str, Decimal] | None,
    venue_instruments: Mapping[str, Any],
    findings: tuple[ExecutionExposureFinding, ...],
    baseline: DayStartBaseline | None,
    now_ns: int,
    market_stale_after_ns: int,
) -> ExecutionAccountSnapshot:
    """Read Cache and the latest valid venue evidence without inventing missing prices or fills."""

    positions = sorted(cache.positions_open(), key=lambda item: item.id.value)
    open_orders, inflight = open_and_inflight_orders(cache)
    complete = True
    position_rows: list[ExecutionAccountPosition] = []
    cache_symbols: set[str] = set()
    for position in positions:
        instrument_id = position.instrument_id
        instrument = cache.instrument(instrument_id)
        symbol = (
            str(instrument.raw_symbol.value)
            if instrument is not None
            else instrument_id.symbol.value.removesuffix("-PERP")
        )
        cache_symbols.add(symbol)
        candidates = plans.get(instrument_id, ())
        plan = (
            candidates[0] if len(candidates) == 1 and position_claimed(position, candidates[0], strategy_id) else None
        )
        quote = cache.quote_tick(instrument_id)
        mark = quote_mid(cache, instrument_id)
        fresh = quote is not None and now_ns - int(quote.ts_event) <= market_stale_after_ns
        unrealized = None
        if mark is not None and fresh and instrument is not None:
            pnl = position.unrealized_pnl(instrument.make_price(mark))
            unrealized = None if pnl is None else _text(pnl)
        if unrealized is None:
            complete = False
        protection, stop, take_profit = _protection(position, (*open_orders, *inflight), strategy_id)
        position_rows.append(
            ExecutionAccountPosition(
                position_id=position.id.value,
                instrument_id=instrument_id.value,
                source="cache",
                side="long" if position.is_long else "short",
                quantity=_text(abs(decimal_value(position.quantity))),
                entry_price=_text(Decimal(str(position.avg_px_open))),
                mark_price=None if mark is None or not fresh else _text(mark),
                unrealized_pnl_usd=unrealized,
                owned=plan is not None,
                plan_entry_id=None if plan is None else plan.entry_id,
                protection_status=protection,
                stop_trigger_price=None if stop is None or stop.trigger_price is None else _text(stop.trigger_price),
                take_profit_trigger_price=None
                if take_profit is None or take_profit.trigger_price is None
                else _text(take_profit.trigger_price),
            )
        )
    if venue_positions is not None:
        for symbol, quantity in sorted(venue_positions.items()):
            if quantity == 0 or symbol in cache_symbols:
                continue
            instrument = venue_instruments.get(symbol)
            instrument_id = instrument.value if instrument is not None else f"{symbol}-PERP.BINANCE"
            candidates = plans.get(instrument, ()) if instrument is not None else ()
            plan = (
                candidates[0]
                if len(candidates) == 1
                and (
                    (quantity > 0 and candidates[0].direction == "long")
                    or (quantity < 0 and candidates[0].direction == "short")
                )
                else None
            )
            position_rows.append(
                ExecutionAccountPosition(
                    position_id=f"venue:{symbol}",
                    instrument_id=instrument_id,
                    source="venue",
                    side="long" if quantity > 0 else "short",
                    quantity=_text(abs(quantity)),
                    entry_price=None,
                    mark_price=None,
                    unrealized_pnl_usd=None,
                    owned=plan is not None,
                    plan_entry_id=None if plan is None else plan.entry_id,
                    protection_status="unknown",
                    stop_trigger_price=None,
                    take_profit_trigger_price=None,
                )
            )
            complete = False

    order_rows: list[ExecutionAccountOrder] = []
    for state, orders in (("open", open_orders), ("inflight", inflight)):
        for order in sorted(orders, key=lambda item: item.client_order_id.value):
            candidates = plans.get(order.instrument_id, ())
            binding = order_bindings.get(order.client_order_id.value)
            plan = (
                candidates[0]
                if len(candidates) == 1 and _order_claimed(order, candidates[0], strategy_id, binding)
                else None
            )
            leg: OrderLeg = binding.leg if binding is not None else "unknown"
            trigger = getattr(order, "trigger_price", None)
            order_rows.append(
                ExecutionAccountOrder(
                    client_order_id=order.client_order_id.value,
                    instrument_id=order.instrument_id.value,
                    state=state,
                    leg=leg,
                    quantity=_text(abs(decimal_value(getattr(order, "leaves_qty", order.quantity)))),
                    reduce_only=bool(order.is_reduce_only),
                    trigger_price=None if trigger is None else _text(trigger),
                    owned=plan is not None,
                    plan_entry_id=None if plan is None else plan.entry_id,
                )
            )

    equity = account_equity_usd(cache=cache, account_id=account_id)
    if equity is None:
        complete = False
    daily_drawdown: Decimal | None = None
    daily_drawdown_bps: int | None = None
    if baseline is not None and equity is not None:
        daily_drawdown = max(Decimal(0), baseline.equity_usd - equity)
        daily_drawdown_bps = int(
            (daily_drawdown * Decimal(10_000) / baseline.equity_usd).to_integral_value(rounding=ROUND_FLOOR)
        )
    return ExecutionAccountSnapshot(
        observed_at_ns=now_ns,
        equity_usd=None if equity is None else _text(equity),
        daily_drawdown_usd=None if daily_drawdown is None else _text(daily_drawdown),
        daily_drawdown_bps=daily_drawdown_bps,
        positions=tuple(position_rows[:_MAX_POSITION_ROWS]),
        positions_total=len(position_rows),
        orders=tuple(order_rows[:_MAX_ORDER_ROWS]),
        orders_total=len(order_rows),
        findings=tuple(findings[:_MAX_FINDING_ROWS]),
        findings_total=len(findings),
        open_orders_count=len(open_orders),
        inflight_orders_count=len(inflight),
        complete=complete,
    )


__all__ = [
    "OrderLeg",
    "account_snapshot",
    "exposure_findings",
    "open_and_inflight_orders",
    "position_claimed",
]
