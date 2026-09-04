from __future__ import annotations

import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from uuid import UUID

import psycopg
import pytest
from pydantic import ValidationError

from tests.postgres_test_utils import connect_postgres_test
from tracefold.app.operator_control import persist_operator_intent
from tracefold.platform.postgres.audit import PostgresQueryAudit, QueryAuditCatalog
from tracefold.trading.execution_contracts import ExecutionObservationV1
from tracefold.trading.storage.execution_stream import (
    ExecutionAccountOrder,
    ExecutionAccountPosition,
    ExecutionAccountSnapshot,
    ExecutionRuntimeState,
    PreparedExecutionObservationBatch,
    PreparedOperatorIntent,
    PreparedTradeSignal,
    execution_stream_query_specs,
    materialize_operator_intents,
    materialize_trade_signals,
    prepare_execution_observations,
    prepare_operator_intent,
    prepare_trade_signal,
)
from tracefold.trading.storage.root import TradingRepository

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_clone_dsn")]


def _prepare_signal(*, suffix: str, case_id: str | None = None, **updates: object) -> PreparedTradeSignal:
    values: dict[str, object] = {
        "signal_id": suffix * 64,
        "case_id": case_id or f"case-{suffix}",
        "market_key": "crypto:perp:BTC:USDT",
        "direction": "long",
        "observed_at_ns": 1_000,
        "expires_at_ns": 10_000,
    }
    values.update(updates)
    return prepare_trade_signal(**values)


def _append_signal(repo: TradingRepository, prepared: PreparedTradeSignal) -> dict[str, object]:
    """Give the dormant execution stream a Signal produced by the current Case owner."""

    case_id = prepared.value.case_id
    repo.conn.execute(
        """
        INSERT INTO trading_cases (
          case_id, underlying_key, trigger_kind, primary_source_key,
          manifest, manifest_sha256, state,
          policy_decision, policy_reason, observed_at_ms, created_at_ms, decided_at_ms,
          updated_at_ms
        ) VALUES (
          %s, %s, 'oi', %s, '{"test":"execution-stream"}'::jsonb,
          %s, 'SIGNAL_EMITTED', 'long', 'execution_stream_fixture', 1, 1, 1, 1
        )
        ON CONFLICT DO NOTHING
        """,
        (case_id, f"stream:{case_id}", f"stream-source:{case_id}", "e" * 64),
    )
    return repo.append_trade_signal(prepared)


def _prepare_command(*, suffix: str, **updates: object) -> PreparedOperatorIntent:
    values: dict[str, object] = {
        "command_id": suffix * 64,
        "account_slot": "demo-v1",
        "action": "pause_entries",
        "scope": "account",
        "reason": "test",
        "operator_identity": "operator:1",
        "authentication_identity": "cli:local",
        "requested_at_ns": 1_000,
        "expires_at_ns": 10_000,
        "market_key": None,
        "direction": None,
    }
    values.update(updates)
    return prepare_operator_intent(**values)


def _observation(
    *, event: str, signal_id: str | None = None, command_id: str | None = None, kind: str, **updates: object
) -> ExecutionObservationV1:
    values: dict[str, object] = {
        "event_id": event * 64,
        "account_slot": "demo-v1",
        "execution_strategy": "oi_nautilus_v1",
        "signal_id": signal_id,
        "command_id": command_id,
        "normalized_kind": kind,
        "occurred_at_ns": 2_000,
        "observed_at_ns": 2_100,
        "native_identity_references": (),
        "summary": {"disposition": "accepted"},
    }
    values.update(updates)
    return ExecutionObservationV1.model_validate(values)


def _plan_index_names(value: object) -> set[str]:
    if isinstance(value, dict):
        names = {str(value["Index Name"])} if "Index Name" in value else set()
        return names | set().union(*(_plan_index_names(item) for item in value.values()), set())
    if isinstance(value, list):
        return set().union(*(_plan_index_names(item) for item in value), set())
    return set()


def _wait_for_database_lock(conn: object, *, application_name: str) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        row = conn.execute(
            """
            SELECT wait_event_type
              FROM pg_stat_activity
             WHERE application_name = %s AND state = 'active'
            """,
            (application_name,),
        ).fetchone()
        if row is not None and row["wait_event_type"] == "Lock":
            return
        time.sleep(0.01)
    raise AssertionError(f"concurrent append did not reach a database lock: {application_name}")


def test_exact_append_is_idempotent_and_identity_conflicts_fail_closed() -> None:
    signal = _prepare_signal(suffix="a")
    conflicting_signal = _prepare_signal(suffix="a", direction="short")
    command = _prepare_command(suffix="d")
    conflicting_command = _prepare_command(suffix="d", reason="different")
    observation = _observation(
        event="f",
        signal_id=signal.value.signal_id,
        kind="signal_disposition",
    )
    observation_batch = prepare_execution_observations((observation,))
    with pytest.raises(ValueError, match="execution_observation_batch_count_exceeded"):
        prepare_execution_observations((observation,) * 129)

    conn = connect_postgres_test(read_only=False)
    try:
        with conn.transaction():
            repo = TradingRepository(conn)
            first_row = _append_signal(repo, signal)
            identical_row = _append_signal(repo, signal)
            assert identical_row == first_row
            assert conn.execute("SELECT count(*) AS n FROM trading_trade_signals").fetchone()["n"] == 1

            with pytest.raises(RuntimeError, match="execution_stream_identity_conflict"):
                _append_signal(repo, conflicting_signal)

            command_row = repo.append_operator_intent(command)
            assert repo.append_operator_intent(command) == command_row
            with pytest.raises(RuntimeError, match="execution_stream_identity_conflict"):
                repo.append_operator_intent(conflicting_command)
            observed_seq = repo.append_execution_observations(observation_batch)
            assert repo.append_execution_observations(observation_batch) == observed_seq
    finally:
        conn.close()

    assert materialize_trade_signals((first_row,)) == materialize_trade_signals((identical_row,))
    assert materialize_operator_intents((command_row,))[0].command_id == command.value.command_id


