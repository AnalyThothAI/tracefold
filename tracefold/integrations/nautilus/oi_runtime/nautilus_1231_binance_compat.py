"""Pinned Nautilus 1.231 Binance private-account compatibility seam."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from nautilus_trader.adapters.binance.http.error import BinanceError, get_binance_error_code
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.reports import OrderStatusReport
from nautilus_trader.model.events import OrderSubmitted
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.orders import OrderUnpacker

from tracefold.trading import TradePlan

from .trade_plans import EntryQueryProof


@dataclass(frozen=True, slots=True)
class CompleteBinanceAccountReports:
    """One complete private-account proof, including Binance Algo orders."""

    positions: tuple[Any, ...]
    regular_orders: tuple[Any, ...]
    algo_orders: tuple[Any, ...]

    @property
    def orders(self) -> tuple[Any, ...]:
        return (*self.regular_orders, *self.algo_orders)

    @property
    def account_flat(self) -> bool:
        return not self.positions and not self.regular_orders and not self.algo_orders


def single_binance_execution_client(engine: Any) -> Any:
    """Return the exact sole Binance execution client or reject the graph."""

    client_ids = tuple(engine.registered_clients)
    clients = engine._clients
    if len(client_ids) != 1 or set(clients) != set(client_ids):
        raise RuntimeError("oi_runtime_execution_client_ambiguous")
    client = clients[client_ids[0]]
    if client.venue.value != "BINANCE":
        raise RuntimeError("oi_runtime_execution_client_unsupported")
    return client


def bind_reconciled_order_account(engine: Any, report: Any) -> None:
    """Repair 1.231's account-less reconciled native order, without dispatching an order.

    Native reconciliation goes INITIALIZED -> ACCEPTED/FILLED. In this pinned version
    only OrderSubmitted sets Order.account_id, so those orders disappear from every
    account-scoped Cache read. Replaying the same native events with a local binding
    event supplies the account proven by the private report. This is Cache repair:
    the binding event is never published, audited or sent to an execution client.
    """
    if not isinstance(report, OrderStatusReport):
        return
    cache = engine._cache
    client_order_id = report.client_order_id or cache.client_order_id(report.venue_order_id)
    order = cache.order(client_order_id) if client_order_id is not None else None
    if order is None:
        raise RuntimeError("oi_runtime_report_order_missing_from_cache")
    if order.account_id is not None:
        if order.account_id != report.account_id:
            raise RuntimeError("oi_runtime_report_order_account_mismatch")
        return
    events = order.events
    rebound = OrderUnpacker.from_init(events[0])
    rebound.apply(
        OrderSubmitted(
            trader_id=order.trader_id,
            strategy_id=order.strategy_id,
            instrument_id=order.instrument_id,
            client_order_id=order.client_order_id,
            account_id=report.account_id,
            event_id=UUID4(),
            ts_event=report.ts_accepted,
            ts_init=report.ts_init,
        )
    )
    for event in events[1:]:
        rebound.apply(event)
    cache.add_order(
        rebound,
        position_id=cache.position_id(order.client_order_id),
        client_id=cache.client_id(order.client_order_id),
        overwrite=True,
    )
    cache.update_order(rebound)


async def load_complete_binance_account_reports(client: Any) -> CompleteBinanceAccountReports:
    """Load active positions plus regular and Algo orders without swallowed API errors."""

    if client.venue.value != "BINANCE":
        raise RuntimeError("oi_runtime_execution_client_unsupported")
    client._active_symbols_cache = None
    try:
        positions = await client._get_binance_position_status_reports()
        _, regular_orders = await client._build_active_symbols(None)
        regular_reports = client._parse_order_status_reports(regular_orders, None, None)
        if len(regular_reports) != len(regular_orders):
            raise RuntimeError("oi_runtime_regular_order_report_incomplete")
        algo_orders, _ = await client._fetch_algo_orders(None)
        algo_reports = []
        for value in algo_orders:
            report = client._parse_algo_order_report(value, None, None)
            if report is None:
                raise RuntimeError("oi_runtime_algo_order_report_incomplete")
            algo_reports.append(report)
    finally:
        client._active_symbols_cache = None
    return CompleteBinanceAccountReports(
        positions=tuple(positions),
        regular_orders=tuple(regular_reports),
        algo_orders=tuple(algo_reports),
    )


async def query_planned_entry(client: Any, plan: TradePlan) -> EntryQueryProof:
    """Query the frozen economic identity without the adapter's error-to-None conversion.

    Only Binance's no-such-order answer means absent. Transport/auth/parse errors propagate,
    so they can never manufacture terminal proof or authorize a duplicate economic order.
    """
    instrument_id = InstrumentId.from_str(plan.instrument_id)
    try:
        order = await client._http_account.query_order(
            symbol=instrument_id.symbol.value, orig_client_order_id=plan.entry_client_order_id
        )
    except BinanceError as exc:
        code = get_binance_error_code(exc)
        if code is not None and code.value == -2013:
            return EntryQueryProof(plan.entry_id, "absent")
        raise
    if (
        order.clientOrderId != plan.entry_client_order_id
        or client._get_cached_instrument_id(order.symbol) != instrument_id
        or order.side is None
        or order.side.value != ("BUY" if plan.direction == "long" else "SELL")
        or order.type is None
        or order.type.value != "MARKET"
        or order.reduceOnly is not False
        or order.origQty is None
        or Decimal(order.origQty) != plan.entry_quantity
    ):
        raise RuntimeError("oi_runtime_planned_entry_query_identity_invalid")
    status = None if order.status is None else order.status.value
    if status in {"FILLED", "CANCELED", "REJECTED", "EXPIRED", "EXPIRED_IN_MATCH"}:
        return EntryQueryProof(plan.entry_id, "terminal", Decimal(order.executedQty))
    if status in {"NEW", "PARTIALLY_FILLED", "PENDING_CANCEL"}:
        return EntryQueryProof(plan.entry_id, "working", Decimal(order.executedQty))
    raise RuntimeError("oi_runtime_planned_entry_query_status_unknown")


__all__ = [
    "CompleteBinanceAccountReports",
    "bind_reconciled_order_account",
    "load_complete_binance_account_reports",
    "query_planned_entry",
    "single_binance_execution_client",
]
