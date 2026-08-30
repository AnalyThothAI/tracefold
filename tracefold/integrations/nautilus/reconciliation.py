"""Fail-closed account report loading for both pinned execution clients."""

from __future__ import annotations

from typing import Any

from nautilus_trader.execution.reports import OrderStatusReport, PositionStatusReport

from tracefold.trading import VenueBinding


def execution_clients(engine: Any) -> dict[VenueBinding, Any]:
    """Return the exact zero/one/two-client closed graph or reject any other registry."""

    client_ids = list(engine.registered_clients)
    clients = engine._clients
    if set(clients) != set(client_ids):
        raise RuntimeError("nautilus_execution_client_ambiguous")
    result: dict[VenueBinding, Any] = {}
    venues = {"BINANCE": "BINANCE_USDM", "HYPERLIQUID": "HYPERLIQUID_PERP"}
    for client in clients.values():
        binding = venues.get(client.venue.value)
        if binding is None or binding in result:
            raise RuntimeError("nautilus_execution_client_ambiguous")
        result[binding] = client
    return result


def single_execution_client(engine: Any) -> Any:
    """Return the pinned engine's sole registered client or fail closed."""

    client_ids = list(engine.registered_clients)
    clients = engine._clients
    if len(client_ids) != 1 or set(clients) != set(client_ids):
        raise RuntimeError("nautilus_execution_client_ambiguous")
    return clients[client_ids[0]]


async def load_complete_account_reports(client: Any) -> tuple[list[Any], list[Any]]:
    """Return every active position/open order for one closed client, or propagate failure.

    Nautilus 1.231.0's public report methods translate Binance errors into empty or
    partial lists. The pinned internal report steps retain the same parsing while
    allowing provider errors to reach Tracefold's fail-closed reconciliation seam.
    """

    if client.venue.value == "HYPERLIQUID":
        return await _load_complete_hyperliquid_account_reports(client)
    if client.venue.value != "BINANCE":
        raise RuntimeError("nautilus_execution_client_unsupported")
    client._active_symbols_cache = None
    try:
        positions = await client._get_binance_position_status_reports()
        _, regular_orders = await client._build_active_symbols(None)
        regular_reports = client._parse_order_status_reports(regular_orders, None, None)
        if len(regular_reports) != len(regular_orders):
            raise RuntimeError("nautilus_open_regular_order_unproven")
        algo_orders, _ = await client._fetch_algo_orders(None)
        algo_reports = []
        for algo_order in algo_orders:
            report = client._parse_algo_order_report(algo_order, None, None)
            if report is None:
                raise RuntimeError("nautilus_open_algo_order_unproven")
            if report is not None:
                algo_reports.append(report)
    finally:
        client._active_symbols_cache = None
    return list(positions), [*regular_reports, *algo_reports]


async def _load_complete_hyperliquid_account_reports(client: Any) -> tuple[list[Any], list[Any]]:
    """Use the raw Rust requests so provider errors cannot be translated into truthful-looking empties."""

    positions = [
        PositionStatusReport.from_pyo3(value)
        for value in await client._client.request_position_status_reports(instrument_id=None)
    ]
    orders: list[OrderStatusReport] = []
    for value in await client._client.request_order_status_reports(instrument_id=None):
        report = OrderStatusReport.from_pyo3(value)
        report.client_order_id = client._resolve_cloid(report.client_order_id)
        orders.append(report)
    return positions, orders


__all__ = ["execution_clients", "load_complete_account_reports", "single_execution_client"]
