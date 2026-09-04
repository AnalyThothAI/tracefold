"""Real PostgreSQL and pinned Nautilus process seam for #433-B."""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal
from functools import partial
from uuid import uuid4

import psycopg
import pytest

from tests.nautilus_oi_runtime_fixtures import NOW_NS, oi_profile
from tests.postgres_test_utils import connect_postgres_test, postgres_settings_storage
from tracefold.app.nautilus.oi_runtime import (
    OiRuntimeDatabaseBridge,
    RuntimeStateProjector,
    flush_audit_once,
    load_or_record_day_start,
    load_runtime_control_state,
    load_unresolved_operator_intents,
    load_unresolved_trade_signals,
)
from tracefold.app.operator_control import persist_operator_intent
from tracefold.app.repository_session import repositories_for_connection
from tracefold.integrations.nautilus.oi_runtime.audit_sink import AuditSink, ObservationFactory
from tracefold.integrations.nautilus.oi_runtime.signal_client import ExecutionSignalClient
from tracefold.integrations.nautilus.oi_runtime.singleton import AccountSlotSingleton
from tracefold.integrations.nautilus.oi_runtime.state import RuntimeControlSnapshot, deterministic_client_order_id
from tracefold.platform.config.models import Settings
from tracefold.trading import ExecutionObservationV1, parse_operator_command, prepare_parsed_operator_intent
from tracefold.trading.storage.execution_stream import (
    ExecutionRuntimeState,
    PreparedOperatorIntent,
    prepare_execution_observations,
    prepare_operator_intent,
    prepare_trade_signal,
)
from tracefold.trading.storage.root import TradingRepository

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_clone_dsn")]


_ACCOUNT_SLOT = "binance_usdm_primary"


def _control_row(repo: TradingRepository) -> None:
    """The current control row a Runtime creates for its slot on first start (#520 PR-A)."""

    with repo.conn.transaction():
        repo.ensure_execution_runtime_control_state(_ACCOUNT_SLOT, now_ns=NOW_NS)


def _append_signal(repo: TradingRepository, *, suffix: str = "1") -> None:
    case_id = f"case-{suffix}"
    prepared = prepare_trade_signal(
        signal_id=suffix * 64,
        case_id=case_id,
        market_key="crypto:perp:BTC:USDT",
        direction="long",
        observed_at_ns=NOW_NS - 1_000_000,
        expires_at_ns=NOW_NS + 60_000_000_000,
    )
    with repo.conn.transaction():
        repo.conn.execute(
            """
            INSERT INTO trading_cases (
              case_id, underlying_key, trigger_kind, primary_source_key,
              manifest, manifest_sha256, state,
              policy_decision, policy_reason, observed_at_ms, created_at_ms, decided_at_ms,
              updated_at_ms
            ) VALUES (
              %s, %s, 'oi', %s, '{"test":"nautilus-runtime"}'::jsonb,
              %s, 'SIGNAL_EMITTED', 'long', 'nautilus_runtime_fixture', 1, 1, 1, 1
            )
            """,
            (case_id, f"runtime:{case_id}", f"runtime-source:{case_id}", "4" * 64),
        )
        repo.append_trade_signal(prepared)


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
        expires_at_ns=NOW_NS + 60_000_000_000,
        market_key=None,
        direction=None,
    )
    with repo.conn.transaction():
        repo.append_operator_intent(prepared)
    return prepared


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _append_entry_order_fact(repo: TradingRepository, *, signal_id: str, observed_at_ns: int = NOW_NS) -> None:
    """Write the durable `order`/`leg=entry` fact a restart reclaims ownership from."""

    observation = ExecutionObservationV1.model_validate(
        {
            "event_id": _sha(f"entry-order:{signal_id}"),
            "account_slot": _ACCOUNT_SLOT,
            "execution_strategy": "oi_nautilus_v1",
            "signal_id": signal_id,
            "command_id": None,
            "normalized_kind": "order",
            "occurred_at_ns": observed_at_ns,
            "observed_at_ns": observed_at_ns,
            "native_identity_references": (
                deterministic_client_order_id(
                    namespace=oi_profile().client_order_namespace,
                    entry_id=signal_id,
                    leg="entry",
                ).value,
            ),
            "summary": {"leg": "entry", "status": "submitted"},
        }
    )
    with repo.conn.transaction():
        repo.append_execution_observations(prepare_execution_observations((observation,)))


def _append_closed_position_fact(repo: TradingRepository, *, signal_id: str) -> None:
    """Write the `position`/`closed` fact that retires an identity from recovery."""

    observation = ExecutionObservationV1.model_validate(
        {
            "event_id": _sha(f"closed-position:{signal_id}"),
            "account_slot": _ACCOUNT_SLOT,
            "execution_strategy": "oi_nautilus_v1",
            "signal_id": signal_id,
            "command_id": None,
            "normalized_kind": "position",
            "occurred_at_ns": NOW_NS,
            "observed_at_ns": NOW_NS,
            "native_identity_references": (),
            "summary": {"status": "closed", "quantity": "0"},
        }
    )
    with repo.conn.transaction():
        repo.append_execution_observations(prepare_execution_observations((observation,)))


