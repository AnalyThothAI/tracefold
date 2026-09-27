"""Native economics are unique across producer paths and late enrichment in real PostgreSQL."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from decimal import Decimal
from threading import Barrier

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tracefold.trading.native_fills import NativeFill
from tracefold.trading.storage.execution_stream import prepare_execution_observations, prepare_operator_intent
from tracefold.trading.storage.root import TradingRepository
from tracefold.trading.trade_plan import PlanOrderBinding

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_clone_dsn")]

FILL = NativeFill(
    account_slot="binance_usdm_primary",
    environment="DEMO",
    instrument="INJUSDT",
    trade_id="63772472",
    order_id="308654865",
    side="SELL",
    quantity=Decimal("121.3"),
    price=Decimal("8.382"),
    occurred_at_ns=1790338365075_000000,
)


def _rows(fill=FILL, **kwargs):
    return fill.observation(execution_strategy="oi_nautilus_v1", observed_at_ns=fill.occurred_at_ns + 1, **kwargs)


def _append(rows):
    with closing(connect_postgres_test(read_only=False)) as conn, conn.transaction():
        return TradingRepository(conn).append_execution_observations(prepare_execution_observations(rows))


def test_late_cost_and_business_binding_cannot_create_a_second_economic_fill():
    [original] = _rows()
    [seq] = _append((original,))
    binding = PlanOrderBinding(
        account_slot=FILL.account_slot,
        entry_id="a" * 64,
        source="manual",
        instrument_id="INJUSDT-PERP.BINANCE",
        client_order_id="tff9790ec0ab0d903641baaa5549a9f9",
        leg="take_profit",
        exit_reason="take_profit",
    )
    with closing(connect_postgres_test(read_only=False)) as conn, conn.transaction():
        repo = TradingRepository(conn)
        repo.ensure_execution_runtime_control_state(FILL.account_slot, now_ns=FILL.occurred_at_ns)
        repo.append_operator_intent(
            prepare_operator_intent(
                command_id=binding.entry_id,
                account_slot=FILL.account_slot,
                action="manual_entry",
                scope="market",
                reason="native fill association fixture",
                operator_identity="operator:test",
                authentication_identity="test:local",
                requested_at_ns=FILL.occurred_at_ns - 1,
                expires_at_ns=FILL.occurred_at_ns + 60_000_000_000,
                market_key="crypto:perp:INJ:USDT",
                direction="long",
            )
        )
    rows = _rows(commission=Decimal("0.40669464"), commission_currency="USDT", binding=binding)
    ids = _append(rows)
    assert ids[0] == seq
    assert _append(rows) == ids
    # A distinct event/report ID and producer version still names this same
    # economic fact. The unique venue key, not that event UUID, decides identity.
    replay = original.model_copy(
        update={
            "event_id": "f" * 64,
            "observed_at_ns": FILL.occurred_at_ns + 99,
            "execution_strategy": "oi_nautilus_v2",
        }
    )
    assert _append((replay,)) == (seq,)
    with closing(connect_postgres_test(read_only=True)) as conn:
        result = conn.execute(
            "SELECT normalized_kind, count(*) AS n FROM trading_execution_observations GROUP BY normalized_kind"
        ).fetchall()
    assert {row["normalized_kind"]: row["n"] for row in result} == {
        "native_fill": 1,
        "native_fill_cost": 1,
        "native_fill_binding": 1,
    }


@pytest.mark.parametrize(
    "update",
    [
        {"quantity": Decimal("122")},
        {"price": Decimal("8.4")},
        {"side": "BUY"},
        {"order_id": "308654866"},
        {"occurred_at_ns": FILL.occurred_at_ns + 1},
    ],
)
def test_same_trade_with_contradictory_economics_rolls_back_the_whole_batch(update):
    seq = _append(_rows())
    offered = (*_rows(replace(FILL, trade_id="63772473")), *_rows(replace(FILL, **update)))
    with closing(connect_postgres_test(read_only=False)) as conn, conn.transaction():
        repo = TradingRepository(conn)
        with pytest.raises(RuntimeError, match="execution_stream_identity_conflict"):
            repo.append_execution_observations(prepare_execution_observations(offered))
        assert conn.execute("SELECT count(*) AS n FROM trading_execution_observations").fetchone()["n"] == 1
        assert repo.append_execution_observations(prepare_execution_observations(_rows())) == seq


def test_cost_for_a_different_order_is_a_conflict_even_when_economics_are_already_stored():
    _append(_rows())
    [_, cost] = _rows(replace(FILL, order_id="308654866"), commission=Decimal("0.4"), commission_currency="USDT")
    with pytest.raises(RuntimeError, match="execution_stream_native_trade_conflict"):
        _append((cost,))
    with closing(connect_postgres_test(read_only=True)) as conn:
        assert conn.execute("SELECT count(*) AS n FROM trading_execution_observations").fetchone()["n"] == 1


@pytest.mark.parametrize(
    "update",
    [
        {"environment": "LIVE"},
        {"instrument": "APTUSDT"},
        {"account_slot": "another_account"},
    ],
)
def test_native_trade_ids_are_scoped_by_account_environment_and_instrument(update):
    _append(_rows())
    _append(_rows(replace(FILL, **update)))
    with closing(connect_postgres_test(read_only=True)) as conn:
        assert conn.execute("SELECT count(*) AS n FROM trading_execution_observations").fetchone()["n"] == 2


def test_concurrent_ws_and_rest_replays_share_one_native_economic_sequence():
    ready = Barrier(2)

    def write(event_id):
        [value] = _rows()
        value = value.model_copy(update={"event_id": event_id})
        with closing(connect_postgres_test(read_only=False)) as conn, conn.transaction():
            ready.wait(timeout=5)
            return TradingRepository(conn).append_execution_observations(prepare_execution_observations((value,)))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(write, identity * 64) for identity in ("a", "b")]
        sequences = [future.result(timeout=10) for future in futures]
    assert sequences[0] == sequences[1]
    with closing(connect_postgres_test(read_only=True)) as conn:
        assert conn.execute("SELECT count(*) AS n FROM trading_execution_observations").fetchone()["n"] == 1


def test_concurrent_cost_and_fill_cannot_disagree_about_native_order_identity():
    ready = Barrier(2)
    [fill] = _rows()
    [_, cost] = _rows(replace(FILL, order_id="308654866"), commission=Decimal("0.4"), commission_currency="USDT")

    def write(row):
        with closing(connect_postgres_test(read_only=False)) as conn, conn.transaction():
            ready.wait(timeout=5)
            try:
                TradingRepository(conn).append_execution_observations(prepare_execution_observations((row,)))
                return "written"
            except RuntimeError as exc:
                assert str(exc) == "execution_stream_native_trade_conflict"
                return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(write, row) for row in (fill, cost)]
        assert sorted(result.result(timeout=10) for result in results) == ["conflict", "written"]
    with closing(connect_postgres_test(read_only=True)) as conn:
        assert conn.execute("SELECT count(*) AS n FROM trading_execution_observations").fetchone()["n"] == 1


def test_recorded_inj_native_result_and_terminal_transition_are_one_durable_unit():
    import asyncio
    import json
    from pathlib import Path
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import msgspec
    from nautilus_trader.adapters.binance.common.enums import BinanceEnvironment
    from nautilus_trader.adapters.binance.common.schemas.account import BinanceOrder, BinanceUserTrade
    from nautilus_trader.adapters.binance.futures.schemas.account import BinanceFuturesAlgoOrder

    from tests.helpers.published_signal_v3 import append_published_v3_signal
    from tracefold.app.nautilus.oi_runtime import write_journal_row, write_terminal_plan
    from tracefold.app.repository_session import repositories_for_connection
    from tracefold.integrations.nautilus.oi_runtime.journal import ExecutionJournal, ObservationFactory
    from tracefold.integrations.nautilus.oi_runtime.observations import offer_native_evidence
    from tracefold.integrations.nautilus.oi_runtime.order_evidence import OrderEvidenceRequest, read_order_evidence
    from tracefold.trading.execution_contracts import ExecutionObservationV1
    from tracefold.trading.storage.trade_plans import prepare_trade_plan
    from tracefold.trading.trade_plan import TradePlan

    fixture = json.loads((Path(__file__).parents[1] / "fixtures/binance/inj_20260925_execution.json").read_text())
    raw = fixture["original_plan"]
    plan = TradePlan.model_validate_json(
        json.dumps(
            raw
            | {
                "status": "open",
                "terminal_at_ns": None,
                "exit_reason": None,
                "updated_at_ns": raw["opened_at_ns"],
            }
        )
    )
    closed = plan.closed(reason="take_profit", terminal_at_ns=1790338365075_000000, now_ns=1790338365075_000000)
    legacy_fill = ExecutionObservationV1.model_validate(fixture["original_entry_observation"])
    journal = ExecutionJournal(factory=ObservationFactory(plan.account_slot, "oi_nautilus_v1"))
    with closing(connect_postgres_test(read_only=False)) as conn:
        repo = TradingRepository(conn)
        session = repositories_for_connection(conn)
        with conn.transaction():
            repo.ensure_execution_runtime_control_state(plan.account_slot, now_ns=plan.created_at_ns)
        append_published_v3_signal(
            repo,
            signal_id=plan.entry_id,
            case_id=plan.case_id,
            observed_at_ns=plan.created_at_ns - 1,
            expires_at_ns=plan.entry_expires_at_ns,
        )
        with conn.transaction():
            assert repo.insert_trade_plan(prepare_trade_plan(plan))
        write_journal_row(session, legacy_fill)
        assert repo.execution_result(entry_id=plan.entry_id)["fill_quantity"] is None
        with pytest.raises(ValueError, match="trade_plan_terminal_entry_evidence_pending"):
            write_terminal_plan(session, closed, ())
        assert TradePlan.model_validate(repo.trade_plan(plan.entry_id)) == plan

        for index in (0, 1):
            order = msgspec.json.decode(msgspec.json.encode(fixture["orders"][index]), type=BinanceOrder)
            parent = msgspec.json.decode(msgspec.json.encode(fixture["algo"]), type=BinanceFuturesAlgoOrder)
            trades = msgspec.json.decode(msgspec.json.encode([fixture["trades"][index]]), type=list[BinanceUserTrade])
            account = SimpleNamespace(
                query_order=AsyncMock(return_value=order),
                query_algo_order=AsyncMock(return_value=parent),
                query_user_trades=AsyncMock(return_value=trades),
            )
            request = OrderEvidenceRequest(
                symbol="INJUSDT",
                client_order_id=order.clientOrderId if index == 0 else parent.clientAlgoId,
                venue_order_id=order.orderId,
                conditional_type=None if index == 0 else "TAKE_PROFIT_MARKET",
            )
            proof = asyncio.run(read_order_evidence(account, request=request, observed_at_ns=1790380800_000000000))
            binding = PlanOrderBinding(
                account_slot=plan.account_slot,
                entry_id=plan.entry_id,
                source=plan.source,
                instrument_id=plan.instrument_id,
                client_order_id=request.client_order_id,
                leg="entry" if index == 0 else "take_profit",
                exit_reason=None if index == 0 else "take_profit",
            )
            assert offer_native_evidence(
                journal,
                proof,
                environment=BinanceEnvironment.DEMO,
                observed_at_ns=1790380800_000000000,
                binding=binding,
            )

        dependencies = journal.terminal_dependencies(closed)
        assert len(dependencies) == 8
        # An interrupted transaction leaves the open Plan and zero native facts.
        with pytest.raises(RuntimeError, match="simulated_pg_interrupt"), conn.transaction():
            write_terminal_plan(session, closed, dependencies)
            raise RuntimeError("simulated_pg_interrupt")
        assert TradePlan.model_validate(repo.trade_plan(plan.entry_id)) == plan
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM trading_execution_observations WHERE normalized_kind='native_fill'"
            ).fetchone()["n"]
            == 0
        )
        write_terminal_plan(session, closed, dependencies)
        result = repo.execution_result(entry_id=plan.entry_id)
        assert TradePlan.model_validate(repo.trade_plan(plan.entry_id)) == closed
        assert Decimal(result["fill_quantity"]) == Decimal("121.3")
        assert Decimal(result["fees_usd"]) == Decimal("0.805432")
        assert Decimal(result["realized_pnl_usd"]) == Decimal("19.087768")
        assert result["exit_reason"] == "take_profit"
        assert result["result_evidence_source"] == "signed_native_trades"
        assert result["position_closed_at_ns"] == closed.terminal_at_ns
        assert repo.recent_stop_exits(account_slot=plan.account_slot, since_ns=0) == {}
        write_terminal_plan(session, closed, dependencies)
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM trading_execution_observations WHERE normalized_kind='native_fill'"
            ).fetchone()["n"]
            == 2
        )


def test_pre_submit_refusal_closes_with_disposition_and_no_invented_trade():
    import json
    from pathlib import Path

    from tests.helpers.published_signal_v3 import append_published_v3_signal
    from tracefold.app.nautilus.oi_runtime import write_terminal_plan
    from tracefold.app.repository_session import repositories_for_connection
    from tracefold.integrations.nautilus.oi_runtime.journal import ExecutionJournal, ObservationFactory
    from tracefold.trading.storage.trade_plans import prepare_trade_plan
    from tracefold.trading.trade_plan import TradePlan

    raw = json.loads((Path(__file__).parents[1] / "fixtures/binance/inj_20260925_execution.json").read_text())[
        "original_plan"
    ]
    plan = TradePlan.model_validate_json(
        json.dumps(raw | {"status": "prepared", "opened_at_ns": None, "terminal_at_ns": None, "exit_reason": None})
    )
    closed = plan.closed(reason="not_submitted", terminal_at_ns=plan.created_at_ns + 1, now_ns=plan.created_at_ns + 1)
    journal = ExecutionJournal(factory=ObservationFactory(plan.account_slot, "oi_nautilus_v1"))
    with closing(connect_postgres_test(read_only=False)) as conn:
        repo = TradingRepository(conn)
        session = repositories_for_connection(conn)
        with conn.transaction():
            repo.ensure_execution_runtime_control_state(plan.account_slot, now_ns=plan.created_at_ns)
        append_published_v3_signal(
            repo,
            signal_id=plan.entry_id,
            case_id=plan.case_id,
            observed_at_ns=plan.created_at_ns - 1,
            expires_at_ns=plan.entry_expires_at_ns,
        )
        with conn.transaction():
            repo.insert_trade_plan(prepare_trade_plan(plan))
        with pytest.raises(ValueError, match="trade_plan_terminal_disposition_pending"):
            write_terminal_plan(session, closed, ())
        disposition = journal.factory.create(
            normalized_kind="signal_disposition",
            occurred_at_ns=closed.terminal_at_ns,
            observed_at_ns=closed.terminal_at_ns,
            signal_id=plan.entry_id,
            summary={"disposition": "market_unavailable"},
        )
        assert journal.offer(disposition)
        dependencies = journal.terminal_dependencies(closed)
        assert [row.value.event_id for row in dependencies] == [disposition.event_id]
        write_terminal_plan(session, closed, dependencies)
        assert TradePlan.model_validate(repo.trade_plan(plan.entry_id)) == closed
        assert repo.execution_result(entry_id=plan.entry_id)["fill_quantity"] is None
