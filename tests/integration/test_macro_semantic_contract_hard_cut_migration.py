from __future__ import annotations

import importlib

import pytest
from alembic import command
from psycopg.errors import CheckViolation

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import test_postgres_dsn as _test_postgres_dsn
from tracefold.platform.postgres.postgres_migrations import alembic_config


def test_0253_invalidates_only_changed_macro_contracts() -> None:
    config = alembic_config()
    config.attributes["database_url"] = _test_postgres_dsn()
    conn = connect_postgres_test(read_only=False)
    old_versions = {
        "rates_fed": "macro_rates_fed_v7",
        "economy_inflation": "macro_economy_inflation_v5",
        "liquidity_funding": "macro_liquidity_funding_v5",
        "credit": "macro_credit_v7",
        "volatility": "macro_volatility_v7",
        "cross_asset": "macro_cross_asset_v7",
    }
    changed_modules = {"rates_fed", "economy_inflation", "cross_asset"}
    try:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("GRANT ALL ON SCHEMA public TO public")
        conn.commit()
        command.upgrade(config, "20260811_0252")
        for index, (module_id, schema_version) in enumerate(old_versions.items(), start=1):
            conn.execute(
                """
                INSERT INTO macro_module_current (
                  module_id, fact_cutoff_ms, payload_json, payload_hash,
                  updated_at_ms, current_health_state, history_depth_state
                ) VALUES (
                  %(module_id)s, %(index)s,
                  jsonb_build_object('schema_version', %(schema_version)s::text),
                  %(payload_hash)s, %(index)s, 'current', 'not_required'
                )
                """,
                {
                    "module_id": module_id,
                    "schema_version": schema_version,
                    "payload_hash": f"sha256:{module_id}",
                    "index": index,
                },
            )
            conn.execute(
                """
                INSERT INTO macro_module_frontiers (
                  module_id, status, input_fingerprint, projection_version,
                  updated_at_ms
                ) VALUES (
                  %(module_id)s, 'clean', %(fingerprint)s, 'sha256:v1', %(index)s
                )
                """,
                {
                    "module_id": module_id,
                    "fingerprint": f"sha256:input-{module_id}",
                    "index": index,
                },
            )
        conn.commit()

        command.upgrade(config, "20260811_0253")

        remaining_current = {
            row["module_id"]
            for row in conn.execute("SELECT module_id FROM macro_module_current ORDER BY module_id").fetchall()
        }
        remaining_frontiers = {
            row["module_id"]
            for row in conn.execute("SELECT module_id FROM macro_module_frontiers ORDER BY module_id").fetchall()
        }
        assert remaining_current == set(old_versions) - changed_modules
        assert remaining_frontiers == set(old_versions) - changed_modules

        with pytest.raises(CheckViolation):
            conn.execute(
                """
                INSERT INTO macro_module_current (
                  module_id, fact_cutoff_ms, payload_json, payload_hash,
                  updated_at_ms, current_health_state, history_depth_state
                ) VALUES (
                  'rates_fed', 20,
                  '{"schema_version":"macro_rates_fed_v7"}'::jsonb,
                  'sha256:old-rates', 20, 'current', 'not_required'
                )
                """
            )
        conn.rollback()

        for index, (module_id, schema_version) in enumerate(
            (
                ("rates_fed", "macro_rates_fed_v8"),
                ("economy_inflation", "macro_economy_inflation_v6"),
                ("cross_asset", "macro_cross_asset_v8"),
            ),
            start=30,
        ):
            conn.execute(
                """
                INSERT INTO macro_module_current (
                  module_id, fact_cutoff_ms, payload_json, payload_hash,
                  updated_at_ms, current_health_state, history_depth_state
                ) VALUES (
                  %(module_id)s, %(index)s,
                  jsonb_build_object('schema_version', %(schema_version)s::text),
                  %(payload_hash)s, %(index)s, 'current', 'not_required'
                )
                """,
                {
                    "module_id": module_id,
                    "schema_version": schema_version,
                    "payload_hash": f"sha256:new-{module_id}",
                    "index": index,
                },
            )
        conn.commit()
    finally:
        conn.close()


def test_0253_downgrade_is_deliberately_unsupported() -> None:
    migration = importlib.import_module(
        "tracefold.platform.postgres.alembic.versions.20260811_0253_macro_semantic_contract_hard_cut"
    )

    with pytest.raises(RuntimeError, match="irreversible Macro semantic-contract hard cut"):
        migration.downgrade()