def _append_input_burst(repo: TradingRepository, *, size: int) -> None:
    with repo.conn.transaction():
        for index in range(size):
            case_id = f"pra-burst-{index}"
            repo.conn.execute(
                """
                INSERT INTO trading_cases (
                  case_id, underlying_key, trigger_kind, primary_source_key,
                  manifest, manifest_sha256, state,
                  policy_decision, policy_reason, observed_at_ms, created_at_ms, decided_at_ms,
                  updated_at_ms
                ) VALUES (
                  %s, %s, 'oi', %s, '{"test":"475-pra"}'::jsonb,
                  %s, 'SIGNAL_EMITTED', 'long', '475-pra', 1, 1, 1, 1
                )
                """,
                (
                    case_id,
                    f"runtime:{case_id}",
                    f"runtime-source:{case_id}",
                    _sha(f"manifest:{index}"),
                ),
            )
            repo.append_trade_signal(
                prepare_trade_signal(
                    signal_id=_sha(f"signal:{index}"),
                    case_id=case_id,
                    market_key="crypto:perp:BTC:USDT",
                    direction="long",
                    observed_at_ns=NOW_NS - 1_000_000,
                    expires_at_ns=NOW_NS + 60_000_000_000,
                )
            )
            repo.append_operator_intent(
                prepare_operator_intent(
                    command_id=_sha(f"command:{index}"),
                    account_slot=_ACCOUNT_SLOT,
                    action="pause_entries",
                    scope="entries",
                    reason="475 PR-A burst",
                    operator_identity="operator:475-pra",
                    authentication_identity="test:authenticated",
                    requested_at_ns=NOW_NS,
                    expires_at_ns=NOW_NS + 60_000_000_000,
                    market_key=None,
                    direction=None,
                )
            )


def _bridge_singleton() -> AccountSlotSingleton:
    """The lock the bridge heartbeats; these tests are about the stream session, not the lock."""

    singleton = AccountSlotSingleton(
        account_slot="binance_usdm_primary",
        try_acquire=lambda _slot: True,
        release=lambda _slot: True,
        heartbeat=lambda: True,
    )
    assert singleton.acquire() is True
    return singleton


def _bridge_projector() -> RuntimeStateProjector:
    """A projector with nothing offered: `write_once` is a no-op until the loop offers a row."""

    return RuntimeStateProjector(initial=_runtime_state(), recovery_inputs=((), ()))


