"""Populated P0→P4 equivalence and transactional rejection of unsafe Trading facts."""

from __future__ import annotations

from contextlib import closing
from decimal import Decimal

import pytest
from alembic import command

from tests.fixtures.trading_analysis_0423 import FrozenAnalysis0423
from tests.fixtures.trading_executor_0423 import FrozenExecutor0423
from tests.integration.test_trading_analysis_closure import _selection
from tests.postgres_test_utils import (
    connect_postgres_test,
    postgres_migration_test_dsn,
    prepare_test_migration_database,
)
from tracefold.platform.postgres.audit import NEWS_TABLES, TRADING_TABLES
from tracefold.platform.postgres.migrations import alembic_config
from tracefold.platform.postgres.runtime_processes import RuntimeProcesses
from tracefold.trading.engine.forecast import Forecast, LegProbabilities, PolicyDecision
from tracefold.trading.engine.paper import PaperLeg
from tracefold.trading.executor.core import SignalV4
from tracefold.trading.operator_control import prepare_operator_intent
from tracefold.trading.storage.root import TradingRepository

pytestmark = [pytest.mark.integration, pytest.mark.migration, pytest.mark.usefixtures("postgres_migration_dsn")]
SOURCE, TARGET = "20261001_0423", "20261001_0424"
SLOT, AT = "migration-demo", 1_800_000_000_000


class FrozenTrading0423(FrozenAnalysis0423, FrozenExecutor0423):
    pass


@pytest.fixture
def source(postgres_migration_dsn):
    with closing(connect_postgres_test()) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.commit()
    prepare_test_migration_database(postgres_migration_dsn)
    config = alembic_config()
    config.attributes["database_url"] = postgres_migration_test_dsn()
    command.upgrade(config, SOURCE)
    return config


