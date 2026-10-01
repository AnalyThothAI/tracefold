from __future__ import annotations

import pytest

from tests.postgres_test_utils import connect_postgres_test, postgres_migration_test_dsn
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tracefold.platform.postgres.audit import NEWS_TABLES, TRADING_TABLES
from tracefold.platform.postgres.maintenance_gate import acquire_steady_gate, release_steady_gate
from tracefold.platform.postgres.migrations import (
    latest_migration_version,
    upgrade_head,
)

pytestmark = [pytest.mark.integration, pytest.mark.migration, pytest.mark.usefixtures("postgres_migration_dsn")]


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
        news_delivery_columns = columns("news_notifications")
        semantic_work_columns = columns("news_jobs")
        semantic_observation_columns = columns("news_analyses")
        event_update_columns = columns("news_analyses")
        scope_repair_columns = columns("news_analyses")
        delivery_queue_columns = columns("news_notifications")
        news_ingest_columns = columns("news_collectors")
        news_v3_indexes = {
            str(row["indexname"]): str(row["indexdef"])
            for row in conn.execute(
                "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' "
                "AND (indexname LIKE 'ix_news_%%' OR indexname LIKE 'news_%%')"
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
        *NEWS_TABLES,
        # #104: the Trading bounded context's own five tables. Registered separately from
        # `NEWS_TABLES` so "exactly these tables" stays a per-capability claim.
        *TRADING_TABLES,
    }
    assert {
        "news_analysis_immutable",
        "news_notifications_guard",
    } <= functions
    assert "news_current_triage_verdict_valid" not in functions
    assert "purge_news_learning_retention" not in functions
    assert "news_strategy_provenance_valid" not in functions
    assert {
        "event_id",
        "dedupe_family",
        "event_kind",
        "leader_item_id",
        "leader_title",
        "comparison_fingerprint",
        "storyline_key",
        "admission",
        "queue_priority",
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
    assert "family" not in news_event_columns
    assert "followup_of" not in news_event_columns
    assert "current_contract_archive_only" not in news_event_columns
    assert "news_current_event_archive_guard" not in functions
    assert {"job_kind", "subject_id", "detail", "lease_until_ms"} <= semantic_work_columns
    assert {"read_refs", "reanalysis_reason", "reanalysis_head_ref", "input_manifest"} <= semantic_observation_columns
    assert {"analysis_id", "origin", "content_revision", "document", "adopted_at_ms"} <= event_update_columns
    assert "repair" in scope_repair_columns
    assert {"dedupe_bands", "evidence", "evidence_version", "current_analysis_id"} <= news_event_columns
    assert "source_contract_reason" not in news_event_columns
    assert {
        "news_current_evidence_snapshot_valid",
        "reject_news_event_evidence_mutation",
        "news_jsonb_exact_keys",
        "news_jsonb_required_optional_keys",
        "news_jsonb_int64_valid",
        "news_identity",
    }.isdisjoint(functions)
    assert {"card_copy_input_digest", "card_copy_document", "settlement"} <= delivery_queue_columns
    assert news_delivery_columns >= {
        "history_context",
        "event_id",
        "kind",
        "state",
        "card",
        "receipt",
        "settlement",
        "error_code",
        "attempted_at_ms",
        "settled_at_ms",
        "created_at_ms",
        "edit_state",
        "pending_card",
        "edit_error_code",
        "edit_attempted_at_ms",
        "edit_settled_at_ms",
        "sent_claims",
        # #706: the intent identity, and the exact frozen selection/body an update intent sent.
        "intent_id",
        "content_revision",
        "claim_refs",
        "plan_key",
        "card_copy_input_digest",
        "card_copy_document",
    }
    assert news_ingest_columns == {"collector_id", "state", "incidents", "updated_at_ms"}
    assert {
        "news_events_dedupe_bands",
        "news_events_bands_expiry",
        "news_events_current_analysis",
        "news_analyses_update_ref",
        "news_analyses_previous_refs",
        "news_analyses_current_refs",
        "news_jobs_semantic_lineage",
        "ix_news_items_published",
        "ix_news_events_opened",
        "ix_news_events_kind_opened",
        "ix_news_events_admission",
        "ix_news_events_expires",
        "ix_news_events_fingerprint",
        "ix_news_events_search",
        "ix_news_event_members_item",
        "ix_news_event_assets_event",
        "ix_news_event_assets_symbol",
        "ix_news_event_assets_retrieval_symbol",
        "ix_news_event_assets_retrieval_pair_base",
        "ix_news_event_members_fact_trgm",
        "ix_news_events_leader_item",
        "ix_news_items_canonical_url",
    } <= set(news_v3_indexes)
    assert "sent" in news_v3_indexes["news_notifications_sent"]
    assert "gin" in news_v3_indexes["ix_news_events_search"].lower()
    assert "event_kind, opened_at_ms DESC, event_id DESC" in news_v3_indexes["ix_news_events_kind_opened"]
    # Semantic jobs own rescue selection; published_at_ms is updated by Event primary key.
    assert "ix_news_events_unpublished" not in news_v3_indexes
    assert version == latest_migration_version() == "20261001_0423"


def test_current_head_is_a_noop_for_an_already_current_database(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        before = {
            row["table_name"]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
            ).fetchall()
        }
        upgrade_head(postgres_migration_test_dsn())
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
    assert version == latest_migration_version() == "20261001_0423"


def test_fresh_baseline_contains_only_current_structural_seeds(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        ingest = conn.execute(
            "SELECT collector_id, updated_at_ms FROM news_collectors ORDER BY collector_id"
        ).fetchall()
    finally:
        conn.close()

    assert [row["collector_id"] for row in ingest] == ["chain_tape", "instrument_catalog", "opennews", "wallet_roster"]
    # A fresh install used to arrive with a `PAUSED` runtime row and three blacklisted symbols. Both
    # seeds belonged to tables `20260901_0347` dropped, so the only structural seeds left are News's.


def test_migration_refuses_to_run_while_the_steady_runtime_holds_the_gate(tmp_path) -> None:
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        migrate(conn)
        acquire_steady_gate(conn)
        with pytest.raises(RuntimeError, match="steady_workers_runtime_active"):
            upgrade_head(postgres_migration_test_dsn())
    finally:
        release_steady_gate(conn)
        conn.close()
