"""Closed configuration and disabled app boundary for the OI Runtime."""

from __future__ import annotations

import ast
import asyncio
import inspect
from collections.abc import Iterator
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from nautilus_trader.adapters.binance import BINANCE, BinanceAccountType
from nautilus_trader.adapters.binance.common.enums import BinanceEnvironment
from nautilus_trader.model.identifiers import AccountId

from tests.nautilus_oi_runtime_fixtures import NOW_NS, oi_profile
from tracefold.app.nautilus.root import _build_active_node, _discover_routes
from tracefold.integrations.nautilus.oi_runtime.audit_sink import AuditSink, ObservationFactory
from tracefold.integrations.nautilus.oi_runtime.config import (
    ActiveRuntimeMode,
    BinanceRuntimeCredentials,
    binance_environment,
    build_oi_node_config,
)
from tracefold.integrations.nautilus.oi_runtime.nautilus_1231_binance_compat import (
    single_binance_execution_client,
)
from tracefold.integrations.nautilus.oi_runtime.risk import DayStartBaseline
from tracefold.integrations.nautilus.oi_runtime.signal_client import ExecutionSignalClient
from tracefold.integrations.nautilus.oi_runtime.state import RuntimeReadiness
from tracefold.integrations.nautilus.oi_runtime.strategy import OiNautilusStrategy
from tracefold.integrations.nautilus.oi_runtime.trade_plans import TradePlanChannel


