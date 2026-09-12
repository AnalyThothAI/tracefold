"""The desk table over a real Signal's whole durable execution (#528 PR-1).

The observations are written by the pinned Nautilus Runtime in its own process, never hand-built
here: what makes this a read-model test rather than a fixture test is that the row it renders is
folded from the exact summaries the production writer produces.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.nautilus_oi_runtime_fixtures import NOW_NS
from tests.postgres_test_utils import connect_postgres_test, postgres_settings_storage
from tracefold.app.http.app import create_app
from tracefold.platform.config.models import Settings
from tracefold.trading.execution_contracts import ExecutionObservationV1
from tracefold.trading.storage.execution_stream import (
    prepare_execution_observations,
    prepare_operator_intent,
    prepare_trade_signal,
)
from tracefold.trading.storage.root import TradingRepository

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_clone_dsn")]

_ACCOUNT_SLOT = "binance_usdm_primary"
_SIGNAL_ID = "1" * 64
_FLATTEN_COMMAND_ID = "b" * 64
_MANUAL_ENTRY_COMMAND_ID = "c" * 64
_MANUAL_FLATTEN_COMMAND_ID = "d" * 64
_MARKET_KEY = "crypto:perp:BTC:USDT"
TOKEN = "executions-read-model-token"


def _seed_signal(
    *,
    signal_id: str = _SIGNAL_ID,
    case_id: str = "case-1",
    observed_at_ns: int = NOW_NS - 1_000_000,
    expires_at_ns: int = NOW_NS + 60_000_000_000,
) -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        repo = TradingRepository(conn)
        with conn.transaction():
            repo.ensure_execution_runtime_control_state(_ACCOUNT_SLOT, now_ns=NOW_NS)
        prepared = prepare_trade_signal(
            signal_id=signal_id,
            case_id=case_id,
            market_key="crypto:perp:BTC:USDT",
            direction="long",
            observed_at_ns=observed_at_ns,
            expires_at_ns=expires_at_ns,
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
                  %s, 'crypto:BTC', 'oi', %s,
                  '{"test":"executions"}'::jsonb, %s, 'SIGNAL_EMITTED', 'long',
                  'executions_read_model', 1, 1, 1, 1
                )
                """,
                (case_id, f"runtime-source:{case_id}", "4" * 64),
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
    data = response.json()["data"]
    assert "commands" not in data
    return data


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
    assert row["exit_reason"] == "operator_flatten"
    assert row["realized_pnl_usd"] is not None
    assert row["fill_quantity"] == "0.049"

    command = next(item for item in _operator_intents() if item["command_id"] == _FLATTEN_COMMAND_ID)
    # A flatten writes no `control_disposition` until the private report proves the slot went flat,
    # and this harness runs no reconciliation. `recorded` is the honest answer: the exposure this
    # Command closed is on the Signal row above, and reading it back onto the Command would be
    # exactly the venue correlation #528 C deleted.
    assert command["disposition"] == "completed"
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
    assert row["exit_reason"] == "operator_flatten"
    assert row["realized_pnl_usd"] is not None
    assert row["exit_price"] is not None
    assert row["fill_quantity"] is not None and float(row["fill_quantity"]) > 0
    assert row["fill_avg_price"] is not None
    assert row["stop_trigger_price"] is not None
    assert {"disposition", "order_status", "position_status", "last_observed_at_ns"}.isdisjoint(row)

    # Removing the console control ledger must not erase retained operator facts.
    commands = _operator_intents()
    command = next(item for item in commands if item["command_id"] == _MANUAL_ENTRY_COMMAND_ID)
    assert command["action"] == "manual_entry"
    assert command["disposition"] == "accepted"
    assert {item["command_id"] for item in commands} == {
        _MANUAL_ENTRY_COMMAND_ID,
        _MANUAL_FLATTEN_COMMAND_ID,
    }


def _operator_intents() -> list[dict[str, object]]:
    conn = connect_postgres_test(read_only=True)
    try:
        return TradingRepository(conn).console_operator_intents(since_ns=0, action=None, limit=100)
    finally:
        conn.close()


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


