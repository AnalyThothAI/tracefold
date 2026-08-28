"""TradeIntent persistence at the real PostgreSQL seam (#283)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from decimal import Decimal
from threading import Barrier, Event
from typing import Any

import pytest
from psycopg.errors import ForeignKeyViolation, InsufficientPrivilege, RaiseException, UniqueViolation

from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tracefold.app.repository_session import repositories_for_connection
from tracefold.trading import (
    BlacklistSnapshotV1,
    ExecutionCapabilitySnapshotV1,
    ExecutionInstrumentCapabilityV1,
    IntentOutcome,
    ReplayReceiptV1,
    TradeIntent,
    deterministic_client_order_id,
)
from tracefold.trading.candidate.eligibility import Funnel
from tracefold.trading.contracts import (
    FrozenMarketContext,
    FrozenStrategyContext,
    InstrumentRef,
    OiMarketTrigger,
    OiTradeCandidate,
    RegimeAssessment,
    TradingCaseManifest,
)
from tracefold.trading.pipeline.candidate import CandidateRunner
from tracefold.trading.pipeline.runtime import TradingConfig
from tracefold.trading.strategy.root import strategies

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_dsn")]

NOW = 1_900_000_000_000
AUTHORITY: dict[str, BlacklistSnapshotV1] = {}
CAPABILITY_SNAPSHOT = ExecutionCapabilitySnapshotV1(
    app_revision="test-revision",
    app_image_digest="test-image",
    nautilus_wheel_identity="test-wheel",
    news_universe_digest="a" * 64,
    provider_universe_digest="b" * 64,
    included={
        symbol: ExecutionInstrumentCapabilityV1(
            instrument_id=symbol,
            native_symbol=symbol.removesuffix("-PERP.BINANCE"),
            underlying_key=f"crypto:{symbol.removesuffix('USDT-PERP.BINANCE')}",
            quote_currency="USDT",
            price_precision=2,
            size_precision=3,
            price_increment="0.01",
            size_increment="0.001",
            min_quantity="0.001",
            min_notional="5",
        )
        for symbol in (
            "BTCUSDT-PERP.BINANCE",
            "DOGEUSDT-PERP.BINANCE",
            "ETHUSDT-PERP.BINANCE",
            "SOLUSDT-PERP.BINANCE",
        )
    },
    excluded={},
)


@pytest.fixture(scope="module")
def conn():
    connection = connect_postgres_test(read_only=False)
    migrate(connection)
    repos = repositories_for_connection(connection)
    connection.execute(
        "UPDATE trading_runtime_state SET nautilus_ready = true, "
        "nautilus_unexpected_exposure = false, nautilus_heartbeat_at_ms = %s WHERE id = 1",
        (NOW,),
    )
    assert repos.trading.append_and_activate_execution_capability_snapshot(
        CAPABILITY_SNAPSHOT,
        created_at_ms=NOW,
    )
    AUTHORITY["blacklist"] = repos.trading.blacklist_snapshot(now_ms=NOW, materialize_expiry=True)
    connection.commit()
    yield connection
    connection.close()


def _case(connection: Any, *, case_id: str = "case-intent-1") -> None:
    connection.execute("TRUNCATE trading_intents, trading_orders, trading_cases CASCADE")
    _reset_authority(connection)
    connection.execute(
        """
        INSERT INTO trading_cases (
          case_id, underlying_key, trigger_kind, strategy_id, strategy_version,
          strategy_config_digest, mode, primary_source_key, supplemental_source_keys,
          manifest, manifest_sha256, state, observed_at_ms, created_at_ms, updated_at_ms
        ) VALUES (%s, 'crypto:SOL', 'oi', 'oi_smart_money_momentum_v1',
                  'oi_smart_money_momentum_v1', %s, 'paper', %s, '[]'::jsonb,
                  '{}'::jsonb, %s, 'RUNNING', %s, %s, %s)
        """,
        (case_id, "0" * 64, f"source-{case_id}", "1" * 64, NOW, NOW, NOW),
    )
    connection.commit()


def _intent(
    *,
    case_id: str = "case-intent-1",
    case_manifest_sha256: str = "1" * 64,
) -> TradeIntent:
    blacklist = AUTHORITY["blacklist"]
    return TradeIntent.create(
        case_id=case_id,
        case_manifest_sha256=case_manifest_sha256,
        execution_capability_snapshot_sha256=CAPABILITY_SNAPSHOT.snapshot_sha256,
        blacklist_snapshot=blacklist,
        instrument_id="SOLUSDT-PERP.BINANCE",
        underlying_key="crypto:SOL",
        created_at_ms=NOW,
        reference_price=Decimal("60000"),
        target_notional_usd=Decimal("10"),
    )


def _allow_entry(connection: Any) -> None:
    connection.execute(
        """
        UPDATE trading_runtime_state
           SET control = 'RUNNING', nautilus_ready = true, nautilus_unexpected_exposure = false
         WHERE id = 1
        """
    )


def _reset_authority(connection: Any) -> None:
    connection.execute("DELETE FROM trading_symbol_blacklist WHERE base_symbol NOT IN ('BTC', 'ETH', 'CL')")
    connection.execute(
        """
        UPDATE trading_runtime_state
           SET control = 'PAUSED', blacklist_revision = 0,
               active_capability_snapshot_sha256 = %s,
               active_capability_included_count = %s,
               nautilus_ready = false, nautilus_unexpected_exposure = false
         WHERE id = 1
        """,
        (CAPABILITY_SNAPSHOT.snapshot_sha256, len(CAPABILITY_SNAPSHOT.included)),
    )


def test_replay_success_receipt_is_idempotent_content_bound_and_append_only(conn: Any) -> None:
    conn.execute("TRUNCATE trading_replay_runs")
    receipt = ReplayReceiptV1(
        run_id="7" * 64,
        spec_sha256="7" * 64,
        created_at_ms=NOW,
        artifact_path="/tracefold-artifacts/replay/7/replay.json",
        artifact_sha256="8" * 64,
        source_count=3,
        directional_count=1,
        terminal_outcome_count=3,
    )
    repos = repositories_for_connection(conn)

    assert repos.trading.insert_replay_receipt(receipt) is True
    assert repos.trading.insert_replay_receipt(receipt) is False
    assert ReplayReceiptV1.model_validate(repos.trading.replay_receipt(receipt.run_id)) == receipt
    conn.commit()

    with pytest.raises(RaiseException, match="trading_append_only_mutation_forbidden"):
        conn.execute("UPDATE trading_replay_runs SET source_count = 4 WHERE run_id = %s", (receipt.run_id,))
    conn.rollback()
    with pytest.raises(RaiseException, match="trading_append_only_mutation_forbidden"):
        conn.execute("DELETE FROM trading_replay_runs WHERE run_id = %s", (receipt.run_id,))
    conn.rollback()


class _RunnerDb:
    def __init__(self, connection: Any, *, fail_intent_settle: bool = False) -> None:
        self.connection = connection
        self.fail_intent_settle = fail_intent_settle

    async def tx(self, name: str, fn: Callable[[Any], Any], *, timeout_seconds: float) -> Any:
        del timeout_seconds
        repos = repositories_for_connection(self.connection)
        target: Any = repos
        if name == "trading_intent_emit" and self.fail_intent_settle:
            trading = repos.trading

            class _TradingProxy:
                def __getattr__(self, attr: str) -> Any:
                    return getattr(trading, attr)

                def settle_case(self, **_kwargs: Any) -> bool:
                    return False

            class _ReposProxy:
                conn = repos.conn
                trading = _TradingProxy()

            target = _ReposProxy()
        with self.connection.transaction():
            return fn(target)

    async def read(self, _name: str, fn: Callable[[Any], Any], *, timeout_seconds: float) -> Any:
        del timeout_seconds
        return fn(repositories_for_connection(self.connection))


def _executable_manifest(*, instrument: InstrumentRef | None = None) -> TradingCaseManifest:
    strategy = strategies()["oi_smart_money_momentum_v1"]
    selected = instrument or InstrumentRef(
        exchange_id="binance",
        venue="binance.perp",
        provider_symbol="SOLUSDT",
        base_symbol="SOL",
        instrument_class="crypto",
        quote_asset="USDT",
        observed_at_ms=NOW,
    )
    oi = OiTradeCandidate(
        event_id=f"event-{selected.base_symbol.lower()}",
        observed_at_ms=NOW,
        verdict_created_at_ms=NOW,
        base_symbol=selected.base_symbol,
        venue="binance",
        oi_direction="rise",
        oi_change_bps=1_548,
        oi_value_usd=250_000_000,
        whale_long_profit_bps=1,
        whale_oi_ratio_bps=6_000,
        rank_in_window=1,
        final_decision="push",
        source_rule="opening_move_with_whale_concentration",
        source_strategy_id="oi_5m",
        source_contract_version="oi_source_v1",
        measurement_window_ms=300_000,
        learning_epoch="program_v8",
        program_version="news_oi_signal_v1",
        program_sha256="a" * 64,
        policy_version="news_triage_policy_v10",
        editorial_origin="telemetry_deterministic",
        editorial_sha256="b" * 64,
        scored_judgment_sha256="c" * 64,
        runtime_manifest_sha="d" * 64,
        metric_version="oi_signal_v1",
    )
    return TradingCaseManifest(
        primary_trigger=OiMarketTrigger(
            source_key=oi.source_key,
            observed_at_ms=NOW,
            persisted_at_ms=NOW,
            venue="binance",
        ),
        contexts=FrozenStrategyContext(
            mode="paper",
            oi=oi,
            regime=RegimeAssessment(
                regime="buildup_up",
                reason="buildup_up",
                pre_move_bps=200,
                oi_direction="rise",
            ),
            market=FrozenMarketContext(
                mark_price=Decimal("100"),
                observed_at_ms=NOW,
                pre_move_bps=200,
                pre_move_lookback_ms=3_600_000,
            ),
        ),
        strategy_id=strategy.strategy_id,
        strategy_version=strategy.strategy_version,
        strategy_config=strategy.config_snapshot,
        strategy_config_digest=strategy.config_digest,
        underlying_key=f"crypto:{selected.base_symbol}",
        base_symbol=selected.base_symbol,
        cutoff_ms=NOW,
        instrument=selected,
    )


def _pending_executable_case(
    connection: Any,
    *,
    manifest: TradingCaseManifest | None = None,
) -> TradingCaseManifest:
    connection.execute("TRUNCATE trading_intents, trading_orders, trading_cases CASCADE")
    _reset_authority(connection)
    manifest = manifest or _executable_manifest()
    repos = repositories_for_connection(connection)
    assert repos.trading.insert_case(
        case_id="case-sol",
        underlying_key=manifest.underlying_key,
        trigger_kind=manifest.trigger_kind,
        strategy_id=manifest.strategy_id,
        strategy_version=manifest.strategy_version,
        strategy_config_digest=manifest.strategy_config_digest,
        mode="paper",
        primary_source_key=manifest.primary_trigger.source_key,
        supplemental_source_keys=(),
        manifest=manifest.model_dump(mode="json"),
        manifest_sha256=manifest.digest(),
        regime=manifest.regime.regime.value,
        observed_at_ms=NOW,
        source_observed_at_ms=NOW,
        trigger_persisted_at_ms=NOW,
        now_ms=NOW,
    )
    connection.commit()
    return manifest


def _candidate_runner(connection: Any, *, fail_intent_settle: bool = False) -> CandidateRunner:
    async def _bars(_symbol: str, _start: int, _end: int) -> tuple[()]:
        return ()

    return CandidateRunner(
        db=_RunnerDb(connection, fail_intent_settle=fail_intent_settle),
        config=TradingConfig(fixed_notional_usd=Decimal("7.5")),
        bars=lambda _venue: _bars,
        candidate_projection=lambda *_args: ((), (), ()),
        instrument_projection=lambda *_args: (),
        clock=lambda: NOW + 1_000,
    )


def _observe_close(
    repos: Any,
    intent: TradeIntent,
    *,
    position_id: str,
    avg_exit_price: Decimal,
    closed_at_ms: int,
    realized_pnl_amount: Decimal | None = None,
    realized_pnl_currency: str | None = None,
    commissions_by_currency: dict[str, str] | None = None,
) -> Any:
    observed = repos.trading.record_position_closed_observed(
        intent.intent_id,
        instrument_id=intent.instrument_id,
        account_id="BINANCE-001",
        position_id=position_id,
        closing_client_order_id=deterministic_client_order_id(intent.intent_id, "close"),
        local_quantity=Decimal(0),
        avg_exit_price=avg_exit_price,
        closed_at_ms=closed_at_ms,
        realized_pnl_amount=realized_pnl_amount,
        realized_pnl_currency=realized_pnl_currency,
        commissions_by_currency=commissions_by_currency,
        now_ms=closed_at_ms,
    )
    assert observed is not None
    assert observed.execution_state == "IN_FLIGHT"
    assert observed.execution_phase == "EXIT"
    assert observed.flat_verified_at_ms is None
    assert observed.commissions_by_currency == commissions_by_currency
    return observed


def test_candidate_runner_atomically_emits_intent_and_advances_case(conn: Any) -> None:
    manifest = _pending_executable_case(conn)

    result = asyncio.run(_candidate_runner(conn)._advance(funnel=Funnel()))

    assert result == "smart_money_momentum_long"
    row = conn.execute(
        "SELECT c.state, c.policy_decision, i.instrument_id, i.execution_state, i.target_notional_usd "
        "FROM trading_cases c JOIN trading_intents i ON i.case_id = c.case_id "
        "WHERE c.case_id = 'case-sol'"
    ).fetchone()
    assert dict(row) == {
        "state": "INTENT_EMITTED",
        "policy_decision": "long",
        "instrument_id": "SOLUSDT-PERP.BINANCE",
        "execution_state": "PENDING",
        "target_notional_usd": Decimal("7.5"),
    }
    assert conn.execute("SELECT count(*) AS n FROM trading_orders").fetchone()["n"] == 0
    stored = repositories_for_connection(conn).trading.intent_for_case(case_id="case-sol")
    assert stored is not None and stored[0].case_manifest_sha256 == manifest.digest()


def test_candidate_runner_emits_a_non_target_instrument_from_the_same_active_snapshot(conn: Any) -> None:
    doge = InstrumentRef(
        exchange_id="binance",
        venue="binance.perp",
        provider_symbol="DOGEUSDT",
        base_symbol="DOGE",
        instrument_class="crypto",
        quote_asset="USDT",
        observed_at_ms=NOW,
    )
    _pending_executable_case(conn, manifest=_executable_manifest(instrument=doge))

    assert asyncio.run(_candidate_runner(conn)._advance(funnel=Funnel())) == "smart_money_momentum_long"
    row = conn.execute(
        "SELECT instrument_id, underlying_key, intent_version FROM trading_intents WHERE case_id = 'case-sol'"
    ).fetchone()
    assert dict(row) == {
        "instrument_id": "DOGEUSDT-PERP.BINANCE",
        "underlying_key": "crypto:DOGE",
        "intent_version": "trade_intent_v2",
    }


def test_candidate_runner_rolls_back_intent_when_case_transition_fails(conn: Any) -> None:
    _pending_executable_case(conn)

    result = asyncio.run(_candidate_runner(conn, fail_intent_settle=True)._advance(funnel=Funnel()))

    assert result == "intent_admission_blocked"
    assert conn.execute("SELECT count(*) AS n FROM trading_intents").fetchone()["n"] == 0
    case = conn.execute("SELECT state, policy_reason FROM trading_cases WHERE case_id = 'case-sol'").fetchone()
    assert dict(case) == {"state": "BLOCKED", "policy_reason": "intent_admission_blocked"}


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    (
        ("exchange_id", "hyperliquid", "intent_instrument_not_allowed"),
        ("venue", "hl.perp", "intent_instrument_not_allowed"),
        ("instrument_class", "equity", "intent_instrument_not_allowed"),
        ("provider_symbol", "SOL", "intent_admission_blocked"),
        ("base_symbol", "ETH", "intent_admission_blocked"),
        ("quote_asset", "USDC", "intent_admission_blocked"),
    ),
)
def test_candidate_runner_refuses_every_nonexact_instrument_identity(
    conn: Any,
    field: str,
    value: str,
    expected: str,
) -> None:
    exact = _executable_manifest().instrument
    altered = InstrumentRef.model_validate({**exact.model_dump(), field: value})
    _pending_executable_case(conn, manifest=_executable_manifest(instrument=altered))

    result = asyncio.run(_candidate_runner(conn)._advance(funnel=Funnel()))

    assert result == expected
    assert conn.execute("SELECT count(*) AS n FROM trading_intents").fetchone()["n"] == 0
    case = conn.execute("SELECT state, policy_reason FROM trading_cases WHERE case_id = 'case-sol'").fetchone()
    assert dict(case) == {
        "state": "POLICY_REJECTED" if expected == "intent_instrument_not_allowed" else "BLOCKED",
        "policy_reason": expected,
    }


def test_capability_replacement_requires_a_fresh_zero_proof_and_no_nonterminal_intent(conn: Any) -> None:
    _case(conn)
    repos = repositories_for_connection(conn)
    replacement = CAPABILITY_SNAPSHOT.model_copy(update={"app_revision": "replacement-revision"})

    conn.execute(
        "UPDATE trading_runtime_state SET control = 'PAUSED', nautilus_ready = true, "
        "nautilus_unexpected_exposure = false, nautilus_heartbeat_at_ms = %s WHERE id = 1",
        (NOW - 15_001,),
    )
    assert not repos.trading.append_and_activate_execution_capability_snapshot(
        replacement,
        created_at_ms=NOW,
    )

    assert repos.trading.insert_intent(_intent())
    conn.execute(
        "UPDATE trading_runtime_state SET control = 'PAUSED', nautilus_ready = true, "
        "nautilus_unexpected_exposure = false, nautilus_heartbeat_at_ms = %s WHERE id = 1",
        (NOW,),
    )
    assert not repos.trading.append_and_activate_execution_capability_snapshot(
        replacement,
        created_at_ms=NOW,
    )
    conn.execute("DELETE FROM trading_intents")

    conn.execute(
        "UPDATE trading_runtime_state SET control = 'PAUSED', nautilus_ready = true, "
        "nautilus_unexpected_exposure = false, nautilus_heartbeat_at_ms = %s WHERE id = 1",
        (NOW,),
    )
    assert (
        conn.execute(
            "SELECT count(*) AS n FROM trading_intents WHERE execution_state IN "
            "('PENDING', 'IN_FLIGHT', 'OPEN_PROTECTED', 'MANUAL_REVIEW')"
        ).fetchone()["n"]
        == 0
    )
    assert repos.trading.append_and_activate_execution_capability_snapshot(
        replacement,
        created_at_ms=NOW,
    )
    runtime = repos.trading.runtime_state()
    assert runtime is not None
    assert runtime["active_capability_snapshot_sha256"] == replacement.snapshot_sha256
    assert runtime["nautilus_ready"] is False
    assert runtime["nautilus_readiness_reason"] == "capability_snapshot_changed"
    _reset_authority(conn)


def test_initial_capability_activation_also_requires_a_fresh_zero_proof(conn: Any) -> None:
    repos = repositories_for_connection(conn)
    initial = CAPABILITY_SNAPSHOT.model_copy(update={"app_revision": "initial-revision"})
    conn.execute(
        "UPDATE trading_runtime_state SET control = 'PAUSED', active_capability_snapshot_sha256 = NULL, "
        "active_capability_included_count = 0, nautilus_ready = false, "
        "nautilus_unexpected_exposure = false, nautilus_heartbeat_at_ms = NULL WHERE id = 1"
    )

    assert not repos.trading.append_and_activate_execution_capability_snapshot(initial, created_at_ms=NOW)
    assert repos.trading.runtime_state()["active_capability_snapshot_sha256"] is None

    conn.execute(
        "UPDATE trading_runtime_state SET nautilus_ready = true, nautilus_heartbeat_at_ms = %s WHERE id = 1",
        (NOW,),
    )
    assert repos.trading.append_and_activate_execution_capability_snapshot(initial, created_at_ms=NOW)
    assert repos.trading.runtime_state()["active_capability_snapshot_sha256"] == initial.snapshot_sha256
    _reset_authority(conn)


def test_capability_activation_cannot_be_overwritten_by_old_engine_readiness(conn: Any) -> None:
    repos = repositories_for_connection(conn)
    replacement = CAPABILITY_SNAPSHOT.model_copy(update={"app_revision": "serialized-replacement"})
    conn.execute(
        "UPDATE trading_runtime_state SET control = 'PAUSED', nautilus_ready = true, "
        "nautilus_unexpected_exposure = false, nautilus_heartbeat_at_ms = %s WHERE id = 1",
        (NOW,),
    )
    started = Event()

    def activate() -> bool:
        contender = connect_postgres_test(read_only=False)
        try:
            contender_repos = repositories_for_connection(contender)
            with contender.transaction():
                started.set()
                return contender_repos.trading.append_and_activate_execution_capability_snapshot(
                    replacement,
                    created_at_ms=NOW,
                )
        finally:
            contender.close()

    pool = ThreadPoolExecutor(max_workers=1)
    try:
        with conn.transaction():
            runtime = repos.trading.nautilus_runtime_state(for_update=True)
            assert runtime is not None
            future = pool.submit(activate)
            assert started.wait(timeout=5)
            with pytest.raises(FutureTimeoutError):
                future.result(timeout=0.5)
            repos.trading.set_nautilus_runtime(
                heartbeat_at_ms=NOW,
                ready=True,
                readiness_reason="ready",
                unexpected_exposure=False,
                now_ms=NOW,
            )
        assert future.result(timeout=5) is True
    finally:
        pool.shutdown(wait=True)

    runtime = repos.trading.runtime_state()
    assert runtime is not None
    assert runtime["active_capability_snapshot_sha256"] == replacement.snapshot_sha256
    assert runtime["nautilus_ready"] is False
    assert runtime["nautilus_readiness_reason"] == "capability_snapshot_changed"
    _reset_authority(conn)


def test_trade_intent_round_trips_as_one_immutable_material_fact(conn: Any) -> None:
    _case(conn)
    intent = _intent()
    repos = repositories_for_connection(conn)

    assert repos.trading.insert_intent(intent) is True
    conn.commit()

    stored = repos.trading.intent(intent.intent_id)
    assert stored == intent
    assert len(intent.intent_id) == 64


def test_intent_projection_schema_has_no_duplicate_timestamp_or_poll_index(conn: Any) -> None:
    columns = {
        row[0]
        for row in conn.execute(
            """
            SELECT column_name
              FROM information_schema.columns
             WHERE table_schema = 'public'
               AND table_name = 'trading_intents'
            """
        ).fetchall()
    }
    indexes = {
        row[0]
        for row in conn.execute(
            """
            SELECT indexname
              FROM pg_indexes
             WHERE schemaname = 'public'
               AND tablename = 'trading_intents'
            """
        ).fetchall()
    }

    assert "engine_seen_at_ms" not in columns
    assert "ix_trading_intents_poll" not in indexes


def test_intent_case_manifest_identity_must_match_the_referenced_case(conn: Any) -> None:
    _case(conn)
    repos = repositories_for_connection(conn)

    with pytest.raises(ForeignKeyViolation):
        repos.trading.insert_intent(_intent(case_manifest_sha256="3" * 64))
    conn.rollback()


def test_entry_fence_is_the_single_durable_permission_for_an_exposure_increase(conn: Any) -> None:
    _case(conn)
    intent = _intent()
    repos = repositories_for_connection(conn)
    assert repos.trading.insert_intent(intent) is True
    conn.execute("UPDATE trading_runtime_state SET control = 'PAUSED', nautilus_ready = false WHERE id = 1")
    conn.commit()

    assert repos.trading.fence_entry(intent.intent_id, engine_identity="nt-1", now_ms=NOW + 1_000) is None
    conn.commit()

    conn.execute("UPDATE trading_runtime_state SET control = 'RUNNING' WHERE id = 1")
    conn.commit()
    assert repos.trading.fence_entry(intent.intent_id, engine_identity="nt-1", now_ms=NOW + 1_500) is None
    conn.commit()

    _allow_entry(conn)
    conn.commit()
    fenced = repos.trading.fence_entry(intent.intent_id, engine_identity="nt-1", now_ms=NOW + 2_000)
    conn.commit()

    assert fenced is not None
    assert fenced.execution_state == "IN_FLIGHT"
    assert fenced.execution_phase == "ENTRY"
    assert fenced.entry_client_order_id == deterministic_client_order_id(intent.intent_id, "entry")
    assert fenced.entry_fenced_at_ms == NOW + 2_000

    # A duplicate scan or restart can read this projection, but cannot acquire a second submit fence.
    assert repos.trading.fence_entry(intent.intent_id, engine_identity="nt-1", now_ms=NOW + 3_000) is None
    conn.commit()
    recovered = repos.trading.intent_outcome(intent.intent_id)
    assert recovered == fenced


def test_blacklist_change_after_emission_is_rechecked_and_frozen_at_the_entry_fence(conn: Any) -> None:
    _case(conn)
    intent = _intent()
    repos = repositories_for_connection(conn)
    assert repos.trading.insert_intent(intent) is True
    repos.trading.blacklist_upsert(
        base_symbol="SOL",
        reason="operator",
        expires_at_ms=None,
        now_ms=NOW + 500,
    )
    _allow_entry(conn)
    conn.commit()

    refused = repos.trading.fence_entry(intent.intent_id, engine_identity="nt-1", now_ms=NOW + 1_000)
    conn.commit()

    assert refused is not None
    assert (refused.execution_state, refused.terminal_outcome, refused.reason_code) == (
        "TERMINAL",
        "REJECTED",
        "blacklisted",
    )
    evidence = conn.execute(
        "SELECT blacklist_revision_at_emission, blacklist_revision_at_fence, "
        "blacklist_snapshot_sha256_at_fence, blacklist_snapshot_payload_at_fence "
        "FROM trading_intents WHERE intent_id = %s",
        (intent.intent_id,),
    ).fetchone()
    assert evidence["blacklist_revision_at_emission"] == 0
    assert evidence["blacklist_revision_at_fence"] == 1
    assert len(evidence["blacklist_snapshot_sha256_at_fence"]) == 64
    assert any(
        row["underlying_key"] == "crypto:SOL" for row in evidence["blacklist_snapshot_payload_at_fence"]["active_rows"]
    )

    assert repos.trading.blacklist_delete(base_symbol="SOL", now_ms=NOW + 2_000) == 1
    conn.commit()


def test_two_database_transactions_competing_for_one_entry_fence_have_one_winner(conn: Any) -> None:
    _case(conn)
    intent = _intent()
    repos = repositories_for_connection(conn)
    assert repos.trading.insert_intent(intent) is True
    _allow_entry(conn)
    conn.commit()
    barrier = Barrier(2)

    def compete(engine_identity: str) -> IntentOutcome | None:
        contender = connect_postgres_test(read_only=False)
        try:
            contender_repos = repositories_for_connection(contender)
            barrier.wait(timeout=5)
            result = contender_repos.trading.fence_entry(
                intent.intent_id,
                engine_identity=engine_identity,
                now_ms=NOW + 2_000,
            )
            contender.commit()
            return result
        finally:
            contender.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(compete, ("nt-a", "nt-b")))

    assert sum(result is not None for result in results) == 1
    stored = repos.trading.intent_outcome(intent.intent_id)
    assert stored is not None
    assert stored.execution_state == "IN_FLIGHT"
    assert stored.engine_identity in {"nt-a", "nt-b"}


def test_entry_fence_enforces_the_code_owned_one_entry_per_utc_day(conn: Any) -> None:
    _case(conn)
    repos = repositories_for_connection(conn)
    first = _intent()
    assert repos.trading.insert_intent(first) is True
    _allow_entry(conn)
    assert repos.trading.fence_entry(first.intent_id, engine_identity="nt-1", now_ms=NOW + 1_000)
    assert repos.trading.record_entry_fill(
        first.intent_id,
        actual_quantity=Decimal("0.0001"),
        avg_entry_price=Decimal("60000"),
        position_id="position-1",
        opened_at_ms=NOW + 2_000,
        now_ms=NOW + 2_000,
    )
    assert repos.trading.record_close_submitted(
        first.intent_id,
        client_order_id=deterministic_client_order_id(first.intent_id, "close"),
        position_id="position-1",
        quantity=Decimal("0.0001"),
        submitted_at_ms=NOW + 3_000,
        now_ms=NOW + 3_000,
    )
    _observe_close(
        repos,
        first,
        position_id="position-1",
        avg_exit_price=Decimal("60001"),
        closed_at_ms=NOW + 4_000,
    )
    assert repos.trading.record_closed_flat(
        first.intent_id,
        position_id="position-1",
        authoritative_quantity=Decimal("0"),
        avg_exit_price=Decimal("60001"),
        closed_at_ms=NOW + 4_000,
        flat_verified_at_ms=NOW + 4_000,
        realized_pnl_amount=None,
        realized_pnl_currency=None,
        commissions_by_currency={},
        now_ms=NOW + 4_000,
    )
    _case_without_reset(conn, case_id="case-intent-2")
    second = _intent(case_id="case-intent-2", case_manifest_sha256="3" * 64)
    assert repos.trading.insert_intent(second) is True
    conn.commit()

    assert repos.trading.fence_entry(second.intent_id, engine_identity="nt-1", now_ms=NOW + 5_000) is None
    conn.commit()
    with pytest.raises(UniqueViolation):
        conn.execute(
            """
            UPDATE trading_intents
               SET execution_state = 'IN_FLIGHT', execution_phase = 'ENTRY',
                   entry_client_order_id = 'tf-e-database-backstop', entry_fenced_at_ms = %s
             WHERE intent_id = %s
            """,
            (NOW + 5_000, second.intent_id),
        )
    conn.rollback()


def test_database_allows_only_one_nonterminal_intent_globally(conn: Any) -> None:
    _case(conn, case_id="case-intent-1")
    first = _intent()
    repos = repositories_for_connection(conn)
    assert repos.trading.insert_intent(first) is True
    conn.commit()

    _case_without_reset(conn, case_id="case-intent-2")
    second = _intent(case_id="case-intent-2", case_manifest_sha256="3" * 64)
    with pytest.raises(UniqueViolation):
        repos.trading.insert_intent(second)
    conn.rollback()


def test_runtime_roles_enforce_the_intent_column_ownership_boundary(conn: Any) -> None:
    privileges = dict(
        conn.execute(
            """
            SELECT
              has_table_privilege('tracefold_workers', 'trading_intents', 'SELECT') AS workers_select,
              has_table_privilege('tracefold_workers', 'trading_intents', 'INSERT') AS workers_table_insert,
              has_column_privilege('tracefold_workers', 'trading_intents', 'case_id', 'INSERT')
                AS workers_intent_insert,
              has_column_privilege('tracefold_workers', 'trading_intents', 'execution_state', 'INSERT')
                AS workers_execution_insert,
              has_table_privilege('tracefold_workers', 'trading_intents', 'UPDATE') AS workers_update,
              has_column_privilege('tracefold_workers', 'trading_intents', 'execution_state', 'UPDATE')
                AS workers_execution_update,
              has_table_privilege('tracefold_nautilus', 'trading_intents', 'SELECT') AS nautilus_select,
              has_table_privilege('tracefold_nautilus', 'trading_intents', 'INSERT') AS nautilus_insert,
              has_table_privilege('tracefold_nautilus', 'trading_intents', 'UPDATE') AS nautilus_table_update,
              has_column_privilege('tracefold_nautilus', 'trading_intents', 'execution_state', 'UPDATE')
                AS nautilus_execution_update,
              has_column_privilege('tracefold_nautilus', 'trading_intents', 'case_id', 'UPDATE')
                AS nautilus_identity_update,
              has_table_privilege('tracefold_nautilus', 'trading_cases', 'UPDATE') AS nautilus_case_update,
              has_column_privilege('tracefold_nautilus', 'trading_runtime_state', 'id', 'SELECT')
                AS nautilus_runtime_id_select,
              has_column_privilege('tracefold_nautilus', 'trading_runtime_state', 'control', 'SELECT')
                AS nautilus_control_select,
              has_column_privilege('tracefold_nautilus', 'trading_runtime_state', 'orders_today', 'SELECT')
                AS nautilus_counter_select,
              has_table_privilege('tracefold_serve', 'trading_intents', 'SELECT') AS serve_select,
              has_table_privilege('tracefold_serve', 'trading_intents', 'INSERT') AS serve_insert
            """
        ).fetchone()
    )

    assert privileges == {
        "workers_select": True,
        "workers_table_insert": False,
        "workers_intent_insert": True,
        "workers_execution_insert": False,
        "workers_update": False,
        "workers_execution_update": False,
        "nautilus_select": True,
        "nautilus_insert": False,
        "nautilus_table_update": False,
        "nautilus_execution_update": True,
        "nautilus_identity_update": False,
        "nautilus_case_update": False,
        "nautilus_runtime_id_select": True,
        "nautilus_control_select": True,
        "nautilus_counter_select": False,
        "serve_select": True,
        "serve_insert": False,
    }


def test_real_nautilus_role_can_poll_fence_and_heartbeat_but_not_read_trading_counters(conn: Any) -> None:
    _case(conn)
    repos = repositories_for_connection(conn)
    intent = _intent()
    assert repos.trading.insert_intent(intent) is True
    _allow_entry(conn)
    conn.commit()

    conn.execute("SET ROLE tracefold_nautilus")
    active = repos.trading.active_intent()
    assert active is not None and active[0] == intent
    assert repos.trading.fence_entry(intent.intent_id, engine_identity="nt-1", now_ms=NOW + 1_000)
    repos.trading.set_nautilus_runtime(
        heartbeat_at_ms=NOW + 1_000,
        ready=True,
        readiness_reason="ready",
        unexpected_exposure=False,
        now_ms=NOW + 1_000,
    )
    assert repos.trading.nautilus_runtime_state(for_update=True) is not None
    conn.commit()

    with pytest.raises(InsufficientPrivilege):
        repos.trading.runtime_state()
    conn.rollback()
    conn.execute("RESET ROLE")
    conn.commit()


def test_unfenced_expiry_is_terminal_and_releases_the_single_active_slot(conn: Any) -> None:
    _case(conn, case_id="case-intent-1")
    repos = repositories_for_connection(conn)
    intent = _intent()
    assert repos.trading.insert_intent(intent) is True
    conn.commit()

    expired = repos.trading.expire_unfenced_intent(intent.intent_id, now_ms=intent.valid_until_ms)
    conn.commit()

    assert expired is not None
    assert (expired.execution_state, expired.terminal_outcome, expired.reason_code) == (
        "TERMINAL",
        "EXPIRED",
        "intent_expired",
    )
    assert expired.entry_fenced_at_ms is None

    _case_without_reset(conn, case_id="case-intent-2")
    assert repos.trading.insert_intent(_intent(case_id="case-intent-2", case_manifest_sha256="3" * 64)) is True
    conn.commit()


def test_authoritative_refusal_before_submit_is_rejected_without_exposure(conn: Any) -> None:
    _case(conn)
    repos = repositories_for_connection(conn)
    intent = _intent()
    assert repos.trading.insert_intent(intent) is True
    conn.commit()

    rejected = repos.trading.record_rejected_without_exposure(
        intent.intent_id,
        reason_code="risk_denied",
        authoritative_quantity=Decimal("0"),
        entry_client_order_id=None,
        now_ms=NOW + 1_000,
    )
    conn.commit()

    assert rejected is not None
    assert (rejected.execution_state, rejected.terminal_outcome, rejected.reason_code) == (
        "TERMINAL",
        "REJECTED",
        "risk_denied",
    )
    assert rejected.entry_fenced_at_ms is None


def test_fenced_authoritative_rejection_requires_the_exact_entry_and_zero_exposure(conn: Any) -> None:
    _case(conn)
    repos = repositories_for_connection(conn)
    intent = _intent()
    assert repos.trading.insert_intent(intent) is True
    _allow_entry(conn)
    fenced = repos.trading.fence_entry(intent.intent_id, engine_identity="nt-1", now_ms=NOW + 1_000)
    conn.commit()
    assert fenced is not None and fenced.entry_client_order_id is not None

    assert (
        repos.trading.record_rejected_without_exposure(
            intent.intent_id,
            reason_code="risk_denied",
            authoritative_quantity=Decimal("0.0001"),
            entry_client_order_id=fenced.entry_client_order_id,
            now_ms=NOW + 2_000,
        )
        is None
    )
    rejected = repos.trading.record_rejected_without_exposure(
        intent.intent_id,
        reason_code="risk_denied",
        authoritative_quantity=Decimal("0"),
        entry_client_order_id=fenced.entry_client_order_id,
        now_ms=NOW + 3_000,
    )
    conn.commit()

    assert rejected is not None
    assert (rejected.execution_state, rejected.terminal_outcome) == ("TERMINAL", "REJECTED")
    assert rejected.flat_verified_at_ms == NOW + 3_000


def test_unknown_entry_outcome_becomes_manual_review_and_never_a_rejection(conn: Any) -> None:
    _case(conn)
    repos = repositories_for_connection(conn)
    intent = _intent()
    assert repos.trading.insert_intent(intent) is True
    _allow_entry(conn)
    conn.commit()
    assert repos.trading.fence_entry(intent.intent_id, engine_identity="nt-1", now_ms=NOW + 1_000)
    conn.commit()

    review = repos.trading.mark_manual_review(
        intent.intent_id,
        reason_code="entry_outcome_unknown",
        now_ms=NOW + 2_000,
    )
    conn.commit()

    assert review is not None
    assert review.execution_state == "MANUAL_REVIEW"
    assert review.terminal_outcome is None
    assert review.reason_code == "entry_outcome_unknown"


def test_manual_review_only_follows_a_fence_and_authoritative_facts_resume_automation(conn: Any) -> None:
    _case(conn)
    repos = repositories_for_connection(conn)
    intent = _intent()
    assert repos.trading.insert_intent(intent) is True
    assert (
        repos.trading.mark_manual_review(
            intent.intent_id,
            reason_code="entry_outcome_unknown",
            now_ms=NOW + 500,
        )
        is None
    )
    _allow_entry(conn)
    assert repos.trading.fence_entry(intent.intent_id, engine_identity="nt-1", now_ms=NOW + 1_000)
    assert repos.trading.mark_manual_review(
        intent.intent_id,
        reason_code="entry_outcome_unknown",
        now_ms=NOW + 1_100,
    )
    conn.commit()

    recovered_fill = repos.trading.record_entry_fill(
        intent.intent_id,
        actual_quantity=Decimal("0.0001"),
        avg_entry_price=Decimal("60000"),
        position_id="position-1",
        opened_at_ms=NOW + 1_200,
        now_ms=NOW + 1_300,
    )

    assert recovered_fill is not None
    assert (recovered_fill.execution_state, recovered_fill.execution_phase) == ("IN_FLIGHT", "PROTECTION")
    assert recovered_fill.reason_code is None


def test_manual_protection_and_close_unknowns_converge_only_from_authoritative_facts(conn: Any) -> None:
    _case(conn)
    repos = repositories_for_connection(conn)
    intent = _intent()
    assert repos.trading.insert_intent(intent) is True
    _allow_entry(conn)
    assert repos.trading.fence_entry(intent.intent_id, engine_identity="nt-1", now_ms=NOW + 1_000)
    assert repos.trading.record_entry_fill(
        intent.intent_id,
        actual_quantity=Decimal("0.0001"),
        avg_entry_price=Decimal("60000"),
        position_id="position-1",
        opened_at_ms=NOW + 1_100,
        now_ms=NOW + 1_100,
    )
    stop_id = deterministic_client_order_id(intent.intent_id, "stop")
    stop = repos.trading.record_stop_submitted(
        intent.intent_id,
        client_order_id=stop_id,
        generation=0,
        previous_client_order_id=None,
        quantity=Decimal("0.0001"),
        now_ms=NOW + 1_200,
    )
    assert stop is not None and stop.stop_client_order_id is not None
    assert repos.trading.mark_manual_review(
        intent.intent_id,
        reason_code="protection_unproven",
        now_ms=NOW + 1_300,
    )
    recovered_protection = repos.trading.record_protected(
        intent.intent_id,
        accepted_client_order_id=stop.stop_client_order_id,
        protection_order_id="venue-stop-1",
        protected_quantity=Decimal("0.0001"),
        stop_price=Decimal("58800"),
        protected_at_ms=NOW + 1_400,
        now_ms=NOW + 1_400,
    )
    assert recovered_protection is not None
    assert recovered_protection.execution_state == "OPEN_PROTECTED"
    assert recovered_protection.reason_code is None

    assert repos.trading.record_close_submitted(
        intent.intent_id,
        client_order_id=deterministic_client_order_id(intent.intent_id, "close"),
        position_id="position-1",
        quantity=Decimal("0.0001"),
        submitted_at_ms=NOW + 2_000,
        now_ms=NOW + 2_000,
    )
    assert repos.trading.mark_manual_review(
        intent.intent_id,
        reason_code="close_outcome_unknown",
        now_ms=NOW + 2_100,
    )
    _observe_close(
        repos,
        intent,
        position_id="position-1",
        avg_exit_price=Decimal("60010"),
        closed_at_ms=NOW + 2_200,
    )
    closed = repos.trading.record_closed_flat(
        intent.intent_id,
        position_id="position-1",
        authoritative_quantity=Decimal("0"),
        avg_exit_price=Decimal("60010"),
        closed_at_ms=NOW + 2_200,
        flat_verified_at_ms=NOW + 2_300,
        realized_pnl_amount=None,
        realized_pnl_currency=None,
        commissions_by_currency={},
        now_ms=NOW + 2_300,
    )
    conn.commit()
    assert closed is not None
    assert (closed.execution_state, closed.terminal_outcome, closed.reason_code) == (
        "TERMINAL",
        "CLOSED_FLAT",
        None,
    )


def test_nautilus_poll_and_runtime_heartbeat_share_the_business_row(conn: Any) -> None:
    _case(conn)
    repos = repositories_for_connection(conn)
    intent = _intent()
    assert repos.trading.insert_intent(intent) is True
    repos.trading.set_nautilus_runtime(
        heartbeat_at_ms=NOW + 500,
        ready=True,
        readiness_reason="ready",
        unexpected_exposure=False,
        now_ms=NOW + 500,
    )
    conn.commit()

    active = repos.trading.active_intent()
    runtime = repos.trading.runtime_state()

    assert active is not None
    assert active[0] == intent
    assert active[1].execution_state == "PENDING"
    assert runtime is not None
    assert runtime["nautilus_heartbeat_at_ms"] == NOW + 500
    assert runtime["nautilus_ready"] is True
    assert runtime["nautilus_readiness_reason"] == "ready"
    assert runtime["nautilus_unexpected_exposure"] is False


def test_event_projection_selects_the_complete_intent(conn: Any) -> None:
    _case(conn)
    repos = repositories_for_connection(conn)
    intent = _intent()
    assert repos.trading.insert_intent(intent) is True
    conn.commit()

    row = repos.trading.console_case_for_source_key(primary_source_key="source-case-intent-1")

    assert row is not None
    assert row["valid_until_ms"] == intent.valid_until_ms
    assert row["created_at_ms"] == intent.created_at_ms
    assert isinstance(row["updated_at_ms"], int)
    assert row["case_state"] == "RUNNING"


def _case_without_reset(connection: Any, *, case_id: str) -> None:
    connection.execute("UPDATE trading_cases SET state = 'ORDER_PREPARED' WHERE state = 'RUNNING'")
    connection.execute(
        """
        INSERT INTO trading_cases (
          case_id, underlying_key, trigger_kind, strategy_id, strategy_version,
          strategy_config_digest, mode, primary_source_key, supplemental_source_keys,
          manifest, manifest_sha256, state, observed_at_ms, created_at_ms, updated_at_ms
        ) VALUES (%s, 'crypto:BTC', 'oi', 'oi_smart_money_momentum_v1',
                  'oi_smart_money_momentum_v1', %s, 'paper', %s, '[]'::jsonb,
                  '{}'::jsonb, %s, 'RUNNING', %s, %s, %s)
        """,
        (case_id, "0" * 64, f"source-{case_id}", "3" * 64, NOW, NOW, NOW),
    )
    connection.commit()


def test_fake_execution_closes_the_five_state_demo_loop_without_a_second_entry(conn: Any) -> None:
    _case(conn)
    intent = _intent()
    repos = repositories_for_connection(conn)
    assert repos.trading.insert_intent(intent) is True
    _allow_entry(conn)
    conn.commit()

    assert repos.trading.fence_entry(intent.intent_id, engine_identity="nt-1", now_ms=NOW + 1_000)
    conn.commit()
    protection = repos.trading.record_entry_fill(
        intent.intent_id,
        actual_quantity=Decimal("100"),
        avg_entry_price=Decimal("0.1"),
        position_id="position-1",
        opened_at_ms=NOW + 1_100,
        now_ms=NOW + 1_200,
    )
    assert protection is not None
    assert (protection.execution_state, protection.execution_phase) == ("IN_FLIGHT", "PROTECTION")

    submitted_stop = repos.trading.record_stop_submitted(
        intent.intent_id,
        client_order_id=deterministic_client_order_id(intent.intent_id, "stop"),
        generation=0,
        previous_client_order_id=None,
        quantity=Decimal("100"),
        now_ms=NOW + 1_300,
    )
    assert submitted_stop is not None
    assert submitted_stop.stop_client_order_id == deterministic_client_order_id(intent.intent_id, "stop")
    protected = repos.trading.record_protected(
        intent.intent_id,
        accepted_client_order_id=submitted_stop.stop_client_order_id,
        protection_order_id="stop-venue-1",
        protected_quantity=Decimal("100"),
        stop_price=Decimal("0.098"),
        protected_at_ms=NOW + 1_400,
        now_ms=NOW + 1_400,
    )
    conn.commit()
    assert protected is not None
    assert (protected.execution_state, protected.execution_phase) == ("OPEN_PROTECTED", "PROTECTION")

    close_id = deterministic_client_order_id(intent.intent_id, "close")
    with pytest.raises(ValueError, match="close_identity_invalid"):
        repos.trading.record_close_submitted(
            intent.intent_id,
            client_order_id="tf-c-wrong",
            position_id="position-1",
            quantity=Decimal("100"),
            submitted_at_ms=NOW + 2_000,
            now_ms=NOW + 2_000,
        )
    assert (
        repos.trading.record_close_submitted(
            intent.intent_id,
            client_order_id=close_id,
            position_id="wrong-position",
            quantity=Decimal("100"),
            submitted_at_ms=NOW + 2_000,
            now_ms=NOW + 2_000,
        )
        is None
    )
    exiting = repos.trading.record_close_submitted(
        intent.intent_id,
        client_order_id=close_id,
        position_id="position-1",
        quantity=Decimal("100"),
        submitted_at_ms=NOW + 2_000,
        now_ms=NOW + 2_000,
    )
    assert exiting is not None
    assert exiting.close_client_order_id == deterministic_client_order_id(intent.intent_id, "close")
    _observe_close(
        repos,
        intent,
        position_id="position-1",
        avg_exit_price=Decimal("0.101"),
        closed_at_ms=NOW + 2_100,
        realized_pnl_amount=Decimal("0.10"),
        realized_pnl_currency="USDT",
        commissions_by_currency={"USDT": "0.02"},
    )
    recovered_unknown = repos.trading.record_position_closed_observed(
        intent.intent_id,
        instrument_id=intent.instrument_id,
        account_id="BINANCE-001",
        position_id="position-1",
        closing_client_order_id=close_id,
        local_quantity=Decimal(0),
        avg_exit_price=Decimal("0.101"),
        closed_at_ms=NOW + 2_100,
        realized_pnl_amount=Decimal("0.10"),
        realized_pnl_currency="USDT",
        commissions_by_currency=None,
        now_ms=NOW + 2_150,
    )
    assert recovered_unknown is not None
    assert recovered_unknown.commissions_by_currency == {"USDT": "0.02"}
    unverified_latency = repos.trading.stage_latency_ms(since_ms=NOW - 1)
    assert unverified_latency["position_opened_to_closed_flat"] == {"n": 0}
    assert (
        repos.trading.record_closed_flat(
            intent.intent_id,
            position_id="position-1",
            authoritative_quantity=Decimal("0.0001"),
            avg_exit_price=Decimal("0.101"),
            closed_at_ms=NOW + 2_100,
            flat_verified_at_ms=NOW + 2_200,
            realized_pnl_amount=Decimal("0.10"),
            realized_pnl_currency="USDT",
            commissions_by_currency=None,
            now_ms=NOW + 2_200,
        )
        is None
    )
    closed = repos.trading.record_closed_flat(
        intent.intent_id,
        position_id="position-1",
        authoritative_quantity=Decimal("0"),
        avg_exit_price=Decimal("0.101"),
        closed_at_ms=NOW + 2_100,
        flat_verified_at_ms=NOW + 2_200,
        realized_pnl_amount=Decimal("0.10"),
        realized_pnl_currency="USDT",
        commissions_by_currency=None,
        now_ms=NOW + 2_200,
    )
    conn.commit()

    assert closed is not None
    assert (closed.execution_state, closed.execution_phase, closed.terminal_outcome) == (
        "TERMINAL",
        "EXIT",
        "CLOSED_FLAT",
    )
    assert closed.flat_verified_at_ms == NOW + 2_200
    assert closed.commissions_by_currency == {"USDT": "0.02"}
    verified_latency = repos.trading.stage_latency_ms(since_ms=NOW - 1)
    assert verified_latency["position_opened_to_closed_flat"] == {"n": 1, "p50": 1_100, "p95": 1_100}
    assert repos.trading.fence_entry(intent.intent_id, engine_identity="nt-1", now_ms=NOW + 3_000) is None


def test_position_change_uses_a_new_deterministic_stop_generation(conn: Any) -> None:
    _case(conn)
    intent = _intent()
    repos = repositories_for_connection(conn)
    assert repos.trading.insert_intent(intent) is True
    _allow_entry(conn)
    conn.commit()
    assert repos.trading.fence_entry(intent.intent_id, engine_identity="nt-1", now_ms=NOW + 1_000)
    assert repos.trading.record_entry_fill(
        intent.intent_id,
        actual_quantity=Decimal("100"),
        avg_entry_price=Decimal("0.1"),
        position_id="position-1",
        opened_at_ms=NOW + 1_100,
        now_ms=NOW + 1_100,
    )
    first = repos.trading.record_stop_submitted(
        intent.intent_id,
        client_order_id=deterministic_client_order_id(intent.intent_id, "stop"),
        generation=0,
        previous_client_order_id=None,
        quantity=Decimal("100"),
        now_ms=NOW + 1_200,
    )
    assert first is not None
    assert first.stop_generation == 0
    assert first.stop_client_order_id is not None
    assert len(first.stop_client_order_id) <= 36
    assert repos.trading.record_protected(
        intent.intent_id,
        accepted_client_order_id=first.stop_client_order_id,
        protection_order_id="stop-venue-1",
        protected_quantity=Decimal("100"),
        stop_price=Decimal("0.098"),
        protected_at_ms=NOW + 1_300,
        now_ms=NOW + 1_300,
    )
    conn.commit()

    repriced = repos.trading.record_position_changed(
        intent.intent_id,
        position_id="position-1",
        actual_quantity=Decimal("100"),
        avg_entry_price=Decimal("0.101"),
        now_ms=NOW + 1_350,
    )
    assert repriced is not None
    assert repriced.avg_entry_price == Decimal("0.101")

    changed = repos.trading.record_position_changed(
        intent.intent_id,
        position_id="position-1",
        actual_quantity=Decimal("80"),
        avg_entry_price=Decimal("0.101"),
        now_ms=NOW + 1_400,
    )
    assert changed is not None
    assert (changed.execution_state, changed.execution_phase) == ("IN_FLIGHT", "PROTECTION")
    assert changed.protected_quantity == Decimal("100")
    assert changed.avg_entry_price == Decimal("0.101")
    latest = repos.trading.record_position_changed(
        intent.intent_id,
        position_id="position-1",
        actual_quantity=Decimal("60"),
        avg_entry_price=Decimal("0.102"),
        now_ms=NOW + 1_450,
    )
    assert latest is not None
    assert latest.actual_quantity == Decimal("60")
    assert latest.avg_entry_price == Decimal("0.102")

    replacement = repos.trading.prepare_stop_replacement(
        intent.intent_id,
        canceled_client_order_id=first.stop_client_order_id,
        submitted_client_order_id=deterministic_client_order_id(
            intent.intent_id,
            "stop",
            previous_client_order_id=first.stop_client_order_id,
        ),
        generation=1,
        quantity=Decimal("60"),
        now_ms=NOW + 1_500,
    )
    assert replacement is not None
    assert replacement.stop_generation == 1
    assert replacement.stop_client_order_id != first.stop_client_order_id
    assert len(replacement.stop_client_order_id or "") <= 36
    assert (
        repos.trading.record_protected(
            intent.intent_id,
            accepted_client_order_id=first.stop_client_order_id,
            protection_order_id="stale-stop-venue",
            protected_quantity=Decimal("60"),
            stop_price=Decimal("0.098"),
            protected_at_ms=NOW + 1_550,
            now_ms=NOW + 1_550,
        )
        is None
    )
    protected = repos.trading.record_protected(
        intent.intent_id,
        accepted_client_order_id=replacement.stop_client_order_id,
        protection_order_id="stop-venue-2",
        protected_quantity=Decimal("60"),
        stop_price=Decimal("0.098"),
        protected_at_ms=NOW + 1_600,
        now_ms=NOW + 1_600,
    )
    conn.commit()

    assert protected is not None
    assert protected.execution_state == "OPEN_PROTECTED"
    assert protected.protected_quantity == protected.actual_quantity == Decimal("60")

    assert repos.trading.record_close_submitted(
        intent.intent_id,
        client_order_id=deterministic_client_order_id(intent.intent_id, "close"),
        position_id="position-1",
        quantity=Decimal("60"),
        submitted_at_ms=NOW + 1_700,
        now_ms=NOW + 1_700,
    )
    exit_changed = repos.trading.record_position_changed(
        intent.intent_id,
        position_id="position-1",
        actual_quantity=Decimal("40"),
        avg_entry_price=Decimal("0.102"),
        now_ms=NOW + 1_800,
    )
    assert exit_changed is not None
    assert (exit_changed.execution_state, exit_changed.execution_phase) == ("IN_FLIGHT", "EXIT")

    exit_replacement_id = deterministic_client_order_id(
        intent.intent_id,
        "stop",
        previous_client_order_id=replacement.stop_client_order_id,
    )
    exit_replacement = repos.trading.prepare_stop_replacement(
        intent.intent_id,
        canceled_client_order_id=replacement.stop_client_order_id,
        submitted_client_order_id=exit_replacement_id,
        generation=2,
        quantity=Decimal("40"),
        now_ms=NOW + 1_900,
    )
    assert exit_replacement is not None
    exit_protected = repos.trading.record_protected(
        intent.intent_id,
        accepted_client_order_id=exit_replacement_id,
        protection_order_id="stop-venue-3",
        protected_quantity=Decimal("40"),
        stop_price=Decimal("0.098"),
        protected_at_ms=NOW + 2_000,
        now_ms=NOW + 2_000,
    )
    conn.commit()

    assert exit_protected is not None
    assert (exit_protected.execution_state, exit_protected.execution_phase) == ("IN_FLIGHT", "EXIT")
    assert exit_protected.protected_quantity == exit_protected.actual_quantity == Decimal("40")
