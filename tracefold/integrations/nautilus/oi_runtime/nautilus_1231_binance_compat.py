"""Pinned Nautilus 1.231 Binance private-account compatibility seam."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


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


__all__ = [
    "CompleteBinanceAccountReports",
    "load_complete_binance_account_reports",
    "single_binance_execution_client",
]
