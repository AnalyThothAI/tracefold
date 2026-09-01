"""Complete Binance private-account proof and Cache projection."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from tracefold.app.nautilus.reconciliation import account_reports_are_flat, reconcile_reports_into_cache
from tracefold.integrations.nautilus.oi_runtime.nautilus_1231_binance_compat import (
    CompleteBinanceAccountReports,
    load_complete_binance_account_reports,
)


class _CompleteClient:
    venue = SimpleNamespace(value="BINANCE")

    def __init__(self) -> None:
        self._active_symbols_cache: Any = {"stale"}
        self.position = SimpleNamespace(kind="position")
        self.regular = SimpleNamespace(kind="regular")
        self.algo = SimpleNamespace(kind="algo")

    async def _get_binance_position_status_reports(self) -> list[Any]:
        return [self.position]

    async def _build_active_symbols(self, _command: Any) -> tuple[set[str], list[str]]:
        return {"BTCUSDT"}, ["regular-native"]

    def _parse_order_status_reports(self, values: list[str], _start: Any, _end: Any) -> list[Any]:
        assert values == ["regular-native"]
        return [self.regular]

    async def _fetch_algo_orders(self, _command: Any) -> tuple[list[str], dict[str, str]]:
        return ["algo-native"], {}

    def _parse_algo_order_report(self, value: str, _start: Any, _end: Any) -> Any:
        assert value == "algo-native"
        return self.algo


def test_complete_private_report_keeps_positions_regular_and_algo_orders_distinct() -> None:
    client = _CompleteClient()

    reports = asyncio.run(load_complete_binance_account_reports(client))

    assert reports == CompleteBinanceAccountReports(
        positions=(client.position,),
        regular_orders=(client.regular,),
        algo_orders=(client.algo,),
    )
    assert reports.orders == (client.regular, client.algo)
    assert account_reports_are_flat(reports) is False
    assert account_reports_are_flat(CompleteBinanceAccountReports((), (), ())) is True
    assert client._active_symbols_cache is None


def test_private_report_error_propagates_and_never_leaves_the_active_symbol_cache_claimed() -> None:
    class _BrokenClient(_CompleteClient):
        async def _fetch_algo_orders(self, _command: Any) -> tuple[list[str], dict[str, str]]:
            raise RuntimeError("binance-private-unavailable")

    client = _BrokenClient()

    with pytest.raises(RuntimeError, match="binance-private-unavailable"):
        asyncio.run(load_complete_binance_account_reports(client))

    assert client._active_symbols_cache is None


def test_unparseable_algo_order_fails_the_complete_account_proof() -> None:
    class _IncompleteClient(_CompleteClient):
        def _parse_algo_order_report(self, value: str, _start: Any, _end: Any) -> None:
            assert value == "algo-native"

    with pytest.raises(RuntimeError, match="oi_runtime_algo_order_report_incomplete"):
        asyncio.run(load_complete_binance_account_reports(_IncompleteClient()))


def test_cache_projection_includes_all_three_report_classes_and_fails_on_any_rejection() -> None:
    reports = CompleteBinanceAccountReports(
        positions=("position",),
        regular_orders=("regular",),
        algo_orders=("algo",),
    )
    accepted: list[str] = []
    engine = SimpleNamespace(reconcile_execution_report=lambda report: accepted.append(report) or True)

    reconcile_reports_into_cache(engine=engine, reports=reports)
    assert accepted == ["position", "regular", "algo"]

    rejecting = SimpleNamespace(reconcile_execution_report=lambda report: report != "algo")
    with pytest.raises(RuntimeError, match="oi_runtime_execution_report_reconciliation_failed"):
        reconcile_reports_into_cache(engine=rejecting, reports=reports)
