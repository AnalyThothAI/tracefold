"""What the #209 upgrade does to orders that already exist, on a real PostgreSQL upgrade path.

The interesting rows are the ones the migration *cannot* answer for. An order that already opened a
position carries the budget that governed it — `promote_acknowledged` writes `must_close_at_ms` and
`position_opened_at_ms` in one statement, so their difference is a fact. An order that never opened
one does not, and nothing in the database says what `max_holding_seconds` was when it was approved.
Reading today's configuration for it is exactly the drift #209 exists to close, so it stays NULL and
the reconciler refuses to manage it.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from alembic import command
from psycopg.errors import CheckViolation, RaiseException, UniqueViolation
from sqlalchemy.exc import DBAPIError

from tests.postgres_test_utils import (
    connect_postgres_test,
    postgres_migration_test_dsn,
    prepare_test_migration_database,
)
from tests.postgres_test_utils import (
    test_postgres_dsn as postgres_test_dsn,
)
from tests.trading_v3_fixtures import append_capital_evidence_fixture
from tracefold.platform.postgres.migrations import alembic_config
from tracefold.trading.catalog import (
    VenueInstrumentCatalogEntryV1,
    VenueInstrumentCatalogSnapshotV1,
    build_venue_catalog_snapshot,
)
from tracefold.trading.contracts import DecisionRuntimeV1, canonical_sha256
from tracefold.trading.evidence_clock import FutureCaptureReceiptV1, FutureDrainReceiptV1
from tracefold.trading.evidence_verification import NautilusRuntimeStartV1
from tracefold.trading.storage.root import TradingRepository

pytestmark = [pytest.mark.integration, pytest.mark.migration, pytest.mark.usefixtures("postgres_migration_dsn")]

NOW = 1_900_000_000_000
# The v2 policy identity the `trading_intents_v2_shape_check` constraint pins.
INTENT_POLICY_SHA256 = "5788964eb8e210bb09b2cfc5d540c4d680bc9982ae023f3d72227194ab2c1ff0"
BEFORE_SNAPSHOT = "20260825_0307"
BEFORE_STRATEGY_KERNEL = "20260825_0309"
BEFORE_INTENT_HARD_CUT = "20260828_0316"
BEFORE_GLOBAL_NAUTILUS_READINESS_CUT = "20260830_0337"


def _upgrade(revision: str) -> None:
    config = alembic_config()
    config.attributes["database_url"] = postgres_migration_test_dsn()
    command.upgrade(config, revision)


def _fresh_schema_at(revision: str) -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("ALTER SCHEMA public OWNER TO tracefold_owner")
        conn.execute("GRANT ALL ON SCHEMA public TO public")
        conn.commit()
    finally:
        conn.close()
    prepare_test_migration_database(postgres_test_dsn())
    _upgrade(revision)


def _has_column(conn: Any, column: str, *, table: str = "trading_cases") -> bool:
    row = conn.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s AND column_name = %s",
        (table, column),
    ).fetchone()
    return row is not None


def _seed_pre_snapshot_order(
    conn: Any,
    *,
    order_id: str,
    state: str,
    position_opened_at_ms: int | None,
    must_close_at_ms: int | None,
) -> None:
    strategy_schema = _has_column(conn, "trigger_kind")
    # `mode` is `'paper'` on every row that ever existed and #331 dropped it. This helper writes at
    # several historical schema points, so it asks the catalogue rather than assuming one of them.
    has_mode = _has_column(conn, "mode")
    if strategy_schema:
        conn.execute(
            f"""
            INSERT INTO trading_cases (
              case_id, underlying_key, trigger_kind, strategy_id, strategy_version,
              strategy_config_digest, {"mode, " if has_mode else ""}primary_source_key,
              supplemental_source_keys,
              manifest, manifest_sha256, state, observed_at_ms, created_at_ms, updated_at_ms
            ) VALUES (%s, %s, 'oi', 'oi_momentum_v1', 'oi_momentum_v1', %s,
                      {"'paper', " if has_mode else ""}%s,
                      '[]'::jsonb, '{{}}'::jsonb, 'sha', 'ORDER_PREPARED', %s, %s, %s)
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
        # Assert #0308 against its own schema. Later hard cuts intentionally add mandatory Case facts,
        # so seeding a pre-#0308 row shape at head would test unrelated migrations instead.
        _fresh_schema_at("20260825_0308")
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
        ("control", "trading_hard_cut_not_paused"),
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
        conn.execute(
            "UPDATE trading_runtime_state SET control = %s WHERE id = 1",
            ("RUNNING" if blocker == "control" else "PAUSED",),
        )
        if blocker == "case":
            _seed_pre_hard_cut_case(conn, case_id="pending-case", state="PENDING")
        elif blocker == "intent":
            _seed_pre_hard_cut_intent(conn)
        elif blocker == "order":
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
            _upgrade("20260828_0320")
    finally:
        if conn is not None:
            conn.close()


def test_0317_admits_intent_emitted_and_removes_legacy_worker_writes() -> None:
    conn: Any | None = None
    try:
        _fresh_schema_at(BEFORE_INTENT_HARD_CUT)
        conn = connect_postgres_test(read_only=False)
        conn.execute("UPDATE trading_runtime_state SET control = 'PAUSED' WHERE id = 1")
        conn.commit()
        conn.close()
        conn = None
        _upgrade("20260828_0320")
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


@pytest.mark.parametrize(
    ("blocker", "error"),
    (
        ("control", "trading_v2_cutover_not_paused"),
        ("intent", "trading_v2_cutover_nonterminal_intent"),
    ),
)
def test_0320_refuses_a_warm_v2_cutover(blocker: str, error: str) -> None:
    conn: Any | None = None
    try:
        _fresh_schema_at("20260828_0319")
        conn = connect_postgres_test(read_only=False)
        conn.execute(
            "UPDATE trading_runtime_state SET control = %s WHERE id = 1",
            ("RUNNING" if blocker == "control" else "PAUSED",),
        )
        if blocker == "intent":
            _seed_pre_hard_cut_intent(conn)
        conn.commit()
        conn.close()
        conn = None

        with pytest.raises(DBAPIError, match=error):
            _upgrade("20260828_0320")
    finally:
        if conn is not None:
            conn.close()