def seed(conn):
    db = FrozenTrading0423(conn)
    _, case_id, _ = db.accept_trigger(
        kind="oi",
        source_fact_key="migration-oi",
        source_revision="v1",
        payload_sha256="a" * 64,
        payload={"kind": "oi", "evidence_ref": "frozen-source"},
        selection=_selection(),
        now_ms=AT,
        root_ttl_ms=600_000,
    )
    claim = db.claim_case(now_ms=AT, lease_ms=60_000)
    assert db.freeze_case(
        case_id=case_id,
        claim_token=claim["claim_token"],
        now_ms=AT,
        view={"model_input": {"frozen": "exact", "decimal": "0.1234567890123456789012345678"}},
        raw_snapshot_ref="b" * 64,
        geometry_version="leg_geometry_v1",
        stop_bps=100,
        tp_bps=200,
        half_spread_bps=Decimal("0.000123456789"),
        reference_price=Decimal("100"),
    )
    db.record_assessment(
        case_id=case_id,
        program_sha="c" * 64,
        route="fixture",
        status="ok",
        forecast=Forecast(
            LegProbabilities(Decimal("0.7"), Decimal("0.2"), Decimal("0.1")),
            LegProbabilities(Decimal("0.2"), Decimal("0.7"), Decimal("0.1")),
        ),
        notes=("frozen",),
        usage={"input_tokens": 17, "output_tokens": 19},
        started_at_ms=AT,
        ended_at_ms=AT + 1,
    )
    db.record_policy_actions(
        case_id=case_id,
        program_sha="c" * 64,
        decisions=(
            PolicyDecision("p0", "v1", "long", "fixture", Decimal("0.1234567890123456789012345678")),
            PolicyDecision("p1", "v1", "abstain", "fixture"),
        ),
        now_ms=AT + 1,
    )
    db.record_paper_legs(
        case_id=case_id,
        geometry_version="leg_geometry_v1",
        now_ms=AT + 20_000,
        legs=tuple(
            PaperLeg(
                side,
                "complete",
                "timeout",
                None,
                AT + 2,
                AT + 10_000,
                Decimal("100"),
                Decimal("101"),
                Decimal("100"),
                Decimal("10.000123456789"),
                Decimal("0.89999876543211"),
            )
            for side in ("long", "short")
        ),
    )
    db.finish_case(
        case_id=case_id, claim_token=claim["claim_token"], status="complete", failure_code=None, now_ms=AT + 2
    )
    amendment = {"kind": "source_update", "affected_claim_refs": ["claim-old"], "retired_claim_refs": ["claim-old"]}
    db.receive_source_update(
        update_id="public:fixture",
        source_fact_key="migration-catalyst",
        content_revision="v2",
        affected_claim_refs=("claim-old",),
        retired_claim_refs=("claim-old",),
        payload=amendment,
        payload_sha256="d" * 64,
        now_ms=AT + 3,
    )
    db.heartbeat(account_slot=SLOT, now_ns=AT * 1_000_000)
    db.set_control(account_slot=SLOT, paused=False, halted=False, now_ns=AT * 1_000_000)
    db.record_full_reconciliation(
        account_slot=SLOT,
        now_ns=AT * 1_000_000,
        unexpected=True,
        account_snapshot={"observed_at_ns": AT * 1_000_000, "positions": []},
    )
    for key, disposition in (("1", "accepted"), ("2", "refused"), ("3", "expired"), ("4", None)):
        sig = SignalV4(
            signal_id=key * 64,
            case_id=case_id,
            decision_id="c" * 64,
            account_slot=SLOT,
            entry_scope_id=key * 64,
            asset_id="crypto:SOL",
            native_symbol="SOLUSDT",
            mapping_semantics_digest="d" * 64,
            side="long",
            reference_price=Decimal("100"),
            max_drift_bps=100,
            stop_bps=100,
            tp_bps=200,
            max_hold_s=3600,
            policy_id="p0",
            policy_version="v1",
            geometry_version="leg_geometry_v1",
            decided_at_ns=(AT + int(key)) * 1_000_000,
            expires_at_ns=(AT + 60_000) * 1_000_000,
        )
        db.append_signal(sig)
        if disposition:
            if disposition == "accepted":
                db.create_plan(
                    plan_id=sig.signal_id,
                    signal_id=sig.signal_id,
                    command_id=None,
                    account_slot=SLOT,
                    native_symbol="SOLUSDT",
                    side="long",
                    quantity="1",
                    reference_price="100",
                    stop_bps=100,
                    tp_bps=200,
                    max_hold_s=3600,
                    now_ns=(AT + 5) * 1_000_000,
                )
            db.record_disposition(
                kind="signal",
                input_id=sig.signal_id,
                account_slot=SLOT,
                disposition=disposition,
                reason=disposition,
                plan_id=sig.signal_id if disposition == "accepted" else None,
                now_ns=(AT + 5) * 1_000_000,
            )
    db.set_publication(
        case_id=case_id,
        program_sha="c" * 64,
        policy_id="p0",
        policy_version="v1",
        publish_status="published",
        signal_id="1" * 64,
    )
    for key, action in (("5", "manual_entry"), ("6", "flatten"), ("7", "pause_entries")):
        intent = prepare_operator_intent(
            command_id=key * 64,
            account_slot=SLOT,
            action=action,
            scope="market" if action == "manual_entry" else "account",
            reason="fixture",
            operator_identity="fixture",
            authentication_identity="fixture",
            requested_at_ns=AT * 1_000_000,
            expires_at_ns=(AT + 600_000) * 1_000_000,
            market_key="crypto:perp:ETH:USDT" if action == "manual_entry" else None,
            direction="short" if action == "manual_entry" else None,
        )
        db.append_operator_intent(intent)
        if action == "manual_entry":
            db.create_plan(
                plan_id=key * 64,
                signal_id=None,
                command_id=key * 64,
                account_slot=SLOT,
                native_symbol="ETHUSDT",
                side="short",
                quantity="2",
                reference_price="100",
                stop_bps=100,
                tp_bps=200,
                max_hold_s=3600,
                now_ns=AT * 1_000_000,
            )
        if action != "pause_entries":
            db.record_disposition(
                kind="intent",
                input_id=key * 64,
                account_slot=SLOT,
                disposition="accepted",
                reason="accepted",
                plan_id=key * 64 if action == "manual_entry" else None,
                now_ns=AT * 1_000_000,
            )
    db.request_flatten(account_slot=SLOT, command_id="6" * 64, now_ns=(AT + 3) * 1_000_000)
    db.set_control(account_slot=SLOT, paused=True, halted=True, now_ns=(AT + 4) * 1_000_000)
    db.reserve_order(
        client_id="migration-order",
        plan_id="1" * 64,
        native_symbol="SOLUSDT",
        leg="entry",
        attempt=1,
        now_ns=AT * 1_000_000,
    )
    db.update_order(
        client_id="migration-order",
        status="filled",
        now_ns=AT * 1_000_000,
        venue_order_id="100",
        evidence={"status": "FILLED", "executedQty": "1"},
    )
    for trade_id in (1, 2):
        db.record_fill(
            symbol="SOLUSDT",
            trade={
                "id": trade_id,
                "orderId": 100 if trade_id == 1 else 101,
                "qty": "1",
                "price": "100",
                "realizedPnl": "0",
                "commission": "0.01",
                "commissionAsset": "USDT",
                "time": AT,
            },
        )
    db.attribute_unbound_fills(symbol="SOLUSDT", now_ns=AT * 1_000_000)
    db.advance_trade_cursor(symbol="SOLUSDT", next_id=3, now_ns=AT * 1_000_000)
    conn.execute("""INSERT INTO workers_runtime(singleton_key,runtime_id,runtime_version,lifecycle_state,
        started_at_ms,heartbeat_at_ms,runtime_revision,image_digest,capabilities)
        VALUES (true,'00000000-0000-0000-0000-000000000099','2','stopped',10,20,'fixture','sha256:fixture',
        '{"news_editorial":{"state":"faulted","reason":"fixture"}}')""")
    db.heartbeat_analysis_runtime(
        runtime_id=SLOT,
        now_ms=AT,
        active_policy="p0",
        program_sha="c" * 64,
        model_name="fixture",
        model_configured=True,
        publish_signals=True,
        config_digest="d" * 64,
        fault_code=None,
    )
    conn.commit()
    return case_id


