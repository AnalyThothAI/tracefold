"""Real PostgreSQL commit failures and native entry hand-off (#644)."""

from __future__ import annotations

from contextlib import closing
from uuid import uuid4

import pytest
from psycopg.errors import CheckViolation

from tests.nautilus_oi_runtime_fixtures import NOW_NS, registered_oi_strategy, trade_signal
from tests.postgres_test_utils import connect_postgres_test, postgres_settings_storage
from tracefold.app.nautilus.oi_runtime import OiRuntimeDatabaseBridge, RuntimeStateProjector, load_recovery_inputs
from tracefold.app.repository_session import repositories_for_connection
from tracefold.integrations.nautilus.oi_runtime.singleton import AccountSlotSingleton
from tracefold.platform.config.models import Settings
from tracefold.trading.storage.execution_stream import ExecutionRuntimeState
from tracefold.trading.storage.trade_plans import prepare_trade_plan

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_clone_dsn")]


def _bridge(harness):
    state = ExecutionRuntimeState(
        account_slot=harness.profile.account_slot,
        mode="paper",
        runtime_id=uuid4(),
        alive=True,
        execution_safe=True,
        entries_armed=True,
        startup_reconciled=True,
        unexpected_exposure=False,
        account_flat=True,
        positions_count=0,
        open_orders_count=0,
        protection_status="not_applicable",
        reconciliation_observed_at_ns=NOW_NS,
        heartbeat_at_ns=NOW_NS,
        entry_block_reason=None,
        started_at_ns=NOW_NS,
        updated_at_ns=NOW_NS,
    )
    singleton = AccountSlotSingleton(
        account_slot=harness.profile.account_slot,
        try_acquire=lambda _slot: True,
        release=lambda _slot: True,
        heartbeat=lambda: True,
    )
    assert singleton.acquire()
    return OiRuntimeDatabaseBridge(
        settings=Settings(storage=postgres_settings_storage()),
        profile=harness.profile,
        signals=harness.signals,
        plans=harness.plans,
        audit=harness.audit,
        update_day_start=lambda _baseline: None,
        singleton=singleton,
        projector=RuntimeStateProjector(initial=state, recovery_inputs=()),
    )


def test_committed_plan_is_visible_on_another_connection_before_native_submission() -> None:
    harness = registered_oi_strategy(values=(trade_signal(),))
    harness.strategy.on_timer(None)
    assert harness.strategy.submitted == []
    with closing(connect_postgres_test(read_only=False)) as conn:
        bridge = _bridge(harness)
        bridge._flush_trade_plans(repositories_for_connection(conn))
        with closing(connect_postgres_test(read_only=True)) as observer:
            row = observer.execute("SELECT entry_id, status FROM trading_trade_plans").fetchone()
            assert row == {"entry_id": trade_signal().signal_id, "status": "prepared"}
        assert harness.strategy.submitted == []
        harness.strategy.on_timer(None)
        assert len(harness.strategy.submitted) == 1
        assert (
            harness.strategy.submitted[0][0].client_order_id.value
            == harness.plans.pending_updates()[0].entry_client_order_id
        )


@pytest.mark.parametrize("deferred", [False, True], ids=["insert-failure", "commit-failure"])
def test_real_insert_or_commit_failure_cannot_authorize_an_entry(deferred: bool) -> None:
    harness = registered_oi_strategy(values=(trade_signal(),))
    harness.strategy.on_timer(None)
    with closing(connect_postgres_test(read_only=False)) as conn:
        with conn.transaction():
            conn.execute("""CREATE FUNCTION test_refuse_trade_plan() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN RAISE check_violation USING MESSAGE = 'test_trade_plan_refused'; END $$""")
            if deferred:
                conn.execute("""CREATE CONSTRAINT TRIGGER test_refuse_plan AFTER INSERT ON trading_trade_plans
                    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION test_refuse_trade_plan()""")
            else:
                conn.execute("""CREATE TRIGGER test_refuse_plan BEFORE INSERT ON trading_trade_plans
                    FOR EACH ROW EXECUTE FUNCTION test_refuse_trade_plan()""")
        with pytest.raises(CheckViolation, match="test_trade_plan_refused"):
            _bridge(harness)._flush_trade_plans(repositories_for_connection(conn))
        harness.strategy.on_timer(None)
        assert harness.strategy.submitted == []
        assert conn.execute("SELECT count(*) AS n FROM trading_trade_plans").fetchone()["n"] == 0


