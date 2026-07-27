from __future__ import annotations

import pytest
from alembic import command
from psycopg.errors import RaiseException

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import test_postgres_dsn as _test_postgres_dsn
from tracefold.platform.postgres.postgres_migrations import alembic_config


def test_0200_destroys_legacy_macro_state_and_starts_new_fact_model_empty(
    tmp_path,
) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("GRANT ALL ON SCHEMA public TO public")
        conn.commit()
        config = alembic_config()
        config.attributes["database_url"] = _test_postgres_dsn()
        command.upgrade(config, "20260727_0199")
        conn.execute(
            """
            INSERT INTO macro_observations(
              observation_id,
              source_name,
              series_key,
              observed_at,
              value_numeric,
              unit,
              frequency,
              data_quality,
              source_ts,
              raw_payload_json,
              ingested_at_ms,
              concept_key,
              source_priority,
              fact_payload_hash
            )
            VALUES (
              'legacy:must-not-migrate',
              'macrodata-cli',
              'legacy:spy',
              '2026-07-25',
              635.25,
              'price',
              'daily',
              'ok',
              '2026-07-25',
              '{}'::jsonb,
              100,
              'asset:spy',
              1,
              'sha256:legacy'
            );
            INSERT INTO checkpoints(
              thread_id,
              checkpoint_ns,
              checkpoint_id,
              checkpoint,
              metadata
            )
            VALUES
              ('macro-research:2026-07-25', '', 'macro-checkpoint', '{}'::jsonb, '{}'::jsonb),
              ('news-research:2026-07-25', '', 'news-checkpoint', '{}'::jsonb, '{}'::jsonb)
            """
        )
        conn.commit()

        command.upgrade(config, "20260727_0200")

        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"]
        legacy_relations = {
            row["name"]: row["relation"]
            for row in conn.execute(
                """
                SELECT name, to_regclass('public.' || name) AS relation
                FROM unnest(
                  ARRAY[
                    'macro_observations',
                    'macro_sync_windows',
                    'macro_sync_runs',
                    'macro_sync_state'
                  ]
                ) AS names(name)
                """
            ).fetchall()
        }
        new_counts = {
            table_name: conn.execute(
                f"SELECT COUNT(*)::int AS count FROM {table_name}"
            ).fetchone()["count"]
            for table_name in (
                "market_observations",
                "market_settlements",
                "market_position_facts",
                "macro_series_facts",
                "macro_release_facts",
                "macro_documents",
                "macro_research_publications",
            )
        }
        remaining_checkpoint_threads = {
            row["thread_id"]
            for row in conn.execute("SELECT thread_id FROM checkpoints").fetchall()
        }
        conn.execute(
            """
            INSERT INTO market_instruments(
              instrument_id,
              symbol,
              name,
              asset_class,
              instrument_type,
              venue,
              currency,
              price_unit,
              created_at_ms
            )
            VALUES (
              'etf:SPY',
              'SPY',
              'SPDR S&P 500 ETF Trust',
              'equity',
              'etf',
              'NYSE Arca',
              'USD',
              'USD',
              100
            );
            INSERT INTO market_observations(
              observation_id,
              instrument_id,
              dataset_id,
              source_id,
              field_name,
              value_numeric,
              unit,
              observed_at_ms,
              published_at_ms,
              received_at_ms,
              trust_tier,
              source_url,
              fact_hash,
              raw_data_json
            )
            VALUES (
              'market:new-fact',
              'etf:SPY',
              'stooq.spy',
              'stooq',
              'close',
              635.25,
              'USD',
              100,
              100,
              100,
              'untrusted_proxy',
              'https://stooq.com/',
              'sha256:new-fact',
              '{}'::jsonb
            )
            """
        )
        conn.commit()

        with pytest.raises(RaiseException, match="market_observations_append_only"):
            conn.execute(
                """
                UPDATE market_observations
                SET value_numeric = 1
                WHERE observation_id = 'market:new-fact'
                """
            )
        conn.rollback()
        with pytest.raises(RuntimeError, match="irreversible"):
            command.downgrade(config, "20260727_0199")
    finally:
        conn.close()

    assert version == "20260727_0200"
    assert set(legacy_relations.values()) == {None}
    assert set(new_counts.values()) == {0}
    assert remaining_checkpoint_threads == {"news-research:2026-07-25"}


def test_0201_removes_stooq_facts_targets_and_derived_state(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("GRANT ALL ON SCHEMA public TO public")
        conn.commit()
        config = alembic_config()
        config.attributes["database_url"] = _test_postgres_dsn()
        command.upgrade(config, "20260727_0200")
        conn.execute(
            """
            INSERT INTO market_instruments(
              instrument_id, symbol, name, asset_class, instrument_type, venue,
              currency, price_unit, created_at_ms
            )
            VALUES ('spy', 'SPY', 'SPY', 'equity', 'etf', 'proxy', 'USD', 'price', 1);
            INSERT INTO market_observations(
              observation_id, instrument_id, dataset_id, source_id, field_name,
              value_numeric, unit, observed_at_ms, published_at_ms, received_at_ms,
              trust_tier, source_url, fact_hash, raw_data_json
            )
            VALUES (
              'old-stooq-fact', 'spy', 'stooq.spy', 'stooq', 'close', 1, 'price',
              1, NULL, 2, 'untrusted_proxy', 'https://stooq.com/', 'old-hash', '{}'::jsonb
            );
            INSERT INTO macro_acquisition_targets(
              target_key, dataset_id, partition_key, clock_kind, cursor_json,
              status, next_due_at_ms, priority, attempt_count, max_attempts,
              created_at_ms, updated_at_ms
            )
            VALUES (
              'stooq.spy:latest', 'stooq.spy', 'latest', 'intraday_market', '{}',
              'current', 2, 100, 1, 5, 1, 2
            );
            INSERT INTO macro_source_receipts(
              receipt_id, target_key, dataset_id, partition_key, started_at_ms,
              completed_at_ms, status, rows_seen, rows_inserted, diagnostics_json
            )
            VALUES (
              'stooq-receipt', 'stooq.spy:latest', 'stooq.spy', 'latest',
              1, 2, 'empty', 0, 0, '{}'
            );
            """
        )
        conn.commit()

        command.upgrade(config, "head")

        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"]
        counts = {
            "observations": conn.execute(
                "SELECT count(*) AS count FROM market_observations WHERE dataset_id LIKE 'stooq.%'"
            ).fetchone()["count"],
            "targets": conn.execute(
                "SELECT count(*) AS count FROM macro_acquisition_targets WHERE dataset_id LIKE 'stooq.%'"
            ).fetchone()["count"],
            "receipts": conn.execute(
                "SELECT count(*) AS count FROM macro_source_receipts WHERE dataset_id LIKE 'stooq.%'"
            ).fetchone()["count"],
        }
    finally:
        conn.close()

    assert version == "20260727_0201"
    assert set(counts.values()) == {0}
