"""Additive DDL retains in-flight responsibilities; unsafe rollback is refused."""

from __future__ import annotations

import time
from contextlib import closing
from decimal import Decimal

import pytest
from alembic import command
from sqlalchemy.exc import DBAPIError

from tests.integration.test_p0_executor_pending import SLOT, intent, seed_case, signal
from tests.postgres_test_utils import connect_postgres_test, postgres_migration_test_dsn
from tracefold.platform.postgres.migrations import alembic_config
from tracefold.trading.storage.executor import ExecutorStorage

pytestmark = [pytest.mark.integration, pytest.mark.migration]


def test_upgrade_preserves_active_unknown_and_controls_and_refuses_unsafe_downgrade(postgres_migration_dsn):
    config = alembic_config()
    config.attributes["database_url"] = postgres_migration_test_dsn(postgres_migration_dsn)
    command.upgrade(config, "20261002_0429")
    now = time.time_ns()
    with closing(connect_postgres_test()) as conn:
        seed_case(conn)
        db = ExecutorStorage(conn)
        with conn.transaction():
            db.append_operator_intent(intent("b", "flatten", now))
            db.apply_control(account_slot=SLOT, action="flatten", command_id="b" * 64, now_ns=now)
            db.append_signal(signal("a", now))
            # Historical 0429 fixture uses its actual column contract.
            conn.execute(
                "UPDATE trading_entries SET quantity=1,reference_price=100,reserved_notional=100,"
                "stop_bps=100,tp_bps=200,max_hold_s=3600,state='accepted',pnl_status='pending',"
                "reason='accepted',disposed_at_ns=%s,updated_at_ns=%s WHERE entry_id=%s",
                (now, now, "a" * 64),
            )
            db.reserve_order(
                client_id="migration-entry",
                entry_id="a" * 64,
                native_symbol="BTCUSDT",
                leg="entry",
                attempt=1,
                now_ns=now,
            )
            db.update_order(client_id="migration-entry", status="unknown", now_ns=now)
        before = db.entry("a" * 64)
        command.upgrade(config, "head")
        after = db.entry("a" * 64)
        assert all(after[key] == value for key, value in before.items())
        assert after["admission"] is None and after["reserved_margin_usdt"] is None
        assert db.entry_orders("a" * 64)[0]["status"] == "unknown"
        assert db.control(SLOT)["flatten_command_id"] == "b" * 64 and db.control(SLOT)["entries_paused"]
        with conn.transaction():
            db.record_fault(
                account_slot=SLOT,
                responsibility="a" * 64,
                code="order_resolution_unknown",
                symbol="BTCUSDT",
                client_ids=["migration-entry"],
                amount=Decimal(1),
                now_ns=now,
            )
        with pytest.raises(DBAPIError, match="requires forward repair"):
            command.downgrade(config, "20261002_0429")
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"] == "20261003_0430"
        assert db.control(SLOT)["execution_faults"]["a" * 64]["code"] == "order_resolution_unknown"
        assert db.entry_orders("a" * 64)[0]["status"] == "unknown"