@pytest.mark.parametrize(
    ("mode", "environment"),
    [("paper", BinanceEnvironment.DEMO), ("live", BinanceEnvironment.LIVE)],
)
def test_paper_and_live_change_only_cold_identity_and_binance_environment(
    mode: ActiveRuntimeMode,
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


def test_disabled_is_not_a_runtime_profile_at_all() -> None:
    """#589 PR-2 (T-F14). `disabled` is refused where a profile is built, not re-checked downstream.

    #537 PR-4 made a disabled Runtime one branch in `run_nautilus`, which returns before any profile
    exists. Three more guards then re-proved the same thing on paths a disabled profile could not
    reach - the mode Literal, the node builder and the strategy constructor - so the mode a profile
    may hold is exactly the mode that can trade, and the refusal is here.
    """

    with pytest.raises(ValueError, match="oi_runtime_mode_invalid"):
        oi_profile(cast(ActiveRuntimeMode, "disabled"))


def test_binance_environment_is_the_single_paper_to_demo_decision() -> None:
    # #537 PR-4. The composition root's catalogue discovery carried a second copy of this ternary;
    # both callers now read it here, so no path can disagree about which venue it is trading on.
    assert binance_environment("paper") is BinanceEnvironment.DEMO
    assert binance_environment("live") is BinanceEnvironment.LIVE
    root_source = (Path(_discover_routes.__code__.co_filename)).read_text(encoding="utf-8")
    assert root_source.count("BinanceEnvironment.") == 0
    assert "environment=binance_environment(mode)" in root_source


def test_unknown_mode_fails_closed_instead_of_falling_through_to_live() -> None:
    with pytest.raises(ValueError, match="oi_runtime_mode_invalid"):
        replace(oi_profile("paper"), mode=cast(ActiveRuntimeMode, "staging"))


def test_credentials_never_expose_secrets_in_repr() -> None:
    credentials = BinanceRuntimeCredentials(api_key="visible-key", api_secret="visible-secret")
    node = build_oi_node_config(oi_profile("paper"), credentials)

    assert "visible-key" not in repr(credentials)
    assert "visible-secret" not in repr(credentials)
    assert "visible-key" not in repr(node)
    assert "visible-secret" not in repr(node)


def test_paper_and_live_have_disjoint_profile_namespaces() -> None:
    paper = oi_profile("paper")
    live = oi_profile("live")
    paper_node = build_oi_node_config(paper, BinanceRuntimeCredentials("paper-key", "paper-secret"))
    live_node = build_oi_node_config(live, BinanceRuntimeCredentials("live-key", "live-secret"))

    # One account slot, two modes: the mode is what keeps paper and live from claiming each other's
    # orders, because it is the second half of the one namespace the Cache identity and every client
    # order id are both derived from (#589 PR-2).
    assert paper.account_slot == live.account_slot
    assert paper.namespace != live.namespace
    assert paper_node.trader_id != live_node.trader_id
    assert paper_node.instance_id != live_node.instance_id


_MASTER_ACCOUNT_ID = AccountId("BINANCE-USDT_FUTURES-master")


@pytest.fixture(name="real_execution_engine", scope="module")
def _real_execution_engine() -> Iterator[Any]:
    """The composition root's own live execution engine, built offline with throwaway credentials.

    Nothing here talks to Binance: `_build_active_node` only constructs the node graph, so the real
    `BinanceFuturesExecutionClient` this yields is the same object the Runtime process reaches into,
    and the module builds it once because that construction is the expensive part.
    """

    profile = replace(oi_profile("paper"), account_id=_MASTER_ACCOUNT_ID)
    signals = ExecutionSignalClient(account_slot=profile.account_slot, execution_strategy="oi_nautilus_v1")
    strategy = OiNautilusStrategy(
        profile=profile,
        signals=signals,
        audit=AuditSink(
            factory=ObservationFactory(
                account_slot=profile.account_slot,
                execution_strategy="oi_nautilus_v1",
            )
        ),
        readiness=RuntimeReadiness(reconciliation_stale_after_ns=profile.risk.reconciliation_stale_after_ns),
        dispatch_pump=lambda pump: pump(),
        singleton_ready=lambda: True,
        day_start=DayStartBaseline(
            utc_day="2030-03-17",
            equity_usd=Decimal("1000"),
            recorded_at_ns=NOW_NS,
            event_id="4" * 64,
        ),
        request_reconciliation=lambda _reason: None,
        plans=TradePlanChannel(),
    )
    loop = asyncio.new_event_loop()
    node = _build_active_node(
        profile=profile,
        credentials=BinanceRuntimeCredentials("paper-key", "paper-secret"),
        strategy=strategy,
        loop=loop,
    )
    try:
        yield node.kernel.exec_engine
    finally:
        node.dispose()
        loop.close()


def test_canonical_root_builds_one_real_binance_execution_client(real_execution_engine: Any) -> None:
    client = single_binance_execution_client(real_execution_engine)

    assert client.account_id == _MASTER_ACCOUNT_ID


# Preserved from the deleted `tests/architecture/test_nautilus_runtime_owner_matrix.py`: everything
# else in that module restated the current wiring line by line, but this one boundary has no other
# owner. Nautilus 1.231 exposes no public route to a complete private-account proof, so exactly one
# module reaches into the adapter's privates and an upgrade has exactly one place to break. The list
# now lives beside the real client this module already builds, because the rule that keeps every
# other module off these names and the rule that proves the names still exist are the same list read
# two ways: a member renamed upstream used to leave every test green and raise `RuntimeError` on the
# Runtime's start-up path, possibly while the account held a position (#604 T1).
_PRIVATE_NAUTILUS_ATTRIBUTES = (
    "_clients",
    "_active_symbols_cache",
    "_get_binance_position_status_reports",
    "_build_active_symbols",
    "_parse_order_status_reports",
    "_fetch_algo_orders",
    "_parse_algo_order_report",
)

# Exactly the calls `load_complete_binance_account_reports` makes, in the argument shapes it makes
# them, and whether it awaits the result.
_PRIVATE_BINANCE_CALLS: tuple[tuple[str, tuple[object, ...], bool], ...] = (
    ("_get_binance_position_status_reports", (), True),
    ("_build_active_symbols", (None,), True),
    ("_parse_order_status_reports", ([], None, None), False),
    ("_fetch_algo_orders", (None,), True),
    ("_parse_algo_order_report", (None, None, None), False),
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SOURCE_ROOT = _REPO_ROOT / "tracefold"
_COMPAT_MODULE = _SOURCE_ROOT / "integrations" / "nautilus" / "oi_runtime" / "nautilus_1231_binance_compat.py"


def test_private_nautilus_adapter_access_has_one_compatibility_seam() -> None:
    violations = [
        f"{path.relative_to(_SOURCE_ROOT).as_posix()}:{node.lineno}:{node.attr}"
        for path in sorted(_SOURCE_ROOT.rglob("*.py"))
        if path != _COMPAT_MODULE and "__pycache__" not in path.parts
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        if isinstance(node, ast.Attribute) and node.attr in _PRIVATE_NAUTILUS_ATTRIBUTES
    ]

    assert violations == []


def test_every_private_member_the_account_proof_needs_exists_on_the_real_1231_client(
    real_execution_engine: Any,
) -> None:
    """The seam's seven names, asserted against the objects the Runtime actually reaches into.

    `tests/deploy/test_nautilus_dependency.py` pins the version and the wheel hash, which cannot
    notice a rename inside it, and `tests/test_nautilus_reconciliation.py` proves the seam's
    behaviour against a stand-in whose shape the test itself chose.
    """

    engine_attribute, *client_attributes = _PRIVATE_NAUTILUS_ATTRIBUTES
    client = single_binance_execution_client(real_execution_engine)

    assert hasattr(real_execution_engine, engine_attribute)
    assert [name for name in client_attributes if not hasattr(client, name)] == []


def test_every_private_call_the_account_proof_makes_binds_on_the_real_1231_client(
    real_execution_engine: Any,
) -> None:
    """A member that survives a Nautilus upgrade under a changed signature is the same outage."""

    client = single_binance_execution_client(real_execution_engine)

    for name, arguments, awaited in _PRIVATE_BINANCE_CALLS:
        member = getattr(client, name)
        inspect.signature(member).bind(*arguments)
        assert inspect.iscoroutinefunction(member) is awaited, name
