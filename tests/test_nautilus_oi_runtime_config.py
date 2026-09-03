"""Closed configuration and disabled app boundary for the OI Runtime."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal
from typing import cast

import pytest
from nautilus_trader.adapters.binance import BINANCE, BinanceAccountType
from nautilus_trader.adapters.binance.common.enums import BinanceEnvironment
from nautilus_trader.model.identifiers import AccountId

from tests.nautilus_oi_runtime_fixtures import NOW_NS, oi_profile
from tracefold.app.nautilus.oi_runtime import run_nautilus
from tracefold.app.nautilus.root import _build_active_node
from tracefold.integrations.nautilus.oi_runtime.audit_sink import AuditSink, ObservationFactory
from tracefold.integrations.nautilus.oi_runtime.config import (
    BinanceRuntimeCredentials,
    RuntimeMode,
    build_oi_node_config,
)
from tracefold.integrations.nautilus.oi_runtime.nautilus_1231_binance_compat import (
    single_binance_execution_client,
)
from tracefold.integrations.nautilus.oi_runtime.risk import DayStartBaseline
from tracefold.integrations.nautilus.oi_runtime.signal_client import ExecutionSignalClient
from tracefold.integrations.nautilus.oi_runtime.state import RuntimeReadiness
from tracefold.integrations.nautilus.oi_runtime.strategy import OiNautilusStrategy


@pytest.mark.parametrize(
    ("mode", "environment"),
    [("paper", BinanceEnvironment.DEMO), ("live", BinanceEnvironment.LIVE)],
)
def test_paper_and_live_change_only_cold_identity_and_binance_environment(
    mode: RuntimeMode,
    environment: BinanceEnvironment,
) -> None:
    profile = oi_profile(mode)
    config = build_oi_node_config(
        profile,
        BinanceRuntimeCredentials(api_key=f"{mode}-key", api_secret=f"{mode}-secret"),
    )

    assert set(config.data_clients) == set(config.exec_clients) == {BINANCE}
    assert config.data_clients[BINANCE].environment == environment
    execution = config.exec_clients[BINANCE]
    assert execution.environment == environment
    assert execution.account_type == BinanceAccountType.USDT_FUTURES
    assert execution.use_reduce_only is True
    assert execution.max_retries is None
    assert config.risk_engine.bypass is False
    assert config.exec_engine.reconciliation is False
    assert config.exec_engine.reconciliation_instrument_ids is None
    assert config.exec_engine.generate_missing_orders is True
    assert config.exec_engine.inflight_check_interval_ms == 2_000
    assert config.exec_engine.open_check_interval_secs == 5.0
    assert config.exec_engine.open_check_open_only is False
    assert config.exec_engine.position_check_interval_secs == 5.0
    assert config.cache.flush_on_start is False
    assert config.cache.use_trader_prefix is True
    assert config.cache.use_instance_id is True


def test_disabled_helper_constructs_no_node_and_rejects_active_profiles() -> None:
    profile = oi_profile("disabled")
    readiness = run_nautilus(profile)

    assert readiness.mode == "disabled"
    assert readiness.alive is False
    assert readiness.execution_safe is False
    assert readiness.entries_armed is False
    assert readiness.entry_block_reason == "disabled"
    with pytest.raises(ValueError, match="oi_runtime_disabled_has_no_node"):
        build_oi_node_config(profile, BinanceRuntimeCredentials(api_key="x", api_secret="y"))
    with pytest.raises(RuntimeError, match="oi_runtime_active_profile_requires_composition_root"):
        run_nautilus(oi_profile("paper"))


def test_unknown_mode_fails_closed_instead_of_falling_through_to_live() -> None:
    with pytest.raises(ValueError, match="oi_runtime_mode_invalid"):
        replace(oi_profile("paper"), mode=cast(RuntimeMode, "staging"))


def test_credentials_never_expose_secrets_in_repr() -> None:
    credentials = BinanceRuntimeCredentials(api_key="visible-key", api_secret="visible-secret")
    node = build_oi_node_config(oi_profile("paper"), credentials)

    assert "visible-key" not in repr(credentials)
    assert "visible-secret" not in repr(credentials)
    assert "visible-key" not in repr(node)
    assert "visible-secret" not in repr(node)


def test_paper_and_live_have_disjoint_profile_cache_credential_and_client_namespaces() -> None:
    paper = oi_profile("paper")
    live = oi_profile("live")
    paper_node = build_oi_node_config(paper, BinanceRuntimeCredentials("paper-key", "paper-secret"))
    live_node = build_oi_node_config(live, BinanceRuntimeCredentials("live-key", "live-secret"))

    assert paper.profile_id != live.profile_id
    assert paper.credential_namespace != live.credential_namespace
    assert paper.cache_namespace != live.cache_namespace
    assert paper.client_order_namespace != live.client_order_namespace
    assert paper_node.trader_id != live_node.trader_id
    assert paper_node.instance_id != live_node.instance_id


def test_canonical_root_builds_one_real_binance_execution_client() -> None:
    profile = replace(oi_profile("paper"), account_id=AccountId("BINANCE-USDT_FUTURES-master"))
    signals = ExecutionSignalClient(runtime_profile_id=profile.profile_id, execution_strategy="oi_nautilus_v1")
    strategy = OiNautilusStrategy(
        profile=profile,
        signals=signals,
        audit=AuditSink(
            factory=ObservationFactory(
                runtime_profile_id=profile.profile_id,
                runtime_release=profile.runtime_release,
                execution_strategy="oi_nautilus_v1",
            )
        ),
        readiness=RuntimeReadiness(),
        dispatch_pump=lambda pump: pump(),
        singleton_ready=lambda: True,
        control_plane_ready=lambda: True,
        day_start=DayStartBaseline(
            utc_day="2030-03-17",
            equity_usd=Decimal("1000"),
            recorded_at_ns=NOW_NS,
            event_id="4" * 64,
        ),
        request_reconciliation=lambda _reason: None,
    )
    loop = asyncio.new_event_loop()
    node = _build_active_node(
        profile=profile,
        credentials=BinanceRuntimeCredentials("paper-key", "paper-secret"),
        strategy=strategy,
        loop=loop,
    )
    try:
        client = single_binance_execution_client(node.kernel.exec_engine)
        assert client.account_id == profile.account_id
    finally:
        node.dispose()
        loop.close()
