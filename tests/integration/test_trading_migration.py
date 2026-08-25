"""What the #209 upgrade does to orders that already exist, on a real PostgreSQL upgrade path.

The interesting rows are the ones the migration *cannot* answer for. An order that already opened a
position carries the budget that governed it — `promote_acknowledged` writes `must_close_at_ms` and
`position_opened_at_ms` in one statement, so their difference is a fact. An order that never opened
one does not, and nothing in the database says what `max_holding_seconds` was when it was approved.
Reading today's configuration for it is exactly the drift #209 exists to close, so it stays NULL and
the reconciler refuses to manage it.
"""

from __future__ import annotations

from typing import Any

import pytest
from alembic import command
from psycopg.errors import CheckViolation

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import (
    test_postgres_dsn as postgres_test_dsn,
)
from tracefold.platform.postgres.migrations import alembic_config

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_dsn")]

NOW = 1_900_000_000_000
BEFORE_SNAPSHOT = "20260825_0307"


def _upgrade(revision: str) -> None:
    config = alembic_config()
    config.attributes["database_url"] = postgres_test_dsn()
    command.upgrade(config, revision)


def _fresh_schema_at(revision: str) -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("GRANT ALL ON SCHEMA public TO public")
        conn.commit()
    finally:
        conn.close()
    _upgrade(revision)


def _seed_pre_snapshot_order(
    conn: Any,
    *,
    order_id: str,
    state: str,
    position_opened_at_ms: int | None,
    must_close_at_ms: int | None,
) -> None:
    conn.execute(
        """
        INSERT INTO trading_cases (
          case_id, underlying_key, case_kind, mode, primary_source_key, supplemental_source_keys,
          manifest, manifest_sha256, state, observed_at_ms, created_at_ms, updated_at_ms
        ) VALUES (%s, %s, 'oi_only', 'paper', %s, '[]'::jsonb, '{}'::jsonb, 'sha', 'ORDER_PREPARED', %s, %s, %s)
        """,
        (f"case-{order_id}", f"crypto:{order_id.upper()}", f"key-{order_id}", NOW, NOW, NOW),
    )
    conn.execute(
        """
        INSERT INTO trading_orders (
          order_id, case_id, underlying_key, exchange_id, provider_symbol, account_ref, mode, side,
          notional_usd, quantity, entry_reference, stop_price, payload, payload_sha256, state,
          position_opened_at_ms, must_close_at_ms, created_at_ms, updated_at_ms
        ) VALUES (%s, %s, %s, 'binance', 'DOGEUSDT', 'default', 'paper', 'buy', 50, 0.5, 100, 98,
                  '{}'::jsonb, %s, %s, %s, %s, %s, %s)
        """,
        (
            order_id,
            f"case-{order_id}",
            f"crypto:{order_id.upper()}",
            f"digest-{order_id}",
            state,
            position_opened_at_ms,
            must_close_at_ms,
            NOW,
            NOW,
        ),
    )


def test_0308_backfills_the_exit_policy_only_where_the_ledger_can_prove_it() -> None:
    conn: Any | None = None
    try:
        _fresh_schema_at(BEFORE_SNAPSHOT)
        conn = connect_postgres_test(read_only=False)
        # Open, with the pair that records the budget that actually governed it.
        _seed_pre_snapshot_order(
            conn,
            order_id="opened",
            state="OPEN",
            position_opened_at_ms=NOW,
            must_close_at_ms=NOW + 1_800_000,
        )
        # Acknowledged and never promoted: no deadline was ever written, so no budget is provable.
        _seed_pre_snapshot_order(
            conn, order_id="unopened", state="ACKNOWLEDGED", position_opened_at_ms=None, must_close_at_ms=None
        )
        # Terminal history. Its realised return is already durable and its inputs are of no further use.
        _seed_pre_snapshot_order(
            conn,
            order_id="closed",
            state="CLOSED",
            position_opened_at_ms=NOW,
            must_close_at_ms=NOW + 1_800_000,
        )
        conn.commit()
        conn.close()
        conn = None

        _upgrade("head")

        conn = connect_postgres_test(read_only=False)
        rows = {
            str(row["order_id"]): (row["max_holding_ms"], row["taker_fee_bps"])
            for row in conn.execute("SELECT order_id, max_holding_ms, taker_fee_bps FROM trading_orders").fetchall()
        }
        # The difference of the pair, not today's configuration.
        assert rows["opened"] == (1_800_000, 5)
        # The fee has never been a configuration key, so 5 is history rather than an assumption; the
        # holding budget is genuinely unknown and is left that way.
        assert rows["unopened"] == (None, 5)
        assert rows["closed"] == (None, None)
    finally:
        if conn is not None:
            conn.close()


def test_0308_refuses_an_unusable_snapshot_at_the_database_boundary() -> None:
    """The CHECKs carry the migration's own safety claim, so they are asserted rather than described.

    A zero or negative holding budget is a snapshot that silently disables the deadline — the exact
    drift the column exists to stop — and it must be impossible to record, not merely unlikely.
    """

    conn: Any | None = None
    try:
        _fresh_schema_at("head")
        conn = connect_postgres_test(read_only=False)
        _seed_pre_snapshot_order(
            conn, order_id="guard", state="OPEN", position_opened_at_ms=NOW, must_close_at_ms=NOW + 1
        )
        conn.commit()
        for column, value in (("max_holding_ms", 0), ("max_holding_ms", -1), ("taker_fee_bps", -1)):
            with pytest.raises(CheckViolation):
                conn.execute(f"UPDATE trading_orders SET {column} = %s WHERE order_id = 'guard'", (value,))
            conn.rollback()
    finally:
        if conn is not None:
            conn.close()
