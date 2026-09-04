from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import pytest

from tracefold.app.repository_session import repositories_for_connection
from tracefold.platform.config.models import Settings
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


def test_connect_postgres_forwards_keepalives_and_session_settings(monkeypatch: Any) -> None:
    """A long-lived session needs both halves of the keepalive contract.

    The parameters existed on `create_pool` and were simply never available on the single-connection
    path, so the one session that must survive a whole process — the account-slot lock holder — was
    the only one without them. A container killed by SIGKILL then left that backend alive on the
    server, still holding the advisory lock, and the next start died with
    `oi_runtime_account_slot_already_owned` (#537 D2).
    """

    captured: dict[str, Any] = {}

    def _connect(dsn: str, **kwargs: Any) -> object:
        captured["dsn"] = dsn
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(client.Connection, "connect", staticmethod(_connect))

    client.connect_postgres(
        "postgresql://tracefold@postgres:5432/tracefold",
        application_name="tracefold_nautilus_singleton",
        keepalives=True,
        keepalives_idle=30,
        keepalives_interval=10,
        keepalives_count=3,
        session_settings={"tcp_keepalives_idle": "30", "tcp_keepalives_interval": "10"},
    )

    # The DSN reaches libpq unchanged: the host-rewriting branch is deleted, so a compose-network
    # address stays a compose-network address and the CLI runs inside a container (#537 D1).
    assert captured["dsn"] == "postgresql://tracefold@postgres:5432/tracefold"
    assert captured["keepalives"] == 1
    assert captured["keepalives_idle"] == 30
    assert captured["keepalives_interval"] == 10
    assert captured["keepalives_count"] == 3
    assert captured["options"] == "-c tcp_keepalives_idle=30 -c tcp_keepalives_interval=10"


def test_a_short_lived_connection_sends_no_keepalive_or_options_parameters(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        client.Connection,
        "connect",
        staticmethod(lambda dsn, **kwargs: captured.update(kwargs) or object()),
    )

    client.connect_postgres("postgresql://tracefold@postgres:5432/tracefold")

    assert set(captured) == {"autocommit", "connect_timeout", "row_factory"}


def test_the_long_lived_repository_session_is_the_one_that_carries_keepalives(monkeypatch: Any) -> None:
    from tracefold.app import repository_session

    captured: list[dict[str, Any]] = []

    def _connect(_dsn: str, **kwargs: Any) -> Any:
        captured.append(kwargs)
        return _ClosableConnection()

    monkeypatch.setattr(repository_session, "connect_postgres", _connect)
    # The operator password file is deployment state, not test state; this case is about which
    # connection carries keepalives.
    monkeypatch.setattr(repository_session, "with_password_from_file", lambda dsn, _path: dsn)
    settings = Settings()

    with repository_session.postgres_connection(settings, long_lived=True):
        pass
    with repository_session.postgres_connection(settings):
        pass

    assert captured[0]["keepalives_idle"] == 30
    assert captured[0]["session_settings"] == {
        "tcp_keepalives_idle": "30",
        "tcp_keepalives_interval": "10",
        "tcp_keepalives_count": "3",
    }
    assert "keepalives_idle" not in captured[1]


class _ClosableConnection:
    def close(self) -> None:
        return None
