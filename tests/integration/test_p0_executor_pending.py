"""Late commits remain pending; venue failures and stale resumes cannot lose control."""

from __future__ import annotations

import asyncio
import time
from contextlib import closing
from decimal import Decimal

import httpx
import pytest

from tests.postgres_test_utils import connect_postgres_test
from tracefold.app.executor import ExecutorRunner
from tracefold.platform.config.models import Settings
from tracefold.trading.executor.core import SignalV4
from tracefold.trading.operator_control import prepare_operator_intent
from tracefold.trading.storage.executor import ExecutorStorage

pytestmark = pytest.mark.integration
SLOT = "p0-demo"


def intent(key: str, action: str, now: int):
    return prepare_operator_intent(
        command_id=key * 64,
        account_slot=SLOT,
        action=action,
        scope="account",
        reason="test",
        operator_identity="test",
        authentication_identity="test",
        requested_at_ns=now,
        expires_at_ns=now + 60_000_000_000,
    )


def signal(key: str, now: int) -> SignalV4:
    return SignalV4(
        signal_id=key * 64,
        decision_id=key * 64,
        case_id="c" * 64,
        account_slot=SLOT,
        entry_scope_id=key * 64,
        asset_id="crypto:BTC",
        native_symbol="BTCUSDT",
        mapping_semantics_digest="0" * 64,
        side="long",
        reference_price=Decimal("100"),
        max_drift_bps=100,
        stop_bps=100,
        tp_bps=200,
        max_hold_s=3600,
        policy_id="test",
        policy_version="v1",
        geometry_version="v1",
        decided_at_ns=now,
        expires_at_ns=now + 60_000_000_000,
    )


