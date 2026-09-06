from __future__ import annotations

import pytest
from psycopg import sql

from tests.postgres_test_utils import connect_postgres_test, postgres_migration_test_dsn
from tracefold.app.query_audit import query_audit_catalog
from tracefold.platform.postgres.audit import (
    LARGE_SEQ_SCAN_ROWS,
    NEWS_TABLES,
    TRADING_TABLES,
    PostgresOperationalAudit,
    PostgresQueryAudit,
    ProjectionValidationAudit,
    QueryAuditCatalog,
    ReadQuerySpec,
)
from tracefold.platform.postgres.migrations import latest_migration_version

pytestmark = pytest.mark.integration


def _composed_catalog(*, now_ms: int = 0) -> QueryAuditCatalog:
    return query_audit_catalog(now_ms=now_ms)


def test_operational_audit_fast_path_uses_catalog_estimates_and_exact_schema(
    tmp_path, postgres_clone_dsn: str, monkeypatch
):
    monkeypatch.setenv("TRACEFOLD_POSTGRES_IMAGE", "postgres:18-bookworm@sha256:test")
    conn = connect_postgres_test(
        tmp_path / "postgres_test_db",
        read_only=False,
        dsn=postgres_migration_test_dsn(postgres_clone_dsn),
    )
    try:
        payload = PostgresOperationalAudit(conn).run()
    finally:
        conn.close()

    assert payload["ok"] is True
    assert payload["engine"] == "postgresql"
    assert payload["migration_version"] == latest_migration_version()
    assert payload["migration_status"] == "ready"
    assert payload["mode"] == "fast"
    assert payload["database_identity"]["ok"] is True
    assert all(payload["database_identity"]["checks"].values())
    assert payload["database_identity"]["server_version_num"] >= 180_000
    assert payload["database_identity"]["declared_image_identity"] == "postgres:18-bookworm@sha256:test"
    assert payload["database_identity"]["image_identity_source"] == "TRACEFOLD_POSTGRES_IMAGE"
    assert "plpgsql" in payload["database_identity"]["extensions"]
    assert payload["database_identity"]["current_user"] == "tracefold"
    assert set(payload["database_identity"]["role_catalog"]["roles"]) == {"tracefold", "tracefold_app"}
    assert payload["database_identity"]["role_catalog"]["retired_roles_present"] == []
    assert payload["database_identity"]["ownership"] == {
        "public_schema_owner": "tracefold",
        "unexpected_application_object_owners": [],
    }
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
    assert "runtime_roles" not in payload
    assert "projection_schema" not in payload
    assert "foreign_key_checks" not in payload


def test_operational_audit_deep_mode_runs_explicit_exact_counts(tmp_path, postgres_clone_dsn: str):
    conn = connect_postgres_test(
        tmp_path / "postgres_test_db",
        read_only=False,
        dsn=postgres_migration_test_dsn(postgres_clone_dsn),
    )
    try:
        payload = PostgresOperationalAudit(conn).run(deep=True)
    finally:
        conn.close()

    assert payload["ok"] is True
    assert payload["mode"] == "deep"
    assert set(payload["counts"]) == set(NEWS_TABLES) | set(TRADING_TABLES)
    assert all(count >= 0 for count in payload["counts"].values())
    assert payload["counts"]["news_learning_epochs"] == 0


