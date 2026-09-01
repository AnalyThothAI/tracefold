"""Real PostgreSQL and pinned Nautilus process seam for #433-B."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from decimal import Decimal
from functools import partial

import pytest

from tests.nautilus_oi_runtime_fixtures import NOW_NS, oi_profile
from tests.postgres_test_utils import connect_postgres_test
from tracefold.app.nautilus.oi_runtime import (
    execution_stream_channel,
    load_or_record_day_start,
    load_runtime_control_state,
    load_unresolved_operator_intents,
    load_unresolved_trade_signals,
)
from tracefold.app.operator_control import persist_operator_intent
from tracefold.app.repository_session import repositories_for_connection
from tracefold.integrations.nautilus.oi_runtime.audit_sink import ObservationFactory
from tracefold.integrations.nautilus.oi_runtime.signal_client import (
    ExecutionSignalClient,
    install_execution_stream_listener,
    wait_for_execution_stream_wake,
)
from tracefold.integrations.nautilus.oi_runtime.singleton import AccountSlotSingleton
from tracefold.integrations.telegram_control import TelegramControlWebhook
from tracefold.trading.storage.execution_stream import (
    ExecutionProfileActivation,
    PreparedOperatorIntent,
    prepare_execution_observations,
    prepare_operator_intent,
    prepare_trade_signal,
)
from tracefold.trading.storage.root import TradingRepository

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_clone_dsn")]


def _activate(repo: TradingRepository) -> None:
    with repo.conn.transaction():
        repo.append_execution_profile_activation(
            ExecutionProfileActivation(
                runtime_profile_id="oi-paper-profile",
                account_slot="binance_usdm_primary",
                activated_after_signal_seq=0,
                activated_after_command_seq=0,
                mode="paper",
                runtime_release="nautilus-1.231.0+oi-v1",
                config_sha256="a" * 64,
                created_at_ns=NOW_NS,
            )
        )


def _append_signal(repo: TradingRepository, *, suffix: str = "1") -> None:
    case_id = f"case-{suffix}"
    prepared = prepare_trade_signal(
        signal_id=suffix * 64,
        case_id=case_id,
        alpha_contract_sha256="2" * 64,
        market_key="crypto:perp:BTC:USDT",
        direction="long",
        observed_at_ns=NOW_NS - 1_000_000,
        expires_at_ns=NOW_NS + 60_000_000_000,
        evidence_sha256="3" * 64,
    )
    with repo.conn.transaction():
        repo.conn.execute(
            """
            INSERT INTO trading_cases (
              case_id, underlying_key, trigger_kind, primary_source_key,
              supplemental_source_keys, manifest, manifest_sha256, state,
              policy_decision, policy_reason, observed_at_ms, created_at_ms, decided_at_ms,
              updated_at_ms, strategy_id, strategy_version, strategy_config_digest,
              capital_disposition, capital_reason
            ) VALUES (
              %s, %s, 'news', %s, '[]'::jsonb, '{"test":"nautilus-runtime"}'::jsonb,
              %s, 'SIGNAL_EMITTED', 'long', 'nautilus_runtime_fixture', 1, 1, 1, 1,
              'nautilus_runtime_fixture', 'v1', %s, 'not_applicable', NULL
            )
            """,
            (case_id, f"runtime:{case_id}", f"runtime-source:{case_id}", "4" * 64, "5" * 64),
        )
        repo.append_trade_signal(prepared)


def _append_command(repo: TradingRepository, *, suffix: str, action: str) -> PreparedOperatorIntent:
    prepared = prepare_operator_intent(
        command_id=suffix * 64,
        target_profile_id="oi-paper-profile",
        action=action,
        scope="account" if action in {"emergency_halt", "flatten"} else "entries",
        reason="operator test",
        operator_identity="operator:test",
        authentication_identity="test:authenticated",
        requested_at_ns=NOW_NS,
        expires_at_ns=NOW_NS + 60_000_000_000,
        confirmation_identity="6" * 64 if action in {"resume_entries", "emergency_halt", "flatten"} else None,
        market_key=None,
        direction=None,
    )
    with repo.conn.transaction():
        repo.append_operator_intent(prepared)
    return prepared


def test_listen_is_wake_only_and_poll_repairs_before_and_after_notifications() -> None:
    listener = connect_postgres_test(read_only=False)
    writer = connect_postgres_test(read_only=False)
    try:
        listener_repos = repositories_for_connection(listener)
        writer_repo = TradingRepository(writer)
        _activate(writer_repo)
        client = ExecutionSignalClient(
            runtime_profile_id="oi-paper-profile",
            execution_strategy="oi_nautilus_v1",
        )
        reader = partial(load_unresolved_trade_signals, listener_repos)
        install_execution_stream_listener(listener, channel=execution_stream_channel())

        assert client.poll_once(reader) == 0
        _append_signal(writer_repo)
        assert wait_for_execution_stream_wake(listener, 2.0) is True
        assert client.poll_once(reader) == 1
        assert client.next_nowait() is not None
        # A timeout is still followed by the same correctness poll; no notification is required.
        assert wait_for_execution_stream_wake(listener, 0.01) is False
        assert client.poll_once(reader) == 0
        command = _append_command(writer_repo, suffix="7", action="pause_entries")
        command_reader = partial(load_unresolved_operator_intents, listener_repos)
        assert wait_for_execution_stream_wake(listener, 2.0) is True
        assert client.poll_commands_once(command_reader) == 1
        assert client.next_command_nowait() == command.value
    finally:
        listener.close()
        writer.close()


def test_restart_control_state_folds_durable_pause_resume_and_sticky_halt() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        repo = TradingRepository(conn)
        _activate(repo)
        factory = ObservationFactory("oi-paper-profile", "runtime-test", "oi_nautilus_v1")
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

        state = load_runtime_control_state(repositories_for_connection(conn), "oi-paper-profile")

        assert state.entries_paused is True
        assert state.emergency_halted is True
        assert state.flatten_pending == ()
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
        assert first.lost is True
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
            runtime_profile_id=profile.profile_id,
            runtime_release=profile.runtime_release,
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
        _activate(repo)
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
        "reduce_only": False,
        "status": "FILLED",
    }
    assert protection == {
        "client_order_id": protection["client_order_id"],
        "order_type": "STOP_MARKET",
        "reduce_only": True,
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
             WHERE runtime_profile_id = 'oi-paper-profile'
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


def test_authenticated_webhook_to_postgres_to_nautilus_command_observation_process_seam(
    postgres_clone_dsn: str,
) -> None:
    webhook = TelegramControlWebhook(
        webhook_secret="test-webhook-secret-433d",
        bot_id=1234567,
        allowed_chat_ids=frozenset({-433}),
        allowed_user_ids=frozenset({433}),
        target_profile_id="oi-paper-profile",
    )
    parsed = webhook.parse(
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-webhook-secret-433d"},
        body=json.dumps(
            {
                "update_id": 433_004,
                "message": {
                    "message_id": 4,
                    "date": NOW_NS // 1_000_000_000,
                    "chat": {"id": -433},
                    "from": {"id": 433},
                    "text": "/pause process-seam",
                },
            }
        ).encode(),
        received_at_ns=NOW_NS,
    )
    assert parsed.intent is not None
    conn = connect_postgres_test(read_only=False)
    try:
        repo = TradingRepository(conn)
        _activate(repo)
        with conn.transaction():
            receipt = persist_operator_intent(repo, parsed.intent)
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
        "flushed": 1,
        "open_position_quantity": None,
        "orders": [],
        "pending": [],
        "pending_commands": [],
    }

    verify = connect_postgres_test(read_only=False)
    try:
        row = verify.execute(
            """
            SELECT normalized_kind, summary
              FROM trading_execution_observations
             WHERE command_id = %s
            """,
            (parsed.intent.value.command_id,),
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
