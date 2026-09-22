"""The desk table over a real entry's whole durable execution (#528 PR-1, #680 PR-1).

The observations and plans are written by the production Strategy on a real Nautilus engine through
the production bridge cycle (`tests/helpers/nautilus_oi_runtime_process.py`), never hand-built, where
the scenario can be run: what makes this a read-model test rather than a fixture test is that the row
it renders is folded from the exact summaries the production writer produces. Realized PnL is folded
from the fill journal -- exit minus entry notional, signed, less every commission -- and is checked
against Nautilus' own realized PnL for the same position.
"""

from __future__ import annotations

import time
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.helpers.nautilus_oi_runtime_process import PostgresRuntime, run_runtime_on_postgres
from tests.nautilus_oi_runtime_fixtures import (
    MARKET,
    NOW_NS,
    SECOND_NS,
    oi_profile,
    open_plan,
    quotes,
    seed_reconciled_position,
)
from tests.postgres_test_utils import connect_postgres_test, postgres_settings_storage
from tracefold.app.http.app import create_app
from tracefold.app.repository_session import repositories_for_connection
from tracefold.platform.config.models import Settings
from tracefold.trading.execution_contracts import ExecutionObservationV1
from tracefold.trading.storage.execution_stream import (
    prepare_execution_observations,
    prepare_operator_intent,
    prepare_trade_signal,
)
from tracefold.trading.storage.root import TradingRepository
from tracefold.trading.storage.trade_plans import prepare_trade_plan

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_clone_dsn")]

_ACCOUNT_SLOT = "binance_usdm_primary"
_SIGNAL_ID = "1" * 64
_FLATTEN_COMMAND_ID = "b" * 64
_MANUAL_ENTRY_COMMAND_ID = "c" * 64
TOKEN = "executions-read-model-token"


def _seed_signal(
    *,
    signal_id: str = _SIGNAL_ID,
    case_id: str = "case-1",
    observed_at_ns: int = NOW_NS - 1_000_000,
    expires_at_ns: int = NOW_NS + 60 * SECOND_NS,
) -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        repo = TradingRepository(conn)
        with conn.transaction():
            repo.ensure_execution_runtime_control_state(_ACCOUNT_SLOT, now_ns=NOW_NS)
            conn.execute(
                """
                INSERT INTO trading_cases (
                  case_id, underlying_key, trigger_kind, primary_source_key,
                  manifest, manifest_sha256, state,
                  policy_decision, policy_reason, observed_at_ms, created_at_ms, decided_at_ms,
                  updated_at_ms
                ) VALUES (
                  %s, 'crypto:BTC', 'oi', %s,
                  '{"test":"executions"}'::jsonb, %s, 'SIGNAL_EMITTED', 'long',
                  'executions_read_model', 1, 1, 1, 1
                )
                """,
                (case_id, f"runtime-source:{case_id}", "4" * 64),
            )
            repo.append_trade_signal(
                prepare_trade_signal(
                    signal_id=signal_id,
                    case_id=case_id,
                    market_key=MARKET,
                    direction="long",
                    observed_at_ns=observed_at_ns,
                    expires_at_ns=expires_at_ns,
                )
            )
    finally:
        conn.close()


def _append_command(*, command_id: str, action: str, market_key: str | None = None) -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        repo = TradingRepository(conn)
        with conn.transaction():
            repo.ensure_execution_runtime_control_state(_ACCOUNT_SLOT, now_ns=NOW_NS)
            repo.append_operator_intent(
                prepare_operator_intent(
                    command_id=command_id,
                    account_slot=_ACCOUNT_SLOT,
                    action=action,
                    scope="market" if action == "manual_entry" else "account",
                    reason="executions read model",
                    operator_identity="operator:test",
                    authentication_identity="test:authenticated",
                    requested_at_ns=NOW_NS,
                    expires_at_ns=NOW_NS + 60 * SECOND_NS,
                    market_key=market_key,
                    direction="long" if market_key else None,
                )
            )
    finally:
        conn.close()


