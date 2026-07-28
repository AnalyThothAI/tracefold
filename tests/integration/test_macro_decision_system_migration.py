from __future__ import annotations

import pytest
from psycopg.errors import CheckViolation

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tracefold.platform.postgres.postgres_migrations import latest_migration_version


def test_current_baseline_rejects_blocked_judgment_status(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        conn.commit()
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"]
        with pytest.raises(CheckViolation, match="macro_judgment_status_state_check"):
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
        conn.rollback()
        conn.execute(
            """
            INSERT INTO macro_judgment_status(
              session_date, judgment_cutoff_ms, state, reason_code,
              details_json, payload_hash, attempted_at_ms, updated_at_ms
            )
            VALUES (
              '2026-07-27', 100, 'current', 'judgment_published',
              '{"gaps":["fred.dgs10"]}'::jsonb, 'sha256:current', 200, 200
            )
            """
        )
        conn.commit()
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

    assert version == latest_migration_version() == "20260728_0211"
    assert judgment_status == {
        "state": "current",
        "reason_code": "judgment_published",
        "details_json": {"gaps": ["fred.dgs10"]},
    }
    assert legacy_macro == {"relation": None}


def test_current_baseline_enforces_market_clock_v4_module_contracts(tmp_path) -> None:
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
                'credit', 'mixed', 200,
                '{"schema_version":"macro_credit_v4"}'::jsonb,
                'sha256:credit-v4', 200
              ),
              (
                'cross_asset', 'current', 200,
                '{"schema_version":"macro_cross_asset_v4"}'::jsonb,
                'sha256:cross-v4', 200
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
        {"module_id": "credit", "schema_version": "macro_credit_v4"},
        {"module_id": "cross_asset", "schema_version": "macro_cross_asset_v4"},
    ]