def test_operator_ingress_records_only_the_idempotent_intent_without_interpreting_it() -> None:
    command = _prepare_command(suffix="9", requested_at_ns=2_000, expires_at_ns=10_000)
    conn = connect_postgres_test(read_only=False)
    try:
        repo = TradingRepository(conn)
        with conn.transaction():
            first = persist_operator_intent(repo, command)
        with conn.transaction():
            retried = persist_operator_intent(repo, command)

        assert first == retried
        assert first.disposition == "awaiting_runtime"
        assert first.reason is None
        assert conn.execute("SELECT count(*) AS n FROM trading_operator_intents").fetchone()["n"] == 1
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM trading_execution_observations WHERE command_id = %s",
                (command.value.command_id,),
            ).fetchone()["n"]
            == 0
        )
    finally:
        conn.close()


def test_operator_ingress_leaves_the_slots_command_for_the_runtime() -> None:
    now_ns = time.time_ns()
    command = _prepare_command(suffix="8", requested_at_ns=now_ns, expires_at_ns=now_ns + 3_600_000_000_000)
    conn = connect_postgres_test(read_only=False)
    try:
        repo = TradingRepository(conn)
        with conn.transaction():
            receipt = persist_operator_intent(repo, command)

        assert receipt.disposition == "awaiting_runtime"
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM trading_execution_observations WHERE command_id = %s",
                (command.value.command_id,),
            ).fetchone()["n"]
            == 0
        )
        with conn.transaction():
            unresolved = repo.unresolved_operator_intents(
                account_slot="demo-v1",
                execution_strategy="oi_nautilus_v1",
                now_ns=now_ns,
                limit=10,
            )
        assert materialize_operator_intents(unresolved) == (command.value.model_copy(update={"seq": 1}),)
    finally:
        conn.close()


def test_execution_stream_append_requires_caller_owned_transaction() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        repo = TradingRepository(conn)
        prepared = prepare_trade_signal(
            signal_id="a" * 64,
            case_id="case-a",
            market_key="crypto:perp:BTC:USDT",
            direction="long",
            observed_at_ns=1_000,
            expires_at_ns=10_000,
        )
        with pytest.raises(RuntimeError, match="append_trade_signal_requires_explicit_transaction"):
            repo.append_trade_signal(prepared)
        assert conn.execute("SELECT count(*) AS n FROM trading_trade_signals").fetchone()["n"] == 0
    finally:
        conn.close()


def test_concurrent_identical_appends_are_idempotent() -> None:
    signal = _prepare_signal(suffix="a")
    command = _prepare_command(suffix="b")
    observation = prepare_execution_observations((_observation(event="c", kind="risk"),))
    first = connect_postgres_test(read_only=False)
    second = connect_postgres_test(read_only=False)
    observer = connect_postgres_test(read_only=False)
    started = threading.Event()
    try:
        first_repo = TradingRepository(first)
        second_repo = TradingRepository(second)
        with ThreadPoolExecutor(max_workers=1) as executor:

            def second_signal_append():
                second.execute("SET application_name = 'tracefold-433-signal-retry'")
                with second.transaction():
                    started.set()
                    return _append_signal(second_repo, signal)

            with first.transaction():
                first_signal = _append_signal(first_repo, signal)
                signal_future = executor.submit(second_signal_append)
                assert started.wait(1)
                _wait_for_database_lock(observer, application_name="tracefold-433-signal-retry")
            assert signal_future.result(timeout=5) == first_signal

            started.clear()

            def second_command_append():
                second.execute("SET application_name = 'tracefold-433-command-retry'")
                with second.transaction():
                    started.set()
                    return second_repo.append_operator_intent(command)

            with first.transaction():
                first_command = first_repo.append_operator_intent(command)
                command_future = executor.submit(second_command_append)
                assert started.wait(1)
                _wait_for_database_lock(observer, application_name="tracefold-433-command-retry")
            assert command_future.result(timeout=5) == first_command

            started.clear()

            def second_observation_append():
                second.execute("SET application_name = 'tracefold-433-observation-retry'")
                with second.transaction():
                    started.set()
                    return second_repo.append_execution_observations(observation)

            with first.transaction():
                first_observation = first_repo.append_execution_observations(observation)
                observation_future = executor.submit(second_observation_append)
                assert started.wait(1)
                _wait_for_database_lock(observer, application_name="tracefold-433-observation-retry")
            assert observation_future.result(timeout=5) == first_observation
    finally:
        first.close()
        second.close()
        observer.close()


def test_a_final_disposition_drives_the_bounded_anti_join_reads() -> None:
    """A Signal or Command leaves the pending set exactly when its disposition is durable."""

    now_ns = time.time_ns()
    hour_ns = 3_600_000_000_000
    signal_prepared = _prepare_signal(suffix="c", observed_at_ns=now_ns, expires_at_ns=now_ns + hour_ns)
    command_prepared = _prepare_command(suffix="e", requested_at_ns=now_ns, expires_at_ns=now_ns + hour_ns)

    def read(repo: TradingRepository) -> tuple[tuple[object, ...], tuple[object, ...]]:
        return (
            materialize_trade_signals(
                repo.unresolved_trade_signals(
                    account_slot="demo-v1", execution_strategy="oi_nautilus_v1", now_ns=now_ns, limit=10
                )
            ),
            materialize_operator_intents(
                repo.unresolved_operator_intents(
                    account_slot="demo-v1", execution_strategy="oi_nautilus_v1", now_ns=now_ns, limit=10
                )
            ),
        )

    conn = connect_postgres_test(read_only=False)
    try:
        repo = TradingRepository(conn)
        with conn.transaction():
            signal_row = _append_signal(repo, signal_prepared)
            command_row = repo.append_operator_intent(command_prepared)

        signal = materialize_trade_signals((signal_row,))[0]
        command = materialize_operator_intents((command_row,))[0]
        with conn.transaction():
            first = read(repo)
            second = read(repo)
        # The read is idempotent: nothing about it consumes what it returns.
        assert first == second == ((signal,), (command,))

        dispositions = prepare_execution_observations(
            (
                _observation(event="f", signal_id=signal.signal_id, kind="signal_disposition"),
                _observation(event="9", command_id=command.command_id, kind="control_disposition"),
            )
        )
        duplicate_disposition = prepare_execution_observations(
            (_observation(event="f", signal_id=signal.signal_id, kind="signal_disposition"),)
        )
        distinct_final_disposition = prepare_execution_observations(
            (_observation(event="8", signal_id=signal.signal_id, kind="signal_disposition"),)
        )
        with conn.transaction():
            rows = repo.append_execution_observations(dispositions)
            assert len(rows) == 2
            assert repo.append_execution_observations(duplicate_disposition) == (rows[0],)
            with pytest.raises(psycopg.errors.UniqueViolation):
                repo.append_execution_observations(distinct_final_disposition)
            assert conn.execute("SELECT count(*) AS n FROM trading_execution_observations").fetchone()["n"] == 2

        with conn.transaction():
            assert read(repo) == ((), ())
    finally:
        conn.close()