def test_0320_hard_cuts_new_v1_writes_and_adds_append_only_authority_ledgers() -> None:
    conn: Any | None = None
    try:
        _fresh_schema_at("20260828_0319")
        conn = connect_postgres_test(read_only=False)
        conn.execute("UPDATE trading_runtime_state SET control = 'PAUSED' WHERE id = 1")
        conn.commit()
        conn.close()
        conn = None
        _upgrade("20260828_0320")

        conn = connect_postgres_test(read_only=False)
        tables = {
            str(row["table_name"])
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' "
                "AND table_name IN ("
                "'news_market_instrument_listing_events', "
                "'trading_execution_capability_snapshots', 'trading_replay_runs')"
            ).fetchall()
        }
        assert tables == {
            "news_market_instrument_listing_events",
            "trading_execution_capability_snapshots",
            "trading_replay_runs",
        }
        runtime = conn.execute(
            "SELECT blacklist_revision, nautilus_bootstrap_account_zero_at_ms FROM trading_runtime_state WHERE id = 1"
        ).fetchone()
        assert runtime["blacklist_revision"] == 0
        assert runtime["nautilus_bootstrap_account_zero_at_ms"] is None

        _seed_pre_hard_cut_case(conn, case_id="v1-refused", state="INTENT_EMITTED")
        with pytest.raises(RaiseException, match="new_trade_intent_v1_forbidden"):
            conn.execute(
                """
                INSERT INTO trading_intents (
                  intent_id, intent_version, case_id, case_manifest_sha256, intent_policy_sha256,
                  execution_environment, instrument_id, side, created_at_ms, valid_until_ms,
                  reference_price, target_notional_usd, stop_loss_bps, max_holding_ms,
                  max_entry_drift_bps, max_spread_bps
                ) VALUES (%s, 'trade_intent_v1', 'v1-refused', %s, %s,
                          'BINANCE_USDM_DEMO', 'SOLUSDT-PERP.BINANCE', 'long', %s, %s,
                          100, 10, 200, 180000, 25, 30)
                """,
                ("f" * 64, "3" * 64, "4" * 64, NOW, NOW + 60_000),
            )
        conn.rollback()

        privileges = conn.execute(
            """
            SELECT
              has_table_privilege('tracefold_workers', 'trading_execution_capability_snapshots', 'INSERT')
                AS workers_capability_insert,
              has_table_privilege('tracefold_nautilus', 'trading_execution_capability_snapshots', 'INSERT')
                AS nautilus_capability_insert,
              has_table_privilege('tracefold_serve', 'trading_replay_runs', 'SELECT')
                AS serve_replay_select,
              has_table_privilege('tracefold_serve', 'trading_replay_runs', 'INSERT')
                AS serve_replay_insert,
              has_table_privilege('tracefold_workers', 'trading_replay_runs', 'INSERT')
                AS workers_replay_insert,
              has_table_privilege('tracefold_workers', 'trading_replay_runs', 'UPDATE')
                AS workers_replay_update,
              has_table_privilege('tracefold_workers', 'trading_replay_runs', 'DELETE')
                AS workers_replay_delete,
              has_column_privilege('tracefold_workers', 'trading_runtime_state', 'nautilus_ready', 'UPDATE')
                AS workers_can_invalidate_readiness,
              has_column_privilege(
                'tracefold_workers', 'trading_runtime_state',
                'nautilus_bootstrap_account_zero_at_ms', 'UPDATE'
              ) AS workers_can_clear_bootstrap_proof,
              has_column_privilege(
                'tracefold_nautilus', 'trading_runtime_state',
                'nautilus_bootstrap_account_zero_at_ms', 'UPDATE'
              ) AS nautilus_can_project_bootstrap_proof
            """
        ).fetchone()
        assert dict(privileges) == {
            "workers_capability_insert": True,
            "nautilus_capability_insert": False,
            "serve_replay_select": True,
            "serve_replay_insert": False,
            "workers_replay_insert": True,
            "workers_replay_update": False,
            "workers_replay_delete": False,
            "workers_can_invalidate_readiness": True,
            "workers_can_clear_bootstrap_proof": True,
            "nautilus_can_project_bootstrap_proof": True,
        }
    finally:
        if conn is not None:
            conn.close()


BEFORE_CAPITAL_LANE_V3 = "20260828_0324"


@pytest.mark.parametrize(
    ("blocker", "error"),
    [
        ("case", "capital_lane_v3_undecided_case"),
        ("intent", "capital_lane_v3_nonterminal_intent"),
    ],
)
def test_0325_refuses_to_cut_over_a_warm_lane(blocker: str, error: str) -> None:
    """#331 Phase 0, stated at the schema. A v6 Case cannot be decided by the v7 policy."""

    conn: Any | None = None
    try:
        _fresh_schema_at(BEFORE_CAPITAL_LANE_V3)
        conn = connect_postgres_test(read_only=False)
        conn.execute("UPDATE trading_runtime_state SET control = 'PAUSED' WHERE id = 1")
        if blocker == "case":
            _seed_pre_hard_cut_case(conn, case_id="undecided-case", state="PENDING")
        else:
            _seed_pre_hard_cut_case(conn, case_id="intent-case", state="INTENT_EMITTED")
            conn.execute(
                """
                INSERT INTO trading_execution_capability_snapshots (
                  snapshot_sha256, created_at_ms, execution_environment,
                  included_count, excluded_count, payload
                ) VALUES (%s, %s, 'BINANCE_USDM_DEMO', 1, 0, '{}'::jsonb)
                """,
                ("4" * 64, NOW),
            )
            conn.execute(
                """
                INSERT INTO trading_intents (
                  intent_id, intent_version, case_id, case_manifest_sha256, intent_policy_sha256,
                  execution_environment, execution_capability_snapshot_sha256,
                  blacklist_revision_at_emission, blacklist_snapshot_sha256_at_emission,
                  blacklist_snapshot_payload_at_emission, instrument_id, underlying_key, side,
                  created_at_ms, valid_until_ms, reference_price, target_notional_usd,
                  stop_loss_bps, max_holding_ms, max_entry_drift_bps, max_spread_bps,
                  execution_state
                ) VALUES (%s, 'trade_intent_v2', 'intent-case', %s, %s,
                          'BINANCE_USDM_DEMO', %s, 0, %s,
                          '{"snapshot_version": "blacklist_snapshot_v1"}'::jsonb,
                          'SOLUSDT-PERP.BINANCE', 'crypto:SOL', 'long', %s, %s, 100, 10,
                          200, 180000, 25, 30, 'PENDING')
                """,
                ("9" * 64, "3" * 64, INTENT_POLICY_SHA256, "4" * 64, "5" * 64, NOW, NOW + 60_000),
            )
        conn.commit()
        conn.close()
        conn = None

        with pytest.raises(DBAPIError, match=error):
            _upgrade("20260829_0325")
    finally:
        if conn is not None:
            conn.close()


