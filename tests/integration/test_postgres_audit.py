from __future__ import annotations

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tracefold.app.query_audit import query_audit_catalog
from tracefold.platform.postgres.audit import (
    NEWS_TABLES,
    TRADING_TABLES,
    PostgresOperationalAudit,
    PostgresQueryAudit,
    ProjectionValidationAudit,
    QueryAuditCatalog,
)
from tracefold.platform.postgres.migrations import latest_migration_version

pytestmark = pytest.mark.integration


def _composed_catalog(*, now_ms: int = 0) -> QueryAuditCatalog:
    return query_audit_catalog(now_ms=now_ms)


def test_operational_audit_fast_path_uses_catalog_estimates_and_exact_schema(
    tmp_path, postgres_clone_dsn: str, monkeypatch
):
    monkeypatch.setenv("TRACEFOLD_POSTGRES_IMAGE", "postgres:18-bookworm@sha256:test")
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        payload = PostgresOperationalAudit(conn).run()
    finally:
        conn.close()

    assert payload["ok"] is True
    assert payload["engine"] == "postgresql"
    assert payload["migration_version"] == latest_migration_version()
    assert payload["migration_status"] == "ready"
    assert payload["mode"] == "fast"
    assert payload["database_identity"]["server_version_num"] >= 180_000
    assert payload["database_identity"]["declared_image_identity"] == "postgres:18-bookworm@sha256:test"
    assert payload["database_identity"]["image_identity_source"] == "TRACEFOLD_POSTGRES_IMAGE"
    assert "plpgsql" in payload["database_identity"]["extensions"]
    assert set(payload["database_identity"]["settings"]) == {
        "transaction_isolation",
        "statement_timeout",
        "lock_timeout",
        "idle_in_transaction_session_timeout",
        "jit",
    }
    assert "counts" not in payload
    assert set(payload["row_estimates"]) == set(NEWS_TABLES) | set(TRADING_TABLES)
    assert payload["trading_schema"]["exact"] is True
    assert all(count >= 0 for count in payload["row_estimates"].values())
    assert payload["news_schema"] == {
        "expected_tables": list(NEWS_TABLES),
        "actual_tables": sorted(NEWS_TABLES),
        "exact": True,
    }
    assert payload["runtime_roles"]["ok"] is True
    assert "projection_schema" not in payload
    assert "foreign_key_checks" not in payload


def test_operational_audit_deep_mode_runs_explicit_exact_counts(tmp_path, postgres_clone_dsn: str):
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        payload = PostgresOperationalAudit(conn).run(deep=True)
    finally:
        conn.close()

    assert payload["ok"] is True
    assert payload["mode"] == "deep"
    assert set(payload["counts"]) == set(NEWS_TABLES) | set(TRADING_TABLES)
    assert all(count >= 0 for count in payload["counts"].values())
    assert payload["counts"]["news_learning_epochs"] > 0


def test_operational_audit_fails_when_workers_cannot_append_evidence(tmp_path, postgres_clone_dsn: str):
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        conn.execute("REVOKE INSERT ON news_event_evidence_snapshots FROM tracefold_workers")
        conn.commit()

        payload = PostgresOperationalAudit(conn).run()
    finally:
        conn.close()

    assert payload["ok"] is False
    assert payload["runtime_roles"]["ok"] is False
    assert "workers_evidence_append" in payload["runtime_roles"]["failures"]


def test_query_audit_explains_hot_read_paths_without_analyze(tmp_path, postgres_clone_dsn: str):
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        payload = PostgresQueryAudit(conn, catalog=_composed_catalog()).run(analyze=False)
    finally:
        conn.close()

    names = {item["name"] for item in payload["queries"]}
    assert payload["ok"] is True
    assert payload["analyze"] is False
    assert {"readiness_schema"} <= names
    retired_prefixes = ("recent_", "search_", "target_posts", "live_market", "provider_")
    assert not any(name.startswith(retired_prefixes) for name in names)
    assert all(item["plan"] for item in payload["queries"])


def test_projection_validation_checks_bounded_public_models(tmp_path, postgres_clone_dsn: str):
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        initial = ProjectionValidationAudit(conn).run(sample=100)
        conn.execute("DELETE FROM news_ingest_state")
        stale = ProjectionValidationAudit(conn).run(sample=100)
    finally:
        conn.close()

    assert initial["ok"] is True
    assert initial["mismatch_count"] == 0
    assert set(initial["checks"]) == {
        "news_ingest_state_mismatch",
        "news_delivery_state_mismatch",
    }
    assert stale["ok"] is False
    assert stale["checks"]["news_ingest_state_mismatch"] == 1


def test_query_audit_analyzes_all_route_query_families_on_empty_schema(
    tmp_path,
    postgres_clone_dsn: str,
):
    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        payload = PostgresQueryAudit(conn, catalog=_composed_catalog()).run(analyze=True)
    finally:
        conn.close()

    assert payload["ok"] is True
    assert payload["analyze"] is True
    assert all(item["metrics"]["plan_json_valid"] for item in payload["queries"])
    assert all(item["violations"] == [] for item in payload["queries"])


def _vacuum_analyze(conn, table_name: str) -> None:
    conn.commit()
    raw_conn = conn._conn
    raw_conn.autocommit = True
    try:
        conn.execute(f"VACUUM (ANALYZE) {table_name}")
    finally:
        raw_conn.autocommit = False