def test_a_stopped_out_signal_publishes_both_clocks_and_the_slots_realized_totals(
    postgres_clone_dsn: str,
    tmp_path: Path,
) -> None:
    """#604 T3. Two clocks a holding time is the distance between, and totals the window cannot add up.

    Before this change the desk had `observed_at_ns` -- when the Signal was *written* -- and nothing
    else, so "how long was this open" had no answer, and the only realized number on the page was the
    sum of whichever rows the 24 h window happened to be showing. Both come off the same durable
    observations the Runtime just wrote in its own process: the first entry `fill` and the `closed`
    position, and one aggregate over every `closed` position this slot has.
    """

    _seed_signal()
    receipt = _run_runtime(postgres_clone_dsn, "stop_filled")
    assert receipt["positions_count"] == 0, receipt

    data = _executions(tmp_path)
    row = next(item for item in data["executions"] if item["entry_id"] == _SIGNAL_ID)

    assert row["stage"] == "closed"
    assert row["entry_filled_at_ns"] is not None
    assert row["position_closed_at_ns"] is not None
    assert row["entry_filled_at_ns"] < row["position_closed_at_ns"]
    # The entry fill is the first venue fact about this entry, and the close is the last.
    assert row["observed_at_ns"] <= row["entry_filled_at_ns"]
    # Nothing refused this order, so there is no venue text to print.
    assert row["order_reject_reason"] is None

    verify = connect_postgres_test(read_only=False)
    try:
        closed = verify.execute(
            """
            SELECT occurred_at_ns, summary
              FROM trading_execution_observations
             WHERE normalized_kind = 'position' AND summary ->> 'status' = 'closed'
            """
        ).fetchone()
        first_fill = verify.execute(
            """
            SELECT min(occurred_at_ns) AS at_ns
              FROM trading_execution_observations
             WHERE normalized_kind = 'fill' AND summary ->> 'leg' = 'entry'
            """
        ).fetchone()
    finally:
        verify.close()

    # The published clocks are the observations' own, not a derivation beside them.
    assert row["position_closed_at_ns"] == int(closed["occurred_at_ns"])
    assert row["entry_filled_at_ns"] == int(first_fill["at_ns"])

    totals = data["totals"]
    assert totals["closed_total"] == 1
    assert Decimal(totals["realized_known_total_usd"]) == Decimal(str(closed["summary"]["realized_pnl_usd"]))
    assert Decimal(totals["realized_known_total_usd"]) == Decimal(row["realized_pnl_usd"])
    # The Runtime's own clock is years ahead of this test's wall clock, so the same close is not in
    # "today" and the day-scoped pair is the honest zero rather than a copy of the all-time pair.
    assert totals["closed_today"] == 0
    assert totals["realized_known_today_usd"] is None


def test_a_venue_refusal_reaches_the_desk_as_the_words_the_venue_used(
    postgres_clone_dsn: str,
    tmp_path: Path,
) -> None:
    """#604 T3 over #604 T1. The Runtime records `summary.reason` on a rejected entry order.

    The desk printed `ordered` and stopped: an operator could see that a Signal reached the venue and
    not that the venue refused it, or what for. The observation is written straight through the
    storage API here rather than provoked out of the backtest engine, because what is under test is
    the fold that publishes the column -- and it tolerates a row written before the Runtime recorded
    a reason at all, which is every row in the production ledger today. The summary is the exact
    shape the writer produces, which `test_nautilus_oi_runtime_strategy.py` pins against the real
    Nautilus event: `{"leg": "entry", "status": "rejected", "reason": <venue text>}`.
    """

    _seed_signal()
    conn = connect_postgres_test(read_only=False)
    try:
        repo = TradingRepository(conn)
        with conn.transaction():
            repo.append_execution_observations(
                prepare_execution_observations(
                    (
                        _observation(
                            event="a",
                            kind="signal_disposition",
                            summary={"disposition": "accepted"},
                        ),
                        _observation(
                            event="b",
                            kind="order",
                            summary={
                                "leg": "entry",
                                "status": "rejected",
                                "reason": "Margin is insufficient.",
                            },
                        ),
                    )
                )
            )
    finally:
        conn.close()

    row = next(item for item in _executions(tmp_path)["executions"] if item["entry_id"] == _SIGNAL_ID)

    assert row["stage"] == "ordered"
    assert row["disposition_reason"] == "accepted"
    assert row["order_reject_reason"] == "Margin is insufficient."
    assert row["entry_filled_at_ns"] is None
    assert row["position_closed_at_ns"] is None


def test_a_signal_whose_ttl_ran_out_without_a_disposition_reads_expired(
    postgres_clone_dsn: str,
    tmp_path: Path,
) -> None:
    """#604 T3 (audit A4). The hole the bridge leaves is a word, not a row that never resolves.

    `UNRESOLVED_TRADE_SIGNALS_SQL` anti-joins on `expires_at_ns > now`, so a Signal refused only for a
    retryable reason -- which writes no durable disposition -- stops being offered the instant it
    expires and never receives one. Read back through `/api/trading/executions` that was `pending`
    for the rest of the 24 h window: a row an operator cannot explain and the desk claims is still in
    flight. Nothing new is written to close it; the Signal's own published TTL is the answer.
    """

    now_ns = time.time_ns()
    _seed_signal(
        signal_id="9" * 64,
        case_id="case-ttl",
        observed_at_ns=now_ns - 3_600_000_000_000,
        expires_at_ns=now_ns - 60_000_000_000,
    )
    _seed_signal(
        signal_id="8" * 64,
        case_id="case-live",
        observed_at_ns=now_ns - 3_600_000_000_000,
        expires_at_ns=now_ns + 3_600_000_000_000,
    )

    rows = {row["entry_id"]: row for row in _executions(tmp_path)["executions"]}

    assert rows["9" * 64]["stage"] == "expired"
    assert rows["9" * 64]["disposition_reason"] is None
    # A Signal still inside its own TTL is still pending: the clock is the only thing that changed.
    assert rows["8" * 64]["stage"] == "pending"