def test_0326_drops_the_daily_fence_and_stops_the_schema_pinning_one_policy_digest() -> None:
    """#348. The index bounded throughput; the CHECK made the table unwritable on a policy change."""

    conn: Any | None = None
    try:
        _fresh_schema_at("20260829_0325")
        _upgrade("20260829_0326")
        conn = connect_postgres_test(read_only=False)

        indexes = {
            row["indexname"]
            for row in conn.execute("SELECT indexname FROM pg_indexes WHERE tablename = 'trading_intents'").fetchall()
        }
        assert "ux_trading_intents_one_entry_per_utc_day" not in indexes
        # The invariant that actually bounds exposure is untouched.
        assert "ux_trading_intents_one_active" in indexes

        # A v2 Intent under a *different* policy digest is now writable. The old CHECK pinned the
        # digest by value, so changing the execution policy did not merely move an identity — it made
        # the table unwritable. What "two entries on one UTC day" looks like end to end is proved
        # against the real repository in `test_a_closed_thesis_frees_the_lane_for_another_entry_the_
        # same_day`; here the question is only whether the schema still claims to know which policy
        # an Intent may name.
        conn.execute(
            """
            INSERT INTO trading_execution_capability_snapshots (
              snapshot_sha256, created_at_ms, execution_environment,
              included_count, excluded_count, payload
            ) VALUES (%s, %s, 'BINANCE_USDM_DEMO', 1, 0, '{}'::jsonb)
            """,
            ("4" * 64, NOW),
        )
        conn.execute(
            """
            INSERT INTO trading_cases (
              case_id, underlying_key, trigger_kind, strategy_id, strategy_version,
              strategy_config_digest, primary_source_key, supplemental_source_keys,
              manifest, manifest_sha256, state, observed_at_ms, created_at_ms, updated_at_ms
            ) VALUES ('intent-case', 'crypto:SOL', 'oi', 'binance_oi_smart_money_long_v2',
                      'binance_oi_smart_money_long_v2', %s, 'source-intent-case', '[]'::jsonb,
                      '{}'::jsonb, %s, 'INTENT_EMITTED', %s, %s, %s)
            """,
            ("0" * 64, "3" * 64, NOW, NOW, NOW),
        )
        conn.execute(
            """
            INSERT INTO trading_intents (
              intent_id, intent_version, case_id, case_manifest_sha256, intent_policy_sha256,
              execution_environment, execution_capability_snapshot_sha256,
              blacklist_revision_at_emission, blacklist_snapshot_sha256_at_emission,
              blacklist_snapshot_payload_at_emission, instrument_id, underlying_key, side,
              created_at_ms, valid_until_ms, reference_price, target_notional_usd,
              stop_loss_bps, max_holding_ms, max_entry_drift_bps, max_spread_bps,
              execution_state, terminal_outcome, reason_code
            ) VALUES (%s, 'trade_intent_v2', 'intent-case', %s, %s,
                      'BINANCE_USDM_DEMO', %s, 0, %s,
                      '{"snapshot_version": "blacklist_snapshot_v1"}'::jsonb,
                      'SOLUSDT-PERP.BINANCE', 'crypto:SOL', 'long', %s, %s, 100, 10,
                      200, 180000, 25, 30, 'TERMINAL', 'EXPIRED', 'intent_expired')
            """,
            # A digest that is emphatically not the one the constraint used to name.
            ("9" * 64, "3" * 64, "b" * 64, "4" * 64, "5" * 64, NOW, NOW + 60_000),
        )
        conn.commit()
    finally:
        if conn is not None:
            conn.close()


def test_0325_drains_the_per_poll_counters_and_admits_the_new_admission_vocabulary() -> None:
    conn: Any | None = None
    try:
        _fresh_schema_at(BEFORE_CAPITAL_LANE_V3)
        conn = connect_postgres_test(read_only=False)
        # Historical rows stay exactly as they were written; nothing here rewrites a ledger.
        _seed_pre_hard_cut_case(conn, case_id="historical-case", state="POLICY_REJECTED")
        conn.commit()
        conn.close()
        conn = None
        _upgrade("20260829_0325")
        conn = connect_postgres_test(read_only=False)

        assert _has_column(conn, "policy_checks")
        for retired in ("mode",):
            assert not _has_column(conn, retired)
        for retired in ("funnel", "dspy_calls_today", "day_key"):
            assert not _has_column(conn, retired, table="trading_runtime_state")
        for dropped in ("trading_strategy_evaluations", "trading_strategy_registrations"):
            assert conn.execute("SELECT to_regclass(%s) AS t", (f"public.{dropped}",)).fetchone()["t"] is None

        row = conn.execute("SELECT state, strategy_id FROM trading_cases WHERE case_id = 'historical-case'").fetchone()
        assert dict(row) == {"state": "POLICY_REJECTED", "strategy_id": "oi_smart_money_momentum_v1"}

        # The Gate can now say `RESEARCH_ONLY` at the two new stages, and still stores `routing` rows.
        for status, stage, reason in (
            ("RESEARCH_ONLY", "venue", "research_only_venue"),
            ("DEFERRED", "capability", "capability_absent"),
            ("DEFERRED", "routing", "no_native_perp"),
        ):
            conn.execute(
                """
                INSERT INTO trading_candidate_gate_decisions (
                  source_key, gate_version, gate_config_digest, trigger_kind, underlying_key,
                  source_observed_at_ms, status, stage, reason, retryable, evidence, case_id,
                  first_evaluated_at_ms, last_evaluated_at_ms, attempt_count
                ) VALUES (%s, 'trading_admission_v2', %s, 'oi', 'crypto:SOL', %s, %s, %s, %s,
                          false, '{}'::jsonb, NULL, %s, %s, 1)
                """,
                (f"oi:{reason}:v1", "0" * 64, NOW, status, stage, reason, NOW, NOW),
            )
        conn.commit()
        assert conn.execute("SELECT count(*) AS n FROM trading_candidate_gate_decisions").fetchone()["n"] == 3

        # Workers keep exactly the two runtime columns they still write.
        granted = conn.execute(
            "SELECT column_name FROM information_schema.column_privileges "
            "WHERE grantee = 'tracefold_workers' AND table_name = 'trading_runtime_state' "
            "AND privilege_type = 'UPDATE' ORDER BY column_name"
        ).fetchall()
        assert [row["column_name"] for row in granted] == ["control", "updated_at_ms"]
    finally:
        if conn is not None:
            conn.close()


