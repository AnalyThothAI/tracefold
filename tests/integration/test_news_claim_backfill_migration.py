"""0428 replaces timestamp indexes reversibly without changing persisted facts."""

from __future__ import annotations

import asyncio
from contextlib import closing

import pytest
from alembic import command

from tests.postgres_test_utils import connect_postgres_test, postgres_migration_test_dsn
from tests.support.news_update_pg import EVENT, Sender, adopted_head, notifications, store
from tracefold.news.claim_recall import CALIBRATION, text_sha, vector_bytes
from tracefold.platform.postgres.migrations import alembic_config

pytestmark = [pytest.mark.integration, pytest.mark.migration, pytest.mark.usefixtures("postgres_migration_dsn")]

FACT_TABLES = (
    "news_events",
    "news_analyses",
    "news_notifications",
    "news_claim_index",
    "news_jobs",
    "news_reader_clock",
    "news_trade_events",
    "news_judgment_cache",
)
CURSOR_INDEXES = {"news_analyses_adopted", "news_notifications_sent"}


def facts(conn):
    # These identifiers are a code-owned list, never input supplied by an operator.
    return {
        table: conn.execute(f"SELECT to_jsonb(t) AS fact FROM {table} t ORDER BY to_jsonb(t)::text").fetchall()
        for table in FACT_TABLES
    }


def indexes(conn):
    return {
        row["indexname"]: row["indexdef"]
        for row in conn.execute("SELECT indexname,indexdef FROM pg_indexes WHERE schemaname='public'")
    }


def test_cursor_indexes_upgrade_and_downgrade_preserve_0427_facts(postgres_migration_dsn):
    config = alembic_config()
    config.attributes["database_url"] = postgres_migration_test_dsn()
    command.upgrade(config, "20261002_0427")
    stores, db, clock = store()
    head = adopted_head(stores.semantic, clock)
    claim = head.claims[0]
    vector = vector_bytes([1.0, *([0.0] * (CALIBRATION.embedder.dimensions - 1))], CALIBRATION.embedder)
    asyncio.run(
        db.tx(
            "migration-vector",
            lambda repos: repos.news.claim_index.save_vectors(
                [(claim.ref, text_sha(claim), vector)], embedder=CALIBRATION.embedder.key
            ),
        )
    )
    assert asyncio.run(notifications(stores.notifications, clock, Sender()).process(EVENT, "news")) == "sent"
    with closing(connect_postgres_test()) as conn:
        before, original_indexes = facts(conn), indexes(conn)
        assert original_indexes.keys() >= CURSOR_INDEXES
        assert before["news_analyses"] and before["news_notifications"] and before["news_claim_index"]
        sent = conn.execute("SELECT sent_claims,card,receipt FROM news_notifications WHERE state='sent'").fetchone()
        assert sent["sent_claims"][0]["ref"] == claim.ref and sent["card"] and sent["receipt"]
        assert conn.execute("SELECT vector FROM news_claim_index").fetchone()["vector"] == vector

    command.upgrade(config, "20261002_0428")
    with closing(connect_postgres_test()) as conn:
        assert facts(conn) == before
        upgraded = indexes(conn)
        assert upgraded.keys() == original_indexes.keys()
        assert {key for key in original_indexes if upgraded[key] != original_indexes[key]} == CURSOR_INDEXES
        definitions = {
            row["name"]: row
            for row in conn.execute(
                """SELECT c.relname AS name,t.relname AS table_name,i.indisvalid,i.indisready,
                          ARRAY(SELECT pg_get_indexdef(i.indexrelid,n,true)
                                  FROM generate_series(1,i.indnkeyatts) n) AS columns,
                          pg_get_expr(i.indpred,i.indrelid) AS predicate
                     FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid
                     JOIN pg_class t ON t.oid=i.indrelid WHERE c.relname=ANY(%s::text[])""",
                (sorted(CURSOR_INDEXES),),
            )
        }
        adopted = definitions["news_analyses_adopted"]
        assert adopted["table_name"] == "news_analyses"
        assert adopted["columns"] == ["adopted_at_ms", "analysis_id"]
        assert adopted["predicate"] == "(adopted_at_ms IS NOT NULL)"
        receipt = definitions["news_notifications_sent"]
        assert receipt["table_name"] == "news_notifications"
        assert receipt["columns"] == ["settled_at_ms", "intent_id"]
        assert receipt["predicate"] == "((kind = 'update'::text) AND (state = 'sent'::text))"
        assert all(row["indisvalid"] and row["indisready"] for row in definitions.values())
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"] == "20261002_0428"

    command.downgrade(config, "20261002_0427")
    with closing(connect_postgres_test()) as conn:
        assert facts(conn) == before
        assert indexes(conn) == original_indexes
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"] == "20261002_0427"
    command.upgrade(config, "head")
    with closing(connect_postgres_test()) as conn:
        assert facts(conn) == before
        assert indexes(conn) == upgraded