def _observation(*, event: str, kind: str, summary: dict[str, object]) -> ExecutionObservationV1:
    return ExecutionObservationV1.model_validate(
        {
            "event_id": event * 64,
            "account_slot": _ACCOUNT_SLOT,
            "execution_strategy": "oi_nautilus_v1",
            "signal_id": _SIGNAL_ID,
            "normalized_kind": kind,
            "occurred_at_ns": NOW_NS,
            "observed_at_ns": NOW_NS + 1,
            "native_identity_references": (),
            "summary": summary,
        }
    )


@pytest.mark.parametrize("exit_reason", ["take_profit", "time_exit"])
def test_normal_exit_uses_one_native_reduce_only_order_and_keeps_complete_pnl(
    postgres_clone_dsn: str,
    tmp_path: Path,
    exit_reason: str,
) -> None:
    _seed_signal()
    receipt = _run_runtime(postgres_clone_dsn, exit_reason)
    assert receipt["positions_count"] == 0
    entries = [order for order in receipt["orders"] if not order["reduce_only"]]
    exits = [order for order in receipt["orders"] if order["reduce_only"] and order["order_type"] == "MARKET"]
    assert len(entries) == len(exits) == 1
    row = next(item for item in _executions(tmp_path)["executions"] if item["entry_id"] == _SIGNAL_ID)
    assert row["stage"] == "closed"
    assert row["exit_reason"] == exit_reason
    assert row["plan_status"] == "closed"
    assert row["pnl_known"] is True
    assert row["history_complete"] is True
    assert row["gap_reason"] is None
    assert row["duration_ns"] > 0


