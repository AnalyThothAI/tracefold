from __future__ import annotations

import pytest
from psycopg import OperationalError

from tests import postgres_test_utils


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
