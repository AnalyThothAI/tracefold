from __future__ import annotations

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tests.postgres_test_utils import test_postgres_dsn as _test_postgres_dsn
from tracefold.platform.postgres.postgres_migrations import latest_migration_version, upgrade_head

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


def test_current_postgres_schema_has_one_kappa_truth_and_durable_macro_research(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        tables = {
            row["table_name"]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
            ).fetchall()
        }
        macro_research_run_columns = {
            row["column_name"]
            for row in conn.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'macro_research_runs'
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
        macro_research_publication_columns = {
            row["column_name"]
            for row in conn.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'macro_research_publications'
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
        radar_rank_source_columns = {
            row["column_name"]
            for row in conn.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'token_radar_rank_source_events'
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
        "macro_evidence_packs",
        "macro_daily_judgments",
        "macro_event_updates",
        "macro_research_runs",
        "macro_research_publications",
        "checkpoint_migrations",
        "checkpoints",
        "checkpoint_blobs",
        "checkpoint_writes",
    } <= tables
    assert RETIRED_BACKEND_TABLES.isdisjoint(tables)
    assert tables >= PROFESSIONAL_NEWS_TABLES
    assert LEGACY_NEWS_TABLES.isdisjoint(tables)
    assert macro_research_run_columns == {
        "session_date",
        "market_cutoff_ms",
        "evidence_pack_id",
        "status",
        "sealed_at_ms",
        "attempt_count",
        "max_attempts",
        "due_at_ms",
        "leased_until_ms",
        "lease_owner",
        "reviewer_disposition",
        "last_error_code",
        "last_error_message",
        "created_at_ms",
        "updated_at_ms",
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
        "created_at_ms",
        "updated_at_ms",
    }
    assert macro_research_publication_columns == {
        "session_date",
        "market_cutoff_ms",
        "evidence_pack_id",
        "artifact_json",
        "report_markdown",
        "audit_json",
        "reviewer_disposition",
        "model_name",
        "prompt_version",
        "workflow_version",
        "artifact_hash",
        "published_at_ms",
    }
    assert {"raw_payload_json", "payload_hash"}.isdisjoint(market_current_columns)
    assert {"matched_handles_json", "is_watched", "matched_at_ms"}.isdisjoint(event_columns)
    assert "is_watched" not in event_entity_columns
    assert {"scope", "social_heat_watched_mentions", "cohort_public_followup_authors"}.isdisjoint(radar_feature_columns)
    assert "cohort_followup_authors" in radar_feature_columns
    assert "scope" not in radar_current_columns
    assert "scope" not in radar_publication_columns
    assert "scope" not in radar_first_seen_columns
    assert "is_watched" not in radar_rank_source_columns
    assert version == latest_migration_version() == "20260728_0210"


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
    assert version == latest_migration_version() == "20260728_0210"