def test_0327_preserves_a_nonterminal_intent_and_0329_refuses_to_orphan_it() -> None:
    conn: Any | None = None
    try:
        _fresh_schema_at("20260829_0326")
        conn = connect_postgres_test(read_only=False)
        conn.execute("UPDATE trading_runtime_state SET control = 'PAUSED' WHERE id = 1")
        conn.execute(
            """
            INSERT INTO trading_execution_capability_snapshots (
              snapshot_sha256, created_at_ms, execution_environment,
              included_count, excluded_count, payload
            ) VALUES (%s, %s, 'BINANCE_USDM_DEMO', 1, 0, '{}'::jsonb)
            """,
            ("4" * 64, NOW),
        )
        conn.execute(
            """
            INSERT INTO trading_cases (
              case_id, underlying_key, trigger_kind, strategy_id, strategy_version,
              strategy_config_digest, primary_source_key, supplemental_source_keys,
              manifest, manifest_sha256, state, observed_at_ms, created_at_ms, updated_at_ms
            ) VALUES ('recovery-case', 'crypto:SOL', 'oi', 'binance_oi_smart_money_long_v2',
                      'binance_oi_smart_money_long_v2', %s, 'source-recovery-case', '[]'::jsonb,
                      '{}'::jsonb, %s, 'INTENT_EMITTED', %s, %s, %s)
            """,
            ("0" * 64, "3" * 64, NOW, NOW, NOW),
        )
        conn.execute(
            """
            INSERT INTO trading_intents (
              intent_id, intent_version, case_id, case_manifest_sha256, intent_policy_sha256,
              execution_environment, execution_capability_snapshot_sha256,
              blacklist_revision_at_emission, blacklist_snapshot_sha256_at_emission,
              blacklist_snapshot_payload_at_emission, instrument_id, underlying_key, side,
              created_at_ms, valid_until_ms, reference_price, target_notional_usd,
              stop_loss_bps, max_holding_ms, max_entry_drift_bps, max_spread_bps,
              execution_state
            ) VALUES (%s, 'trade_intent_v2', 'recovery-case', %s, %s,
                      'BINANCE_USDM_DEMO', %s, 0, %s,
                      '{"snapshot_version": "blacklist_snapshot_v1"}'::jsonb,
                      'SOLUSDT-PERP.BINANCE', 'crypto:SOL', 'long', %s, %s, 100, 10,
                      200, 180000, 25, 30, 'PENDING')
            """,
            ("9" * 64, "3" * 64, INTENT_POLICY_SHA256, "4" * 64, "5" * 64, NOW, NOW + 60_000),
        )
        conn.commit()
        conn.close()
        conn = None

        _upgrade("20260829_0327")
        conn = connect_postgres_test(read_only=False)
        # This assertion owns the historical 0327 schema.  Current repository methods intentionally
        # target head and must not grow compatibility branches for a migration-point test.
        conn.execute(
            "UPDATE trading_runtime_state SET control = 'PAUSED', updated_at_ms = %s WHERE id = 1",
            (NOW + 1,),
        )
        conn.execute(
            "UPDATE trading_binding_runtime SET credential_state = 'unconfigured', "
            "credential_fingerprint = NULL, runtime_state = 'stopped', account_state = 'unknown', "
            "heartbeat_at_ms = NULL, reason = 'recovery_blocked_credentials_missing', updated_at_ms = %s "
            "WHERE binding = 'BINANCE_USDM'",
            (NOW + 1,),
        )
        conn.commit()
        runtime = conn.execute("SELECT reason FROM trading_binding_runtime WHERE binding = 'BINANCE_USDM'").fetchone()
        assert runtime is not None and runtime["reason"] == "recovery_blocked_credentials_missing"
        assert (
            conn.execute("SELECT execution_state FROM trading_intents WHERE intent_id = %s", ("9" * 64,)).fetchone()[
                "execution_state"
            ]
            == "PENDING"
        )
        conn.close()
        conn = None
        with pytest.raises(DBAPIError, match="intent_quote_authority_requires_no_recovery_obligation"):
            _upgrade("20260829_0329")
    finally:
        if conn is not None:
            conn.close()


def test_0329_adds_the_bounded_submission_fence_contract() -> None:
    conn: Any | None = None
    try:
        _fresh_schema_at("20260829_0328")
        _upgrade("20260829_0329")
        conn = connect_postgres_test(read_only=False)

        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"] == "20260829_0329"
        columns = {
            row["column_name"]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'trading_intents'"
            ).fetchall()
        }
        assert {
            "adopted_at_ms",
            "entry_fence_requested_at_ms",
            "submission_fence_version",
            "submission_quantity",
            "entry_quote_q1",
            "entry_quote_q2",
            "entry_submitted_at_ms",
            "entry_accepted_at_ms",
        } <= columns
        granted = {
            row["column_name"]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.column_privileges "
                "WHERE grantee = 'tracefold_nautilus' AND table_name = 'trading_intents' "
                "AND privilege_type = 'UPDATE'"
            ).fetchall()
        }
        assert {"submission_quantity", "entry_quote_q1", "entry_quote_q2"} <= granted
    finally:
        if conn is not None:
            conn.close()


def test_0331_refuses_a_legacy_obligation_then_hard_cuts_to_production_v3() -> None:
    """#376 PR 1 cannot orphan a live V1/V2 Intent; a terminal archive may cross."""

    conn: Any | None = None
    intent_id = "9" * 64
    try:
        _fresh_schema_at("20260829_0329")
        conn = connect_postgres_test(read_only=False)
        conn.execute("UPDATE trading_runtime_state SET control = 'PAUSED' WHERE id = 1")
        conn.execute(
            """
            INSERT INTO trading_execution_capability_snapshots (
              snapshot_sha256, created_at_ms, execution_environment,
              included_count, excluded_count, payload
            ) VALUES (%s, %s, 'BINANCE_USDM_DEMO', 1, 0, '{}'::jsonb)
            """,
            ("4" * 64, NOW),
        )
        conn.execute(
            """
            INSERT INTO trading_cases (
              case_id, underlying_key, trigger_kind, strategy_id, strategy_version,
              strategy_config_digest, primary_source_key, supplemental_source_keys,
              manifest, manifest_sha256, state, policy_decision, policy_reason,
              capital_disposition, capital_reason, observed_at_ms, created_at_ms, updated_at_ms
            ) VALUES ('v3-cutover-case', 'crypto:SOL', 'oi', 'binance_oi_smart_money_long_v2',
                      'binance_oi_smart_money_long_v2', %s, 'source-v3-cutover', '[]'::jsonb,
                      '{}'::jsonb, %s, 'INTENT_EMITTED', 'long', 'smart_money_momentum_long',
                      'allowed', NULL, %s, %s, %s)
            """,
            ("0" * 64, "3" * 64, NOW, NOW, NOW),
        )
        conn.execute(
            """
            INSERT INTO trading_intents (
              intent_id, intent_version, case_id, case_manifest_sha256, intent_policy_sha256,
              execution_environment, execution_capability_snapshot_sha256,
              blacklist_revision_at_emission, blacklist_snapshot_sha256_at_emission,
              blacklist_snapshot_payload_at_emission, instrument_id, underlying_key, side,
              created_at_ms, valid_until_ms, reference_price, target_notional_usd,
              stop_loss_bps, max_holding_ms, max_entry_drift_bps, max_spread_bps,
              execution_state
            ) VALUES (%s, 'trade_intent_v2', 'v3-cutover-case', %s, %s,
                      'BINANCE_USDM_DEMO', %s, 0, %s,
                      '{"snapshot_version": "blacklist_snapshot_v1"}'::jsonb,
                      'SOLUSDT-PERP.BINANCE', 'crypto:SOL', 'long', %s, %s, 100, 10,
                      200, 180000, 25, 30, 'PENDING')
            """,
            (intent_id, "3" * 64, INTENT_POLICY_SHA256, "4" * 64, "5" * 64, NOW, NOW + 60_000),
        )
        conn.commit()
        conn.close()
        conn = None

        with pytest.raises(DBAPIError, match="trading_v3_contract_cutover_legacy_obligation"):
            _upgrade("20260830_0331")

        conn = connect_postgres_test(read_only=False)
        conn.execute(
            "UPDATE trading_intents SET execution_state = 'TERMINAL', "
            "terminal_outcome = 'EXPIRED', reason_code = 'intent_expired', updated_at_ms = %s "
            "WHERE intent_id = %s",
            (NOW + 1, intent_id),
        )
        conn.commit()
        conn.close()
        conn = None

        _upgrade("20260830_0331")
        conn = connect_postgres_test(read_only=False)
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"] == "20260830_0331"
        assert (
            conn.execute("SELECT intent_version FROM trading_intents WHERE intent_id = %s", (intent_id,)).fetchone()[
                "intent_version"
            ]
            == "trade_intent_v2"
        )
    finally:
        if conn is not None:
            conn.close()


