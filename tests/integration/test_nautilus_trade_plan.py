"""Real PostgreSQL under the entry handshake and the plan store (#644, #680)."""

from __future__ import annotations

from contextlib import closing

import pytest
from psycopg.errors import RaiseException, UniqueViolation

from tests.nautilus_oi_runtime_fixtures import NOW_NS, open_plan, trade_signal, unit_runtime
from tests.postgres_test_utils import connect_postgres_test
from tracefold.app.nautilus.oi_runtime import (
    OiRuntimeDatabaseBridge,
    commit_entry_plan,
    load_runtime_inputs,
)
from tracefold.app.repository_session import repositories_for_connection
from tracefold.integrations.nautilus.oi_runtime.journal import EntryValidityReceipt
from tracefold.integrations.nautilus.oi_runtime.singleton import AccountSlotSingleton
from tracefold.trading.storage.trade_plans import prepare_trade_plan, prepare_trade_plan_update

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_clone_dsn")]


def _bridge(runtime) -> OiRuntimeDatabaseBridge:  # type: ignore[no-untyped-def]
    singleton = AccountSlotSingleton(
        account_slot=runtime.profile.account_slot,
        try_acquire=lambda _slot: True,
        release=lambda _slot: True,
        heartbeat=lambda: True,
    )
    assert singleton.acquire()
    return OiRuntimeDatabaseBridge(
        settings=None,
        profile=runtime.profile,
        signals=runtime.signals,
        journal=runtime.journal,
        update_day_start=lambda _baseline: None,
        singleton=singleton,
    )


def test_the_plan_is_durable_on_another_connection_before_its_entry_order_exists() -> None:
    runtime = unit_runtime(signals=(trade_signal(),))
    runtime.pump()
    assert runtime.strategy.submitted == []
    with closing(connect_postgres_test(read_only=False)) as conn:
        bridge = _bridge(runtime)
        repos = repositories_for_connection(conn)
        bridge._cycle(repos)
        with closing(connect_postgres_test(read_only=True)) as observer:
            row = observer.execute("SELECT entry_id, status FROM trading_trade_plans").fetchone()
            assert row == {"entry_id": trade_signal().signal_id, "status": "prepared"}
        assert runtime.strategy.submitted == []
        runtime.pump()
        assert runtime.strategy.submitted == []
        pending = runtime.journal.pending_entry_validity()
        assert pending is not None and pending.entry_id == trade_signal().signal_id
        runtime.journal.settle_entry_validity(
            EntryValidityReceipt(entry_id=pending.entry_id, allowed=True, reason="fixture_valid", checked_at_ns=NOW_NS)
        )
        runtime.pump()
        [(order, _position)] = runtime.strategy.submitted
        assert order.client_order_id.value == open_plan(opened_at_ns=None).entry_client_order_id


@pytest.mark.parametrize("deferred", [False, True], ids=["insert-refused", "commit-refused"])
def test_a_refused_insert_or_commit_answers_the_strategy_and_never_authorizes_an_order(deferred: bool) -> None:
    runtime = unit_runtime(signals=(trade_signal(),))
    runtime.pump()
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
        _bridge(runtime)._cycle(repositories_for_connection(conn))
        runtime.pump()
        assert runtime.strategy.submitted == []
        assert conn.execute("SELECT count(*) AS n FROM trading_trade_plans").fetchone()["n"] == 0
    assert runtime.dispositions() == [{"disposition": "trade_plan_rejected"}]


def test_a_lost_commit_receipt_retried_finds_its_own_plan_and_a_foreign_one_authorizes_nothing() -> None:
    plan = open_plan(opened_at_ns=None, created_at_ns=NOW_NS)
    with closing(connect_postgres_test(read_only=False)) as conn:
        repos = repositories_for_connection(conn)
        with repos.transaction():
            assert repos.trading.insert_trade_plan(prepare_trade_plan(plan))
        # The transaction committed and its receipt was lost: the retry reads back exactly this plan.
        assert commit_entry_plan(repos, plan).committed is True
        foreign = plan.model_copy(update={"entry_quantity": plan.entry_quantity * 2})
        receipt = commit_entry_plan(repos, foreign)
        assert (receipt.committed, receipt.reason) == (False, "trade_plan_conflict")


@pytest.mark.parametrize("age_days", [8, 30])
def test_open_plans_are_the_restart_input_whatever_their_age_and_only_for_their_slot_and_mode(age_days: int) -> None:
    from tests.nautilus_oi_runtime_fixtures import oi_profile

    age = age_days * 86_400_000_000_000
    plan = open_plan(opened_at_ns=None, created_at_ns=NOW_NS - age)
    with closing(connect_postgres_test(read_only=False)) as conn:
        repos = repositories_for_connection(conn)
        with repos.transaction():
            assert repos.trading.insert_trade_plan(prepare_trade_plan(plan))
        inputs = load_runtime_inputs(repos, oi_profile(), now_ns=NOW_NS)
        assert [(value.plan, value.disposition_pending) for value in inputs.open_plans] == [(plan, True)]
        assert load_runtime_inputs(repos, oi_profile("live"), now_ns=NOW_NS).open_plans == ()
        assert conn.execute("SELECT count(*) AS n FROM trading_execution_observations").fetchone()["n"] == 0


def test_frozen_intent_a_terminal_plan_and_the_open_clock_cannot_be_rewritten() -> None:
    plan = open_plan(opened_at_ns=None, created_at_ns=NOW_NS)
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

        opened = plan.opened(opened_at_ns=NOW_NS + 5, now_ns=NOW_NS + 5)
        with repos.transaction():
            assert repos.trading.update_trade_plan(prepare_trade_plan_update(opened))
        # A later transition carrying a different open clock keeps the one already written.
        reopened = opened.model_copy(update={"opened_at_ns": NOW_NS + 9, "updated_at_ns": NOW_NS + 9})
        with repos.transaction():
            assert repos.trading.update_trade_plan(prepare_trade_plan_update(reopened))
        assert repos.trading.trade_plan(plan.entry_id)["opened_at_ns"] == NOW_NS + 5

        closed = opened.closed(reason="stop_filled", terminal_at_ns=NOW_NS + 10, now_ns=NOW_NS + 10)
        with repos.transaction():
            assert repos.trading.update_trade_plan(prepare_trade_plan_update(closed))
        # A terminal plan is final: a late transition is a no-op, not an error, and SQL cannot reopen it.
        with repos.transaction():
            assert repos.trading.update_trade_plan(prepare_trade_plan_update(reopened)) is False
        with pytest.raises(RaiseException, match="trade_plan_terminal_immutable"), repos.transaction():
            conn.execute("UPDATE trading_trade_plans SET status = 'open', terminal_at_ns = NULL")
        assert repos.trading.recent_stop_exits(account_slot=plan.account_slot, since_ns=NOW_NS) == {
            plan.market_key: NOW_NS + 10
        }
        assert repos.trading.recent_stop_exits(account_slot=plan.account_slot, since_ns=NOW_NS + 11) == {}


def test_mixed_exit_is_durable() -> None:
    plan = open_plan()
    closed = plan.closed(reason="mixed_exit", terminal_at_ns=NOW_NS + 10, now_ns=NOW_NS + 10)
    with closing(connect_postgres_test(read_only=False)) as conn:
        repos = repositories_for_connection(conn)
        with repos.transaction():
            assert repos.trading.insert_trade_plan(prepare_trade_plan(plan))
            assert repos.trading.update_trade_plan(prepare_trade_plan_update(closed))
        assert repos.trading.trade_plan(plan.entry_id)["exit_reason"] == "mixed_exit"
