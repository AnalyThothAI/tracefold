from __future__ import annotations

import pytest
from alembic import command
from psycopg.errors import CheckViolation
from sqlalchemy.exc import ProgrammingError

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tests.postgres_test_utils import test_postgres_dsn as _test_postgres_dsn
from tracefold.market.macro_market_repository import GeneralMarketRepository
from tracefold.platform.postgres.postgres_migrations import (
    alembic_config,
    latest_migration_version,
    upgrade_head,
)

RETIRED_BACKEND_TABLES = {
    "projection_runs",
    "projection_offsets",
    "pulse_agent_eval_results",
    "pulse_agent_eval_cases",
    "pulse_evidence_packets",
    "pulse_agent_run_steps",
    "pulse_playbook_snapshots",
    "pulse_candidates",
    "pulse_agent_runs",
    "pulse_agent_jobs",
    "pulse_agent_runtime_versions",
    "pulse_candidate_edge_state",
    "pulse_candidate_run_budget",
    "pulse_target_run_budget",
    "pulse_trigger_dirty_targets",
    "narrative_admissions",
    "narrative_admission_dirty_targets",
    "macro_daily_briefs",
    "macro_import_runs",
    "macro_observations",
    "macro_projection_dirty_targets",
    "macro_observation_series_rows",
    "macro_observation_series_publication_state",
    "macro_view_snapshots",
    "macro_judgment_jobs",
    "macro_judgment_publications",
    "macro_judgment_outcomes",
    "macro_daily_judgments",
    "macro_daily_judgments_v1_archive",
    "macro_judgment_status",
    "macro_event_updates",
    "macro_event_updates_v1_archive",
    "macro_research_runs",
    "macro_research_runs_v1_archive",
    "macro_research_publications",
    "macro_research_publications_v1_archive",
    "macro_sync_runs",
    "macro_sync_state",
    "macro_sync_windows",
    "cex_oi_radar_publication_state",
    "cex_oi_radar_rows",
    "cex_detail_snapshots",
    "account_profiles",
    "account_token_call_stats",
    "account_token_alerts",
    "account_quality_snapshots",
    "notification_deliveries",
    "notification_reads",
    "notifications",
    "news_item_agent_briefs",
    "news_item_agent_runs",
    "news_source_quality_rows",
    "token_radar_source_dirty_events",
    "market_tick_current_dirty_targets",
    "token_capture_tier_dirty_targets",
    "token_capture_tier",
    "news_story_agent_briefs",
    "news_story_agent_runs",
}
PROFESSIONAL_NEWS_TABLES = {
    "news_sources",
    "news_source_memberships",
    "news_source_fetches",
    "news_feed_observations",
    "news_items",
    "news_stories",
    "news_story_members",
    "news_story_aliases",
    "news_story_input_state",
    "news_projection_summary",
    "news_story_facet_counts",
    "news_source_facet_counts",
    "news_brief_selection_current",
    "news_brief_runs",
    "news_brief_publications",
    "news_brief_current",
}
LEGACY_NEWS_TABLES = {
    "news_fetch_runs",
    "news_provider_items",
    "news_item_entities",
    "news_token_mentions",
    "news_fact_candidates",
    "news_item_observation_edges",
    "news_projection_dirty_targets",
    "news_page_rows",
    "news_story_articles",
    "news_story_analyses",
    "news_story_analysis_attempts",
    "news_brief_selection_snapshots",
    "news_articles",
    "news_article_revisions",
    "news_story_memberships",
    "news_story_profiles",
    "news_story_identity_decisions",
    "news_story_material_events",
    "news_narrative_grouping_snapshots",
    "news_brief_selections",
    "news_brief_proposals",
    "news_brief_activations",
    "news_brief_active",
    "news_story_analysis_requests",
    "news_ai_attempts",
    "news_ai_current_targets",
    "news_brief_activation_analysis",
    "news_story_analysis_publications",
    "news_story_analysis_current",
}


