"""The additive read indexes upgrade the deployed predecessor without mutating jobs."""

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


def test_read_index_upgrade_preserves_predecessor_job_facts(postgres_migration_dsn, capsys):
    with closing(connect_postgres_test()) as conn, conn.transaction():
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
    prepare_test_migration_database(postgres_migration_dsn)
    config = alembic_config()
    config.attributes["database_url"] = postgres_migration_test_dsn()
    command.upgrade(config, "20261001_0424")
    notices = capsys.readouterr().err
    assert all(f"p{phase}_verify ok" in notices for phase in (2, 3, 4))
    with closing(connect_postgres_test()) as conn, conn.transaction():
        conn.execute(
            """INSERT INTO news_jobs(job_kind,subject_id,state,attempts,next_attempt_at_ms,
                                     lease_token,lease_until_ms,last_error_code,detail,created_at_ms,updated_at_ms)
               VALUES ('semantic','leased','pending',2,100,'owner',200,'deferred',
                       '{"wanted_revision":2,"done_revision":1,"lineage_id":"lineage"}',10,100),
                      ('semantic','completed-failure','done',3,100,NULL,NULL,'failed',
                       '{"wanted_revision":1,"done_revision":1,"last_outcome":"failed"}',10,100)"""
        )
        before = conn.execute("SELECT to_jsonb(j) AS fact FROM news_jobs j ORDER BY subject_id").fetchall()
    command.upgrade(config, "20261002_0425")
    with closing(connect_postgres_test()) as conn:
        assert conn.execute("SELECT to_jsonb(j) AS fact FROM news_jobs j ORDER BY subject_id").fetchall() == before
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"] == "20261002_0425"
        indexes = conn.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname='public' AND indexname LIKE 'news_jobs_semantic_%'"
        ).fetchall()
        assert {r["indexname"] for r in indexes} == {
            "news_jobs_semantic_lineage",
            "news_jobs_semantic_outstanding",
            "news_jobs_semantic_failed",
        }