def test_operational_audit_rejects_wrong_application_ownership(tmp_path, postgres_clone_dsn: str):
    admin = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        admin_role = str(admin.execute("SELECT current_user AS role").fetchone()["role"])
        admin.execute(sql.SQL("ALTER SCHEMA public OWNER TO {}").format(sql.Identifier(admin_role)))
        admin.commit()
    finally:
        admin.close()

    conn = connect_postgres_test(
        tmp_path / "postgres_test_db",
        read_only=False,
        dsn=postgres_migration_test_dsn(postgres_clone_dsn),
    )
    try:
        payload = PostgresOperationalAudit(conn).run()
    finally:
        conn.close()

    assert payload["ok"] is False
    assert payload["database_identity"]["checks"]["public_schema_owned_by_application"] is False
    assert payload["database_identity"]["ownership"]["public_schema_owner"] == admin_role


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
        # #553 PR-2: the same bounded-model question for the market card ledger, plus the claim only
        # it can make -- an observation may not point at a card that is not there.
        "news_market_delivery_state_mismatch",
        "news_market_coverage_mismatch",
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
    # #510 PR-5a: a console route that plans a filtered statement too has it EXPLAINed here rather
    # than only its unfiltered first page. #537 PR-5 deleted the three GET routes whose filtered
    # plans the other names covered; the two CLI-only ledger reads have no route and are still here.
    assert {
        "trading_console_cases_filtered",
        "trading_console_commands_filtered",
        "trading_signal_ledger",
        "trading_observation_ledger",
        "trading_status_latest_case",
        "trading_gate_decision_counts",
    } <= {item["name"] for item in payload["queries"]}


def _vacuum_analyze(conn, table_name: str) -> None:
    conn.commit()
    raw_conn = conn._conn
    raw_conn.autocommit = True
    try:
        conn.execute(f"VACUUM (ANALYZE) {table_name}")
    finally:
        raw_conn.autocommit = False


# #570 A1's isolated counterexample, as a regression. 50,000 rows, one id index, `ANALYZE`, and two
# read-only statements whose real plans are fed to the real audit. Parallel gather is off so the plan is
# the single-worker shape the numbers below describe.
_COUNTEREXAMPLE_ROWS = 50_000
_COUNTEREXAMPLE_BUDGET = 100.0


_COUNTEREXAMPLE_SCAN_BUDGET = 100_000


def _counterexample_catalog(*queries: ReadQuerySpec) -> QueryAuditCatalog:
    return QueryAuditCatalog(
        queries=queries,
        query_routes={"/audit": tuple(query.name for query in queries)},
        no_sql_routes=frozenset(),
    )