def test_0332_capital_authority_cutover_requires_paused_and_installs_attempt_day_ledger() -> None:
    conn: Any | None = None
    try:
        _fresh_schema_at("20260830_0331")
        conn = connect_postgres_test(read_only=False)
        conn.execute("UPDATE trading_runtime_state SET control = 'RUNNING' WHERE id = 1")
        conn.commit()
        conn.close()
        conn = None

        with pytest.raises(DBAPIError, match="trading_capital_authority_cutover_requires_paused"):
            _upgrade("20260830_0332")

        conn = connect_postgres_test(read_only=False)
        conn.execute("UPDATE trading_runtime_state SET control = 'PAUSED' WHERE id = 1")
        conn.commit()
        conn.close()
        conn = None
        _upgrade("20260830_0332")

        conn = connect_postgres_test(read_only=False)
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"] == "20260830_0332"
        columns = {
            row["column_name"]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'trading_capital_risk_reservation_state'"
            ).fetchall()
        }
        assert {"attempt_day_start_ms", "attempt_day_end_ms", "attempt_consumed"} <= columns
        assert conn.execute("SELECT arm_epoch FROM trading_runtime_state WHERE id = 1").fetchone()["arm_epoch"] == 1
    finally:
        if conn is not None:
            conn.close()


def test_0334_evidence_clock_requires_paused_and_is_append_only() -> None:
    conn: Any | None = None
    try:
        _fresh_schema_at("20260830_0332")
        conn = connect_postgres_test(read_only=False)
        conn.execute("UPDATE trading_runtime_state SET control = 'RUNNING' WHERE id = 1")
        conn.commit()
        conn.close()
        conn = None

        with pytest.raises(DBAPIError, match="trading_evidence_clock_cutover_requires_paused"):
            _upgrade("20260830_0334")

        conn = connect_postgres_test(read_only=False)
        conn.execute("UPDATE trading_runtime_state SET control = 'PAUSED' WHERE id = 1")
        conn.commit()
        conn.close()
        conn = None
        _upgrade("20260830_0334")

        conn = connect_postgres_test(read_only=False)
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"] == "20260830_0334"
        grant_columns = {
            row["column_name"]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'trading_production_promotion_grants'"
            ).fetchall()
        }
        assert {"sealed_corpus_sha256", "locked_future_report_sha256"} <= grant_columns
        privileges = conn.execute(
            "SELECT has_table_privilege('tracefold_workers', 'trading_evidence_clock_receipts', 'INSERT') "
            "AS worker_insert, "
            "has_table_privilege('tracefold_serve', 'trading_evidence_clock_receipts', 'INSERT') "
            "AS serve_insert, "
            "has_table_privilege('tracefold_nautilus', 'trading_nautilus_runtime_starts', 'INSERT') "
            "AS nautilus_start_insert, "
            "has_table_privilege('tracefold_workers', 'trading_nautilus_runtime_starts', 'INSERT') "
            "AS worker_start_insert, "
            "has_function_privilege('tracefold_workers', "
            "'store_trading_venue_catalog_snapshot(text,text,bigint,bigint,integer,jsonb,bigint)', "
            "'EXECUTE') AS worker_catalog_function, "
            "has_function_privilege('tracefold_serve', "
            "'store_trading_venue_catalog_snapshot(text,text,bigint,bigint,integer,jsonb,bigint)', "
            "'EXECUTE') AS serve_catalog_function, "
            "has_function_privilege('tracefold_workers', 'trading_evidence_now_ms()', 'EXECUTE') "
            "AS worker_evidence_clock, "
            "has_function_privilege('tracefold_serve', 'trading_evidence_now_ms()', 'EXECUTE') "
            "AS serve_evidence_clock"
        ).fetchone()
        assert dict(privileges) == {
            "worker_insert": True,
            "serve_insert": False,
            "nautilus_start_insert": True,
            # Owner-direct migrations apply the owner's broad Workers default privilege.
            # The single current privilege revision removes this Nautilus-owned write.
            "worker_start_insert": True,
            "worker_catalog_function": True,
            "serve_catalog_function": False,
            "worker_evidence_clock": True,
            "serve_evidence_clock": False,
        }

        repos = TradingRepository(conn)
        runtime_start = NautilusRuntimeStartV1(
            runtime_id="00000000-0000-0000-0000-000000000099",
            runtime_revision="1" * 40,
            image_digest="tracefold@sha256:" + "2" * 64,
            nautilus_version="1.231.0",
            nautilus_source_git_commit="3" * 40,
            nautilus_wheel_identity="linux@sha256:" + "4" * 64,
            started_at_ms=NOW,
        )
        assert repos.append_nautilus_runtime_start(runtime_start)
        result = append_capital_evidence_fixture(repos)
        assert repos.future_holdout_result_for_artifact(result.report_sha256) == result
        protocol_receipt = repos.future_holdout_receipt_for_protocol(result.protocol_sha256)
        assert protocol_receipt is not None
        assert protocol_receipt["artifact_sha256"] == result.report_sha256
        drain_receipt = repos.future_drain_receipt_for_protocol(result.protocol_sha256)
        assert drain_receipt is not None
        assert protocol_receipt["parent_receipt_sha256"] == drain_receipt["receipt_sha256"]
        capture_receipt = repos.future_capture_receipt_for_protocol(result.protocol_sha256)
        assert capture_receipt is not None
        assert drain_receipt["parent_receipt_sha256"] == capture_receipt["receipt_sha256"]
        conn.commit()
        with pytest.raises(UniqueViolation), conn.transaction():
            repos.append_future_capture_receipt(
                FutureCaptureReceiptV1(
                    binding="BINANCE_USDM",
                    candidate_receipt_sha256=str(capture_receipt["parent_receipt_sha256"]),
                    protocol_sha256=result.protocol_sha256,
                    sealed_corpus_sha256=result.sealed_corpus_sha256,
                    capture_sha256="8" * 64,
                    artifact_sha256="8" * 64,
                    artifact_path="test-evidence/second-future-capture.json",
                    batch_count=int(capture_receipt["payload"]["receipt"]["batch_count"]),
                    batch_health_sha256=str(capture_receipt["payload"]["receipt"]["batch_health_sha256"]),
                    collection_incidents=tuple(capture_receipt["payload"]["receipt"]["collection_incidents"]),
                    created_at_ms=300_004,
                )
            )
        with pytest.raises(UniqueViolation), conn.transaction():
            repos.append_future_drain_receipt(
                FutureDrainReceiptV1(
                    binding="BINANCE_USDM",
                    candidate_receipt_sha256=str(drain_receipt["payload"]["receipt"]["candidate_receipt_sha256"]),
                    capture_receipt_sha256=str(capture_receipt["receipt_sha256"]),
                    protocol_sha256=result.protocol_sha256,
                    sealed_corpus_sha256=result.sealed_corpus_sha256,
                    capture_sha256=result.future_capture_sha256,
                    drain_sha256="9" * 64,
                    artifact_sha256="9" * 64,
                    artifact_path="test-evidence/second-future-drain.json",
                    created_at_ms=300_005,
                )
            )
        candidate_row = conn.execute(
            "SELECT receipt_sha256 FROM trading_evidence_clock_receipts WHERE receipt_kind = 'CANDIDATE_DECISION'"
        ).fetchone()
        invalid_receipt = {
            "receipt_version": "candidate_decision_receipt_v1",
            "terminal": "NO_CANDIDATE",
            "binding": "HYPERLIQUID_PERP",
            "sealed_corpus_sha256": "4" * 64,
            "artifact_sha256": "8" * 64,
            "artifact_path": "invalid-parent.json",
            "protocol_sha256": None,
            "created_at_ms": 4,
        }
        invalid_payload = {
            "receipt_sha256": "9" * 64,
            "receipt_kind": "CANDIDATE_DECISION",
            "terminal": "NO_CANDIDATE",
            "binding": "HYPERLIQUID_PERP",
            "parent_receipt_sha256": candidate_row["receipt_sha256"],
            "artifact_sha256": "8" * 64,
            "corpus_sha256": "4" * 64,
            "protocol_sha256": None,
            "receipt": invalid_receipt,
            "evidence": {"terminal": "NO_CANDIDATE"},
        }
        with (
            pytest.raises(RaiseException, match="trading_evidence_candidate_parent_invalid"),
            conn.transaction(),
        ):
            conn.execute(
                """
                INSERT INTO trading_evidence_clock_receipts (
                  receipt_sha256, receipt_kind, terminal, binding, parent_receipt_sha256,
                  artifact_sha256, corpus_sha256, protocol_sha256, created_at_ms, payload
                ) VALUES (%s, 'CANDIDATE_DECISION', 'NO_CANDIDATE', 'HYPERLIQUID_PERP',
                          %s, %s, %s, NULL, 4, %s::jsonb)
                """,
                (
                    "9" * 64,
                    candidate_row["receipt_sha256"],
                    "8" * 64,
                    "4" * 64,
                    json.dumps(invalid_payload),
                ),
            )
        with (
            pytest.raises(RaiseException, match="trading_append_only_mutation_forbidden"),
            conn.transaction(),
        ):
            conn.execute(
                "UPDATE trading_evidence_clock_receipts SET created_at_ms = created_at_ms + 1 "
                "WHERE artifact_sha256 = %s",
                (result.report_sha256,),
            )
        with (
            pytest.raises(RaiseException, match="trading_append_only_mutation_forbidden"),
            conn.transaction(),
        ):
            conn.execute(
                "DELETE FROM trading_nautilus_runtime_starts WHERE start_sha256 = %s",
                (runtime_start.start_sha256,),
            )
    finally:
        if conn is not None:
            conn.close()


