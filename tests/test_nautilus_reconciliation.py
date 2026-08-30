from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from nautilus_trader.adapters.binance.common.enums import (
    BinanceAccountType,
    BinanceEnvironment,
    BinanceFuturesPositionSide,
)
from nautilus_trader.adapters.binance.common.schemas.account import BinanceOrder
from nautilus_trader.adapters.binance.common.urls import get_http_base_url
from nautilus_trader.adapters.binance.config import BinanceExecClientConfig
from nautilus_trader.adapters.binance.futures.execution import BinanceFuturesExecutionClient
from nautilus_trader.adapters.binance.futures.http.account import BinanceFuturesAccountHttpAPI
from nautilus_trader.adapters.binance.futures.providers import BinanceFuturesInstrumentProvider
from nautilus_trader.adapters.binance.futures.schemas.account import (
    BinanceFuturesAlgoOrder,
    BinanceFuturesPositionRisk,
)
from nautilus_trader.adapters.binance.http.account import BinanceAccountHttpAPI
from nautilus_trader.adapters.binance.http.client import BinanceHttpClient
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.model.identifiers import TraderId

from tracefold.app.nautilus import root
from tracefold.integrations.nautilus.messages import (
    StartupAccountReconciliationConfirmed,
    StartupAccountReconciliationUnproven,
    strategy_queues,
)
from tracefold.integrations.nautilus.reconciliation import load_complete_account_reports


@pytest.fixture
def nautilus_client() -> Iterator[BinanceFuturesExecutionClient]:
    loop = asyncio.new_event_loop()
    clock = LiveClock()
    account_type = BinanceAccountType.USDT_FUTURES
    environment = BinanceEnvironment.DEMO
    http = BinanceHttpClient(
        clock=clock,
        api_key="test-key",
        api_secret="test-secret",
        base_url=get_http_base_url(account_type, environment, is_us=False),
    )
    provider_config = InstrumentProviderConfig(load_all=False)
    provider = BinanceFuturesInstrumentProvider(
        client=http,
        clock=clock,
        account_type=account_type,
        config=provider_config,
    )
    client = BinanceFuturesExecutionClient(
        loop=loop,
        client=http,
        msgbus=MessageBus(TraderId("TRACEFOLD-001"), clock),
        cache=Cache(),
        clock=clock,
        instrument_provider=provider,
        base_url_ws="wss://example.invalid",
        config=BinanceExecClientConfig(
            api_key="test-key",
            api_secret="test-secret",
            account_type=account_type,
            environment=environment,
            instrument_provider=provider_config,
            max_retries=None,
        ),
        account_type=account_type,
        environment=environment,
        api_key="test-key",
        api_secret="test-secret",
    )
    yield client
    loop.close()


