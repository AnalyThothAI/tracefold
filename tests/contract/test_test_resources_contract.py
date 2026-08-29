from __future__ import annotations

import pytest
from psycopg import OperationalError

from tests import postgres_test_utils


class _DatabaseIdentityConnection:
    def __init__(self, database_name: str) -> None:
        self.database_name = database_name

    def execute(self, _query: str) -> _DatabaseIdentityConnection:
        return self

    def fetchone(self) -> tuple[str]:
        return (self.database_name,)


def test_destructive_test_database_helpers_require_the_exact_test_identity() -> None:
    postgres_test_utils.assert_dedicated_test_database(_DatabaseIdentityConnection("tracefold_test"))
    postgres_test_utils.assert_dedicated_test_database(
        _DatabaseIdentityConnection("tracefold_test_case_012345abcdef_1"),
        expected_database="tracefold_test_case_012345abcdef_1",
    )
    postgres_test_utils.assert_dedicated_test_database(
        _DatabaseIdentityConnection("tracefold_test_migration_012345abcdef"),
        expected_database="tracefold_test_migration_012345abcdef",
    )

    for unsafe_name in ("tracefold", "postgres", "test", "tracefold_test_backup"):
        with pytest.raises(RuntimeError, match="postgres_test_database_identity_invalid"):
            postgres_test_utils.assert_dedicated_test_database(_DatabaseIdentityConnection(unsafe_name))


def test_postgres_connection_failure_fails_in_evidence_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRACEFOLD_TEST_EVIDENCE", "1")
    monkeypatch.setattr(
        postgres_test_utils,
        "connect_postgres",
        lambda _dsn: (_ for _ in ()).throw(OperationalError("unavailable")),
    )

    try:
        postgres_test_utils.connect_postgres_test()
    except BaseException as exc:
        assert isinstance(exc, pytest.fail.Exception)
        assert "PostgreSQL test database is required in evidence mode" in str(exc)
    else:
        pytest.fail("evidence mode accepted an unavailable PostgreSQL resource")


def test_postgres_connection_failure_remains_a_local_convenience_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRACEFOLD_TEST_EVIDENCE", raising=False)
    monkeypatch.setattr(
        postgres_test_utils,
        "connect_postgres",
        lambda _dsn: (_ for _ in ()).throw(OperationalError("unavailable")),
    )

    with pytest.raises(pytest.skip.Exception, match="PostgreSQL test database is not available"):
        postgres_test_utils.connect_postgres_test()