def test_query_audit_classifies_the_570_counterexample_plans_from_real_postgresql(
    tmp_path,
    postgres_clone_dsn: str,
):
    """The filtered full scan is a large scan; the bounded aggregate is not an amplified read.

    Before this change the audit read `Actual Rows` -- a node's *output* -- as the rows it had read, and
    judged sequential scans on `Plan Rows`, the planner's estimate of that same output. A filter that
    discarded all 50,000 rows was therefore recorded as zero rows read with no violation, while a
    `count(*)` over 500 bounded rows was reported as a 500x amplified read because it returned one row.
    Both are real PostgreSQL plans, not fixtures.
    """

    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        conn.execute("SET max_parallel_workers_per_gather = 0")
        conn.execute("CREATE TABLE audit_scan (id bigint PRIMARY KEY, payload text NOT NULL)")
        conn.execute(
            "INSERT INTO audit_scan (id, payload) SELECT g, 'row-' || g FROM generate_series(1, %s) AS g",
            (_COUNTEREXAMPLE_ROWS,),
        )
        _vacuum_analyze(conn, "audit_scan")
        payload = PostgresQueryAudit(
            conn,
            catalog=_counterexample_catalog(
                ReadQuerySpec(
                    name="filtered_full_scan",
                    sql="SELECT id FROM audit_scan WHERE id::text = %s",
                    params=("missing",),
                    max_read_return_amplification=_COUNTEREXAMPLE_BUDGET,
                    max_scanned_rows=_COUNTEREXAMPLE_SCAN_BUDGET,
                ),
                ReadQuerySpec(
                    name="bounded_aggregate",
                    sql="SELECT count(*) AS n FROM audit_scan WHERE id <= %s",
                    params=(500,),
                    max_read_return_amplification=_COUNTEREXAMPLE_BUDGET,
                    max_scanned_rows=_COUNTEREXAMPLE_SCAN_BUDGET,
                ),
                ReadQuerySpec(
                    name="bounded_index_page",
                    sql="SELECT id FROM audit_scan WHERE id <= %s ORDER BY id LIMIT 50",
                    params=(500,),
                    max_read_return_amplification=_COUNTEREXAMPLE_BUDGET,
                    max_scanned_rows=_COUNTEREXAMPLE_SCAN_BUDGET,
                ),
                ReadQuerySpec(
                    name="single_row_lookup",
                    sql="SELECT id, payload FROM audit_scan WHERE id = %s",
                    params=(42,),
                    max_read_return_amplification=_COUNTEREXAMPLE_BUDGET,
                    max_scanned_rows=_COUNTEREXAMPLE_SCAN_BUDGET,
                ),
            ),
        ).run(analyze=True)
    finally:
        conn.execute("DROP TABLE IF EXISTS audit_scan")
        conn.commit()
        conn.close()

    audited = {item["name"]: item for item in payload["queries"]}
    full_scan = audited["filtered_full_scan"]
    aggregate = audited["bounded_aggregate"]

    # (a) Output 0, filter discarded 50,000. Reported as zero rows read and no violation before.
    assert full_scan["metrics"]["returned_rows"] == 0
    assert full_scan["metrics"]["scan_output_rows"] == 0
    assert full_scan["metrics"]["scanned_rows"] == _COUNTEREXAMPLE_ROWS
    assert full_scan["metrics"]["discarded_rows"] == _COUNTEREXAMPLE_ROWS
    assert [scan["relation"] for scan in full_scan["metrics"]["large_seq_scans"]] == ["audit_scan"]
    assert full_scan["violations"] == [
        "unexpected_large_table_seq_scan",
        "read_return_amplification_exceeded",
    ]

    # (b) 500 bounded rows in, one row out. Reported as a 500x amplified read before.
    assert aggregate["metrics"]["returned_rows"] == 1
    assert aggregate["metrics"]["scanned_rows"] == 500
    assert aggregate["metrics"]["amplification_basis"] == "aggregate_input"
    assert aggregate["metrics"]["amplification_basis_rows"] == 500
    assert aggregate["metrics"]["read_return_amplification"] == 1.0
    assert aggregate["violations"] == []

    # The bounded reads beside them keep passing, and neither is a folded aggregate.
    for name in ("bounded_index_page", "single_row_lookup"):
        assert audited[name]["violations"] == [], name
        assert audited[name]["metrics"]["amplification_basis"] == "returned_rows", name
        assert audited[name]["metrics"]["large_seq_scans"] == [], name
    assert payload["ok"] is False
    assert payload["thresholds"]["large_seq_scan_rows"] == LARGE_SEQ_SCAN_ROWS


