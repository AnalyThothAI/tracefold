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
    ExecutionProfileActivation,
    ExecutionRuntimeState,
    PreparedExecutionObservationBatch,
    PreparedOperatorIntent,
    PreparedTradeSignal,
    materialize_operator_intent,
    materialize_operator_intents,
    materialize_trade_signal,
    materialize_trade_signals,
    prepare_execution_observations,
    prepare_operator_intent,
    prepare_trade_signal,
)
from tracefold.trading.storage.execution_stream_query_specs import execution_stream_query_specs
from tracefold.trading.storage.root import TradingRepository

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_clone_dsn")]


def _prepare_signal(*, suffix: str, case_id: str | None = None, **updates: object) -> PreparedTradeSignal:
    values: dict[str, object] = {
        "signal_id": suffix * 64,
        "case_id": case_id or f"case-{suffix}",
        "alpha_contract_sha256": "b" * 64,
        "market_key": "crypto:perp:BTC:USDT",
        "direction": "long",
        "observed_at_ns": 1_000,
        "expires_at_ns": 10_000,
        "evidence_sha256": "c" * 64,
        "alpha_metadata": {"policy": "oi-v1"},
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
          supplemental_source_keys, manifest, manifest_sha256, state,
          policy_decision, policy_reason, observed_at_ms, created_at_ms, decided_at_ms,
          updated_at_ms, strategy_id, strategy_version, strategy_config_digest,
          capital_disposition, capital_reason
        ) VALUES (
          %s, %s, 'news', %s, '[]'::jsonb, '{"test":"execution-stream"}'::jsonb,
          %s, 'SIGNAL_EMITTED', 'long', 'execution_stream_fixture', 1, 1, 1, 1,
          'execution_stream_fixture', 'v1', %s, 'not_applicable', NULL
        )
        ON CONFLICT DO NOTHING
        """,
        (case_id, f"stream:{case_id}", f"stream-source:{case_id}", "e" * 64, "f" * 64),
    )
    return repo.append_trade_signal(prepared)


def _prepare_command(*, suffix: str, **updates: object) -> PreparedOperatorIntent:
    values: dict[str, object] = {
        "command_id": suffix * 64,
        "target_profile_id": "demo-v1",
        "action": "pause_entries",
        "scope": "account",
        "reason": "test",
        "operator_identity": "operator:1",
        "authentication_identity": "cli:local",
        "requested_at_ns": 1_000,
        "expires_at_ns": 10_000,
        "confirmation_identity": None,
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
        "runtime_profile_id": "demo-v1",
        "runtime_release": "sha256:" + "1" * 64,
        "execution_strategy": "oi-nautilus-v1",
        "signal_id": signal_id,
        "command_id": command_id,
        "normalized_kind": kind,
        "occurred_at_ns": 2_000,
        "observed_at_ns": 2_100,
        "native_identity_references": (),
        "summary": {"disposition": "accepted"},
        "payload_digest": "2" * 64,
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
    conflicting_observation_batch = prepare_execution_observations(
        (observation.model_copy(update={"payload_digest": "4" * 64}),)
    )
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
            with pytest.raises(RuntimeError, match="execution_stream_identity_conflict"):
                repo.append_execution_observations(conflicting_observation_batch)
    finally:
        conn.close()

    assert materialize_trade_signal(first_row) == materialize_trade_signal(identical_row)
    assert materialize_operator_intent(command_row).command_id == command.value.command_id


def test_operator_ingress_records_inactive_profile_disposition_in_the_same_idempotent_transaction() -> None:
    command = _prepare_command(suffix="9", requested_at_ns=2_000, expires_at_ns=10_000)
    conn = connect_postgres_test(read_only=False)
    try:
        repo = TradingRepository(conn)
        with conn.transaction():
            first = persist_operator_intent(repo, command)
        with conn.transaction():
            retried = persist_operator_intent(repo, command)

        assert first == retried
        assert first.disposition == "not_applied"
        assert first.reason == "execution_profile_inactive"
        assert conn.execute("SELECT count(*) AS n FROM trading_operator_intents").fetchone()["n"] == 1
        observation = conn.execute(
            """
            SELECT normalized_kind, command_id, summary
              FROM trading_execution_observations
             WHERE command_id = %s
            """,
            (command.value.command_id,),
        ).fetchone()
        assert observation == {
            "normalized_kind": "control_disposition",
            "command_id": command.value.command_id,
            "summary": {"disposition": "not_applied", "reason": "execution_profile_inactive"},
        }
    finally:
        conn.close()


def test_operator_ingress_leaves_active_profile_command_for_the_runtime() -> None:
    command = _prepare_command(suffix="8", requested_at_ns=2_000, expires_at_ns=10_000)
    activation = ExecutionProfileActivation(
        runtime_profile_id="demo-v1",
        account_slot="binance_usdm_primary",
        activated_after_signal_seq=0,
        activated_after_command_seq=0,
        mode="disabled",
        runtime_release="sha256:" + "1" * 64,
        config_sha256="3" * 64,
        created_at_ns=1_500,
    )
    conn = connect_postgres_test(read_only=False)
    try:
        repo = TradingRepository(conn)
        with conn.transaction():
            repo.append_execution_profile_activation(activation)
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
                runtime_profile_id="demo-v1",
                execution_strategy="oi-nautilus-v1",
                limit=10,
            )
        assert materialize_operator_intents(unresolved) == (command.value.model_copy(update={"seq": 1}),)
    finally:
        conn.close()


def test_a_webhook_receipt_carries_no_message_id_and_gains_its_four_hour_result() -> None:
    """#458 PR-B on real PostgreSQL: the receipt outlives the absence of a provider message id.

    A Feishu custom-bot webhook returns none, so `message_id` is nullable rather than faked, and the
    four-hour outcome is recorded as a second delivery instant on the same row rather than as an edit
    of a message this channel cannot address again.
    """

    observation = _observation(event="a", kind="audit_gap")
    target = "9" * 64
    conn = connect_postgres_test(read_only=False)
    try:
        repo = TradingRepository(conn)
        with conn.transaction():
            sequences = repo.append_execution_observations(prepare_execution_observations((observation,)))
        with conn.transaction():
            receipt = repo.append_execution_notification_delivery(
                target_sha256=target,
                observation_seq=sequences[0],
                message_id=None,
                delivered_at_ns=5_000,
            )
        assert receipt["message_id"] is None
        assert receipt["result_delivered_at_ns"] is None
        # The watermark advances on the receipt, not on the message id: a webhook target still moves on.
        assert repo.next_execution_notification(target) is None

        # An `audit_gap` has no Signal and therefore no outcome to report.
        assert repo.next_execution_notification_result(target, due_at_or_before_ns=10_000_000) is None

        with conn.transaction():
            assert (
                repo.mark_execution_notification_result(
                    target_sha256=target, observation_seq=sequences[0], result_delivered_at_ns=6_000
                )
                is True
            )
        # Marking is once-only: a retry after the outcome went out must not move the recorded instant.
        with conn.transaction():
            assert (
                repo.mark_execution_notification_result(
                    target_sha256=target, observation_seq=sequences[0], result_delivered_at_ns=7_000
                )
                is False
            )
        stored = conn.execute(
            "SELECT message_id, result_delivered_at_ns FROM trading_execution_notification_deliveries"
        ).fetchone()
        assert stored["message_id"] is None
        assert stored["result_delivered_at_ns"] == 6_000
    finally:
        conn.close()


def test_notification_delivery_is_append_only_and_anti_joined_without_mutating_observation_truth() -> None:
    non_notifiable = _observation(event="6", kind="risk")
    observation = _observation(event="7", kind="audit_gap")
    target = "8" * 64
    conn = connect_postgres_test(read_only=False)
    try:
        repo = TradingRepository(conn)
        with conn.transaction():
            sequences = repo.append_execution_observations(
                prepare_execution_observations((non_notifiable, observation))
            )
        assert len(sequences) == 2
        assert repo.next_execution_notification(target)["event_id"] == observation.event_id
        with pytest.raises(RuntimeError, match="append_execution_notification_delivery_requires_explicit_transaction"):
            repo.append_execution_notification_delivery(
                target_sha256=target,
                observation_seq=sequences[1],
                message_id=41,
                delivered_at_ns=3_000,
            )
        with conn.transaction(), pytest.raises(ValueError, match="execution_notification_delivery_out_of_order"):
            repo.append_execution_notification_delivery(
                target_sha256=target,
                observation_seq=sequences[1] + 1,
                message_id=40,
                delivered_at_ns=2_999,
            )
        with conn.transaction():
            first = repo.append_execution_notification_delivery(
                target_sha256=target,
                observation_seq=sequences[1],
                message_id=41,
                delivered_at_ns=3_000,
            )
        with conn.transaction():
            retried = repo.append_execution_notification_delivery(
                target_sha256=target,
                observation_seq=sequences[1],
                message_id=42,
                delivered_at_ns=3_001,
            )
        assert first == retried
        assert first["message_id"] == 41
        assert repo.next_execution_notification(target) is None
        assert conn.execute("SELECT count(*) AS n FROM trading_execution_notification_deliveries").fetchone()["n"] == 1
        assert conn.execute("SELECT count(*) AS n FROM trading_execution_observations").fetchone()["n"] == 2
    finally:
        conn.close()


def test_execution_stream_append_requires_caller_owned_transaction() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        repo = TradingRepository(conn)
        prepared = prepare_trade_signal(
            signal_id="a" * 64,
            case_id="case-a",
            alpha_contract_sha256="b" * 64,
            market_key="crypto:perp:BTC:USDT",
            direction="long",
            observed_at_ns=1_000,
            expires_at_ns=10_000,
            evidence_sha256="c" * 64,
        )
        with pytest.raises(RuntimeError, match="append_trade_signal_requires_explicit_transaction"):
            repo.append_trade_signal(prepared)
        assert conn.execute("SELECT count(*) AS n FROM trading_trade_signals").fetchone()["n"] == 0
    finally:
        conn.close()


def test_activation_rejects_postgres_unrepresentable_text_before_storage() -> None:
    with pytest.raises(ValueError, match="execution_profile_release_invalid"):
        ExecutionProfileActivation(
            runtime_profile_id="demo-v1",
            account_slot="binance_usdm_primary",
            activated_after_signal_seq=0,
            activated_after_command_seq=0,
            mode="disabled",
            runtime_release="bad\x00release",
            config_sha256="3" * 64,
            created_at_ns=1_500,
        )


def test_concurrent_identical_appends_are_idempotent() -> None:
    signal = _prepare_signal(suffix="a")
    command = _prepare_command(suffix="b")
    activation = ExecutionProfileActivation(
        runtime_profile_id="demo-v1",
        account_slot="binance_usdm_primary",
        activated_after_signal_seq=0,
        activated_after_command_seq=0,
        mode="disabled",
        runtime_release="sha256:" + "1" * 64,
        config_sha256="3" * 64,
        created_at_ns=1_500,
    )
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

            def second_activation_append():
                second.execute("SET application_name = 'tracefold-433-activation-retry'")
                with second.transaction():
                    started.set()
                    return second_repo.append_execution_profile_activation(activation)

            with first.transaction():
                first_activation = first_repo.append_execution_profile_activation(activation)
                activation_future = executor.submit(second_activation_append)
                assert started.wait(1)
                _wait_for_database_lock(observer, application_name="tracefold-433-activation-retry")
            assert activation_future.result(timeout=5) == first_activation

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


def test_activation_fence_and_final_disposition_drive_bounded_anti_join_reads() -> None:
    historical_signal_prepared = _prepare_signal(suffix="a")
    historical_command_prepared = _prepare_command(suffix="b")
    signal_prepared = _prepare_signal(suffix="c", observed_at_ns=2_000, expires_at_ns=20_000)
    command_prepared = _prepare_command(suffix="e", requested_at_ns=2_000, expires_at_ns=20_000)

    conn = connect_postgres_test(read_only=False)
    try:
        with conn.transaction():
            repo = TradingRepository(conn)
            historical_signal_row = _append_signal(repo, historical_signal_prepared)
            historical_command_row = repo.append_operator_intent(historical_command_prepared)

        historical_signal = materialize_trade_signal(historical_signal_row)
        historical_command = materialize_operator_intent(historical_command_row)
        activation = ExecutionProfileActivation(
            runtime_profile_id="demo-v1",
            account_slot="binance_usdm_primary",
            activated_after_signal_seq=historical_signal.seq,
            activated_after_command_seq=historical_command.seq,
            mode="paper",
            runtime_release="sha256:" + "1" * 64,
            config_sha256="3" * 64,
            created_at_ns=1_500,
        )
        conflicting_activation = ExecutionProfileActivation(**(activation.as_kwargs() | {"mode": "disabled"}))

        with conn.transaction():
            assert repo.append_execution_profile_activation(activation) == activation
            with pytest.raises(RuntimeError, match="execution_stream_identity_conflict"):
                repo.append_execution_profile_activation(conflicting_activation)
            signal_row = _append_signal(repo, signal_prepared)
            command_row = repo.append_operator_intent(command_prepared)

        signal = materialize_trade_signal(signal_row)
        command = materialize_operator_intent(command_row)
        with conn.transaction():
            first_signal_read = repo.unresolved_trade_signals(
                runtime_profile_id="demo-v1", execution_strategy="oi-nautilus-v1", limit=10
            )
            second_signal_read = repo.unresolved_trade_signals(
                runtime_profile_id="demo-v1", execution_strategy="oi-nautilus-v1", limit=10
            )
            first_command_read = repo.unresolved_operator_intents(
                runtime_profile_id="demo-v1", execution_strategy="oi-nautilus-v1", limit=10
            )
            second_command_read = repo.unresolved_operator_intents(
                runtime_profile_id="demo-v1", execution_strategy="oi-nautilus-v1", limit=10
            )
        assert materialize_trade_signals(first_signal_read) == (signal,)
        assert materialize_trade_signals(second_signal_read) == (signal,)
        assert materialize_operator_intents(first_command_read) == (command,)
        assert materialize_operator_intents(second_command_read) == (command,)

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
            final_signal_read = repo.unresolved_trade_signals(
                runtime_profile_id="demo-v1", execution_strategy="oi-nautilus-v1", limit=10
            )
            final_command_read = repo.unresolved_operator_intents(
                runtime_profile_id="demo-v1", execution_strategy="oi-nautilus-v1", limit=10
            )
        assert materialize_trade_signals(final_signal_read) == ()
        assert materialize_operator_intents(final_command_read) == ()
    finally:
        conn.close()


def test_rejected_observation_batch_rolls_back_its_new_prefix() -> None:
    existing = _observation(event="a", kind="risk")
    existing_batch = prepare_execution_observations((existing,))
    new_value = _observation(event="b", kind="risk")
    conflicting = existing.model_copy(update={"payload_digest": "4" * 64})
    conflicting_batch = prepare_execution_observations((new_value, conflicting))

    conn = connect_postgres_test(read_only=False)
    try:
        repo = TradingRepository(conn)
        with conn.transaction():
            repo.append_execution_observations(existing_batch)

        with conn.transaction():
            with pytest.raises(RuntimeError, match="execution_stream_identity_conflict"):
                repo.append_execution_observations(conflicting_batch)

            assert conn.execute("SELECT event_id FROM trading_execution_observations ORDER BY event_id").fetchall() == [
                {"event_id": existing.event_id}
            ]
    finally:
        conn.close()


def test_append_rechecks_forged_observation_batch_bounds() -> None:
    forged_count = PreparedExecutionObservationBatch(payload_json=json.dumps([{}] * 129), count=1)
    forged_bytes = PreparedExecutionObservationBatch(
        payload_json=json.dumps([{"blob": "x" * 1_048_576}]),
        count=1,
    )
    conn = connect_postgres_test(read_only=False)
    try:
        repo = TradingRepository(conn)
        for prepared in (forged_count, forged_bytes):
            with conn.transaction(), pytest.raises(ValueError, match="execution_observation_batch_bounds_invalid"):
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


def test_runtime_state_is_single_generation_and_activation_recency_is_authoritative() -> None:
    first_activation = ExecutionProfileActivation(
        runtime_profile_id="demo-v1",
        account_slot="binance_usdm_primary",
        activated_after_signal_seq=0,
        activated_after_command_seq=0,
        mode="paper",
        runtime_release="nautilus-1.231.0+oi-v1",
        config_sha256="3" * 64,
        created_at_ns=1_500,
    )
    second_activation = replace(
        first_activation,
        runtime_profile_id="demo-v2",
        config_sha256="4" * 64,
        created_at_ns=1_600,
    )
    running = ExecutionRuntimeState(
        account_slot="binance_usdm_primary",
        runtime_profile_id="demo-v2",
        mode="paper",
        runtime_release="nautilus-1.231.0+oi-v1",
        config_sha256="4" * 64,
        runtime_id=UUID("11111111-1111-4111-8111-111111111111"),
        runtime_revision="a" * 40,
        image_digest="sha256:" + "b" * 64,
        credential_fingerprint="c" * 64,
        lifecycle_state="running",
        ready=True,
        singleton_ready=True,
        credential_ready=True,
        activation_ready=True,
        startup_reconciled=True,
        portfolio_ready=True,
        audit_ready=True,
        unexpected_exposure=False,
        account_flat=True,
        reconciliation_observed_at_ns=2_000,
        heartbeat_at_ns=2_100,
        unavailable_reason=None,
        started_at_ns=1_900,
        updated_at_ns=2_100,
    )

    conn = connect_postgres_test(read_only=False)
    try:
        repo = TradingRepository(conn)
        with conn.transaction():
            repo.append_execution_profile_activation(first_activation)
            repo.append_execution_profile_activation(second_activation)
            assert repo.latest_execution_profile_activation("binance_usdm_primary") == second_activation
            assert repo.execution_stream_fence() == (0, 0)
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
            lifecycle_state="stopped",
            ready=False,
            heartbeat_at_ns=2_300,
            unavailable_reason="runtime_stopped",
            updated_at_ns=2_300,
        )
        with conn.transaction():
            assert repo.update_execution_runtime_state(stopped) is True
        assert repo.execution_runtime_state("binance_usdm_primary") == stopped
    finally:
        conn.close()


def test_manual_entry_recovery_read_is_activation_bounded() -> None:
    before = _prepare_command(
        suffix="7",
        action="manual_entry",
        scope="market",
        market_key="crypto:perp:BTC:USDT",
        direction="long",
    )
    after = _prepare_command(
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
            repo.append_operator_intent(before)
            _signal_seq, command_seq = repo.execution_stream_fence()
            repo.append_execution_profile_activation(
                ExecutionProfileActivation(
                    runtime_profile_id="demo-v1",
                    account_slot="binance_usdm_primary",
                    activated_after_signal_seq=0,
                    activated_after_command_seq=command_seq,
                    mode="paper",
                    runtime_release="nautilus-1.231.0+oi-v1",
                    config_sha256="3" * 64,
                    created_at_ns=1_500,
                )
            )
            repo.append_operator_intent(after)

        rows = repo.execution_recovery_manual_entries(runtime_profile_id="demo-v1", limit=10)

        assert materialize_operator_intents(rows) == (after.value.model_copy(update={"seq": rows[0][0]}),)
        final = _observation(
            event="9",
            command_id=after.value.command_id,
            kind="control_disposition",
            summary={"action": "manual_entry", "disposition": "rejected"},
        )
        with conn.transaction():
            repo.append_execution_observations(prepare_execution_observations((final,)))
        assert repo.execution_recovery_manual_entries(runtime_profile_id="demo-v1", limit=10) == ()
    finally:
        conn.close()


def test_signal_recovery_keeps_only_current_order_or_position_obligations() -> None:
    active = _prepare_signal(suffix="6", case_id="case-active")
    closed = _prepare_signal(suffix="7", case_id="case-closed")
    conn = connect_postgres_test(read_only=False)
    try:
        repo = TradingRepository(conn)
        with conn.transaction():
            _signal_seq, command_seq = repo.execution_stream_fence()
            repo.append_execution_profile_activation(
                ExecutionProfileActivation(
                    runtime_profile_id="demo-v1",
                    account_slot="binance_usdm_primary",
                    activated_after_signal_seq=0,
                    activated_after_command_seq=command_seq,
                    mode="paper",
                    runtime_release="nautilus-1.231.0+oi-v1",
                    config_sha256="3" * 64,
                    created_at_ns=1_500,
                )
            )
            _append_signal(repo, active)
            _append_signal(repo, closed)
            repo.append_execution_observations(
                prepare_execution_observations(
                    (
                        _observation(
                            event="1",
                            signal_id=active.value.signal_id,
                            kind="signal_disposition",
                        ),
                        _observation(
                            event="2",
                            signal_id=active.value.signal_id,
                            kind="position",
                            summary={"status": "opened", "quantity": "0.01"},
                        ),
                        _observation(
                            event="3",
                            signal_id=closed.value.signal_id,
                            kind="signal_disposition",
                        ),
                        _observation(
                            event="4",
                            signal_id=closed.value.signal_id,
                            kind="position",
                            summary={"status": "opened", "quantity": "0.01"},
                        ),
                        _observation(
                            event="5",
                            signal_id=closed.value.signal_id,
                            kind="position",
                            summary={"status": "closed", "quantity": "0"},
                            occurred_at_ns=2_200,
                            observed_at_ns=2_300,
                        ),
                    )
                )
            )

        rows = repo.execution_recovery_signals(runtime_profile_id="demo-v1", limit=10)

        assert materialize_trade_signals(rows) == (active.value.model_copy(update={"seq": rows[0][0]}),)
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


def test_contract_and_postgres_json_bounds_match_at_exact_edges() -> None:
    metadata = {f"k{index}": "x" * 246 for index in range(8)}
    references = tuple(f"{index:02d}" + "x" * 250 for index in range(16))
    oversized_metadata = metadata | {"k0": "x" * 247}
    oversized_references = (references[0] + "x", *references[1:])

    signal = _prepare_signal(suffix="3", alpha_metadata=metadata)
    observation = _observation(
        event="4",
        kind="risk",
        native_identity_references=references,
        summary=metadata,
    )
    observation_batch = prepare_execution_observations((observation,))
    with pytest.raises(ValidationError, match="execution_metadata_invalid"):
        _prepare_signal(suffix="5", alpha_metadata=oversized_metadata)
    with pytest.raises(ValidationError, match="execution_observation_native_identity_invalid"):
        _observation(event="6", kind="risk", native_identity_references=oversized_references)

    metadata_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
    oversized_metadata_json = json.dumps(oversized_metadata, ensure_ascii=False, sort_keys=True)
    references_json = json.dumps(references, ensure_ascii=False)
    oversized_references_json = json.dumps(oversized_references, ensure_ascii=False)
    assert len(metadata_json.encode("utf-8")) == 2_048
    assert len(oversized_metadata_json.encode("utf-8")) == 2_049
    assert len(references_json.encode("utf-8")) == 4_096
    assert len(oversized_references_json.encode("utf-8")) == 4_097

    conn = connect_postgres_test(read_only=False)
    try:
        repo = TradingRepository(conn)
        with conn.transaction():
            _append_signal(repo, signal)
            repo.append_execution_observations(observation_batch)

        validators = conn.execute(
            """
            SELECT trading_execution_metadata_valid(%s::jsonb) AS metadata_at_limit,
                   trading_execution_metadata_valid(%s::jsonb) AS metadata_over_limit,
                   trading_execution_string_array_valid(%s::jsonb) AS references_at_limit,
                   trading_execution_string_array_valid(%s::jsonb) AS references_over_limit
            """,
            (metadata_json, oversized_metadata_json, references_json, oversized_references_json),
        ).fetchone()
        assert validators == {
            "metadata_at_limit": True,
            "metadata_over_limit": False,
            "references_at_limit": True,
            "references_over_limit": False,
        }

        with pytest.raises(psycopg.errors.CheckViolation) as metadata_rejected, conn.transaction():
            conn.execute(
                """
                INSERT INTO trading_trade_signals (
                  signal_id, case_id, alpha_contract_sha256, market_key, direction,
                  observed_at_ns, expires_at_ns, evidence_sha256, alpha_metadata, payload
                )
                SELECT %s, 'json-boundary-invalid', alpha_contract_sha256, market_key, direction,
                       observed_at_ns, expires_at_ns, evidence_sha256, %s::jsonb,
                       payload || jsonb_build_object(
                         'signal_id', %s::text, 'case_id', 'json-boundary-invalid',
                         'alpha_metadata', %s::jsonb
                       )
                  FROM trading_trade_signals WHERE signal_id = %s
                """,
                (
                    "5" * 64,
                    oversized_metadata_json,
                    "5" * 64,
                    oversized_metadata_json,
                    signal.value.signal_id,
                ),
            )
        assert metadata_rejected.value.diag.constraint_name == "trading_trade_signal_metadata_check"

        with pytest.raises(psycopg.errors.CheckViolation) as references_rejected, conn.transaction():
            conn.execute(
                """
                INSERT INTO trading_execution_observations (
                  event_id, runtime_profile_id, runtime_release, execution_strategy,
                  signal_id, command_id, normalized_kind, occurred_at_ns, observed_at_ns,
                  native_identity_references, summary, payload_digest, payload
                )
                SELECT %s, runtime_profile_id, runtime_release, execution_strategy,
                       signal_id, command_id, normalized_kind, occurred_at_ns, observed_at_ns,
                       %s::jsonb, summary, payload_digest,
                       payload || jsonb_build_object(
                         'event_id', %s::text, 'native_identity_references', %s::jsonb
                       )
                  FROM trading_execution_observations WHERE event_id = %s
                """,
                (
                    "6" * 64,
                    oversized_references_json,
                    "6" * 64,
                    oversized_references_json,
                    observation.event_id,
                ),
            )
        assert references_rejected.value.diag.constraint_name == "trading_execution_observation_native_refs_check"
    finally:
        conn.close()


def test_execution_stream_constraints_reject_direct_invalid_facts() -> None:
    signal = _prepare_signal(suffix="a")
    command = _prepare_command(suffix="b")
    activation = ExecutionProfileActivation(
        runtime_profile_id="demo-v1",
        account_slot="binance_usdm_primary",
        activated_after_signal_seq=0,
        activated_after_command_seq=0,
        mode="disabled",
        runtime_release="sha256:" + "1" * 64,
        config_sha256="3" * 64,
        created_at_ns=1_500,
    )
    observation = _observation(event="c", kind="risk")
    observation_batch = prepare_execution_observations((observation,))

    conn = connect_postgres_test(read_only=False)
    try:
        repo = TradingRepository(conn)
        with conn.transaction():
            _append_signal(repo, signal)
            repo.append_operator_intent(command)
            repo.append_execution_profile_activation(activation)
            repo.append_execution_observations(observation_batch)

        cases: tuple[tuple[str, tuple[object, ...], type[Exception], str], ...] = (
            (
                """
                INSERT INTO trading_trade_signals (
                  signal_id, case_id, alpha_contract_sha256, market_key, direction,
                  observed_at_ns, expires_at_ns, evidence_sha256, alpha_metadata, payload
                )
                SELECT %s, 'case-drift', alpha_contract_sha256, market_key, 'short',
                       observed_at_ns, expires_at_ns, evidence_sha256, alpha_metadata,
                       payload || jsonb_build_object('signal_id', %s::text, 'case_id', 'case-drift')
                  FROM trading_trade_signals WHERE signal_id = %s
                """,
                ("d" * 64, "d" * 64, signal.value.signal_id),
                psycopg.errors.CheckViolation,
                "trading_trade_signal_payload_check",
            ),
            (
                """
                INSERT INTO trading_operator_intents (
                  command_id, target_profile_id, action, scope, reason, operator_identity,
                  authentication_identity, requested_at_ns, expires_at_ns,
                  confirmation_identity, market_key, direction, payload
                )
                SELECT %s, target_profile_id, 'resume_entries', scope, reason, operator_identity,
                       authentication_identity, requested_at_ns, expires_at_ns,
                       NULL, NULL, NULL,
                       payload || jsonb_build_object('command_id', %s::text, 'action', 'resume_entries')
                  FROM trading_operator_intents WHERE command_id = %s
                """,
                ("d" * 64, "d" * 64, command.value.command_id),
                psycopg.errors.CheckViolation,
                "trading_operator_intent_confirmation_check",
            ),
            (
                """
                INSERT INTO trading_operator_intents (
                  command_id, target_profile_id, action, scope, reason, operator_identity,
                  authentication_identity, requested_at_ns, expires_at_ns,
                  confirmation_identity, market_key, direction, payload
                )
                SELECT %s, target_profile_id, 'manual_entry', scope, reason, operator_identity,
                       authentication_identity, requested_at_ns, expires_at_ns,
                       NULL, NULL, NULL,
                       payload || jsonb_build_object('command_id', %s::text, 'action', 'manual_entry')
                  FROM trading_operator_intents WHERE command_id = %s
                """,
                ("e" * 64, "e" * 64, command.value.command_id),
                psycopg.errors.CheckViolation,
                "trading_operator_intent_manual_entry_check",
            ),
            (
                """
                INSERT INTO trading_operator_intents (
                  command_id, target_profile_id, action, scope, reason, operator_identity,
                  authentication_identity, requested_at_ns, expires_at_ns,
                  confirmation_identity, market_key, direction, payload
                )
                SELECT %s, target_profile_id, action, scope, reason, operator_identity,
                       authentication_identity, requested_at_ns, expires_at_ns,
                       confirmation_identity, market_key, direction,
                       payload || jsonb_build_object('command_id', %s::text, 'reason', 'payload drift')
                  FROM trading_operator_intents WHERE command_id = %s
                """,
                ("f" * 64, "f" * 64, command.value.command_id),
                psycopg.errors.CheckViolation,
                "trading_operator_intent_payload_check",
            ),
            (
                """
                INSERT INTO trading_execution_profile_activations (
                  runtime_profile_id, account_slot, activated_after_signal_seq,
                  activated_after_command_seq, mode, runtime_release, config_sha256, created_at_ns
                ) VALUES ('invalid-fence', 'slot', -1, 0, 'disabled', 'release', %s, 1)
                """,
                ("4" * 64,),
                psycopg.errors.CheckViolation,
                "trading_execution_activation_fence_check",
            ),
            (
                """
                INSERT INTO trading_execution_observations (
                  event_id, runtime_profile_id, runtime_release, execution_strategy,
                  signal_id, command_id, normalized_kind, occurred_at_ns, observed_at_ns,
                  native_identity_references, summary, payload_digest, payload
                )
                SELECT %s, runtime_profile_id, runtime_release, execution_strategy,
                       %s, %s, normalized_kind, occurred_at_ns, observed_at_ns,
                       native_identity_references, summary, payload_digest,
                       payload || jsonb_build_object(
                         'event_id', %s::text, 'signal_id', %s::text, 'command_id', %s::text
                       )
                  FROM trading_execution_observations WHERE event_id = %s
                """,
                (
                    "d" * 64,
                    signal.value.signal_id,
                    command.value.command_id,
                    "d" * 64,
                    signal.value.signal_id,
                    command.value.command_id,
                    observation.event_id,
                ),
                psycopg.errors.CheckViolation,
                "trading_execution_observation_correlation_check",
            ),
            (
                """
                INSERT INTO trading_execution_observations (
                  event_id, runtime_profile_id, runtime_release, execution_strategy,
                  signal_id, command_id, normalized_kind, occurred_at_ns, observed_at_ns,
                  native_identity_references, summary, payload_digest, payload
                )
                SELECT %s, runtime_profile_id, runtime_release, execution_strategy,
                       signal_id, command_id, normalized_kind, 3000, observed_at_ns,
                       native_identity_references, summary, payload_digest,
                       payload || jsonb_build_object('event_id', %s::text, 'occurred_at_ns', 3000)
                  FROM trading_execution_observations WHERE event_id = %s
                """,
                ("e" * 64, "e" * 64, observation.event_id),
                psycopg.errors.CheckViolation,
                "trading_execution_observation_clock_check",
            ),
            (
                """
                INSERT INTO trading_execution_observations (
                  event_id, runtime_profile_id, runtime_release, execution_strategy,
                  signal_id, command_id, normalized_kind, occurred_at_ns, observed_at_ns,
                  native_identity_references, summary, payload_digest, payload
                )
                SELECT %s, runtime_profile_id, runtime_release, execution_strategy,
                       %s, command_id, normalized_kind, occurred_at_ns, observed_at_ns,
                       native_identity_references, summary, payload_digest,
                       payload || jsonb_build_object('event_id', %s::text, 'signal_id', %s::text)
                  FROM trading_execution_observations WHERE event_id = %s
                """,
                ("f" * 64, "0" * 64, "f" * 64, "0" * 64, observation.event_id),
                psycopg.errors.ForeignKeyViolation,
                "trading_execution_observations_signal_id_fkey",
            ),
            (
                """
                INSERT INTO trading_execution_observations (
                  event_id, runtime_profile_id, runtime_release, execution_strategy,
                  signal_id, command_id, normalized_kind, occurred_at_ns, observed_at_ns,
                  native_identity_references, summary, payload_digest, payload
                )
                SELECT %s, runtime_profile_id, runtime_release, execution_strategy,
                       signal_id, %s, normalized_kind, occurred_at_ns, observed_at_ns,
                       native_identity_references, summary, payload_digest,
                       payload || jsonb_build_object('event_id', %s::text, 'command_id', %s::text)
                  FROM trading_execution_observations WHERE event_id = %s
                """,
                ("2" * 64, "2" * 64, "2" * 64, "2" * 64, observation.event_id),
                psycopg.errors.ForeignKeyViolation,
                "trading_execution_observation_command_fk",
            ),
            (
                """
                INSERT INTO trading_execution_observations (
                  event_id, runtime_profile_id, runtime_release, execution_strategy,
                  signal_id, command_id, normalized_kind, occurred_at_ns, observed_at_ns,
                  native_identity_references, summary, payload_digest, payload
                )
                SELECT %s, runtime_profile_id, runtime_release, execution_strategy,
                       signal_id, command_id, normalized_kind, occurred_at_ns, observed_at_ns,
                       native_identity_references, '{"different":true}'::jsonb, payload_digest,
                       payload || jsonb_build_object('event_id', %s::text)
                  FROM trading_execution_observations WHERE event_id = %s
                """,
                ("1" * 64, "1" * 64, observation.event_id),
                psycopg.errors.CheckViolation,
                "trading_execution_observation_payload_check",
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
        "trading_execution_profile_activations",
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
                   AND proname = ANY(%s)
                """,
                (
                    [
                        "trading_execution_metadata_valid",
                        "trading_execution_string_array_valid",
                        "reject_trading_execution_stream_mutation",
                    ],
                ),
            ).fetchall()
        }
        validators = conn.execute(
            """
            SELECT trading_execution_metadata_valid('{"ok":1,"flag":true}'::jsonb) AS metadata_valid,
                   trading_execution_metadata_valid('{"nested":{}}'::jsonb) AS metadata_nested,
                   trading_execution_string_array_valid('["a","b"]'::jsonb) AS refs_valid,
                   trading_execution_string_array_valid('["b","a"]'::jsonb) AS refs_unsorted,
                   trading_execution_string_array_valid('["a","a"]'::jsonb) AS refs_duplicate
            """
        ).fetchone()
    finally:
        conn.close()

    assert set(indexes) == {
        "trading_trade_signals_pkey",
        "trading_trade_signals_case_id_key",
        "ix_trading_trade_signals_observed_at",
        "ix_trading_trade_signals_expires_at",
        "ix_trading_trade_signals_unresolved",
        "trading_operator_intents_pkey",
        "trading_operator_intent_profile_unique",
        "ix_trading_operator_intents_unresolved",
        "trading_execution_observations_pkey",
        "trading_execution_observations_seq_key",
        "ix_trading_execution_observations_runtime",
        "ix_trading_execution_observations_signal_recovery",
        "ix_trading_execution_observations_command_recovery",
        "trading_execution_notification_candidates_idx",
        "ux_trading_execution_signal_disposition",
        "ux_trading_execution_control_disposition",
        "trading_execution_profile_activations_pkey",
        "ix_trading_execution_activations_slot_created",
        "trading_execution_runtime_state_pkey",
        "trading_execution_runtime_state_runtime_id_key",
    }
    assert indexes["ix_trading_trade_signals_unresolved"].endswith(
        "USING btree (seq) INCLUDE (signal_id, alpha_contract_sha256, expires_at_ns, payload)"
    )
    assert indexes["ix_trading_trade_signals_observed_at"].endswith("USING btree (observed_at_ns)")
    assert indexes["ix_trading_trade_signals_expires_at"].endswith("USING btree (expires_at_ns)")
    assert indexes["ix_trading_operator_intents_unresolved"].endswith(
        "USING btree (target_profile_id, seq) INCLUDE (command_id, expires_at_ns)"
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
            "trading_trade_signal_alpha_sha_check",
            "trading_trade_signal_market_check",
            "trading_trade_signal_direction_check",
            "trading_trade_signal_clock_check",
            "trading_trade_signal_evidence_sha_check",
            "trading_trade_signal_metadata_check",
            "trading_trade_signal_payload_check",
        },
        "trading_operator_intents": {
            "trading_operator_intents_pkey",
            "trading_operator_intent_profile_unique",
            "trading_operator_intent_id_check",
            "trading_operator_intent_profile_check",
            "trading_operator_intent_action_check",
            "trading_operator_intent_text_check",
            "trading_operator_intent_clock_check",
            "trading_operator_intent_confirmation_check",
            "trading_operator_intent_manual_entry_check",
            "trading_operator_intent_payload_check",
        },
        "trading_execution_profile_activations": {
            "trading_execution_profile_activations_pkey",
            "trading_execution_activation_profile_check",
            "trading_execution_activation_slot_check",
            "trading_execution_activation_fence_check",
            "trading_execution_activation_mode_check",
            "trading_execution_activation_release_check",
            "trading_execution_activation_config_check",
            "trading_execution_activation_clock_check",
        },
        "trading_execution_observations": {
            "trading_execution_observations_pkey",
            "trading_execution_observations_seq_key",
            "trading_execution_observations_signal_id_fkey",
            "trading_execution_observation_command_fk",
            "trading_execution_observation_id_check",
            "trading_execution_observation_profile_check",
            "trading_execution_observation_release_check",
            "trading_execution_observation_strategy_check",
            "trading_execution_observation_kind_check",
            "trading_execution_observation_correlation_check",
            "trading_execution_observation_clock_check",
            "trading_execution_observation_native_refs_check",
            "trading_execution_observation_summary_check",
            "trading_execution_observation_digest_check",
            "trading_execution_observation_payload_check",
        },
        "trading_execution_runtime_state": {
            "trading_execution_runtime_state_pkey",
            "trading_execution_runtime_state_runtime_id_key",
            "trading_execution_runtime_state_runtime_profile_id_fkey",
            "trading_execution_runtime_slot_check",
            "trading_execution_runtime_profile_check",
            "trading_execution_runtime_mode_check",
            "trading_execution_runtime_release_check",
            "trading_execution_runtime_config_check",
            "trading_execution_runtime_revision_check",
            "trading_execution_runtime_image_check",
            "trading_execution_runtime_credential_check",
            "trading_execution_runtime_lifecycle_check",
            "trading_execution_runtime_clock_check",
            "trading_execution_runtime_ready_check",
            "trading_execution_runtime_reason_check",
        },
    }
    assert set(triggers) == {
        "trg_trading_trade_signals_append_only",
        "trg_trading_operator_intents_append_only",
        "trg_trading_execution_observations_append_only",
        "trg_trading_execution_profile_activations_append_only",
        "trading_trade_signals_case_link",
    }
    assert all(
        "BEFORE DELETE OR UPDATE" in definition
        for name, definition in triggers.items()
        if name != "trading_trade_signals_case_link"
    )
    assert "CONSTRAINT TRIGGER trading_trade_signals_case_link" in triggers["trading_trade_signals_case_link"]
    assert functions == {
        "trading_execution_metadata_valid": ("i", "s", False, "boolean"),
        "trading_execution_string_array_valid": ("i", "s", False, "boolean"),
        "reject_trading_execution_stream_mutation": ("v", "u", False, "trigger"),
    }
    assert validators == {
        "metadata_valid": True,
        "metadata_nested": False,
        "refs_valid": True,
        "refs_unsorted": False,
        "refs_duplicate": False,
    }