def _runtime_state() -> ExecutionRuntimeState:
    return ExecutionRuntimeState(
        account_slot="binance_usdm_primary",
        mode="paper",
        runtime_id=uuid4(),
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


def _runtime_bridge(signals: ExecutionSignalClient, *, poll_seconds: float = 0.2) -> OiRuntimeDatabaseBridge:
    profile = oi_profile()
    return OiRuntimeDatabaseBridge(
        settings=Settings(ws_token="475-pra", storage=postgres_settings_storage()),
        profile=profile,
        signals=signals,
        audit=AuditSink(factory=ObservationFactory(profile.account_slot, "oi_nautilus_v1")),
        update_day_start=lambda _baseline: None,
        singleton=_bridge_singleton(),
        projector=_bridge_projector(),
        poll_seconds=poll_seconds,
    )


def _wait_for_bridge(
    bridge: OiRuntimeDatabaseBridge,
    predicate: Callable[[], bool],
    *,
    timeout_seconds: float = 3.0,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while not predicate() and time.monotonic() < deadline:
        if bridge.fatal_error is not None:
            raise bridge.fatal_error
        time.sleep(0.005)
    assert predicate()
    assert bridge.fatal_error is None


def _stop_bridge(bridge: OiRuntimeDatabaseBridge) -> None:
    bridge.stop()
    bridge.join(2.0)
    assert bridge.connected is False


_NAUTILUS_SHAPED_REFERENCES = (
    "462066006",
    "61742419",
    "UNIUSDT-PERP.BINANCE-OI-RUNTIME-02A27DC240DF",
    "tf0065f6482c5577533ba696da631582",
)


def test_a_database_refused_batch_is_quarantined_and_leaves_a_durable_audit_gap() -> None:
    """The real refusal, the real recovery: PostgreSQL says no, the ledger says what it lost.

    The refusal is the Command foreign key -- a fill correlated to a Command nobody issued.
    Until #520 PR-C this test installed the pre-`0353` collation function instead; that whole class of
    refusal is gone with the JSON-shape CHECKs, and what is left is exactly the relational kind the
    contract cannot check for itself. What is being proved here is everything a fake writer cannot:
    that psycopg raises an `IntegrityError` the App writer catches, that the aborted transaction leaves
    the session usable, that the `audit_gap` lands durably in the same flush, and that the Signal whose
    disposition was refused stops being pending instead of hanging forever.
    """

    conn = connect_postgres_test(read_only=False)
    try:
        repos = repositories_for_connection(conn)
        repo = TradingRepository(conn)
        _control_row(repo)
        _append_signal(repo, suffix="1")

        profile = oi_profile()
        factory = ObservationFactory(profile.account_slot, "oi_nautilus_v1")
        audit = AuditSink(factory=factory)
        signals = ExecutionSignalClient(
            account_slot=profile.account_slot,
            execution_strategy="oi_nautilus_v1",
        )
        assert signals.poll_once(partial(load_unresolved_trade_signals, repos)) == 1
        signal = signals.next_nowait()
        assert signal is not None
        assert signals.pending_ids == {signal.signal_id}

        orphan_command_id = "9" * 64
        orphan = factory.create(
            normalized_kind="fill",
            command_id=orphan_command_id,
            occurred_at_ns=NOW_NS,
            observed_at_ns=NOW_NS,
            native_identity_references=_NAUTILUS_SHAPED_REFERENCES,
            summary={"leg": "entry", "last_quantity": "3"},
            payload={"leg": "entry"},
        )
        disposition = factory.create(
            normalized_kind="signal_disposition",
            signal_id=signal.signal_id,
            occurred_at_ns=NOW_NS + 1,
            observed_at_ns=NOW_NS + 1,
            native_identity_references=_NAUTILUS_SHAPED_REFERENCES,
            summary={"disposition": "accepted"},
            payload={"disposition": "accepted"},
        )
        assert audit.offer(orphan) is True
        assert audit.offer(disposition) is True

        with pytest.raises(psycopg.errors.ForeignKeyViolation) as refused, conn.transaction():
            repo.append_execution_observations(prepare_execution_observations((orphan,)))
        assert refused.value.diag.constraint_name == "trading_execution_observation_command_fk"

        assert flush_audit_once(repos=repos, audit=audit, signals=signals) == 3

        assert signals.pending_ids == set()
        assert audit.queued_count == 0
        assert audit.healthy is True
        rows = conn.execute(
            "SELECT event_id, normalized_kind, summary FROM trading_execution_observations ORDER BY seq"
        ).fetchall()
        assert [row["normalized_kind"] for row in rows] == ["audit_gap"]
        assert rows[0]["summary"] == {
            "cause": "audit_append_rejected",
            "dropped_count": 2,
            "first_event_id": orphan.event_id,
            "kind.fill": 1,
            "kind.signal_disposition": 1,
        }

        # Issue the Command the fill names, and the same observation is admitted unchanged --
        # mixed-case Nautilus identities and all.
        with conn.transaction():
            repo.append_operator_intent(
                prepare_operator_intent(
                    command_id=orphan_command_id,
                    account_slot=profile.account_slot,
                    action="manual_entry",
                    scope="market",
                    reason="quarantine recovery",
                    operator_identity="operator:test",
                    authentication_identity="test:authenticated",
                    requested_at_ns=NOW_NS,
                    expires_at_ns=NOW_NS + 1_000_000_000,
                    market_key="crypto:perp:UNI:USDT",
                    direction="long",
                )
            )
        assert audit.offer(orphan) is True
        assert flush_audit_once(repos=repos, audit=audit, signals=signals) == 1
        stored = conn.execute(
            "SELECT native_identity_references FROM trading_execution_observations WHERE event_id = %s",
            (orphan.event_id,),
        ).fetchone()
        assert stored is not None
        assert stored["native_identity_references"] == list(_NAUTILUS_SHAPED_REFERENCES)
    finally:
        conn.close()


def test_a_signal_with_no_notification_is_consumed_within_one_poll() -> None:
    """#537 PR-4. The indexed anti-join is the whole delivery path.

    A Signal is unresolved until a disposition observation exists, so this read is complete on its
    own: nothing about the append has to tell the reader it happened. The `LISTEN`/`NOTIFY` wake that
    used to sit beside it could only make an already-correct read arrive sooner, and it cost an
    autocommit session, a channel-name regex and a `pg_notify` on all three append paths.
    """

    reader_conn = connect_postgres_test(read_only=False)
    writer = connect_postgres_test(read_only=False)
    try:
        reader_repos = repositories_for_connection(reader_conn)
        writer_repo = TradingRepository(writer)
        _control_row(writer_repo)
        client = ExecutionSignalClient(account_slot=_ACCOUNT_SLOT, execution_strategy="oi_nautilus_v1")
        reader = partial(load_unresolved_trade_signals, reader_repos)
        command_reader = partial(load_unresolved_operator_intents, reader_repos)

        assert client.poll_once(reader) == 0
        _append_signal(writer_repo)
        # No notification was sent and none is listened for; the very next poll finds the Signal.
        assert client.poll_once(reader) == 1
        assert client.next_nowait() is not None
        # And it stays consumed: an unresolved read is idempotent, so a repeat poll admits nothing.
        assert client.poll_once(reader) == 0
        command = _append_command(writer_repo, suffix="7", action="pause_entries")
        assert client.poll_commands_once(command_reader) == 1
        assert client.next_command_nowait() == command.value
    finally:
        reader_conn.close()
        writer.close()


def test_production_bridge_delivers_within_one_poll_interval_on_one_session() -> None:
    writer = connect_postgres_test(read_only=False)
    bridge: OiRuntimeDatabaseBridge | None = None
    try:
        repo = TradingRepository(writer)
        _control_row(repo)
        signals = ExecutionSignalClient(
            account_slot=_ACCOUNT_SLOT,
            execution_strategy="oi_nautilus_v1",
        )
        bridge = _runtime_bridge(signals)
        bridge.start()
        _wait_for_bridge(bridge, lambda: bridge.connected)

        latencies: list[float] = []
        for suffix in "123456":
            started = time.perf_counter()
            _append_signal(repo, suffix=suffix)
            _wait_for_bridge(bridge, lambda: signals.queued_count == 1)
            assert signals.next_nowait() is not None
            latencies.append(time.perf_counter() - started)

        p95_seconds = sorted(latencies)[math.ceil(0.95 * len(latencies)) - 1]
        # One poll interval (200 ms) plus one cycle, and far inside the 60-second Signal TTL. It was
        # a `NOTIFY` wake with the same poll behind it as repair (#537 PR-4).
        assert p95_seconds <= 0.6
        row = writer.execute(
            """
            SELECT count(*) AS n
              FROM pg_stat_activity
             WHERE datname = current_database()
               AND application_name = 'tracefold_nautilus_stream'
            """
        ).fetchone()
        assert row == {"n": 1}
    finally:
        if bridge is not None:
            _stop_bridge(bridge)
        writer.close()


def test_production_bridge_100_pair_burst_is_bounded_and_repeat_polls_do_not_duplicate_pending() -> None:
    writer = connect_postgres_test(read_only=False)
    bridge: OiRuntimeDatabaseBridge | None = None
    try:
        repo = TradingRepository(writer)
        _control_row(repo)
        signals = ExecutionSignalClient(
            account_slot=_ACCOUNT_SLOT,
            execution_strategy="oi_nautilus_v1",
        )
        bridge = _runtime_bridge(signals)
        bridge.start()
        _wait_for_bridge(bridge, lambda: bridge.connected)

        _append_input_burst(repo, size=100)
        _wait_for_bridge(bridge, lambda: signals.queued_count == 200)
        # Several more poll cycles over the same unresolved rows: the in-process pending set is what
        # keeps a redelivery from becoming a second queued input.
        time.sleep(1.0)

        assert signals.queued_count == 200
        assert signals.queued_command_count == 100
        assert signals.queued_bytes <= 1_048_576
        commands = tuple(signals.next_command_nowait() for _ in range(100))
        values = tuple(signals.next_nowait() for _ in range(100))
        assert None not in commands
        assert None not in values
        assert len({command.command_id for command in commands if command is not None}) == 100
        assert len({value.signal_id for value in values if value is not None}) == 100
        assert signals.queued_count == 0
        assert signals.queued_bytes == 0
        assert len(signals.pending_command_ids) == 100
        assert len(signals.pending_ids) == 100
    finally:
        if bridge is not None:
            _stop_bridge(bridge)
        writer.close()


def test_production_bridge_reconnect_resumes_consuming_on_a_new_session() -> None:
    writer = connect_postgres_test(read_only=False)
    bridge: OiRuntimeDatabaseBridge | None = None
    try:
        repo = TradingRepository(writer)
        _control_row(repo)
        signals = ExecutionSignalClient(
            account_slot=_ACCOUNT_SLOT,
            execution_strategy="oi_nautilus_v1",
        )
        bridge = _runtime_bridge(signals)
        bridge.start()
        _wait_for_bridge(bridge, lambda: bridge.connected)
        row = writer.execute(
            """
            SELECT pg_terminate_backend(pid) AS terminated
              FROM pg_stat_activity
             WHERE datname = current_database()
               AND application_name = 'tracefold_nautilus_stream'
            """
        ).fetchone()
        assert row == {"terminated": True}

        _wait_for_bridge(bridge, lambda: not bridge.connected)
        _wait_for_bridge(bridge, lambda: bridge.connected)
        _append_signal(repo, suffix="8")
        _wait_for_bridge(bridge, lambda: signals.queued_count == 1)
        assert signals.next_nowait() is not None
    finally:
        if bridge is not None:
            _stop_bridge(bridge)
        writer.close()


def test_restart_control_state_reads_current_pause_resume_and_sticky_halt_projection() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        repo = TradingRepository(conn)
        _control_row(repo)
        factory = ObservationFactory(_ACCOUNT_SLOT, "oi_nautilus_v1")
        actions = ("pause_entries", "resume_entries", "emergency_halt")
        for suffix, action in zip(("4", "5", "6"), actions, strict=True):
            prepared = _append_command(repo, suffix=suffix, action=action)
            observation = factory.create(
                normalized_kind="control_disposition",
                command_id=prepared.value.command_id,
                occurred_at_ns=NOW_NS,
                observed_at_ns=NOW_NS,
                summary={"action": action, "disposition": "accepted", "reason": "test"},
                payload={"action": action, "disposition": "accepted"},
                event_identity="final",
            )
            with conn.transaction():
                repo.append_execution_observations(prepare_execution_observations((observation,)))

        state = load_runtime_control_state(repositories_for_connection(conn), _ACCOUNT_SLOT, now_ns=NOW_NS)

        assert state.entries_paused is True
        assert state.emergency_halted is True
        assert state.flatten_pending == ()
    finally:
        conn.close()


def test_current_control_projection_is_idempotent_and_never_regresses_on_out_of_order_observations() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        repo = TradingRepository(conn)
        _control_row(repo)
        factory = ObservationFactory(_ACCOUNT_SLOT, "oi_nautilus_v1")
        resume = _append_command(repo, suffix="7", action="resume_entries")
        pause = _append_command(repo, suffix="8", action="pause_entries")

        def accepted(prepared: PreparedOperatorIntent, action: str, *, observed_at_ns: int):
            return factory.create(
                normalized_kind="control_disposition",
                command_id=prepared.value.command_id,
                occurred_at_ns=observed_at_ns,
                observed_at_ns=observed_at_ns,
                summary={"action": action, "disposition": "accepted", "reason": "test"},
                payload={"action": action, "disposition": "accepted"},
                event_identity="accepted",
            )

        pause_observation = accepted(pause, "pause_entries", observed_at_ns=NOW_NS + 2)
        with conn.transaction():
            repo.append_execution_observations(prepare_execution_observations((pause_observation,)))
        after_pause = repo.execution_runtime_control_state(_ACCOUNT_SLOT)
        assert after_pause is not None
        assert after_pause.entries_paused is True
        assert after_pause.last_command_seq == 2

        with conn.transaction():
            repo.append_execution_observations(
                prepare_execution_observations((accepted(resume, "resume_entries", observed_at_ns=NOW_NS + 1),))
            )
        assert repo.execution_runtime_control_state(_ACCOUNT_SLOT) == after_pause

        with conn.transaction():
            repo.append_execution_observations(prepare_execution_observations((pause_observation,)))
        assert repo.execution_runtime_control_state(_ACCOUNT_SLOT) == after_pause

        halt = _append_command(repo, suffix="9", action="emergency_halt")
        with conn.transaction():
            repo.append_execution_observations(
                prepare_execution_observations((accepted(halt, "emergency_halt", observed_at_ns=NOW_NS + 3),))
            )
        halted = repo.execution_runtime_control_state(_ACCOUNT_SLOT)
        assert halted is not None
        assert halted.entries_paused is True
        assert halted.emergency_halted is True
        assert halted.last_command_seq == 3

        impossible_resume = _append_command(repo, suffix="a", action="resume_entries")
        with conn.transaction():
            repo.append_execution_observations(
                prepare_execution_observations(
                    (accepted(impossible_resume, "resume_entries", observed_at_ns=NOW_NS + 4),)
                )
            )
        still_halted = repo.execution_runtime_control_state(_ACCOUNT_SLOT)
        assert still_halted is not None
        assert still_halted.entries_paused is True
        assert still_halted.emergency_halted is True
        assert still_halted.last_command_seq == 4
    finally:
        conn.close()


def test_a_slot_with_no_commands_starts_armed_and_a_restart_reads_back_the_same_row() -> None:
    """#520 PR-A. Control belongs to the slot: only a Command pauses it, and nothing re-pauses it.

    Every new `profile_id` used to insert `entries_paused = TRUE`, so a deploy silently disarmed
    entries and needed another authenticated `/resume`. `mode: disabled` is the switch that
    means "do not trade"; a restart is not one.
    """

    conn = connect_postgres_test(read_only=False)
    try:
        repos = repositories_for_connection(conn)
        unpaused = RuntimeControlSnapshot(entries_paused=False, emergency_halted=False, flatten_pending=())

        assert load_runtime_control_state(repos, _ACCOUNT_SLOT, now_ns=NOW_NS) == unpaused
        # The second start is the restart: the same row, not a fresh paused one.
        assert load_runtime_control_state(repos, _ACCOUNT_SLOT, now_ns=NOW_NS + 1_000) == unpaused

        row = TradingRepository(conn).execution_runtime_control_state(_ACCOUNT_SLOT)
        assert row is not None
        assert (row.entries_paused, row.emergency_halted, row.last_command_seq) == (False, False, 0)
        assert row.updated_at_ns == NOW_NS
    finally:
        conn.close()


def test_account_slot_lock_is_single_session_and_loss_fails_closed() -> None:
    first_conn = connect_postgres_test(read_only=False)
    second_conn = connect_postgres_test(read_only=False)
    first_repo = TradingRepository(first_conn)
    second_repo = TradingRepository(second_conn)
    first = AccountSlotSingleton(
        account_slot="binance_usdm_primary",
        try_acquire=first_repo.try_acquire_execution_account_slot,
        release=first_repo.release_execution_account_slot,
        heartbeat=lambda: bool(first_conn.execute("SELECT 1 AS alive").fetchone()["alive"]),
    )
    second = AccountSlotSingleton(
        account_slot="binance_usdm_primary",
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


def test_day_start_baseline_is_append_only_and_restart_reads_original_equity() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        repos = repositories_for_connection(conn)
        profile = oi_profile()
        factory = ObservationFactory(
            account_slot=profile.account_slot,
            execution_strategy="oi_nautilus_v1",
        )
        first = load_or_record_day_start(
            repos=repos,
            factory=factory,
            utc_day="2030-03-17",
            equity_usd=Decimal("1000.123456"),
            recorded_at_ns=NOW_NS,
        )
        restarted = load_or_record_day_start(
            repos=repos,
            factory=factory,
            utc_day="2030-03-17",
            equity_usd=Decimal("900"),
            recorded_at_ns=NOW_NS + 1,
        )

        assert restarted == first
        assert restarted.equity_usd == Decimal("1000.123456")
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM trading_execution_observations WHERE event_id = %s",
                (first.event_id,),
            ).fetchone()["n"]
            == 1
        )
    finally:
        conn.close()


def test_real_postgres_signal_to_pinned_nautilus_callback_to_observation_process_seam(
    postgres_clone_dsn: str,
) -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        repo = TradingRepository(conn)
        _control_row(repo)
        _append_signal(repo)
    finally:
        conn.close()

    result = subprocess.run(
        [sys.executable, "-m", "tests.helpers.nautilus_oi_runtime_process", postgres_clone_dsn],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout.strip().splitlines()[-1])
    assert receipt["admitted"] == 1
    assert receipt["pending"] == []
    assert receipt["flushed"] == 6
    assert receipt["open_position_quantity"] == "0.049"
    assert len(receipt["orders"]) == 2
    entry, protection = receipt["orders"]
    assert entry == {
        "client_order_id": entry["client_order_id"],
        "order_type": "MARKET",
        "quantity": "0.049",
        "reduce_only": False,
        "side": "BUY",
        "status": "FILLED",
    }
    assert protection == {
        "client_order_id": protection["client_order_id"],
        "order_type": "STOP_MARKET",
        "quantity": "0.049",
        "reduce_only": True,
        "side": "SELL",
        "status": "ACCEPTED",
    }
    assert entry["client_order_id"].startswith("tf")
    assert protection["client_order_id"].startswith("tf")

    verify = connect_postgres_test(read_only=False)
    try:
        rows = verify.execute(
            """
            SELECT normalized_kind, payload -> 'summary' ->> 'disposition' AS disposition
              FROM trading_execution_observations
             WHERE account_slot = 'binance_usdm_primary'
             ORDER BY seq
            """
        ).fetchall()
        assert rows[0] == {"normalized_kind": "order", "disposition": None}
        assert {row["normalized_kind"] for row in rows} >= {
            "fill",
            "order",
            "position",
            "protection",
            "signal_disposition",
        }
        assert [row["disposition"] for row in rows if row["normalized_kind"] == "signal_disposition"] == ["accepted"]
    finally:
        verify.close()


def test_replayed_database_signal_reaches_one_economic_entry_in_pinned_nautilus_process(
    postgres_clone_dsn: str,
) -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        repo = TradingRepository(conn)
        _control_row(repo)
        _append_signal(repo)
    finally:
        conn.close()

    result = subprocess.run(
        [sys.executable, "-m", "tests.helpers.nautilus_oi_runtime_process", postgres_clone_dsn, "signal_replay"],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout.strip().splitlines()[-1])
    assert receipt["admitted"] == 1
    assert receipt["replay_admitted"] == 0
    assert receipt["pending"] == []
    # #510 PR-5b. One admitted entry, one quote stream: `on_start` subscribes nothing, and this
    # profile's catalogue is what the admission opened a stream out of.
    assert receipt["quote_subscriptions"] == 1
    economic_entries = [order for order in receipt["orders"] if not order["reduce_only"]]
    active_protections = [order for order in receipt["orders"] if order["reduce_only"]]
    assert len(economic_entries) == 1
    assert len(active_protections) == 1


def test_authenticated_cli_to_postgres_to_nautilus_command_observation_process_seam(
    postgres_clone_dsn: str,
) -> None:
    """`tracefold trading issue` is the one manual ingress since #528 deleted the Telegram webhook."""

    prepared = prepare_parsed_operator_intent(
        parse_operator_command("/pause process-seam"),
        source="cli:uid:0:host:process-seam",
        source_command_id="00000000-0000-4000-8000-000000000433",
        account_slot=_ACCOUNT_SLOT,
        operator_identity="local-cli:0",
        authentication_identity="local-os-uid:0",
        requested_at_ns=NOW_NS,
    )
    conn = connect_postgres_test(read_only=False)
    try:
        repo = TradingRepository(conn)
        _control_row(repo)
        with conn.transaction():
            receipt = persist_operator_intent(repo, prepared)
        assert receipt.disposition == "awaiting_runtime"
    finally:
        conn.close()

    result = subprocess.run(
        [sys.executable, "-m", "tests.helpers.nautilus_oi_runtime_process", postgres_clone_dsn, "command"],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    process_receipt = json.loads(result.stdout.strip().splitlines()[-1])
    assert process_receipt == {
        "admitted": 0,
        "admitted_commands": 1,
        "control": {"emergency_halted": False, "entries_paused": True, "flatten_pending": []},
        "execution_safe": True,
        "flushed": 1,
        "open_position_quantity": None,
        "orders": [],
        "pending": [],
        "pending_commands": [],
        "positions_count": 0,
        "protection_status": "not_applicable",
        # #510 PR-5b: a control Command opens no market-data stream, and neither does `on_start` -
        # before this change it subscribed every route in the catalogue at startup.
        "quote_subscribe_calls": 0,
        "quote_subscriptions": 0,
        "quote_unsubscribe_calls": 0,
        "recovered": True,
        "recovered_seeds": 0,
        "recovery_signals": 0,
        "route_catalogue": 1,
        "unexpected_exposure": False,
    }

    verify = connect_postgres_test(read_only=False)
    try:
        row = verify.execute(
            """
            SELECT normalized_kind, summary
              FROM trading_execution_observations
             WHERE command_id = %s
            """,
            (prepared.value.command_id,),
        ).fetchone()
        assert row == {
            "normalized_kind": "control_disposition",
            "summary": {
                "action": "pause_entries",
                "disposition": "accepted",
                "reason": "entries_paused",
            },
        }
    finally:
        verify.close()


def test_cold_cache_restart_reclaims_position_and_stop_from_durable_entry_facts(
    postgres_clone_dsn: str,
) -> None:
    """#510 PR-3. Nautilus Cache is process memory and Binance never reports a filled entry."""

    conn = connect_postgres_test(read_only=False)
    try:
        repo = TradingRepository(conn)
        _control_row(repo)
        _append_signal(repo)
        _append_entry_order_fact(repo, signal_id="1" * 64)
    finally:
        conn.close()

    result = subprocess.run(
        [sys.executable, "-m", "tests.helpers.nautilus_oi_runtime_process", postgres_clone_dsn, "cold_recovery"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout.strip().splitlines()[-1])

    assert receipt["recovery_signals"] == 1, receipt
    assert receipt["recovered_seeds"] == 1, receipt
    assert receipt["recovered"] is True, receipt
    assert receipt["unexpected_exposure"] is False, receipt
    assert receipt["execution_safe"] is True
    assert receipt["positions_count"] == 1
    assert receipt["protection_status"] == "protected"
    assert receipt["open_position_quantity"] == "0.049"
    # #510 PR-5b. A position reclaimed at startup still needs a mark, so recovery opens its stream.
    assert receipt["quote_subscriptions"] == 1
    economic_entries = [
        order for order in receipt["orders"] if not order["reduce_only"] and order["client_order_id"].startswith("tf")
    ]
    protections = [order for order in receipt["orders"] if order["order_type"] == "STOP_MARKET"]
    assert economic_entries == []
    assert len(protections) == 1

    verify = connect_postgres_test(read_only=False)
    try:
        dispositions = verify.execute(
            """
            SELECT summary ->> 'disposition' AS disposition
              FROM trading_execution_observations
             WHERE account_slot = 'binance_usdm_primary'
               AND normalized_kind = 'signal_disposition'
            """
        ).fetchall()
        assert [row["disposition"] for row in dispositions] == ["recovered"]
    finally:
        verify.close()


def test_rolling_restart_after_an_identity_change_keeps_control_state_and_needs_no_flat(
    postgres_clone_dsn: str,
) -> None:
    """#520 PR-A. A deploy that changes the release or the risk config is a restart, not a new identity.

    On 2026-09-03 this was 58 crash loops: the configuration digest moved, `_preflight_profile` refused with
    `oi_runtime_profile_identity_changed`, and the only way out was a new `profile_id`, a flat account
    and a fresh `/resume`. The account slot is the identity, so the same slot restarts into whatever
    control state the operator last set, while holding a position.
    """

    conn = connect_postgres_test(read_only=False)
    try:
        repo = TradingRepository(conn)
        _control_row(repo)
        _append_signal(repo)
        _append_entry_order_fact(repo, signal_id="1" * 64)
        # The operator resumed entries on the previous generation; a restart must not undo that.
        resume = _append_command(repo, suffix="7", action="resume_entries")
        factory = ObservationFactory(_ACCOUNT_SLOT, "oi_nautilus_v1")
        with conn.transaction():
            repo.append_execution_observations(
                prepare_execution_observations(
                    (
                        factory.create(
                            normalized_kind="control_disposition",
                            command_id=resume.value.command_id,
                            occurred_at_ns=NOW_NS,
                            observed_at_ns=NOW_NS,
                            summary={
                                "action": "resume_entries",
                                "disposition": "accepted",
                                "reason": "entries_resumed",
                            },
                            payload={"action": "resume_entries", "disposition": "accepted"},
                            event_identity="resume",
                        ),
                    )
                )
            )
    finally:
        conn.close()

    result = subprocess.run(
        [sys.executable, "-m", "tests.helpers.nautilus_oi_runtime_process", postgres_clone_dsn, "rolling_restart"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout.strip().splitlines()[-1])

    assert receipt["execution_safe"] is True, receipt
    assert receipt["unexpected_exposure"] is False, receipt
    # The account was holding a position across the restart and nothing asked it to be flat.
    assert receipt["positions_count"] == 1, receipt
    assert receipt["recovered"] is True, receipt
    assert receipt["control"] == {
        "entries_paused": False,
        "emergency_halted": False,
        "flatten_pending": [],
    }, receipt


def test_position_without_durable_entry_facts_halts_and_flatten_account_closes_it(
    postgres_clone_dsn: str,
) -> None:
    """#510 PR-3. Ownership only constrains new exposure; flatten converges the whole slot."""

    conn = connect_postgres_test(read_only=False)
    try:
        repo = TradingRepository(conn)
        _control_row(repo)
        _append_command(repo, suffix="b", action="flatten")
    finally:
        conn.close()

    result = subprocess.run(
        [sys.executable, "-m", "tests.helpers.nautilus_oi_runtime_process", postgres_clone_dsn, "cold_unclaimed"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout.strip().splitlines()[-1])

    assert receipt["recovery_signals"] == 0, receipt
    assert receipt["recovered_seeds"] == 0, receipt
    assert receipt["recovered"] is False, receipt
    assert receipt["unexpected_exposure"] is True, receipt
    assert receipt["execution_safe"] is False, receipt
    assert receipt["admitted_commands"] == 1, receipt
    closes = [
        order
        for order in receipt["orders"]
        if order["reduce_only"] and order["order_type"] == "MARKET" and not order["client_order_id"].startswith("tf")
    ]
    assert len(closes) == 1, receipt
    assert closes[0]["side"] == "SELL"
    assert closes[0]["quantity"] == "0.049"
    assert closes[0]["status"] == "FILLED"
    assert receipt["positions_count"] == 0, receipt
    assert receipt["open_position_quantity"] is None, receipt

    verify = connect_postgres_test(read_only=False)
    try:
        rows = verify.execute(
            """
            SELECT summary
              FROM trading_execution_observations
             WHERE account_slot = 'binance_usdm_primary'
               AND normalized_kind = 'order'
            """
        ).fetchall()
        assert [row["summary"]["leg"] for row in rows] == ["unclaimed_flatten"]
        assert rows[0]["summary"]["side"] == "long"
        assert rows[0]["summary"]["quantity"] == "0.049"
        # #528 A. The close of exposure this Runtime does not own is still a position fact, under the
        # Command that asked for it; without it a `/flatten account` left no record of what it closed.
        closed = verify.execute(
            """
            SELECT command_id, summary
              FROM trading_execution_observations
             WHERE account_slot = 'binance_usdm_primary'
               AND normalized_kind = 'position'
            """
        ).fetchall()
        assert [row["summary"]["exit_reason"] for row in closed] == ["unclaimed_flatten"]
        assert closed[0]["summary"]["status"] == "closed"
        assert closed[0]["summary"]["quantity"] == "0.049"
        assert closed[0]["summary"]["exit_price"] is not None
        assert closed[0]["command_id"] == "b" * 64
    finally:
        verify.close()


def test_stopped_out_identity_does_not_reclaim_a_new_position_on_the_same_route(
    postgres_clone_dsn: str,
) -> None:
    """#510 PR-3. `_matched_position` claims by instrument and direction, so the read must retire
    an identity whose own position fact says closed; otherwise a hand-opened position on the same
    route would be adopted as Runtime-owned instead of halting."""

    conn = connect_postgres_test(read_only=False)
    try:
        repo = TradingRepository(conn)
        _control_row(repo)
        _append_signal(repo)
        _append_entry_order_fact(repo, signal_id="1" * 64)
        _append_closed_position_fact(repo, signal_id="1" * 64)
        _append_command(repo, suffix="b", action="flatten")
    finally:
        conn.close()

    result = subprocess.run(
        [sys.executable, "-m", "tests.helpers.nautilus_oi_runtime_process", postgres_clone_dsn, "cold_unclaimed"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout.strip().splitlines()[-1])

    assert receipt["recovery_signals"] == 0, receipt
    assert receipt["recovered_seeds"] == 0, receipt
    assert receipt["recovered"] is False, receipt
    assert receipt["unexpected_exposure"] is True, receipt
    assert receipt["execution_safe"] is False, receipt
    closes = [
        order
        for order in receipt["orders"]
        if order["reduce_only"] and order["order_type"] == "MARKET" and not order["client_order_id"].startswith("tf")
    ]
    assert len(closes) == 1, receipt
    assert closes[0]["status"] == "FILLED"
    assert receipt["positions_count"] == 0, receipt


def test_the_bridge_thread_owns_the_projection_write_and_the_account_slot_heartbeat() -> None:
    """#510 PR-5b. Real PostgreSQL: the trading event loop keeps no session of its own.

    Production ran the generation-fenced projection write, the durable recovery read and the
    account-slot heartbeat synchronously on the trading event loop, over a third connection, every
    500 ms, on the same thread as every Nautilus order callback and with no statement timeout
    (#510 E). This starts only the bridge and proves the row still moves.
    """

    writer = connect_postgres_test(read_only=False)
    lock_conn = connect_postgres_test(read_only=False)
    bridge: OiRuntimeDatabaseBridge | None = None
    try:
        repo = TradingRepository(writer)
        _control_row(repo)
        lock_repo = TradingRepository(lock_conn)
        singleton = AccountSlotSingleton(
            account_slot="binance_usdm_primary",
            try_acquire=lock_repo.try_acquire_execution_account_slot,
            release=lock_repo.release_execution_account_slot,
            heartbeat=lambda: bool(lock_conn.execute("SELECT 1 AS alive").fetchone()["alive"]),
        )
        assert singleton.acquire() is True

        projector = _bridge_projector()
        # The composition root's startup session inserts the row this generation owns.
        projector.start(repositories_for_connection(writer))
        profile = oi_profile()
        signals = ExecutionSignalClient(
            account_slot=profile.account_slot,
            execution_strategy="oi_nautilus_v1",
        )
        bridge = OiRuntimeDatabaseBridge(
            settings=Settings(ws_token="510-pr5b", storage=postgres_settings_storage()),
            profile=profile,
            signals=signals,
            audit=AuditSink(factory=ObservationFactory(profile.account_slot, "oi_nautilus_v1")),
            update_day_start=lambda _baseline: None,
            singleton=singleton,
            projector=projector,
        )
        bridge.start()
        _wait_for_bridge(bridge, lambda: bridge.connected)

        started = projector.current
        running = replace(
            started,
            entry_block_reason="reconciliation_stale",
            heartbeat_at_ns=started.heartbeat_at_ns + 1,
            updated_at_ns=started.updated_at_ns + 1,
        )
        projector.offer(running)
        _wait_for_bridge(bridge, lambda: projector.current == running)

        row = writer.execute(
            """
            SELECT entry_block_reason
              FROM trading_execution_runtime_state
             WHERE account_slot = 'binance_usdm_primary'
            """
        ).fetchone()
        assert row == {"entry_block_reason": "reconciliation_stale"}
        sessions = writer.execute(
            """
            SELECT application_name, count(*) AS n
              FROM pg_stat_activity
             WHERE datname = current_database()
               AND application_name LIKE 'tracefold_nautilus%'
             GROUP BY application_name
            """
        ).fetchall()
        assert sessions == [{"application_name": "tracefold_nautilus_stream", "n": 1}]

        # The heartbeat that notices a dead lock session also runs on this thread.
        lock_conn.close()
        _wait_for_bridge(bridge, lambda: singleton.acquired is False)
    finally:
        if bridge is not None:
            _stop_bridge(bridge)
        if not lock_conn.closed:
            lock_conn.close()
        writer.close()
