"""Fail-closed account report loading for the pinned Binance futures client."""

from __future__ import annotations

from typing import Any


async def load_complete_account_reports(client: Any) -> tuple[list[Any], list[Any]]:
    """Return active positions and every regular/algo open order, or propagate failure.

    Nautilus 1.231.0's public report methods translate Binance errors into empty or
    partial lists. The pinned internal report steps retain the same parsing while
    allowing provider errors to reach Tracefold's fail-closed reconciliation seam.
    """

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


__all__ = ["load_complete_account_reports"]