@pytest.mark.parametrize("age_days", [8, 30])
def test_active_recovery_has_no_age_or_observation_dependency(age_days: int) -> None:
    harness = registered_oi_strategy(values=(trade_signal(),))
    harness.strategy.on_timer(None)
    plan = harness.plans.pending_prepare()
    assert plan is not None
    age = age_days * 86_400_000_000_000
    old = plan.model_copy(
        update={
            "created_at_ns": plan.created_at_ns - age,
            "entry_expires_at_ns": plan.entry_expires_at_ns - age,
            "updated_at_ns": plan.updated_at_ns - age,
        }
    )
    with closing(connect_postgres_test(read_only=False)) as conn:
        repos = repositories_for_connection(conn)
        values = prepare_trade_plan(old)
        with repos.transaction():
            assert repos.trading.insert_trade_plan(values)
        assert load_recovery_inputs(repos, plan.account_slot, "paper") == (old,)
        assert load_recovery_inputs(repos, plan.account_slot, "live") == ()
        assert load_recovery_inputs(repos, "another-slot", "paper") == ()
        assert conn.execute("SELECT count(*) AS n FROM trading_execution_observations").fetchone()["n"] == 0


def test_retry_after_a_lost_commit_receipt_cannot_submit_twice() -> None:
    harness = registered_oi_strategy(values=(trade_signal(),))
    harness.strategy.on_timer(None)
    plan = harness.plans.pending_prepare()
    assert plan is not None
    with closing(connect_postgres_test(read_only=False)) as conn:
        repos = repositories_for_connection(conn)
        values = prepare_trade_plan(plan)
        with repos.transaction():
            assert repos.trading.insert_trade_plan(values)
        # The transaction committed but no callback receipt was delivered; the bridge observes
        # the existing identity on retry and asks for venue reconciliation, never a new entry.
        _bridge(harness)._flush_trade_plans(repos)
        harness.strategy.on_timer(None)
        assert harness.strategy.submitted == []
        assert harness.reconciliation_requests == ["unknown_outcome"]


def test_frozen_intent_and_terminal_plan_cannot_be_rewritten_or_recovered() -> None:
    from psycopg.errors import RaiseException, UniqueViolation

    from tracefold.trading.storage.trade_plans import prepare_trade_plan_update

    harness = registered_oi_strategy(values=(trade_signal(),))
    harness.strategy.on_timer(None)
    plan = harness.plans.pending_prepare()
    assert plan is not None
    with closing(connect_postgres_test(read_only=False)) as conn:
        repos = repositories_for_connection(conn)
        with repos.transaction():
            repos.trading.insert_trade_plan(prepare_trade_plan(plan))
        for sql in (
            "UPDATE trading_trade_plans SET stop_distance_bps = 400",
            "UPDATE trading_trade_plans SET take_profit_bps = 500",
            "DELETE FROM trading_trade_plans",
        ):
            with pytest.raises(RaiseException), repos.transaction():
                conn.execute(sql)
        competitor = plan.model_copy(update={"entry_id": "2" * 64, "entry_client_order_id": "tf" + "2" * 30})
        with pytest.raises(UniqueViolation), repos.transaction():
            repos.trading.insert_trade_plan(prepare_trade_plan(competitor))
        closed = plan.model_copy(
            update={
                "status": "closed",
                "terminal_at_ns": NOW_NS + 1,
                "updated_at_ns": NOW_NS + 1,
                "exit_reason": "not_submitted",
            }
        )
        with repos.transaction():
            assert repos.trading.update_trade_plan(prepare_trade_plan_update(closed))
        assert load_recovery_inputs(repos, plan.account_slot, "paper") == ()
        with pytest.raises(RaiseException, match="trade_plan_terminal_immutable"), repos.transaction():
            conn.execute("UPDATE trading_trade_plans SET status = 'open', terminal_at_ns = NULL")
        # A lost receipt on an already terminal identity still cannot grant submit authority.
        _bridge(harness)._flush_trade_plans(repos)
        harness.strategy.on_timer(None)
        assert harness.strategy.submitted == []
