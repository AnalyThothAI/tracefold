"""Closed configuration and dormant app boundary for #433-B."""

from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from nautilus_trader.adapters.binance import BINANCE, BinanceAccountType
from nautilus_trader.adapters.binance.common.enums import BinanceEnvironment

from tests.nautilus_oi_runtime_fixtures import oi_profile
from tracefold.app.nautilus.oi_runtime import run_nautilus
from tracefold.integrations.nautilus.oi_runtime.config import (
    BinanceRuntimeCredentials,
    RuntimeMode,
    build_oi_node_config,
)


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
    assert config.exec_engine.reconciliation is True
    assert config.exec_engine.reconciliation_instrument_ids is None
    assert config.exec_engine.generate_missing_orders is True
    assert config.exec_engine.inflight_check_interval_ms == 2_000
    assert config.exec_engine.open_check_interval_secs == 5.0
    assert config.exec_engine.open_check_open_only is False
    assert config.exec_engine.position_check_interval_secs == 5.0
    assert config.cache.flush_on_start is False
    assert config.cache.use_trader_prefix is True
    assert config.cache.use_instance_id is True


def test_disabled_is_the_only_reachable_app_state_and_constructs_no_node() -> None:
    profile = oi_profile("disabled")
    readiness = run_nautilus(profile)

    assert readiness.mode == "disabled"
    assert readiness.ready is False
    assert readiness.reason == "disabled"
    with pytest.raises(ValueError, match="oi_runtime_disabled_has_no_node"):
        build_oi_node_config(profile, BinanceRuntimeCredentials(api_key="x", api_secret="y"))
    with pytest.raises(RuntimeError, match="oi_runtime_activation_not_available_before_433e"):
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


def test_canonical_root_references_only_new_runtime_and_remains_disabled() -> None:
    repository = Path(__file__).resolve().parents[1]
    source = (repository / "tracefold/app/nautilus/oi_runtime.py").read_text()
    tree = ast.parse(source)
    imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module is not None}

    assert "tracefold.app.nautilus.root" not in imports
    assert "tracefold.app.nautilus.database" not in imports
    assert "tracefold.integrations.nautilus.strategy" not in imports
    assert "tracefold.trading.intent" not in imports
    root_source = (repository / "tracefold/app/nautilus/root.py").read_text()
    assert "tracefold.app.nautilus.oi_runtime" in root_source
    assert "activation_not_available_before_433e" in root_source
    assert "tracefold.app.nautilus.database" not in root_source
    assert "tracefold.integrations.nautilus.strategy" not in root_source
