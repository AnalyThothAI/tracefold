"""Offline lexical scoring uses the production PostgreSQL adapter in TEMP scope."""

import pytest

from scripts.eval_news_recall import attach_lexical_scores, build_queries
from tests.news.test_news_recall_evaluator import frozen_scope
from tests.postgres_test_utils import connect_postgres_test

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_clone_dsn")]


def test_postgres_replay_uses_production_fts_and_never_changes_persistent_index_facts() -> None:
    bundle, facts, sample, units, original = frozen_scope()
    queries, index = build_queries(bundle, facts, sample, units, original, window="original_audit")
    conn = connect_postgres_test(read_only=False)
    try:
        before = conn.execute("SELECT count(*) AS n FROM public.news_claim_index").fetchone()["n"]
        attach_lexical_scores(conn, queries, index)
        q = queries[0]
        assert all(c.lexical > 0 for c in q.prior if c.key in q.prior_valid)
        assert all(c.lexical > 0 for c in q.receipt if c.key in q.receipt_valid)
        assert conn.execute("SELECT count(*) AS n FROM public.news_claim_index").fetchone()["n"] == before
        assert (
            conn.execute(
                "SELECT relpersistence FROM pg_class WHERE oid='pg_temp.news_claim_index'::regclass"
            ).fetchone()["relpersistence"]
            == "t"
        )
    finally:
        conn.close()
    conn = connect_postgres_test(read_only=False)
    try:
        assert conn.execute("SELECT to_regclass('pg_temp.news_claim_index') AS relation").fetchone()["relation"] is None
        assert conn.execute("SELECT count(*) AS n FROM public.news_claim_index").fetchone()["n"] == before
    finally:
        conn.close()
