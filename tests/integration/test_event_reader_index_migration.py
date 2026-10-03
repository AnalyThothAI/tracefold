"""0429 adds reader indexes and reverses without touching facts or frozen receipts."""

from __future__ import annotations

import asyncio
from contextlib import closing

import pytest
from alembic import command

from tests.postgres_test_utils import connect_postgres_test, postgres_migration_test_dsn
from tests.support.news_update_pg import EVENT, Sender, adopted_head, notifications, store
from tracefold.platform.postgres.migrations import alembic_config

pytestmark = [
    pytest.mark.integration,
    pytest.mark.migration,
    pytest.mark.usefixtures("postgres_migration_dsn"),
    pytest.mark.usefixtures("synthetic_reader_calibration"),
]
READER_INDEXES = {"news_events_story_window", "news_notifications_sent_claims"}


def snapshot(conn):
    tables = ("news_events", "news_analyses", "news_notifications", "news_jobs")
    facts = {
        table: conn.execute(f"SELECT to_jsonb(t) AS fact FROM {table} t ORDER BY to_jsonb(t)::text").fetchall()
        for table in tables
    }
    indexes = {
        row["indexname"]: row["indexdef"]
        for row in conn.execute("SELECT indexname,indexdef FROM pg_indexes WHERE schemaname='public'")
    }
    return facts, indexes


def test_reader_indexes_upgrade_and_downgrade_preserve_received_messages(postgres_migration_dsn):
    config = alembic_config()
    config.attributes["database_url"] = postgres_migration_test_dsn()
    command.upgrade(config, "20261002_0428")
    stores, _, clock = store()
    adopted_head(stores.semantic, clock)
    assert asyncio.run(notifications(stores.notifications, clock, Sender()).process(EVENT, "news")) == "sent"
    with closing(connect_postgres_test()) as conn:
        facts, indexes = snapshot(conn)
        assert facts["news_notifications"]
    command.upgrade(config, "head")
    with closing(connect_postgres_test()) as conn:
        after, upgraded = snapshot(conn)
        assert after == facts
        assert upgraded.keys() - indexes.keys() == READER_INDEXES
        assert {key: upgraded[key] for key in indexes} == indexes
        assert "(storyline_key, opened_at_ms, event_id)" in upgraded["news_events_story_window"]
        assert "USING gin (claim_refs jsonb_path_ops)" in upgraded["news_notifications_sent_claims"]
        assert "state = 'sent'" in upgraded["news_notifications_sent_claims"]
    command.downgrade(config, "20261002_0428")
    with closing(connect_postgres_test()) as conn:
        assert snapshot(conn) == (facts, indexes)