def test_unresolved_reads_return_only_unexpired_intents() -> None:
    """#520 PR-A. A pending Signal or Command is one whose own TTL has not run out.

    The activation waterline used to answer this question by sequence number, which meant a Runtime
    could only ever be told about facts newer than the row that named it. Expiry is the fact the
    contract already carries, so the read states it directly and needs no activation row at all.
    """

    now_ns = time.time_ns()
    hour_ns = 3_600_000_000_000
    expired_signal = _prepare_signal(
        suffix="a",
        observed_at_ns=now_ns - 2 * hour_ns,
        expires_at_ns=now_ns - hour_ns,
    )
    live_signal = _prepare_signal(suffix="c", observed_at_ns=now_ns, expires_at_ns=now_ns + hour_ns)
    expired_command = _prepare_command(
        suffix="b",
        requested_at_ns=now_ns - 2 * hour_ns,
        expires_at_ns=now_ns - hour_ns,
    )
    live_command = _prepare_command(suffix="e", requested_at_ns=now_ns, expires_at_ns=now_ns + hour_ns)

    conn = connect_postgres_test(read_only=False)
    try:
        repo = TradingRepository(conn)
        with conn.transaction():
            _append_signal(repo, expired_signal)
            live_signal_row = _append_signal(repo, live_signal)
            repo.append_operator_intent(expired_command)
            live_command_row = repo.append_operator_intent(live_command)

        with conn.transaction():
            signals = repo.unresolved_trade_signals(
                account_slot="demo-v1", execution_strategy="oi_nautilus_v1", now_ns=now_ns, limit=10
            )
            commands = repo.unresolved_operator_intents(
                account_slot="demo-v1", execution_strategy="oi_nautilus_v1", now_ns=now_ns, limit=10
            )
        assert materialize_trade_signals(signals) == materialize_trade_signals((live_signal_row,))
        assert materialize_operator_intents(commands) == materialize_operator_intents((live_command_row,))
    finally:
        conn.close()


def test_rejected_observation_batch_rolls_back_its_new_prefix() -> None:
    """One append is one batch: a row the database refuses takes the whole batch with it.

    The refusal used here is the Command foreign key, which is what remains after #520 PR-C took the
    per-key `payload` CHECK away: an observation disposing of a Command nobody issued. Without the
    savepoint the first element of the batch would already be durable when the second raised.
    """

    existing = _observation(event="a", kind="risk")
    existing_batch = prepare_execution_observations((existing,))
    new_value = _observation(event="b", kind="risk")
    unknown_command = _observation(event="c", command_id="9" * 64, kind="control_disposition")
    rejected_batch = prepare_execution_observations((new_value, unknown_command))

    conn = connect_postgres_test(read_only=False)
    try:
        repo = TradingRepository(conn)
        with conn.transaction():
            repo.append_execution_observations(existing_batch)

        with conn.transaction():
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                repo.append_execution_observations(rejected_batch)

            assert conn.execute("SELECT event_id FROM trading_execution_observations ORDER BY event_id").fetchall() == [
                {"event_id": existing.event_id}
            ]
    finally:
        conn.close()


def test_a_hand_built_observation_batch_writes_nothing() -> None:
    """`prepare_execution_observations` is the only thing that may produce an append.

    It states the batch bounds once and validates every row; #520 PR-C removed the SQL that
    re-derived those bounds so the per-key `payload` CHECK could compare against them. A batch built
    by hand therefore reaches the INSERT, and what stops it is what the contract would have supplied:
    the NOT NULL identity columns. Nothing durable is left behind either way.
    """

    forged_count = PreparedExecutionObservationBatch(payload_json=json.dumps([{}] * 129), count=129)
    forged_bytes = PreparedExecutionObservationBatch(
        payload_json=json.dumps([{"blob": "x" * 1_048_576}]),
        count=1,
    )
    conn = connect_postgres_test(read_only=False)
    try:
        repo = TradingRepository(conn)
        for prepared in (forged_count, forged_bytes):
            with conn.transaction(), pytest.raises(psycopg.errors.NotNullViolation):
                repo.append_execution_observations(prepared)
        assert conn.execute("SELECT count(*) AS n FROM trading_execution_observations").fetchone()["n"] == 0
    finally:
        conn.close()


def test_account_slot_advisory_lock_has_one_session_owner() -> None:
    first = connect_postgres_test(read_only=False)
    second = connect_postgres_test(read_only=False)
    try:
        assert TradingRepository(first).try_acquire_execution_account_slot("binance_usdm_primary") is True
        assert TradingRepository(second).try_acquire_execution_account_slot("binance_usdm_primary") is False
        first.close()
        first = None
        assert TradingRepository(second).try_acquire_execution_account_slot("binance_usdm_primary") is True
    finally:
        if first is not None:
            first.close()
        second.close()


