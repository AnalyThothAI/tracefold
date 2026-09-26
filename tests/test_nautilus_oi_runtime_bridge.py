"""The database bridge: one row per transaction, nothing blocks the next, nothing is fatal (#680 RC1, RC6)."""

from __future__ import annotations

import json
from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import psycopg
import pytest

from tests.nautilus_oi_runtime_fixtures import NOW_NS, oi_profile, open_plan, operator_intent, trade_signal
from tracefold.app.nautilus import oi_runtime
from tracefold.app.nautilus.oi_runtime import OiRuntimeDatabaseBridge
from tracefold.integrations.nautilus.oi_runtime.journal import ExecutionJournal, ObservationFactory
from tracefold.integrations.nautilus.oi_runtime.signal_client import ExecutionSignalClient
from tracefold.integrations.nautilus.oi_runtime.singleton import AccountSlotSingleton
from tracefold.trading.storage.execution_stream import ExecutionRuntimeState, PreparedExecutionObservationBatch


def _runtime_state() -> ExecutionRuntimeState:
    return ExecutionRuntimeState(
        account_slot="binance_usdm_primary",
        connection="DEMO",
        runtime_id=UUID("11111111-1111-4111-8111-111111111111"),
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


class _FakeTrading:
    """Only the calls `_cycle` makes, each able to fail the way production failed."""

    def __init__(self) -> None:
        self.refused: set[str] = set()
        self.transient: set[str] = set()
        self.appended: list[str] = []
        self.plan_updates: list[tuple[Any, ...]] = []
        self.inserted: list[tuple[Any, ...]] = []
        self.stored_plan: dict[str, Any] | None = None
        self.command_reads = 0
        self.signal_reads = 0
        self.signals: tuple[Any, ...] = ()

    def update_execution_runtime_state(self, _state: ExecutionRuntimeState) -> bool:
        return True

    def unresolved_operator_intents(self, **_kwargs: Any) -> tuple[Any, ...]:
        self.command_reads += 1
        return ()

    def unresolved_trade_signals(self, **_kwargs: Any) -> tuple[Any, ...]:
        self.signal_reads += 1
        return self.signals

    def append_execution_observations(self, prepared: PreparedExecutionObservationBatch) -> tuple[int, ...]:
        [payload] = json.loads(prepared.payload_json)
        event_id = str(payload["event_id"])
        if event_id in self.refused:
            raise psycopg.errors.UniqueViolation("ux_trading_execution_signal_disposition")
        if event_id in self.transient:
            raise psycopg.errors.QueryCanceled("canceling statement due to statement timeout")
        self.appended.append(event_id)
        return (len(self.appended),)

    def update_trade_plan(self, values: tuple[Any, ...]) -> bool:
        self.plan_updates.append(values)
        return True

    def insert_trade_plan(self, values: tuple[Any, ...]) -> bool:
        self.inserted.append(values)
        return True

    def trade_plan(self, _entry_id: str) -> dict[str, Any] | None:
        return self.stored_plan

    def trade_plan_for_scope(self, **_kwargs: Any) -> dict[str, Any] | None:
        return self.stored_plan


class _FakeRepos:
    def __init__(self, trading: _FakeTrading) -> None:
        self.trading = trading
        self.conn = SimpleNamespace(execute=lambda *_args, **_kwargs: None)

    def transaction(self) -> Any:
        return nullcontext()


def _singleton() -> AccountSlotSingleton:
    singleton = AccountSlotSingleton(
        account_slot="binance_usdm_primary",
        try_acquire=lambda _slot: True,
        release=lambda _slot: True,
        heartbeat=lambda: True,
    )
    assert singleton.acquire() is True
    return singleton


def _bridge() -> tuple[OiRuntimeDatabaseBridge, ExecutionJournal, ExecutionSignalClient]:
    profile = oi_profile()
    signals = ExecutionSignalClient(account_slot=profile.account_slot, execution_strategy="oi_nautilus_v1")
    journal = ExecutionJournal(factory=ObservationFactory(profile.account_slot, "oi_nautilus_v1"))
    bridge = OiRuntimeDatabaseBridge(
        settings=SimpleNamespace(),
        profile=profile,
        signals=signals,
        journal=journal,
        update_day_start=lambda _baseline: None,
        singleton=_singleton(),
    )
    return bridge, journal, signals


def _order(journal: ExecutionJournal, index: int) -> Any:
    value = journal.factory.create(
        normalized_kind="order",
        occurred_at_ns=NOW_NS + index,
        observed_at_ns=NOW_NS + index,
        summary={"leg": "entry", "status": "submitted"},
        event_identity=f"order-{index}",
    )
    journal.offer(value)
    return value


def test_every_journal_row_is_its_own_transaction_and_a_refused_row_never_holds_up_the_next() -> None:
    bridge, journal, signals = _bridge()
    trading = _FakeTrading()
    signal = trade_signal()
    signals.poll_once(lambda *_args: (signal,))
    assert signals.next_nowait() == signal
    refused = journal.factory.create(
        normalized_kind="signal_disposition",
        signal_id=signal.signal_id,
        occurred_at_ns=NOW_NS,
        observed_at_ns=NOW_NS,
        summary={"disposition": "accepted"},
        event_identity="final",
    )
    journal.offer(refused)
    after = _order(journal, 1)
    trading.refused.add(refused.event_id)

    bridge._cycle(_FakeRepos(trading))  # type: ignore[arg-type]

    assert trading.appended == [after.event_id]
    assert journal.backlog() == 0
    # A refused verdict still settles its input: the database's refusal is the answer it will get.
    assert signals.poll_once(lambda *_args: (signal,)) == 1


def test_a_lost_statement_ends_the_cycle_and_its_row_waits_out_a_backoff_instead_of_being_dropped() -> None:
    bridge, journal, _signals = _bridge()
    trading = _FakeTrading()
    stuck = _order(journal, 1)
    behind = _order(journal, 2)
    trading.transient.add(stuck.event_id)

    with pytest.raises(psycopg.OperationalError):
        bridge._cycle(_FakeRepos(trading))  # type: ignore[arg-type]
    # The statement timeout is a lost-session class: the cycle ends so `_run` replaces the session,
    # and the row it was writing waits out its backoff rather than being dropped.
    assert journal.backlog() == 2
    trading.transient.clear()
    [row_behind] = [row for row in journal.due(float("inf")) if row.value is behind]
    assert row_behind.not_before == 0.0


def test_a_row_the_storage_layer_raises_on_is_deferred_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    bridge, journal, _signals = _bridge()
    trading = _FakeTrading()
    first = _order(journal, 1)
    second = _order(journal, 2)
    calls: list[str] = []

    def flaky(_repos: Any, value: Any) -> None:
        calls.append(value.event_id)
        if value.event_id == first.event_id:
            raise OSError("socket reset")
        trading.appended.append(value.event_id)

    monkeypatch.setattr(oi_runtime, "write_journal_row", flaky)
    bridge._cycle(_FakeRepos(trading))  # type: ignore[arg-type]

    assert trading.appended == [second.event_id]
    [waiting] = journal.due(float("inf"))
    assert waiting.value is first and waiting.attempts == 1


def test_plan_transitions_are_written_as_updates_one_per_transaction() -> None:
    bridge, journal, _signals = _bridge()
    trading = _FakeTrading()
    plan = open_plan(opened_at_ns=None)
    opened = plan.opened(opened_at_ns=NOW_NS, now_ns=NOW_NS)
    journal.offer_plan(opened)

    bridge._cycle(_FakeRepos(trading))  # type: ignore[arg-type]

    [values] = trading.plan_updates
    assert values[0] == "open" and values[1] == NOW_NS and values[5] == plan.entry_id


def test_plan_false_update_needs_a_storage_verdict_and_is_never_dropped() -> None:
    bridge, journal, _signals = _bridge()
    trading = _FakeTrading()
    prepared = open_plan(opened_at_ns=None)
    opened = prepared.opened(opened_at_ns=NOW_NS, now_ns=NOW_NS)
    trading.update_trade_plan = lambda _values: False  # type: ignore[method-assign]
    trading.stored_plan = prepared.model_dump()
    journal.offer_plan(opened)

    bridge._cycle(_FakeRepos(trading))  # type: ignore[arg-type]
    assert journal.backlog() == 1
    [waiting] = journal.due(float("inf"))
    assert waiting.attempts == 1

    trading.stored_plan = opened.model_dump()
    waiting.not_before = 0.0
    bridge._cycle(_FakeRepos(trading))  # type: ignore[arg-type]
    assert journal.backlog() == 0


def test_only_the_exact_prepared_plan_authorizes_its_entry_order() -> None:
    bridge, journal, _signals = _bridge()
    trading = _FakeTrading()
    plan = open_plan(opened_at_ns=None)
    journal.prepare(plan)
    trading.stored_plan = plan.model_dump()
    bridge._cycle(_FakeRepos(trading))  # type: ignore[arg-type]
    receipt = journal.take_receipt()
    assert receipt is not None and receipt.committed

    other = open_plan(entry_id="2" * 64, opened_at_ns=None)
    journal.prepare(other)
    trading.stored_plan = {**other.model_dump(), "entry_quantity": other.entry_quantity * 2}
    bridge._cycle(_FakeRepos(trading))  # type: ignore[arg-type]
    receipt = journal.take_receipt()
    assert receipt is not None and not receipt.committed and receipt.reason == "trade_plan_conflict"


def test_an_insert_the_database_refuses_answers_the_strategy_instead_of_stalling_it() -> None:
    bridge, journal, _signals = _bridge()
    trading = _FakeTrading()

    def refuse(_values: tuple[Any, ...]) -> bool:
        raise psycopg.errors.UniqueViolation("ux_trading_trade_plans_active_instrument")

    trading.insert_trade_plan = refuse  # type: ignore[method-assign]
    plan = open_plan(opened_at_ns=None)
    journal.prepare(plan)
    bridge._cycle(_FakeRepos(trading))  # type: ignore[arg-type]
    receipt = journal.take_receipt()
    assert receipt is not None and (receipt.committed, receipt.reason) == (False, "trade_plan_rejected")


def test_commands_are_read_first_and_a_broken_signal_read_is_contained() -> None:
    bridge, _journal, signals = _bridge()
    trading = _FakeTrading()

    def explode(**_kwargs: Any) -> tuple[Any, ...]:
        trading.signal_reads += 1
        raise RuntimeError("signal read exploded")

    trading.unresolved_trade_signals = explode  # type: ignore[method-assign]
    bridge._cycle(_FakeRepos(trading))  # type: ignore[arg-type]
    bridge._cycle(_FakeRepos(trading))  # type: ignore[arg-type]
    assert (trading.command_reads, trading.signal_reads) == (2, 2)
    signals.poll_commands_once(lambda *_args: (operator_intent(),))
    assert signals.next_command_nowait() is not None


def test_a_lost_session_is_replaced_and_the_bridge_thread_never_dies(monkeypatch: pytest.MonkeyPatch) -> None:
    bridge, _journal, _signals = _bridge()
    trading = _FakeTrading()
    sessions: list[int] = []

    class _Session:
        def __enter__(self) -> _FakeRepos:
            sessions.append(1)
            if len(sessions) == 1:
                raise psycopg.OperationalError("server closed the connection unexpectedly")
            if len(sessions) == 2:
                raise RuntimeError("anything else a connect can raise")
            bridge.stop()
            return _FakeRepos(trading)

        def __exit__(self, *_exc: object) -> None:
            return None

    monkeypatch.setattr(oi_runtime, "open_repositories", lambda *_args, **_kwargs: _Session())
    monkeypatch.setattr(oi_runtime, "_RECONNECT_BACKOFF_SECONDS", (0.0,))
    bridge.start()
    bridge.join(5.0)

    assert len(sessions) == 3


@pytest.mark.parametrize("receipt", [False, (), None])
def test_a_missing_observation_write_receipt_retains_the_critical_fill(
    monkeypatch: pytest.MonkeyPatch, receipt: Any
) -> None:
    bridge, journal, _signals = _bridge()
    trading = _FakeTrading()
    value = journal.factory.create(
        normalized_kind="fill",
        occurred_at_ns=NOW_NS,
        observed_at_ns=NOW_NS,
        summary={"leg": "entry", "last_quantity": "1", "last_price": "2"},
        event_identity="native-trade:1",
    )
    assert journal.offer(value)
    monkeypatch.setattr(trading, "append_execution_observations", lambda _prepared: receipt)
    bridge._flush_journal(_FakeRepos(trading))
    [pending] = journal.due(float("inf"))
    assert pending.value == value and pending.attempts == 1

    monkeypatch.setattr(trading, "append_execution_observations", lambda _prepared: (42,))
    pending.not_before = 0
    bridge._flush_journal(_FakeRepos(trading))
    assert journal.due(float("inf")) == ()


@pytest.mark.parametrize("kind", ["fill", "protection"])
def test_pending_critical_replay_cannot_hide_a_conflicting_fact(kind: str) -> None:
    _bridge_value, journal, _signals = _bridge()
    summary: dict[str, str] = {"leg": "stop"}
    if kind == "protection":
        summary["binding_version"] = "plan_order_v1"
    value = journal.factory.create(
        normalized_kind=kind,
        occurred_at_ns=NOW_NS,
        observed_at_ns=NOW_NS,
        summary=summary,
        event_identity="native:1",
    )
    assert journal.offer(value)
    assert journal.offer(value.model_copy(update={"observed_at_ns": NOW_NS + 1}))
    with pytest.raises(ValueError, match="pending_identity_conflict"):
        journal.offer(value.model_copy(update={"summary": {**summary, "leg": "take_profit"}}))
    [pending] = journal.due(float("inf"))
    assert pending.value == value
