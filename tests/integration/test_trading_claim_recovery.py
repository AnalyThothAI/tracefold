"""Real PostgreSQL fences protect slow inference, cancellation and output checkpoints."""

import asyncio
import time
from dataclasses import asdict
from decimal import Decimal

import pytest

from tests.integration.test_trading_analysis_closure import _selection
from tests.postgres_test_utils import connect_postgres_test, postgres_migration_test_dsn
from tracefold.app.trading_analysis import AnalysisRunner
from tracefold.app.trading_assessor import AssessmentResult
from tracefold.integrations.binance_catalogue import BinanceCatalogue
from tracefold.platform.config.models import PostgresConfig, Settings
from tracefold.trading.engine.case_view import BaseRates, build_case_view
from tracefold.trading.engine.forecast import Forecast, LegProbabilities
from tracefold.trading.engine.paper import LegGeometry
from tracefold.trading.storage.root import TradingRepository

pytestmark = pytest.mark.integration


def _now():
    return int(time.time() * 1000)


def _claim(trading, *, amplitude=350, source="first"):
    at = _now()
    payload = {
        "kind": "oi",
        "provider_event_at_ms": at,
        "source_recorded_at_ms": at,
        "oi_change_bps": amplitude,
        "measurement_definition": "contracts_change_bps_v1",
        "measurement_window_ms": 300000,
        "direction": "up",
        "source_venue": "binance",
        "ingest_mode": "prospective",
    }
    _, identity, _ = trading.accept_trigger(
        kind="oi",
        source_fact_key=source,
        source_revision="v1",
        payload_sha256="a" * 64,
        payload=payload,
        selection=_selection(),
        now_ms=at,
        root_ttl_ms=600000,
    )
    claim = trading.claim_case(now_ms=at, lease_ms=30000)
    assert claim["case_id"] == identity
    view = build_case_view(
        case_id=identity,
        asset_id="crypto:SOL",
        trigger_kind="oi",
        decided_at_ms=at,
        source_fact=payload,
        features={},
        geometry=LegGeometry(100, 200),
        half_spread_bps=Decimal(1),
        base_rates=(BaseRates("long", 0, None), BaseRates("short", 0, None)),
        native_symbol="SOLUSDT",
        units_per_contract=Decimal(1),
    )
    assert trading.freeze_case(
        case_id=identity,
        claim_token=claim["claim_token"],
        now_ms=at,
        view=asdict(view),
        raw_snapshot_ref="b" * 64,
        geometry_version=view.geometry.version,
        stop_bps=100,
        tp_bps=200,
        half_spread_bps=Decimal(1),
        reference_price=Decimal(100),
    )
    return trading.conn.execute("SELECT * FROM trading_cases WHERE case_id=%s", (identity,)).fetchone()


def _runner(tmp_path, dsn, assessor):
    settings = Settings()
    settings.set_config_dir(tmp_path)
    settings.storage.postgres = PostgresConfig(dsn=postgres_migration_test_dsn(dsn), password_file=None)
    settings.trading.analysis.publish_signals = False
    settings.trading.execution.account_slot = "demo-primary"
    settings.trading.execution.binance.environment = "DEMO"
    return AnalysisRunner(
        settings=settings, market_data=object(), assessor=assessor, program_sha="e" * 64, raw_root=tmp_path / "archive"
    )


class _Assessor:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def assess(self, _view):
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return AssessmentResult(
            Forecast(
                LegProbabilities(Decimal(".7"), Decimal(".2"), Decimal(".1")),
                LegProbabilities(Decimal(".2"), Decimal(".7"), Decimal(".1")),
            ),
            "complete",
            None,
            (),
            {"input_tokens": 12, "output_tokens": 8},
        )


def test_slow_inference_renews_and_cancelled_owner_is_reclaimed(tmp_path, postgres_clone_dsn):
    conn = connect_postgres_test(tmp_path, read_only=False, dsn=postgres_clone_dsn)
    runner = None
    try:
        trading = TradingRepository(conn)
        with conn.transaction():
            claim = _claim(trading)
            conn.execute(
                "UPDATE trading_cases SET lease_until_ms=%s WHERE case_id=%s", (_now() + 300, claim["case_id"])
            )

        async def run():
            nonlocal runner
            assessor = _Assessor()
            runner = _runner(tmp_path, postgres_clone_dsn, assessor)
            runner._lease_ms = 300
            owner = asyncio.create_task(runner.analyze_one(claim))
            await asyncio.wait_for(assessor.started.wait(), 2)
            await asyncio.sleep(0.5)  # Exceeds the original lease, but renewal keeps one owner.
            with conn.transaction():
                assert trading.claim_case(now_ms=_now(), lease_ms=300) is None
            owner.cancel()
            with pytest.raises(asyncio.CancelledError):
                await owner
            await asyncio.sleep(0.4)
            with conn.transaction():
                next_claim = trading.claim_case(now_ms=_now(), lease_ms=30000)
                assert next_claim["claim_token"] != claim["claim_token"]
                assert next_claim["claim_attempt"] == 2
            assert assessor.calls == 1

        asyncio.run(run())
        assert conn.execute("SELECT count(*) AS n FROM trading_assessments").fetchone()["n"] == 0
    finally:
        if runner:
            runner._db_pool.shutdown(wait=True)
        conn.close()