def test_runtime_state_is_single_generation_per_account_slot() -> None:
    running = ExecutionRuntimeState(
        account_slot="binance_usdm_primary",
        mode="paper",
        runtime_id=UUID("11111111-1111-4111-8111-111111111111"),
        alive=True,
        execution_safe=True,
        entries_armed=True,
        startup_reconciled=True,
        unexpected_exposure=False,
        account_flat=True,
        positions_count=0,
        open_orders_count=0,
        protection_status="not_applicable",
        reconciliation_observed_at_ns=2_000,
        heartbeat_at_ns=2_100,
        entry_block_reason=None,
        started_at_ns=1_900,
        updated_at_ns=2_100,
        account_snapshot=ExecutionAccountSnapshot(
            observed_at_ns=2_000,
            market_observed_at_ns=1_990,
            equity_usd="995",
            day_start_equity_usd="1000",
            daily_drawdown_usd="5",
            daily_drawdown_bps=50,
            aggregate_risk_usd="2",
            positions=(
                ExecutionAccountPosition(
                    position_id="position-1",
                    instrument_id="BTCUSDT-PERP.BINANCE",
                    side="long",
                    quantity="0.01",
                    entry_price="100000",
                    mark_price="100500",
                    unrealized_pnl_usd="5",
                    owned=True,
                    protection_status="protected",
                    protection_quantity="0.01",
                    protection_trigger_price="99000",
                    protection_full_coverage=True,
                ),
            ),
            orders=(
                ExecutionAccountOrder(
                    client_order_id="stop-1",
                    instrument_id="BTCUSDT-PERP.BINANCE",
                    state="open",
                    leg="protection",
                    quantity="0.01",
                    reduce_only=True,
                    trigger_price="99000",
                    owned=True,
                ),
            ),
            open_orders_count=1,
            inflight_orders_count=0,
            unknown_orders_count=0,
            complete=True,
        ),
        routes_count=2,
    )

    conn = connect_postgres_test(read_only=False)
    try:
        repo = TradingRepository(conn)
        with conn.transaction():
            assert repo.put_execution_runtime_state(running) == running

        assert repo.execution_runtime_state("binance_usdm_primary") == running
        stale_generation = replace(
            running,
            runtime_id=UUID("22222222-2222-4222-8222-222222222222"),
            heartbeat_at_ns=2_200,
            updated_at_ns=2_200,
        )
        with conn.transaction():
            assert repo.update_execution_runtime_state(stale_generation) is False
        stopped = replace(
            running,
            alive=False,
            execution_safe=False,
            entries_armed=False,
            heartbeat_at_ns=2_300,
            entry_block_reason="runtime_stopped",
            updated_at_ns=2_300,
        )
        with conn.transaction():
            assert repo.update_execution_runtime_state(stopped) is True
        # The catalogue's size is generation identity, not heartbeat state: the update statement
        # never names it and the row keeps what the insert published (#510 PR-2). It is a count
        # rather than the keys themselves because the count is all any reader rendered, and the
        # catalogue's one rule belongs to the Runtime that can act on it (#537 PR-3).
        assert repo.execution_runtime_state("binance_usdm_primary") == stopped
        assert repo.execution_runtime_state("binance_usdm_primary").routes_count == 2

        with pytest.raises(ValueError, match="execution_runtime_routes_invalid"):
            replace(running, routes_count=-1)
    finally:
        conn.close()


def test_manual_entry_recovery_read_is_bounded_by_durable_facts_and_the_window() -> None:
    """#520 PR-A. Recovery is bounded by what a manual entry actually did, not by a waterline.

    A Command with no durable entry-order fact never reached the venue and can hold nothing; one whose
    latest position fact is `closed` is finished. Both used to sit behind the activation fence as well,
    which also hid every intent older than the current profile.
    """

    unsent = _prepare_command(
        suffix="7",
        action="manual_entry",
        scope="market",
        market_key="crypto:perp:BTC:USDT",
        direction="long",
    )
    submitted = _prepare_command(
        suffix="8",
        action="manual_entry",
        scope="market",
        market_key="crypto:perp:ETH:USDT",
        direction="short",
    )
    conn = connect_postgres_test(read_only=False)
    try:
        repo = TradingRepository(conn)
        with conn.transaction():
            repo.append_operator_intent(unsent)
            repo.append_operator_intent(submitted)

        assert repo.execution_recovery_manual_entries(account_slot="demo-v1", since_ns=0, limit=10) == ()
        with conn.transaction():
            repo.append_execution_observations(
                prepare_execution_observations(
                    (
                        _observation(
                            event="9",
                            command_id=submitted.value.command_id,
                            kind="order",
                            summary={"leg": "entry", "status": "submitted"},
                        ),
                    )
                )
            )

        rows = repo.execution_recovery_manual_entries(account_slot="demo-v1", since_ns=0, limit=10)

        assert materialize_operator_intents(rows) == (submitted.value.model_copy(update={"seq": rows[0][0]}),)
        assert repo.execution_recovery_manual_entries(account_slot="demo-v1", since_ns=2_101, limit=10) == ()
        # A different account slot never claims this one's exposure.
        assert repo.execution_recovery_manual_entries(account_slot="other-slot", since_ns=0, limit=10) == ()
        with conn.transaction():
            repo.append_execution_observations(
                prepare_execution_observations(
                    (
                        _observation(
                            event="a",
                            command_id=submitted.value.command_id,
                            kind="position",
                            summary={"status": "closed", "quantity": "0"},
                        ),
                    )
                )
            )
        assert repo.execution_recovery_manual_entries(account_slot="demo-v1", since_ns=0, limit=10) == ()
    finally:
        conn.close()


