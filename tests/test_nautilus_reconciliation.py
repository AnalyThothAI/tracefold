"""Complete Binance private-account proof and Cache projection."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from tracefold.app.nautilus.reconciliation import reconcile_reports_into_cache
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
    assert reports.account_flat is False
    assert CompleteBinanceAccountReports((), (), ()).account_flat is True
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


@pytest.mark.parametrize(
    "reply,expected",
    [
        ("filled", "terminal"),
        ("absent", "absent"),
        ("working", "working"),
        ("auth_error", None),
        ("wrong_identity", None),
    ],
)
def test_planned_entry_query_uses_the_pinned_binance_http_contract(reply: str, expected: str | None) -> None:
    import json
    from decimal import Decimal
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    from urllib.parse import parse_qs, urlsplit

    from nautilus_trader.adapters.binance.futures.http.account import BinanceFuturesAccountHttpAPI
    from nautilus_trader.adapters.binance.http.client import BinanceHttpClient
    from nautilus_trader.adapters.binance.http.error import BinanceError
    from nautilus_trader.common.component import LiveClock
    from nautilus_trader.model.identifiers import InstrumentId

    from tests.nautilus_oi_runtime_fixtures import trade_plan_for_entry, trade_signal
    from tracefold.integrations.nautilus.oi_runtime.nautilus_1231_binance_compat import query_planned_entry
    from tracefold.integrations.nautilus.oi_runtime.state import RuntimeEntryRequest

    plan = trade_plan_for_entry(RuntimeEntryRequest.from_signal(trade_signal()))
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = urlsplit(self.path)
            requests.append((path.path, parse_qs(path.query)))
            if reply in {"absent", "auth_error"}:
                body = {"code": -2013 if reply == "absent" else -2015, "msg": "test rejection"}
                status = 400 if reply == "absent" else 401
            else:
                body = {
                    "symbol": "BTCUSDT",
                    "orderId": 100,
                    "clientOrderId": "wrong" if reply == "wrong_identity" else plan.entry_client_order_id,
                    "side": "BUY",
                    "type": "MARKET",
                    "origQty": str(plan.entry_quantity),
                    "executedQty": "0" if reply == "working" else str(plan.entry_quantity),
                    "status": "NEW" if reply == "working" else "FILLED",
                    "reduceOnly": False,
                }
                status = 200
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        clock = LiveClock()
        http = BinanceHttpClient(
            clock=clock, api_key="unit-test", api_secret="unit-test", base_url=f"http://127.0.0.1:{server.server_port}"
        )
        account = BinanceFuturesAccountHttpAPI(client=http, clock=clock)
        client = SimpleNamespace(
            _http_account=account,
            _get_cached_instrument_id=lambda symbol: InstrumentId.from_str(f"{symbol}-PERP.BINANCE"),
        )
        if expected is None:
            with pytest.raises((BinanceError, RuntimeError)):
                asyncio.run(query_planned_entry(client, plan))
        else:
            proof = asyncio.run(query_planned_entry(client, plan))
            assert proof.status == expected
            assert proof.entry_id == plan.entry_id
            if reply == "filled":
                assert proof.filled_quantity == Decimal("0.049")
        assert len(requests) == 1
        path, query = requests[0]
        assert path == "/fapi/v1/order"
        assert query["origClientOrderId"] == [plan.entry_client_order_id]
        assert query["symbol"] == ["BTCUSDT"]
        assert "signature" in query
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