def seed_case(conn) -> None:
    conn.execute(
        """INSERT INTO trading_inputs(input_id,kind,source_fact_key,source_revision,payload_sha256,payload,
             first_visible_at_ms,source_observed_at_ms,selected_asset_id,target_selection,received_at_ms)
           VALUES (%s,'oi','p0-oi','v1',%s,'{}',1,1,'crypto:BTC','{}',1)""",
        ("c" * 64, "0" * 64),
    )
    conn.execute(
        """INSERT INTO trading_cases(case_id,trigger_id,trigger_kind,asset_id,native_symbol,mapping_digest,
             created_at_ms,root_expires_at_ms,state,updated_at_ms)
           VALUES (%s,%s,'oi','crypto:BTC','BTCUSDT','test',1,%s,'pending',1)""",
        ("c" * 64, "c" * 64, time.time_ns() // 1_000_000 + 3_600_000),
    )
    ExecutorStorage(conn).ensure_account(SLOT)
    conn.commit()


@pytest.mark.parametrize("kind", ["signal", "intent"])
def test_late_commit_is_consumed_after_higher_sequence_is_disposed(postgres_clone_dsn, kind: str) -> None:
    now = time.time_ns()
    with closing(connect_postgres_test()) as late, closing(connect_postgres_test()) as consumer:
        seed_case(consumer)
        late.execute("BEGIN")
        first, second = ExecutorStorage(late), ExecutorStorage(consumer)
        if kind == "signal":
            first.append_signal(signal("a", now))
            second.append_signal(signal("b", now))
            consumer.commit()
            assert second.next_signal(account_slot=SLOT).signal_id == "b" * 64
        else:
            first.append_operator_intent(intent("a", "pause_entries", now))
            second.append_operator_intent(intent("b", "pause_entries", now))
            consumer.commit()
            assert second.next_intent(account_slot=SLOT)["command_id"] == "b" * 64
        second.record_disposition(
            kind=kind,
            input_id="b" * 64,
            account_slot=SLOT,
            disposition="refused",
            reason="test",
            now_ns=now,
        )
        consumer.commit()
        late.commit()
        if kind == "signal":
            assert second.next_signal(account_slot=SLOT).signal_id == "a" * 64
        else:
            assert second.next_intent(account_slot=SLOT)["command_id"] == "a" * 64


@pytest.mark.parametrize("stop", ["pause_entries", "emergency_halt", "flatten"])
def test_late_resume_cannot_reverse_newer_accepted_stop(postgres_clone_dsn, stop: str) -> None:
    now = time.time_ns()
    with closing(connect_postgres_test()) as late, closing(connect_postgres_test()) as conn:
        late.execute("BEGIN")
        ExecutorStorage(late).append_operator_intent(intent("a", "resume_entries", now))
        db = ExecutorStorage(conn)
        db.ensure_account(SLOT)
        db.append_operator_intent(intent("b", stop, now))
        conn.commit()
        runner = ExecutorRunner(
            settings=Settings(trading={"execution": {"binance": {"environment": "DEMO"}}}), conn=conn, venue=object()
        )
        runner.account_slot = SLOT
        asyncio.run(runner._one_intent(now))
        late.commit()
        asyncio.run(runner._one_intent(now))
        result = db.disposition(kind="intent", input_id="a" * 64)
        assert (result["disposition"], result["reason"]) == ("refused", "superseded")
        assert db.control(SLOT)["entries_paused"] is True
        assert db.control(SLOT)["emergency_halted"] is (stop == "emergency_halt")


def test_unavailable_venue_leaves_signal_pending(postgres_clone_dsn) -> None:
    class UnavailableVenue:
        def __getattr__(self, _name):
            async def unavailable(*_args):
                raise httpx.ConnectError("unavailable")

            return unavailable

    now = time.time_ns()
    with closing(connect_postgres_test()) as conn:
        seed_case(conn)
        db = ExecutorStorage(conn)
        db.ensure_account(SLOT)
        db.append_signal(signal("a", now))
        conn.commit()
        runner = ExecutorRunner(
            settings=Settings(trading={"execution": {"binance": {"environment": "DEMO"}}}),
            conn=conn,
            venue=UnavailableVenue(),
        )
        runner.account_slot = SLOT
        asyncio.run(runner._one_signal(now))
        assert db.disposition(kind="signal", input_id="a" * 64) is None
        assert db.next_signal(account_slot=SLOT).signal_id == "a" * 64


def test_identical_order_evidence_does_not_rewrite_xmin(postgres_clone_dsn) -> None:
    now = time.time_ns()
    with closing(connect_postgres_test()) as conn:
        db = ExecutorStorage(conn)
        db.append_operator_intent(intent("a", "flatten", now))
        db.reserve_external_flatten(client_id="p0order", command_id="a" * 64, symbol="BTCUSDT", attempt=1, now_ns=now)
        db.update_order(
            client_id="p0order", status="filled", now_ns=now, venue_order_id="123", evidence={"status": "FILLED"}
        )
        conn.commit()
        before = conn.execute("SELECT xmin::text,updated_at_ns FROM trading_orders").fetchone()
        conn.commit()
        db.update_order(
            client_id="p0order", status="filled", now_ns=now + 1, venue_order_id="123", evidence={"status": "FILLED"}
        )
        conn.commit()
        assert conn.execute("SELECT xmin::text,updated_at_ns FROM trading_orders").fetchone() == before
        db.update_order(
            client_id="p0order", status="filled", now_ns=now + 2, evidence={"status": "FILLED", "new_evidence": True}
        )
        conn.commit()
        assert conn.execute("SELECT xmin::text FROM trading_orders").fetchone()["xmin"] != before["xmin"]


def test_resume_refuses_until_flatten_command_is_cleared(postgres_clone_dsn):
    now = time.time_ns()
    with closing(connect_postgres_test()) as conn:
        db = ExecutorStorage(conn)
        db.ensure_account(SLOT)
        db.append_operator_intent(intent("b", "flatten", now))
        conn.commit()
        runner = ExecutorRunner(
            settings=Settings(trading={"execution": {"binance": {"environment": "DEMO"}}}), conn=conn, venue=object()
        )
        runner.account_slot = SLOT
        asyncio.run(runner._one_intent(now))
        db.append_operator_intent(intent("c", "resume_entries", now + 1))
        conn.commit()
        asyncio.run(runner._one_intent(now + 1))
        result = db.disposition(kind="intent", input_id="c" * 64)
        assert (result["disposition"], result["reason"]) == ("refused", "flatten_in_progress")
        assert db.control(SLOT)["entries_paused"] is True
        db.clear_flatten(account_slot=SLOT)
        db.append_operator_intent(intent("d", "resume_entries", now + 3))
        conn.commit()
        asyncio.run(runner._one_intent(now + 3))
        assert db.control(SLOT)["entries_paused"] is False


def test_signal_publication_does_not_create_an_executor_account(postgres_clone_dsn):
    from psycopg.errors import ForeignKeyViolation

    with closing(connect_postgres_test()) as conn:
        seed_case(conn)
        conn.execute("DELETE FROM trading_accounts WHERE account_slot=%s", (SLOT,))
        conn.commit()
        with pytest.raises(ForeignKeyViolation), conn.transaction():
            ExecutorStorage(conn).append_signal(signal("a", time.time_ns()))
        assert conn.execute("SELECT count(*) AS n FROM trading_accounts").fetchone()["n"] == 0