def test_0327_preserves_historical_capability_stage_but_rejects_new_alias_writes() -> None:
    conn: Any | None = None
    insert_decision = """
        INSERT INTO trading_candidate_gate_decisions (
          source_key, gate_version, gate_config_digest, trigger_kind, underlying_key,
          source_observed_at_ms, status, stage, reason, retryable, evidence, case_id,
          first_evaluated_at_ms, last_evaluated_at_ms, attempt_count
        ) VALUES (%s, 'trading_admission_v2', %s, 'oi', 'crypto:SOL', %s, 'DEFERRED', %s, %s,
                  false, '{}'::jsonb, NULL, %s, %s, 1)
    """
    try:
        _fresh_schema_at("20260829_0326")
        conn = connect_postgres_test(read_only=False)
        conn.execute("UPDATE trading_runtime_state SET control = 'PAUSED' WHERE id = 1")
        conn.execute(
            insert_decision,
            ("oi:historical-capability:v1", "0" * 64, NOW, "capability", "capability_absent", NOW, NOW),
        )
        conn.commit()
        conn.close()
        conn = None

        _upgrade("20260829_0327")
        conn = connect_postgres_test(read_only=False)
        historical = conn.execute(
            "SELECT status, stage, reason FROM trading_candidate_gate_decisions "
            "WHERE source_key = 'oi:historical-capability:v1'"
        ).fetchone()
        assert dict(historical) == {"status": "DEFERRED", "stage": "capability", "reason": "capability_absent"}

        repos = TradingRepository(conn)
        assert repos.expire_stale_gate_decisions(stale_before_ms=NOW + 1, now_ms=NOW + 2) == 1
        expired = conn.execute(
            "SELECT status, stage, reason FROM trading_candidate_gate_decisions "
            "WHERE source_key = 'oi:historical-capability:v1'"
        ).fetchone()
        assert dict(expired) == {"status": "EXPIRED", "stage": "capability", "reason": "capability_absent"}
        conn.commit()

        with pytest.raises(RaiseException, match="trading_candidate_gate_stage_retired"):
            conn.execute(
                insert_decision,
                ("oi:new-capability:v1", "1" * 64, NOW + 1, "capability", "capability_absent", NOW, NOW),
            )
        conn.rollback()
        conn.execute(
            insert_decision,
            ("oi:new-catalog:v1", "1" * 64, NOW + 1, "catalog", "catalog_absent", NOW, NOW),
        )
        conn.commit()
        with pytest.raises(RaiseException, match="trading_candidate_gate_stage_retired"):
            conn.execute(
                "UPDATE trading_candidate_gate_decisions SET stage = 'capability' "
                "WHERE source_key = 'oi:new-catalog:v1'"
            )
        conn.rollback()
    finally:
        if conn is not None:
            conn.close()


