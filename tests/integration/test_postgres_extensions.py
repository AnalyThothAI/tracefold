import pytest

from tests.postgres_test_utils import connect_postgres_test

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_clone_dsn")]


def test_postgres_keeps_supported_extensions() -> None:
    conn = connect_postgres_test(read_only=True)
    try:
        installed = {row["extname"] for row in conn.execute("SELECT extname FROM pg_extension").fetchall()}
    finally:
        conn.close()

    assert {"pg_stat_statements", "pg_trgm"} <= installed