def test_signal_recovery_keeps_only_windowed_durable_entry_order_facts() -> None:
    active = _prepare_signal(suffix="6", case_id="case-active")
    stopped = _prepare_signal(suffix="7", case_id="case-stopped")
    retired = _prepare_signal(suffix="8", case_id="case-retired")
    conn = connect_postgres_test(read_only=False)
    try:
        repo = TradingRepository(conn)
        with conn.transaction():
            _append_signal(repo, active)
            _append_signal(repo, stopped)
            _append_signal(repo, retired)
            repo.append_execution_observations(
                prepare_execution_observations(
                    (
                        _observation(
                            event="1",
                            signal_id=active.value.signal_id,
                            kind="order",
                            summary={"leg": "entry", "status": "submitted"},
                            observed_at_ns=5_000,
                        ),
                        _observation(
                            event="2",
                            signal_id=active.value.signal_id,
                            kind="position",
                            summary={"status": "opened", "quantity": "0.01"},
                            observed_at_ns=5_000,
                        ),
                        _observation(
                            event="3",
                            signal_id=stopped.value.signal_id,
                            kind="order",
                            summary={"leg": "entry", "status": "submitted"},
                            observed_at_ns=5_000,
                        ),
                        _observation(
                            event="4",
                            signal_id=stopped.value.signal_id,
                            kind="position",
                            summary={"status": "opened", "quantity": "0.01"},
                            observed_at_ns=5_000,
                        ),
                        _observation(
                            event="5",
                            signal_id=stopped.value.signal_id,
                            kind="position",
                            summary={"status": "closed", "quantity": "0"},
                            observed_at_ns=5_000,
                        ),
                        _observation(
                            event="6",
                            signal_id=retired.value.signal_id,
                            kind="order",
                            summary={"leg": "entry", "status": "submitted"},
                            observed_at_ns=5_000,
                        ),
                        _observation(
                            event="7",
                            signal_id=retired.value.signal_id,
                            kind="order",
                            summary={"leg": "entry", "status": "canceled"},
                            observed_at_ns=5_000,
                        ),
                    )
                )
            )

        rows = repo.execution_recovery_signals(account_slot="demo-v1", since_ns=0, limit=10)

        # `_matched_position` claims by instrument and direction alone, so a stopped-out identity
        # and a canceled entry must never reach it: either would adopt an unrelated position.
        assert materialize_trade_signals(rows) == (active.value.model_copy(update={"seq": rows[0][0]}),)
        assert repo.execution_recovery_signals(account_slot="demo-v1", since_ns=5_001, limit=10) == ()
    finally:
        conn.close()


def test_signal_recovery_readmits_an_identity_that_reopened_after_a_closed_position() -> None:
    """Only the *latest* position fact retires an identity; a reopen is live exposure again."""

    reopened = _prepare_signal(suffix="9", case_id="case-reopened")
    conn = connect_postgres_test(read_only=False)
    try:
        repo = TradingRepository(conn)
        with conn.transaction():
            _append_signal(repo, reopened)
            repo.append_execution_observations(
                prepare_execution_observations(
                    (
                        _observation(
                            event="1",
                            signal_id=reopened.value.signal_id,
                            kind="order",
                            summary={"leg": "entry", "status": "submitted"},
                            observed_at_ns=5_000,
                        ),
                        _observation(
                            event="2",
                            signal_id=reopened.value.signal_id,
                            kind="position",
                            summary={"status": "closed", "quantity": "0"},
                            observed_at_ns=5_000,
                        ),
                        _observation(
                            event="3",
                            signal_id=reopened.value.signal_id,
                            kind="position",
                            summary={"status": "changed", "quantity": "0.02"},
                            observed_at_ns=5_000,
                        ),
                    )
                )
            )

        rows = repo.execution_recovery_signals(account_slot="demo-v1", since_ns=0, limit=10)

        assert materialize_trade_signals(rows) == (reopened.value.model_copy(update={"seq": rows[0][0]}),)
    finally:
        conn.close()


def test_signal_recovery_rejects_a_negative_window() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        repo = TradingRepository(conn)
        with pytest.raises(ValueError, match="execution_recovery_window_invalid"):
            repo.execution_recovery_signals(account_slot="demo-v1", since_ns=-1, limit=10)
    finally:
        conn.close()


def test_database_rejects_execution_fact_mutation() -> None:
    signal = _prepare_signal(suffix="a")
    conn = connect_postgres_test(read_only=False)
    try:
        repo = TradingRepository(conn)
        with conn.transaction():
            _append_signal(repo, signal)
        with (
            pytest.raises(psycopg.errors.RaiseException, match="trading_execution_stream_append_only"),
            conn.transaction(),
        ):
            conn.execute(
                "UPDATE trading_trade_signals SET direction = 'short' WHERE signal_id = %s",
                (signal.value.signal_id,),
            )
        with (
            pytest.raises(psycopg.errors.RaiseException, match="trading_execution_stream_append_only"),
            conn.transaction(),
        ):
            conn.execute("DELETE FROM trading_trade_signals WHERE signal_id = %s", (signal.value.signal_id,))
    finally:
        conn.close()