@pytest.mark.parametrize("crash", ["crash_after_prepare", "crash_after_entry"])
def test_process_crash_keeps_plan_ownership_without_an_entry_observation(
    postgres_clone_dsn: str,
    crash: str,
) -> None:
    _seed_signal()
    stopped = subprocess.run(
        [sys.executable, "-m", "tests.helpers.nautilus_oi_runtime_process", postgres_clone_dsn, crash],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert stopped.returncode == 74, stopped.stderr
    conn = connect_postgres_test(read_only=False)
    try:
        plan = conn.execute(
            "SELECT entry_id, entry_client_order_id, status, stop_distance_bps FROM trading_trade_plans"
        ).fetchone()
        assert plan["entry_id"] == _SIGNAL_ID
        assert plan["status"] == "prepared"
        assert conn.execute("SELECT count(*) AS n FROM trading_execution_observations").fetchone()["n"] == 0
    finally:
        conn.close()
    if crash == "crash_after_entry":
        recovered = _run_runtime(postgres_clone_dsn, "cold_config_change")
        assert recovered["recovered"] is True
        assert recovered["positions_count"] == 1
        assert recovered["protection_status"] == "protected"
        assert recovered["execution_safe"] is True
        assert all(
            order["reduce_only"] or order["client_order_id"] == "EXTERNAL-COLD-ENTRY" for order in recovered["orders"]
        )
        assert not any(order["client_order_id"] == plan["entry_client_order_id"] for order in recovered["orders"])
        verify = connect_postgres_test(read_only=True)
        try:
            restored = verify.execute(
                "SELECT entry_id, entry_client_order_id, stop_distance_bps, history_gap_reason FROM trading_trade_plans"
            ).fetchone()
            assert restored["entry_client_order_id"] == plan["entry_client_order_id"]
            assert restored["stop_distance_bps"] == 200
            assert restored["history_gap_reason"] == "native_pnl_basis_incomplete_after_restart"
        finally:
            verify.close()


@pytest.mark.parametrize(
    "pnls,known_sum,known_count",
    [
        (("3", "-1", "2"), "4", 3),
        (("3", None, "2"), "5", 2),
        ((None, None, None), None, 0),
    ],
)
@pytest.mark.parametrize("audit_gap", [False, True])
def test_realized_totals_name_known_pnl_and_never_turn_missing_history_into_zero(
    tmp_path: Path,
    pnls: tuple[str | None, ...],
    known_sum: str | None,
    known_count: int,
    audit_gap: bool,
) -> None:
    from hashlib import sha256

    from tests.nautilus_oi_runtime_fixtures import trade_plan_for_entry, trade_signal
    from tracefold.integrations.nautilus.oi_runtime.state import RuntimeEntryRequest
    from tracefold.trading.storage.trade_plans import prepare_trade_plan

    conn = connect_postgres_test(read_only=False)
    try:
        repo = TradingRepository(conn)
        values = []
        for index, pnl in enumerate(pnls):
            identity = str(index + 1) * 64
            start = time.time_ns() - (index + 1) * 60_000_000_000
            _seed_signal(
                signal_id=identity, case_id=f"case-pnl-{index}", observed_at_ns=start, expires_at_ns=start + 10
            )
            plan = trade_plan_for_entry(RuntimeEntryRequest.from_signal(trade_signal(signal_id=identity)))
            plan = plan.model_copy(
                update={
                    "account_slot": _ACCOUNT_SLOT,
                    "created_at_ns": start,
                    "entry_expires_at_ns": start + 10,
                    "opened_at_ns": start + 1,
                    "terminal_at_ns": start + 20,
                    "updated_at_ns": start + 20,
                    "status": "closed",
                    "exit_reason": "time_exit",
                }
            )
            with conn.transaction():
                assert repo.insert_trade_plan(prepare_trade_plan(plan))
            # Missing PnL is an absent close observation, while the plan preserves the closed identity.
            if pnl is None:
                continue
            for kind, offset, summary in (
                ("fill", 1, {"leg": "entry", "last_quantity": "0.049", "last_price": "10000"}),
                ("fill", 19, {"leg": "exit", "last_quantity": "0.049", "last_price": "10100"}),
                ("position", 20, {"status": "closed", "quantity": "0", "realized_pnl_usd": pnl}),
            ):
                values.append(
                    _observation(event="a", kind=kind, summary=summary).model_copy(
                        update={
                            "signal_id": identity,
                            "event_id": sha256(f"{identity}:{offset}".encode()).hexdigest(),
                            "occurred_at_ns": start + offset,
                            "observed_at_ns": start + offset,
                        }
                    )
                )
        if audit_gap:
            values.append(
                _observation(event="f", kind="audit_gap", summary={"count": 1}).model_copy(
                    update={
                        "signal_id": None,
                        "occurred_at_ns": start,
                        "observed_at_ns": time.time_ns(),
                    }
                )
            )
        with conn.transaction():
            repo.append_execution_observations(prepare_execution_observations(tuple(values)))
    finally:
        conn.close()
    data = _executions(tmp_path)
    totals = data["totals"]
    assert totals["closed_today"] == totals["closed_total"] == 3
    assert totals["pnl_known_today"] == totals["pnl_known_total"] == known_count
    assert totals["pnl_missing_today"] == totals["pnl_missing_total"] == 3 - known_count
    assert totals["realized_known_today_usd"] == totals["realized_known_total_usd"] == known_sum
    assert totals["pnl_complete_today"] == totals["pnl_complete_total"] == (known_count == 3 and not audit_gap)
    assert len(data["executions"]) == 3
    for row in data["executions"]:
        assert row["stage"] == "closed"
        assert row["pnl_known"] == (row["realized_pnl_usd"] is not None)
        assert row["history_complete"] == (row["pnl_known"] and not audit_gap)


def test_active_plan_older_than_the_console_window_remains_visible_without_any_audit(tmp_path: Path) -> None:
    from tests.nautilus_oi_runtime_fixtures import trade_plan_for_entry, trade_signal
    from tracefold.integrations.nautilus.oi_runtime.state import RuntimeEntryRequest
    from tracefold.trading.storage.trade_plans import prepare_trade_plan

    old = time.time_ns() - 30 * 86_400_000_000_000
    plan = trade_plan_for_entry(RuntimeEntryRequest.from_signal(trade_signal()))
    plan = plan.model_copy(update={"created_at_ns": old, "entry_expires_at_ns": old + 10, "updated_at_ns": old})
    conn = connect_postgres_test(read_only=False)
    try:
        with conn.transaction():
            TradingRepository(conn).insert_trade_plan(prepare_trade_plan(plan))
    finally:
        conn.close()
    rows = _executions(tmp_path)["executions"]
    assert len(rows) == 1
    assert rows[0]["entry_id"] == plan.entry_id
    assert rows[0]["stage"] == "pending"
    assert rows[0]["entry_client_order_id"] == plan.entry_client_order_id
    assert rows[0]["risk_budget_usd"] == str(plan.risk_budget_usd)
