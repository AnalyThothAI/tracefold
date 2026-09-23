"""The production Strategy on a real `BacktestEngine`, fed and journaled by the real bridge on PostgreSQL.

Nothing between the two is a test double: Signals and Commands come from the database through the
bridge's own indexed reads, every plan is committed by `commit_entry_plan` before its entry order
exists, and every observation and plan transition reaches PostgreSQL through the bridge's own
one-row-per-transaction journal flush. The backtest engine does not wait for wall time, so the bridge
cycle runs after every pump instead of on its own thread.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import uuid4

from nautilus_trader.backtest.config import BacktestEngineConfig
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.model.objects import Money

from tests.helpers.published_signal_v2 import execution_fixture_profile
from tests.nautilus_oi_runtime_fixtures import INSTRUMENT, NOW_NS
from tracefold.app.nautilus.oi_runtime import OiRuntimeDatabaseBridge, RuntimeStateProjector, load_runtime_inputs
from tracefold.app.repository_session import RepositorySession
from tracefold.integrations.nautilus.oi_runtime.config import OiRuntimeProfile
from tracefold.integrations.nautilus.oi_runtime.journal import ExecutionJournal, ObservationFactory
from tracefold.integrations.nautilus.oi_runtime.risk import DayStartBaseline
from tracefold.integrations.nautilus.oi_runtime.signal_client import ExecutionSignalClient
from tracefold.integrations.nautilus.oi_runtime.singleton import AccountSlotSingleton
from tracefold.integrations.nautilus.oi_runtime.strategy import OiNautilusStrategy
from tracefold.trading.storage.execution_stream import ExecutionRuntimeState


@dataclass
class PostgresRuntime:
    engine: BacktestEngine
    strategy: OiNautilusStrategy
    journal: ExecutionJournal
    profile: OiRuntimeProfile


def _singleton(account_slot: str) -> AccountSlotSingleton:
    singleton = AccountSlotSingleton(
        account_slot=account_slot,
        try_acquire=lambda _slot: True,
        release=lambda _slot: True,
        heartbeat=lambda: True,
    )
    singleton.acquire()
    return singleton


def _idle_projector(profile: OiRuntimeProfile) -> RuntimeStateProjector:
    """A projector nothing offers to: the projection row is not what these runs are about."""

    return RuntimeStateProjector(
        initial=ExecutionRuntimeState(
            account_slot=profile.account_slot,
            mode=profile.mode,
            runtime_id=uuid4(),
            alive=True,
            entries_armed=False,
            unexpected_exposure=False,
            positions_count=0,
            open_orders_count=0,
            protection_status="not_applicable",
            heartbeat_at_ns=NOW_NS,
            entry_block_reason="runtime_starting",
            started_at_ns=NOW_NS,
            updated_at_ns=NOW_NS,
        )
    )


def run_runtime_on_postgres(
    repos: RepositorySession,
    *,
    tape: Iterable[Any],
    profile: OiRuntimeProfile | None = None,
    seed: Callable[[BacktestEngine, OiNautilusStrategy], None] | None = None,
    stop_after_commit: bool = False,
    starting_balance: int = 1_000,
) -> PostgresRuntime:
    """One Runtime generation over `tape`. `stop_after_commit` is a crash between plan and order."""

    profile = profile or execution_fixture_profile()
    inputs = load_runtime_inputs(repos, profile, now_ns=NOW_NS)
    signals = ExecutionSignalClient(account_slot=profile.account_slot, execution_strategy="oi_nautilus_v1")
    journal = ExecutionJournal(factory=ObservationFactory(profile.account_slot, "oi_nautilus_v1"))
    crashed: list[bool] = []

    def settings_free_cycle() -> None:
        bridge._cycle(repos)  # the production cycle, on this test's connection

    def dispatch(pump: Callable[[], None]) -> None:
        if crashed:
            return
        pump()
        pending = journal.pending_prepare() is not None
        settings_free_cycle()
        if pending:
            if stop_after_commit:
                crashed.append(True)
                return
            pump()
            settings_free_cycle()

    strategy = OiNautilusStrategy(
        profile=profile,
        signals=signals,
        journal=journal,
        inputs=inputs,
        dispatch_pump=dispatch,
        singleton_ready=lambda: True,
        venue_reads=False,
        day_start=DayStartBaseline("2030-03-17", Decimal(starting_balance), NOW_NS - 1, "4" * 64),
    )
    bridge = OiRuntimeDatabaseBridge(
        settings=None,
        profile=profile,
        signals=signals,
        journal=journal,
        update_day_start=strategy.update_day_start,
        singleton=_singleton(profile.account_slot),
        projector=_idle_projector(profile),
    )
    settings_free_cycle()
    engine = BacktestEngine(
        BacktestEngineConfig(trader_id=TraderId("OI-PROCESS"), logging=LoggingConfig(bypass_logging=True))
    )
    engine.add_venue(
        venue=INSTRUMENT.id.venue,
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        starting_balances=[Money(starting_balance, INSTRUMENT.quote_currency)],
        base_currency=None,
        default_leverage=Decimal(2),
    )
    engine.add_instrument(INSTRUMENT)
    engine.add_data(list(tape))
    engine.add_strategy(strategy)
    if seed is not None:
        seed(engine, strategy)
    engine.run()
    if not crashed:
        settings_free_cycle()
    # The bridge writes on its own clock; give it the rows whose backoff a real cycle would outwait.
    deadline = time.monotonic() + 2.0
    while journal.backlog() and time.monotonic() < deadline and not crashed:
        settings_free_cycle()
    return PostgresRuntime(engine=engine, strategy=strategy, journal=journal, profile=profile)


__all__ = ["PostgresRuntime", "run_runtime_on_postgres"]