def test_0327_persists_orthogonal_decision_binding_and_catalog_truth() -> None:
    conn: Any | None = None
    try:
        _fresh_schema_at("20260829_0326")
        _upgrade("20260829_0327")
        conn = connect_postgres_test(read_only=False)
        repos = TradingRepository(conn)

        def binding_runtime_0327(*, now_ms: int) -> Any:
            return conn.execute(
                """
                SELECT CASE
                         WHEN runtime.catalog_state = 'ready'
                          AND snapshot.stale_after_ms IS NOT NULL
                          AND runtime.catalog_captured_at_ms + snapshot.stale_after_ms <= %(now)s
                         THEN 'stale'
                         ELSE runtime.catalog_state
                       END AS catalog_state,
                       runtime.catalog_snapshot_sha256, runtime.reason
                  FROM trading_binding_runtime runtime
                  LEFT JOIN trading_venue_catalog_snapshots snapshot
                    ON snapshot.snapshot_sha256 = runtime.catalog_snapshot_sha256
                 WHERE runtime.binding = 'BINANCE_USDM'
                """,
                {"now": now_ms},
            ).fetchone()

        def store_catalog_0327(*, snapshot: Any, now_ms: int) -> None:
            payload = snapshot.model_dump(mode="json")
            conn.execute(
                """
                INSERT INTO trading_venue_catalog_snapshots (
                  snapshot_sha256, binding, captured_at_ms, stale_after_ms,
                  provider_instrument_count, payload, created_at_ms
                ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)
                ON CONFLICT (snapshot_sha256) DO NOTHING
                """,
                (
                    snapshot.snapshot_sha256,
                    snapshot.binding,
                    snapshot.captured_at_ms,
                    snapshot.stale_after_ms,
                    snapshot.provider_instrument_count,
                    json.dumps(payload, sort_keys=True, separators=(",", ":")),
                    now_ms,
                ),
            )
            conn.execute(
                """
                UPDATE trading_binding_runtime
                   SET catalog_state = 'ready', catalog_snapshot_sha256 = %s,
                       catalog_captured_at_ms = %s,
                       reason = CASE
                         WHEN credential_state = 'unconfigured' THEN 'credentials_unconfigured'
                         WHEN credential_state = 'invalid' THEN 'credentials_invalid'
                         WHEN runtime_state = 'stopped' THEN 'binding_adapter_unavailable'
                         WHEN runtime_state != 'ready' THEN 'binding_unready'
                         ELSE NULL
                       END,
                       updated_at_ms = %s
                 WHERE binding = %s
                """,
                (snapshot.snapshot_sha256, snapshot.captured_at_ms, now_ms, snapshot.binding),
            )

        def active_catalog_0327() -> VenueInstrumentCatalogSnapshotV1 | None:
            row = conn.execute(
                """
                SELECT snapshot.payload, runtime.catalog_snapshot_sha256
                  FROM trading_binding_runtime runtime
                  LEFT JOIN trading_venue_catalog_snapshots snapshot
                    ON snapshot.snapshot_sha256 = runtime.catalog_snapshot_sha256
                 WHERE runtime.binding = 'BINANCE_USDM'
                """
            ).fetchone()
            if row is None or row["payload"] is None:
                return None
            active = VenueInstrumentCatalogSnapshotV1.model_validate(row["payload"])
            assert active.snapshot_sha256 == row["catalog_snapshot_sha256"]
            return active

        decision = repos.decision_runtime()
        assert decision is not None
        assert decision == DecisionRuntimeV1(
            state="DISABLED",
            heartbeat_at_ms=None,
            reason="trading_disabled",
            updated_at_ms=0,
        )
        bindings = conn.execute(
            "SELECT binding, credential_state FROM trading_binding_runtime ORDER BY binding"
        ).fetchall()
        assert [row["binding"] for row in bindings] == ["BINANCE_USDM", "HYPERLIQUID_PERP"]
        assert {row["credential_state"] for row in bindings} == {"unconfigured"}

        conn.execute("UPDATE trading_runtime_state SET control = 'RUNNING' WHERE id = 1")
        conn.execute(
            """
            UPDATE trading_binding_runtime
               SET credential_state = 'configured', credential_fingerprint = %s,
                   runtime_state = 'stopped', account_state = 'unknown',
                   heartbeat_at_ms = NULL, reason = 'binding_adapter_unavailable', updated_at_ms = %s
             WHERE binding = 'BINANCE_USDM'
            """,
            ("f" * 64, NOW),
        )
        conn.execute("UPDATE trading_runtime_state SET control = 'PAUSED' WHERE id = 1")
        assert conn.execute("SELECT control FROM trading_runtime_state WHERE id = 1").fetchone()["control"] == "PAUSED"

        entry = VenueInstrumentCatalogEntryV1(
            provider_instrument_id="BTCUSDT",
            provider_symbol="BTCUSDT",
            venue="binance.usdm",
            canonical_asset="BTC",
            canonical_namespace="native",
            product_kind="linear_perpetual",
            active=True,
            settlement_asset="USDT",
            margin_asset="USDT",
            multiplier="1",
            price_increment="0.1",
            size_increment="0.001",
            raw_metadata_sha256=canonical_sha256({"symbol": "BTCUSDT"}),
        )
        snapshot = build_venue_catalog_snapshot(
            binding="BINANCE_USDM", captured_at_ms=NOW, stale_after_ms=21_600_000, instruments=(entry,)
        )
        store_catalog_0327(snapshot=snapshot, now_ms=NOW)
        conn.commit()
        assert active_catalog_0327() == snapshot
        with (
            pytest.raises(RaiseException, match="trading_append_only_mutation_forbidden"),
            conn.transaction(),
        ):
            conn.execute(
                "UPDATE trading_venue_catalog_snapshots SET created_at_ms = created_at_ms + 1 "
                "WHERE snapshot_sha256 = %s",
                (snapshot.snapshot_sha256,),
            )

        conn.execute(
            "UPDATE trading_binding_runtime SET catalog_state = 'stale', reason = 'venue_timeout', "
            "updated_at_ms = %s WHERE binding = 'BINANCE_USDM'",
            (NOW + 1,),
        )
        conn.commit()
        runtime = binding_runtime_0327(now_ms=NOW + 1)
        assert runtime is not None
        assert runtime["catalog_state"] == "stale"
        assert runtime["catalog_snapshot_sha256"] == snapshot.snapshot_sha256
        assert active_catalog_0327() == snapshot

        store_catalog_0327(snapshot=snapshot, now_ms=NOW + 2)
        conn.commit()
        recovered = binding_runtime_0327(now_ms=NOW + 2)
        assert recovered is not None
        assert (recovered["catalog_state"], recovered["reason"]) == (
            "ready",
            "binding_adapter_unavailable",
        )
        expired = binding_runtime_0327(now_ms=NOW + 21_600_000)
        assert expired is not None
        assert expired["catalog_state"] == "stale"
        assert active_catalog_0327() == snapshot

        columns = {
            row["column_name"]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'trading_cases'"
            ).fetchall()
        }
        assert {"capital_disposition", "capital_reason"} <= columns
        privileges = conn.execute(
            """
            SELECT has_table_privilege('tracefold_serve', 'trading_venue_catalog_snapshots', 'SELECT') AS serve_read,
                   has_table_privilege('tracefold_serve', 'trading_venue_catalog_snapshots', 'INSERT') AS serve_write,
                   has_table_privilege(
                     'tracefold_workers', 'trading_venue_catalog_snapshots', 'INSERT'
                   ) AS worker_write,
                   has_column_privilege('tracefold_nautilus', 'trading_binding_runtime', 'runtime_state', 'UPDATE')
                     AS adapter_runtime_write,
                   has_column_privilege('tracefold_nautilus', 'trading_binding_runtime', 'credential_state', 'UPDATE')
                     AS adapter_credential_write,
                   has_column_privilege('tracefold_nautilus', 'trading_binding_runtime', 'catalog_state', 'UPDATE')
                     AS adapter_catalog_write
            """
        ).fetchone()
        assert dict(privileges) == {
            "serve_read": True,
            "serve_write": False,
            "worker_write": True,
            "adapter_runtime_write": True,
            "adapter_credential_write": False,
            "adapter_catalog_write": False,
        }
    finally:
        if conn is not None:
            conn.close()