def projections(db, case_id):
    return {
        "case": db.analysis_case(case_id),
        "executions": db.console_executions(since_ns=0, limit=100),
        "commands": db.console_operator_intents(since_ns=0, action=None, limit=100),
        "fills": db.fill_ledger(since_ns=0, limit=100),
    }


@pytest.mark.parametrize("start", ["20261001_0419", SOURCE])
def test_populated_chain_preserves_model_inputs_and_all_public_ledgers(source, start, postgres_migration_dsn, capsys):
    if start != SOURCE:
        with closing(connect_postgres_test()) as conn:
            conn.execute("DROP SCHEMA public CASCADE")
            conn.execute("CREATE SCHEMA public")
            conn.commit()
        prepare_test_migration_database(postgres_migration_dsn)
        command.upgrade(source, start)
    with closing(connect_postgres_test()) as conn:
        case_id = seed(conn)
        expected = projections(FrozenTrading0423(conn), case_id)
        # The intentional CLI key change preserves every fill identity and value.
        expected["fills"] = [
            {"entry_id": row["plan_id"], **{k: v for k, v in row.items() if k != "plan_id"}}
            for row in expected["fills"]
        ]
    command.upgrade(source, TARGET)
    verify_log = capsys.readouterr().err
    assert "p4_verify ok" in verify_log
    assert sum(" md5=" in line for line in verify_log.splitlines() if line.startswith("p4_verify")) == 13
    with closing(connect_postgres_test()) as conn:
        tables = {row["tablename"] for row in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")}
        assert tables == {
            "alembic_version",
            "runtime_processes",
            *(set(NEWS_TABLES) - {"news_reader_clock", "news_claim_index"}),
            *TRADING_TABLES,
        }
        assert len(tables) == 26 and len(TRADING_TABLES) == 7
        account = TradingRepository(conn).account(SLOT)
        assert (account["entries_paused"], account["emergency_halted"], account["unexpected_exposure"]) == (
            True,
            True,
            True,
        )
        assert account["flatten_command_id"] == "6" * 64
        assert account["trade_cursors"]["SOLUSDT"] == {"next_trade_id": 3, "checked_at_ns": AT * 1_000_000}
        fills = conn.execute(
            "SELECT trade_id,account_slot,client_order_id,attributed_at_ns FROM trading_fills ORDER BY trade_id"
        ).fetchall()
        assert fills[0]["account_slot"] == SLOT and fills[0]["client_order_id"] == "migration-order"
        assert fills[1]["account_slot"] is None and fills[1]["client_order_id"] is None
        assert (
            conn.execute("SELECT count(*) AS n FROM runtime_processes WHERE process_kind<>'workers'").fetchone()["n"]
            == 0
        )
        assert RuntimeProcesses(conn).workers_row()["capabilities"]["news_editorial"]["state"] == "faulted"

    # Historical P4 assertions above remain at 0424. The current repository
    # requires the current schema; legacy rows acquire nullable evidence, not facts.
    command.upgrade(source, "head")
    for row in expected["executions"]:
        if row.get("disposition_reason") == "accepted":
            row.update(admission=None, reserved_margin_usdt=None, entry_resolution=None)
    with closing(connect_postgres_test()) as conn:
        assert projections(TradingRepository(conn), case_id) == expected
        assert TradingRepository(conn).account(SLOT)["execution_faults"] == {}


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("ALTER TABLE trading_executor_state ADD last_signal_seq bigint", "p4_requires_p0"),
        (
            "INSERT INTO trading_assessments SELECT case_id,repeat('e',64),route,status,forecast,drivers,notes,"
            "input_tokens,output_tokens,started_at_ms,ended_at_ms FROM trading_assessments",
            "p4_replay_assessments_present",
        ),
        ("UPDATE trading_policy_actions SET program_sha=repeat('e',64)", "p4_action_assessment_mismatch"),
        ("DELETE FROM trading_paper_legs WHERE side='short'", "p4_paper_case_mismatch"),
        ("UPDATE trading_source_amendments SET affected_claim_refs='[]'", "p4_amendment_claim_mismatch"),
        ("UPDATE trading_signals SET payload=jsonb_set(payload,'{case_id}','\"wrong\"')", "p4_signal_case_mismatch"),
        ("UPDATE trading_dispositions SET account_slot='wrong'", "p4_disposition_input_mismatch"),
        ("UPDATE trading_plans SET side='short' WHERE signal_id IS NOT NULL", "p4_plan_disposition_mismatch"),
        ("UPDATE trading_orders SET venue_order_id='wrong'", "p4_fill_attribution_mismatch"),
        ("UPDATE trading_cases SET claim_token='wrong'", "p4_case_claim_mismatch"),
        ("UPDATE trading_control_state SET flatten_command_id=repeat('7',64)", "p4_flatten_command_mismatch"),
        (
            "INSERT INTO trading_executor_state(account_slot,environment,heartbeat_at_ns) VALUES ('another','DEMO',1)",
            "p4_trade_cursor_account_ambiguous",
        ),
    ],
)
def test_preflight_rolls_back_without_deleting_predecessor_facts(source, mutation, reason):
    with closing(connect_postgres_test()) as conn:
        seed(conn)
        for table in (
            "trading_policy_actions",
            "trading_paper_legs",
            "trading_signals",
            "trading_dispositions",
            "trading_fill_attributions",
            "trading_source_amendments",
        ):
            conn.execute(f"ALTER TABLE {table} DISABLE TRIGGER USER")
        conn.execute(mutation)
        conn.commit()
    with pytest.raises(Exception, match=reason):
        command.upgrade(source, TARGET)
    with closing(connect_postgres_test()) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"] == SOURCE
        assert conn.execute("SELECT to_regclass('trading_entries') AS rel").fetchone()["rel"] is None
        assert conn.execute("SELECT count(*) AS n FROM trading_signals").fetchone()["n"] == 4


def test_preflight_refuses_connected_writer(source):
    with closing(connect_postgres_test()) as writer:
        writer.execute("SET application_name='tracefold_analysis'")
        writer.commit()
        with pytest.raises(Exception, match="p4_writers_connected"):
            command.upgrade(source, TARGET)


def test_cut_is_forward_only(source):
    command.upgrade(source, TARGET)
    with pytest.raises(RuntimeError, match="P4 is forward-only"):
        command.downgrade(source, SOURCE)
