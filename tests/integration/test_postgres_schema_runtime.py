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
        semantic_work_columns = columns("news_semantic_work")
        semantic_observation_columns = columns("news_semantic_observations")
        event_update_columns = columns("news_event_updates")
        scope_repair_columns = columns("news_head_scope_repairs")
        delivery_queue_columns = columns("news_notifications")
        news_ingest_columns = columns("news_collectors")
        news_v3_indexes = {
            str(row["indexname"]): str(row["indexdef"])
            for row in conn.execute(
                "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' "
                "AND (indexname LIKE 'ix_news_%%' OR indexname LIKE 'news_notifications_%%')"
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
        "reject_news_event_evidence_mutation",
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
    assert "processed_evidence_refs" not in semantic_work_columns
    assert {
        "processed_read_refs",
        "reanalysis_read_ref",
        "reanalysis_reason",
        "reanalysis_head_ref",
    } <= semantic_work_columns
    assert {"read_refs", "reanalysis_reason", "reanalysis_head_ref"} <= semantic_observation_columns
    assert {"observation_result_id", "scope_repair_id"} <= event_update_columns
    assert {"repair_id", "proof", "claim_refs", "projection_version"} <= scope_repair_columns
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
        "ix_news_items_published",
        "ix_news_events_opened",
        "ix_news_events_kind_opened",
        "ix_news_events_admission",
        "ix_news_events_expires",
        "ix_news_events_storyline",
        "ix_news_events_fingerprint",
        "ix_news_events_search",
        "ix_news_events_unpublished",
        "ix_news_event_members_item",
        "ix_news_event_bands_lookup",
        "ix_news_event_bands_expires",
        "ix_news_event_assets_event",
        "ix_news_event_assets_symbol",
        "ix_news_event_assets_retrieval_symbol",
        "ix_news_event_assets_retrieval_pair_base",
        "ix_news_event_members_fact_trgm",
        "ix_news_events_leader_item",
        "ix_news_items_canonical_url",
        "ix_news_event_evidence_created",
    } <= set(news_v3_indexes)
    assert "sent" in news_v3_indexes["news_notifications_sent"]
    assert "gin" in news_v3_indexes["ix_news_events_search"].lower()
    assert "event_kind, opened_at_ms DESC, event_id DESC" in news_v3_indexes["ix_news_events_kind_opened"]
    # The Janitor's rescue index must cover every admitted admission, not just `candidate`: a partial index on
    # `candidate` alone left crashed-before-publish listing Events unrecoverable (#72).
    unpublished_index = news_v3_indexes["ix_news_events_unpublished"]
    assert "published_at_ms IS NULL" in unpublished_index
    assert "'candidate'" in unpublished_index and "'listing_deterministic'" in unpublished_index
    assert "telemetry_deterministic" not in unpublished_index
    assert "liquidation_deterministic" not in unpublished_index
    assert version == latest_migration_version() == "20261001_0422"


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
    assert version == latest_migration_version() == "20261001_0422"


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
