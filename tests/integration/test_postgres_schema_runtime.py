from __future__ import annotations

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tests.postgres_test_utils import test_postgres_dsn as _test_postgres_dsn
from tracefold.platform.postgres.postgres_audit import NEWS_TABLES
from tracefold.platform.postgres.postgres_migrations import (
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
    "stock_attention_target_features",
    "stocks_radar_current_rows",
    "stocks_radar_publication_state",
    "token_radar_source_dirty_events",
    "market_tick_current_dirty_targets",
    "token_capture_tier_dirty_targets",
    "token_capture_tier",
    "news_story_agent_briefs",
    "news_story_agent_runs",
}
PROFESSIONAL_NEWS_TABLES = set(NEWS_TABLES)
LEGACY_NEWS_TABLES = {
    "news_sources",
    "news_stories",
    "news_story_members",
    "news_projection_summary",
    "news_brief_selection_current",
    "news_brief_current",
    "news_item_title_presentations",
    "news_push_state",
    "news_push_deliveries",
    "news_story_facet_counts",
    "news_source_facet_counts",
    "news_brief_runs",
    "news_brief_publications",
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
RETIRED_MACRO_RESEARCH_TABLES = {
    "macro_live_deltas",
    "macro_outcome_replays",
    "macro_thesis_publications",
    "macro_thesis_reviews",
    "macro_thesis_runs",
    "macro_research_inputs",
    "macro_evidence_packs",
    "macro_source_receipts",
    "macro_feature_series",
    "macro_projection_state",
}


def test_current_postgres_schema_has_macro_facts_and_six_current_modules(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        tables = {
            row["table_name"]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
            ).fetchall()
        }
        news_event_columns = {
            row["column_name"]
            for row in conn.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'news_events'
                """
            ).fetchall()
        }
        news_delivery_columns = {
            row["column_name"]
            for row in conn.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'news_deliveries'
                """
            ).fetchall()
        }
        news_control_columns = {
            row["column_name"]
            for row in conn.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'news_control_state'
                """
            ).fetchall()
        }
        news_ingest_columns = {
            row["column_name"]
            for row in conn.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'news_ingest_state'
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
                  AND table_name = 'token_radar_current'
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
                        "token_profile_current",
                        "token_profile_projection_frontiers",
                        "asset_profiles",
                        "asset_profile_refresh_targets",
                        "token_image_assets",
                        "token_image_source_dirty_targets",
                        "token_discovery_results",
                        "token_discovery_dirty_lookup_keys",
                        "cex_token_profiles",
                        "checkpoints",
                        "checkpoint_writes",
                        "checkpoint_blobs",
                        "checkpoint_migrations",
                        "token_radar_current",
                        "token_radar_dirty_targets",
                        "token_radar_rank_source_events",
                        "token_radar_current_rows",
                        "token_radar_publication_state",
                        "token_radar_target_features",
                        "token_radar_target_first_seen",
                        "radar_projection_frontiers",
                        "radar_source_edges",
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
                    'idx_asset_identity_evidence_profile_source',
                    'idx_asset_identity_evidence_asset_provider_lookup',
                    'idx_market_observations_projection_history',
                    'ix_news_events_opened',
                    'ix_news_events_search',
                    'ix_news_event_bands_lookup'
                  )
                """
            ).fetchall()
        }
        resolution_lookup_index = conn.execute(
            """
            SELECT indexdef
            FROM pg_indexes
            WHERE schemaname = 'public'
              AND indexname = 'idx_token_intent_lookup_keys_intent_lookup'
            """
        ).fetchone()
        radar_event_index = conn.execute(
            """
            SELECT indexdef
            FROM pg_indexes
            WHERE schemaname = 'public'
              AND indexname = 'idx_events_token_radar_source_time'
            """
        ).fetchone()
        macro_market_projection_index = conn.execute(
            """
            SELECT indexdef
            FROM pg_indexes
            WHERE schemaname = 'public'
              AND indexname = 'idx_market_observations_projection_history'
            """
        ).fetchone()
        radar_fingerprint_column = conn.execute(
            """
            SELECT is_generated, generation_expression
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'events'
              AND column_name = 'token_radar_text_fingerprint'
            """
        ).fetchone()
        radar_resolution_index = conn.execute(
            """
            SELECT indexdef
            FROM pg_indexes
            WHERE schemaname = 'public'
              AND indexname = 'idx_token_intent_resolutions_token_radar_material'
            """
        ).fetchone()
        news_member_score_index = conn.execute(
            """
            SELECT indexdef
            FROM pg_indexes
            WHERE schemaname = 'public'
              AND indexname = 'ix_news_items_member_provider_score'
            """
        ).fetchone()
        news_v3_indexes = {
            str(row["indexname"]): str(row["indexdef"])
            for row in conn.execute(
                """
                SELECT indexname, indexdef
                  FROM pg_indexes
                 WHERE schemaname = 'public'
                   AND indexname LIKE 'ix_news_%%'
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
        terminal_owner_constraint = conn.execute(
            """
            SELECT pg_get_constraintdef(oid) AS definition
              FROM pg_constraint
             WHERE conrelid = 'queue_terminal_events'::regclass
               AND conname = 'queue_terminal_events_owner_key_check'
            """
        ).fetchone()
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
        "market_instruments",
        "market_observations",
        "market_settlements",
        "market_position_facts",
        "macro_series_facts",
        "macro_release_facts",
        "macro_documents",
        "macro_fed_official_role_facts",
        "macro_document_analyses",
        "macro_document_analysis_jobs",
        "macro_acquisition_targets",
        "macro_dataset_projection_states",
        "macro_module_frontiers",
        "macro_module_current",
        "workers_runtime",
        "queue_terminal_events",
    } <= tables
    assert {"worker_runtime_status", "model_generation_frontiers", "worker_queue_terminal_events"}.isdisjoint(tables)
    assert RETIRED_BACKEND_TABLES.isdisjoint(tables)
    assert RETIRED_MACRO_RESEARCH_TABLES.isdisjoint(tables)
    assert {table for table in tables if table.startswith("news_")} == PROFESSIONAL_NEWS_TABLES
    assert LEGACY_NEWS_TABLES.isdisjoint(tables)
    assert {
        "event_id",
        "family",
        "leader_item_id",
        "leader_title",
        "comparison_fingerprint",
        "storyline_key",
        "admission",
        "priority",
        "opened_at_ms",
        "published_at_ms",
        "ingest_mode",
        "search_doc",
    } <= news_event_columns
    assert {"story_id", "canonical_key", "facet_facts", "identity_evidence"}.isdisjoint(news_event_columns)
    assert news_delivery_columns == {
        "event_id",
        "kind",
        "state",
        "card",
        "receipt",
        "error_code",
        "attempted_at_ms",
        "settled_at_ms",
        "created_at_ms",
    }
    assert news_control_columns == {"singleton_key", "paused", "mutes", "updated_at_ms"}
    assert news_ingest_columns == {
        "singleton_key",
        "connected",
        "last_frame_at_ms",
        "last_publish_at_ms",
        "last_error_code",
        "configured_strategy_ids",
        "provider_enabled_strategy_ids",
        "strategy_warnings",
        "broker_snapshot",
        "updated_at_ms",
    }
    assert {"raw_payload_json", "payload_hash"}.isdisjoint(market_current_columns)
    assert {"fact_schema_version", "contract_expiration_date"} <= market_settlement_columns
    assert {"matched_handles_json", "is_watched", "matched_at_ms"}.isdisjoint(event_columns)
    assert "is_watched" not in event_entity_columns
    assert radar_feature_columns == set()
    assert radar_publication_columns == set()
    assert radar_first_seen_columns == set()
    assert radar_frontier_columns == set()
    assert radar_current_columns == set()
    assert retired_projection_tables == set()
    assert performance_indexes == {
        "idx_asset_identity_evidence_profile_source",
        "idx_asset_identity_evidence_asset_provider_lookup",
        "idx_market_observations_projection_history",
        "ix_news_events_opened",
        "ix_news_events_search",
        "ix_news_event_bands_lookup",
    }
    assert resolution_lookup_index is not None
    assert "INCLUDE (event_id)" in resolution_lookup_index["indexdef"]
    assert radar_event_index is None
    assert macro_market_projection_index is not None
    assert (
        "(dataset_id, ((observed_at_ms / 86400000)) DESC, observed_at_ms DESC, "
        "received_at_ms DESC, observation_id DESC)" in macro_market_projection_index["indexdef"]
    )
    assert (
        "INCLUDE (instrument_id, source_id, field_name, value_numeric, unit, "
        "published_at_ms, trust_tier, source_url, fact_hash)" in macro_market_projection_index["indexdef"]
    )
    assert radar_fingerprint_column is None
    assert radar_resolution_index is None
    assert news_member_score_index is None
    assert {
        "ix_news_incidents_open",
        "ix_news_incidents_recovery",
        "ix_news_items_published",
        "ix_news_events_opened",
        "ix_news_events_admission",
        "ix_news_events_expires",
        "ix_news_events_storyline",
        "ix_news_events_fingerprint",
        "ix_news_events_search",
        "ix_news_events_unpublished",
        "ix_news_event_members_item",
        "ix_news_event_bands_lookup",
        "ix_news_event_bands_expires",
        "ix_news_event_assets_symbol",
        "ix_news_verdicts_stage_created",
        "ix_news_verdicts_final",
        "ix_news_deliveries_state",
        "ix_news_deliveries_sent",
        "ix_news_marks_due",
    } <= set(news_v3_indexes)
    retired_index_prefixes = ("ix_news_push_", "ix_news_item_title_", "ix_news_stories")
    assert not any(name.startswith(retired_index_prefixes) for name in news_v3_indexes)
    assert "state = 'sent'" in news_v3_indexes["ix_news_deliveries_sent"]
    assert "gin" in news_v3_indexes["ix_news_events_search"].lower()
    assert projection_eligibility_indexes == {"idx_macro_module_frontiers_eligible"}
    assert event_reloptions == {
        "autovacuum_analyze_scale_factor=0.01",
        "autovacuum_analyze_threshold=10000",
        "autovacuum_vacuum_insert_scale_factor=0.01",
        "autovacuum_vacuum_insert_threshold=10000",
        "autovacuum_vacuum_scale_factor=0.01",
        "autovacuum_vacuum_threshold=10000",
    }
    assert terminal_owner_constraint is not None
    assert "radar_projection" not in terminal_owner_constraint["definition"]
    assert version == latest_migration_version() == "20260818_0276"


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
    assert version == latest_migration_version() == "20260818_0276"
