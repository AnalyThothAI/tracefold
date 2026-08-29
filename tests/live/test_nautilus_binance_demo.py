"""Opt-in dynamic Binance USD-M Demo lifecycle proof for #286.

Provide a migrated, empty, isolated PostgreSQL database named
``tracefold_286_live`` for the first run, then retain its terminal ledgers for
the second UTC-day run. Provide one *stopped* container whose exact clean-HEAD
image runs ``tracefold nautilus run`` against it. Mount a test-owned config at
``/root/.tracefold/config.yaml`` and mount the operator-configured
0600 Binance Demo key/secret and Nautilus-role password files at their exact
container paths, read-only. The container must set
``TRACEFOLD_IMAGE_DIGEST`` to that immutable image ID. Then run::

    TRACEFOLD_RUN_BINANCE_DEMO=1 \
    TRACEFOLD_BINANCE_DEMO_IMAGE=sha256:<image-id> \
    TRACEFOLD_BINANCE_DEMO_CONTAINER=<stopped-container> \
    TRACEFOLD_BINANCE_DEMO_CONFIG=<host-config-path> \
    TRACEFOLD_BINANCE_DEMO_POSTGRES_DSN=<isolated-admin-dsn> \
      uv run pytest -q tests/live/test_nautilus_binance_demo.py

The test owns only the intent, one temporary database trigger, kill/restart,
and assertions. The instrument is selected mechanically from the active
capability snapshot minus the current blacklist and prior successful symbols;
there is no target-symbol input. Set
``TRACEFOLD_BINANCE_DEMO_REQUIRE_TWO_DAY=1`` on the second UTC-day run to prove
two different instruments and entry-fence days. The test never copies or
prints credentials. After a post-entry failure it leaves the production
process running for recovery.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import subprocess
import time
import uuid
from contextlib import suppress
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
import pytest
import yaml
from psycopg import conninfo

from tracefold.app.repository_session import repositories_for_connection
from tracefold.platform.config.loader import load_settings
from tracefold.platform.config.secret_file import SecretFileError, read_secure_secret_text
from tracefold.platform.postgres.client import connect_postgres
from tracefold.platform.postgres.migrations import latest_migration_version
from tracefold.trading import (
    INTENT_POLICY_SHA256,
    ExecutionInstrumentCapabilityV1,
    TradeIntent,
    deterministic_client_order_id,
)
from tracefold.trading.execution_policy import evaluate_entry

pytestmark = [pytest.mark.live, pytest.mark.integration, pytest.mark.slow]

_BASE_URL = "https://demo-fapi.binance.com"
_LOCK = 286_031_800
_TARGET_NOTIONAL = Decimal("9.50")


class _Demo:
    def __init__(self, key: str, secret: str) -> None:
        self._secret = secret.encode()
        self._client = httpx.Client(base_url=_BASE_URL, headers={"X-MBX-APIKEY": key}, timeout=10)
        self._offset_ms = int(self.get("/fapi/v1/time", signed=False)["serverTime"]) - int(time.time() * 1_000)

    def close(self) -> None:
        self._client.close()

    def get(self, path: str, params: dict[str, object] | None = None, *, signed: bool = True) -> Any:
        query = dict(params or {})
        if signed:
            query.update(timestamp=int(time.time() * 1_000) + self._offset_ms, recvWindow=5_000)
            encoded = urlencode(query)
            query["signature"] = hmac.new(self._secret, encoded.encode(), hashlib.sha256).hexdigest()
        try:
            response = self._client.get(path, params=query)
            if response.status_code != 200:
                raise RuntimeError(f"binance_demo_http_{response.status_code}:{path}")
            return response.json()
        except (httpx.HTTPError, json.JSONDecodeError):
            raise RuntimeError(f"binance_demo_unavailable:{path}") from None

    def orders(self, symbol: str, start_ms: int) -> list[dict[str, Any]]:
        return list(self.get("/fapi/v1/allOrders", {"symbol": symbol, "startTime": start_ms, "limit": 1_000}))

    def algo_orders(self, symbol: str, start_ms: int) -> list[dict[str, Any]]:
        return list(self.get("/fapi/v1/allAlgoOrders", {"symbol": symbol, "startTime": start_ms, "limit": 1_000}))

    def positions(self) -> list[dict[str, Any]]:
        return list(self.get("/fapi/v2/positionRisk"))

    def now_ms(self) -> int:
        return int(time.time() * 1_000) + self._offset_ms


def _docker(*args: str, check: bool = True) -> str:
    result = subprocess.run(["docker", *args], capture_output=True, check=False, text=True, timeout=60)
    if check and result.returncode:
        raise RuntimeError(f"docker_{args[0]}_failed")
    return result.stdout.strip()


def _running(container: str) -> bool:
    return _docker("inspect", "--format", "{{.State.Running}}", container, check=False) == "true"


def _wait[T](label: str, seconds: int, read: Any, accept: Any) -> T:
    deadline = time.monotonic() + seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            value = read()
            if accept(value):
                return value
        except Exception as exc:
            last_error = exc
        time.sleep(1)
    suffix = "" if last_error is None else f":{type(last_error).__name__}"
    raise AssertionError(f"timed_out_waiting_for_{label}{suffix}")


def _mounted(
    inspect: dict[str, Any],
    source: Path,
    destination: str,
    *,
    read_only: bool,
) -> bool:
    for mount in inspect["Mounts"]:
        with suppress(OSError):
            if (
                Path(mount["Source"]).samefile(source)
                and mount["Destination"] == destination
                and (not read_only or mount.get("RW") is False)
            ):
                return True
    return False


def _block_projection(conn: Any) -> None:
    conn.execute(
        f"""
        CREATE FUNCTION tf286_block_entry_projection() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF OLD.actual_quantity IS NULL AND NEW.actual_quantity IS NOT NULL THEN
            PERFORM pg_advisory_xact_lock({_LOCK});
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER tf286_block_entry_projection BEFORE UPDATE OF actual_quantity ON trading_intents
          FOR EACH ROW EXECUTE FUNCTION tf286_block_entry_projection();
        """
    )
    conn.execute("SELECT pg_advisory_lock(%s)", (_LOCK,))


def _unblock_projection(conn: Any) -> None:
    conn.execute("SELECT pg_advisory_unlock(%s)", (_LOCK,))
    conn.execute("DROP TRIGGER IF EXISTS tf286_block_entry_projection ON trading_intents")
    conn.execute("DROP FUNCTION IF EXISTS tf286_block_entry_projection()")


def _outcome(conn: Any, intent_id: str) -> Any:
    return repositories_for_connection(conn).trading.intent_outcome(intent_id)


def _create_intent(
    conn: Any,
    capability: ExecutionInstrumentCapabilityV1,
    reference_price: Decimal,
) -> TradeIntent:
    now_ms = int(time.time() * 1_000) - 1_000
    manifest_sha = "286".ljust(64, "0")
    repos = repositories_for_connection(conn)
    with repos.transaction():
        snapshot = repos.trading.active_execution_capability_snapshot(for_update=True)
        if snapshot is None or snapshot.included.get(capability.instrument_id) != capability:
            pytest.fail("active capability snapshot does not include the Demo instrument", pytrace=False)
        blacklist = repos.trading.blacklist_snapshot(now_ms=now_ms, materialize_expiry=True)
        if any(row.underlying_key == capability.underlying_key for row in blacklist.active_rows):
            pytest.fail("mechanically selected Demo instrument became blacklisted", pytrace=False)
        intent = TradeIntent.create(
            case_id=f"case-binance-demo-{uuid.uuid4().hex}",
            case_manifest_sha256=manifest_sha,
            execution_capability_snapshot_sha256=snapshot.snapshot_sha256,
            blacklist_snapshot=blacklist,
            instrument_id=capability.instrument_id,
            underlying_key=capability.underlying_key,
            created_at_ms=now_ms,
            reference_price=reference_price,
            target_notional_usd=_TARGET_NOTIONAL,
        )
        conn.execute(
            """
            INSERT INTO trading_cases (
              case_id, underlying_key, trigger_kind, strategy_id, strategy_version,
              strategy_config_digest, primary_source_key, supplemental_source_keys,
              manifest, manifest_sha256, state, observed_at_ms, created_at_ms, updated_at_ms
            ) VALUES (%s, %s, 'oi', 'binance_oi_smart_money_long_v2',
                      'binance_oi_smart_money_long_v2', %s, %s, '[]'::jsonb,
                      '{}'::jsonb, %s, 'INTENT_EMITTED', %s, %s, %s)
            """,
            (
                intent.case_id,
                capability.underlying_key,
                "0" * 64,
                intent.intent_id,
                manifest_sha,
                now_ms,
                now_ms,
                now_ms,
            ),
        )
        assert repos.trading.insert_intent(intent)
    return intent


def _select_capability(
    conn: Any,
    venue: _Demo,
) -> tuple[ExecutionInstrumentCapabilityV1, Decimal]:
    """Choose the first currently executable unused capability; no symbol is operator-selected."""

    now_ms = venue.now_ms()
    repos = repositories_for_connection(conn)
    with repos.transaction():
        snapshot = repos.trading.active_execution_capability_snapshot(for_update=True)
        if snapshot is None:
            pytest.fail("active capability snapshot is missing", pytrace=False)
        blacklist = repos.trading.blacklist_snapshot(now_ms=now_ms, materialize_expiry=True)
        used = {
            str(row["instrument_id"])
            for row in conn.execute(
                "SELECT instrument_id FROM trading_intents WHERE terminal_outcome = 'CLOSED_FLAT'"
            ).fetchall()
        }
    denied = {row.underlying_key for row in blacklist.active_rows}
    tickers = {
        str(row["symbol"]): row
        for row in venue.get("/fapi/v1/ticker/bookTicker", signed=False)
        if isinstance(row, dict) and row.get("symbol")
    }
    for instrument_id in sorted(snapshot.included):
        capability = snapshot.included[instrument_id]
        if capability.instrument_id in used or capability.underlying_key in denied:
            continue
        ticker = tickers.get(capability.native_symbol)
        if ticker is None:
            continue
        bid = Decimal(str(ticker["bidPrice"]))
        ask = Decimal(str(ticker["askPrice"]))
        decision = evaluate_entry(
            now_ms=now_ms,
            created_at_ms=now_ms - 1,
            valid_until_ms=now_ms + 60_000,
            quote_at_ms=now_ms,
            bid=bid,
            ask=ask,
            reference_price=ask,
            target_notional=_TARGET_NOTIONAL,
            size_increment=Decimal(capability.size_increment),
            min_quantity=None if capability.min_quantity is None else Decimal(capability.min_quantity),
            min_notional=None if capability.min_notional is None else Decimal(capability.min_notional),
            max_spread_bps=30,
            max_drift_bps=25,
        )
        if decision.quantity is not None:
            return capability, ask
    pytest.fail("active capability snapshot has no unused quantity-executable Demo instrument", pytrace=False)


def _assert_two_day_evidence(conn: Any) -> None:
    if os.environ.get("TRACEFOLD_BINANCE_DEMO_REQUIRE_TWO_DAY") != "1":
        return
    rows = conn.execute(
        """
        SELECT instrument_id, entry_fenced_at_ms / 86400000 AS utc_day
          FROM trading_intents
         WHERE terminal_outcome = 'CLOSED_FLAT'
           AND entry_fenced_at_ms IS NOT NULL
         ORDER BY entry_fenced_at_ms
        """
    ).fetchall()
    assert len({str(row["instrument_id"]) for row in rows}) >= 2
    assert len({int(row["utc_day"]) for row in rows}) >= 2


def _preconditions(
    container: str,
    image: str,
    config_path: Path,
    key_path: Path,
    secret_path: Path,
    postgres_password_path: Path,
) -> str:
    root = Path(__file__).resolve().parents[2]
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=root,
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, check=True, text=True
    ).stdout.strip()
    if dirty:
        pytest.fail("Binance Demo acceptance requires a clean checkout", pytrace=False)
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        pytest.fail("TRACEFOLD_BINANCE_DEMO_IMAGE must be an image ID", pytrace=False)
    inspected = json.loads(_docker("inspect", container))[0]
    revision = _docker(
        "image",
        "inspect",
        "--format",
        '{{index .Config.Labels "org.opencontainers.image.revision"}}',
        image,
    )
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    try:
        postgres_config = config["storage"]["postgres"]
        nautilus_config = config["trading"]["nautilus"]
        dsn = conninfo.conninfo_to_dict(str(postgres_config["nautilus_dsn"]))
        config_bound = bool(
            dsn.get("user") == "tracefold_nautilus"
            and dsn.get("dbname") == "tracefold_286_live"
            and dsn.get("host") == "postgres"
            and dsn.get("port", "5432") == "5432"
            and "password" not in dsn
            and postgres_config.get("nautilus_password_file", "postgres_nautilus_password")
            == "postgres_nautilus_password"
            and nautilus_config.get("api_key_file", "binance_demo_api_key") == "binance_demo_api_key"
            and nautilus_config.get("api_secret_file", "binance_demo_api_secret") == "binance_demo_api_secret"
        )
    except Exception:
        pytest.fail("Binance Demo harness config is not bound to the isolated Nautilus role", pytrace=False)
    container_env = set(inspected["Config"]["Env"])
    if not (
        inspected["Image"] == image
        and revision == head
        and not inspected["State"]["Running"]
        and inspected["HostConfig"]["RestartPolicy"]["Name"] in {"", "no"}
        and inspected["Config"]["Cmd"] == ["tracefold", "nautilus", "run"]
        and config_bound
        and _mounted(inspected, config_path, "/root/.tracefold/config.yaml", read_only=True)
        and _mounted(inspected, key_path, "/root/.tracefold/binance_demo_api_key", read_only=True)
        and _mounted(inspected, secret_path, "/root/.tracefold/binance_demo_api_secret", read_only=True)
        and _mounted(
            inspected,
            postgres_password_path,
            "/root/.tracefold/postgres_nautilus_password",
            read_only=True,
        )
        and f"TRACEFOLD_IMAGE_DIGEST={image}" in container_env
    ):
        pytest.fail("stopped Nautilus container does not satisfy the live setup contract", pytrace=False)
    return head


def test_binance_demo_entry_restart_and_max_holding_close() -> None:
    if os.environ.get("TRACEFOLD_RUN_BINANCE_DEMO") != "1":
        pytest.skip("set TRACEFOLD_RUN_BINANCE_DEMO=1 and the four setup variables documented above")
    required = {
        name: os.environ.get(name, "").strip()
        for name in (
            "TRACEFOLD_BINANCE_DEMO_IMAGE",
            "TRACEFOLD_BINANCE_DEMO_CONTAINER",
            "TRACEFOLD_BINANCE_DEMO_CONFIG",
            "TRACEFOLD_BINANCE_DEMO_POSTGRES_DSN",
        )
    }
    if not all(required.values()):
        pytest.fail("Binance Demo live setup variables are incomplete", pytrace=False)
    image = required["TRACEFOLD_BINANCE_DEMO_IMAGE"]
    container = required["TRACEFOLD_BINANCE_DEMO_CONTAINER"]
    config_path = Path(required["TRACEFOLD_BINANCE_DEMO_CONFIG"])

    settings = load_settings(require_ws_token=False)
    operator_config = settings.app_home / "config.yaml"
    if config_path.is_symlink() or not config_path.is_file() or config_path.stat().st_mode & 0o077:
        pytest.fail("Binance Demo harness config must be a regular 0600 test file", pytrace=False)
    with suppress(OSError):
        if config_path.samefile(operator_config):
            pytest.fail("Binance Demo harness refuses the operator runtime config", pytrace=False)
    key_path = settings.trading_nautilus_api_key_file()
    secret_path = settings.trading_nautilus_api_secret_file()
    postgres_password_path = settings.postgres_password_file("nautilus")
    if key_path is None or secret_path is None or postgres_password_path is None:
        pytest.fail("operator Binance Demo credential files are not configured", pytrace=False)
    try:
        key, secret = read_secure_secret_text(key_path), read_secure_secret_text(secret_path)
        read_secure_secret_text(postgres_password_path)
    except SecretFileError as exc:
        pytest.fail(f"operator secret file rejected:{exc.code}", pytrace=False)
    head = _preconditions(
        container,
        image,
        config_path,
        key_path,
        secret_path,
        postgres_password_path,
    )

    try:
        conn = connect_postgres(required["TRACEFOLD_BINANCE_DEMO_POSTGRES_DSN"])
    except Exception:
        pytest.fail("isolated Binance Demo PostgreSQL is unavailable", pytrace=False)
    venue = _Demo(key, secret)
    blocked = False
    capital_possible = False
    flat_seen = False
    start_ms = venue.now_ms() - 1_000
    try:
        version = conn.execute(
            "SELECT current_database() AS database_name, version_num FROM alembic_version"
        ).fetchone()
        counts = conn.execute(
            """
            SELECT count(*) FILTER (
                     WHERE execution_state IN ('PENDING', 'IN_FLIGHT', 'OPEN_PROTECTED', 'MANUAL_REVIEW')
                   ) AS nonterminal_intents,
                   count(*) FILTER (
                     WHERE entry_fenced_at_ms >= %s AND entry_fenced_at_ms < %s
                   ) AS entries_today
              FROM trading_intents
            """,
            (
                venue.now_ms() // 86_400_000 * 86_400_000,
                (venue.now_ms() // 86_400_000 + 1) * 86_400_000,
            ),
        ).fetchone()
        runtime = conn.execute(
            "SELECT control, active_capability_snapshot_sha256 FROM trading_runtime_state WHERE id = 1"
        ).fetchone()
        if not (
            version
            and version["database_name"] == "tracefold_286_live"
            and version["version_num"] == latest_migration_version()
            and counts
            and counts["nonterminal_intents"] == 0
            and counts["entries_today"] == 0
            and runtime
            and runtime["control"] == "PAUSED"
            and runtime["active_capability_snapshot_sha256"] is not None
        ):
            pytest.fail(
                "isolated PostgreSQL must be migrated, PAUSED, active-snapshot bound, "
                "and free of nonterminal/today-entry rows",
                pytrace=False,
            )
        if venue.get("/fapi/v1/positionSide/dual")["dualSidePosition"] is not False:
            pytest.fail("Binance Demo account must use one-way mode", pytrace=False)
        if (
            venue.get("/fapi/v1/openOrders")
            or venue.get("/fapi/v1/openAlgoOrders")
            or any(Decimal(str(row["positionAmt"])) != 0 for row in venue.positions())
        ):
            pytest.fail("Binance Demo account must be dedicated and flat", pytrace=False)

        _block_projection(conn)
        blocked = True
        process_started_at_ms = int(time.time() * 1_000)
        _docker("start", container)
        repos = repositories_for_connection(conn)
        _wait(
            "paused_readiness",
            90,
            repos.trading.nautilus_runtime_state,
            lambda row: bool(
                row
                and row["nautilus_ready"]
                and not row["nautilus_unexpected_exposure"]
                and row["nautilus_heartbeat_at_ms"] is not None
                and row["nautilus_heartbeat_at_ms"] >= process_started_at_ms
            ),
        )
        capability, ask = _select_capability(conn, venue)
        symbol = capability.native_symbol
        selected_position = next(row for row in venue.positions() if row["symbol"] == symbol)
        assert int(selected_position["leverage"]) == 1
        assert Decimal(str(selected_position["positionAmt"])) == 0
        conn.execute("UPDATE trading_runtime_state SET control = 'RUNNING' WHERE id = 1")
        intent = _create_intent(conn, capability, ask)
        capital_possible = True
        entry_id = deterministic_client_order_id(intent.intent_id, "entry")
        stop_id = deterministic_client_order_id(intent.intent_id, "stop")
        close_id = deterministic_client_order_id(intent.intent_id, "close")

        def entry_and_stop() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
            orders, algos = venue.orders(symbol, start_ms), venue.algo_orders(symbol, start_ms)
            entry = [row for row in orders if row["clientOrderId"] == entry_id and row["status"] == "FILLED"]
            stop = [row for row in algos if row["clientAlgoId"] == stop_id and row["algoStatus"] == "NEW"]
            return (orders, algos) if len(entry) == len(stop) == 1 else ([], [])

        accepted_orders, accepted_algos = _wait(
            "entry_and_stop", 90, entry_and_stop, lambda pair: bool(pair[0] and pair[1])
        )
        entry = next(row for row in accepted_orders if row["clientOrderId"] == entry_id)
        stop = next(row for row in accepted_algos if row["clientAlgoId"] == stop_id)
        assert entry["side"] == "BUY" and entry["reduceOnly"] is False
        assert (
            stop["orderType"] == "STOP_MARKET"
            and stop["side"] == "SELL"
            and stop["reduceOnly"] is True
            and stop["closePosition"] is not True
            and Decimal(str(stop["quantity"])) == Decimal(str(entry["executedQty"]))
        )
        assert Decimal(str(entry["executedQty"])) * Decimal(str(entry["avgPrice"])) <= Decimal("10")
        _wait(
            "blocked_projection",
            20,
            lambda: conn.execute(
                "SELECT count(*) AS n FROM pg_stat_activity WHERE usename='tracefold_nautilus' "
                "AND wait_event_type='Lock' AND wait_event='advisory'"
            ).fetchone()["n"],
            lambda count: count == 1,
        )
        assert _outcome(conn, intent.intent_id).actual_quantity is None

        _docker("kill", "--signal", "KILL", container)
        _wait("killed_process", 15, lambda: _running(container), lambda value: not value)
        assert len([row for row in venue.orders(symbol, start_ms) if row["clientOrderId"] == entry_id]) == 1
        _unblock_projection(conn)
        blocked = False
        _docker("start", container)
        protected = _wait(
            "restart_open_protected",
            90,
            lambda: _outcome(conn, intent.intent_id),
            lambda row: bool(row and row.execution_state == "OPEN_PROTECTED"),
        )
        assert protected.stop_client_order_id == stop_id
        assert protected.opened_at_ms is not None
        close_deadline_ms = protected.opened_at_ms + intent.max_holding_ms
        assert venue.now_ms() < close_deadline_ms
        assert [
            row["clientOrderId"] for row in venue.orders(symbol, start_ms) if row["clientOrderId"].startswith("tf-e-")
        ] == [entry_id]
        assert [row for row in venue.orders(symbol, start_ms) if row["clientOrderId"] == close_id] == []
        closed = _wait(
            "closed_flat",
            300,
            lambda: _outcome(conn, intent.intent_id),
            lambda row: bool(row and row.execution_state == "TERMINAL" and row.terminal_outcome == "CLOSED_FLAT"),
        )
        assert closed.close_client_order_id == close_id and closed.flat_verified_at_ms is not None
        assert closed.close_submitted_at_ms is not None
        assert closed.close_submitted_at_ms >= close_deadline_ms
        assert closed.engine_identity is not None
        assert f"tracefold@{head};" in closed.engine_identity
        assert f"image@{image};" in closed.engine_identity
        assert "nautilus@1.231.0+27a8e54e7ac3c57d6cbf8891f0283dfbaee97317;" in closed.engine_identity
        assert "wheel@cp313-cp313-manylinux_2_35_" in closed.engine_identity
        assert f"intent-policy@{INTENT_POLICY_SHA256}" in closed.engine_identity

        orders, algos = _wait(
            "terminal_owned_orders",
            30,
            lambda: (venue.orders(symbol, start_ms), venue.algo_orders(symbol, start_ms)),
            lambda pair: (
                len([row for row in pair[0] if row["clientOrderId"] == entry_id and row["status"] == "FILLED"]) == 1
                and len([row for row in pair[0] if row["clientOrderId"] == close_id and row["status"] == "FILLED"]) == 1
                and len(
                    [
                        row
                        for row in pair[1]
                        if row["clientAlgoId"] == stop_id and row["algoStatus"] in {"CANCELED", "EXPIRED"}
                    ]
                )
                == 1
            ),
        )
        by_id = {row["clientOrderId"]: row for row in orders}
        stop = next(row for row in algos if row["clientAlgoId"] == stop_id)
        assert by_id[entry_id]["status"] == by_id[close_id]["status"] == "FILLED"
        assert stop["algoStatus"] in {"CANCELED", "EXPIRED"}
        fresh_position = next(row for row in venue.positions() if row["symbol"] == symbol)
        assert Decimal(str(fresh_position["positionAmt"])) == 0
        flat_seen = True
        trades = venue.get("/fapi/v1/userTrades", {"symbol": symbol, "startTime": start_ms, "limit": 1_000})
        assert {str(row["orderId"]) for row in trades if row["buyer"]} == {str(by_id[entry_id]["orderId"])}
        assert str(by_id[close_id]["orderId"]) in {str(row["orderId"]) for row in trades if not row["buyer"]}
        _assert_two_day_evidence(conn)
    except BaseException as exc:
        with suppress(Exception):
            conn.execute("UPDATE trading_runtime_state SET control = 'PAUSED' WHERE id = 1")
        if blocked:
            with suppress(Exception):
                _unblock_projection(conn)
        if capital_possible and not flat_seen:
            if not _running(container):
                with suppress(Exception):
                    _docker("start", container)
            exc.add_note(f"Demo exposure may remain; production recovery container {container} was left running")
        else:
            _docker("stop", "-t", "15", container, check=False)
        raise
    else:
        _docker("stop", "-t", "15", container)
    finally:
        venue.close()
        conn.close()
