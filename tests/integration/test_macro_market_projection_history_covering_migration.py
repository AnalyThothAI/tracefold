from __future__ import annotations

import importlib
from typing import Any

import pytest
from alembic import command

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import test_postgres_dsn as _test_postgres_dsn
from tracefold.app.repositories import repositories_for_connection
from tracefold.macro.registry import require_dataset
from tracefold.platform.postgres.postgres_migrations import alembic_config


def test_0262_replaces_the_projection_history_index_without_changing_market_facts() -> None:
    config = alembic_config()
    config.attributes["database_url"] = _test_postgres_dsn()
    conn = connect_postgres_test(read_only=False)
    try:
        _reset_schema(config, conn, revision="20260813_0261")
        _insert_preexisting_observations(conn)
        facts_before = _market_fact_identity(conn)

        command.upgrade(config, "20260813_0262")

        facts_after = _market_fact_identity(conn)
        indexes = conn.execute(
            """
            SELECT indexname, indexdef
              FROM pg_indexes
             WHERE schemaname = 'public'
               AND tablename = 'market_observations'
               AND indexname LIKE 'idx_market_observations_projection_history%%'
             ORDER BY indexname
            """
        ).fetchall()
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    finally:
        conn.close()

    assert facts_after == facts_before
    assert version == {"version_num": "20260813_0262"}
    assert [row["indexname"] for row in indexes] == ["idx_market_observations_projection_history"]
    definition = " ".join(str(indexes[0]["indexdef"]).split())
    assert (
        "USING btree (dataset_id, ((observed_at_ms / 86400000)) DESC, "
        "observed_at_ms DESC, received_at_ms DESC, observation_id DESC)"
    ) in definition
    assert (
        "INCLUDE (instrument_id, source_id, field_name, value_numeric, unit, "
        "published_at_ms, trust_tier, source_url, fact_hash)"
    ) in definition


def test_0262_downgrade_is_explicitly_irreversible() -> None:
    migration = importlib.import_module(
        "tracefold.platform.postgres.alembic.versions.20260813_0262_macro_market_projection_history_cover"
    )

    with pytest.raises(RuntimeError, match="irreversible Macro projection covering-read cut"):
        migration.downgrade()


def _reset_schema(config: Any, conn: Any, *, revision: str) -> None:
    conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
    conn.execute("CREATE SCHEMA public")
    conn.execute("GRANT ALL ON SCHEMA public TO public")
    conn.commit()
    command.upgrade(config, revision)


def _insert_preexisting_observations(conn: Any) -> None:
    spec = require_dataset("yfinance.spy.intraday")
    with conn.transaction():
        repositories_for_connection(conn).macro_market.ensure_instrument(spec, now_ms=1_800_000_000_000)
        conn.execute(
            """
            INSERT INTO market_observations(
              observation_id, instrument_id, dataset_id, source_id, field_name,
              value_numeric, unit, observed_at_ms, published_at_ms,
              received_at_ms, trust_tier, source_url, fact_hash, raw_data_json
            )
            SELECT
              'macro-cover-migration-' || lpad(series_no::text, 5, '0'),
              %s, %s, %s, 'close', series_no::double precision, %s,
              1800000000000 + series_no * 300000,
              NULL,
              1800000001000 + series_no * 300000,
              %s, %s,
              'macro-cover-migration-hash-' || lpad(series_no::text, 5, '0'),
              jsonb_build_object('series_no', series_no)
            FROM generate_series(1, 1_000) AS series_no
            """,
            (
                spec.instrument_id,
                spec.dataset_id,
                spec.source_id,
                spec.unit,
                spec.trust_tier,
                spec.source_url,
            ),
        )


def _market_fact_identity(conn: Any) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT
          count(*)::bigint AS row_count,
          min(observation_id) AS first_observation_id,
          max(observation_id) AS last_observation_id,
          md5(string_agg(fact_hash, ',' ORDER BY observation_id)) AS facts_fingerprint
        FROM market_observations
        """
    ).fetchone()
    assert row is not None
    return dict(row)
