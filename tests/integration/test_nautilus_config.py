"""Pinned public Nautilus capability contract for #283."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.slow]


def test_production_v3_binance_node_is_mainnet_reconciling_and_in_memory() -> None:
    from nautilus_trader.adapters.binance import BINANCE, BinanceAccountType
    from nautilus_trader.adapters.binance.common.enums import BinanceEnvironment
    from nautilus_trader.live.node import TradingNode
    from nautilus_trader.model.identifiers import InstrumentId

    from tracefold.integrations.nautilus import (
        NAUTILUS_LINUX_WHEELS,
        NAUTILUS_RELEASE,
        BinanceCredentials,
        build_node_config,
    )

    instrument_id = InstrumentId.from_str("SOLUSDT-PERP.BINANCE")
    config = build_node_config(
        binance_credentials=BinanceCredentials(api_key="mainnet-key", api_secret="mainnet-secret"),
        hyperliquid_credentials=None,
        instrument_ids_by_binding={"BINANCE_USDM": [instrument_id, InstrumentId.from_str("BTCUSDT-PERP.BINANCE")]},
    )

    assert TradingNode.__module__ == "nautilus_trader.live.node"
    assert NAUTILUS_RELEASE.version == "1.231.0"
    assert NAUTILUS_RELEASE.git_tag == "v1.231.0"
    assert NAUTILUS_RELEASE.git_commit == "27a8e54e7ac3c57d6cbf8891f0283dfbaee97317"
    assert NAUTILUS_LINUX_WHEELS["x86_64"] == (
        "cp313-cp313-manylinux_2_35_x86_64",
        "429ea61c33a32cd8498d39e0ea95ebaa12b8dbfc25c71fbaba845f2b05e8ab91",
    )
    assert NAUTILUS_LINUX_WHEELS["aarch64"] == (
        "cp313-cp313-manylinux_2_35_aarch64",
        "e536d7c925b3c475bef4f3f8e75196944f6b8758710e41da1109b8b837001690",
    )
    assert config.cache is not None
    assert config.logging.log_level == "WARNING"
    assert config.cache.database is None
    assert config.cache.flush_on_start is False
    assert config.exec_engine.reconciliation is True
    # Reconcile the whole dedicated account so another symbol cannot remain invisible exposure.
    assert config.exec_engine.reconciliation_instrument_ids is None
    assert config.exec_engine.inflight_check_interval_ms == 0
    assert config.exec_engine.open_check_interval_secs == 5.0
    assert config.exec_engine.open_check_open_only is False
    assert config.exec_engine.position_check_interval_secs == 30.0
    execution = config.exec_clients[BINANCE]
    assert config.data_clients[BINANCE].instrument_provider.query_commission_rates is False
    assert execution.instrument_provider.query_commission_rates is False
    assert execution.account_type == BinanceAccountType.USDT_FUTURES
    assert execution.environment == BinanceEnvironment.LIVE
    assert execution.use_reduce_only is True
    assert execution.max_retries is None
    assert execution.api_key == "mainnet-key"
    assert execution.api_secret == "mainnet-secret"


def test_public_v1_node_config_allows_zero_claim_bootstrap() -> None:
    import asyncio

    from nautilus_trader.adapters.binance import (
        BINANCE,
        BinanceLiveDataClientFactory,
        BinanceLiveExecClientFactory,
    )
    from nautilus_trader.live.node import TradingNode

    from tracefold.integrations.nautilus import BinanceCredentials, build_node_config, single_execution_client

    config = build_node_config(
        binance_credentials=BinanceCredentials(api_key="mainnet-key", api_secret="mainnet-secret"),
        hyperliquid_credentials=None,
        instrument_ids_by_binding={"BINANCE_USDM": []},
    )

    assert config.data_clients[BINANCE].instrument_provider.load_ids == frozenset()
    assert config.exec_clients[BINANCE].instrument_provider.load_ids == frozenset()
    loop = asyncio.new_event_loop()
    node = TradingNode(config=config, loop=loop)
    try:
        node.add_data_client_factory(BINANCE, BinanceLiveDataClientFactory)
        node.add_exec_client_factory(BINANCE, BinanceLiveExecClientFactory)
        node.build()
        assert single_execution_client(node.kernel.exec_engine).__class__.__name__ == "BinanceFuturesExecutionClient"
    finally:
        node.dispose()
        loop.close()


@pytest.mark.parametrize(
    ("mode", "binance", "hyperliquid", "expected"),
    [
        ("zero", False, False, set()),
        ("binance", True, False, {"BINANCE"}),
        ("hyperliquid", False, True, {"HYPERLIQUID"}),
        ("dual", True, True, {"BINANCE", "HYPERLIQUID"}),
    ],
)
def test_closed_startup_graphs_have_no_dynamic_provider_registry(
    mode: str,
    binance: bool,
    hyperliquid: bool,
    expected: set[str],
) -> None:
    del mode
    from nautilus_trader.adapters.hyperliquid import HYPERLIQUID
    from nautilus_trader.model.identifiers import InstrumentId

    from tracefold.integrations.nautilus import (
        BinanceCredentials,
        HyperliquidCredentials,
        build_node_config,
    )

    config = build_node_config(
        binance_credentials=(
            BinanceCredentials(api_key="mainnet-key", api_secret="mainnet-secret") if binance else None
        ),
        hyperliquid_credentials=(
            HyperliquidCredentials(private_key="1" * 64, account_address="0x" + "2" * 40) if hyperliquid else None
        ),
        instrument_ids_by_binding={
            "BINANCE_USDM": [InstrumentId.from_str("BTCUSDT-PERP.BINANCE")],
            "HYPERLIQUID_PERP": [InstrumentId.from_str("BTC-PERP.HYPERLIQUID")],
        },
    )

    assert set(config.data_clients) == expected
    assert set(config.exec_clients) == expected
    if hyperliquid:
        execution = config.exec_clients[HYPERLIQUID]
        assert execution.max_retries == 3
        assert execution.retry_delay_initial_ms == 250
        assert execution.retry_delay_max_ms == 2_000
        assert execution.normalize_prices is True
