from __future__ import annotations

import pytest
from alembic import command
from psycopg.errors import CheckViolation, RaiseException

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
            table_name: conn.execute(f"SELECT COUNT(*)::int AS count FROM {table_name}").fetchone()["count"]
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
            row["thread_id"] for row in conn.execute("SELECT thread_id FROM checkpoints").fetchall()
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

        command.upgrade(config, "20260727_0201")

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


def test_0202_removes_open_binance_candles_and_wrong_unit_fred_facts(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("GRANT ALL ON SCHEMA public TO public")
        conn.commit()
        config = alembic_config()
        config.attributes["database_url"] = _test_postgres_dsn()
        command.upgrade(config, "20260727_0201")
        conn.execute(
            """
            INSERT INTO market_instruments(
              instrument_id, symbol, name, asset_class, instrument_type, venue,
              currency, price_unit, created_at_ms
            )
            VALUES ('btc', 'BTC', 'Bitcoin', 'crypto', 'spot', 'binance', 'USD', 'usdt', 1);
            INSERT INTO market_observations(
              observation_id, instrument_id, dataset_id, source_id, field_name,
              value_numeric, unit, observed_at_ms, published_at_ms, received_at_ms,
              trust_tier, source_url, fact_hash, raw_data_json
            )
            VALUES
              (
                'btc-closed', 'btc', 'binance.btcusdt.spot', 'binance', 'close',
                65000, 'usdt', 100, 100, 200, 'exchange',
                'https://api.binance.com/', 'btc-closed-hash', '{}'::jsonb
              ),
              (
                'btc-open', 'btc', 'binance.btcusdt.spot', 'binance', 'close',
                66000, 'usdt', 9999999999999, 9999999999999, 9999999999999,
                'exchange', 'https://api.binance.com/', 'btc-open-hash', '{}'::jsonb
              );
            INSERT INTO macro_series_facts(
              fact_id, dataset_id, series_id, reference_date, vintage_date,
              value_numeric, value_text, unit, published_at_ms, received_at_ms,
              source_url, fact_hash, raw_data_json
            )
            VALUES
              (
                'reserve-wrong-unit', 'fred.wrbwfrbl', 'WRBWFRBL',
                '2026-07-22', '2026-07-27', 3064896, NULL, 'billions_usd',
                NULL, 200, 'https://fred.stlouisfed.org/', 'reserve-hash', '{}'::jsonb
              ),
              (
                'tga-wrong-unit', 'fred.wtregen', 'WTREGEN',
                '2026-07-22', '2026-07-27', 829623, NULL, 'billions_usd',
                NULL, 200, 'https://fred.stlouisfed.org/', 'tga-hash', '{}'::jsonb
              ),
              (
                'rrp-correct-unit', 'fred.rrpontsyd', 'RRPONTSYD',
                '2026-07-24', '2026-07-27', 11.4, NULL, 'billions_usd',
                NULL, 200, 'https://fred.stlouisfed.org/', 'rrp-hash', '{}'::jsonb
              );
            INSERT INTO macro_acquisition_targets(
              target_key, dataset_id, partition_key, clock_kind, cursor_json,
              status, next_due_at_ms, priority, attempt_count, max_attempts,
              created_at_ms, updated_at_ms
            )
            VALUES
              (
                'binance.btcusdt.spot:latest', 'binance.btcusdt.spot', 'latest',
                'intraday_market', '{"observed_at_ms":9999999999999}', 'current',
                300, 100, 1, 5, 1, 200
              ),
              (
                'fred.wrbwfrbl:latest', 'fred.wrbwfrbl', 'latest',
                'official_state', '{"reference_date":"2026-07-22"}', 'current',
                300, 100, 1, 5, 1, 200
              ),
              (
                'fred.wtregen:latest', 'fred.wtregen', 'latest',
                'official_state', '{"reference_date":"2026-07-22"}', 'current',
                300, 100, 1, 5, 1, 200
              );
            INSERT INTO macro_module_current(
              module_id, readiness, fact_cutoff_ms, payload_json, payload_hash,
              updated_at_ms
            )
            VALUES ('cross_asset', 'ready', 9999999999999, '{}', 'derived-hash', 200)
            """
        )
        conn.commit()

        command.upgrade(config, "20260727_0202")

        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"]
        observations = conn.execute(
            """
            SELECT observation_id
              FROM market_observations
             WHERE dataset_id = 'binance.btcusdt.spot'
             ORDER BY observation_id
            """
        ).fetchall()
        series = conn.execute(
            """
            SELECT fact_id
              FROM macro_series_facts
             ORDER BY fact_id
            """
        ).fetchall()
        targets = conn.execute(
            """
            SELECT dataset_id, status, cursor_json, attempt_count, last_success_at_ms
              FROM macro_acquisition_targets
             ORDER BY dataset_id
            """
        ).fetchall()
        module_count = conn.execute("SELECT count(*) AS count FROM macro_module_current").fetchone()["count"]
        with pytest.raises(RaiseException, match="market_observations_append_only"):
            conn.execute("UPDATE market_observations SET value_numeric = 1 WHERE observation_id = 'btc-closed'")
        conn.rollback()
        with pytest.raises(RaiseException, match="macro_series_facts_append_only"):
            conn.execute("UPDATE macro_series_facts SET value_numeric = 1 WHERE fact_id = 'rrp-correct-unit'")
        conn.rollback()
    finally:
        conn.close()

    assert version == "20260727_0202"
    assert [row["observation_id"] for row in observations] == ["btc-closed"]
    assert [row["fact_id"] for row in series] == ["rrp-correct-unit"]
    assert {row["dataset_id"] for row in targets} == {
        "binance.btcusdt.spot",
        "fred.wrbwfrbl",
        "fred.wtregen",
    }
    assert all(
        row["status"] == "pending"
        and row["cursor_json"] == {}
        and row["attempt_count"] == 0
        and row["last_success_at_ms"] is None
        for row in targets
    )
    assert module_count == 0


def test_0203_rebuilds_binance_daily_close_on_the_settlement_clock(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("GRANT ALL ON SCHEMA public TO public")
        conn.commit()
        config = alembic_config()
        config.attributes["database_url"] = _test_postgres_dsn()
        command.upgrade(config, "20260727_0202")
        conn.execute(
            """
            INSERT INTO market_instruments(
              instrument_id, symbol, name, asset_class, instrument_type, venue,
              currency, price_unit, created_at_ms
            )
            VALUES ('btc', 'BTC', 'Bitcoin', 'crypto', 'spot', 'binance', 'USD', 'usdt', 1);
            INSERT INTO market_observations(
              observation_id, instrument_id, dataset_id, source_id, field_name,
              value_numeric, unit, observed_at_ms, published_at_ms, received_at_ms,
              trust_tier, source_url, fact_hash, raw_data_json
            )
            VALUES (
              'btc-legacy-daily', 'btc', 'binance.btcusdt.spot', 'binance', 'close',
              65000, 'usdt', 100, 100, 200, 'exchange',
              'https://api.binance.com/', 'btc-legacy-hash', '{}'::jsonb
            );
            INSERT INTO macro_acquisition_targets(
              target_key, dataset_id, partition_key, clock_kind, cursor_json,
              status, next_due_at_ms, priority, attempt_count, max_attempts,
              created_at_ms, updated_at_ms
            )
            VALUES (
              'binance.btcusdt.spot:latest', 'binance.btcusdt.spot', 'latest',
              'intraday_market', '{"observed_at_ms":100}', 'current',
              300, 100, 1, 5, 1, 200
            )
            """
        )
        conn.commit()

        command.upgrade(config, "head")

        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"]
        observation_count = conn.execute(
            """
            SELECT count(*) AS count
              FROM market_observations
             WHERE dataset_id = 'binance.btcusdt.spot'
            """
        ).fetchone()["count"]
        target = conn.execute(
            """
            SELECT clock_kind, status, cursor_json, attempt_count
              FROM macro_acquisition_targets
             WHERE dataset_id = 'binance.btcusdt.spot'
            """
        ).fetchone()
        with pytest.raises(CheckViolation, match="clock_kind_check"):
            conn.execute(
                """
                INSERT INTO macro_acquisition_targets(
                  target_key, dataset_id, partition_key, clock_kind, cursor_json,
                  status, next_due_at_ms, priority, attempt_count, max_attempts,
                  created_at_ms, updated_at_ms
                )
                VALUES (
                  'retired-intraday', 'retired.intraday', 'latest',
                  'intraday_market', '{}', 'pending', 0, 100, 0, 5, 1, 1
                )
                """
            )
        conn.rollback()
    finally:
        conn.close()

    assert version == "20260727_0205"
    assert observation_count == 0
    assert target == {
        "clock_kind": "daily_settlement",
        "status": "pending",
        "cursor_json": {},
        "attempt_count": 0,
    }


def test_0205_archives_v1_publications_and_enforces_typed_v2_contracts(
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
        command.upgrade(config, "20260727_0204")
        conn.execute(
            """
            INSERT INTO macro_module_current(
              module_id, readiness, fact_cutoff_ms, payload_json, payload_hash,
              updated_at_ms
            )
            VALUES (
              'rates_fed', 'degraded', 100,
              '{"schema_version":"macro_module_v1"}'::jsonb,
              'sha256:v1-module', 100
            );
            INSERT INTO macro_evidence_packs(
              evidence_pack_id, session_date, judgment_cutoff_ms,
              latest_fact_at_ms, schema_version, compiler_version,
              payload_json, payload_hash, created_at_ms
            )
            VALUES (
              'v1-pack', '2026-07-24', 100, 90,
              'macro_evidence_pack_v1', 'v1',
              '{"schema_version":"macro_evidence_pack_v1"}'::jsonb,
              'sha256:v1-pack', 100
            );
            INSERT INTO macro_daily_judgments(
              session_date, evidence_pack_id, judgment_cutoff_ms,
              latest_fact_at_ms, judgment_json, memo_text, schema_version,
              compiler_version, payload_hash, published_at_ms
            )
            VALUES (
              '2026-07-24', 'v1-pack', 100, 90,
              '{"schema_version":"macro_daily_judgment_v1"}'::jsonb,
              '# v1', 'macro_daily_judgment_v1', 'v1',
              'sha256:v1-judgment', 100
            )
            """
        )
        conn.commit()

        command.upgrade(config, "20260727_0205")

        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"]
        active_counts = {
            table_name: conn.execute(f"SELECT count(*) AS count FROM {table_name}").fetchone()["count"]
            for table_name in (
                "macro_module_current",
                "macro_evidence_packs",
                "macro_daily_judgments",
                "macro_research_runs",
                "macro_research_publications",
            )
        }
        archived_pack = conn.execute(
            """
            SELECT schema_version
            FROM macro_evidence_packs_v1_archive
            WHERE evidence_pack_id = 'v1-pack'
            """
        ).fetchone()
        archived_judgment = conn.execute(
            """
            SELECT schema_version
            FROM macro_daily_judgments_v1_archive
            WHERE session_date = '2026-07-24'
            """
        ).fetchone()
        module_columns = {
            row["column_name"]
            for row in conn.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'macro_module_current'
                """
            ).fetchall()
        }

        with pytest.raises(CheckViolation, match="schema_version"):
            conn.execute(
                """
                INSERT INTO macro_evidence_packs(
                  evidence_pack_id, session_date, judgment_cutoff_ms,
                  latest_fact_at_ms, schema_version, compiler_version,
                  payload_json, payload_hash, created_at_ms
                )
                VALUES (
                  'bad-v1-pack', '2026-07-25', 200, 190,
                  'macro_evidence_pack_v1', 'v1',
                  '{"schema_version":"macro_evidence_pack_v1"}'::jsonb,
                  'sha256:bad-v1-pack', 200
                )
                """
            )
        conn.rollback()
    finally:
        conn.close()

    assert version == "20260727_0205"
    assert set(active_counts.values()) == {0}
    assert archived_pack == {"schema_version": "macro_evidence_pack_v1"}
    assert archived_judgment == {"schema_version": "macro_daily_judgment_v1"}
    assert "readiness" not in module_columns
    assert "data_health_state" in module_columns
