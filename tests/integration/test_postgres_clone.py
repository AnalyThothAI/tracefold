from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.postgres_test_utils import MigratedPostgresCloneFactory, connect_postgres_test

pytestmark = pytest.mark.integration


def test_clone_factory_migrates_head_once_and_copies_the_complete_schema(
    postgres_clone_factory: MigratedPostgresCloneFactory,
) -> None:
    assert postgres_clone_factory.head_migration_count == 1
    starting_clone_count = postgres_clone_factory.clone_count

    with postgres_clone_factory.clone() as first_dsn, postgres_clone_factory.clone() as second_dsn:
        first = connect_postgres_test(dsn=first_dsn)
        second = connect_postgres_test(dsn=second_dsn)
        try:
            first_head = first.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"]
            second_head = second.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"]
            assert first_head == second_head
            assert first.execute("SELECT to_regclass('public.news_events') AS table_name").fetchone()["table_name"]
            assert second.execute("SELECT to_regclass('public.trading_cases') AS table_name").fetchone()["table_name"]
        finally:
            first.close()
            second.close()

    assert postgres_clone_factory.head_migration_count == 1
    assert postgres_clone_factory.clone_count == starting_clone_count + 2


def test_two_clones_cannot_destroy_each_others_public_schema(
    postgres_clone_factory: MigratedPostgresCloneFactory,
) -> None:
    with postgres_clone_factory.clone() as destructive_dsn, postgres_clone_factory.clone() as observer_dsn:

        def destroy_left() -> None:
            conn = connect_postgres_test(dsn=destructive_dsn)
            try:
                conn.execute("DROP SCHEMA public CASCADE")
            finally:
                conn.close()

        def observe_right() -> str:
            conn = connect_postgres_test(dsn=observer_dsn)
            try:
                return str(conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"])
            finally:
                conn.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            destroyed = executor.submit(destroy_left)
            observed = executor.submit(observe_right)
            destroyed.result()
            assert observed.result()