def _empty_account(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_positions(
        _api: BinanceFuturesAccountHttpAPI,
        _symbol: str | None = None,
        _recv_window: str | None = None,
    ) -> list[object]:
        return []

    async def no_regular_orders(
        _api: BinanceAccountHttpAPI,
        _symbol: str | None = None,
        _recv_window: str | None = None,
    ) -> list[object]:
        return []

    monkeypatch.setattr(BinanceFuturesAccountHttpAPI, "query_futures_position_risk", no_positions)
    monkeypatch.setattr(BinanceAccountHttpAPI, "query_open_orders", no_regular_orders)


def test_complete_account_reports_proves_empty_provider_account(
    monkeypatch: pytest.MonkeyPatch,
    nautilus_client: BinanceFuturesExecutionClient,
) -> None:
    _empty_account(monkeypatch)

    async def no_algo_orders(
        _api: BinanceFuturesAccountHttpAPI,
        _symbol: str | None = None,
        _recv_window: str | None = None,
    ) -> list[BinanceFuturesAlgoOrder]:
        return []

    monkeypatch.setattr(BinanceFuturesAccountHttpAPI, "query_open_algo_orders", no_algo_orders)

    assert asyncio.run(load_complete_account_reports(nautilus_client)) == ([], [])
    client_id = object()
    engine = type(
        "Engine",
        (),
        {"registered_clients": [client_id], "_clients": {client_id: nautilus_client}},
    )()
    node = type(
        "Node",
        (),
        {
            "kernel": type(
                "Kernel",
                (),
                {
                    "clock": type("Clock", (), {"timestamp_ms": lambda _self: 1_900_000_000_000})(),
                    "exec_engine": engine,
                },
            )(),
        },
    )()
    queues = strategy_queues()

    asyncio.run(
        root._run_startup_account_reconciliation(
            node=node,
            queues=queues,
            bootstrap_account_zero=True,
        )
    )

    assert queues.commands.get_nowait() == StartupAccountReconciliationConfirmed(
        verified_at_ms=1_900_000_000_000,
        bootstrap_account_zero=True,
    )


def test_real_pinned_position_report_refuses_bootstrap_zero_proof(
    monkeypatch: pytest.MonkeyPatch,
    nautilus_client: BinanceFuturesExecutionClient,
) -> None:
    _empty_account(monkeypatch)
    position = BinanceFuturesPositionRisk(
        symbol="BTCUSDT",
        positionSide=BinanceFuturesPositionSide.BOTH,
        positionAmt="0.001",
        entryPrice="60000",
        markPrice="61000",
        unRealizedProfit="1",
        liquidationPrice="1000",
        isolatedMargin="0",
        updateTime=1_900_000_000_000,
    )

    async def one_position(
        _api: BinanceFuturesAccountHttpAPI,
        _symbol: str | None = None,
        _recv_window: str | None = None,
    ) -> list[BinanceFuturesPositionRisk]:
        return [position]

    async def no_algo_orders(
        _api: BinanceFuturesAccountHttpAPI,
        _symbol: str | None = None,
        _recv_window: str | None = None,
    ) -> list[BinanceFuturesAlgoOrder]:
        return []

    monkeypatch.setattr(BinanceFuturesAccountHttpAPI, "query_futures_position_risk", one_position)
    monkeypatch.setattr(BinanceFuturesAccountHttpAPI, "query_open_algo_orders", no_algo_orders)
    client_id = object()
    engine = type(
        "Engine",
        (),
        {"registered_clients": [client_id], "_clients": {client_id: nautilus_client}},
    )()
    node = type(
        "Node",
        (),
        {
            "kernel": type(
                "Kernel",
                (),
                {
                    "clock": type("Clock", (), {"timestamp_ms": lambda _self: 1_900_000_000_000})(),
                    "exec_engine": engine,
                },
            )(),
        },
    )()
    queues = strategy_queues()

    asyncio.run(
        root._run_startup_account_reconciliation(
            node=node,
            queues=queues,
            bootstrap_account_zero=True,
        )
    )

    assert queues.commands.get_nowait() == StartupAccountReconciliationUnproven(
        observed_at_ms=1_900_000_000_000,
        unexpected_exposure=True,
    )


def test_active_startup_reconciles_every_complete_account_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_id = object()
    reports = [type("Report", (), {"account_id": account_id})() for _ in range(3)]
    reconciled: list[object] = []
    client = type("Client", (), {"account_id": account_id})()

    async def complete_reports(_client: object) -> tuple[list[object], list[object]]:
        return reports[:1], reports[1:]

    engine = type(
        "Engine",
        (),
        {"reconcile_execution_report": lambda _self, report: not reconciled.append(report)},
    )()
    node = type(
        "Node",
        (),
        {
            "kernel": type(
                "Kernel",
                (),
                {
                    "clock": type("Clock", (), {"timestamp_ms": lambda _self: 1_900_000_000_000})(),
                    "exec_engine": engine,
                },
            )(),
        },
    )()
    queues = strategy_queues()
    monkeypatch.setattr(root, "execution_clients", lambda _engine: {"BINANCE_USDM": client})
    monkeypatch.setattr(root, "load_complete_account_reports", complete_reports)

    asyncio.run(
        root._run_startup_account_reconciliation(
            node=node,
            queues=queues,
            bootstrap_account_zero=False,
        )
    )

    assert reconciled == reports
    assert queues.commands.get_nowait() == StartupAccountReconciliationConfirmed(
        verified_at_ms=1_900_000_000_000,
        bootstrap_account_zero=False,
    )


def test_partial_algo_order_query_failure_is_not_an_empty_account_proof(
    monkeypatch: pytest.MonkeyPatch,
    nautilus_client: BinanceFuturesExecutionClient,
) -> None:
    _empty_account(monkeypatch)

    async def failed_algo_query(
        _api: BinanceFuturesAccountHttpAPI,
        _symbol: str | None = None,
        _recv_window: str | None = None,
    ) -> list[BinanceFuturesAlgoOrder]:
        raise RuntimeError("binance_algo_query_failed")

    monkeypatch.setattr(BinanceFuturesAccountHttpAPI, "query_open_algo_orders", failed_algo_query)

    with pytest.raises(RuntimeError, match="binance_algo_query_failed"):
        asyncio.run(load_complete_account_reports(nautilus_client))


def test_unparseable_open_regular_order_is_not_an_empty_account_proof(
    monkeypatch: pytest.MonkeyPatch,
    nautilus_client: BinanceFuturesExecutionClient,
) -> None:
    _empty_account(monkeypatch)
    malformed_order = BinanceOrder(
        symbol="SOLUSDT",
        orderId=1,
        clientOrderId="tracefold-1",
        origQty="0",
    )

    async def malformed_regular_query(
        _api: BinanceAccountHttpAPI,
        _symbol: str | None = None,
        _recv_window: str | None = None,
    ) -> list[BinanceOrder]:
        return [malformed_order]

    async def no_algo_orders(
        _api: BinanceFuturesAccountHttpAPI,
        _symbol: str | None = None,
        _recv_window: str | None = None,
    ) -> list[BinanceFuturesAlgoOrder]:
        return []

    monkeypatch.setattr(BinanceAccountHttpAPI, "query_open_orders", malformed_regular_query)
    monkeypatch.setattr(BinanceFuturesAccountHttpAPI, "query_open_algo_orders", no_algo_orders)

    with pytest.raises(RuntimeError, match="nautilus_open_regular_order_unproven"):
        asyncio.run(load_complete_account_reports(nautilus_client))


def test_unparseable_open_algo_order_is_not_an_empty_account_proof(
    monkeypatch: pytest.MonkeyPatch,
    nautilus_client: BinanceFuturesExecutionClient,
) -> None:
    _empty_account(monkeypatch)
    malformed_order = BinanceFuturesAlgoOrder(
        algoId=1,
        clientAlgoId="tracefold-1",
        algoType="CONDITIONAL",
        orderType="STOP_MARKET",
        symbol="SOLUSDT",
        side="BUY",
        quantity=None,
    )

    async def malformed_algo_query(
        _api: BinanceFuturesAccountHttpAPI,
        _symbol: str | None = None,
        _recv_window: str | None = None,
    ) -> list[BinanceFuturesAlgoOrder]:
        return [malformed_order]

    monkeypatch.setattr(BinanceFuturesAccountHttpAPI, "query_open_algo_orders", malformed_algo_query)

    with pytest.raises(RuntimeError, match="nautilus_open_algo_order_unproven"):
        asyncio.run(load_complete_account_reports(nautilus_client))


def test_position_query_failure_is_not_an_empty_account_proof(
    monkeypatch: pytest.MonkeyPatch,
    nautilus_client: BinanceFuturesExecutionClient,
) -> None:
    async def failed_position_query(
        _api: BinanceFuturesAccountHttpAPI,
        _symbol: str | None = None,
        _recv_window: str | None = None,
    ) -> list[object]:
        raise RuntimeError("binance_position_query_failed")

    monkeypatch.setattr(
        BinanceFuturesAccountHttpAPI,
        "query_futures_position_risk",
        failed_position_query,
    )

    with pytest.raises(RuntimeError, match="binance_position_query_failed"):
        asyncio.run(load_complete_account_reports(nautilus_client))


def test_bootstrap_transport_failure_never_becomes_account_zero(
    monkeypatch: pytest.MonkeyPatch,
    nautilus_client: BinanceFuturesExecutionClient,
) -> None:
    async def failed_position_query(
        _api: BinanceFuturesAccountHttpAPI,
        _symbol: str | None = None,
        _recv_window: str | None = None,
    ) -> list[object]:
        raise RuntimeError("binance_position_query_failed")

    monkeypatch.setattr(
        BinanceFuturesAccountHttpAPI,
        "query_futures_position_risk",
        failed_position_query,
    )
    queues = strategy_queues()
    client_id = object()
    engine = type(
        "Engine",
        (),
        {"registered_clients": [client_id], "_clients": {client_id: nautilus_client}},
    )()
    node = type(
        "Node",
        (),
        {
            "kernel": type(
                "Kernel",
                (),
                {
                    "clock": type("Clock", (), {"timestamp_ms": lambda _self: 1_900_000_000_000})(),
                    "exec_engine": engine,
                },
            )(),
        },
    )()

    asyncio.run(
        root._run_startup_account_reconciliation(
            node=node,
            queues=queues,
            bootstrap_account_zero=True,
        )
    )

    assert queues.commands.get_nowait() == StartupAccountReconciliationUnproven(
        observed_at_ms=1_900_000_000_000,
        unexpected_exposure=False,
    )
