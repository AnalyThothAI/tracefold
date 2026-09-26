"""Closed configuration and disabled app boundary for the OI Runtime."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from nautilus_trader.adapters.binance import BINANCE, BinanceAccountType
from nautilus_trader.adapters.binance.common.enums import BinanceEnvironment
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import AccountId
from nautilus_trader.model.objects import Quantity

from tests.nautilus_oi_runtime_fixtures import RESUMED, oi_profile
from tracefold.app.nautilus.root import _build_active_node, _discover_routes
from tracefold.integrations.nautilus.oi_runtime.binance import OiBinanceFuturesExecutionClient
from tracefold.integrations.nautilus.oi_runtime.config import (
    BinanceRuntimeCredentials,
    build_oi_node_config,
    connection_environment,
)
from tracefold.integrations.nautilus.oi_runtime.journal import ExecutionJournal, ObservationFactory
from tracefold.integrations.nautilus.oi_runtime.signal_client import ExecutionSignalClient
from tracefold.integrations.nautilus.oi_runtime.strategy import OiNautilusStrategy, RuntimeInputs


@pytest.mark.parametrize(
    ("environment", "expected"),
    [
        (BinanceEnvironment.DEMO, "demo"),
        (BinanceEnvironment.LIVE, "live"),
        (BinanceEnvironment.TESTNET, "testnet"),
        (None, "live"),
    ],
)
def test_native_binance_environment_is_shared_by_data_and_execution(
    environment: BinanceEnvironment | None,
    expected: str,
) -> None:
    profile = replace(oi_profile(), environment=environment)
    config = build_oi_node_config(
        profile,
        BinanceRuntimeCredentials(api_key="test-key", api_secret="test-secret"),
    )

    assert set(config.data_clients) == set(config.exec_clients) == {BINANCE}
    assert config.data_clients[BINANCE].environment == environment
    execution = config.exec_clients[BINANCE]
    assert execution.environment == environment
    assert connection_environment(environment) == expected
    assert execution.account_type == BinanceAccountType.USDT_FUTURES
    assert execution.use_reduce_only is True
    assert execution.max_retries is None
    assert config.risk_engine.bypass is False
    # Nautilus owns execution state (#680): it reconciles the venue before the Strategy starts, over
    # more history than the longest holding time, and keeps the Cache converged every five seconds
    # over open orders only.
    engine = config.exec_engine
    assert engine.reconciliation is True
    assert engine.reconciliation_lookback_mins == profile.reconciliation_lookback_mins >= 1_440
    assert engine.reconciliation_instrument_ids is None
    # Reconciliation applies the venue's own orders and fills and never invents one to make the Cache
    # match a position report: a positionRisk error answered "no reports", and the position check
    # closed a held position with a synthetic fill (#680 PR-3, Path B).
    assert engine.generate_missing_orders is False
    assert engine.inflight_check_retries > 0
    assert engine.open_check_interval_secs == 5.0
    assert engine.open_check_open_only is True
    assert engine.position_check_interval_secs == 5.0
    assert engine.graceful_shutdown_on_exception is True
    assert config.cache.database is None
    assert config.cache.flush_on_start is False
    assert config.cache.use_trader_prefix is True
    assert config.cache.use_instance_id is True
    # Without a log directory (every test) Nautilus writes to stdout only.
    assert config.logging.log_directory is None and config.logging.log_file_name is None


def test_nautilus_warnings_and_errors_are_kept_in_a_bounded_file_under_the_logs_directory(tmp_path: Path) -> None:
    """A reconciliation decision must outlive the container that made it (#680 PR-3)."""

    config = build_oi_node_config(
        oi_profile(BinanceEnvironment.DEMO),
        BinanceRuntimeCredentials("paper-key", "paper-secret"),
        log_directory=tmp_path,
    )

    logging = config.logging
    assert (logging.log_level, logging.log_level_file) == ("WARNING", "WARNING")
    assert logging.log_directory == str(tmp_path)
    assert logging.log_file_name == "nautilus-engine"
    # Size-rotated: at most one current file and five backups of 10 MiB.
    assert logging.log_file_max_size == 10 * 1024 * 1024
    assert logging.log_file_max_backup_count == 5


def test_catalogue_uses_the_selected_native_connection() -> None:
    root_source = Path(_discover_routes.__code__.co_filename).read_text(encoding="utf-8")
    assert '"environment": environment' in root_source


def test_credentials_never_expose_secrets_in_repr() -> None:
    credentials = BinanceRuntimeCredentials(api_key="visible-key", api_secret="visible-secret")
    node = build_oi_node_config(oi_profile(BinanceEnvironment.DEMO), credentials)

    assert "visible-key" not in repr(credentials)
    assert "visible-secret" not in repr(credentials)
    assert "visible-key" not in repr(node)
    assert "visible-secret" not in repr(node)


def test_historical_opaque_namespaces_keep_disjoint_client_identifiers() -> None:
    paper = oi_profile(BinanceEnvironment.DEMO)
    live = oi_profile(BinanceEnvironment.LIVE)
    paper_node = build_oi_node_config(paper, BinanceRuntimeCredentials("paper-key", "paper-secret"))
    live_node = build_oi_node_config(live, BinanceRuntimeCredentials("live-key", "live-secret"))

    # The migration retains old opaque namespaces, so existing venue orders keep their identity.
    assert paper.account_slot == live.account_slot
    assert paper.namespace != live.namespace
    assert paper_node.trader_id != live_node.trader_id
    assert paper_node.instance_id != live_node.instance_id


_MASTER_ACCOUNT_ID = AccountId("BINANCE-USDT_FUTURES-master")


@pytest.fixture(name="real_node", scope="module")
def _real_node() -> Iterator[Any]:
    """The composition root's own node graph, built offline with throwaway credentials.

    Nothing here talks to Binance: `_build_active_node` only constructs the graph.
    """

    profile = replace(oi_profile(BinanceEnvironment.DEMO), account_id=_MASTER_ACCOUNT_ID)
    journal = ExecutionJournal(factory=ObservationFactory(profile.account_slot, "oi_nautilus_v1"))
    strategy = OiNautilusStrategy(
        profile=profile,
        signals=ExecutionSignalClient(account_slot=profile.account_slot, execution_strategy="oi_nautilus_v1"),
        journal=journal,
        inputs=RuntimeInputs(control=RESUMED),
        dispatch_pump=lambda pump: pump(),
        singleton_ready=lambda: True,
        venue_reads=True,
    )
    loop = asyncio.new_event_loop()
    node = _build_active_node(
        journal=journal,
        profile=profile,
        credentials=BinanceRuntimeCredentials("paper-key", "paper-secret"),
        strategy=strategy,
        loop=loop,
        recovery_symbols=frozenset(),
    )
    try:
        yield node
    finally:
        node.dispose()
        loop.close()


def test_the_canonical_root_builds_one_binance_execution_client_and_one_claiming_strategy(real_node: Any) -> None:
    assert [client.value for client in real_node.kernel.exec_engine.registered_clients] == [BINANCE]
    [strategy] = real_node.trader.strategies()
    assert strategy.external_order_claims == [route.instrument_id for route in oi_profile().routes]
    # Every order routes to Nautilus' own Binance USD-M client, the one whose fill reports name each
    # venue trade once (#680 PR-3).
    order = strategy.order_factory.market(
        instrument_id=oi_profile().routes[0].instrument_id, order_side=OrderSide.BUY, quantity=Quantity.from_str("1")
    )
    [client] = real_node.kernel.exec_engine.get_clients_for_orders([order])
    assert type(client) is OiBinanceFuturesExecutionClient
