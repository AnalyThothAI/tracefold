"""Real PostgreSQL under the OI Runtime: the bridge, the plan store, and the Strategy on real Nautilus.

The seam tests run the production Strategy on a real `BacktestEngine` with the production bridge cycle
writing to this test's database (`tests/helpers/nautilus_oi_runtime_process.py`), so every durable
fact asserted here is one the Runtime itself wrote through the path production uses.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal
from functools import partial
from uuid import uuid4

import psycopg
import pytest

from tests.helpers.nautilus_oi_runtime_process import run_runtime_on_postgres
from tests.helpers.published_signal_v3 import append_published_v3_signal
from tests.nautilus_oi_runtime_fixtures import (
    MARKET,
    NOW_NS,
    SECOND_NS,
    oi_profile,
    quotes,
    seed_reconciled_position,
)
from tests.postgres_test_utils import connect_postgres_test, postgres_settings_storage
from tracefold.app.nautilus.oi_runtime import (
    OiRuntimeDatabaseBridge,
    RuntimeStateProjector,
    load_or_record_day_start,
    load_runtime_inputs,
    load_unresolved_operator_intents,
    load_unresolved_trade_signals,
)
from tracefold.app.repository_session import repositories_for_connection
from tracefold.integrations.nautilus.oi_runtime.entry import deterministic_client_order_id
from tracefold.integrations.nautilus.oi_runtime.journal import ExecutionJournal, ObservationFactory
from tracefold.integrations.nautilus.oi_runtime.risk import DayStartBaseline
from tracefold.integrations.nautilus.oi_runtime.signal_client import ExecutionSignalClient
from tracefold.integrations.nautilus.oi_runtime.singleton import AccountSlotSingleton
from tracefold.integrations.nautilus.oi_runtime.strategy import RuntimeControlSnapshot
from tracefold.platform.config.models import Settings
from tracefold.trading.storage.execution_stream import (
    ExecutionRuntimeState,
    PreparedOperatorIntent,
    prepare_execution_observations,
    prepare_operator_intent,
)
from tracefold.trading.storage.root import TradingRepository

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_clone_dsn")]

_ACCOUNT_SLOT = "binance_usdm_primary"
_SIGNAL_ID = "1" * 64


def _control_row(repo: TradingRepository) -> None:
    with repo.conn.transaction():
        repo.ensure_execution_runtime_control_state(_ACCOUNT_SLOT, now_ns=NOW_NS)


def _append_signal(repo: TradingRepository, *, suffix: str = "1") -> str:
    case_id = f"case-{suffix}"
    signal_id = suffix * 64
    return append_published_v3_signal(
        repo,
        signal_id=signal_id,
        case_id=case_id,
        observed_at_ns=NOW_NS - 1_000_000,
        expires_at_ns=NOW_NS + 60 * SECOND_NS,
    )


def _append_command(repo: TradingRepository, *, suffix: str, action: str) -> PreparedOperatorIntent:
    prepared = prepare_operator_intent(
        command_id=suffix * 64,
        account_slot=_ACCOUNT_SLOT,
        action=action,
        scope="account" if action in {"emergency_halt", "flatten"} else "entries",
        reason="operator test",
        operator_identity="operator:test",
        authentication_identity="test:authenticated",
        requested_at_ns=NOW_NS,
        expires_at_ns=NOW_NS + 60 * SECOND_NS,
        market_key=None,
        direction=None,
    )
    with repo.conn.transaction():
        repo.append_operator_intent(prepared)
    return prepared


def _rows(conn: psycopg.Connection, sql: str, *params: object) -> list[dict[str, object]]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _plan(conn: psycopg.Connection, entry_id: str = _SIGNAL_ID) -> dict[str, object]:
    [row] = _rows(conn, "SELECT * FROM trading_trade_plans WHERE entry_id = %s", entry_id)
    return row


def _kinds(conn: psycopg.Connection, entry_id: str = _SIGNAL_ID) -> list[tuple[str, dict[str, object]]]:
    return [
        (str(row["normalized_kind"]), dict(row["summary"]))
        for row in _rows(
            conn,
            """
            SELECT normalized_kind, summary FROM trading_execution_observations
             WHERE signal_id = %s OR command_id = %s ORDER BY seq
            """,
            entry_id,
            entry_id,
        )
    ]


# -- the Strategy on real Nautilus, journaled into PostgreSQL ---------------------------------------


def test_a_database_signal_becomes_one_committed_plan_one_order_and_its_protection() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        repos = repositories_for_connection(conn)
        _control_row(repos.trading)
        _append_signal(repos.trading)

        runtime = run_runtime_on_postgres(repos, tape=quotes(9_999, 10_000, start_ns=NOW_NS, count=20))

        plan = _plan(conn)
        assert (plan["status"], plan["exit_reason"], plan["terminal_at_ns"]) == ("open", None, None)
        assert plan["opened_at_ns"] is not None and "history_gap_reason" not in plan
        orders = {order.client_order_id.value: order for order in runtime.engine.cache.orders()}
        namespace = runtime.profile.namespace
        for leg in ("entry", "stop", "take_profit"):
            assert deterministic_client_order_id(namespace=namespace, entry_id=_SIGNAL_ID, leg=leg).value in orders
        kinds = _kinds(conn)
        assert ("signal_disposition", {"disposition": "accepted"}) in kinds
        [fill] = [summary for kind, summary in kinds if kind == "fill"]
        assert fill["leg"] == "entry" and fill["commission_currency"] == "USDT"
        assert Decimal(str(fill["commission"])) > 0
        submitted = [summary for kind, summary in kinds if kind == "protection" and summary["status"] == "submitted"]
        assert [summary["leg"] for summary in submitted] == ["stop", "take_profit"]
        # The Signal is resolved for good: a plan exists and its verdict is durable.
        assert load_unresolved_trade_signals(repos, _ACCOUNT_SLOT, "oi_nautilus_v1", 10, "paper") == ()
        inputs = load_runtime_inputs(repos, oi_profile(), now_ns=NOW_NS)
        assert [(value.plan.entry_id, value.disposition_pending) for value in inputs.open_plans] == [
            (_SIGNAL_ID, False)
        ]
    finally:
        conn.close()


def test_a_stop_out_ends_the_plan_durably_and_its_fill_folded_pnl_is_nautilus_own() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        repos = repositories_for_connection(conn)
        _control_row(repos.trading)
        _append_signal(repos.trading)

        runtime = run_runtime_on_postgres(
            repos,
            tape=[
                *quotes(9_999, 10_000, start_ns=NOW_NS, count=10),
                *quotes(9_700, 9_701, start_ns=NOW_NS + 2 * SECOND_NS, count=10),
            ],
        )
        closed_pnl = [position.realized_pnl.as_decimal() for position in runtime.engine.cache.positions_closed()]

        plan = _plan(conn)
        assert (plan["status"], plan["exit_reason"]) == ("closed", "stop_filled")
        [row] = [
            item for item in repos.trading.console_executions(since_ns=0, limit=10) if item["entry_id"] == _SIGNAL_ID
        ]
        assert row["plan_status"] == "closed"
        assert Decimal(str(row["realized_pnl_usd"])) == closed_pnl[0]
        assert Decimal(str(row["fees_usd"])) > 0
        assert Decimal(str(row["exit_price"])) == Decimal(9_700)
        totals = repos.trading.console_realized_totals(account_slot=_ACCOUNT_SLOT, day_start_ns=0, day_end_ns=2**62)
        assert Decimal(str(totals["realized_known_total_usd"])) == closed_pnl[0]
        assert (totals["closed_total"], totals["pnl_known_total"], totals["pnl_missing_total"]) == (1, 1, 0)
        # The stop-out is what the next generation's cooldown is keyed on.
        inputs = load_runtime_inputs(repos, oi_profile(), now_ns=NOW_NS + 10 * SECOND_NS)
        assert inputs.stop_exits == {MARKET: plan["terminal_at_ns"]}
    finally:
        conn.close()


def test_a_restart_adopts_the_reconciled_position_and_its_orders_and_writes_nothing_new() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        repos = repositories_for_connection(conn)
        _control_row(repos.trading)
        _append_signal(repos.trading)
        run_runtime_on_postgres(repos, tape=quotes(9_999, 10_000, start_ns=NOW_NS, count=20))
        plan_before = _plan(conn)
        observations_before = _rows(conn, "SELECT event_id FROM trading_execution_observations")

        # The next generation: Nautilus' reconciliation left the position and both orders in the Cache.
        restarted = run_runtime_on_postgres(
            repos,
            tape=quotes(9_999, 10_000, start_ns=NOW_NS + 60 * SECOND_NS, count=150),
            seed=seed_reconciled_position,
        )

        assert _plan(conn) == plan_before
        assert _rows(conn, "SELECT event_id FROM trading_execution_observations") == observations_before
        assert sorted(order.client_order_id.value for order in restarted.engine.cache.orders()) == [
            "RECONCILED-ENTRY",
            "RECONCILED-STOP",
            "RECONCILED-TP",
        ]
    finally:
        conn.close()


def test_a_crash_before_final_check_resumes_the_original_plan_once() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        repos = repositories_for_connection(conn)
        _control_row(repos.trading)
        _append_signal(repos.trading)
        crashed = run_runtime_on_postgres(
            repos, tape=quotes(9_999, 10_000, start_ns=NOW_NS, count=10), stop_after_commit=True
        )
        assert crashed.engine.cache.orders() == []
        assert _plan(conn)["status"] == "prepared"
        assert load_runtime_inputs(repos, oi_profile(), now_ns=NOW_NS).open_plans[0].disposition_pending

        restarted = run_runtime_on_postgres(repos, tape=quotes(9_999, 10_000, start_ns=NOW_NS + SECOND_NS, count=10))

        assert len(restarted.engine.cache.orders()) == 3
        plan = _plan(conn)
        assert (plan["status"], plan["exit_reason"]) == ("open", None)
        assert _rows(conn, "SELECT count(*) AS n FROM trading_trade_plans") == [{"n": 1}]
        assert _rows(conn, "SELECT count(*) AS n FROM trading_entry_validity_checks") == [{"n": 1}]
        assert ("signal_disposition", {"disposition": "accepted"}) in _kinds(conn)
    finally:
        conn.close()


def test_a_crash_after_final_check_keeps_an_unknown_send_from_retrying() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        repos = repositories_for_connection(conn)
        _control_row(repos.trading)
        _append_signal(repos.trading)
        crashed = run_runtime_on_postgres(
            repos, tape=quotes(9_999, 10_000, start_ns=NOW_NS, count=10), stop_after_commit=True
        )
        assert crashed.engine.cache.orders() == []
        with conn.transaction():
            assert repos.trading.validate_signal_entry(entry_id=_SIGNAL_ID, now_ns=NOW_NS) == (True, "valid")

        restarted = run_runtime_on_postgres(repos, tape=quotes(9_999, 10_000, start_ns=NOW_NS + SECOND_NS, count=10))

        assert restarted.engine.cache.orders() == []
        plan = _plan(conn)
        assert (plan["status"], plan["exit_reason"], plan["opened_at_ns"]) == ("closed", "venue_unknown", None)
        assert ("signal_disposition", {"disposition": "entry_outcome_unknown"}) in _kinds(conn)
    finally:
        conn.close()


def test_a_refused_entry_is_not_submitted_and_its_signal_never_reads_accepted() -> None:
    from nautilus_trader.model.enums import TradingState

    conn = connect_postgres_test(read_only=False)
    try:
        repos = repositories_for_connection(conn)
        _control_row(repos.trading)
        _append_signal(repos.trading)

        def halt(engine: object, _strategy: object) -> None:
            engine.kernel.risk_engine.set_trading_state(TradingState.HALTED)  # type: ignore[attr-defined]

        run_runtime_on_postgres(repos, tape=quotes(9_999, 10_000, start_ns=NOW_NS, count=10), seed=halt)

        plan = _plan(conn)
        assert (plan["status"], plan["exit_reason"]) == ("closed", "not_submitted")
        dispositions = [summary for kind, summary in _kinds(conn) if kind == "signal_disposition"]
        assert [value["disposition"] for value in dispositions] == ["venue_rejected"]
        [row] = [
            item for item in repos.trading.console_executions(since_ns=0, limit=10) if item["entry_id"] == _SIGNAL_ID
        ]
        assert row["disposition_reason"] == "venue_rejected"
        assert row["order_reject_reason"] is not None and "HALTED" in str(row["order_reject_reason"])
    finally:
        conn.close()


def test_a_stop_out_in_the_database_cools_the_market_down_for_the_next_signal() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        repos = repositories_for_connection(conn)
        _control_row(repos.trading)
        _append_signal(repos.trading)
        run_runtime_on_postgres(
            repos,
            tape=[
                *quotes(9_999, 10_000, start_ns=NOW_NS, count=10),
                *quotes(9_700, 9_701, start_ns=NOW_NS + 2 * SECOND_NS, count=10),
            ],
        )
        second = _append_signal(repos.trading, suffix="2")

        run_runtime_on_postgres(repos, tape=quotes(9_999, 10_000, start_ns=NOW_NS + 10 * SECOND_NS, count=10))

        assert ("signal_disposition", {"disposition": "post_stop_cooldown"}) in _kinds(conn, second)
        assert _rows(conn, "SELECT 1 FROM trading_trade_plans WHERE entry_id = %s", second) == []
    finally:
        conn.close()


# -- the bridge on its own session -----------------------------------------------------------------


def _bridge_singleton() -> AccountSlotSingleton:
    singleton = AccountSlotSingleton(
        account_slot=_ACCOUNT_SLOT,
        try_acquire=lambda _slot: True,
        release=lambda _slot: True,
        heartbeat=lambda: True,
    )
    assert singleton.acquire() is True
    return singleton


def _runtime_state() -> ExecutionRuntimeState:
    return ExecutionRuntimeState(
        account_slot=_ACCOUNT_SLOT,
        mode="paper",
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


def _runtime_bridge(
    signals: ExecutionSignalClient,
    *,
    journal: ExecutionJournal | None = None,
    projector: RuntimeStateProjector | None = None,
    update_day_start: Callable[[DayStartBaseline], None] = lambda _baseline: None,
) -> OiRuntimeDatabaseBridge:
    profile = oi_profile()
    return OiRuntimeDatabaseBridge(
        settings=Settings(ws_token="680-bridge", storage=postgres_settings_storage()),
        profile=profile,
        signals=signals,
        journal=journal or ExecutionJournal(factory=ObservationFactory(profile.account_slot, "oi_nautilus_v1")),
        update_day_start=update_day_start,
        singleton=_bridge_singleton(),
        projector=projector or RuntimeStateProjector(initial=_runtime_state()),
    )


def _queued(signals: ExecutionSignalClient) -> int:
    with signals._lock:
        return len(signals._values) + len(signals._commands)


def _connected(bridge: OiRuntimeDatabaseBridge) -> bool:
    with bridge._lock:
        return bridge._connected


def _wait(predicate: Callable[[], bool], *, timeout_seconds: float = 3.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert predicate()


def test_the_bridge_delivers_within_one_poll_interval_on_one_session_and_survives_its_termination() -> None:
    writer = connect_postgres_test(read_only=False)
    signals = ExecutionSignalClient(account_slot=_ACCOUNT_SLOT, execution_strategy="oi_nautilus_v1")
    bridge = _runtime_bridge(signals)
    try:
        repo = TradingRepository(writer)
        _control_row(repo)
        bridge.start()
        _wait(lambda: _connected(bridge))
        latencies: list[float] = []
        for suffix in "123456":
            started = time.perf_counter()
            _append_signal(repo, suffix=suffix)
            _wait(lambda: _queued(signals) == 1)
            assert signals.next_nowait() is not None
            latencies.append(time.perf_counter() - started)
        assert sorted(latencies)[math.ceil(0.95 * len(latencies)) - 1] <= 0.6
        assert writer.execute(
            """
            SELECT count(*) AS n FROM pg_stat_activity
             WHERE datname = current_database() AND application_name = 'tracefold_nautilus_stream'
            """
        ).fetchone() == {"n": 1}

        writer.execute(
            """
            SELECT pg_terminate_backend(pid) FROM pg_stat_activity
             WHERE datname = current_database() AND application_name = 'tracefold_nautilus_stream'
            """
        )
        _wait(lambda: not _connected(bridge))
        _wait(lambda: _connected(bridge))
        _append_signal(repo, suffix="8")
        _wait(lambda: _queued(signals) == 1)
    finally:
        bridge.stop()
        bridge.join(3.0)
        writer.close()


def test_a_refused_journal_row_is_dropped_and_the_row_behind_it_is_still_written() -> None:
    """#680 RC6. One row per transaction: a refusal is that row's answer and nobody else's."""

    conn = connect_postgres_test(read_only=False)
    try:
        repos = repositories_for_connection(conn)
        _control_row(repos.trading)
        signals = ExecutionSignalClient(account_slot=_ACCOUNT_SLOT, execution_strategy="oi_nautilus_v1")
        journal = ExecutionJournal(factory=ObservationFactory(_ACCOUNT_SLOT, "oi_nautilus_v1"))
        bridge = _runtime_bridge(signals, journal=journal)
        # A fill correlated to a Command nobody issued: the foreign key refuses it.
        refused = journal.factory.create(
            normalized_kind="fill",
            command_id="e" * 64,
            occurred_at_ns=NOW_NS,
            observed_at_ns=NOW_NS,
            summary={"leg": "entry", "last_quantity": "1", "last_price": "1"},
            event_identity="orphan",
        )
        written = journal.factory.create(
            normalized_kind="risk",
            occurred_at_ns=NOW_NS,
            observed_at_ns=NOW_NS,
            summary={"risk_fact": "unexpected_exposure", "count": 0, "exposure": ""},
            event_identity="after",
        )
        journal.offer(refused)
        journal.offer(written)

        bridge._cycle(repos)

        assert _rows(conn, "SELECT event_id FROM trading_execution_observations") == [{"event_id": written.event_id}]
        assert journal.backlog() == 0
    finally:
        conn.close()


