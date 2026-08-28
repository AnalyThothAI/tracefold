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
from sqlalchemy.exc import DBAPIError

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import (
    test_postgres_dsn as postgres_test_dsn,
)
from tracefold.platform.postgres.migrations import alembic_config

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_dsn")]

NOW = 1_900_000_000_000
BEFORE_SNAPSHOT = "20260825_0307"
BEFORE_STRATEGY_KERNEL = "20260825_0309"
BEFORE_INTENT_HARD_CUT = "20260828_0316"


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
    strategy_schema = conn.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'trading_cases' AND column_name = 'trigger_kind'"
    ).fetchone()
    if strategy_schema is not None:
        conn.execute(
            """
            INSERT INTO trading_cases (
              case_id, underlying_key, trigger_kind, strategy_id, strategy_version,
              strategy_config_digest, mode, primary_source_key, supplemental_source_keys,
              manifest, manifest_sha256, state, observed_at_ms, created_at_ms, updated_at_ms
            ) VALUES (%s, %s, 'oi', 'oi_momentum_v1', 'oi_momentum_v1', %s, 'paper', %s,
                      '[]'::jsonb, '{}'::jsonb, 'sha', 'ORDER_PREPARED', %s, %s, %s)
            """,
            (f"case-{order_id}", f"crypto:{order_id.upper()}", "0" * 64, f"key-{order_id}", NOW, NOW, NOW),
        )
    else:
        conn.execute(
            """
            INSERT INTO trading_cases (
              case_id, underlying_key, case_kind, mode, primary_source_key, supplemental_source_keys,
              manifest, manifest_sha256, state, observed_at_ms, created_at_ms, updated_at_ms
            ) VALUES (%s, %s, 'oi_only', 'paper', %s, '[]'::jsonb, '{}'::jsonb, 'sha',
                      'ORDER_PREPARED', %s, %s, %s)
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

        _upgrade(BEFORE_INTENT_HARD_CUT)

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


def _seed_pre_index_case(conn: Any, *, case_id: str, underlying: str, observed_at_ms: int, state: str) -> None:
    conn.execute(
        """
        INSERT INTO trading_cases (
          case_id, underlying_key, case_kind, mode, primary_source_key, supplemental_source_keys,
          manifest, manifest_sha256, state, observed_at_ms, created_at_ms, updated_at_ms
        ) VALUES (%s, %s, 'oi_only', 'paper', %s, '[]'::jsonb, '{}'::jsonb, 'sha', %s, %s, %s, %s)
        """,
        (case_id, underlying, f"key-{case_id}", state, observed_at_ms, NOW, NOW),
    )


def test_0309_coalesces_undecided_cases_before_the_index_and_never_disowns_an_order() -> None:
    """The upgrade path the invariant has to survive, on a database that already has duplicates.

    Failing the migration on pre-existing duplicates would leave the invariant unenforced on exactly
    the databases that need it, so the coalescing is a total order rather than a filter — and a case
    that already authored an order outranks a newer one that did not, because `_place` commits the
    order before `_settle` terminalises the case and blocking that case would leave the ledger
    asserting no authorisation for a position reconciliation is still managing.
    """

    conn: Any | None = None
    try:
        _fresh_schema_at(BEFORE_SNAPSHOT)
        conn = connect_postgres_test(read_only=False)
        _seed_pre_index_case(conn, case_id="old", underlying="crypto:DOGE", observed_at_ms=NOW - 1_000, state="PENDING")
        _seed_pre_index_case(conn, case_id="new", underlying="crypto:DOGE", observed_at_ms=NOW, state="RUNNING")
        # A third for the same underlying, older than both, but it authored an order.
        _seed_pre_snapshot_order(
            conn, order_id="ordered", state="OPEN", position_opened_at_ms=NOW, must_close_at_ms=NOW + 1_800_000
        )
        conn.execute(
            "UPDATE trading_cases SET underlying_key = 'crypto:DOGE', state = 'RUNNING', observed_at_ms = %s "
            "WHERE case_id = 'case-ordered'",
            (NOW - 2_000,),
        )
        conn.execute("UPDATE trading_orders SET underlying_key = 'crypto:DOGE' WHERE order_id = 'ordered'")
        # A different underlying, untouched.
        _seed_pre_index_case(conn, case_id="other", underlying="crypto:SOL", observed_at_ms=NOW, state="PENDING")
        conn.commit()
        conn.close()
        conn = None

        _upgrade(BEFORE_INTENT_HARD_CUT)

        conn = connect_postgres_test(read_only=False)
        rows = {
            str(row["case_id"]): (str(row["state"]), row["policy_reason"], row["policy_decision"], row["decided_at_ms"])
            for row in conn.execute(
                "SELECT case_id, state, policy_reason, policy_decision, decided_at_ms FROM trading_cases"
            ).fetchall()
        }
        # The order-owning case survives even though it is the oldest of the three.
        assert rows["case-ordered"][0] == "RUNNING"
        assert rows["other"][0] == "PENDING"
        for blocked in ("old", "new"):
            state, reason, decision, decided = rows[blocked]
            assert state == "BLOCKED"
            # The reason the runner would have written anyway: this release retires every v2 manifest.
            assert reason == "news_generation_retired"
            # Nothing decided anything, so no fabricated decision and no fabricated decision instant —
            # the latter would feed `stage_latency_ms` as if it were a measurement.
            assert (decision, decided) == (None, None)

        # And the invariant now holds: a second undecided case for a busy underlying is refused.
        assert (
            conn.execute(
                "SELECT indexdef FROM pg_indexes WHERE indexname = 'ux_trading_case_in_flight_underlying'"
            ).fetchone()
            is not None
        )
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


def test_0310_hard_cuts_case_kind_and_adds_immutable_strategy_ledgers() -> None:
    conn: Any | None = None
    try:
        _fresh_schema_at(BEFORE_STRATEGY_KERNEL)
        conn = connect_postgres_test(read_only=False)
        _seed_pre_index_case(conn, case_id="legacy-oi", underlying="crypto:DOGE", observed_at_ms=NOW, state="PENDING")
        conn.commit()
        conn.close()
        conn = None

        _upgrade(BEFORE_INTENT_HARD_CUT)

        conn = connect_postgres_test(read_only=False)
        columns = {
            str(row["column_name"])
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'trading_cases'"
            ).fetchall()
        }
        assert "case_kind" not in columns
        assert {"trigger_kind", "strategy_id", "strategy_version", "strategy_config_digest"} <= columns

        row = conn.execute(
            "SELECT trigger_kind, strategy_id, strategy_version, strategy_config_digest "
            "FROM trading_cases WHERE case_id = 'legacy-oi'"
        ).fetchone()
        assert row is not None
        assert dict(row) == {
            "trigger_kind": "oi",
            "strategy_id": "oi_momentum_v1",
            "strategy_version": "oi_momentum_v1",
            "strategy_config_digest": "0" * 64,
        }

        tables = {
            str(row["table_name"])
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name IN "
                "('news_market_liquidations', 'trading_strategy_registrations', "
                "'trading_strategy_evaluations')"
            ).fetchall()
        }
        assert tables == {
            "news_market_liquidations",
            "trading_strategy_registrations",
            "trading_strategy_evaluations",
        }
        evaluation_columns = {
            str(row["column_name"])
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'trading_strategy_evaluations'"
            ).fetchall()
        }
        assert {
            "outcome_attempt_count",
            "outcome_next_attempt_at_ms",
            "outcome_last_error",
        } <= evaluation_columns
        liquidation_columns = {
            str(row["column_name"])
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'news_market_liquidations'"
            ).fetchall()
        }
        assert "ingest_mode" in liquidation_columns
        liquidation_fks = conn.execute(
            "SELECT constraint_name FROM information_schema.table_constraints "
            "WHERE table_schema = 'public' AND table_name = 'news_market_liquidations' "
            "AND constraint_type = 'FOREIGN KEY'"
        ).fetchall()
        assert liquidation_fks == []
    finally:
        if conn is not None:
            conn.close()


def _seed_pre_hard_cut_case(conn: Any, *, case_id: str, state: str) -> None:
    conn.execute(
        """
        INSERT INTO trading_cases (
          case_id, underlying_key, trigger_kind, strategy_id, strategy_version,
          strategy_config_digest, mode, primary_source_key, supplemental_source_keys,
          manifest, manifest_sha256, state, observed_at_ms, created_at_ms, updated_at_ms
        ) VALUES (%s, 'crypto:SOL', 'oi', 'oi_smart_money_momentum_v1',
                  'oi_smart_money_momentum_v1', %s, 'paper', %s, '[]'::jsonb,
                  '{}'::jsonb, %s, %s, %s, %s, %s)
        """,
        (case_id, "0" * 64, f"source-{case_id}", "3" * 64, state, NOW, NOW, NOW),
    )


def _seed_pre_hard_cut_intent(conn: Any) -> None:
    _seed_pre_hard_cut_case(conn, case_id="intent-case", state="ORDER_PREPARED")
    conn.execute(
        """
        INSERT INTO trading_intents (
          intent_id, intent_version, case_id, case_manifest_sha256, intent_policy_sha256,
          execution_environment, instrument_id, side, created_at_ms, valid_until_ms,
          reference_price, target_notional_usd, stop_loss_bps, max_holding_ms,
          max_entry_drift_bps, max_spread_bps
        ) VALUES (%s, 'trade_intent_v1', 'intent-case', %s, %s,
                  'BINANCE_USDM_DEMO', 'SOLUSDT-PERP.BINANCE', 'long', %s, %s,
                  100, 10, 200, 180000, 25, 30)
        """,
        (
            "1" * 64,
            "3" * 64,
            "45702e47bf093ba7c5996eae2186e9e2d1dfee0d9c0a434ced7afa4377286243",
            NOW,
            NOW + 60_000,
        ),
    )


@pytest.mark.parametrize(
    ("blocker", "error"),
    [
        ("case", "trading_hard_cut_pending_case"),
        ("intent", "trading_hard_cut_nonterminal_intent"),
        ("order", "trading_hard_cut_active_legacy_order"),
    ],
)
def test_0317_refuses_every_durable_legacy_or_nonterminal_owner(blocker: str, error: str) -> None:
    conn: Any | None = None
    try:
        _fresh_schema_at(BEFORE_INTENT_HARD_CUT)
        conn = connect_postgres_test(read_only=False)
        if blocker == "case":
            _seed_pre_hard_cut_case(conn, case_id="pending-case", state="PENDING")
        elif blocker == "intent":
            _seed_pre_hard_cut_intent(conn)
        else:
            _seed_pre_snapshot_order(
                conn,
                order_id="active-order",
                state="OPEN",
                position_opened_at_ms=NOW,
                must_close_at_ms=NOW + 180_000,
            )
        conn.commit()
        conn.close()
        conn = None

        with pytest.raises(DBAPIError, match=error):
            _upgrade("20260828_0317")
    finally:
        if conn is not None:
            conn.close()


def test_0317_admits_intent_emitted_and_removes_legacy_worker_writes() -> None:
    conn: Any | None = None
    try:
        _fresh_schema_at(BEFORE_INTENT_HARD_CUT)
        _upgrade("20260828_0317")
        conn = connect_postgres_test(read_only=False)
        _seed_pre_hard_cut_case(conn, case_id="emitted-case", state="INTENT_EMITTED")
        conn.commit()

        for table in ("trading_orders", "trading_order_observations"):
            for privilege in ("INSERT", "UPDATE", "DELETE"):
                assert (
                    conn.execute(
                        "SELECT has_table_privilege('tracefold_workers', %s, %s)",
                        (table, privilege),
                    ).fetchone()["has_table_privilege"]
                    is False
                )
        assert (
            conn.execute(
                "SELECT has_column_privilege('tracefold_workers', 'trading_runtime_state', "
                "'dspy_calls_today', 'UPDATE')"
            ).fetchone()["has_column_privilege"]
            is True
        )
        assert (
            conn.execute(
                "SELECT has_column_privilege('tracefold_workers', 'trading_runtime_state', 'orders_today', 'UPDATE')"
            ).fetchone()["has_column_privilege"]
            is False
        )
    finally:
        if conn is not None:
            conn.close()
