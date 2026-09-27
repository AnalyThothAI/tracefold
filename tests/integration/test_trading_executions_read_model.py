"""The desk table over a real entry's whole durable execution (#528 PR-1, #680 PR-1).

The observations and plans are written by the production Strategy on a real Nautilus engine through
the production bridge cycle (`tests/helpers/nautilus_oi_runtime_process.py`), never hand-built, where
the scenario can be run: what makes this a read-model test rather than a fixture test is that the row
it renders is folded from the exact summaries the production writer produces. The offline engine
provides no signed venue trades, so its ordinary fill journal is process audit only and the production
read model must keep economic fields unknown.
"""

from __future__ import annotations

import time
from contextlib import closing
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.helpers.nautilus_oi_runtime_process import PostgresRuntime, run_runtime_on_postgres
from tests.helpers.published_signal_v3 import append_published_v3_signal, execution_fixture_profile
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
from tracefold.app.http.routes import trading as trading_routes
from tracefold.app.nautilus.oi_runtime import write_terminal_plan
from tracefold.app.repository_session import repositories_for_connection
from tracefold.integrations.nautilus.oi_runtime.entry import deterministic_client_order_id
from tracefold.integrations.nautilus.oi_runtime.journal import ObservationFactory
from tracefold.platform.config.models import Settings
from tracefold.trading.execution_contracts import ExecutionObservationV1
from tracefold.trading.native_fills import NativeFill
from tracefold.trading.storage.execution_stream import (
    prepare_execution_observations,
    prepare_operator_intent,
)
from tracefold.trading.storage.root import TradingRepository
from tracefold.trading.storage.trade_plans import prepare_trade_plan
from tracefold.trading.trade_plan import PlanOrderBinding, TradePlan

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
    max_holding_ns: int = 4 * 3_600 * SECOND_NS,
) -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        repo = TradingRepository(conn)
        with conn.transaction():
            repo.ensure_execution_runtime_control_state(_ACCOUNT_SLOT, now_ns=NOW_NS)
        append_published_v3_signal(
            repo,
            signal_id=signal_id,
            case_id=case_id,
            observed_at_ns=observed_at_ns,
            expires_at_ns=expires_at_ns,
            max_holding_ns=max_holding_ns,
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


def test_an_offline_stop_keeps_the_plan_open_and_economics_unknown(tmp_path: Path) -> None:
    _seed_signal()
    runtime = _run(
        tape=[
            *quotes(9_999, 10_000, start_ns=NOW_NS, count=10),
            *quotes(9_700, 9_701, start_ns=NOW_NS + 2 * SECOND_NS, count=10),
        ]
    )

    data = _executions(tmp_path)
    [row] = [item for item in data["executions"] if item["entry_id"] == _SIGNAL_ID]
    assert runtime.engine.cache.positions_closed()
    assert (row["stage"], row["plan_status"], row["exit_reason"]) == ("protected", "open", None)
    assert (row["source"], row["case_id"], row["market_key"], row["direction"]) == ("signal", "case-1", MARKET, "long")
    assert row["disposition_reason"] == "accepted"
    assert row["fill_quantity"] is None
    assert row["fill_avg_price"] is None
    assert row["exit_price"] is None
    assert Decimal(row["stop_trigger_price"]) == Decimal(9_800)
    assert Decimal(row["take_profit_trigger_price"]) == Decimal(10_200)
    assert row["realized_pnl_usd"] is None and row["fees_usd"] is None
    assert row["pnl_known"] is False
    assert {"history_complete", "gap_reason", "order_status", "position_status"}.isdisjoint(row)

    totals = data["totals"]
    assert (totals["closed_total"], totals["pnl_known_total"], totals["pnl_missing_total"]) == (0, 0, 0)


@pytest.mark.parametrize("exit_reason", ["take_profit", "time_exit"])
def test_offline_exits_do_not_supply_signed_native_economics(tmp_path: Path, exit_reason: str) -> None:
    _seed_signal(max_holding_ns=SECOND_NS if exit_reason == "time_exit" else 4 * 3_600 * SECOND_NS)
    if exit_reason == "take_profit":
        tape = [
            *quotes(9_999, 10_000, start_ns=NOW_NS, count=10),
            *quotes(10_300, 10_301, start_ns=NOW_NS + 2 * SECOND_NS, count=10),
        ]
        profile = execution_fixture_profile()
    else:
        tape = quotes(9_999, 10_000, start_ns=NOW_NS, count=80)
        profile = execution_fixture_profile()
    runtime = _run(tape=tape, profile=profile)

    row = _row(tmp_path)
    assert runtime.engine.cache.positions_closed()
    assert (row["stage"], row["plan_status"], row["exit_reason"]) == ("protected", "open", None)
    assert row["realized_pnl_usd"] is None and row["pnl_known"] is False


def test_flatten_after_restart_records_the_command_but_waits_for_native_terminal_evidence(tmp_path: Path) -> None:

    _seed_signal()
    _run(tape=quotes(9_999, 10_000, start_ns=NOW_NS, count=10))
    _append_command(command_id=_FLATTEN_COMMAND_ID, action="flatten")
    _run(tape=quotes(10_049, 10_051, start_ns=NOW_NS + 5 * SECOND_NS, count=20), seed=seed_reconciled_position)

    row = _row(tmp_path)
    assert (row["stage"], row["plan_status"], row["exit_reason"]) == ("protected", "open", None)
    assert row["pnl_known"] is False and row["exit_price"] is None
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


def _native_trade(
    plan: TradePlan,
    *,
    leg: str,
    order_id: str,
    trade_id: str,
    price: str,
    at_ns: int,
    commission: str | None,
) -> tuple[ExecutionObservationV1, ...]:
    """A complete synthetic signed-order fixture for the read model's native-only fold."""
    binding = PlanOrderBinding(
        account_slot=plan.account_slot,
        entry_id=plan.entry_id,
        source=plan.source,
        instrument_id=plan.instrument_id,
        client_order_id=(
            plan.entry_client_order_id
            if leg == "entry"
            else deterministic_client_order_id(namespace=oi_profile().namespace, entry_id=plan.entry_id, leg=leg).value
        ),
        leg=leg,
        exit_reason="stop_filled" if leg == "stop" else "take_profit" if leg == "take_profit" else None,
    )
    fill = NativeFill(
        account_slot=plan.account_slot,
        environment="DEMO",
        instrument="BTCUSDT",
        trade_id=trade_id,
        order_id=order_id,
        side="BUY" if leg == "entry" else "SELL",
        quantity=Decimal("0.049"),
        price=Decimal(price),
        occurred_at_ns=at_ns,
    )
    rows = fill.observation(
        execution_strategy="oi_nautilus_v1",
        observed_at_ns=at_ns,
        commission=None if commission is None else Decimal(commission),
        commission_currency=None if commission is None else "USDT",
        binding=binding,
    )
    proof = ObservationFactory(plan.account_slot, "oi_nautilus_v1").create(
        normalized_kind="native_order_result",
        occurred_at_ns=at_ns,
        observed_at_ns=at_ns,
        native_identity_references=(order_id, trade_id),
        summary={
            "venue_environment": "DEMO",
            "native_instrument": "BTCUSDT",
            "venue_order_id": order_id,
            "source": "signed_order_trades_v1",
            "status": "FILLED",
            "trade_count": 1,
            "trade_digest": sha256(trade_id.encode()).hexdigest(),
            "executed_quantity": "0.049",
        },
        event_identity=f"fixture:{order_id}",
    )
    return (*rows, proof)


def _venue_funding(kind: str, *, at_ns: int, start_ns: int, end_ns: int) -> ExecutionObservationV1:
    summary: dict[str, str | int] = (
        {
            "venue": "binance.usdm",
            "source": "signed_income_v1",
            "venue_transaction_id": "123456789",
            "symbol": "BTCUSDT",
            "asset": "USDT",
            "amount_decimal": "0.11",
        }
        if kind == "funding"
        else {
            "venue": "binance.usdm",
            "source": "signed_income_v1",
            "start_at_ns": start_ns,
            "end_at_ns": end_ns,
            "status": "complete",
        }
    )
    return ExecutionObservationV1.model_validate(
        {
            "event_id": sha256(f"{kind}:{at_ns}:{start_ns}:{end_ns}".encode()).hexdigest(),
            "account_slot": _ACCOUNT_SLOT,
            "execution_strategy": "oi_nautilus_v1",
            "normalized_kind": kind,
            "occurred_at_ns": at_ns,
            "observed_at_ns": at_ns,
            "native_identity_references": ("123456789",) if kind == "funding" else (),
            "summary": summary,
        }
    )


def test_net_requires_complete_signed_funding_coverage(tmp_path: Path) -> None:
    _seed_signal()
    runtime = _run(
        tape=[
            *quotes(9_999, 10_000, start_ns=NOW_NS, count=10),
            *quotes(10_300, 10_301, start_ns=NOW_NS + 2 * SECOND_NS, count=10),
        ]
    )
    [closed] = [
        row.value
        for row in runtime.journal.due(float("inf"))
        if isinstance(row.value, TradePlan) and row.value.status == "closed"
    ]
    for observation in (
        *_native_trade(
            closed,
            leg="entry",
            order_id="301",
            trade_id="311",
            price="10000",
            at_ns=closed.opened_at_ns,
            commission="0.1",
        ),
        *_native_trade(
            closed,
            leg="take_profit",
            order_id="302",
            trade_id="312",
            price="10300",
            at_ns=closed.terminal_at_ns,
            commission="0.2",
        ),
    ):
        assert runtime.journal.offer(observation)
    with closing(connect_postgres_test(read_only=False)) as conn:
        write_terminal_plan(repositories_for_connection(conn), closed, runtime.journal.terminal_dependencies(closed))
    initial = _row(tmp_path)
    assert initial["realized_pnl_usd"] is not None
    assert initial["net_pnl_usd"] is None
    assert initial["net_known"] is False
    start = int(initial["entry_filled_at_ns"])
    end = int(initial["position_closed_at_ns"])
    middle = (start + end) // 2
    conn = connect_postgres_test(read_only=False)
    try:
        repo = TradingRepository(conn)
        with conn.transaction():
            repo.append_execution_observations(
                prepare_execution_observations(
                    (
                        _venue_funding("funding", at_ns=middle, start_ns=start, end_ns=end),
                        _venue_funding("funding_coverage", at_ns=middle, start_ns=start, end_ns=middle),
                    )
                )
            )
    finally:
        conn.close()
    assert _row(tmp_path)["net_pnl_usd"] is None

    conn = connect_postgres_test(read_only=False)
    try:
        with conn.transaction():
            TradingRepository(conn).append_execution_observations(
                prepare_execution_observations(
                    (_venue_funding("funding_coverage", at_ns=end + 1, start_ns=middle, end_ns=end + 1),)
                )
            )
    finally:
        conn.close()
    final = _row(tmp_path)
    assert Decimal(final["funding_usd"]) == Decimal("0.11")
    assert Decimal(final["net_pnl_usd"]) == Decimal(final["realized_pnl_usd"]) + Decimal("0.11")
    assert final["net_known"] is True
    totals = _executions(tmp_path)["totals"]
    assert totals["net_known_total"] == 1
    assert Decimal(totals["net_known_total_usd"]) == Decimal(final["net_pnl_usd"])

    conn = connect_postgres_test(read_only=False)
    try:
        with conn.transaction():
            TradingRepository(conn).insert_trade_plan(
                prepare_trade_plan(open_plan(entry_id="7" * 64, opened_at_ns=start, created_at_ns=start - 1))
            )
    finally:
        conn.close()
    ambiguous = _row(tmp_path)
    assert ambiguous["funding_usd"] is None
    assert ambiguous["net_pnl_usd"] is None


def test_realized_totals_count_a_plan_whose_fills_cannot_yield_a_result_as_missing_never_as_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three closed rows: verified native costs, missing native cost, and audit-only fills."""

    # Freeze the read at UTC noon: a CI run crossing midnight must still test
    # three same-day closes rather than the wall clock's previous day.
    day_ns = 86_400 * SECOND_NS
    now_ns = (time.time_ns() // day_ns) * day_ns + 12 * 3_600 * SECOND_NS
    monkeypatch.setattr(trading_routes, "time", SimpleNamespace(time=lambda: now_ns / SECOND_NS))
    conn = connect_postgres_test(read_only=False)
    try:
        repo = TradingRepository(conn)
        observations = []
        for index, (entry_fee, exit_leg) in enumerate((("0.1", "stop"), (None, "take_profit"), ("0.1", None))):
            identity = str(index + 1) * 64
            start = now_ns - (index + 1) * 60 * SECOND_NS
            _seed_signal(
                signal_id=identity, case_id=f"case-pnl-{index}", observed_at_ns=start, expires_at_ns=start + 10
            )
            base_plan = open_plan(entry_id=identity, opened_at_ns=start + 1, created_at_ns=start)
            plan = base_plan.closed(
                reason="stop_filled" if exit_leg == "stop" else "venue_unknown",
                terminal_at_ns=start + 20,
                now_ns=start + 20,
            )
            with conn.transaction():
                assert repo.insert_trade_plan(prepare_trade_plan(plan))
            observations.append(_fill(identity, leg="entry", price="10000", at_ns=start + 1, commission=entry_fee))
            if exit_leg is not None:
                observations.append(_fill(identity, leg=exit_leg, price="10100", at_ns=start + 19, commission="0.2"))
            if index < 2:
                observations.extend(
                    _native_trade(
                        base_plan,
                        leg="entry",
                        order_id=f"{index + 1}01",
                        trade_id=f"{index + 1}11",
                        price="10000",
                        at_ns=start + 1,
                        commission=entry_fee,
                    )
                )
                observations.extend(
                    _native_trade(
                        base_plan,
                        leg=exit_leg,
                        order_id=f"{index + 1}02",
                        trade_id=f"{index + 1}12",
                        price="10100",
                        at_ns=start + 19,
                        commission="0.2",
                    )
                )
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
