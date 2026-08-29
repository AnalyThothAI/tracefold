from __future__ import annotations

import pytest
from alembic import command
from sqlalchemy.exc import ProgrammingError

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import test_postgres_dsn as postgres_test_dsn
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.opennews import parse_opennews_message
from tracefold.news.pipeline.admission import admit_item
from tracefold.platform.postgres.migrations import alembic_config, upgrade_head

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_migration_dsn")]


def test_0324_refuses_an_invalid_lifecycle_row_admitted_by_0323_without_advancing() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("GRANT ALL ON SCHEMA public TO public")
        conn.commit()
    finally:
        conn.close()

    config = alembic_config()
    config.attributes["database_url"] = postgres_test_dsn()
    command.upgrade(config, "20260828_0323")

    event = parse_opennews_message(
        {
            "method": "strategy.triggered",
            "params": {
                "id": 324_001,
                "text": "Delivery lifecycle migration fixture",
                "link": "https://example.test/324001",
                "source": "Reuters",
                "newsType": "news",
                "engineType": "news",
                "ts": "2026-08-28T00:00:00Z",
                "aiRating": {"score": 80, "signal": "long", "status": "done"},
                "coins": [],
                "strategy": {
                    "id": 1018,
                    "name": "News Score > 70",
                    "engine_type": "news",
                    "source_type": "news",
                },
            },
        }
    )
    assert event is not None
    conn = connect_postgres_test(read_only=False)
    try:
        repositories = repositories_for_connection(conn)
        with repositories.transaction():
            opened = admit_item(
                repositories,
                event=event,
                ingest_mode="live",
                observed_at_ms=1_788_000_000_000,
                trace_id="delivery-lifecycle-0324",
                watchlist_symbols=frozenset(),
                now_ms=1_788_000_000_000,
            )
            conn.execute(
                """
                INSERT INTO news_deliveries(
                  event_id, kind, state, card, receipt, error_code,
                  attempted_at_ms, settled_at_ms, created_at_ms,
                  edit_state, pending_card, edit_error_code,
                  edit_attempted_at_ms, edit_settled_at_ms
                ) VALUES (
                  %s, 'first', 'sent', '{}'::jsonb, NULL, NULL,
                  1, 2, 1,
                  NULL, '{}'::jsonb, NULL, 1, NULL
                )
                """,
                (opened.event_id,),
            )
    finally:
        conn.close()

    try:
        with pytest.raises(ProgrammingError, match="news_delivery_lifecycle_shape_invalid"):
            upgrade_head(postgres_test_dsn())
        conn = connect_postgres_test(read_only=False)
        try:
            assert conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"] == (
                "20260828_0323"
            )
            conn.execute("DELETE FROM news_deliveries WHERE event_id = %s", (opened.event_id,))
            conn.commit()
        finally:
            conn.close()
    finally:
        upgrade_head(postgres_test_dsn())
