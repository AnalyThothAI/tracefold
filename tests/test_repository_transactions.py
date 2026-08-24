from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import pytest

from tracefold.app.repository_session import repositories_for_connection
from tracefold.platform.postgres import client


def test_require_transaction_rejects_connections_without_psycopg_info() -> None:
    with pytest.raises(RuntimeError, match="fake_write_requires_transaction_status_contract"):
        client.require_transaction(object(), operation="fake_write")


def test_repository_session_transaction_owns_database_transaction() -> None:
    conn = FakeTransactionConnection()
    repos = repositories_for_connection(conn)

    with repos.transaction():
        conn.events.append("body")

    assert conn.events == ["begin", "body", "commit"]


class FakeTransactionConnection:
    def __init__(self) -> None:
        self.events: list[str] = []

    @contextmanager
    def transaction(self) -> Any:
        self.events.append("begin")
        try:
            yield
        except BaseException:
            self.events.append("rollback")
            raise
        else:
            self.events.append("commit")