def test_unresolved_reads_use_the_production_query_specs_and_indexes() -> None:
    activation = ExecutionProfileActivation(
        runtime_profile_id="demo-v1",
        account_slot="binance_usdm_primary",
        activated_after_signal_seq=0,
        activated_after_command_seq=0,
        mode="disabled",
        runtime_release="sha256:" + "1" * 64,
        config_sha256="3" * 64,
        created_at_ns=1_500,
    )
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
            repo.append_execution_profile_activation(activation)
            for signal, command in zip(signals, commands, strict=True):
                _append_signal(repo, signal)
                repo.append_operator_intent(command)
        conn.execute("ANALYZE trading_trade_signals, trading_operator_intents, trading_execution_observations")
        conn.execute("SET enable_seqscan = off")
        audit = PostgresQueryAudit(
            conn,
            catalog=QueryAuditCatalog(
                queries=execution_stream_query_specs(runtime_profile_id="demo-v1"),
                query_routes={"dormant-execution-stream": tuple(spec.name for spec in execution_stream_query_specs())},
                no_sql_routes=frozenset(),
            ),
        ).run(analyze=True)
    finally:
        conn.close()

    assert audit["ok"] is True
    plans = {item["name"]: _plan_index_names(item["plan"]) for item in audit["queries"]}
    assert "ix_trading_trade_signals_unresolved" in plans["trading_unresolved_trade_signals"]
    assert "ix_trading_operator_intents_unresolved" in plans["trading_unresolved_operator_intents"]
