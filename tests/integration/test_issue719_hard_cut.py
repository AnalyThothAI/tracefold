"""The reviewed one-time cut cannot replay an old Signal or touch a second account."""

from __future__ import annotations

import json
import subprocess
import time
from contextlib import closing
from pathlib import Path
from uuid import UUID

import pytest

from tests.helpers.published_signal_v3 import append_published_v3_signal
from tests.postgres_test_utils import connect_postgres_test
from tracefold.trading.execution_contracts import ExecutionObservationV1
from tracefold.trading.storage.execution_stream import (
    ExecutionRuntimeState,
    prepare_execution_observations,
    prepare_operator_intent,
)
from tracefold.trading.storage.root import TradingRepository
from tracefold.trading.storage.trade_plans import prepare_trade_plan
from tracefold.trading.trade_plan import TradePlan

pytestmark = pytest.mark.integration
ROOT = Path(__file__).parents[2]


def test_stopped_account_cut_retires_inputs_and_preserves_other_accounts(postgres_clone_dsn: str) -> None:
    fixture = json.loads((ROOT / "tests/fixtures/binance/inj_20260925_execution.json").read_text())
    plan = TradePlan.model_validate_json(json.dumps(fixture["original_plan"]))
    old_fill = ExecutionObservationV1.model_validate(fixture["original_entry_observation"])
    old_command = prepare_operator_intent(
        command_id="d" * 64,
        account_slot=plan.account_slot,
        action="manual_entry",
        scope="market",
        reason="expired command replay fixture",
        operator_identity="operator:test",
        authentication_identity="test:local",
        requested_at_ns=plan.created_at_ns,
        expires_at_ns=plan.created_at_ns + 1_000_000_000,
        market_key=plan.market_key,
        direction=plan.direction,
    )
    assert old_command.value.expires_at_ns < time.time_ns()
    with closing(connect_postgres_test(read_only=False)) as conn:
        repo = TradingRepository(conn)
        with conn.transaction():
            repo.ensure_execution_runtime_control_state(plan.account_slot, now_ns=plan.created_at_ns)
            repo.ensure_execution_runtime_control_state("another_account", now_ns=plan.created_at_ns)
            repo.put_execution_runtime_state(
                ExecutionRuntimeState(
                    account_slot=plan.account_slot,
                    connection="DEMO",
                    runtime_id=UUID("11111111-1111-4111-8111-111111111111"),
                    alive=True,
                    entries_armed=False,
                    unexpected_exposure=False,
                    positions_count=0,
                    open_orders_count=0,
                    protection_status="not_applicable",
                    heartbeat_at_ns=1_000_000_000,
                    entry_block_reason="runtime_stopped",
                    started_at_ns=1_000_000_000,
                    updated_at_ns=1_000_000_000,
                )
            )
        append_published_v3_signal(
            repo,
            signal_id=plan.entry_id,
            case_id=plan.case_id,
            observed_at_ns=plan.created_at_ns - 1,
            expires_at_ns=plan.entry_expires_at_ns,
        )
        with conn.transaction():
            repo.insert_trade_plan(prepare_trade_plan(plan))
            repo.append_execution_observations(prepare_execution_observations((old_fill,)))
            repo.append_operator_intent(old_command)
        assert repo.trade_plan(plan.entry_id) is not None

    result = subprocess.run(
        [
            "psql",
            "-X",
            "-v",
            "ON_ERROR_STOP=1",
            "-v",
            f"account_slot={plan.account_slot}",
            "-v",
            "expected_connection=DEMO",
            "-d",
            postgres_clone_dsn,
            "-f",
            str(ROOT / "scripts/issue719_execution_hard_cut.sql"),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    with closing(connect_postgres_test(read_only=True)) as conn:
        repo = TradingRepository(conn)
        assert repo.trade_plan(plan.entry_id) is None
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM trading_execution_observations WHERE account_slot=%s", (plan.account_slot,)
            ).fetchone()["n"]
            == 0
        )
        assert (
            conn.execute(
                "SELECT reason FROM trading_signal_retirements WHERE signal_id=%s", (plan.entry_id,)
            ).fetchone()["reason"]
            == "execution_hard_cut"
        )
        assert (
            repo.unresolved_trade_signals(
                account_slot=plan.account_slot,
                execution_strategy="oi_nautilus_v1",
                now_ns=plan.created_at_ns,
                limit=10,
            )
            == ()
        )
        assert repo.execution_runtime_control_state(plan.account_slot) is not None
        assert repo.execution_runtime_control_state("another_account") is not None
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM trading_operator_intents WHERE account_slot=%s", (plan.account_slot,)
            ).fetchone()["n"]
            == 0
        )
    with closing(connect_postgres_test(read_only=False)) as conn, conn.transaction():
        repo = TradingRepository(conn)
        repo.append_operator_intent(old_command)  # a late retry keeps the old expiry
        assert (
            repo.unresolved_operator_intents(
                account_slot=plan.account_slot,
                execution_strategy="oi_nautilus_v1",
                now_ns=time.time_ns(),
                limit=10,
            )
            == ()
        )


def test_cut_refuses_to_delete_a_still_executable_manual_intent(postgres_clone_dsn: str) -> None:
    account_slot = "binance_usdm_primary"
    command = prepare_operator_intent(
        command_id="e" * 64,
        account_slot=account_slot,
        action="pause_entries",
        scope="entries",
        reason="still executable command fixture",
        operator_identity="operator:test",
        authentication_identity="test:local",
        requested_at_ns=time.time_ns(),
        expires_at_ns=time.time_ns() + 60_000_000_000,
        market_key=None,
        direction=None,
    )
    with closing(connect_postgres_test(read_only=False)) as conn, conn.transaction():
        repo = TradingRepository(conn)
        repo.ensure_execution_runtime_control_state(account_slot, now_ns=1_000_000_000)
        repo.put_execution_runtime_state(
            ExecutionRuntimeState(
                account_slot=account_slot,
                connection="DEMO",
                runtime_id=UUID("11111111-1111-4111-8111-111111111111"),
                alive=False,
                entries_armed=False,
                unexpected_exposure=False,
                positions_count=0,
                open_orders_count=0,
                protection_status="not_applicable",
                heartbeat_at_ns=1_000_000_000,
                entry_block_reason="runtime_stopped",
                started_at_ns=1_000_000_000,
                updated_at_ns=1_000_000_000,
            )
        )
        repo.append_operator_intent(command)

    result = subprocess.run(
        [
            "psql",
            "-X",
            "-v",
            "ON_ERROR_STOP=1",
            "-v",
            f"account_slot={account_slot}",
            "-v",
            "expected_connection=DEMO",
            "-d",
            postgres_clone_dsn,
            "-f",
            str(ROOT / "scripts/issue719_execution_hard_cut.sql"),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode != 0
    assert "issue719_operator_intent_still_executable" in result.stderr
    with closing(connect_postgres_test(read_only=True)) as conn:
        assert conn.execute("SELECT count(*) AS n FROM trading_operator_intents").fetchone()["n"] == 1
