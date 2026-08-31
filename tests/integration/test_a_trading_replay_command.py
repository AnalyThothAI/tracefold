"""Replay command behavior at the real PostgreSQL seam (#286)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.postgres_test_utils import connect_postgres_test, postgres_settings_storage
from tests.trading_v3_fixtures import binance_capability, binance_catalog, store_catalog_fixture
from tracefold.app.repository_session import repositories_for_connection

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def conn(postgres_module_clone_dsn: str):
    connection = connect_postgres_test(read_only=False)
    now_ms = _db_now_ms(connection)
    connection.execute(
        "UPDATE trading_runtime_state SET nautilus_bootstrap_account_zero_at_ms = %s WHERE id = 1",
        (now_ms,),
    )
    catalog = binance_catalog(captured_at_ms=now_ms)
    snapshot = binance_capability(catalog=catalog, app_revision="test-revision")
    store_catalog_fixture(repositories_for_connection(connection).trading, catalog, now_ms=now_ms)
    connection.execute(
        "UPDATE trading_binding_runtime SET account_state = 'reconciled_flat', updated_at_ms = %s "
        "WHERE binding = 'BINANCE_USDM'",
        (now_ms,),
    )
    assert repositories_for_connection(connection).trading.append_and_activate_execution_capability_snapshot(
        snapshot,
        created_at_ms=now_ms,
    )
    connection.commit()
    yield connection
    connection.close()


def _db_now_ms(connection: Any) -> int:
    return int(
        connection.execute("SELECT floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint AS now_ms").fetchone()[
            "now_ms"
        ]
    )


def _runtime_role_storage(connection: Any) -> dict[str, Any]:
    del connection
    return postgres_settings_storage()


def _expire_sol(connection: Any, *, now_ms: int) -> None:
    repositories_for_connection(connection).trading.blacklist_upsert(
        base_symbol="SOL",
        reason="timed_operator_hold",
        expires_at_ms=now_ms - 1,
        now_ms=now_ms - 500,
    )
    connection.commit()


def _run_replay(connection: Any, tmp_path: Path) -> tuple[int, dict[str, Any], str]:
    home = tmp_path / "home"
    app_home = home / ".tracefold"
    app_home.mkdir(parents=True)
    (app_home / "config.yaml").write_text(
        yaml.safe_dump({"storage": _runtime_role_storage(connection)}, sort_keys=False),
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["HOME"] = str(home)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "tracefold",
            "trading",
            "replay-oi",
            "--venues",
            "binance.perp",
            "--out",
            str(tmp_path / "artifacts"),
        ],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
        timeout=30,
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            {"stdout": completed.stdout, "stderr": completed.stderr, "returncode": completed.returncode}
        ) from exc
    return completed.returncode, payload, completed.stderr


def test_replay_handler_materializes_expiry_before_the_serve_snapshot(conn: Any, tmp_path: Any) -> None:
    now_ms = _db_now_ms(conn)
    _expire_sol(conn, now_ms=now_ms)

    code, payload, stderr = _run_replay(conn, tmp_path)

    assert code == 0, {"payload": payload, "stderr": stderr}
    assert payload["data"]["terminal"] == "OI_BAR_REPLAY_ATTRIBUTED"
    assert payload["data"]["summary"]["source_count"] == 0
    evidence = conn.execute(
        "SELECT blacklist_revision, "
        "EXISTS (SELECT 1 FROM trading_symbol_blacklist WHERE base_symbol = 'SOL') AS sol_present "
        "FROM trading_runtime_state WHERE id = 1"
    ).fetchone()
    assert evidence == {"blacklist_revision": 2, "sol_present": False}
    assert conn.execute("SELECT count(*) AS n FROM trading_replay_runs").fetchone()["n"] == 1
