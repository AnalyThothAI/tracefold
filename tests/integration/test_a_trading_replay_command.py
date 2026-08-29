"""Replay command authorization at the real PostgreSQL role seam (#286)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from psycopg import conninfo, sql

from tests.postgres_test_utils import connect_postgres_test, postgres_settings_storage
from tracefold.app.repository_session import repositories_for_connection
from tracefold.trading import ExecutionCapabilitySnapshotV1, ExecutionInstrumentCapabilityV1

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def conn(postgres_module_clone_dsn: str):
    connection = connect_postgres_test(read_only=False)
    now_ms = _db_now_ms(connection)
    connection.execute(
        "UPDATE trading_runtime_state SET nautilus_ready = false, "
        "nautilus_unexpected_exposure = false, nautilus_bootstrap_account_zero_at_ms = %s "
        "WHERE id = 1",
        (now_ms,),
    )
    snapshot = ExecutionCapabilitySnapshotV1(
        app_revision="test-revision",
        app_image_digest="test-image",
        nautilus_wheel_identity="test-wheel",
        news_universe_digest="a" * 64,
        provider_universe_digest="b" * 64,
        included={
            "SOLUSDT-PERP.BINANCE": ExecutionInstrumentCapabilityV1(
                instrument_id="SOLUSDT-PERP.BINANCE",
                native_symbol="SOLUSDT",
                underlying_key="crypto:SOL",
                quote_currency="USDT",
                price_precision=2,
                size_precision=3,
                price_increment="0.01",
                size_increment="0.001",
                min_quantity="0.001",
                min_notional="5",
            )
        },
        excluded={},
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
    password = "tracefold-integration-role-password"
    for role in ("tracefold_workers", "tracefold_serve"):
        connection.execute(sql.SQL("ALTER ROLE {} PASSWORD {}").format(sql.Identifier(role), sql.Literal(password)))
    connection.commit()
    storage = postgres_settings_storage()
    for role in ("workers", "serve"):
        parts = conninfo.conninfo_to_dict(storage["postgres"][f"{role}_dsn"])
        parts.update(user=f"tracefold_{role}", password=password)
        storage["postgres"][f"{role}_dsn"] = conninfo.make_conninfo(**parts)
    return storage


def _expire_sol(connection: Any, *, now_ms: int) -> None:
    repositories_for_connection(connection).trading.blacklist_upsert(
        base_symbol="SOL",
        reason="timed_operator_hold",
        expires_at_ms=now_ms - 1,
        now_ms=now_ms - 500,
    )
    connection.commit()


def _run_replay(connection: Any, tmp_path: Path) -> tuple[int, dict[str, Any]]:
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
    return completed.returncode, json.loads(completed.stdout)


def test_replay_handler_materializes_expiry_before_the_serve_snapshot(conn: Any, tmp_path: Any) -> None:
    now_ms = _db_now_ms(conn)
    _expire_sol(conn, now_ms=now_ms)

    code, payload = _run_replay(conn, tmp_path)

    assert code == 0
    assert payload["data"]["terminal"] == "OI_BAR_REPLAY_ATTRIBUTED"
    assert payload["data"]["summary"]["source_count"] == 0
    evidence = conn.execute(
        "SELECT blacklist_revision, "
        "EXISTS (SELECT 1 FROM trading_symbol_blacklist WHERE base_symbol = 'SOL') AS sol_present "
        "FROM trading_runtime_state WHERE id = 1"
    ).fetchone()
    assert evidence == {"blacklist_revision": 2, "sol_present": False}
    assert conn.execute("SELECT count(*) AS n FROM trading_replay_runs").fetchone()["n"] == 1


def test_replay_handler_returns_stable_error_when_workers_cannot_materialize_expiry(
    conn: Any,
    tmp_path: Any,
) -> None:
    now_ms = _db_now_ms(conn)
    _expire_sol(conn, now_ms=now_ms)
    receipts_before = conn.execute("SELECT count(*) AS n FROM trading_replay_runs").fetchone()["n"]
    conn.execute("REVOKE EXECUTE ON FUNCTION materialize_trading_blacklist_expiry() FROM tracefold_workers")
    conn.commit()
    try:
        code, payload = _run_replay(conn, tmp_path)
    finally:
        conn.execute("GRANT EXECUTE ON FUNCTION materialize_trading_blacklist_expiry() TO tracefold_workers")
        conn.commit()

    assert (code, payload) == (1, {"ok": False, "error": "replay_authority_unavailable"})
    assert conn.execute("SELECT count(*) AS n FROM trading_replay_runs").fetchone()["n"] == receipts_before
