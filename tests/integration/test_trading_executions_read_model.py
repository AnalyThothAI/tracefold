"""The desk table over a real Signal's whole durable execution (#528 PR-1).

The observations are written by the pinned Nautilus Runtime in its own process, never hand-built
here: what makes this a read-model test rather than a fixture test is that the row it renders is
folded from the exact summaries the production writer produces.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.nautilus_oi_runtime_fixtures import NOW_NS
from tests.postgres_test_utils import connect_postgres_test, postgres_settings_storage
from tracefold.app.http.app import create_app
from tracefold.platform.config.models import Settings
from tracefold.trading.storage.execution_stream import prepare_operator_intent, prepare_trade_signal
from tracefold.trading.storage.root import TradingRepository

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_clone_dsn")]

_ACCOUNT_SLOT = "binance_usdm_primary"
_SIGNAL_ID = "1" * 64
_FLATTEN_COMMAND_ID = "b" * 64
_MANUAL_ENTRY_COMMAND_ID = "c" * 64
_MANUAL_FLATTEN_COMMAND_ID = "d" * 64
_MARKET_KEY = "crypto:perp:BTC:USDT"
TOKEN = "executions-read-model-token"


def _seed_signal() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        repo = TradingRepository(conn)
        with conn.transaction():
            repo.ensure_execution_runtime_control_state(_ACCOUNT_SLOT, now_ns=NOW_NS)
        prepared = prepare_trade_signal(
            signal_id=_SIGNAL_ID,
            case_id="case-1",
            market_key="crypto:perp:BTC:USDT",
            direction="long",
            observed_at_ns=NOW_NS - 1_000_000,
            expires_at_ns=NOW_NS + 60_000_000_000,
        )
        with conn.transaction():
            conn.execute(
                """
                INSERT INTO trading_cases (
                  case_id, underlying_key, trigger_kind, primary_source_key,
                  manifest, manifest_sha256, state,
                  policy_decision, policy_reason, observed_at_ms, created_at_ms, decided_at_ms,
                  updated_at_ms
                ) VALUES (
                  'case-1', 'crypto:BTC', 'oi', 'runtime-source:case-1',
                  '{"test":"executions"}'::jsonb, %s, 'SIGNAL_EMITTED', 'long',
                  'executions_read_model', 1, 1, 1, 1
                )
                """,
                ("4" * 64,),
            )
            repo.append_trade_signal(prepared)
    finally:
        conn.close()


def _run_runtime(dsn: str, mode: str) -> dict[str, object]:
    result = subprocess.run(
        [sys.executable, "-m", "tests.helpers.nautilus_oi_runtime_process", dsn, mode],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def _executions(tmp_path: Path) -> dict[str, object]:
    settings = Settings(ws_token=TOKEN, storage=postgres_settings_storage())
    settings.set_config_dir(tmp_path / "app-home")
    with TestClient(create_app(settings=settings)) as client:
        response = client.get("/api/trading/executions", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_a_stopped_out_signal_is_one_closed_row_with_its_exit_price_and_realized_result(
    postgres_clone_dsn: str,
    tmp_path: Path,
) -> None:
    """#528 A/B. Before this change the `closed` position fact was `{status, quantity: 0}`: no exit
    price, no realized result, no reason, so the one row an operator reads a finished trade off could
    not say how it ended. The read model then had nothing to fold, and there was no read model.
    """

    _seed_signal()
    receipt = _run_runtime(postgres_clone_dsn, "stop_filled")
    assert receipt["positions_count"] == 0, receipt

    data = _executions(tmp_path)
    assert data["complete"] is True
    rows = [row for row in data["executions"] if row["entry_id"] == _SIGNAL_ID]
    assert len(rows) == 1, data["executions"]
    row = rows[0]

    assert row["stage"] == "closed"
    assert row["source"] == "signal"
    assert row["case_id"] == "case-1"
    assert row["market_key"] == "crypto:perp:BTC:USDT"
    assert row["direction"] == "long"
    assert row["disposition_reason"] == "accepted"
    assert row["exit_reason"] == "stop_filled"
    assert row["realized_pnl_usd"] is not None
    assert float(row["realized_pnl_usd"]) < 0
    assert row["exit_price"] is not None and float(row["exit_price"]) < float(row["fill_avg_price"])
    # `closed` reports the quantity that was open, not the zero the Runtime's own counter had reached.
    assert row["fill_quantity"] == "0.049"
    assert float(row["stop_trigger_price"]) < float(row["fill_avg_price"])
    # #537 PR-5. The venue's own `order_status` and `position_status` are what `stage` is derived
    # from, and the `accepted` / `rejected` split beside it said what `closed` already says.
    assert {"disposition", "order_status", "position_status", "last_observed_at_ns"}.isdisjoint(row)

    verify = connect_postgres_test(read_only=False)
    try:
        closed = verify.execute(
            """
            SELECT summary
              FROM trading_execution_observations
             WHERE normalized_kind = 'position' AND summary ->> 'status' = 'closed'
            """
        ).fetchone()
    finally:
        verify.close()
    assert closed["summary"]["quantity"] == "0.049"
    assert set(closed["summary"]) == {
        "status",
        "quantity",
        "avg_entry_price",
        "exit_price",
        "realized_pnl_usd",
        "exit_reason",
    }


def test_a_flattened_signal_names_the_operator_exit_and_its_command_stays_recorded(
    postgres_clone_dsn: str,
    tmp_path: Path,
) -> None:
    """The exit order carries the entry Signal's `signal_id`, which is why the fold is by Signal."""

    _seed_signal()
    conn = connect_postgres_test(read_only=False)
    try:
        repo = TradingRepository(conn)
        with conn.transaction():
            repo.append_operator_intent(
                _flatten_intent(),
            )
    finally:
        conn.close()

    receipt = _run_runtime(postgres_clone_dsn, "flatten_owned")
    assert receipt["positions_count"] == 0, receipt
    assert receipt["admitted_commands"] == 0, receipt

    data = _executions(tmp_path)
    row = next(item for item in data["executions"] if item["entry_id"] == _SIGNAL_ID)
    assert row["source"] == "signal"
    assert row["stage"] == "closed"
    assert row["exit_reason"] == "flatten"
    assert row["realized_pnl_usd"] is not None
    assert row["fill_quantity"] == "0.049"

    command = next(item for item in data["commands"] if item["command_id"] == _FLATTEN_COMMAND_ID)
    # A flatten writes no `control_disposition` until the private report proves the slot went flat,
    # and this harness runs no reconciliation. `recorded` is the honest answer: the exposure this
    # Command closed is on the Signal row above, and reading it back onto the Command would be
    # exactly the venue correlation #528 C deleted.
    assert command["stage"] == "recorded"
    assert command["action"] == "flatten"


