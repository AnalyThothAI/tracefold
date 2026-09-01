"""Build the bounded current account read projection from Nautilus-owned state."""

from __future__ import annotations

from decimal import ROUND_FLOOR, Decimal
from typing import Any

from nautilus_trader.model.currencies import USDT

from tracefold.trading import ExecutionAccountOrder, ExecutionAccountPosition, ExecutionAccountSnapshot

from .config import OiRuntimeProfile
from .risk import DayStartBaseline
from .state import RuntimeExecutionState

_MAX_POSITION_ROWS = 100
_MAX_ORDER_ROWS = 200


def _decimal(value: Any) -> Decimal:
    method = getattr(value, "as_decimal", None)
    return Decimal(method()) if method is not None else Decimal(str(value))


def _text(value: Decimal) -> str:
    return format(value.normalize(), "f")


class RuntimeAccountProjector:
    """Read Cache/Portfolio without becoming an order, risk, or reconciliation owner."""

    def __init__(self, *, engine: Any, profile: OiRuntimeProfile, state: RuntimeExecutionState) -> None:
        self._engine = engine
        self._profile = profile
        self._state = state
        self._route_stop_bps = {route.instrument_id: route.stop_distance_bps for route in profile.routes}

    def snapshot(
        self,
        *,
        baseline: DayStartBaseline | None,
        account_observed_at_ns: int,
    ) -> ExecutionAccountSnapshot:
        cache = self._engine.cache
        account = cache.account(self._profile.account_id)
        raw_positions = tuple(cache.positions_open(account_id=self._profile.account_id))
        raw_open_orders = tuple(cache.orders_open(account_id=self._profile.account_id))
        open_ids = {order.client_order_id for order in raw_open_orders}
        raw_inflight_orders = tuple(
            order
            for order in cache.orders_inflight(account_id=self._profile.account_id)
            if order.client_order_id not in open_ids
        )
        complete = account is not None
        equity: Decimal | None = None
        if account is not None:
            total = account.balance_total(USDT)
            if total is None:
                complete = False
            else:
                equity = _decimal(total)

        marks: dict[Any, tuple[Decimal, int]] = {}
        market_clocks: list[int] = []
        instruments = {
            *(position.instrument_id for position in raw_positions),
            *(order.instrument_id for order in (*raw_open_orders, *raw_inflight_orders)),
        }
        for instrument_id in instruments:
            quote = cache.quote_tick(instrument_id)
            instrument = cache.instrument(instrument_id)
            if quote is None or instrument is None:
                complete = False
                continue
            quote_observed_at_ns = int(quote.ts_event)
            if quote_observed_at_ns <= 0:
                complete = False
                continue
            market_clocks.append(quote_observed_at_ns)
            quote_age_ns = account_observed_at_ns - quote_observed_at_ns
            if quote_age_ns < 0 or quote_age_ns > self._profile.risk.market_stale_after_ns:
                complete = False
                continue
            bid = _decimal(quote.bid_price)
            ask = _decimal(quote.ask_price)
            if bid <= 0 or ask <= 0 or ask < bid:
                complete = False
                continue
            mark = (bid + ask) / Decimal(2)
            marks[instrument_id] = (mark, quote_observed_at_ns)

        aggregate_risk = Decimal(0)
        position_rows: list[ExecutionAccountPosition] = []
        for position in sorted(raw_positions, key=lambda item: item.id.value):
            mark_fact = marks.get(position.instrument_id)
            mark = None if mark_fact is None else mark_fact[0]
            unrealized: Decimal | None = None
            if mark is not None:
                instrument = cache.instrument(position.instrument_id)
                if instrument is None:
                    complete = False
                else:
                    pnl = position.unrealized_pnl(instrument.make_price(mark))
                    if pnl is not None:
                        unrealized = _decimal(pnl)
                        if equity is not None:
                            equity += unrealized
            stop_bps = self._route_stop_bps.get(position.instrument_id)
            if mark is None or stop_bps is None:
                complete = False
            else:
                aggregate_risk += abs(_decimal(position.quantity)) * mark * Decimal(stop_bps) / Decimal(10_000)
            protection = self._position_protection(position)
            position_rows.append(
                ExecutionAccountPosition(
                    position_id=position.id.value,
                    instrument_id=position.instrument_id.value,
                    side="long" if position.is_long else "short",
                    quantity=_text(abs(_decimal(position.quantity))),
                    entry_price=_text(Decimal(str(position.avg_px_open))),
                    mark_price=None if mark is None else _text(mark),
                    unrealized_pnl_usd=None if unrealized is None else _text(unrealized),
                    owned=position.id in self._state.positions and position.strategy_id == self._engine.id,
                    **protection,
                )
            )

        order_rows: list[ExecutionAccountOrder] = []
        unknown_ids: set[str] = set()
        for order_state, orders in (("open", raw_open_orders), ("inflight", raw_inflight_orders)):
            for order in sorted(orders, key=lambda item: item.client_order_id.value):
                route = self._state.orders.get(order.client_order_id)
                owned = route is not None and order.strategy_id == self._engine.id
                if not owned:
                    unknown_ids.add(order.client_order_id.value)
                leg_root = None if route is None else route[1].partition(":")[0]
                leg = leg_root if leg_root in {"entry", "exit", "protection"} else "unknown"
                if leg == "unknown":
                    unknown_ids.add(order.client_order_id.value)
                quantity = _decimal(getattr(order, "leaves_qty", order.quantity))
                trigger = getattr(order, "trigger_price", None)
                order_rows.append(
                    ExecutionAccountOrder(
                        client_order_id=order.client_order_id.value,
                        instrument_id=order.instrument_id.value,
                        state=order_state,
                        leg=leg,
                        quantity=_text(abs(quantity)),
                        reduce_only=bool(order.is_reduce_only),
                        trigger_price=None if trigger is None else _text(_decimal(trigger)),
                        owned=owned,
                    )
                )
                if bool(order.is_reduce_only):
                    continue
                mark_fact = marks.get(order.instrument_id)
                stop_bps = self._route_stop_bps.get(order.instrument_id)
                if mark_fact is None or stop_bps is None:
                    complete = False
                else:
                    aggregate_risk += abs(quantity) * mark_fact[0] * Decimal(stop_bps) / Decimal(10_000)
        for execution in self._state.executions.values():
            if execution.entry_query_pending or execution.private_reconciliation_requested:
                unknown_ids.add(execution.entry_order.client_order_id.value)

        daily_drawdown: Decimal | None = None
        daily_drawdown_bps: int | None = None
        if baseline is not None and equity is not None:
            daily_drawdown = max(Decimal(0), baseline.equity_usd - equity)
            daily_drawdown_bps = int(
                (daily_drawdown * Decimal(10_000) / baseline.equity_usd).to_integral_value(rounding=ROUND_FLOOR)
            )

        positions_truncated = len(position_rows) > _MAX_POSITION_ROWS
        orders_truncated = len(order_rows) > _MAX_ORDER_ROWS
        return ExecutionAccountSnapshot(
            observed_at_ns=account_observed_at_ns,
            market_observed_at_ns=min(market_clocks) if market_clocks else None,
            equity_usd=None if equity is None else _text(equity),
            day_start_equity_usd=None if baseline is None else _text(baseline.equity_usd),
            daily_drawdown_usd=None if daily_drawdown is None else _text(daily_drawdown),
            daily_drawdown_bps=daily_drawdown_bps,
            aggregate_risk_usd=None if not complete else _text(aggregate_risk),
            positions=tuple(position_rows[:_MAX_POSITION_ROWS]),
            orders=tuple(order_rows[:_MAX_ORDER_ROWS]),
            open_orders_count=len(raw_open_orders),
            inflight_orders_count=len(raw_inflight_orders),
            unknown_orders_count=len(unknown_ids),
            complete=complete and not positions_truncated and not orders_truncated,
            truncated=positions_truncated or orders_truncated,
        )

    def _position_protection(self, position: Any) -> dict[str, Any]:
        entry_id = self._state.positions.get(position.id)
        execution = None if entry_id is None else self._state.executions.get(entry_id)
        if execution is None:
            return {
                "protection_status": "unknown",
                "protection_quantity": None,
                "protection_trigger_price": None,
                "protection_full_coverage": False,
            }
        stop = execution.stop_order
        active_stop = stop if stop is not None and not stop.is_closed else None
        trigger = None if active_stop is None else getattr(active_stop, "trigger_price", None)
        quantity = execution.stop_quantity if active_stop is not None else None
        full_coverage = quantity is not None and quantity == abs(_decimal(position.quantity))
        if active_stop is not None and full_coverage:
            status = "protected"
        elif execution.pending_stop_order is not None or execution.desired_stop is not None:
            status = "pending"
        else:
            status = "unprotected"
        return {
            "protection_status": status,
            "protection_quantity": None if quantity is None else _text(quantity),
            "protection_trigger_price": None if trigger is None else _text(_decimal(trigger)),
            "protection_full_coverage": full_coverage,
        }


__all__ = ["RuntimeAccountProjector"]