def _run(**kwargs: Any) -> PostgresRuntime:
    conn = connect_postgres_test(read_only=False)
    try:
        return run_runtime_on_postgres(repositories_for_connection(conn), **kwargs)
    finally:
        conn.close()


def _executions(tmp_path: Path) -> dict[str, Any]:
    settings = Settings(ws_token=TOKEN, storage=postgres_settings_storage())
    settings.set_config_dir(tmp_path / "app-home")
    with TestClient(create_app(settings=settings)) as client:
        response = client.get("/api/trading/executions", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _row(tmp_path: Path, entry_id: str = _SIGNAL_ID) -> dict[str, Any]:
    return next(item for item in _executions(tmp_path)["executions"] if item["entry_id"] == entry_id)


def _nautilus_realized(runtime: PostgresRuntime) -> Decimal:
    [position] = runtime.engine.cache.positions_closed()
    return position.realized_pnl.as_decimal()


def test_a_stopped_out_signal_is_one_closed_row_whose_pnl_is_folded_from_its_fills(tmp_path: Path) -> None:
    _seed_signal()
    runtime = _run(
        tape=[
            *quotes(9_999, 10_000, start_ns=NOW_NS, count=10),
            *quotes(9_700, 9_701, start_ns=NOW_NS + 2 * SECOND_NS, count=10),
        ]
    )

    data = _executions(tmp_path)
    [row] = [item for item in data["executions"] if item["entry_id"] == _SIGNAL_ID]
    assert (row["stage"], row["plan_status"], row["exit_reason"]) == ("closed", "closed", "stop_filled")
    assert (row["source"], row["case_id"], row["market_key"], row["direction"]) == ("signal", "case-1", MARKET, "long")
    assert row["disposition_reason"] == "accepted"
    assert row["fill_quantity"] == "0.049"
    assert Decimal(row["fill_avg_price"]) == Decimal(10_000)
    assert Decimal(row["exit_price"]) == Decimal(9_700)
    assert Decimal(row["stop_trigger_price"]) == Decimal(9_800)
    assert Decimal(row["take_profit_trigger_price"]) == Decimal(10_200)
    # Net of both commissions, exactly Nautilus' own realized PnL for the same position.
    realized = Decimal(row["realized_pnl_usd"])
    assert realized == _nautilus_realized(runtime)
    assert realized == (Decimal(9_700) - Decimal(10_000)) * Decimal("0.049") - Decimal(row["fees_usd"])
    assert row["pnl_known"] is True
    assert row["entry_filled_at_ns"] < row["position_closed_at_ns"]
    assert row["duration_ns"] == row["position_closed_at_ns"] - row["entry_filled_at_ns"]
    assert {"history_complete", "gap_reason", "order_status", "position_status"}.isdisjoint(row)

    totals = data["totals"]
    assert (totals["closed_total"], totals["pnl_known_total"], totals["pnl_missing_total"]) == (1, 1, 0)
    assert Decimal(totals["realized_known_total_usd"]) == realized
    # The Runtime's clock is years ahead of this test's wall clock, so the close is not "today".
    assert (totals["closed_today"], totals["realized_known_today_usd"]) == (0, None)


@pytest.mark.parametrize("exit_reason", ["take_profit", "time_exit"])
def test_a_normal_exit_is_one_native_close_with_complete_pnl(tmp_path: Path, exit_reason: str) -> None:
    _seed_signal()
    if exit_reason == "take_profit":
        tape = [
            *quotes(9_999, 10_000, start_ns=NOW_NS, count=10),
            *quotes(10_300, 10_301, start_ns=NOW_NS + 2 * SECOND_NS, count=10),
        ]
        profile = oi_profile()
    else:
        tape = quotes(9_999, 10_000, start_ns=NOW_NS, count=80)
        profile = replace(oi_profile(), exit_policy=replace(oi_profile().exit_policy, max_holding_ns=SECOND_NS))
    runtime = _run(tape=tape, profile=profile)

    row = _row(tmp_path)
    assert (row["stage"], row["exit_reason"]) == ("closed", exit_reason)
    assert Decimal(row["realized_pnl_usd"]) == _nautilus_realized(runtime)
    assert row["duration_ns"] > 0


def test_a_flatten_after_a_restart_closes_the_adopted_position_under_the_operators_reason(tmp_path: Path) -> None:
    """The entry fill was journaled by one generation and the exit fill by the next; the fold spans both."""

    _seed_signal()
    _run(tape=quotes(9_999, 10_000, start_ns=NOW_NS, count=10))
    _append_command(command_id=_FLATTEN_COMMAND_ID, action="flatten")
    _run(tape=quotes(10_049, 10_051, start_ns=NOW_NS + 5 * SECOND_NS, count=20), seed=seed_reconciled_position)

    row = _row(tmp_path)
    assert (row["stage"], row["exit_reason"]) == ("closed", "operator_flatten")
    assert row["pnl_known"] is True
    assert Decimal(row["exit_price"]) == Decimal(10_049)
    conn = connect_postgres_test(read_only=True)
    try:
        [command] = [
            item
            for item in TradingRepository(conn).console_operator_intents(since_ns=0, action=None, limit=10)
            if item["command_id"] == _FLATTEN_COMMAND_ID
        ]
    finally:
        conn.close()
    assert (command["disposition"], command["disposition_reason"]) == ("accepted", "flatten_submitted")


def test_a_manual_entry_is_its_own_row_with_the_same_protection_and_its_command_says_accepted(
    tmp_path: Path,
) -> None:
    _append_command(command_id=_MANUAL_ENTRY_COMMAND_ID, action="manual_entry", market_key=MARKET)
    _run(tape=quotes(9_999, 10_000, start_ns=NOW_NS, count=20))

    row = _row(tmp_path, _MANUAL_ENTRY_COMMAND_ID)
    assert (row["source"], row["case_id"], row["stage"]) == ("manual", None, "protected")
    assert row["disposition_reason"] == "accepted"
    assert Decimal(row["stop_trigger_price"]) == Decimal(9_800)
    assert row["realized_pnl_usd"] is None and row["pnl_known"] is False


def test_a_refused_entry_is_a_rejected_row_in_the_venues_words(tmp_path: Path) -> None:
    from nautilus_trader.model.enums import TradingState

    _seed_signal()

    def halt(engine: Any, _strategy: Any) -> None:
        engine.kernel.risk_engine.set_trading_state(TradingState.HALTED)

    _run(tape=quotes(9_999, 10_000, start_ns=NOW_NS, count=10), seed=halt)

    row = _row(tmp_path)
    assert (row["stage"], row["exit_reason"], row["disposition_reason"]) == (
        "rejected",
        "not_submitted",
        "venue_rejected",
    )
    assert "HALTED" in row["order_reject_reason"]
    assert row["fill_quantity"] is None and row["entry_filled_at_ns"] is None


def test_a_signal_whose_ttl_ran_out_without_a_disposition_reads_expired(tmp_path: Path) -> None:
    now_ns = time.time_ns()
    _seed_signal(
        signal_id="9" * 64,
        case_id="case-ttl",
        observed_at_ns=now_ns - 3_600 * SECOND_NS,
        expires_at_ns=now_ns - 60 * SECOND_NS,
    )
    _seed_signal(
        signal_id="8" * 64,
        case_id="case-live",
        observed_at_ns=now_ns - 3_600 * SECOND_NS,
        expires_at_ns=now_ns + 3_600 * SECOND_NS,
    )

    rows = {row["entry_id"]: row for row in _executions(tmp_path)["executions"]}
    assert (rows["9" * 64]["stage"], rows["9" * 64]["disposition_reason"]) == ("expired", None)
    assert rows["8" * 64]["stage"] == "pending"


def _fill(identity: str, *, leg: str, price: str, at_ns: int, commission: str | None) -> ExecutionObservationV1:
    summary: dict[str, str] = {"leg": leg, "last_quantity": "0.049", "last_price": price}
    if commission is not None:
        summary |= {"commission": commission, "commission_currency": "USDT"}
    return ExecutionObservationV1.model_validate(
        {
            "event_id": sha256(f"{identity}:{leg}".encode()).hexdigest(),
            "account_slot": _ACCOUNT_SLOT,
            "execution_strategy": "oi_nautilus_v1",
            "signal_id": identity,
            "normalized_kind": "fill",
            "occurred_at_ns": at_ns,
            "observed_at_ns": at_ns,
            "native_identity_references": (),
            "summary": summary,
        }
    )


def test_realized_totals_count_a_plan_whose_fills_cannot_yield_a_result_as_missing_never_as_zero(
    tmp_path: Path,
) -> None:
    """Three closed plans: whole fills with fees, a fill with no commission (history), no exit fill."""

    conn = connect_postgres_test(read_only=False)
    try:
        repo = TradingRepository(conn)
        observations = []
        for index, (entry_fee, exit_leg) in enumerate((("0.1", "stop"), (None, "take_profit"), ("0.1", None))):
            identity = str(index + 1) * 64
            start = time.time_ns() - (index + 1) * 60 * SECOND_NS
            _seed_signal(
                signal_id=identity, case_id=f"case-pnl-{index}", observed_at_ns=start, expires_at_ns=start + 10
            )
            plan = open_plan(entry_id=identity, opened_at_ns=start + 1, created_at_ns=start).closed(
                reason="stop_filled" if exit_leg == "stop" else "venue_unknown",
                terminal_at_ns=start + 20,
                now_ns=start + 20,
            )
            with conn.transaction():
                assert repo.insert_trade_plan(prepare_trade_plan(plan))
            observations.append(_fill(identity, leg="entry", price="10000", at_ns=start + 1, commission=entry_fee))
            if exit_leg is not None:
                observations.append(_fill(identity, leg=exit_leg, price="10100", at_ns=start + 19, commission="0.2"))
        with conn.transaction():
            repo.append_execution_observations(prepare_execution_observations(tuple(observations)))
    finally:
        conn.close()

    data = _executions(tmp_path)
    totals = data["totals"]
    assert totals["closed_today"] == totals["closed_total"] == 3
    assert totals["pnl_known_total"] == 1 and totals["pnl_missing_total"] == 2
    # (10100 - 10000) x 0.049 - 0.1 - 0.2
    assert Decimal(totals["realized_known_total_usd"]) == Decimal("4.6")
    rows = {row["entry_id"]: row for row in data["executions"]}
    assert Decimal(rows["1" * 64]["realized_pnl_usd"]) == Decimal("4.6")
    assert Decimal(rows["1" * 64]["fees_usd"]) == Decimal("0.3")
    assert rows["2" * 64]["realized_pnl_usd"] is None and rows["2" * 64]["fees_usd"] is None
    assert rows["3" * 64]["realized_pnl_usd"] is None
    assert {row["stage"] for row in rows.values()} == {"closed"}


def test_an_open_plan_older_than_the_console_window_remains_visible_without_any_audit(tmp_path: Path) -> None:
    old = time.time_ns() - 30 * 86_400 * SECOND_NS
    plan = open_plan(opened_at_ns=None, created_at_ns=old)
    conn = connect_postgres_test(read_only=False)
    try:
        with conn.transaction():
            TradingRepository(conn).insert_trade_plan(prepare_trade_plan(plan))
    finally:
        conn.close()

    [row] = _executions(tmp_path)["executions"]
    assert (row["entry_id"], row["stage"]) == (plan.entry_id, "pending")
    assert row["entry_client_order_id"] == plan.entry_client_order_id
    assert row["risk_budget_usd"] == str(plan.risk_budget_usd)