def test_query_audit_guards_a_paged_read_and_an_unbounded_aggregate_on_real_plans(
    tmp_path,
    postgres_clone_dsn: str,
):
    """Three shapes the fold must not excuse, all planned by PostgreSQL rather than written by hand.

    A page read carrying `(SELECT count(*) FROM ...)` -- the shape of `news_market_groups` and every
    review-desk queue read -- returns many rows and folded nothing, so its own returned rows stay the
    denominator. The same page filtered down to nothing is the same case, not a smaller one: an ungrouped
    aggregate emits exactly one row, so a result of none did not come from one, and a filter that
    discards the window to return nothing is the most amplified read there is. And an aggregate that
    reads a whole table by an index path has amplification 1 by construction and no sequential scan to
    flag, so only the spec's declared scanned-rows ceiling can bound it.
    """

    conn = connect_postgres_test(tmp_path / "postgres_test_db", read_only=False)
    try:
        conn.execute("SET max_parallel_workers_per_gather = 0")
        conn.execute("CREATE TABLE audit_wide (id bigint PRIMARY KEY)")
        conn.execute("INSERT INTO audit_wide (id) SELECT generate_series(1, %s)", (500_000,))
        conn.execute("CREATE TABLE audit_page (id bigint PRIMARY KEY, payload text NOT NULL)")
        conn.execute("INSERT INTO audit_page (id, payload) SELECT g, 'present' FROM generate_series(1, 25000) AS g")
        _vacuum_analyze(conn, "audit_wide")
        _vacuum_analyze(conn, "audit_page")
        payload = PostgresQueryAudit(
            conn,
            catalog=_counterexample_catalog(
                ReadQuerySpec(
                    name="paged_read_with_scalar_subplan",
                    sql="SELECT id, (SELECT count(*) FROM audit_wide) AS scanned FROM audit_page ORDER BY id LIMIT 200",
                    params=(),
                    max_read_return_amplification=_COUNTEREXAMPLE_BUDGET,
                    max_scanned_rows=1_000_000,
                ),
                ReadQuerySpec(
                    # The window is materialised before the filter, the way `news_market_groups`
                    # materialises its own, so the aggregate really runs on a group key that matches
                    # nothing. A target-list subquery would be left unevaluated and prove less.
                    name="empty_page_with_materialised_aggregate",
                    sql="WITH counted AS MATERIALIZED (SELECT count(*) AS n FROM audit_wide)"
                    " SELECT p.id, counted.n AS scanned FROM counted CROSS JOIN audit_page p"
                    " WHERE p.payload = %s ORDER BY p.id LIMIT 200",
                    params=("absent",),
                    max_read_return_amplification=_COUNTEREXAMPLE_BUDGET,
                    max_scanned_rows=1_000_000,
                ),
                ReadQuerySpec(
                    name="unbounded_aggregate_by_index",
                    sql="SELECT count(*) AS n FROM audit_wide WHERE id > %s",
                    params=(250_000,),
                    max_read_return_amplification=20.0,
                    max_scanned_rows=_COUNTEREXAMPLE_SCAN_BUDGET,
                ),
            ),
        ).run(analyze=True)
    finally:
        conn.execute("DROP TABLE IF EXISTS audit_page")
        conn.execute("DROP TABLE IF EXISTS audit_wide")
        conn.commit()
        conn.close()

    audited = {item["name"]: item for item in payload["queries"]}
    paged = audited["paged_read_with_scalar_subplan"]
    empty = audited["empty_page_with_materialised_aggregate"]
    unbounded = audited["unbounded_aggregate_by_index"]

    # The page returned 200 rows, so those are the denominator; folding the subplan would report 1.0004.
    assert paged["metrics"]["returned_rows"] == 200
    assert paged["metrics"]["amplification_basis"] == "returned_rows"
    assert paged["metrics"]["amplification_basis_rows"] == 200
    assert paged["metrics"]["read_return_amplification"] > _COUNTEREXAMPLE_BUDGET
    assert "read_return_amplification_exceeded" in paged["violations"]

    # Nothing matched, and the aggregate ran anyway: 500,000 rows folded plus 25,000 discarded, for no
    # row at all. Reading that as a fold reported 1.05 and passed.
    assert empty["metrics"]["returned_rows"] == 0
    assert empty["metrics"]["scan_output_rows"] == 500_000
    assert empty["metrics"]["scanned_rows"] == 525_000
    assert empty["metrics"]["amplification_basis"] == "returned_rows"
    assert empty["metrics"]["amplification_basis_rows"] == 0
    assert empty["metrics"]["read_return_amplification"] == 525_000.0
    assert "read_return_amplification_exceeded" in empty["violations"]

    # One row out, a quarter of a million rows in, by an index. Amplification and the sequential-scan
    # rule are both silent here on purpose; the declared ceiling is not.
    assert unbounded["metrics"]["returned_rows"] == 1
    assert unbounded["metrics"]["amplification_basis"] == "aggregate_input"
    assert unbounded["metrics"]["read_return_amplification"] == 1.0
    assert unbounded["metrics"]["large_seq_scans"] == []
    assert unbounded["metrics"]["scanned_rows"] > _COUNTEREXAMPLE_SCAN_BUDGET
    assert unbounded["violations"] == ["scanned_rows_budget_exceeded"]