def test_0338_drops_unowned_global_nautilus_readiness() -> None:
    conn: Any | None = None
    try:
        _fresh_schema_at(BEFORE_GLOBAL_NAUTILUS_READINESS_CUT)
        conn = connect_postgres_test(read_only=False)
        conn.execute(
            """
            UPDATE trading_runtime_state
               SET nautilus_heartbeat_at_ms = %s,
                   nautilus_ready = true,
                   nautilus_readiness_reason = 'ready',
                   nautilus_unexpected_exposure = false
             WHERE id = 1
            """,
            (NOW,),
        )
        before = conn.execute(
            "SELECT pg_relation_filenode('trading_runtime_state'::regclass) AS relfilenode, "
            "(SELECT count(*) FROM trading_runtime_state) AS row_count"
        ).fetchone()
        conn.commit()

        conn.execute("BEGIN")
        conn.execute("LOCK TABLE trading_runtime_state IN ACCESS SHARE MODE")
        with pytest.raises(DBAPIError):
            _upgrade("head")
        conn.rollback()
        conn.close()
        conn = None

        _upgrade("head")

        conn = connect_postgres_test(read_only=False)
        columns = {
            str(row["column_name"])
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'trading_runtime_state'"
            ).fetchall()
        }
        assert columns.isdisjoint(
            {
                "nautilus_heartbeat_at_ms",
                "nautilus_ready",
                "nautilus_readiness_reason",
                "nautilus_unexpected_exposure",
                "active_capability_snapshot_sha256",
                "active_capability_included_count",
                "nautilus_bootstrap_account_zero_at_ms",
            }
        )
        runtime = conn.execute(
            "SELECT control, blacklist_revision, arm_epoch FROM trading_runtime_state WHERE id = 1"
        ).fetchone()
        assert dict(runtime) == {"control": "PAUSED", "blacklist_revision": 0, "arm_epoch": 1}
        after = conn.execute(
            "SELECT pg_relation_filenode('trading_runtime_state'::regclass) AS relfilenode, "
            "(SELECT count(*) FROM trading_runtime_state) AS row_count"
        ).fetchone()
        assert dict(after) == dict(before)

        conn.execute("SET ROLE tracefold_nautilus")
        assert TradingRepository(conn).capital_control() == "PAUSED"
        conn.execute("RESET ROLE")
    finally:
        if conn is not None:
            conn.close()


def test_0340_hard_cuts_active_execution_to_binance_demo_only() -> None:
    conn: Any | None = None
    try:
        _fresh_schema_at("20260831_0339")
        conn = connect_postgres_test(read_only=False)
        conn.execute("UPDATE trading_runtime_state SET control = 'RUNNING' WHERE id = 1")
        conn.execute(
            """
            UPDATE trading_binding_runtime
               SET credential_state = 'configured', credential_fingerprint = repeat('a', 64),
                   runtime_state = 'ready', account_state = 'reconciled_flat',
                   heartbeat_at_ms = %s, reason = NULL
            """,
            (NOW,),
        )
        conn.commit()
        conn.close()
        conn = None

        _upgrade("head")

        conn = connect_postgres_test(read_only=False)
        capital = conn.execute("SELECT control, arm_epoch FROM trading_runtime_state WHERE id = 1").fetchone()
        assert dict(capital) == {"control": "PAUSED", "arm_epoch": 2}
        rows = conn.execute(
            """
            SELECT binding, credential_state, credential_fingerprint, runtime_state,
                   account_state, capability_state, capability_snapshot_sha256,
                   execution_binding_sha256, active_arm_receipt_sha256, heartbeat_at_ms, reason
              FROM trading_binding_runtime
             ORDER BY binding
            """
        ).fetchall()
        assert [dict(row) for row in rows] == [
            {
                "binding": "BINANCE_USDM",
                "credential_state": "unconfigured",
                "credential_fingerprint": None,
                "runtime_state": "stopped",
                "account_state": "unknown",
                "capability_state": "missing",
                "capability_snapshot_sha256": None,
                "execution_binding_sha256": None,
                "active_arm_receipt_sha256": None,
                "heartbeat_at_ms": None,
                "reason": "binance_demo_contract_cutover",
            },
            {
                "binding": "HYPERLIQUID_PERP",
                "credential_state": "unconfigured",
                "credential_fingerprint": None,
                "runtime_state": "stopped",
                "account_state": "unknown",
                "capability_state": "missing",
                "capability_snapshot_sha256": None,
                "execution_binding_sha256": None,
                "active_arm_receipt_sha256": None,
                "heartbeat_at_ms": None,
                "reason": "execution_binding_disabled",
            },
        ]
        definition = conn.execute(
            "SELECT pg_get_functiondef("
            "'store_trading_venue_catalog_snapshot(text,text,bigint,bigint,integer,jsonb,bigint)'::regprocedure"
            ") AS definition"
        ).fetchone()["definition"]
        assert "p_binding = 'HYPERLIQUID_PERP'" in definition
        assert "execution_binding_disabled" in definition
    finally:
        if conn is not None:
            conn.close()


def test_0340_refuses_cutover_with_reported_exposure() -> None:
    conn: Any | None = None
    try:
        _fresh_schema_at("20260831_0339")
        conn = connect_postgres_test(read_only=False)
        conn.execute(
            "UPDATE trading_binding_runtime SET account_state = 'exposure_present' WHERE binding = 'BINANCE_USDM'"
        )
        conn.commit()
        conn.close()
        conn = None

        with pytest.raises(DBAPIError, match="binance_demo_execution_cut_requires_flat_drained_runtime"):
            _upgrade("head")
    finally:
        if conn is not None:
            conn.close()


def test_0340_rechecks_flatness_after_waiting_for_an_earlier_binding_writer() -> None:
    writer: Any | None = None
    observer: Any | None = None
    try:
        _fresh_schema_at("20260831_0339")
        writer = connect_postgres_test(read_only=False)
        observer = connect_postgres_test(read_only=False)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with writer.transaction():
                writer.execute(
                    "UPDATE trading_binding_runtime SET account_state = 'exposure_present' "
                    "WHERE binding = 'BINANCE_USDM'"
                )
                upgrading = pool.submit(_upgrade, "head")
                deadline = time.monotonic() + 0.8
                while time.monotonic() < deadline:
                    waiting = observer.execute(
                        """
                        SELECT 1
                          FROM pg_stat_activity
                         WHERE datname = current_database()
                           AND pid <> pg_backend_pid()
                           AND %s = ANY(pg_blocking_pids(pid))
                         LIMIT 1
                        """,
                        (writer.info.backend_pid,),
                    ).fetchone()
                    if waiting is not None:
                        break
                    time.sleep(0.01)
                else:
                    pytest.fail("0339 did not wait behind the earlier binding writer")

            with pytest.raises(DBAPIError, match="binance_demo_execution_cut_requires_flat_drained_runtime"):
                upgrading.result(timeout=5)
    finally:
        if writer is not None:
            writer.close()
        if observer is not None:
            observer.close()
