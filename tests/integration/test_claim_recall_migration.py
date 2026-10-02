"""#791 upgrades and restores 0419 retrieval without rewriting adopted facts."""

from contextlib import closing

import pytest
from alembic import command

from tests.postgres_test_utils import (
    connect_postgres_test,
    postgres_migration_test_dsn,
    prepare_test_migration_database,
)
from tracefold.platform.postgres.migrations import alembic_config

pytestmark = [pytest.mark.integration, pytest.mark.migration, pytest.mark.usefixtures("postgres_migration_dsn")]


def test_claim_recall_upgrade_downgrade_restores_predecessor_columns_functions_and_indexes(postgres_migration_dsn):
    with closing(connect_postgres_test()) as conn, conn.transaction():
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
    prepare_test_migration_database(postgres_migration_dsn)
    config = alembic_config()
    config.attributes["database_url"] = postgres_migration_test_dsn()
    command.upgrade(config, "20261002_0425")
    with closing(connect_postgres_test()) as conn:
        before = conn.execute(
            "SELECT public.news_asset_retrieval_symbol('$XYZ-SEI'),"
            "public.news_asset_retrieval_pair_base('SEIUSDT','crypto')"
        ).fetchone()
        old_indexes = {
            r["indexname"] for r in conn.execute("SELECT indexname FROM pg_indexes WHERE schemaname='public'")
        }
    command.upgrade(config, "20261002_0426")
    with closing(connect_postgres_test()) as conn:
        assert conn.execute("SELECT count(*) AS n FROM news_claim_index").fetchone()["n"] == 0
        assert (
            conn.execute("SELECT to_regprocedure('public.news_asset_retrieval_symbol(text)') AS f").fetchone()["f"]
            is None
        )
        assert not conn.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_name='news_event_assets' "
            "AND column_name IN ('retrieval_symbol','retrieval_pair_base')"
        ).fetchall()
        assert conn.execute("SELECT to_regclass('public.ix_news_event_members_fact_trgm') AS i").fetchone()["i"] is None
    command.downgrade(config, "20261002_0425")
    with closing(connect_postgres_test()) as conn:
        assert conn.execute("SELECT to_regclass('public.news_claim_index') AS t").fetchone()["t"] is None
        assert (
            conn.execute(
                "SELECT public.news_asset_retrieval_symbol('$XYZ-SEI'),"
                "public.news_asset_retrieval_pair_base('SEIUSDT','crypto')"
            ).fetchone()
            == before
        )
        restored = {r["indexname"] for r in conn.execute("SELECT indexname FROM pg_indexes WHERE schemaname='public'")}
        assert restored == old_indexes
        triggers = {
            r["tgname"] for r in conn.execute("SELECT tgname FROM pg_trigger WHERE tgname LIKE 'news_reader_%'")
        }
        assert {"news_reader_membership", "news_reader_item_metadata", "news_reader_event_kind"} <= triggers
    command.upgrade(config, "head")