def test_current_postgres_schema_has_one_kappa_truth_and_durable_macro_thesis(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        tables = {
            row["table_name"]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
            ).fetchall()
        }
        macro_thesis_run_columns = {
            row["column_name"]
            for row in conn.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'macro_thesis_runs'
                """
            ).fetchall()
        }
        macro_research_input_columns = {
            row["column_name"]
            for row in conn.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'macro_research_inputs'
                """
            ).fetchall()
        }
        news_source_columns = {
            row["column_name"]
            for row in conn.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'news_sources'
                """
            ).fetchall()
        }
        macro_thesis_publication_columns = {
            row["column_name"]
            for row in conn.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'macro_thesis_publications'
                """
            ).fetchall()
        }
        market_current_columns = {
            row["column_name"]
            for row in conn.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'market_tick_current'
                """
            ).fetchall()
        }
        market_settlement_columns = {
            row["column_name"]
            for row in conn.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'market_settlements'
                """
            ).fetchall()
        }
        asset_profile_refresh_target_columns = {
            row["column_name"]
            for row in conn.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'asset_profile_refresh_targets'
                """
            ).fetchall()
        }
        event_columns = {
            row["column_name"]
            for row in conn.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'events'
                """
            ).fetchall()
        }
        event_entity_columns = {
            row["column_name"]
            for row in conn.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'event_entities'
                """
            ).fetchall()
        }
        radar_feature_columns = {
            row["column_name"]
            for row in conn.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'token_radar_target_features'
                """
            ).fetchall()
        }
        radar_current_columns = {
            row["column_name"]
            for row in conn.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'token_radar_current_rows'
                """
            ).fetchall()
        }
        radar_publication_columns = {
            row["column_name"]
            for row in conn.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'token_radar_publication_state'
                """
            ).fetchall()
        }
        radar_first_seen_columns = {
            row["column_name"]
            for row in conn.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'token_radar_target_first_seen'
                """
            ).fetchall()
        }
        radar_frontier_columns = {
            row["column_name"]
            for row in conn.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'radar_projection_frontiers'
                """
            ).fetchall()
        }
        retired_projection_tables = {
            row["table_name"]
            for row in conn.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name = ANY(%s)
                """,
                (
                    [
                        "token_profile_current_dirty_targets",
                        "token_radar_dirty_targets",
                        "token_radar_rank_source_events",
                    ],
                ),
            ).fetchall()
        }
        performance_indexes = {
            row["indexname"]
            for row in conn.execute(
                """
                SELECT indexname
                FROM pg_indexes
                WHERE schemaname = 'public'
                  AND indexname IN (
                    'ix_news_source_fetches_source_time',
                    'idx_asset_identity_evidence_profile_source',
                    'idx_asset_identity_evidence_asset_provider_lookup'
                  )
                """
            ).fetchall()
        }
        projection_eligibility_indexes = {
            row["indexname"]
            for row in conn.execute(
                """
                    SELECT indexname
                    FROM pg_indexes
                    WHERE schemaname = 'public'
                      AND (
                        indexname LIKE '%frontiers_eligible'
                        OR indexname IN (
                          'idx_radar_projection_frontiers_microbatch_eligible',
                          'idx_radar_projection_frontiers_expired_claim'
                        )
                      )
                """
            ).fetchall()
        }
        event_reloptions = {
            str(row["option"])
            for row in conn.execute(
                """
                SELECT unnest(reloptions) AS option
                FROM pg_class
                WHERE oid = 'events'::regclass
                """
            ).fetchall()
        }
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"]
    finally:
        conn.close()

    assert {
        "raw_frames",
        "events",
        "token_intents",
        "token_intent_resolutions",
        "market_ticks",
        "enriched_events",
        "token_radar_current_rows",
        "token_radar_publication_state",
        "stock_attention_target_features",
        "stocks_radar_current_rows",
        "stocks_radar_publication_state",
        "market_instruments",
        "market_observations",
        "market_settlements",
        "market_position_facts",
        "macro_series_facts",
        "macro_release_facts",
        "macro_documents",
        "macro_source_receipts",
        "macro_acquisition_targets",
        "macro_feature_series",
        "macro_module_current",
        "macro_projection_state",
        "macro_evidence_packs",
        "macro_research_inputs",
        "macro_thesis_runs",
        "macro_thesis_reviews",
        "macro_thesis_publications",
        "macro_live_deltas",
        "macro_outcome_replays",
        "checkpoint_migrations",
        "checkpoints",
        "checkpoint_blobs",
        "checkpoint_writes",
        "workers_runtime",
        "queue_terminal_events",
    } <= tables
    assert {"worker_runtime_status", "model_generation_frontiers", "worker_queue_terminal_events"}.isdisjoint(tables)
    assert RETIRED_BACKEND_TABLES.isdisjoint(tables)
    assert tables >= PROFESSIONAL_NEWS_TABLES
    assert LEGACY_NEWS_TABLES.isdisjoint(tables)
    assert macro_thesis_run_columns == {
        "session_date",
        "cutoff_ms",
        "evidence_pack_id",
        "evidence_pack_hash",
        "research_input_id",
        "research_input_hash",
        "status",
        "attempt_count",
        "max_attempts",
        "due_at_ms",
        "leased_until_ms",
        "lease_owner",
        "publication_id",
        "last_error_code",
        "last_error_message",
        "last_gate_category",
        "last_candidate_hash",
        "created_at_ms",
        "updated_at_ms",
    }
    assert macro_research_input_columns == {
        "research_input_id",
        "evidence_pack_id",
        "session_date",
        "cutoff_ms",
        "schema_version",
        "profile_version",
        "prompt_version",
        "payload_json",
        "input_hash",
    }
    assert news_source_columns == {
        "source_id",
        "name",
        "feed_url",
        "tier",
        "lang",
        "enabled",
        "refresh_interval_seconds",
        "etag",
        "last_modified",
        "last_fetch_started_at_ms",
        "last_fetch_finished_at_ms",
        "last_success_at_ms",
        "last_http_status",
        "consecutive_failures",
        "last_error",
        "next_fetch_at_ms",
        "claim_token",
        "claim_lease_expires_at_ms",
        "created_at_ms",
        "updated_at_ms",
    }
    assert macro_thesis_publication_columns == {
        "publication_id",
        "session_date",
        "cutoff_ms",
        "evidence_pack_id",
        "schema_version",
        "thesis_json",
        "thesis_hash",
        "reviewer_invocation_id",
        "reviewer_draft_hash",
        "published_at_ms",
    }
    assert {"raw_payload_json", "payload_hash"}.isdisjoint(market_current_columns)
    assert {"fact_schema_version", "contract_expiration_date"} <= market_settlement_columns
    assert {"heat_tier", "terminal_reason"} <= asset_profile_refresh_target_columns
    assert {"matched_handles_json", "is_watched", "matched_at_ms"}.isdisjoint(event_columns)
    assert "is_watched" not in event_entity_columns
    assert {"scope", "social_heat_watched_mentions", "cohort_public_followup_authors"}.isdisjoint(radar_feature_columns)
    assert "cohort_followup_authors" in radar_feature_columns
    assert "scope" not in radar_current_columns
    assert "scope" not in radar_publication_columns
    assert "scope" not in radar_first_seen_columns
    assert {
        "claimed_input_fingerprint",
        "claimed_projection_version",
    } <= radar_frontier_columns
    assert {
        "pending_first_dirty_at_ms",
        "pending_deadline_at_ms",
        "pending_input_fingerprint",
        "pending_projection_version",
    }.isdisjoint(radar_frontier_columns)
    assert retired_projection_tables == set()
    assert performance_indexes == {
        "ix_news_source_fetches_source_time",
        "idx_asset_identity_evidence_profile_source",
        "idx_asset_identity_evidence_asset_provider_lookup",
    }
    assert projection_eligibility_indexes == {
        "idx_radar_projection_frontiers_microbatch_eligible",
        "idx_radar_projection_frontiers_expired_claim",
        "idx_macro_module_frontiers_eligible",
        "idx_news_projection_frontiers_eligible",
        "idx_token_profile_projection_frontiers_eligible",
    }
    assert event_reloptions == {
        "autovacuum_analyze_scale_factor=0.01",
        "autovacuum_analyze_threshold=10000",
    }
    assert version == latest_migration_version() == "20260731_0233"


def test_current_baseline_is_a_noop_for_an_already_current_database(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        before = {
            row["table_name"]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
            ).fetchall()
        }
        upgrade_head(_test_postgres_dsn())
        after = {
            row["table_name"]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
            ).fetchall()
        }
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"]
    finally:
        conn.close()

    assert after == before
    assert version == latest_migration_version() == "20260731_0233"


def test_projection_eligibility_migration_preserves_material_deadlines_and_schedules_rechecks(
    tmp_path,
) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    config = alembic_config()
    config.attributes["database_url"] = _test_postgres_dsn()
    try:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("GRANT ALL ON SCHEMA public TO public")
        conn.commit()
        command.upgrade(config, "20260731_0229")
        conn.execute(
            """
            INSERT INTO news_projection_frontiers(
              bucket_id, status, first_dirty_at_ms, deadline_at_ms,
              next_attempt_at_ms, attempt_count, transient_failure_count,
              active_item_count, input_fingerprint, projection_version,
              claimed_by, claimed_until_ms, last_error_code, updated_at_ms
            )
            VALUES
              (
                'identity:material', 'dirty', 1000, 61000,
                NULL, 0, 0, 1, 'material', 'news-v1',
                NULL, NULL, NULL, 1000
              ),
              (
                'identity:scheduled', 'dirty', 1000, 1000000,
                NULL, 0, 0, 1, 'scheduled', 'news-v1',
                NULL, NULL, NULL, 1000
              )
            """
        )
        conn.commit()

        command.upgrade(config, "head")

        rows = conn.execute(
            """
            SELECT bucket_id, deadline_at_ms, next_attempt_at_ms
            FROM news_projection_frontiers
            ORDER BY bucket_id
            """
        ).fetchall()
    finally:
        conn.close()

    assert rows == [
        {
            "bucket_id": "identity:material",
            "deadline_at_ms": 61_000,
            "next_attempt_at_ms": None,
        },
        {
            "bucket_id": "identity:scheduled",
            "deadline_at_ms": 1_060_000,
            "next_attempt_at_ms": 1_000_000,
        },
    ]


def test_news_score_bucket_migration_collapses_story_timer_fanout(
    tmp_path,
) -> None:
    conn = connect_postgres_test(
        tmp_path / "postgres_test_db",
        read_only=False,
    )
    config = alembic_config()
    config.attributes["database_url"] = _test_postgres_dsn()
    try:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("GRANT ALL ON SCHEMA public TO public")
        conn.commit()
        command.upgrade(config, "20260731_0230")
        conn.execute(
            """
            INSERT INTO news_sources(
              source_id, name, feed_url, tier, lang,
              refresh_interval_seconds, next_fetch_at_ms,
              created_at_ms, updated_at_ms
            )
            VALUES (
              'source-1', 'Source 1', 'https://example.com/feed',
              1, 'en', 60, 1, 1, 1
            );

            INSERT INTO news_items(
              item_id, source_id, source_item_key, canonical_url,
              reporting_origin, title, normalized_title, lang,
              published_at_ms, first_observed_at_ms,
              last_observed_at_ms, content_fingerprint,
              level, category, classification_source,
              classification_confidence, importance_score,
              importance_factors, created_at_ms, updated_at_ms
            )
            VALUES (
              'item-1', 'source-1', 'item-1',
              'https://example.com/item-1', 'source-1',
              'Test story', 'test story', 'en',
              1, 1, 1, 'fingerprint-1',
              'info', 'general', 'keyword', 1.0, 1,
              '{"total":1}'::jsonb, 1, 1
            );

            INSERT INTO news_stories(
              story_id, canonical_key, canonical_title,
              representative_item_id, representative_source_id,
              representative_title, representative_url,
              scoring_item_id, level, category, importance_score,
              importance_factors, item_count, source_count,
              first_published_at_ms, last_published_at_ms,
              state_fingerprint, created_at_ms, updated_at_ms
            )
            VALUES (
              'story-1', 'story-key-1', 'Test story',
              'item-1', 'source-1', 'Test story',
              'https://example.com/item-1', 'item-1',
              'info', 'general', 1, '{"total":1}'::jsonb,
              1, 1, 1, 1, 'story-fingerprint-1', 1, 1
            );

            INSERT INTO news_projection_frontiers(
              bucket_id, status, first_dirty_at_ms, deadline_at_ms,
              next_attempt_at_ms, attempt_count,
              transient_failure_count, active_item_count,
              input_fingerprint, projection_version,
              claimed_by, claimed_until_ms, last_error_code,
              updated_at_ms
            )
            VALUES (
              'score:story-1', 'dirty', 1, 61001,
              NULL, 0, 0, 1, 'old-score',
              'news-v1', NULL, NULL, NULL, 1
            )
            """
        )
        conn.commit()

        command.upgrade(config, "head")

        rows = conn.execute(
            """
            SELECT bucket_id, status, active_item_count,
                   deadline_at_ms - first_dirty_at_ms AS deadline_ms
            FROM news_projection_frontiers
            WHERE bucket_id LIKE 'score%'
            ORDER BY bucket_id
            """
        ).fetchall()
    finally:
        conn.close()

    assert rows == [
        {
            "bucket_id": "score-bucket:07",
            "status": "dirty",
            "active_item_count": 1,
            "deadline_ms": 60_000,
        }
    ]


def test_rates_curve_v6_migration_deletes_only_old_rates_projection_and_rejects_v5(
    tmp_path,
) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    config = alembic_config()
    config.attributes["database_url"] = _test_postgres_dsn()
    try:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("GRANT ALL ON SCHEMA public TO public")
        conn.commit()
        command.upgrade(config, "20260729_0216")
        conn.execute(
            """
            INSERT INTO macro_module_current (
              module_id, fact_cutoff_ms, payload_json, payload_hash,
              updated_at_ms, current_health_state, history_depth_state
            ) VALUES
            (
              'rates_fed', 1, '{"schema_version":"macro_rates_fed_v5"}'::jsonb,
              'sha256:old-rates', 1, 'current', 'not_required'
            ),
            (
              'credit', 1, '{"schema_version":"macro_credit_v7"}'::jsonb,
              'sha256:current-credit', 1, 'current', 'not_required'
            )
            """
        )
        conn.commit()

        command.upgrade(config, "head")

        assert [
            row["module_id"]
            for row in conn.execute("SELECT module_id FROM macro_module_current ORDER BY module_id").fetchall()
        ] == ["credit"]
        with pytest.raises(CheckViolation):
            conn.execute(
                """
                INSERT INTO macro_module_current (
                  module_id, fact_cutoff_ms, payload_json, payload_hash,
                  updated_at_ms, current_health_state, history_depth_state
                ) VALUES (
                  'rates_fed', 2, '{"schema_version":"macro_rates_fed_v5"}'::jsonb,
                  'sha256:stale-rates', 2, 'current', 'not_required'
                )
                """
            )
        conn.rollback()
        conn.execute(
            """
            INSERT INTO macro_module_current (
              module_id, fact_cutoff_ms, payload_json, payload_hash,
              updated_at_ms, current_health_state, history_depth_state
            ) VALUES (
              'rates_fed', 3, '{"schema_version":"macro_rates_fed_v6"}'::jsonb,
              'sha256:rates-v6', 3, 'current', 'not_required'
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def test_macro_reader_hard_cut_deletes_old_projections_and_requires_reprojection(
    tmp_path,
) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    config = alembic_config()
    config.attributes["database_url"] = _test_postgres_dsn()
    try:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("GRANT ALL ON SCHEMA public TO public")
        conn.commit()
        command.upgrade(config, "20260728_0213")
        conn.execute(
            """
            INSERT INTO macro_module_current (
              module_id,
              fact_cutoff_ms,
              payload_json,
              payload_hash,
              updated_at_ms,
              current_health_state,
              history_depth_state
            ) VALUES (
              'rates_fed',
              1,
              '{"schema_version":"macro_rates_fed_v4"}'::jsonb,
              'sha256:old',
              1,
              'current',
              'not_required'
            )
            """
        )
        conn.commit()

        command.upgrade(config, "20260729_0214")

        assert conn.execute("SELECT count(*) AS count FROM macro_module_current").fetchone()["count"] == 0
        with pytest.raises(CheckViolation):
            conn.execute(
                """
                INSERT INTO macro_module_current (
                  module_id,
                  fact_cutoff_ms,
                  payload_json,
                  payload_hash,
                  updated_at_ms,
                  current_health_state,
                  history_depth_state
                ) VALUES (
                  'rates_fed',
                  2,
                  '{"schema_version":"macro_rates_fed_v4"}'::jsonb,
                  'sha256:stale',
                  2,
                  'current',
                  'not_required'
                )
                """
            )
        conn.rollback()
        conn.execute(
            """
            INSERT INTO macro_module_current (
              module_id,
              fact_cutoff_ms,
              payload_json,
              payload_hash,
              updated_at_ms,
              current_health_state,
              history_depth_state
            ) VALUES (
              'rates_fed',
              3,
              '{"schema_version":"macro_rates_fed_v5"}'::jsonb,
              'sha256:reprojected',
              3,
              'current',
              'not_required'
            )
            """
        )
        conn.commit()
        assert (
            conn.execute("SELECT payload_json ->> 'schema_version' AS version FROM macro_module_current").fetchone()[
                "version"
            ]
            == "macro_rates_fed_v5"
        )
    finally:
        conn.close()


def test_macro_exact_schema_hard_cut_repairs_an_already_applied_reader_migration(
    tmp_path,
) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    config = alembic_config()
    config.attributes["database_url"] = _test_postgres_dsn()
    try:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("GRANT ALL ON SCHEMA public TO public")
        conn.commit()
        command.upgrade(config, "20260729_0214")
        conn.execute(
            """
            ALTER TABLE macro_module_current
              DROP CONSTRAINT macro_module_current_typed_schema_check;
            ALTER TABLE macro_module_current
              ADD CONSTRAINT macro_module_current_typed_schema_check
              CHECK (
                payload_json ->> 'schema_version' = CASE module_id
                  WHEN 'rates_fed' THEN 'macro_rates_fed_v5'
                  WHEN 'economy_inflation' THEN 'macro_economy_inflation_v5'
                  WHEN 'liquidity_funding' THEN 'macro_liquidity_funding_v5'
                  WHEN 'credit' THEN 'macro_credit_v6'
                  WHEN 'volatility' THEN 'macro_volatility_v5'
                  WHEN 'cross_asset' THEN 'macro_cross_asset_v6'
                  ELSE NULL
                END
              );
            INSERT INTO macro_module_current (
              module_id,
              fact_cutoff_ms,
              payload_json,
              payload_hash,
              updated_at_ms,
              current_health_state,
              history_depth_state
            ) VALUES (
              'credit',
              1,
              '{"schema_version":"macro_credit_v6"}'::jsonb,
              'sha256:old-credit',
              1,
              'current',
              'not_required'
            );
            """
        )
        conn.commit()

        command.upgrade(config, "head")

        assert conn.execute("SELECT count(*) AS count FROM macro_module_current").fetchone()["count"] == 0
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"]
        assert version == "20260731_0233"
        with pytest.raises(CheckViolation):
            conn.execute(
                """
                INSERT INTO macro_module_current (
                  module_id,
                  fact_cutoff_ms,
                  payload_json,
                  payload_hash,
                  updated_at_ms,
                  current_health_state,
                  history_depth_state
                ) VALUES (
                  'credit',
                  2,
                  '{"schema_version":"macro_credit_v6"}'::jsonb,
                  'sha256:stale-credit',
                  2,
                  'current',
                  'not_required'
                )
                """
            )
        conn.rollback()
        conn.execute(
            """
            INSERT INTO macro_module_current (
              module_id,
              fact_cutoff_ms,
              payload_json,
              payload_hash,
              updated_at_ms,
              current_health_state,
              history_depth_state
            ) VALUES (
              'credit',
              3,
              '{"schema_version":"macro_credit_v7"}'::jsonb,
              'sha256:exact-credit',
              3,
              'current',
              'not_required'
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def test_macro_thin_v2_migration_appends_only_provable_cfe_revisions(
    tmp_path,
) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    config = alembic_config()
    config.attributes["database_url"] = _test_postgres_dsn()
    try:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("GRANT ALL ON SCHEMA public TO public")
        conn.commit()
        command.upgrade(config, "20260729_0215")
        conn.execute(
            """
            INSERT INTO market_instruments(
              instrument_id, symbol, name, asset_class, instrument_type,
              venue, currency, price_unit, source_metadata_json, created_at_ms
            ) VALUES (
              'cfe.vx', 'VX', 'CFE VIX futures', 'volatility', 'future',
              'CFE', 'USD', 'index_points', '{}'::jsonb, 1
            )
            """
        )
        conn.execute(
            """
            INSERT INTO market_settlements(
              settlement_id, instrument_id, dataset_id, source_id, trade_date,
              contract_code, settlement_price, open_interest, volume, unit,
              published_at_ms, received_at_ms, source_url, fact_hash, raw_data_json
            ) VALUES
            (
              'legacy-provable', 'cfe.vx', 'cboe.cfe.vx.settlement', 'cfe',
              '2026-07-28', 'VXQ6', 18.25, 100, 50, 'index_points',
              10, 20, 'https://www.cboe.com/', 'sha256:legacy-provable',
              '{"Expiration Date":"2026-08-19","Contract":"VXQ6"}'::jsonb
            ),
            (
              'legacy-unprovable', 'cfe.vx', 'cboe.cfe.vx.settlement', 'cfe',
              '2026-07-28', 'VXU6', 19.10, 90, 40, 'index_points',
              10, 20, 'https://www.cboe.com/', 'sha256:legacy-unprovable',
              '{"Contract":"VXU6"}'::jsonb
            )
            """
        )
        conn.commit()

        command.upgrade(config, "head")

        facts = conn.execute(
            """
            SELECT settlement_id, contract_code, fact_schema_version,
                   contract_expiration_date, fact_hash
            FROM market_settlements
            ORDER BY fact_schema_version, contract_code
            """
        ).fetchall()
        served = GeneralMarketRepository(conn).settlement_history(dataset_ids=("cboe.cfe.vx.settlement",))
    finally:
        conn.close()

    assert len(facts) == 3
    assert {
        (row["settlement_id"], row["fact_schema_version"])
        for row in facts
        if row["fact_schema_version"] == "market_settlement_v1"
    } == {
        ("legacy-provable", "market_settlement_v1"),
        ("legacy-unprovable", "market_settlement_v1"),
    }
    v2 = next(row for row in facts if row["fact_schema_version"] == "market_settlement_v2")
    assert v2["settlement_id"] != "legacy-provable"
    assert v2["fact_hash"] != "sha256:legacy-provable"
    assert str(v2["contract_expiration_date"]) == "2026-08-19"
    assert [row["contract_code"] for row in served] == ["VXQ6"]


def test_macro_thin_v2_migration_preserves_v1_and_cleans_only_macro_control_state(
    tmp_path,
) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    config = alembic_config()
    config.attributes["database_url"] = _test_postgres_dsn()
    try:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("GRANT ALL ON SCHEMA public TO public")
        conn.commit()
        command.upgrade(config, "20260729_0215")
        conn.execute(
            """
            INSERT INTO macro_evidence_packs(
              evidence_pack_id, session_date, cutoff_ms, sealed_at_ms,
              source_max_received_at_ms, schema_version, payload_json, payload_hash
            ) VALUES (
              'mep3_migration_fixture', '2026-07-28', 1000, 1000, 1000,
              'macro_evidence_pack_v3', '{"fixture":"v1"}'::jsonb, 'sha256:v1-pack'
            );
            INSERT INTO macro_thesis_runs(
              session_date, cutoff_ms, evidence_pack_id, evidence_pack_hash,
              status, attempt_count, max_attempts, due_at_ms,
              created_at_ms, updated_at_ms
            ) VALUES (
              '2026-07-28', 1000, 'mep3_migration_fixture', 'sha256:v1-pack',
              'pending', 0, 2, 1000, 1000, 1000
            );
            UPDATE macro_thesis_runs
            SET status = 'running',
                attempt_count = 1,
                leased_until_ms = 9999999999999,
                lease_owner = 'migration-fixture-owner',
                updated_at_ms = 1100
            WHERE session_date = '2026-07-28';
            INSERT INTO macro_thesis_reviews(
              review_id, session_date, review_sequence, draft_hash, disposition,
              review_json, invocation_id, model_name, prompt_version, created_at_ms
            ) VALUES (
              'review-v1-fixture', '2026-07-28', 1, 'sha256:v1-draft', 'pass',
              '{"disposition":"pass"}'::jsonb, 'review-invocation-v1',
              'legacy-model', 'legacy-prompt', 1200
            );
            INSERT INTO macro_thesis_publications(
              publication_id, session_date, cutoff_ms, evidence_pack_id,
              schema_version, thesis_json, thesis_hash, reviewer_invocation_id,
              reviewer_draft_hash, published_at_ms
            ) VALUES (
              'publication-v1-fixture', '2026-07-28', 1000, 'mep3_migration_fixture',
              'macro_thesis_v1', '{"nested":{"b":2,"a":1},"schema_version":"macro_thesis_v1"}'::jsonb,
              'sha256:v1-thesis', 'review-invocation-v1', 'sha256:v1-draft', 1300
            );
            UPDATE macro_thesis_runs
            SET status = 'published',
                publication_id = 'publication-v1-fixture',
                leased_until_ms = NULL,
                lease_owner = NULL,
                updated_at_ms = 1300
            WHERE session_date = '2026-07-28';

            INSERT INTO macro_evidence_packs(
              evidence_pack_id, session_date, cutoff_ms, sealed_at_ms,
              source_max_received_at_ms, schema_version, payload_json, payload_hash
            ) VALUES (
              'mep3_active_lease_fixture', '2026-07-29', 1000, 1000, 1000,
              'macro_evidence_pack_v3', '{"fixture":"active-lease"}'::jsonb,
              'sha256:active-lease-pack'
            );
            INSERT INTO macro_thesis_runs(
              session_date, cutoff_ms, evidence_pack_id, evidence_pack_hash,
              status, attempt_count, max_attempts, due_at_ms,
              created_at_ms, updated_at_ms
            ) VALUES (
              '2026-07-29', 1000, 'mep3_active_lease_fixture',
              'sha256:active-lease-pack', 'pending', 0, 2, 1000, 1000, 1000
            );
            UPDATE macro_thesis_runs
            SET status = 'running',
                attempt_count = 1,
                leased_until_ms = 9999999999999,
                lease_owner = 'active-cutover-owner',
                updated_at_ms = 1400
            WHERE session_date = '2026-07-29';

            INSERT INTO checkpoints(
              thread_id, checkpoint_ns, checkpoint_id, checkpoint, metadata
            ) VALUES
              ('research:mep3_fixture', '', 'macro-checkpoint', '{"v":1}'::jsonb, '{}'::jsonb),
              ('other:workflow', '', 'other-checkpoint', '{"v":1}'::jsonb, '{}'::jsonb);
            INSERT INTO checkpoint_writes(
              thread_id, checkpoint_ns, checkpoint_id, task_id, idx,
              channel, type, blob, task_path
            ) VALUES
              ('review:mep3_fixture', '', 'macro-checkpoint', 'task', 0,
               'messages', 'bytes', decode('00', 'hex'), ''),
              ('other:workflow', '', 'other-checkpoint', 'task', 0,
               'messages', 'bytes', decode('00', 'hex'), '');
            INSERT INTO checkpoint_blobs(
              thread_id, checkpoint_ns, channel, version, type, blob
            ) VALUES
              ('research:mep3_fixture', '', 'messages', '1', 'bytes', decode('00', 'hex')),
              ('other:workflow', '', 'messages', '1', 'bytes', decode('00', 'hex'));
            """
        )
        conn.commit()
        before = conn.execute(
            """
            SELECT encode(convert_to(thesis_json::text, 'UTF8'), 'hex') AS payload_bytes,
                   thesis_hash, reviewer_invocation_id, reviewer_draft_hash
            FROM macro_thesis_publications
            WHERE publication_id = 'publication-v1-fixture'
            """
        ).fetchone()

        with pytest.raises(ProgrammingError, match="macro_thesis_active_lease_blocks_v2_cutover"):
            command.upgrade(config, "head")
        conn.rollback()

        conn.execute(
            """
            UPDATE macro_thesis_runs
            SET leased_until_ms = 0,
                updated_at_ms = 1500
            WHERE session_date = '2026-07-29'
            """
        )
        conn.commit()
        command.upgrade(config, "head")

        after = conn.execute(
            """
            SELECT encode(convert_to(thesis_json::text, 'UTF8'), 'hex') AS payload_bytes,
                   thesis_hash, reviewer_invocation_id, reviewer_draft_hash
            FROM macro_thesis_publications
            WHERE publication_id = 'publication-v1-fixture'
            """
        ).fetchone()
        assert after == before
        assert (
            conn.execute(
                """
                SELECT count(*) AS count
                FROM checkpoints
                WHERE thread_id LIKE 'research:mep3_%'
                   OR thread_id LIKE 'review:mep3_%'
                """
            ).fetchone()["count"]
            == 0
        )
        assert (
            conn.execute(
                """
                SELECT count(*) AS count
                FROM checkpoint_writes
                WHERE thread_id LIKE 'research:mep3_%'
                   OR thread_id LIKE 'review:mep3_%'
                """
            ).fetchone()["count"]
            == 0
        )
        assert (
            conn.execute(
                """
                SELECT count(*) AS count
                FROM checkpoint_blobs
                WHERE thread_id LIKE 'research:mep3_%'
                   OR thread_id LIKE 'review:mep3_%'
                """
            ).fetchone()["count"]
            == 0
        )
        for table in ("checkpoints", "checkpoint_writes", "checkpoint_blobs"):
            assert (
                conn.execute(f"SELECT count(*) AS count FROM {table} WHERE thread_id = 'other:workflow'").fetchone()[
                    "count"
                ]
                == 1
            )
    finally:
        conn.close()
