"""Forward preservation with failed assessments, fills and a live Plan."""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal

import psycopg
import pytest
from alembic import command
from psycopg.rows import dict_row

from tests.postgres_test_utils import postgres_migration_test_dsn
from tracefold.platform.postgres.migrations import alembic_config
from tracefold.trading.engine.evaluation import EvaluationRun, EvaluatorSpec
from tracefold.trading.engine.forecast import Forecast, LegProbabilities
from tracefold.trading.storage.root import TradingRepository

pytestmark = [pytest.mark.integration, pytest.mark.migration, pytest.mark.usefixtures("postgres_migration_dsn")]


def test_0418_populated_ledger_is_preserved_and_new_runs_are_isolated(postgres_migration_dsn: str) -> None:
    config = alembic_config()
    config.attributes["database_url"] = postgres_migration_test_dsn(postgres_migration_dsn)
    command.upgrade(config, "20260929_0418")
    case, program, decision, signal, plan = (value * 64 for value in ("a", "b", "c", "d", "e"))
    with psycopg.connect(postgres_migration_dsn, autocommit=True, row_factory=dict_row) as conn:
        TradingRepository(conn).seed_restore_drill_case(case_id=case)
        with conn.transaction():
            conn.execute(
                "INSERT INTO trading_assessments "
                "(case_id,program_sha,route,status,started_at_ms,ended_at_ms) "
                "VALUES (%s,%s,'old-model','rate_limit',20,30)",
                (case, program),
            )
            conn.execute(
                "INSERT INTO trading_signals "
                "(signal_id,case_id,decision_id,account_slot,native_symbol,decided_at_ns,"
                "expires_at_ns,payload,created_at_ns) "
                "VALUES (%s,%s,%s,'legacy','RESTOREUSDT',100,1000000100,'{}'::jsonb,100)",
                (signal, case, decision),
            )
            conn.execute(
                "INSERT INTO trading_policy_actions "
                "(case_id,program_sha,policy_id,policy_version,calibrator_version,action,reason,"
                "publish_status,signal_id,decided_at_ms) "
                "VALUES (%s,%s,'always_long','policy_v1','identity_v1','long','baseline','published',%s,40)",
                (case, program, signal),
            )
            conn.execute(
                "INSERT INTO trading_plans "
                "(plan_id,signal_id,account_slot,environment,native_symbol,side,quantity,reference_price,reserved_notional,"
                "stop_bps,tp_bps,max_hold_s,status,opened_at_ns,updated_at_ns) "
                "VALUES (%s,%s,'legacy','DEMO','RESTOREUSDT','long',1,100,100,100,200,14400,'open',100,200)",
                (plan, signal),
            )
            conn.execute(
                "INSERT INTO trading_orders "
                "(client_order_id,plan_id,environment,native_symbol,leg,attempt,status,venue_order_id,updated_at_ns) "
                "VALUES ('legacy-entry',%s,'DEMO','RESTOREUSDT','entry',1,'filled','1',200)",
                (plan,),
            )
            for trade, venue_order in ((1, "1"), (2, "older-unbound")):
                conn.execute(
                    "INSERT INTO trading_fills "
                    "(environment,native_symbol,trade_id,venue_order_id,quantity,price,realized_pnl,fee,fee_asset,"
                    "traded_at_ns,evidence) VALUES ('DEMO','RESTOREUSDT',%s,%s,1,100,0,.01,'USDT',100,'{}'::jsonb)",
                    (trade, venue_order),
                )
            conn.execute(
                "INSERT INTO trading_fill_attributions "
                "(environment,native_symbol,trade_id,plan_id,client_order_id,attributed_at_ns) "
                "VALUES ('DEMO','RESTOREUSDT',1,%s,'legacy-entry',200)",
                (plan,),
            )
        tables = (
            "trading_triggers",
            "trading_cases",
            "trading_assessments",
            "trading_policy_actions",
            "trading_signals",
            "trading_plans",
            "trading_orders",
            "trading_fills",
            "trading_fill_attributions",
        )
        before = {table: [dict(row) for row in conn.execute(f"SELECT * FROM {table}").fetchall()] for table in tables}
        command.upgrade(config, "head")
        for table, original in before.items():
            after = [dict(row) for row in conn.execute(f"SELECT * FROM {table}").fetchall()]
            assert [{key: row[key] for key in original[0]} for row in after] == original
        assert conn.execute("SELECT reserved_margin,admission_snapshot FROM trading_plans").fetchone() == {
            "reserved_margin": None,
            "admission_snapshot": None,
        }
        assert conn.execute("SELECT intake_context FROM trading_cases").fetchone()["intake_context"] is None
        storage = TradingRepository(conn)
        historical = storage.analysis_case(case)["assessments"][0]
        assert historical["status"] == "rate_limit" and historical["input_tokens"] is None
        legacy_run = storage.evaluation_run(historical["run_id"])
        assert legacy_run["kind"] == "legacy"
        assert legacy_run["evaluator_spec"]["generation_parameters"] == "unknown"
        action = conn.execute("SELECT action_id FROM trading_policy_actions").fetchone()
        assert action["action_id"] == decision

        spec = EvaluatorSpec(program, "new-model", "revision-1", "case_view_v2", "forecast_v1", 2000)
        run = EvaluationRun.online(spec)
        forecast = Forecast(
            LegProbabilities(Decimal(".6"), Decimal(".3"), Decimal(".1")),
            LegProbabilities(Decimal(".3"), Decimal(".6"), Decimal(".1")),
        )
        with conn.transaction():
            storage.register_evaluation_run(run, now_ms=50)
            stored = storage.record_assessment(
                case_id=case,
                run_id=run.run_id,
                status="ok",
                forecast=forecast,
                notes=(),
                usage={},
                started_at_ms=50,
                ended_at_ms=60,
            )
            assert (
                storage.record_assessment(
                    case_id=case,
                    run_id=run.run_id,
                    status="ok",
                    forecast=forecast,
                    notes=(),
                    usage={},
                    started_at_ms=50,
                    ended_at_ms=60,
                )
                == stored
            )
            other = EvaluationRun.online(replace(spec, model_name="other-model"))
            storage.register_evaluation_run(other, now_ms=50)
            storage.record_assessment(
                case_id=case,
                run_id=other.run_id,
                status="timeout",
                forecast=None,
                notes=(),
                usage={},
                started_at_ms=50,
                ended_at_ms=60,
            )
        rows = storage.analysis_case(case)["assessments"]
        assert len(rows) == 3 and {row["status"] for row in rows} == {"ok", "timeout", "rate_limit"}
        with pytest.raises(psycopg.errors.RaiseException), conn.transaction():
            conn.execute(
                "UPDATE trading_assessments SET notes=%s::jsonb WHERE assessment_id=%s",
                (json.dumps(["rewrite"]), stored),
            )
        with pytest.raises(ValueError, match="assessment_identity_conflict"), conn.transaction():
            storage.record_assessment(
                case_id=case,
                run_id=run.run_id,
                status="timeout",
                forecast=None,
                notes=(),
                usage={},
                started_at_ms=50,
                ended_at_ms=60,
            )
