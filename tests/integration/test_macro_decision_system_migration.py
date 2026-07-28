from __future__ import annotations

import pytest
from psycopg.errors import CheckViolation

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tracefold.platform.postgres.postgres_migrations import latest_migration_version


def test_current_baseline_has_intraday_market_and_persisted_judgment_status(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        conn.execute(
            """
            INSERT INTO macro_acquisition_targets(
              target_key, dataset_id, partition_key, clock_kind, cursor_json,
              status, next_due_at_ms, priority, attempt_count, max_attempts,
              created_at_ms, updated_at_ms
            )
            VALUES (
              'yfinance.spy.market:latest', 'yfinance.spy.market', 'latest',
              'intraday_market', '{}', 'pending', 0, 100, 0, 5, 1, 1
            )
            """
        )
        conn.execute(
            """
            INSERT INTO macro_judgment_status(
              session_date, judgment_cutoff_ms, state, reason_code,
              details_json, payload_hash, attempted_at_ms, updated_at_ms
            )
            VALUES (
              '2026-07-27', 100, 'blocked', 'critical_evidence_blocked',
              '{"blocked_modules":["rates_fed"]}'::jsonb, 'sha256:blocked',
              200, 200
            )
            """
        )
        conn.commit()
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"]
        intraday_target = conn.execute(
            """
            SELECT clock_kind, status
              FROM macro_acquisition_targets
             WHERE dataset_id = 'yfinance.spy.market'
            """
        ).fetchone()
        judgment_status = conn.execute(
            """
            SELECT state, reason_code, details_json
              FROM macro_judgment_status
             WHERE session_date = '2026-07-27'
            """
        ).fetchone()
        legacy_macro = conn.execute("SELECT to_regclass('public.macro_observations') AS relation").fetchone()
    finally:
        conn.close()

    assert version == latest_migration_version() == "20260728_0210"
    assert intraday_target == {"clock_kind": "intraday_market", "status": "pending"}
    assert judgment_status == {
        "state": "blocked",
        "reason_code": "critical_evidence_blocked",
        "details_json": {"blocked_modules": ["rates_fed"]},
    }
    assert legacy_macro == {"relation": None}


def test_current_baseline_enforces_live_market_v3_module_contracts(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        conn.execute(
            """
            INSERT INTO macro_module_current(
              module_id, data_health_state, fact_cutoff_ms,
              payload_json, payload_hash, updated_at_ms
            )
            VALUES
              (
                'credit', 'current', 200,
                '{"schema_version":"macro_credit_v3"}'::jsonb,
                'sha256:credit-v3', 200
              ),
              (
                'cross_asset', 'current', 200,
                '{"schema_version":"macro_cross_asset_v3"}'::jsonb,
                'sha256:cross-v3', 200
              )
            """
        )
        conn.commit()
        with pytest.raises(CheckViolation, match="typed_schema_check"):
            conn.execute(
                """
                UPDATE macro_module_current
                   SET payload_json = '{"schema_version":"macro_credit_v2"}'::jsonb
                 WHERE module_id = 'credit'
                """
            )
        conn.rollback()
        versions = conn.execute(
            """
            SELECT module_id, payload_json ->> 'schema_version' AS schema_version
              FROM macro_module_current
             ORDER BY module_id
            """
        ).fetchall()
    finally:
        conn.close()

    assert [dict(row) for row in versions] == [
        {"module_id": "credit", "schema_version": "macro_credit_v3"},
        {"module_id": "cross_asset", "schema_version": "macro_cross_asset_v3"},
    ]