def test_the_contract_is_the_only_json_bound_and_it_holds_at_the_exact_edges() -> None:
    """The bounds live in one place now, so the test that pins them writes through the real seam.

    Until #520 PR-C the same numbers were stated twice -- once in `ExecutionObservationV1` /
    `TradeSignalV1` and once in `trading_execution_metadata_valid` /
    `trading_execution_string_array_valid` -- and this test proved the two agreed. There is no second
    statement left to disagree with, so what has to be proven is that the surviving one still admits
    the largest legal fact and still refuses the next byte, all the way to durable storage.
    """

    metadata = {f"k{index}": "x" * 246 for index in range(8)}
    references = tuple(f"{index:02d}" + "x" * 250 for index in range(16))
    oversized_metadata = metadata | {"k0": "x" * 247}
    oversized_references = (references[0] + "x", *references[1:])

    assert len(json.dumps(metadata, ensure_ascii=False, sort_keys=True).encode("utf-8")) == 2_048
    assert len(json.dumps(oversized_metadata, ensure_ascii=False, sort_keys=True).encode("utf-8")) == 2_049
    assert len(json.dumps(references, ensure_ascii=False).encode("utf-8")) == 4_096
    assert len(json.dumps(oversized_references, ensure_ascii=False).encode("utf-8")) == 4_097

    signal = _prepare_signal(suffix="3")
    observation = _observation(
        event="4",
        kind="risk",
        native_identity_references=references,
        summary=metadata,
    )
    observation_batch = prepare_execution_observations((observation,))
    with pytest.raises(ValidationError, match="execution_metadata_invalid"):
        _observation(event="5", kind="risk", summary=oversized_metadata)
    with pytest.raises(ValidationError, match="execution_observation_native_identity_invalid"):
        _observation(event="6", kind="risk", native_identity_references=oversized_references)
    with pytest.raises(ValidationError, match="execution_observation_native_identity_invalid"):
        _observation(event="6", kind="risk", native_identity_references=("tf00", 17))
    with pytest.raises(ValidationError):
        _observation(event="6", kind="risk", native_identity_references=tuple(f"ref-{n:03d}" for n in range(17)))

    conn = connect_postgres_test(read_only=False)
    try:
        repo = TradingRepository(conn)
        with conn.transaction():
            _append_signal(repo, signal)
            repo.append_execution_observations(observation_batch)

        stored = conn.execute(
            """
            SELECT (SELECT summary FROM trading_execution_observations WHERE event_id = %s) AS summary,
                   (SELECT native_identity_references FROM trading_execution_observations
                     WHERE event_id = %s) AS native_identity_references
            """,
            (observation.event_id, observation.event_id),
        ).fetchone()
        assert stored is not None
        assert stored["summary"] == metadata
        assert stored["native_identity_references"] == list(references)
    finally:
        conn.close()


def test_unsorted_mixed_case_nautilus_references_are_normalized_by_the_contract_alone() -> None:
    """The collation incident cannot recur, because only one component orders these now.

    Real Nautilus identities mix cases -- Binance contract and position ids are upper case,
    deterministic client order ids are lower case `tf...` -- and they arrive in whatever order the
    callback saw them. `trading_execution_string_array_valid` demanded the database's own collation
    order for the same array `ExecutionObservationV1` sorted by code point, and on 2026-09-02 that
    single disagreement refused every observation of an open position for six hours (#510 A). #520
    PR-C deletes the second opinion: the contract normalizes, the database stores, and no function
    named `trading_*` is left to hold a third one.
    """

    unsorted_references = (
        "tf0065f6482c5577533ba696da631582",
        "UNIUSDT-PERP.BINANCE-OI-RUNTIME-02A27DC240DF",
        "462066006",
        "61742419",
        "tf0065f6482c5577533ba696da631582",
    )
    normalized = tuple(sorted(set(unsorted_references)))
    assert unsorted_references[: len(normalized)] != normalized

    fill = _observation(
        event="7",
        kind="fill",
        native_identity_references=unsorted_references,
        summary={"leg": "entry", "last_quantity": "3", "last_price": "7.401"},
    )
    assert fill.native_identity_references == normalized

    conn = connect_postgres_test(read_only=False)
    try:
        repo = TradingRepository(conn)
        with conn.transaction():
            repo.append_execution_observations(prepare_execution_observations((fill,)))

        stored = conn.execute(
            "SELECT native_identity_references FROM trading_execution_observations WHERE event_id = %s",
            (fill.event_id,),
        ).fetchone()
        assert stored is not None
        assert stored["native_identity_references"] == list(normalized)

        surviving = conn.execute(
            """
            SELECT count(*) AS n FROM pg_proc
             WHERE pronamespace = 'public'::regnamespace AND proname LIKE 'trading\\_%'
            """
        ).fetchone()
        assert surviving is not None
        assert surviving["n"] == 0
    finally:
        conn.close()


