from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tests.trading_v3_fixtures import binance_capability, binance_catalog, store_catalog_fixture
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
    if variant in {"single", "invalid"}:
        _secure(path / "binance-key", "binance-key-value")
    if variant == "single":
        _secure(path / "binance-secret", "binance-secret-value")
    settings = Settings(
        trading={
            "enabled": True,
            "bindings": {
                "binance_demo": {
                    "api_key_file": "binance-key",
                    "api_secret_file": "binance-secret",
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
        ("invalid", ("invalid", "unconfigured")),
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


@pytest.mark.usefixtures("postgres_clone_dsn")
@pytest.mark.parametrize(
    ("variant", "expected_state", "expected_reason"),
    [
        ("none", "unconfigured", "recovery_blocked_credentials_missing"),
        ("single", "invalid", "recovery_blocked_account_identity_unproven"),
        ("invalid", "invalid", "recovery_blocked_credentials_invalid"),
    ],
)
def test_credential_projection_never_erases_a_recovery_obligation(
    tmp_path: Path,
    variant: str,
    expected_state: str,
    expected_reason: str,
) -> None:
    connection = connect_postgres_test(read_only=False)
    try:
        connection.execute(
            "UPDATE trading_binding_runtime SET account_state = 'exposure_present' WHERE binding = 'BINANCE_USDM'"
        )
        connection.commit()

        asyncio.run(project_binding_credentials(_settings(tmp_path, variant), _Database(connection)))

        runtime = repositories_for_connection(connection).trading.binding_runtime(
            binding="BINANCE_USDM",
            now_ms=1_900_000_000_000,
        )
        assert runtime is not None
        assert (runtime.credential_state, runtime.account_state, runtime.reason) == (
            expected_state,
            "exposure_present",
            expected_reason,
        )
    finally:
        connection.close()


@pytest.mark.usefixtures("postgres_clone_dsn")
def test_exposure_keeps_its_original_credential_identity_until_restored(tmp_path: Path) -> None:
    connection = connect_postgres_test(read_only=False)
    try:
        settings = _settings(tmp_path, "single")
        asyncio.run(project_binding_credentials(settings, _Database(connection)))
        original = repositories_for_connection(connection).trading.binding_runtime(
            binding="BINANCE_USDM",
            now_ms=1_900_000_000_000,
        )
        assert original is not None and original.credential_fingerprint is not None
        connection.execute(
            "UPDATE trading_binding_runtime SET account_state = 'exposure_present' WHERE binding = 'BINANCE_USDM'"
        )
        connection.commit()

        _secure(tmp_path / "binance-key", "rotated-key-value")
        _secure(tmp_path / "binance-secret", "rotated-secret-value")
        asyncio.run(project_binding_credentials(settings, _Database(connection)))
        blocked = repositories_for_connection(connection).trading.binding_runtime(
            binding="BINANCE_USDM",
            now_ms=1_900_000_000_001,
        )

        assert blocked is not None
        assert blocked.credential_state == "invalid"
        assert blocked.credential_fingerprint == original.credential_fingerprint
        assert blocked.account_generation == original.account_generation
        assert blocked.reason == "recovery_blocked_credential_changed"

        _secure(tmp_path / "binance-key", "binance-key-value")
        _secure(tmp_path / "binance-secret", "binance-secret-value")
        asyncio.run(project_binding_credentials(settings, _Database(connection)))
        restored = repositories_for_connection(connection).trading.binding_runtime(
            binding="BINANCE_USDM",
            now_ms=1_900_000_000_002,
        )
        assert restored is not None
        assert (restored.credential_state, restored.reason) == (
            "configured",
            "binance_demo_recovery_required",
        )
        assert restored.credential_fingerprint == original.credential_fingerprint
        assert restored.account_generation == original.account_generation
    finally:
        connection.close()


@pytest.mark.usefixtures("postgres_clone_dsn")
def test_unchanged_credentials_preserve_a_flat_reconciliation(tmp_path: Path) -> None:
    connection = connect_postgres_test(read_only=False)
    try:
        settings = _settings(tmp_path, "single")
        asyncio.run(project_binding_credentials(settings, _Database(connection)))
        connection.execute(
            "UPDATE trading_binding_runtime SET account_state = 'reconciled_flat' WHERE binding = 'BINANCE_USDM'"
        )
        connection.commit()

        asyncio.run(project_binding_credentials(settings, _Database(connection)))

        runtime = repositories_for_connection(connection).trading.binding_runtime(
            binding="BINANCE_USDM",
            now_ms=1_900_000_000_000,
        )
        assert runtime is not None and runtime.account_state == "reconciled_flat"
    finally:
        connection.close()


@pytest.mark.usefixtures("postgres_clone_dsn")
def test_binding_readiness_expires_from_its_own_heartbeat() -> None:
    connection = connect_postgres_test(read_only=False)
    try:
        connection.execute(
            "UPDATE trading_binding_runtime SET runtime_state = 'ready', heartbeat_at_ms = %s, reason = NULL "
            "WHERE binding = 'BINANCE_USDM'",
            (1_900_000_000_000,),
        )
        repository = repositories_for_connection(connection).trading

        fresh = repository.binding_runtime(binding="BINANCE_USDM", now_ms=1_900_000_005_000)
        stale = repository.binding_runtime(binding="BINANCE_USDM", now_ms=1_900_000_005_001)

        assert fresh is not None and fresh.runtime_state == "ready"
        assert stale is not None
        assert (stale.runtime_state, stale.reason) == ("stale", "binding_heartbeat_stale")
    finally:
        connection.close()


@pytest.mark.usefixtures("postgres_clone_dsn")
def test_status_marks_an_archived_contract_pointer_stale() -> None:
    connection = connect_postgres_test(read_only=False)
    try:
        repository = repositories_for_connection(connection).trading
        catalog = binance_catalog(captured_at_ms=1_900_000_000_000)
        store_catalog_fixture(repository, catalog, now_ms=1_900_000_000_000)
        stale = binance_capability(catalog=catalog).model_copy(update={"adapter_contract_sha256": "0" * 64})
        connection.execute(
            """
            INSERT INTO trading_execution_capability_snapshots (
              snapshot_sha256, created_at_ms, execution_environment,
              binding, venue, catalog_snapshot_sha256, catalog_instrument_count,
              included_count, excluded_count, partition_sha256, payload
            ) VALUES (%s, %s, NULL, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            """,
            (
                stale.snapshot_sha256,
                1_900_000_000_000,
                stale.binding,
                stale.venue,
                stale.catalog_snapshot_sha256,
                stale.catalog_instrument_count,
                stale.included_count,
                stale.excluded_count,
                stale.partition_sha256,
                json.dumps(stale.model_dump(mode="json"), sort_keys=True, separators=(",", ":")),
            ),
        )
        connection.execute(
            "UPDATE trading_binding_runtime SET capability_state = 'ready', "
            "capability_snapshot_sha256 = %s, capability_compiled_at_ms = %s "
            "WHERE binding = 'BINANCE_USDM'",
            (stale.snapshot_sha256, 1_900_000_000_000),
        )

        runtime = repository.binding_runtime(binding="BINANCE_USDM", now_ms=1_900_000_000_001)
        rows = {row.binding: row for row in repository.binding_runtime_rows(now_ms=1_900_000_000_001)}

        assert runtime is not None
        assert (runtime.capability_state, runtime.reason) == ("stale", "capability_contract_mismatch")
        assert (rows["BINANCE_USDM"].capability_state, rows["BINANCE_USDM"].reason) == (
            "stale",
            "capability_contract_mismatch",
        )
    finally:
        connection.close()