def test_stale_owner_cannot_checkpoint_or_publish(tmp_path, postgres_clone_dsn):
    conn = connect_postgres_test(tmp_path, read_only=False, dsn=postgres_clone_dsn)
    runner = None
    try:
        trading = TradingRepository(conn)
        with conn.transaction():
            claim = _claim(trading)

        async def run():
            nonlocal runner
            assessor = _Assessor()
            runner = _runner(tmp_path, postgres_clone_dsn, assessor)
            task = asyncio.create_task(runner.analyze_one(claim))
            await asyncio.wait_for(assessor.started.wait(), 2)
            with conn.transaction():
                conn.execute(
                    "UPDATE trading_cases SET lease_until_ms=%s WHERE case_id=%s", (_now() - 1, claim["case_id"])
                )
                winner = trading.claim_case(now_ms=_now(), lease_ms=30000)
            assessor.release.set()
            assert await task
            assert winner["claim_token"] != claim["claim_token"]
            assert conn.execute("SELECT count(*) AS n FROM trading_assessments").fetchone()["n"] == 0
            assert conn.execute("SELECT count(*) AS n FROM trading_signals").fetchone()["n"] == 0
            # The stale frozen claim is fenced before another provider invocation.
            assert await runner.analyze_one(claim)
            assert assessor.calls == 1

        asyncio.run(run())
    finally:
        if runner:
            runner._db_pool.shutdown(wait=True)
        conn.close()


def test_checkpoint_survives_cancel_and_real_publication_uses_action_identity(tmp_path, postgres_clone_dsn):
    conn = connect_postgres_test(tmp_path, read_only=False, dsn=postgres_clone_dsn)
    runners = []
    try:
        trading = TradingRepository(conn)
        with conn.transaction():
            claim = _claim(trading)

        async def run():
            assessor = _Assessor()
            assessor.release.set()
            first = _runner(tmp_path, postgres_clone_dsn, assessor)
            runners.append(first)

            async def crash_after_checkpoint(_case):
                raise asyncio.CancelledError()

            first._publication_reason = crash_after_checkpoint
            with pytest.raises(asyncio.CancelledError):
                await first.analyze_one(claim)
            checkpoint = trading.assessment_for_run(case_id=claim["case_id"], run_id=first.evaluation.run_id)
            assert checkpoint["input_tokens"] == 12
            with conn.transaction():
                conn.execute(
                    "UPDATE trading_cases SET lease_until_ms=%s WHERE case_id=%s", (_now() - 1, claim["case_id"])
                )
                next_claim = trading.claim_case(now_ms=_now(), lease_ms=30000)
                trading.heartbeat(account_slot="demo-primary", now_ns=time.time_ns())
            second = _runner(tmp_path, postgres_clone_dsn, assessor)
            second.settings.trading.analysis.publish_signals = True
            second.settings.trading.execution.enabled = True

            async def catalogue(_url, **_kwargs):
                return {
                    "symbols": [
                        {
                            "symbol": "SOLUSDT",
                            "baseAsset": "SOL",
                            "quoteAsset": "USDT",
                            "marginAsset": "USDT",
                            "status": "TRADING",
                            "contractType": "PERPETUAL",
                        }
                    ]
                }

            second.execution_catalogue = BinanceCatalogue(catalogue, clock_ms=_now)
            runners.append(second)
            assert await second.analyze_one(next_claim)
            assert assessor.calls == 1
            action = conn.execute("SELECT * FROM trading_policy_actions WHERE policy_id='forecast'").fetchone()
            signal = conn.execute("SELECT * FROM trading_signals").fetchone()
            assert action["publish_status"] == "published"
            assert signal["decision_id"] == action["action_id"]
            assert len(signal["payload"]["policy_version"]) == 64
            assert trading.analysis_case(claim["case_id"])["state"] == "complete"
            # The next overlapping observation remains a Case but cannot publish again.
            with conn.transaction():
                repeat = _claim(trading, source="second")
                assert repeat["episode_id"] == claim["episode_id"] and repeat["episode_role"] == "repeat"
                assert (
                    trading.publication_admission_status(
                        case_id=repeat["case_id"],
                        account_slot="demo-primary",
                        now_ms=_now(),
                        max_source_age_ms=600000,
                    )
                    == "episode_repeated"
                )

        asyncio.run(run())
    finally:
        for runner in runners:
            runner._db_pool.shutdown(wait=True)
        conn.close()