def test_execution_stream_constraints_reject_direct_invalid_facts() -> None:
    """What the database still refuses on its own: enumerated values, clocks, correlation and links.

    The per-key `payload` CHECKs used to be in this list. They only ever restated the INSERT that
    produced the row, so #520 PR-C deleted them; the cases below are the refusals no writer can
    supply for itself, because each one is about a *relationship* -- to another row, to the clock, or
    to the fixed value set the column is allowed to hold.
    """

    signal = _prepare_signal(suffix="a")
    command = _prepare_command(suffix="b")
    observation = _observation(event="c", kind="risk")
    observation_batch = prepare_execution_observations((observation,))

    conn = connect_postgres_test(read_only=False)
    try:
        repo = TradingRepository(conn)
        with conn.transaction():
            _append_signal(repo, signal)
            repo.append_operator_intent(command)
            repo.append_execution_observations(observation_batch)

        cases: tuple[tuple[str, tuple[object, ...], type[Exception], str], ...] = (
            (
                """
                INSERT INTO trading_trade_signals (
                  signal_id, case_id, market_key, direction,
                  observed_at_ns, expires_at_ns, payload
                )
                SELECT %s, case_id, market_key, 'sideways',
                       observed_at_ns, expires_at_ns, payload
                  FROM trading_trade_signals WHERE signal_id = %s
                """,
                ("d" * 64, signal.value.signal_id),
                psycopg.errors.CheckViolation,
                "trading_trade_signal_direction_check",
            ),
            (
                """
                INSERT INTO trading_trade_signals (
                  signal_id, case_id, market_key, direction,
                  observed_at_ns, expires_at_ns, payload
                )
                SELECT %s, case_id, market_key, direction,
                       observed_at_ns, observed_at_ns, payload
                  FROM trading_trade_signals WHERE signal_id = %s
                """,
                ("e" * 64, signal.value.signal_id),
                psycopg.errors.CheckViolation,
                "trading_trade_signal_clock_check",
            ),
            (
                """
                INSERT INTO trading_operator_intents (
                  command_id, account_slot, action, scope, reason, operator_identity,
                  authentication_identity, requested_at_ns, expires_at_ns,
                  market_key, direction, payload
                )
                SELECT %s, account_slot, 'manual_entry', scope, reason, operator_identity,
                       authentication_identity, requested_at_ns, expires_at_ns,
                       NULL, NULL, payload
                  FROM trading_operator_intents WHERE command_id = %s
                """,
                ("e" * 64, command.value.command_id),
                psycopg.errors.CheckViolation,
                "trading_operator_intent_manual_entry_check",
            ),
            (
                """
                INSERT INTO trading_execution_observations (
                  event_id, account_slot, execution_strategy,
                  signal_id, command_id, normalized_kind, occurred_at_ns, observed_at_ns,
                  native_identity_references, summary, payload
                )
                SELECT %s, account_slot, execution_strategy,
                       %s, %s, normalized_kind, occurred_at_ns, observed_at_ns,
                       native_identity_references, summary, payload
                  FROM trading_execution_observations WHERE event_id = %s
                """,
                ("d" * 64, signal.value.signal_id, command.value.command_id, observation.event_id),
                psycopg.errors.CheckViolation,
                "trading_execution_observation_correlation_check",
            ),
            (
                """
                INSERT INTO trading_execution_observations (
                  event_id, account_slot, execution_strategy,
                  signal_id, command_id, normalized_kind, occurred_at_ns, observed_at_ns,
                  native_identity_references, summary, payload
                )
                SELECT %s, account_slot, execution_strategy,
                       signal_id, command_id, normalized_kind, 3000, observed_at_ns,
                       native_identity_references, summary, payload
                  FROM trading_execution_observations WHERE event_id = %s
                """,
                ("e" * 64, observation.event_id),
                psycopg.errors.CheckViolation,
                "trading_execution_observation_clock_check",
            ),
            (
                """
                INSERT INTO trading_execution_observations (
                  event_id, account_slot, execution_strategy,
                  signal_id, command_id, normalized_kind, occurred_at_ns, observed_at_ns,
                  native_identity_references, summary, payload
                )
                SELECT %s, account_slot, execution_strategy,
                       %s, command_id, normalized_kind, occurred_at_ns, observed_at_ns,
                       native_identity_references, summary, payload
                  FROM trading_execution_observations WHERE event_id = %s
                """,
                ("f" * 64, "0" * 64, observation.event_id),
                psycopg.errors.ForeignKeyViolation,
                "trading_execution_observations_signal_id_fkey",
            ),
            (
                """
                INSERT INTO trading_execution_observations (
                  event_id, account_slot, execution_strategy,
                  signal_id, command_id, normalized_kind, occurred_at_ns, observed_at_ns,
                  native_identity_references, summary, payload
                )
                SELECT %s, account_slot, execution_strategy,
                       signal_id, %s, normalized_kind, occurred_at_ns, observed_at_ns,
                       native_identity_references, summary, payload
                  FROM trading_execution_observations WHERE event_id = %s
                """,
                ("2" * 64, "2" * 64, observation.event_id),
                psycopg.errors.ForeignKeyViolation,
                "trading_execution_observation_command_fk",
            ),
        )
        for statement, params, error, constraint_name in cases:
            try:
                with conn.transaction():
                    conn.execute(statement, params)
            except error as caught:
                actual_constraint_name = caught.diag.constraint_name
            else:
                pytest.fail(f"database accepted invalid fact for {constraint_name}")
            assert actual_constraint_name == constraint_name
    finally:
        conn.close()


