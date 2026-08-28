from __future__ import annotations

from typing import Any

from tracefold.platform.postgres import legacy_reconciliation


class _Cursor:
    def __init__(self, value: Any) -> None:
        self._value = value

    def fetchone(self) -> tuple[Any] | None:
        return None if self._value is None else (self._value,)


class _Connection:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def execute(self, query: str, _params: tuple[Any, ...] = ()) -> _Cursor:
        statement = " ".join(query.split())
        self.statements.append(statement)
        if "SELECT current_user = 'tracefold_migrate'" in statement:
            return _Cursor(True)
        if statement == "SET ROLE tracefold_owner":
            return _Cursor(None)
        if "SELECT to_regclass" in statement:
            return _Cursor(False)
        raise AssertionError(f"unexpected query before lineage detection: {statement}")


def test_legacy_lineage_assumes_owner_before_reading_alembic_tables(monkeypatch: Any) -> None:
    connection = _Connection()
    monkeypatch.setattr(legacy_reconciliation.psycopg, "connect", lambda _dsn: connection)

    assert legacy_reconciliation.reconcile_colliding_telegram_lineage("postgresql://migrate@db/tracefold") is False
    assert connection.statements[:2] == [
        "SELECT current_user = 'tracefold_migrate' AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = "
        "'tracefold_owner')",
        "SET ROLE tracefold_owner",
    ]
    assert all("alembic_version" not in statement for statement in connection.statements[:2])
