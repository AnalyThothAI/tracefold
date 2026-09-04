"""The database bridge cycle: three steps, one of which cannot silence the other two (#510 PR-1)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import psycopg
from loguru import logger

from tests.nautilus_oi_runtime_fixtures import NOW_NS, oi_profile
from tracefold.app.nautilus.oi_runtime import OiRuntimeDatabaseBridge, RuntimeStateProjector
from tracefold.integrations.nautilus.oi_runtime.audit_sink import AuditSink, ObservationFactory
from tracefold.integrations.nautilus.oi_runtime.signal_client import ExecutionSignalClient
from tracefold.integrations.nautilus.oi_runtime.singleton import AccountSlotSingleton
from tracefold.integrations.nautilus.oi_runtime.state import RuntimeReadiness, RuntimeReadinessSnapshot
from tracefold.trading.storage.execution_stream import (
    ExecutionRuntimeState,
    PreparedExecutionObservationBatch,
)


def _runtime_state() -> ExecutionRuntimeState:
    return ExecutionRuntimeState(
        account_slot="binance_usdm_primary",
        mode="paper",
        runtime_id=UUID("11111111-1111-4111-8111-111111111111"),
        alive=True,
        execution_safe=False,
        entries_armed=False,
        startup_reconciled=False,
        unexpected_exposure=False,
        account_flat=True,
        positions_count=0,
        open_orders_count=0,
        protection_status="not_applicable",
        reconciliation_observed_at_ns=NOW_NS,
        heartbeat_at_ns=NOW_NS,
        entry_block_reason="runtime_starting",
        started_at_ns=NOW_NS,
        updated_at_ns=NOW_NS,
    )


class _FakeTrading:
    """Only the calls `_cycle` makes, each able to fail the way production failed."""

    def __init__(self, *, rejected_event_ids: frozenset[str]) -> None:
        self._rejected_event_ids = rejected_event_ids
        self.signals_broken = True
        self.command_reads = 0
        self.signal_reads = 0
        self.appended: list[str] = []
        self.updates: list[ExecutionRuntimeState] = []
        self.recovery_reads = 0

    def execution_recovery_signals(self, **_kwargs: Any) -> tuple[Any, ...]:
        self.recovery_reads += 1
        return ()

    def execution_recovery_manual_entries(self, **_kwargs: Any) -> tuple[Any, ...]:
        return ()

    def put_execution_runtime_state(self, _state: ExecutionRuntimeState) -> None:
        raise AssertionError("the startup session inserts the row, never the bridge cycle")

    def update_execution_runtime_state(self, state: ExecutionRuntimeState) -> bool:
        self.updates.append(state)
        return True

    def unresolved_operator_intents(self, **_kwargs: Any) -> tuple[Any, ...]:
        self.command_reads += 1
        return ()

    def unresolved_trade_signals(self, **_kwargs: Any) -> tuple[Any, ...]:
        self.signal_reads += 1
        if self.signals_broken:
            raise RuntimeError("signal read exploded")
        return ()

    def recover(self) -> None:
        self.signals_broken = False

    def append_execution_observations(self, prepared: PreparedExecutionObservationBatch) -> tuple[int, ...]:
        payloads = json.loads(prepared.payload_json)
        event_ids = [str(payload["event_id"]) for payload in payloads]
        if any(event_id in self._rejected_event_ids for event_id in event_ids):
            raise psycopg.errors.CheckViolation("trading_execution_observation_native_refs_check")
        self.appended.extend(event_ids)
        return tuple(range(len(event_ids)))


def _singleton(alive: list[bool]) -> AccountSlotSingleton:
    singleton = AccountSlotSingleton(
        account_slot="binance_usdm_primary",
        try_acquire=lambda _slot: True,
        release=lambda _slot: True,
        heartbeat=lambda: alive[0],
    )
    assert singleton.acquire() is True
    return singleton


def _bridge(
    *,
    audit: AuditSink,
    signals: ExecutionSignalClient,
    singleton: AccountSlotSingleton | None = None,
    projector: RuntimeStateProjector | None = None,
) -> OiRuntimeDatabaseBridge:
    return OiRuntimeDatabaseBridge(
        settings=SimpleNamespace(),
        profile=oi_profile(),
        signals=signals,
        audit=audit,
        update_day_start=lambda _baseline: None,
        singleton=singleton or _singleton([True]),
        projector=projector or RuntimeStateProjector(initial=_runtime_state(), recovery_inputs=((), ())),
    )


def test_a_failing_audit_step_never_stops_the_command_read_and_logs_one_cause_once() -> None:
    """The operator must still be able to flatten while the ledger is refusing writes.

    On 2026-09-02 a `CheckViolation` in the audit append aborted the whole cycle, so the Command read
    in the same `try` stopped for six hours while a position was open, and the same traceback was
    logged 742 times (#510 A).
    """

    profile = oi_profile()
    factory = ObservationFactory(
        account_slot=profile.account_slot,
        execution_strategy="oi_nautilus_v1",
    )
    audit = AuditSink(factory=factory, max_count=32, max_bytes=200_000)
    poisoned = factory.create(
        normalized_kind="fill",
        occurred_at_ns=NOW_NS,
        observed_at_ns=NOW_NS,
        summary={"leg": "entry"},
        payload={"leg": "entry"},
    )
    assert audit.offer(poisoned) is True

    trading = _FakeTrading(rejected_event_ids=frozenset({poisoned.event_id}))
    repos: Any = SimpleNamespace(trading=trading, transaction=nullcontext)
    signals = ExecutionSignalClient(
        account_slot=profile.account_slot,
        execution_strategy="oi_nautilus_v1",
    )
    bridge = _bridge(audit=audit, signals=signals)

    records: list[str] = []
    sink_id = logger.add(lambda message: records.append(message.record["message"]), level="INFO")
    try:
        bridge._cycle(repos)
        bridge._cycle(repos)
    finally:
        logger.remove(sink_id)

    assert trading.command_reads == 2
    assert trading.signal_reads == 2
    assert bridge.fatal_error is None
    assert len(trading.appended) == 1
    gap = next(iter(trading.appended))
    assert gap != poisoned.event_id
    assert audit.queued_count == 0
    assert audit.healthy is True
    assert records.count("OI Runtime database bridge step failed (signals)") == 1
    assert records.count("OI Runtime database bridge step failed (audit)") == 0


def test_a_transient_audit_failure_leaves_the_batch_queued_without_killing_the_bridge() -> None:
    profile = oi_profile()
    factory = ObservationFactory(
        account_slot=profile.account_slot,
        execution_strategy="oi_nautilus_v1",
    )
    audit = AuditSink(factory=factory, max_count=32, max_bytes=200_000)
    value = factory.create(
        normalized_kind="readiness",
        occurred_at_ns=NOW_NS,
        observed_at_ns=NOW_NS,
        summary={"lifecycle": "started"},
        payload={"lifecycle": "started"},
    )
    assert audit.offer(value) is True

    class _Unavailable(_FakeTrading):
        def append_execution_observations(self, prepared: PreparedExecutionObservationBatch) -> tuple[int, ...]:
            raise TimeoutError("statement timeout")

    trading = _Unavailable(rejected_event_ids=frozenset())
    repos: Any = SimpleNamespace(trading=trading, transaction=nullcontext)
    signals = ExecutionSignalClient(
        account_slot=profile.account_slot,
        execution_strategy="oi_nautilus_v1",
    )
    bridge = _bridge(audit=audit, signals=signals)

    bridge._cycle(repos)

    assert trading.command_reads == 1
    assert bridge.fatal_error is None
    assert audit.queued_count == 1
    assert audit.failure_reason == "audit_append_failed"
    assert audit.healthy is False


def test_a_lost_connection_still_aborts_the_cycle_so_the_session_is_replaced() -> None:
    profile = oi_profile()
    factory = ObservationFactory(
        account_slot=profile.account_slot,
        execution_strategy="oi_nautilus_v1",
    )
    audit = AuditSink(factory=factory, max_count=32, max_bytes=200_000)

    class _Disconnected(_FakeTrading):
        def unresolved_operator_intents(self, **_kwargs: Any) -> Sequence[Any]:
            raise psycopg.OperationalError("server closed the connection unexpectedly")

    trading = _Disconnected(rejected_event_ids=frozenset())
    repos: Any = SimpleNamespace(trading=trading, transaction=nullcontext)
    signals = ExecutionSignalClient(
        account_slot=profile.account_slot,
        execution_strategy="oi_nautilus_v1",
    )
    bridge = _bridge(audit=audit, signals=signals)

    try:
        bridge._cycle(repos)
    except psycopg.OperationalError:
        return
    raise AssertionError("a lost connection must reach _run, which replaces the session")


def test_a_stuck_input_step_keeps_reading_and_never_disarms_entries() -> None:
    """#520 PR-B: the read that is failing is the only source of entry requests.

    `control_plane_ready` turned a stuck Command or Signal read into `entries_armed=false`, on the
    theory that a Runtime consuming nothing must not stay armed. It could never fire on anything: an
    entry request arrives through exactly that read, so while it fails there is nothing to admit. The
    cycle logs the cause once and keeps re-reading, which is what actually recovers.
    """

    profile = oi_profile()
    factory = ObservationFactory(
        account_slot=profile.account_slot,
        execution_strategy="oi_nautilus_v1",
    )
    audit = AuditSink(factory=factory, max_count=32, max_bytes=200_000)
    trading = _FakeTrading(rejected_event_ids=frozenset())
    repos: Any = SimpleNamespace(trading=trading, transaction=nullcontext)
    signals = ExecutionSignalClient(
        account_slot=profile.account_slot,
        execution_strategy="oi_nautilus_v1",
    )
    bridge = _bridge(audit=audit, signals=signals)

    readiness = RuntimeReadiness(reconciliation_stale_after_ns=profile.risk.reconciliation_stale_after_ns)
    readiness.reconciled(account_observed_at_ns=NOW_NS, reconciliation_observed_at_ns=NOW_NS)

    def snapshot() -> RuntimeReadinessSnapshot:
        return readiness.snapshot(
            now_ns=NOW_NS,
            singleton_ready=True,
            entries_paused=False,
            emergency_halted=False,
        )

    armed = snapshot()
    assert (armed.execution_safe, armed.entries_armed, armed.entry_block_reason) == (True, True, None)

    bridge._cycle(repos)
    bridge._cycle(repos)

    # A failing read is retried on the very next cycle rather than silencing itself or the others.
    assert trading.signal_reads == 2
    still_armed = snapshot()
    assert (still_armed.execution_safe, still_armed.entries_armed, still_armed.entry_block_reason) == (
        True,
        True,
        None,
    )

    trading.recover()
    bridge._cycle(repos)

    assert trading.signal_reads == 3
    assert snapshot().entries_armed is True


def test_the_bridge_thread_owns_the_projection_write_the_recovery_read_and_the_slot_heartbeat() -> None:
    """#510 PR-5b. The event loop offers a row and reads memory; this cycle does every statement.

    Production ran `singleton.check()`, the recovery read and the projection write synchronously on
    the trading event loop every 500 ms, over a third connection with no statement timeout, on the
    same thread as every Nautilus order callback (#510 E).
    """

    profile = oi_profile()
    factory = ObservationFactory(
        account_slot=profile.account_slot,
        execution_strategy="oi_nautilus_v1",
    )
    audit = AuditSink(factory=factory, max_count=32, max_bytes=200_000)
    trading = _FakeTrading(rejected_event_ids=frozenset())
    trading.recover()
    repos: Any = SimpleNamespace(trading=trading, transaction=nullcontext)
    signals = ExecutionSignalClient(
        account_slot=profile.account_slot,
        execution_strategy="oi_nautilus_v1",
    )
    alive = [True]
    singleton = _singleton(alive)
    starting = _runtime_state()
    projector = RuntimeStateProjector(initial=starting, recovery_inputs=((), ()))
    bridge = _bridge(audit=audit, signals=signals, singleton=singleton, projector=projector)

    bridge._cycle(repos)

    assert trading.updates == []
    assert trading.recovery_reads == 1
    assert bridge.recovery_inputs() == ((), ())

    running = replace(
        starting,
        entry_block_reason="reconciliation_stale",
        heartbeat_at_ns=starting.heartbeat_at_ns + 1,
        updated_at_ns=starting.updated_at_ns + 1,
    )
    projector.offer(running)
    bridge._cycle(repos)

    assert trading.updates == [running]
    assert projector.current == running

    # The lock's session dies; the heartbeat that notices runs here, and the loop reads `acquired`.
    alive[0] = False
    bridge._cycle(repos)

    assert singleton.acquired is False


def test_a_failing_projection_write_logs_once_and_leaves_the_inputs_and_the_gates_alone() -> None:
    """A stale `alive` heartbeat is already how every reader decides a Runtime is gone."""

    profile = oi_profile()
    factory = ObservationFactory(
        account_slot=profile.account_slot,
        execution_strategy="oi_nautilus_v1",
    )
    audit = AuditSink(factory=factory, max_count=32, max_bytes=200_000)

    class _LostGeneration(_FakeTrading):
        def update_execution_runtime_state(self, state: ExecutionRuntimeState) -> bool:
            self.updates.append(state)
            return False

    trading = _LostGeneration(rejected_event_ids=frozenset())
    trading.recover()
    repos: Any = SimpleNamespace(trading=trading, transaction=nullcontext)
    signals = ExecutionSignalClient(
        account_slot=profile.account_slot,
        execution_strategy="oi_nautilus_v1",
    )
    starting = _runtime_state()
    projector = RuntimeStateProjector(initial=starting, recovery_inputs=((), ()))
    bridge = _bridge(audit=audit, signals=signals, projector=projector)

    records: list[str] = []
    sink_id = logger.add(lambda message: records.append(message.record["message"]), level="INFO")
    try:
        for offset in range(1, 4):
            projector.offer(
                replace(
                    starting,
                    entry_block_reason="reconciliation_stale",
                    heartbeat_at_ns=starting.heartbeat_at_ns + offset,
                    updated_at_ns=starting.updated_at_ns + offset,
                )
            )
            bridge._cycle(repos)
    finally:
        logger.remove(sink_id)

    assert len(trading.updates) == 3
    assert projector.current == starting
    assert bridge.fatal_error is None
    assert trading.command_reads == 3
    assert records.count("OI Runtime database bridge step failed (projection)") == 1
