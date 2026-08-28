"""Pinned public Nautilus capability contract for #283."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.slow]


def test_public_v1_trading_node_config_is_demo_only_reconciling_and_in_memory() -> None:
    from nautilus_trader.adapters.binance import BINANCE, BinanceAccountType
    from nautilus_trader.adapters.binance.common.enums import BinanceEnvironment
    from nautilus_trader.live.node import TradingNode
    from nautilus_trader.model.identifiers import InstrumentId

    from tracefold.integrations.nautilus import NAUTILUS_LINUX_WHEELS, NAUTILUS_RELEASE, build_node_config

    instrument_id = InstrumentId.from_str("SOLUSDT-PERP.BINANCE")
    config = build_node_config(
        api_key="demo-key",
        api_secret="demo-secret",
        instrument_ids=[instrument_id, InstrumentId.from_str("BTCUSDT-PERP.BINANCE")],
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
    assert execution.account_type == BinanceAccountType.USDT_FUTURES
    assert execution.environment == BinanceEnvironment.DEMO
    assert execution.use_reduce_only is True
    assert execution.max_retries is None
    assert execution.api_key == "demo-key"
    assert execution.api_secret == "demo-secret"
