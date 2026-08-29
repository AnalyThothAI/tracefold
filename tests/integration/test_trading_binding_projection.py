from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tracefold.app.repository_session import repositories_for_connection
from tracefold.app.trading_bindings import project_binding_credentials
from tracefold.platform.config.models import Settings

pytestmark = pytest.mark.integration


class _Database:
    def __init__(self, connection: Any) -> None:
        self.connection = connection

    async def tx(self, _name: str, fn: Any, *, timeout_seconds: float) -> Any:
        del timeout_seconds
        with self.connection.transaction():
            return fn(repositories_for_connection(self.connection))


def _secure(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)


def _settings(path: Path, variant: str) -> Settings:
    if variant in {"single", "dual", "invalid"}:
        _secure(path / "binance-key", "binance-key-value")
    if variant in {"single", "dual"}:
        _secure(path / "binance-secret", "binance-secret-value")
    if variant in {"dual", "invalid"}:
        _secure(path / "hyperliquid-key", "11" * 32 if variant == "dual" else "invalid")
    settings = Settings(
        trading={
            "enabled": True,
            "bindings": {
                "binance_usdm": {
                    "api_key_file": "binance-key",
                    "api_secret_file": "binance-secret",
                },
                "hyperliquid_perp": {
                    "private_key_file": "hyperliquid-key",
                    "account_address": "0x" + "22" * 20 if variant in {"dual", "invalid"} else None,
                },
            },
        }
    )
    settings.set_config_dir(path)
    return settings


@pytest.mark.usefixtures("postgres_clone_dsn")
@pytest.mark.parametrize(
    ("variant", "expected"),
    [
        ("none", ("unconfigured", "unconfigured")),
        ("single", ("configured", "unconfigured")),
        ("dual", ("configured", "configured")),
        ("invalid", ("invalid", "invalid")),
    ],
)
def test_workers_projects_each_closed_binding_and_forces_capital_paused(
    tmp_path: Path,
    variant: str,
    expected: tuple[str, str],
) -> None:
    connection = connect_postgres_test(read_only=False)
    try:
        connection.execute("UPDATE trading_runtime_state SET control = 'RUNNING' WHERE id = 1")
        connection.commit()

        asyncio.run(project_binding_credentials(_settings(tmp_path, variant), _Database(connection)))

        rows = repositories_for_connection(connection).trading.binding_runtime_rows(now_ms=1_900_000_000_000)
        assert tuple(row.credential_state for row in rows) == expected
        assert connection.execute("SELECT control FROM trading_runtime_state WHERE id = 1").fetchone()["control"] == (
            "PAUSED"
        )
        assert all(row.credential_fingerprint is None or len(row.credential_fingerprint) == 64 for row in rows)
        assert "binance-key-value" not in repr(rows)
        assert "binance-secret-value" not in repr(rows)
        assert [row.runtime_state for row in rows] == [
            "faulted" if state == "invalid" else "stopped" for state in expected
        ]
    finally:
        connection.close()
