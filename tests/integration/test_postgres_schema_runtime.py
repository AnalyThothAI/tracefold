from __future__ import annotations

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tests.postgres_test_utils import test_postgres_dsn as _test_postgres_dsn
from tracefold.platform.postgres.audit import NEWS_TABLES, TRADING_TABLES
from tracefold.platform.postgres.migrations import (
    latest_migration_version,
    upgrade_head,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_dsn")]


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
        news_verdict_columns = columns("news_verdicts")
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
        *NEWS_TABLES,
        # #104: the Trading bounded context's own tables. Registered separately from
        # `NEWS_TABLES` so "exactly these tables" stays a per-capability claim.
        *TRADING_TABLES,
    }
    assert {
        "news_strategy_provenance_valid",
        "reject_news_event_evidence_mutation",
        "reject_news_review_mutation",
    } <= functions
    assert {
        "event_id",
        "family",
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
    assert {
        "latency_ms",
        "queue_lag_ms",
        "reasked_after_told_change",
        "novelty_defaulted",
        "seen_scope",
    } <= news_verdict_columns
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
        "edit_state",
        "pending_card",
        "edit_error_code",
        "edit_attempted_at_ms",
        "edit_settled_at_ms",
        "delete_state",
        "delete_evidence",
        "delete_reason",
        "delete_error_code",
        "delete_attempted_at_ms",
        "delete_settled_at_ms",
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
        "ix_news_verdicts_stage_created",
        "ix_news_verdicts_final",
        "ix_news_deliveries_state",
        "ix_news_deliveries_sent",
        "ix_news_deliveries_editing",
        "ix_news_deliveries_deleting",
        "ix_news_event_evidence_created",
        "ix_news_external_miss_created",
        "ix_news_reviews_event_created",
        "ix_news_reviews_task_created",
    } <= set(news_v3_indexes)
    assert "state = 'sent'" in news_v3_indexes["ix_news_deliveries_sent"]
    assert "gin" in news_v3_indexes["ix_news_events_search"].lower()
    assert "event_kind, opened_at_ms DESC, event_id DESC" in news_v3_indexes["ix_news_events_kind_opened"]
    # The Janitor's rescue index must cover every admitted admission, not just `candidate`: a partial index on
    # `candidate` alone left crashed-before-publish listing Events unrecoverable (#72).
    unpublished_index = news_v3_indexes["ix_news_events_unpublished"]
    assert "published_at_ms IS NULL" in unpublished_index
    assert "'candidate'" in unpublished_index and "'listing_deterministic'" in unpublished_index
    assert "'telemetry_deterministic'" in unpublished_index and "'liquidation_deterministic'" in unpublished_index
    assert version == latest_migration_version() == "20260829_0325"


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
    assert version == latest_migration_version() == "20260829_0325"