def test_the_control_projection_is_the_restart_input_and_never_regresses() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        repos = repositories_for_connection(conn)
        repo = repos.trading
        assert load_runtime_inputs(repos, oi_profile(), now_ns=NOW_NS).control == RuntimeControlSnapshot(
            entries_paused=False, emergency_halted=False
        )
        factory = ObservationFactory(_ACCOUNT_SLOT, "oi_nautilus_v1")

        def accept(prepared: PreparedOperatorIntent, action: str, at_ns: int) -> None:
            observation = factory.create(
                normalized_kind="control_disposition",
                command_id=prepared.value.command_id,
                occurred_at_ns=at_ns,
                observed_at_ns=at_ns,
                summary={"action": action, "disposition": "accepted", "reason": "test"},
                event_identity="final",
            )
            with conn.transaction():
                repo.append_execution_observations(prepare_execution_observations((observation,)))

        pause = _append_command(repo, suffix="4", action="pause_entries")
        resume = _append_command(repo, suffix="5", action="resume_entries")
        accept(resume, "resume_entries", NOW_NS + 2)
        accept(pause, "pause_entries", NOW_NS + 1)
        assert load_runtime_inputs(repos, oi_profile(), now_ns=NOW_NS).control.entries_paused is False
        halt = _append_command(repo, suffix="6", action="emergency_halt")
        accept(halt, "emergency_halt", NOW_NS + 3)
        flatten = _append_command(repo, suffix="7", action="flatten")
        accept(flatten, "flatten", NOW_NS + 4)
        assert load_runtime_inputs(repos, oi_profile(), now_ns=NOW_NS).control == RuntimeControlSnapshot(
            entries_paused=True, emergency_halted=True
        )
        commands = load_unresolved_operator_intents(repos, _ACCOUNT_SLOT, "oi_nautilus_v1", 10)
        assert commands == ()
    finally:
        conn.close()


