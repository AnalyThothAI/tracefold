"""The bounded current account read projection, read from the Nautilus Cache (#680).

It reads the Cache Nautilus reconciles with the venue and decides nothing: which positions a plan
claims comes from the plan index the Strategy already holds, and which resting orders protect them is
their type and side. Every Cache query here is unscoped or instrument-scoped, never account-scoped.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import ROUND_FLOOR, Decimal
from typing import Any, Literal

from nautilus_trader.model.enums import OrderType

from tracefold.trading import ExecutionAccountOrder, ExecutionAccountPosition, ExecutionAccountSnapshot

from .risk import DayStartBaseline, account_equity_usd, decimal_value, quote_mid

OrderLeg = Literal["entry", "stop", "take_profit", "exit", "unknown"]
_MAX_POSITION_ROWS = 100
_MAX_ORDER_ROWS = 200


def _text(value: Any) -> str:
    return format(decimal_value(value).normalize(), "f")


def open_and_inflight_orders(cache: Any) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
    """Every working order once: an order Nautilus lists as both open and in flight is open."""

    open_orders = tuple(cache.orders_open())
    open_ids = {order.client_order_id for order in open_orders}
    inflight = tuple(order for order in cache.orders_inflight() if order.client_order_id not in open_ids)
    return open_orders, inflight


def order_leg(order: Any, *, entry_client_order_id: str | None) -> OrderLeg:
    if entry_client_order_id is not None and order.client_order_id.value == entry_client_order_id:
        return "entry"
    if order.order_type == OrderType.STOP_MARKET and order.is_reduce_only:
        return "stop"
    if order.order_type == OrderType.MARKET_IF_TOUCHED and order.is_reduce_only:
        return "take_profit"
    if order.is_reduce_only:
        return "exit"
    return "unknown"


def account_snapshot(
    *,
    cache: Any,
    account_id: Any,
    plan_entry_orders: Mapping[Any, str],
    baseline: DayStartBaseline | None,
    now_ns: int,
    market_stale_after_ns: int,
) -> ExecutionAccountSnapshot:
    """Positions, their protective orders and every working order, as the Cache holds them now.

    `plan_entry_orders` maps each instrument a non-terminal plan claims to that plan's entry client
    order id; a position or a non-reduce-only order on any other instrument is not `owned`.
    """

    positions = sorted(cache.positions_open(), key=lambda item: item.id.value)
    open_orders, inflight = open_and_inflight_orders(cache)
    complete = True
    position_rows: list[ExecutionAccountPosition] = []
    for position in positions:
        instrument_id = position.instrument_id
        quote = cache.quote_tick(instrument_id)
        mark = quote_mid(cache, instrument_id)
        fresh = quote is not None and now_ns - int(quote.ts_event) <= market_stale_after_ns
        instrument = cache.instrument(instrument_id)
        unrealized = None
        if mark is not None and fresh and instrument is not None:
            pnl = position.unrealized_pnl(instrument.make_price(mark))
            unrealized = None if pnl is None else _text(pnl)
        if unrealized is None:
            complete = False
        closing = [
            order
            for order in (*open_orders, *inflight)
            if order.instrument_id == instrument_id and order.is_reduce_only and order.side != position.entry
        ]
        stop = next((order for order in closing if order.order_type == OrderType.STOP_MARKET), None)
        take_profit = next((order for order in closing if order.order_type == OrderType.MARKET_IF_TOUCHED), None)
        position_rows.append(
            ExecutionAccountPosition(
                position_id=position.id.value,
                instrument_id=instrument_id.value,
                side="long" if position.is_long else "short",
                quantity=_text(abs(decimal_value(position.quantity))),
                entry_price=_text(Decimal(str(position.avg_px_open))),
                mark_price=None if mark is None or not fresh else _text(mark),
                unrealized_pnl_usd=unrealized,
                owned=instrument_id in plan_entry_orders,
                stop_trigger_price=None if stop is None else _text(stop.trigger_price),
                take_profit_trigger_price=None if take_profit is None else _text(take_profit.trigger_price),
            )
        )

    order_rows: list[ExecutionAccountOrder] = []
    for state, orders in (("open", open_orders), ("inflight", inflight)):
        for order in sorted(orders, key=lambda item: item.client_order_id.value):
            entry = plan_entry_orders.get(order.instrument_id)
            leg = order_leg(order, entry_client_order_id=entry)
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
                    owned=entry is not None and (leg != "unknown"),
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
    truncated = len(position_rows) > _MAX_POSITION_ROWS or len(order_rows) > _MAX_ORDER_ROWS
    return ExecutionAccountSnapshot(
        observed_at_ns=now_ns,
        equity_usd=None if equity is None else _text(equity),
        daily_drawdown_usd=None if daily_drawdown is None else _text(daily_drawdown),
        daily_drawdown_bps=daily_drawdown_bps,
        positions=tuple(position_rows[:_MAX_POSITION_ROWS]),
        orders=tuple(order_rows[:_MAX_ORDER_ROWS]),
        open_orders_count=len(open_orders),
        inflight_orders_count=len(inflight),
        complete=complete and not truncated,
    )


__all__ = ["OrderLeg", "account_snapshot", "open_and_inflight_orders", "order_leg"]
