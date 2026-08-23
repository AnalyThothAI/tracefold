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


def test_current_postgres_schema_is_news_v3_only(tmp_path) -> None:
    """After #68 the schema is exactly the News V3 tables plus alembic_version and workers_runtime."""

    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        tables = {
            row["table_name"]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables"
                " WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
            ).fetchall()
        }

        def columns(table: str) -> set[str]:
            return {
                row["column_name"]
                for row in conn.execute(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_schema = 'public' AND table_name = %s",
                    (table,),
                ).fetchall()
            }

        news_event_columns = columns("news_events")
        news_delivery_columns = columns("news_deliveries")
        news_ingest_columns = columns("news_ingest_state")
        news_v3_indexes = {
            str(row["indexname"]): str(row["indexdef"])
            for row in conn.execute(
                "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' AND indexname LIKE 'ix_news_%%'"
            ).fetchall()
        }
        functions = {
            row["proname"]
            for row in conn.execute(
                "SELECT proname FROM pg_proc WHERE pronamespace = 'public'::regnamespace"
            ).fetchall()
        }
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"]
    finally:
        conn.close()

    assert tables == {
        "alembic_version",
        "workers_runtime",
        *PROFESSIONAL_NEWS_TABLES,
    }
    assert RETIRED_BACKEND_TABLES.isdisjoint(tables)
    assert RETIRED_MACRO_RESEARCH_TABLES.isdisjoint(tables)
    assert LEGACY_NEWS_TABLES.isdisjoint(tables)
    assert {
        "news_strategy_provenance_valid",
        "reject_news_event_evidence_mutation",
        "reject_news_review_mutation",
    } <= functions
    assert {"forbid_market_fact_update", "reject_macro_fact_mutation"}.isdisjoint(functions)
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
        "focus_fact_id",
        "focus_fact_text",
        "focus_fact_context",
        "focus_fact_method",
        "focus_span_start",
        "focus_span_end",
    } <= news_event_columns
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
    assert news_ingest_columns == {
        "singleton_key",
        "connected",
        "last_frame_at_ms",
        "last_publish_at_ms",
        "last_error_code",
        "broker_snapshot",
        "updated_at_ms",
    }
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
        "ix_news_event_evidence_created",
        "ix_news_external_miss_created",
        "ix_news_reviews_event_created",
        "ix_news_reviews_task_created",
    } <= set(news_v3_indexes)
    assert "ix_news_marks_due" not in news_v3_indexes
    assert "state = 'sent'" in news_v3_indexes["ix_news_deliveries_sent"]
    assert "gin" in news_v3_indexes["ix_news_events_search"].lower()
    # The Janitor's rescue index must cover every admitted admission, not just `candidate`: a partial index on
    # `candidate` alone left crashed-before-publish listing Events unrecoverable (#72).
    unpublished_index = news_v3_indexes["ix_news_events_unpublished"]
    assert "published_at_ms IS NULL" in unpublished_index
    assert "'candidate'" in unpublished_index and "'listing_deterministic'" in unpublished_index
    assert version == latest_migration_version() == "20260823_0299"


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
    assert version == latest_migration_version() == "20260823_0299"