def test_execution_stream_schema_has_the_bounded_read_and_append_guards() -> None:
    tables = (
        "trading_trade_signals",
        "trading_operator_intents",
        "trading_execution_observations",
        "trading_execution_runtime_control_state",
        "trading_execution_runtime_state",
    )
    conn = connect_postgres_test(read_only=False)
    try:
        indexes = {
            row["indexname"]: row["indexdef"]
            for row in conn.execute(
                "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' AND tablename = ANY(%s)",
                (list(tables),),
            ).fetchall()
        }
        constraints: dict[str, set[str]] = {table: set() for table in tables}
        for row in conn.execute(
            """
            SELECT relation.relname AS table_name, con.conname AS constraint_name
              FROM pg_constraint con
              JOIN pg_class relation ON relation.oid = con.conrelid
             WHERE relation.relname = ANY(%s) AND con.contype <> 'n'
            """,
            (list(tables),),
        ).fetchall():
            constraints[row["table_name"]].add(row["constraint_name"])
        triggers = {
            row["tgname"]: row["definition"]
            for row in conn.execute(
                """
                SELECT tgname, pg_get_triggerdef(oid) AS definition
                  FROM pg_trigger
                 WHERE NOT tgisinternal AND tgrelid = ANY(%s::regclass[])
                """,
                (list(tables),),
            ).fetchall()
        }
        functions = {
            row["proname"]: (row["provolatile"], row["proparallel"], row["prosecdef"], row["result_type"])
            for row in conn.execute(
                """
                SELECT proname, provolatile, proparallel, prosecdef,
                       pg_get_function_result(oid) AS result_type
                  FROM pg_proc
                 WHERE pronamespace = 'public'::regnamespace
                   AND (proname LIKE 'trading\\_%' OR proname = 'reject_trading_execution_stream_mutation')
                """
            ).fetchall()
        }
    finally:
        conn.close()

    assert set(indexes) == {
        "trading_trade_signals_pkey",
        "trading_trade_signals_case_id_key",
        "ix_trading_trade_signals_observed_at",
        "ix_trading_trade_signals_expires_at",
        "ix_trading_trade_signals_unresolved",
        "trading_operator_intents_pkey",
        "trading_operator_intent_slot_unique",
        "ix_trading_operator_intents_pending",
        "trading_execution_observations_pkey",
        "trading_execution_observations_seq_key",
        "ix_trading_execution_observations_slot",
        "ix_trading_execution_observations_signal_recovery",
        "ix_trading_execution_observations_command_recovery",
        "ux_trading_execution_signal_disposition",
        "ux_trading_execution_control_disposition",
        "trading_execution_runtime_control_state_pkey",
        "trading_execution_runtime_state_pkey",
        "trading_execution_runtime_state_runtime_id_key",
    }
    assert indexes["ix_trading_trade_signals_unresolved"].endswith(
        "USING btree (seq) INCLUDE (signal_id, expires_at_ns, payload)"
    )
    assert indexes["ix_trading_trade_signals_observed_at"].endswith("USING btree (observed_at_ns)")
    assert indexes["ix_trading_trade_signals_expires_at"].endswith("USING btree (expires_at_ns)")
    assert indexes["ix_trading_operator_intents_pending"].endswith(
        "USING btree (account_slot, seq) INCLUDE (command_id, expires_at_ns)"
    )
    assert "WHERE (normalized_kind = 'signal_disposition'::text)" in indexes["ux_trading_execution_signal_disposition"]
    assert (
        "WHERE (normalized_kind = 'control_disposition'::text)" in indexes["ux_trading_execution_control_disposition"]
    )
    assert constraints == {
        "trading_trade_signals": {
            "trading_trade_signals_pkey",
            "trading_trade_signals_case_id_key",
            "trading_trade_signals_case_fkey",
            "trading_trade_signals_case_link",
            "trading_trade_signal_id_check",
            "trading_trade_signal_case_check",
            "trading_trade_signal_market_check",
            "trading_trade_signal_direction_check",
            "trading_trade_signal_clock_check",
        },
        "trading_operator_intents": {
            "trading_operator_intents_pkey",
            "trading_operator_intent_slot_unique",
            "trading_operator_intent_id_check",
            "trading_operator_intent_slot_check",
            "trading_operator_intent_action_check",
            "trading_operator_intent_text_check",
            "trading_operator_intent_clock_check",
            "trading_operator_intent_manual_entry_check",
        },
        "trading_execution_observations": {
            "trading_execution_observations_pkey",
            "trading_execution_observations_seq_key",
            "trading_execution_observations_signal_id_fkey",
            "trading_execution_observation_command_fk",
            "trading_execution_observation_id_check",
            "trading_execution_observation_slot_check",
            "trading_execution_observation_strategy_check",
            "trading_execution_observation_kind_check",
            "trading_execution_observation_correlation_check",
            "trading_execution_observation_clock_check",
        },
        "trading_execution_runtime_control_state": {
            "trading_execution_runtime_control_state_pkey",
            "trading_execution_runtime_control_slot_check",
            "trading_execution_runtime_control_seq_check",
            "trading_execution_runtime_control_command_check",
            "trading_execution_runtime_control_clock_check",
            "trading_execution_runtime_control_halt_check",
        },
        "trading_execution_runtime_state": {
            "trading_execution_runtime_state_pkey",
            "trading_execution_runtime_state_runtime_id_key",
            "trading_execution_runtime_slot_check",
            "trading_execution_runtime_mode_check",
            "trading_execution_runtime_clock_check",
            "trading_execution_runtime_reason_check",
            "trading_execution_runtime_counts_check",
            "trading_execution_runtime_protection_check",
            "trading_execution_runtime_safe_check",
            "trading_execution_runtime_armed_check",
            "trading_execution_runtime_entry_reason_check",
        },
    }
    assert set(triggers) == {
        "trg_trading_trade_signals_append_only",
        "trg_trading_operator_intents_append_only",
        "trg_trading_execution_observations_append_only",
        "trading_trade_signals_case_link",
    }
    assert all(
        "BEFORE DELETE OR UPDATE" in definition
        for name, definition in triggers.items()
        if name != "trading_trade_signals_case_link"
    )
    assert "CONSTRAINT TRIGGER trading_trade_signals_case_link" in triggers["trading_trade_signals_case_link"]
    # One function is left on this seam, and it is the append-only trigger. Every `trading_*`
    # validator went with the CHECKs that called it (#520 PR-C).
    assert functions == {"reject_trading_execution_stream_mutation": ("v", "u", False, "trigger")}


def test_unresolved_reads_use_the_production_query_specs_and_indexes() -> None:
    signals = tuple(
        _prepare_signal(
            suffix="a",
            case_id=f"query-plan-{index}",
            signal_id=hashlib.sha256(f"signal:{index}".encode()).hexdigest(),
        )
        for index in range(64)
    )
    commands = tuple(
        _prepare_command(
            suffix="d",
            command_id=hashlib.sha256(f"command:{index}".encode()).hexdigest(),
        )
        for index in range(64)
    )
    conn = connect_postgres_test(read_only=False)
    try:
        repo = TradingRepository(conn)
        with conn.transaction():
            for signal, command in zip(signals, commands, strict=True):
                _append_signal(repo, signal)
                repo.append_operator_intent(command)
        conn.execute("ANALYZE trading_trade_signals, trading_operator_intents, trading_execution_observations")
        conn.execute("SET enable_seqscan = off")
        audit = PostgresQueryAudit(
            conn,
            catalog=QueryAuditCatalog(
                queries=execution_stream_query_specs(account_slot="demo-v1"),
                query_routes={"dormant-execution-stream": tuple(spec.name for spec in execution_stream_query_specs())},
                no_sql_routes=frozenset(),
            ),
        ).run(analyze=True)
    finally:
        conn.close()

    assert audit["ok"] is True
    plans = {item["name"]: _plan_index_names(item["plan"]) for item in audit["queries"]}
    # The Signal read is now bounded by the intent's own TTL, so it enters on the expiry index and
    # anti-joins through the disposition index; the Command read still enters on its slot index.
    assert plans["trading_unresolved_trade_signals"] == {
        "ix_trading_trade_signals_expires_at",
        "ux_trading_execution_signal_disposition",
    }
    assert "ix_trading_operator_intents_pending" in plans["trading_unresolved_operator_intents"]