def test_a_manual_entry_is_its_own_row_and_carries_the_same_close_facts_as_a_signal(
    postgres_clone_dsn: str,
    tmp_path: Path,
) -> None:
    """#528 PR-3. The manual entry is the one ingress an operator can prove the chain with, and the
    Runtime writes its order, fill, protection and position facts under the Command's own id. Folding
    only by `signal_id` left the whole trade out of the desk table: block 3 showed `manual_entry
    accepted` and block 4 showed nothing, so the fills, the exit and the realized result an operator
    just produced were invisible where they are read.
    """

    _seed_manual_entry()
    receipt = _run_runtime(postgres_clone_dsn, "manual_entry_flatten")
    assert receipt["admitted_commands"] == 1, receipt
    assert receipt["positions_count"] == 0, receipt

    data = _executions(tmp_path)
    rows = [row for row in data["executions"] if row["entry_id"] == _MANUAL_ENTRY_COMMAND_ID]
    assert len(rows) == 1, data["executions"]
    row = rows[0]

    assert row["source"] == "manual"
    assert row["case_id"] is None
    assert row["market_key"] == _MARKET_KEY
    assert row["direction"] == "long"
    assert row["observed_at_ns"] == NOW_NS
    assert row["disposition_reason"] == "accepted"
    assert row["stage"] == "closed"
    assert row["exit_reason"] == "flatten"
    assert row["realized_pnl_usd"] is not None
    assert row["exit_price"] is not None
    assert row["fill_quantity"] is not None and float(row["fill_quantity"]) > 0
    assert row["fill_avg_price"] is not None
    assert row["stop_trigger_price"] is not None
    assert {"disposition", "order_status", "position_status", "last_observed_at_ns"}.isdisjoint(row)

    # `commands[]` is unchanged: the same manual entry is still one instruction record beside the
    # execution row it produced, and the flatten that closed it is another.
    command = next(item for item in data["commands"] if item["command_id"] == _MANUAL_ENTRY_COMMAND_ID)
    assert command["action"] == "manual_entry"
    assert command["stage"] == "accepted"
    assert {item["command_id"] for item in data["commands"]} == {
        _MANUAL_ENTRY_COMMAND_ID,
        _MANUAL_FLATTEN_COMMAND_ID,
    }


def _seed_manual_entry() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        repo = TradingRepository(conn)
        with conn.transaction():
            repo.ensure_execution_runtime_control_state(_ACCOUNT_SLOT, now_ns=NOW_NS)
        with conn.transaction():
            repo.append_operator_intent(
                prepare_operator_intent(
                    command_id=_MANUAL_ENTRY_COMMAND_ID,
                    account_slot=_ACCOUNT_SLOT,
                    action="manual_entry",
                    scope="market",
                    reason="manual long",
                    operator_identity="operator:test",
                    authentication_identity="test:authenticated",
                    requested_at_ns=NOW_NS,
                    expires_at_ns=NOW_NS + 60_000_000_000,
                    market_key=_MARKET_KEY,
                    direction="long",
                )
            )
            repo.append_operator_intent(_flatten_intent(command_id=_MANUAL_FLATTEN_COMMAND_ID))
    finally:
        conn.close()


def _flatten_intent(*, command_id: str = _FLATTEN_COMMAND_ID):
    return prepare_operator_intent(
        command_id=command_id,
        account_slot=_ACCOUNT_SLOT,
        action="flatten",
        scope="account",
        reason="executions read model",
        operator_identity="operator:test",
        authentication_identity="test:authenticated",
        requested_at_ns=NOW_NS,
        expires_at_ns=NOW_NS + 60_000_000_000,
        market_key=None,
        direction=None,
    )