def test_account_slot_lock_is_single_session_and_loss_fails_closed() -> None:
    first_conn = connect_postgres_test(read_only=False)
    second_conn = connect_postgres_test(read_only=False)
    first_repo = TradingRepository(first_conn)
    second_repo = TradingRepository(second_conn)
    first = AccountSlotSingleton(
        account_slot=_ACCOUNT_SLOT,
        try_acquire=first_repo.try_acquire_execution_account_slot,
        release=first_repo.release_execution_account_slot,
        heartbeat=lambda: bool(first_conn.execute("SELECT 1 AS alive").fetchone()["alive"]),
    )
    second = AccountSlotSingleton(
        account_slot=_ACCOUNT_SLOT,
        try_acquire=second_repo.try_acquire_execution_account_slot,
        release=second_repo.release_execution_account_slot,
        heartbeat=lambda: bool(second_conn.execute("SELECT 1 AS alive").fetchone()["alive"]),
    )
    try:
        assert first.acquire() is True
        assert second.acquire() is False
        first_conn.close()
        assert first.check() is False
        assert first.acquired is False
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not second.acquire():
            time.sleep(0.01)
        assert second.acquired is True
    finally:
        if not first_conn.closed:
            first_conn.close()
        second.release()
        second_conn.close()


def test_a_failing_day_start_write_never_stops_commands_or_the_projection() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        repos = repositories_for_connection(conn)
        _control_row(repos.trading)
        signals = ExecutionSignalClient(account_slot=_ACCOUNT_SLOT, execution_strategy="oi_nautilus_v1")
        projector = RuntimeStateProjector(initial=_runtime_state())
        projector.start(repos)
        baselines: list[DayStartBaseline] = []
        bridge = _runtime_bridge(signals, projector=projector, update_day_start=baselines.append)
        bridge.set_equity(Decimal("1000"), NOW_NS)
        conn.execute(
            """
            ALTER TABLE trading_execution_observations ADD CONSTRAINT test_day_start_rejected
            CHECK (summary->>'risk_fact' IS DISTINCT FROM 'day_start_equity')
            """
        )
        command = _append_command(repos.trading, suffix="a", action="pause_entries")
        heartbeat = projector.current.heartbeat_at_ns + SECOND_NS
        projected = replace(projector.current, heartbeat_at_ns=heartbeat, updated_at_ns=heartbeat)
        projector.offer(projected)

        bridge._cycle(repos)

        received = signals.next_command_nowait()
        assert received is not None and received.command_id == command.value.command_id
        assert repos.trading.execution_runtime_state(_ACCOUNT_SLOT) == projected
        assert baselines == []

        conn.execute("ALTER TABLE trading_execution_observations DROP CONSTRAINT test_day_start_rejected")
        bridge.set_equity(Decimal("900"), NOW_NS + 1)
        bridge._cycle(repos)
        assert [baseline.equity_usd for baseline in baselines] == [Decimal("900")]
        restarted = load_or_record_day_start(
            repos=repos,
            factory=ObservationFactory(_ACCOUNT_SLOT, "oi_nautilus_v1"),
            utc_day=baselines[0].utc_day,
            equity_usd=Decimal("800"),
            recorded_at_ns=NOW_NS + 2,
        )
        assert restarted == baselines[0]
    finally:
        conn.close()


def test_the_unresolved_reads_are_indexed_anti_joins_the_next_poll_answers() -> None:
    reader_conn = connect_postgres_test(read_only=False)
    writer = connect_postgres_test(read_only=False)
    try:
        reader_repos = repositories_for_connection(reader_conn)
        writer_repo = TradingRepository(writer)
        _control_row(writer_repo)
        client = ExecutionSignalClient(account_slot=_ACCOUNT_SLOT, execution_strategy="oi_nautilus_v1")
        reader = partial(load_unresolved_trade_signals, reader_repos, runtime_mode="paper")
        assert client.poll_once(reader) == 0
        _append_signal(writer_repo)
        assert client.poll_once(reader) == 1
        assert client.next_nowait() is not None
        assert client.poll_once(reader) == 0
        command = _append_command(writer_repo, suffix="7", action="pause_entries")
        assert client.poll_commands_once(partial(load_unresolved_operator_intents, reader_repos)) == 1
        assert client.next_command_nowait() == command.value
    finally:
        reader_conn.close()
        writer.close()
