"""TradeIntent persistence at the real PostgreSQL seam (#283)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from decimal import Decimal
from threading import Barrier, Event
from typing import Any

import pytest
from psycopg import OperationalError
from psycopg.errors import CheckViolation, ForeignKeyViolation, RaiseException, UniqueViolation

from tests.postgres_test_utils import connect_postgres_test
from tests.trading_v3_fixtures import (
    append_capital_evidence_fixture,
    binance_binding,
    binance_capability,
    binance_catalog,
    capital_arm_fixture,
    capital_bundle_fixture,
    capital_grant_fixture,
    capital_risk_policy_fixture,
    store_catalog_fixture,
    trade_intent,
)
from tracefold.app.repository_session import repositories_for_connection
from tracefold.trading import (
    BlacklistSnapshotV1,
    DailyRiskPolicyV1,
    IntentOutcome,
    OperatorArmReceiptV1,
    ProductionPromotionGrantV1,
    ReplayReceiptV1,
    TradeIntent,
    deterministic_client_order_id,
)
from tracefold.trading.admission import ADMISSION_VERSION
from tracefold.trading.capital_lane import CapitalLane, CapitalLaneConfig
from tracefold.trading.contracts import (
    CaseState,
    FrozenMarketContext,
    FrozenPolicyContext,
    InstrumentRef,
    OiMarketTrigger,
    OiTradeCandidate,
    TradingCaseManifest,
)
from tracefold.trading.policy import CAPITAL_POLICY
from tracefold.trading.quote_authority import (
    ExecutionQuoteRejectionV1,
    ExecutionQuoteSnapshotV1,
    QuoteStage,
)
from tracefold.trading.storage.intents import materialize_entry_fence

pytestmark = pytest.mark.integration

NOW = 1_900_000_000_000


# The bundle epoch the fixture Cases are frozen under, and the one the runner is composed with.
NEWS_GENERATION = "bundle_00000000"
AUTHORITY: dict[str, BlacklistSnapshotV1] = {}
CATALOG_SNAPSHOT = binance_catalog(
    captured_at_ms=NOW,
    symbols=("BTCUSDT", "DOGEUSDT", "ETHUSDT", "SOLUSDT"),
)
CAPABILITY_SNAPSHOT = binance_capability(catalog=CATALOG_SNAPSHOT, app_revision="test-revision")
EXECUTION_BINDING = binance_binding(catalog=CATALOG_SNAPSHOT, capability=CAPABILITY_SNAPSHOT)


@pytest.fixture(scope="module")
def conn(postgres_module_clone_dsn: str):
    connection = connect_postgres_test(read_only=False)
    repos = repositories_for_connection(connection)
    connection.execute(
        "UPDATE trading_runtime_state SET nautilus_bootstrap_account_zero_at_ms = %s WHERE id = 1",
        (NOW,),
    )
    store_catalog_fixture(repos.trading, CATALOG_SNAPSHOT, now_ms=NOW)
    connection.execute(
        "UPDATE trading_binding_runtime SET account_state = 'reconciled_flat', "
        "credential_state = 'configured', credential_fingerprint = %s, account_generation = 1 "
        "WHERE binding = 'BINANCE_USDM'",
        (EXECUTION_BINDING.credential_fingerprint,),
    )
    assert repos.trading.append_and_activate_execution_capability_snapshot(
        CAPABILITY_SNAPSHOT,
        created_at_ms=NOW,
    )
    assert repos.trading.append_and_activate_execution_binding(EXECUTION_BINDING)
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
          strategy_config_digest, primary_source_key, supplemental_source_keys,
          manifest, manifest_sha256, state, policy_decision, policy_reason,
          capital_disposition, capital_reason, observed_at_ms, created_at_ms, updated_at_ms
        ) VALUES (%s, 'crypto:SOL', 'oi', 'source_native_oi_smart_money_long_v3',
                  'source_native_oi_smart_money_long_v3', %s, %s, '[]'::jsonb,
                  '{}'::jsonb, %s, 'RUNNING', 'not_run', 'not_run',
                  'not_applicable', NULL, %s, %s, %s)
        """,
        (case_id, "0" * 64, f"source-{case_id}", "1" * 64, NOW, NOW, NOW),
    )
    connection.commit()


def _intent(
    *,
    case_id: str = "case-intent-1",
    case_manifest_sha256: str = "1" * 64,
    created_at_ms: int = NOW,
) -> TradeIntent:
    blacklist = AUTHORITY["blacklist"]
    return trade_intent(
        case_id=case_id,
        case_manifest_sha256=case_manifest_sha256,
        blacklist_snapshot=blacklist,
        catalog=CATALOG_SNAPSHOT,
        capability=CAPABILITY_SNAPSHOT,
        binding=EXECUTION_BINDING,
        created_at_ms=created_at_ms,
        reference_price=Decimal("60000"),
        target_notional=Decimal("10"),
    )


def _insert_test_intent(repos: Any, intent: TradeIntent) -> bool:
    """Seed the minimum test-only authority chain before exercising Intent lifecycle storage.

    Production has no equivalent escape hatch: CapitalLane inserts the same facts through the typed
    atomic bundle.  These lifecycle tests intentionally start at an already-authorized Intent, so the
    fixture writes an explicit chain instead of weakening the production foreign key.
    """

    if repos.trading.intent(intent.intent_id) is not None:
        return bool(repos.trading.insert_intent(intent))
    policy = capital_risk_policy_fixture()
    grant = capital_grant_fixture(
        catalog=CATALOG_SNAPSHOT,
        capability=CAPABILITY_SNAPSHOT,
        binding=EXECUTION_BINDING,
        allowed_capability_entry_id=intent.capability_entry_id,
    )
    arm = capital_arm_fixture(
        catalog=CATALOG_SNAPSHOT,
        capability=CAPABILITY_SNAPSHOT,
        binding=EXECUTION_BINDING,
        allowed_capability_entry_id=intent.capability_entry_id,
    )
    if repos.trading.daily_risk_policy(policy.risk_policy_sha256) is None:
        repos.trading.append_daily_risk_policy(policy, created_at_ms=NOW)
    append_capital_evidence_fixture(repos)
    if repos.trading.production_promotion_grant(grant.grant_sha256) is None:
        repos.trading.append_production_promotion_grant(grant, created_at_ms=NOW)
    if repos.trading.operator_arm_receipt(arm.arm_receipt_sha256) is None:
        repos.trading.append_operator_arm_receipt(arm, created_at_ms=NOW)
    repos.trading.conn.execute(
        "UPDATE trading_binding_runtime SET active_arm_receipt_sha256 = %s WHERE binding = %s",
        (arm.arm_receipt_sha256, intent.binding),
    )
    reservation, receipt = capital_bundle_fixture(
        intent,
        catalog=CATALOG_SNAPSHOT,
        capability=CAPABILITY_SNAPSHOT,
        binding=EXECUTION_BINDING,
    )
    assert intent.capital_authorization_receipt_sha256 == receipt.authorization_receipt_sha256
    return bool(
        repos.trading.insert_authorized_intent_bundle(
            reservation=reservation,
            receipt=receipt,
            intent=intent,
            now_ms=intent.created_at_ms,
        )
    )


def _allow_entry(connection: Any) -> None:
    connection.execute(
        """
        UPDATE trading_runtime_state
           SET control = 'RUNNING'
         WHERE id = 1
        """
    )
    _set_binance_binding_flat(connection)


def _accepted_q1(
    intent: TradeIntent,
    *,
    evaluated_at_ms: int,
    stage: QuoteStage = "Q1",
) -> ExecutionQuoteSnapshotV1:
    return ExecutionQuoteSnapshotV1(
        stage=stage,
        intent_id=intent.intent_id,
        instrument_id=intent.instrument_id,
        side="buy",
        side_price=intent.reference_price,
        bid=intent.reference_price,
        ask=intent.reference_price,
        ts_event_ns=evaluated_at_ms * 1_000_000,
        ts_init_ns=evaluated_at_ms * 1_000_000,
        evaluated_at_ns=evaluated_at_ms * 1_000_000,
        stream_generation=1,
        receive_age_ns=0,
        event_age_ns=0,
        source_latency_ns=0,
        spread_bps=Decimal(0),
        reference_drift_bps=Decimal(0),
    )


def _fence(
    repos: Any,
    intent: TradeIntent,
    *,
    engine_identity: str,
    now_ms: int,
) -> Any:
    with repos.transaction():
        blacklist_state = repos.trading.blacklist_snapshot_rows(now_ms=now_ms, materialize_expiry=True)
    prepared = repos.trading.prepare_entry_fence(
        intent.intent_id,
        submission_quantity=Decimal("0.0001"),
        q1_evidence=_accepted_q1(intent, evaluated_at_ms=now_ms),
        blacklist_state=blacklist_state,
        requested_at_ms=now_ms,
        now_ms=now_ms,
    )
    with repos.transaction():
        written = repos.trading.fence_entry(
            prepared,
            engine_identity=engine_identity,
            submission_quantity=Decimal("0.0001"),
            requested_at_ms=now_ms,
            now_ms=now_ms,
        )
    return materialize_entry_fence(written)


def _reset_authority(connection: Any) -> None:
    connection.execute("DELETE FROM trading_symbol_blacklist WHERE base_symbol NOT IN ('BTC', 'ETH', 'CL')")
    connection.execute(
        """
        UPDATE trading_runtime_state
           SET control = 'PAUSED', blacklist_revision = 0, arm_epoch = 1,
               active_capability_snapshot_sha256 = %s,
               active_capability_included_count = %s,
               nautilus_bootstrap_account_zero_at_ms = NULL
         WHERE id = 1
        """,
        (CAPABILITY_SNAPSHOT.snapshot_sha256, len(CAPABILITY_SNAPSHOT.included)),
    )
    store_catalog_fixture(repositories_for_connection(connection).trading, CATALOG_SNAPSHOT, now_ms=NOW)
    connection.execute(
        """
        UPDATE trading_binding_runtime
           SET credential_state = 'unconfigured', credential_fingerprint = NULL,
               runtime_state = 'stopped', account_state = 'unknown',
               capability_state = 'ready', capability_snapshot_sha256 = %s,
               capability_compiled_at_ms = %s, capability_compile_error = NULL,
               execution_binding_sha256 = %s, active_arm_receipt_sha256 = NULL,
               heartbeat_at_ms = NULL, reason = 'credentials_unconfigured', updated_at_ms = %s
         WHERE binding = 'BINANCE_USDM'
        """,
        (CAPABILITY_SNAPSHOT.snapshot_sha256, NOW, EXECUTION_BINDING.binding_sha256, NOW),
    )


def _set_binance_binding_flat(connection: Any, *, runtime_state: str = "ready") -> None:
    connection.execute(
        """
        UPDATE trading_binding_runtime
           SET credential_state = 'configured', credential_fingerprint = %s,
               account_generation = 1, runtime_state = %s,
               account_state = 'reconciled_flat', reason = NULL, updated_at_ms = %s
         WHERE binding = 'BINANCE_USDM'
        """,
        (EXECUTION_BINDING.credential_fingerprint, runtime_state, NOW),
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
    """One bounded read and one bounded transaction over a real connection.

    `fail_capital_settle` makes the Case's terminal capital transition return `False` inside the
    commit, proving the policy and capital facts stay atomic.
    """

    def __init__(self, connection: Any, *, fail_capital_settle: bool = False) -> None:
        self.connection = connection
        self.fail_capital_settle = fail_capital_settle

    async def tx(self, name: str, fn: Callable[[Any], Any], *, timeout_seconds: float) -> Any:
        del timeout_seconds
        repos = repositories_for_connection(self.connection)
        if name == "trading_capital_disposition_commit" and self.fail_capital_settle:
            trading = repos.trading
            original = trading.settle_case

            def refuse_capital_block(**kwargs: Any) -> bool:
                if kwargs.get("capital_disposition") == "blocked":
                    return False
                return bool(original(**kwargs))

            trading.settle_case = refuse_capital_block  # type: ignore[method-assign]
        with self.connection.transaction():
            return fn(repos)

    async def read(self, _name: str, fn: Callable[[Any], Any], *, timeout_seconds: float) -> Any:
        del timeout_seconds
        return fn(repositories_for_connection(self.connection))


def _executable_manifest(
    *,
    instrument: InstrumentRef | None = None,
    catalog_sha256: str | None = None,
) -> TradingCaseManifest:
    selected = instrument or InstrumentRef(
        exchange_id="binance",
        venue="binance.usdm",
        binding="BINANCE_USDM",
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
        learning_epoch=NEWS_GENERATION,
        program_version="news_oi_signal_v2",
        program_sha256="a" * 64,
        policy_version="news_triage_policy_v11",
        judgment_contract_version="news_judgment_v2",
        judgment_origin="oi",
        judgment_sha256="c" * 64,
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
        contexts=FrozenPolicyContext(
            oi=oi,
            market=FrozenMarketContext(
                mark_price=Decimal("100"),
                observed_at_ms=NOW,
                pre_move_bps=200,
                pre_move_lookback_ms=3_600_000,
            ),
        ),
        policy_id=CAPITAL_POLICY.policy_id,
        policy_version=CAPITAL_POLICY.policy_version,
        policy_config=CAPITAL_POLICY.config_snapshot,
        policy_config_digest=CAPITAL_POLICY.config_digest,
        underlying_key=f"crypto:{selected.base_symbol}",
        base_symbol=selected.base_symbol,
        cutoff_ms=NOW,
        instrument=selected,
        venue_catalog_snapshot_sha256=catalog_sha256 or CATALOG_SNAPSHOT.snapshot_sha256,
    )


def _pending_executable_case(
    connection: Any,
    *,
    manifest: TradingCaseManifest | None = None,
    case_id: str = "case-sol",
) -> TradingCaseManifest:
    connection.execute("TRUNCATE trading_intents, trading_orders, trading_cases CASCADE")
    _reset_authority(connection)
    manifest = manifest or _executable_manifest()
    repos = repositories_for_connection(connection)
    admission = _admission_row(manifest)
    assert repos.trading.create_case(
        case_id=case_id,
        manifest=manifest,
        admission=admission,
        release_revision="test-release",
        now_ms=NOW,
    )
    connection.commit()
    return manifest


def _admission_row(manifest: TradingCaseManifest) -> dict[str, Any]:
    return {
        "source_key": manifest.primary_trigger.source_key,
        "gate_version": ADMISSION_VERSION,
        "gate_config_digest": "0" * 64,
        "trigger_kind": "oi",
        "underlying_key": manifest.underlying_key,
        "source_observed_at_ms": NOW,
        "status": "CASE_CREATED",
        "stage": "freeze",
        "reason": "case_created",
        "retryable": False,
        "evidence": {},
        "case_id": None,
    }


def _capital_lane(connection: Any, *, fail_capital_settle: bool = False) -> CapitalLane:
    async def _bars(_symbol: str, _start: int, _end: int) -> tuple[()]:
        return ()

    async def _oi_projection(_metric: str, _after: int, _until: int) -> tuple[()]:
        return ()

    return CapitalLane(
        db=_RunnerDb(connection, fail_capital_settle=fail_capital_settle),
        config=CapitalLaneConfig(target_notional_usd=Decimal("7.5")),
        bars=_bars,
        oi_projection=_oi_projection,
        # The News generation this lane may advance a Case under (#314 review). It matches the epoch the
        # fixture manifests are frozen with; the superseded-generation test is where they disagree.
        news_generation=NEWS_GENERATION,
        release_revision="test-release",
        clock=lambda: NOW + 1_000,
    )


def _activate_lane_authority(connection: Any, lane: CapitalLane) -> tuple[str, str, str]:
    repos = repositories_for_connection(connection)
    _set_binance_binding_flat(connection)
    policy = capital_risk_policy_fixture()
    capability_entry = CAPABILITY_SNAPSHOT.resolve("SOL")
    assert capability_entry is not None
    base_grant = capital_grant_fixture(
        catalog=CATALOG_SNAPSHOT,
        capability=CAPABILITY_SNAPSHOT,
        binding=EXECUTION_BINDING,
        allowed_capability_entry_id=capability_entry.catalog_entry_id,
    )
    future_result = append_capital_evidence_fixture(
        repos,
        source_contract_sha256=lane.source_contract_sha256,
        feature_contract_sha256=lane.feature_contract_sha256,
        policy_config_sha256=CAPITAL_POLICY.config_digest,
    )
    grant = base_grant.model_copy(
        update={
            "source_contract_sha256": lane.source_contract_sha256,
            "feature_contract_sha256": lane.feature_contract_sha256,
            "policy_config_sha256": CAPITAL_POLICY.config_digest,
            "locked_future_report_sha256": future_result.report_sha256,
        }
    )
    base_arm = capital_arm_fixture(
        catalog=CATALOG_SNAPSHOT,
        capability=CAPABILITY_SNAPSHOT,
        binding=EXECUTION_BINDING,
        allowed_capability_entry_id=capability_entry.catalog_entry_id,
    )
    arm = base_arm.model_copy(
        update={
            "grant_sha256": grant.grant_sha256,
            "risk_policy_sha256": policy.risk_policy_sha256,
        }
    )
    repos.trading.append_daily_risk_policy(policy, created_at_ms=NOW)
    repos.trading.append_production_promotion_grant(grant, created_at_ms=NOW)
    repos.trading.append_operator_arm_receipt(arm, created_at_ms=NOW)
    assert repos.trading.activate_operator_arms([arm.arm_receipt_sha256], now_ms=NOW + 500)
    connection.commit()
    return policy.risk_policy_sha256, grant.grant_sha256, arm.arm_receipt_sha256


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
    funding_by_currency: dict[str, str] | None = None,
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
        funding_by_currency=funding_by_currency,
    )
    assert observed is not None
    assert observed.execution_state == "IN_FLIGHT"
    assert observed.execution_phase == "EXIT"
    assert observed.flat_verified_at_ms is None
    assert observed.commissions_by_currency == commissions_by_currency
    assert observed.funding_by_currency == funding_by_currency
    return observed


def test_funding_json_python_and_database_contracts_share_a_native_write_budget(conn: Any) -> None:
    case_id = "case-funding-contract"
    _case(conn, case_id=case_id)
    intent = _intent(case_id=case_id)
    repos = repositories_for_connection(conn)
    assert _insert_test_intent(repos, intent)
    conn.commit()

    production_limit_payload = {f"CUR{index:02d}": "9" * 64 for index in range(16)}
    assert IntentOutcome.validate_currency_amounts(production_limit_payload) == production_limit_payload
    conn.execute("SET LOCAL statement_timeout = '250ms'")
    conn.execute(
        "UPDATE trading_intents SET funding_by_currency = %s::jsonb WHERE intent_id = %s",
        (json.dumps(production_limit_payload), intent.intent_id),
    )
    conn.commit()

    for invalid in ({"bad-key": "1"}, {"USDT": "1e3"}):
        with pytest.raises(ValueError):
            IntentOutcome.validate_currency_amounts(invalid)
        with pytest.raises(CheckViolation) as rejected:
            conn.execute(
                "UPDATE trading_intents SET funding_by_currency = %s::jsonb WHERE intent_id = %s",
                (json.dumps(invalid), intent.intent_id),
            )
        assert rejected.value.diag.constraint_name == "trading_intents_funding_check"
        conn.rollback()


def test_the_lane_refuses_a_case_frozen_under_a_superseded_news_generation(conn: Any) -> None:
    """A Case that outlived the News bundle it was reasoned under must not become an Intent (#314 review).

    The sequence is ordinary rather than exotic: a Case is frozen, left undecided, and a deployment moves
    the News bundle — a prompt edit or a model re-slot does that without moving `program_version` or
    `policy_version`, which are the only other upstream pins this path holds. The projection that creates
    Cases joins the running epoch, so a stale row cannot enter; nothing but this check stops one that is
    already persisted from advancing.
    """

    _pending_executable_case(conn)
    lane = _capital_lane(conn)
    lane._news_generation = "bundle_deadbeef"

    assert asyncio.run(lane._decide_one()) is CaseState.BLOCKED

    row = conn.execute(
        "SELECT state, policy_decision, policy_reason, capital_disposition, capital_reason "
        "FROM trading_cases WHERE case_id = 'case-sol'"
    ).fetchone()
    assert dict(row) == {
        "state": "BLOCKED",
        "policy_decision": "not_run",
        "policy_reason": "source_generation_retired",
        "capital_disposition": "not_applicable",
        "capital_reason": None,
    }
    assert conn.execute("SELECT count(*) AS n FROM trading_intents").fetchone()["n"] == 0


def test_no_key_lane_persists_long_and_capital_block_without_an_intent(conn: Any) -> None:
    """#350 F2P: Decision runs under PAUSED Capital and preserves both independent answers."""

    _pending_executable_case(conn)

    assert asyncio.run(_capital_lane(conn)._decide_one()) is CaseState.BLOCKED

    row = conn.execute(
        "SELECT state, policy_decision, policy_reason, policy_checks, "
        "       capital_disposition, capital_reason "
        "FROM trading_cases WHERE case_id = 'case-sol'"
    ).fetchone()
    assert dict(row) | {"policy_checks": None} == {
        "state": "BLOCKED",
        "policy_decision": "long",
        "policy_reason": "smart_money_momentum_long",
        "policy_checks": None,
        "capital_disposition": "blocked",
        "capital_reason": "capital_paused",
    }
    # The frozen evidence travels with the Case, so a console can explain it without today's config.
    checks = row["policy_checks"]["checks"]
    assert {check["check"] for check in checks} == {
        "source_measurement_window_ms",
        "oi_direction",
        "oi_change_bps",
        "whale_oi_ratio_bps",
        "whale_long_profit_bps",
        "pre_move_bps",
    }
    assert all(check["passed"] for check in checks)
    assert conn.execute("SELECT count(*) AS n FROM trading_intents").fetchone()["n"] == 0
    assert conn.execute("SELECT count(*) AS n FROM trading_orders").fetchone()["n"] == 0


def test_lane_atomically_reserves_risk_authorizes_and_emits_v3_intent(conn: Any) -> None:
    manifest = _pending_executable_case(conn)
    lane = _capital_lane(conn)
    risk_sha, grant_sha, arm_sha = _activate_lane_authority(conn, lane)

    assert asyncio.run(lane._decide_one()) is CaseState.INTENT_EMITTED

    case = conn.execute(
        "SELECT state, policy_decision, capital_disposition, capital_reason "
        "FROM trading_cases WHERE case_id = 'case-sol'"
    ).fetchone()
    assert dict(case) == {
        "state": "INTENT_EMITTED",
        "policy_decision": "long",
        "capital_disposition": "allowed",
        "capital_reason": "capital_authorized",
    }
    row = conn.execute(
        """
        SELECT intent.intent_version, intent.case_manifest_sha256,
               intent.capital_authorization_receipt_sha256,
               reservation.risk_policy_sha256, reservation.grant_sha256,
               reservation.arm_receipt_sha256, state.status, state.attempt_consumed
          FROM trading_intents intent
          JOIN trading_capital_authorization_receipts receipt
            ON receipt.authorization_receipt_sha256 = intent.capital_authorization_receipt_sha256
          JOIN trading_capital_risk_reservations reservation
            ON reservation.reservation_sha256 = receipt.reservation_sha256
          JOIN trading_capital_risk_reservation_state state
            ON state.intent_id = intent.intent_id
        """
    ).fetchone()
    assert dict(row) == {
        "intent_version": "trade_intent_v3",
        "case_manifest_sha256": manifest.digest(),
        "capital_authorization_receipt_sha256": row["capital_authorization_receipt_sha256"],
        "risk_policy_sha256": risk_sha,
        "grant_sha256": grant_sha,
        "arm_receipt_sha256": arm_sha,
        "status": "RESERVED",
        "attempt_consumed": False,
    }
    assert len(str(row["capital_authorization_receipt_sha256"])) == 64


def test_capital_console_projection_reads_exact_authority_and_pages_without_duplicates(conn: Any) -> None:
    _case(conn, case_id="case-evidence-1")
    repos = repositories_for_connection(conn)
    first_intent = _intent(case_id="case-evidence-1", created_at_ms=NOW)
    assert _insert_test_intent(repos, first_intent)
    assert (
        repos.trading.expire_unfenced_intent(
            first_intent.intent_id,
            now_ms=first_intent.valid_until_ms,
        )
        is not None
    )
    _case_without_reset(conn, case_id="case-evidence-2")
    second_intent = _intent(
        case_id="case-evidence-2",
        case_manifest_sha256="3" * 64,
        created_at_ms=NOW + 1,
    )
    assert _insert_test_intent(repos, second_intent)
    conn.commit()

    authorities = repos.trading.authority_projection()
    binance = next(row for row in authorities if row["binding"] == "BINANCE_USDM")
    arm = OperatorArmReceiptV1.model_validate(binance["arm_payload"])
    grant = ProductionPromotionGrantV1.model_validate(binance["grant_payload"])
    policy = DailyRiskPolicyV1.model_validate(binance["policy_payload"])
    assert arm.arm_receipt_sha256 == binance["active_arm_receipt_sha256"]
    assert arm.grant_sha256 == grant.grant_sha256
    assert arm.risk_policy_sha256 == grant.risk_policy_sha256 == policy.risk_policy_sha256
    assert binance["revocation_payload"] is None

    first_page = repos.trading.console_capital_evidence(limit=1)
    marker = (int(first_page[0]["updated_at_ms"]), str(first_page[0]["reservation_sha256"]))
    second_page = repos.trading.console_capital_evidence(before=marker, limit=1)
    assert first_page[0]["reservation_sha256"] != second_page[0]["reservation_sha256"]
    assert {first_page[0]["intent_id"], second_page[0]["intent_id"]} == {
        first_intent.intent_id,
        second_intent.intent_id,
    }

    assert len(repos.trading.authority_projection()) == 2
    assert {row["intent_id"] for row in repos.trading.console_capital_evidence(limit=10)} == {
        first_intent.intent_id,
        second_intent.intent_id,
    }


def test_running_capital_still_blocks_no_key_long_as_credentials_unconfigured(conn: Any) -> None:
    doge = InstrumentRef(
        exchange_id="binance",
        venue="binance.usdm",
        binding="BINANCE_USDM",
        provider_symbol="DOGEUSDT",
        base_symbol="DOGE",
        instrument_class="crypto",
        quote_asset="USDT",
        observed_at_ms=NOW,
    )
    _pending_executable_case(conn, manifest=_executable_manifest(instrument=doge))
    conn.execute("UPDATE trading_runtime_state SET control = 'RUNNING' WHERE id = 1")
    conn.commit()

    assert asyncio.run(_capital_lane(conn)._decide_one()) is CaseState.BLOCKED
    row = conn.execute(
        "SELECT policy_decision, capital_disposition, capital_reason FROM trading_cases WHERE case_id = 'case-sol'"
    ).fetchone()
    assert dict(row) == {
        "policy_decision": "long",
        "capital_disposition": "blocked",
        "capital_reason": "credentials_unconfigured",
    }
    assert conn.execute("SELECT count(*) AS n FROM trading_intents").fetchone()["n"] == 0


def test_a_failed_capital_transition_leaves_the_case_claimable(conn: Any) -> None:
    """The LONG and capital block are one transition; neither may be partially persisted."""

    _pending_executable_case(conn)

    with pytest.raises(RuntimeError, match="trading_case_block_transition_failed"):
        asyncio.run(_capital_lane(conn, fail_capital_settle=True)._decide_one())
    conn.rollback()

    assert conn.execute("SELECT count(*) AS n FROM trading_intents").fetchone()["n"] == 0
    case = conn.execute("SELECT state, policy_reason FROM trading_cases WHERE case_id = 'case-sol'").fetchone()
    # `RUNNING` with an expired lease is claimable again; it is emphatically not a terminal state, and
    # `intent_admission_blocked` — the catch-all this replaces — no longer exists to be written.
    assert case["state"] == "RUNNING"
    assert case["policy_reason"] == "not_run"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("exchange_id", "hyperliquid"),
        ("venue", "hl.perp"),
        ("instrument_class", "equity"),
        ("provider_symbol", "SOL"),
        ("base_symbol", "ETH"),
        ("quote_asset", "USDC"),
    ),
)
def test_a_public_catalog_identity_never_grants_execution(
    conn: Any,
    field: str,
    value: str,
) -> None:
    """The public catalog freezes evidence; it is not an execution permission or adapter registry."""

    exact = _executable_manifest().instrument
    altered = InstrumentRef.model_validate({**exact.model_dump(), field: value})
    _pending_executable_case(conn, manifest=_executable_manifest(instrument=altered))

    assert asyncio.run(_capital_lane(conn)._decide_one()) is CaseState.BLOCKED

    assert conn.execute("SELECT count(*) AS n FROM trading_intents").fetchone()["n"] == 0
    case = conn.execute(
        "SELECT state, policy_decision, capital_reason FROM trading_cases WHERE case_id = 'case-sol'"
    ).fetchone()
    assert dict(case) == {
        "state": "BLOCKED",
        "policy_decision": "long",
        "capital_reason": "capital_paused",
    }


def test_a_catalog_pointer_that_moves_after_freeze_blocks_the_case(conn: Any) -> None:
    """A Case must retain the exact public catalog evidence it was frozen against."""

    _pending_executable_case(conn, manifest=_executable_manifest(catalog_sha256="9" * 64))
    conn.execute("UPDATE trading_runtime_state SET control = 'RUNNING' WHERE id = 1")
    conn.execute(
        "UPDATE trading_binding_runtime SET credential_state = 'configured', reason = NULL "
        "WHERE binding = 'BINANCE_USDM'"
    )
    conn.commit()

    assert asyncio.run(_capital_lane(conn)._decide_one()) is CaseState.BLOCKED

    case = conn.execute("SELECT state, capital_reason FROM trading_cases WHERE case_id = 'case-sol'").fetchone()
    assert dict(case) == {"state": "BLOCKED", "capital_reason": "catalog_mismatch"}
    assert conn.execute("SELECT count(*) AS n FROM trading_intents").fetchone()["n"] == 0


def test_an_operator_deny_list_cannot_turn_paused_capital_into_execution(conn: Any) -> None:

    _pending_executable_case(conn)
    repos = repositories_for_connection(conn)
    with conn.transaction():
        repos.trading.blacklist_upsert(base_symbol="SOL", reason="operator", expires_at_ms=None, now_ms=NOW)
    conn.commit()

    assert asyncio.run(_capital_lane(conn)._decide_one()) is CaseState.BLOCKED

    case = conn.execute(
        "SELECT state, policy_decision, capital_reason FROM trading_cases WHERE case_id = 'case-sol'"
    ).fetchone()
    assert dict(case) == {
        "state": "BLOCKED",
        "policy_decision": "long",
        "capital_reason": "capital_paused",
    }
    assert conn.execute("SELECT count(*) AS n FROM trading_intents").fetchone()["n"] == 0
    conn.execute("DELETE FROM trading_symbol_blacklist WHERE base_symbol = 'SOL'")
    conn.commit()


def test_two_workers_deciding_the_same_case_produce_one_capital_block_and_no_intent(conn: Any) -> None:
    """#331 F2P 6. The claim is exclusive; the loser reads no Case and writes nothing."""

    _pending_executable_case(conn)
    first = _capital_lane(conn)
    second = _capital_lane(conn)

    assert asyncio.run(first._decide_one()) is CaseState.BLOCKED
    assert asyncio.run(second._decide_one()) is None
    assert conn.execute("SELECT count(*) AS n FROM trading_intents").fetchone()["n"] == 0


def test_two_concurrent_connections_claiming_one_case_write_one_block(conn: Any) -> None:
    """The claim remains exclusive against real PostgreSQL concurrency.

    `FOR UPDATE SKIP LOCKED` is what makes the claim exclusive, and the loser's `SKIP LOCKED` returns no
    row rather than blocking — so the second worker does no work at all instead of racing to a second
    terminal receipt. The terminal receipt both read afterwards is the one the winner committed.
    """

    _pending_executable_case(conn)
    barrier = Barrier(2)

    def compete(run_id: str) -> CaseState | None:
        contender = connect_postgres_test(read_only=False)
        try:
            lane = _capital_lane(contender)
            lane._run_id = run_id
            barrier.wait(timeout=5)
            decided = asyncio.run(lane._decide_one())
            contender.commit()
            return decided
        finally:
            contender.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(compete, ("run-a", "run-b")))

    assert sorted(results, key=lambda item: item is None) == [CaseState.BLOCKED, None]
    assert conn.execute("SELECT count(*) AS n FROM trading_intents").fetchone()["n"] == 0
    case = conn.execute("SELECT state FROM trading_cases WHERE case_id = 'case-sol'").fetchone()
    assert case["state"] == "BLOCKED"


def test_a_postgres_fault_inside_the_capital_commit_rolls_back_and_never_terminalises(conn: Any) -> None:
    """#331 comment F2P 5. A PostgreSQL fault is not a business refusal and must not consume the Case.

    The old writer caught every `Exception` from the emission transaction and wrote
    `BLOCKED / intent_admission_blocked` — so a timeout, a serialization failure and a genuine capability
    change were one statistic, and the Source that caused it was consumed forever.
    """

    _pending_executable_case(conn)
    lane = _capital_lane(conn)
    original = lane._db.tx  # type: ignore[attr-defined]

    async def explode(name: str, fn: Any, *, timeout_seconds: float) -> Any:
        if name == "trading_capital_disposition_commit":
            raise OperationalError("server closed the connection unexpectedly")
        return await original(name, fn, timeout_seconds=timeout_seconds)

    lane._db.tx = explode  # type: ignore[attr-defined, method-assign]

    with pytest.raises(OperationalError):
        asyncio.run(lane._decide_one())
    conn.rollback()

    assert conn.execute("SELECT count(*) AS n FROM trading_intents").fetchone()["n"] == 0
    case = conn.execute("SELECT state, policy_reason FROM trading_cases WHERE case_id = 'case-sol'").fetchone()
    assert case["state"] == "RUNNING"
    assert case["policy_reason"] == "not_run"
    # And the retired catch-all is not written under any name.
    assert (
        conn.execute(
            "SELECT count(*) AS n FROM trading_cases WHERE policy_reason = 'intent_admission_blocked'"
        ).fetchone()["n"]
        == 0
    )


def test_a_case_that_settles_only_to_the_three_current_terminal_states(conn: Any) -> None:
    """The two historical states stay readable and have no path back into the table (#331)."""

    _pending_executable_case(conn)
    repos = repositories_for_connection(conn)
    claimed = repos.trading.claim_case(run_id="run-1", lease_ms=60_000, now_ms=NOW)
    assert claimed is not None
    for retired in (CaseState.POLICY_REJECTED, CaseState.ORDER_PREPARED, CaseState.PENDING):
        with pytest.raises(ValueError, match="trading_case_terminal_state_retired"):
            repos.trading.settle_case(
                case_id="case-sol",
                run_id="run-1",
                state=retired,
                policy_decision="no_trade",
                policy_reason="whatever",
                capital_disposition="not_applicable",
                capital_reason=None,
                now_ms=NOW,
            )
    conn.rollback()


def test_capability_activation_requires_paused_flat_and_no_nonterminal_intent(conn: Any) -> None:
    _case(conn)
    repos = repositories_for_connection(conn)
    replacement = CAPABILITY_SNAPSHOT.model_copy(update={"app_revision": "replacement-revision"})

    assert not repos.trading.append_and_activate_execution_capability_snapshot(replacement, created_at_ms=NOW)
    _set_binance_binding_flat(conn)
    conn.execute("UPDATE trading_runtime_state SET control = 'RUNNING' WHERE id = 1")
    assert not repos.trading.append_and_activate_execution_capability_snapshot(replacement, created_at_ms=NOW)

    conn.execute("UPDATE trading_runtime_state SET control = 'PAUSED' WHERE id = 1")
    assert _insert_test_intent(repos, _intent())
    assert not repos.trading.append_and_activate_execution_capability_snapshot(replacement, created_at_ms=NOW)
    conn.execute("TRUNCATE trading_intents CASCADE")

    assert repos.trading.append_and_activate_execution_capability_snapshot(replacement, created_at_ms=NOW)
    runtime = repos.trading.binding_runtime(binding="BINANCE_USDM", now_ms=NOW)
    assert runtime is not None
    assert runtime.capability_snapshot_sha256 == replacement.snapshot_sha256
    assert runtime.capability_state == "ready"
    assert runtime.execution_binding_sha256 is None
    assert runtime.runtime_state == "stale"
    assert runtime.reason == "capability_snapshot_changed"
    _reset_authority(conn)


def test_initial_capability_activation_requires_the_exact_catalog_and_flat_account(conn: Any) -> None:
    repos = repositories_for_connection(conn)
    initial = CAPABILITY_SNAPSHOT.model_copy(update={"app_revision": "initial-revision"})
    conn.execute("UPDATE trading_runtime_state SET control = 'PAUSED' WHERE id = 1")
    conn.execute(
        "UPDATE trading_binding_runtime SET capability_state = 'missing', "
        "capability_snapshot_sha256 = NULL, capability_compiled_at_ms = NULL, "
        "execution_binding_sha256 = NULL, account_state = 'unknown' "
        "WHERE binding = 'BINANCE_USDM'"
    )
    assert not repos.trading.append_and_activate_execution_capability_snapshot(initial, created_at_ms=NOW)

    _set_binance_binding_flat(conn, runtime_state="stopped")
    other_catalog = binance_catalog(captured_at_ms=NOW + 1, symbols=("BTCUSDT",))
    store_catalog_fixture(repos.trading, other_catalog, now_ms=NOW + 1)
    assert not repos.trading.append_and_activate_execution_capability_snapshot(initial, created_at_ms=NOW)
    conn.execute(
        "UPDATE trading_binding_runtime SET catalog_snapshot_sha256 = %s WHERE binding = 'BINANCE_USDM'",
        (CATALOG_SNAPSHOT.snapshot_sha256,),
    )
    assert repos.trading.append_and_activate_execution_capability_snapshot(initial, created_at_ms=NOW)
    assert repos.trading.active_execution_capability_snapshot(binding="BINANCE_USDM") == initial
    _reset_authority(conn)


@pytest.mark.parametrize("projection", ("missing", "ready", "stale"))
def test_successful_compile_clears_stale_error_without_moving_authority(
    conn: Any,
    projection: str,
) -> None:
    repos = repositories_for_connection(conn)
    _reset_authority(conn)
    conn.commit()

    with conn.transaction():
        current_catalog = CATALOG_SNAPSHOT
        if projection == "stale":
            current_catalog = binance_catalog(captured_at_ms=NOW + 2, symbols=("BTCUSDT",))
            store_catalog_fixture(repos.trading, current_catalog, now_ms=NOW + 2)
        compiled = binance_capability(
            catalog=current_catalog,
            app_revision=f"successful-inactive-compile-{projection}",
        )
        missing_projection = projection == "missing"
        conn.execute(
            "UPDATE trading_binding_runtime SET capability_state = 'error', "
            "capability_snapshot_sha256 = CASE WHEN %s THEN NULL ELSE capability_snapshot_sha256 END, "
            "capability_compiled_at_ms = CASE WHEN %s THEN NULL ELSE capability_compiled_at_ms END, "
            "capability_compile_error = 'execution_capability_validationerror_failed', "
            "execution_binding_sha256 = CASE WHEN %s THEN NULL ELSE execution_binding_sha256 END, "
            "account_state = 'unknown' WHERE binding = 'BINANCE_USDM'",
            (missing_projection, missing_projection, missing_projection),
        )

        assert not repos.trading.append_and_activate_execution_capability_snapshot(
            compiled,
            created_at_ms=NOW + 3,
        )

        runtime = repos.trading.binding_runtime(binding="BINANCE_USDM", now_ms=NOW + 3)
        assert runtime is not None
        assert runtime.capability_state == projection
        assert runtime.capability_compile_error is None
        if missing_projection:
            assert runtime.capability_snapshot_sha256 is None
            assert runtime.capability_compiled_at_ms is None
            assert runtime.execution_binding_sha256 is None
        else:
            assert runtime.capability_snapshot_sha256 == CAPABILITY_SNAPSHOT.snapshot_sha256
            assert runtime.capability_compiled_at_ms == NOW
            assert runtime.execution_binding_sha256 == EXECUTION_BINDING.binding_sha256
        assert repos.trading.execution_capability_snapshot(compiled.snapshot_sha256) == compiled

    _reset_authority(conn)
    conn.commit()


def test_late_success_for_stale_catalog_cannot_clear_current_compile_error(conn: Any) -> None:
    repos = repositories_for_connection(conn)
    compiled = CAPABILITY_SNAPSHOT.model_copy(update={"app_revision": "late-success"})
    current_catalog = binance_catalog(captured_at_ms=NOW + 4, symbols=("BTCUSDT",))
    started = Event()
    _reset_authority(conn)
    conn.commit()

    def publish_stale_snapshot() -> bool:
        contender = connect_postgres_test(read_only=False)
        try:
            contender_repos = repositories_for_connection(contender)
            with contender.transaction():
                started.set()
                return contender_repos.trading.append_and_activate_execution_capability_snapshot(
                    compiled,
                    created_at_ms=NOW + 5,
                )
        finally:
            contender.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with conn.transaction():
            conn.execute(
                "SELECT binding FROM trading_binding_runtime WHERE binding = 'BINANCE_USDM' FOR UPDATE"
            ).fetchone()
            future = pool.submit(publish_stale_snapshot)
            assert started.wait(timeout=5)
            with pytest.raises(FutureTimeoutError):
                future.result(timeout=0.5)
            store_catalog_fixture(repos.trading, current_catalog, now_ms=NOW + 4)
            conn.execute(
                "UPDATE trading_binding_runtime SET capability_state = 'error', "
                "capability_compile_error = 'execution_capability_current_catalog_failed' "
                "WHERE binding = 'BINANCE_USDM'"
            )
        assert future.result(timeout=5) is False

    runtime = repos.trading.binding_runtime(binding="BINANCE_USDM", now_ms=NOW + 5)
    assert runtime is not None
    assert runtime.catalog_snapshot_sha256 == current_catalog.snapshot_sha256
    assert runtime.capability_state == "error"
    assert runtime.capability_compile_error == "execution_capability_current_catalog_failed"
    assert repos.trading.execution_capability_snapshot(compiled.snapshot_sha256) == compiled
    conn.rollback()
    _reset_authority(conn)
    conn.commit()


def test_execution_binding_activation_is_atomic_with_current_generation_and_capability(conn: Any) -> None:
    _case(conn)
    repos = repositories_for_connection(conn)
    _set_binance_binding_flat(conn)
    candidate = EXECUTION_BINDING.model_copy(update={"created_at_ms": NOW + 1})
    wrong_generation = candidate.model_copy(update={"account_generation": 2, "created_at_ms": NOW + 2})

    assert not repos.trading.append_and_activate_execution_binding(wrong_generation)
    assert _insert_test_intent(repos, _intent())
    assert not repos.trading.append_and_activate_execution_binding(candidate)
    conn.execute("TRUNCATE trading_intents CASCADE")
    assert repos.trading.append_and_activate_execution_binding(candidate)
    assert repos.trading.active_execution_binding(binding="BINANCE_USDM") == candidate
    _reset_authority(conn)


def test_capability_activation_serializes_against_account_exposure(conn: Any) -> None:
    _case(conn)
    repos = repositories_for_connection(conn)
    _set_binance_binding_flat(conn)
    replacement = CAPABILITY_SNAPSHOT.model_copy(update={"app_revision": "serialized-replacement"})
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
            conn.execute(
                "SELECT binding FROM trading_binding_runtime WHERE binding = 'BINANCE_USDM' FOR UPDATE"
            ).fetchone()
            future = pool.submit(activate)
            assert started.wait(timeout=5)
            with pytest.raises(FutureTimeoutError):
                future.result(timeout=0.5)
            conn.execute(
                "UPDATE trading_binding_runtime SET account_state = 'exposure_present' WHERE binding = 'BINANCE_USDM'"
            )
        assert future.result(timeout=5) is False
    finally:
        pool.shutdown(wait=True)

    assert repos.trading.active_execution_capability_snapshot(binding="BINANCE_USDM") == CAPABILITY_SNAPSHOT
    _reset_authority(conn)


def test_trade_intent_round_trips_as_one_immutable_material_fact(conn: Any) -> None:
    _case(conn)
    intent = _intent()
    repos = repositories_for_connection(conn)

    assert _insert_test_intent(repos, intent) is True
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
        _insert_test_intent(repos, _intent(case_manifest_sha256="3" * 64))
    conn.rollback()


def test_entry_fence_is_the_single_durable_permission_for_an_exposure_increase(conn: Any) -> None:
    _case(conn)
    intent = _intent()
    repos = repositories_for_connection(conn)
    assert _insert_test_intent(repos, intent) is True
    conn.execute("UPDATE trading_runtime_state SET control = 'PAUSED' WHERE id = 1")
    conn.commit()

    # `UNAVAILABLE`, and it says why (#331). The old `None` meant this, a stale dispatch, an expired
    # TTL and a spent daily fence at once, so an engine held back by readiness looked exactly like one
    # with nothing to do.
    paused = _fence(repos, intent, engine_identity="nt-1", now_ms=NOW + 1_000)
    assert (paused.disposition, paused.reason, paused.outcome) == ("UNAVAILABLE", "runtime_not_ready", None)
    conn.commit()

    conn.execute("UPDATE trading_runtime_state SET control = 'RUNNING' WHERE id = 1")
    conn.commit()
    not_ready = _fence(repos, intent, engine_identity="nt-1", now_ms=NOW + 1_500)
    assert (not_ready.disposition, not_ready.reason) == ("UNAVAILABLE", "runtime_not_ready")
    conn.commit()

    _allow_entry(conn)
    conn.commit()
    fence = _fence(repos, intent, engine_identity="nt-1", now_ms=NOW + 2_000)
    conn.commit()

    assert (fence.disposition, fence.reason) == ("GRANTED", "entry_fence_granted")
    fenced = fence.outcome
    assert fenced is not None
    assert fenced.execution_state == "IN_FLIGHT"
    assert fenced.execution_phase == "ENTRY"
    assert fenced.entry_client_order_id == deterministic_client_order_id(intent.intent_id, "entry")
    assert fenced.entry_fenced_at_ms == NOW + 2_000
    assert fenced.submission_fence_version == "submission_fence_v1"
    assert fenced.submission_quantity == Decimal("0.0001")
    assert fenced.entry_quote_q1 == _accepted_q1(intent, evaluated_at_ms=NOW + 2_000)
    risk = conn.execute(
        "SELECT status, current_planned_risk_amount, attempt_consumed, "
        "attempt_day_start_ms, attempt_day_end_ms "
        "FROM trading_capital_risk_reservation_state WHERE intent_id = %s",
        (intent.intent_id,),
    ).fetchone()
    day_start = ((NOW + 2_000) // 86_400_000) * 86_400_000
    assert dict(risk) == {
        "status": "FENCED",
        "current_planned_risk_amount": Decimal("0.150000"),
        "attempt_consumed": True,
        "attempt_day_start_ms": day_start,
        "attempt_day_end_ms": day_start + 86_400_000,
    }

    # A duplicate scan or restart can read this projection, but cannot acquire a second submit fence.
    again = _fence(repos, intent, engine_identity="nt-1", now_ms=NOW + 3_000)
    assert (again.disposition, again.reason) == ("UNAVAILABLE", "intent_not_claimable")
    conn.commit()
    recovered = repos.trading.intent_outcome(intent.intent_id)
    assert recovered == fenced


def test_q2_acceptance_preserves_the_fenced_quantity_before_submit(conn: Any) -> None:
    _case(conn)
    intent = _intent()
    repos = repositories_for_connection(conn)
    assert _insert_test_intent(repos, intent) is True
    _allow_entry(conn)
    fenced = _fence(repos, intent, engine_identity="nt-1", now_ms=NOW + 1_000).outcome
    assert fenced is not None and fenced.entry_client_order_id is not None
    q2 = _accepted_q1(intent, evaluated_at_ms=NOW + 2_000, stage="Q2")

    authorized = repos.trading.authorize_entry_submission(
        intent.intent_id,
        entry_client_order_id=fenced.entry_client_order_id,
        q2_evidence=q2,
        now_ms=NOW + 2_000,
    )
    conn.commit()

    assert authorized is not None
    assert authorized.submission_quantity == fenced.submission_quantity == Decimal("0.0001")
    assert authorized.entry_quote_q1 == fenced.entry_quote_q1
    assert authorized.entry_quote_q2 == q2
    assert authorized.entry_submitted_at_ms is None
    submitted = repos.trading.record_entry_submitted(
        intent.intent_id,
        entry_client_order_id=fenced.entry_client_order_id,
        submitted_at_ms=NOW + 2_500,
    )
    assert submitted is not None
    accepted = repos.trading.record_entry_accepted(
        intent.intent_id,
        entry_client_order_id=fenced.entry_client_order_id,
        accepted_at_ms=NOW + 2_600,
    )
    conn.commit()
    assert accepted is not None
    latency = repos.trading.stage_latency_ms(since_ms=NOW - 1)
    assert latency["intent_emitted_to_adopted"] == {"n": 1, "p50": 1_000, "p95": 1_000}
    assert latency["entry_fence_requested_to_entry_fenced"] == {"n": 1, "p50": 0, "p95": 0}
    assert latency["entry_fenced_to_entry_submitted"] == {"n": 1, "p50": 1_500, "p95": 1_500}
    assert latency["entry_submitted_to_entry_accepted"] == {"n": 1, "p50": 100, "p95": 100}


def test_reconnect_can_replace_an_unspent_q2_authorization_with_durable_no_submit(conn: Any) -> None:
    case_id = "case-q2-reconnect"
    _case(conn, case_id=case_id)
    intent = _intent(case_id=case_id)
    repos = repositories_for_connection(conn)
    assert _insert_test_intent(repos, intent) is True
    _allow_entry(conn)
    fenced = _fence(repos, intent, engine_identity="nt-1", now_ms=NOW + 1_000).outcome
    assert fenced is not None and fenced.entry_client_order_id is not None
    accepted_q2 = _accepted_q1(intent, evaluated_at_ms=NOW + 2_000, stage="Q2")
    assert (
        repos.trading.authorize_entry_submission(
            intent.intent_id,
            entry_client_order_id=fenced.entry_client_order_id,
            q2_evidence=accepted_q2,
            now_ms=NOW + 2_000,
        )
        is not None
    )
    rejected_q2 = ExecutionQuoteRejectionV1(
        stage="Q2",
        reason="quote_missing",
        intent_id=intent.intent_id,
        instrument_id=intent.instrument_id,
        side="buy",
        evaluated_at_ns=(NOW + 2_001) * 1_000_000,
    )

    rejected = repos.trading.record_fenced_quote_no_submit(
        intent.intent_id,
        entry_client_order_id=fenced.entry_client_order_id,
        reason_code="quote_missing",
        q2_evidence=rejected_q2,
        now_ms=NOW + 2_001,
    )
    conn.commit()

    assert rejected is not None
    assert rejected.entry_quote_q2 == rejected_q2
    assert (rejected.terminal_outcome, rejected.entry_submitted_at_ms) == ("REJECTED", None)


def test_q2_rejection_is_a_durable_fenced_no_submit_terminal(conn: Any) -> None:
    _case(conn)
    intent = _intent()
    repos = repositories_for_connection(conn)
    assert _insert_test_intent(repos, intent) is True
    _allow_entry(conn)
    fenced = _fence(repos, intent, engine_identity="nt-1", now_ms=NOW + 1_000).outcome
    assert fenced is not None and fenced.entry_client_order_id is not None
    q2 = ExecutionQuoteRejectionV1(
        stage="Q2",
        reason="quote_receive_stale",
        intent_id=intent.intent_id,
        instrument_id=intent.instrument_id,
        side="buy",
        evaluated_at_ns=(NOW + 3_001) * 1_000_000,
        receive_age_ns=2_001_000_000,
    )

    rejected = repos.trading.record_fenced_quote_no_submit(
        intent.intent_id,
        entry_client_order_id=fenced.entry_client_order_id,
        reason_code="quote_receive_stale",
        q2_evidence=q2,
        now_ms=NOW + 3_001,
    )
    conn.commit()

    assert rejected is not None
    assert (rejected.execution_state, rejected.terminal_outcome, rejected.reason_code) == (
        "TERMINAL",
        "REJECTED",
        "quote_receive_stale",
    )
    assert rejected.submission_quantity == Decimal("0.0001")
    assert rejected.entry_quote_q2 == q2
    assert rejected.entry_submitted_at_ms is None
    assert repos.trading.active_intent() is None
    risk = conn.execute(
        "SELECT status, current_planned_risk_amount, attempt_consumed, attempt_day_start_ms "
        "FROM trading_capital_risk_reservation_state WHERE intent_id = %s",
        (intent.intent_id,),
    ).fetchone()
    assert dict(risk) == {
        "status": "RELEASED",
        "current_planned_risk_amount": Decimal("0"),
        "attempt_consumed": True,
        "attempt_day_start_ms": (NOW // 86_400_000) * 86_400_000,
    }
    assert (
        repos.trading.record_fenced_quote_no_submit(
            intent.intent_id,
            entry_client_order_id=fenced.entry_client_order_id,
            reason_code="quote_receive_stale",
            q2_evidence=q2,
            now_ms=NOW + 3_002,
        )
        is None
    )
    conn.commit()


@pytest.mark.parametrize(
    "change",
    (
        {"intent_id": "f" * 64},
        {"instrument_id": "BTCUSDT-PERP.BINANCE"},
        {"side": "sell"},
        {"stage": "Q1"},
        {"reason": "quote_missing"},
    ),
    ids=("intent", "instrument", "side", "stage", "reason"),
)
def test_repository_rejects_quote_audit_that_does_not_match_the_durable_intent(
    conn: Any,
    change: dict[str, str],
) -> None:
    _case(conn)
    intent = _intent()
    repos = repositories_for_connection(conn)
    assert _insert_test_intent(repos, intent) is True
    _allow_entry(conn)
    fenced = _fence(repos, intent, engine_identity="nt-1", now_ms=NOW + 1_000).outcome
    assert fenced is not None and fenced.entry_client_order_id is not None
    audit = ExecutionQuoteRejectionV1(
        stage="Q2",
        reason="quote_receive_stale",
        intent_id=intent.intent_id,
        instrument_id=intent.instrument_id,
        side="buy",
        evaluated_at_ns=(NOW + 3_001) * 1_000_000,
    ).model_copy(update=change)

    with pytest.raises(ValueError, match="entry_quote_audit_invalid"):
        repos.trading.record_fenced_quote_no_submit(
            intent.intent_id,
            entry_client_order_id=fenced.entry_client_order_id,
            reason_code="quote_receive_stale",
            q2_evidence=audit,
            now_ms=NOW + 3_001,
        )
    conn.rollback()


def test_blacklist_change_after_emission_is_rechecked_and_frozen_at_the_entry_fence(conn: Any) -> None:
    _case(conn)
    intent = _intent()
    repos = repositories_for_connection(conn)
    assert _insert_test_intent(repos, intent) is True
    repos.trading.blacklist_upsert(
        base_symbol="SOL",
        reason="operator",
        expires_at_ms=None,
        now_ms=NOW + 500,
    )
    _allow_entry(conn)
    conn.commit()

    fence = _fence(repos, intent, engine_identity="nt-1", now_ms=NOW + 1_000)
    conn.commit()

    assert fence.disposition == "REFUSED"
    refused = fence.outcome
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


def test_expired_blacklist_does_not_kill_a_pending_entry_fence(conn: Any) -> None:
    _case(conn)
    db_now_ms = int(
        conn.execute("SELECT floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint AS now_ms").fetchone()[
            "now_ms"
        ]
    )
    intent = _intent(created_at_ms=db_now_ms - 1_000)
    repos = repositories_for_connection(conn)
    assert _insert_test_intent(repos, intent) is True
    repos.trading.blacklist_upsert(
        base_symbol="SOL",
        reason="timed_operator_hold",
        expires_at_ms=db_now_ms - 1,
        now_ms=db_now_ms - 500,
    )
    _allow_entry(conn)
    conn.commit()

    fenced = _fence(repos, intent, engine_identity="nt-1", now_ms=db_now_ms).outcome
    conn.commit()

    assert fenced is not None
    assert (fenced.execution_state, fenced.execution_phase) == ("IN_FLIGHT", "ENTRY")
    evidence = conn.execute(
        "SELECT blacklist_revision_at_fence, blacklist_snapshot_payload_at_fence "
        "FROM trading_intents WHERE intent_id = %s",
        (intent.intent_id,),
    ).fetchone()
    assert evidence["blacklist_revision_at_fence"] == 2
    assert all(
        row["underlying_key"] != "crypto:SOL" for row in evidence["blacklist_snapshot_payload_at_fence"]["active_rows"]
    )


def test_two_database_transactions_competing_for_one_entry_fence_have_one_winner(conn: Any) -> None:
    _case(conn)
    intent = _intent()
    repos = repositories_for_connection(conn)
    assert _insert_test_intent(repos, intent) is True
    _allow_entry(conn)
    conn.commit()
    barrier = Barrier(2)

    def compete(engine_identity: str) -> IntentOutcome | None:
        contender = connect_postgres_test(read_only=False)
        try:
            contender_repos = repositories_for_connection(contender)
            barrier.wait(timeout=5)
            result = _fence(
                contender_repos,
                intent,
                engine_identity=engine_identity,
                now_ms=NOW + 2_000,
            ).outcome
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


def test_a_closed_thesis_frees_the_lane_for_another_entry_the_same_day(conn: Any) -> None:
    """#348 inverts this test. It used to prove the one-entry-per-UTC-day fence, code and index both.

    Exposure was never what that fence bounded — `ux_trading_intents_one_active` is a unique index
    admitting a single nonterminal Intent, and it is untouched. The daily cap bounded *throughput*,
    and the cost was a blind spot on exactly the days the lane worked: every frame after the first
    entry was refused before the policy ran, so the lane could not say which of them it should have
    taken. Once the first thesis is flat, the second may enter the same day.
    """

    _case(conn)
    repos = repositories_for_connection(conn)
    first = _intent()
    assert _insert_test_intent(repos, first) is True
    _allow_entry(conn)
    assert _fence(repos, first, engine_identity="nt-1", now_ms=NOW + 1_000).granted
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
        realized_pnl_amount=Decimal("0"),
        realized_pnl_currency="USDT",
        commissions_by_currency={},
        funding_by_currency={},
    )
    assert repos.trading.record_closed_flat(
        first.intent_id,
        position_id="position-1",
        authoritative_quantity=Decimal("0"),
        avg_exit_price=Decimal("60001"),
        closed_at_ms=NOW + 4_000,
        flat_verified_at_ms=NOW + 4_000,
        realized_pnl_amount=Decimal("0"),
        realized_pnl_currency="USDT",
        commissions_by_currency={},
        now_ms=NOW + 4_000,
        funding_by_currency={},
    )
    _case_without_reset(conn, case_id="case-intent-2")
    second = _intent(case_id="case-intent-2", case_manifest_sha256="3" * 64)
    assert _insert_test_intent(repos, second) is True
    conn.commit()

    granted = _fence(repos, second, engine_identity="nt-1", now_ms=NOW + 5_000)
    assert granted.disposition == "GRANTED"
    conn.commit()


def test_database_allows_only_one_nonterminal_intent_globally(conn: Any) -> None:
    _case(conn, case_id="case-intent-1")
    first = _intent()
    repos = repositories_for_connection(conn)
    assert _insert_test_intent(repos, first) is True
    conn.commit()

    _case_without_reset(conn, case_id="case-intent-2")
    second = _intent(case_id="case-intent-2", case_manifest_sha256="3" * 64)
    with pytest.raises(UniqueViolation):
        _insert_test_intent(repos, second)
    conn.rollback()


def test_unfenced_expiry_is_terminal_and_releases_the_single_active_slot(conn: Any) -> None:
    _case(conn, case_id="case-intent-1")
    repos = repositories_for_connection(conn)
    intent = _intent()
    assert _insert_test_intent(repos, intent) is True
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
    risk = conn.execute(
        "SELECT status, current_planned_risk_amount, attempt_consumed "
        "FROM trading_capital_risk_reservation_state WHERE intent_id = %s",
        (intent.intent_id,),
    ).fetchone()
    assert dict(risk) == {
        "status": "RELEASED",
        "current_planned_risk_amount": Decimal("0"),
        "attempt_consumed": False,
    }

    _case_without_reset(conn, case_id="case-intent-2")
    assert _insert_test_intent(repos, _intent(case_id="case-intent-2", case_manifest_sha256="3" * 64)) is True
    conn.commit()


def test_authoritative_refusal_before_submit_is_rejected_without_exposure(conn: Any) -> None:
    _case(conn)
    repos = repositories_for_connection(conn)
    intent = _intent()
    assert _insert_test_intent(repos, intent) is True
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


def test_fence_attempt_is_charged_to_its_utc_day_while_open_reservation_crosses_midnight(conn: Any) -> None:
    boundary = (NOW // 86_400_000 + 1) * 86_400_000
    created_at_ms = boundary - 1_000
    _case(conn)
    repos = repositories_for_connection(conn)
    intent = _intent(created_at_ms=created_at_ms)
    assert _insert_test_intent(repos, intent)
    _allow_entry(conn)

    fence = _fence(repos, intent, engine_identity="nt-midnight", now_ms=boundary + 1_000)
    assert fence.granted
    risk = conn.execute(
        """
        SELECT reservation.risk_day_start_ms AS reservation_day_start_ms,
               state.attempt_day_start_ms, state.attempt_day_end_ms, state.status
          FROM trading_capital_risk_reservation_state state
          JOIN trading_capital_risk_reservations reservation
            ON reservation.reservation_sha256 = state.reservation_sha256
         WHERE state.intent_id = %s
        """,
        (intent.intent_id,),
    ).fetchone()
    assert dict(risk) == {
        "reservation_day_start_ms": boundary - 86_400_000,
        "attempt_day_start_ms": boundary,
        "attempt_day_end_ms": boundary + 86_400_000,
        "status": "FENCED",
    }


def test_fenced_authoritative_rejection_requires_the_exact_entry_and_zero_exposure(conn: Any) -> None:
    _case(conn)
    repos = repositories_for_connection(conn)
    intent = _intent()
    assert _insert_test_intent(repos, intent) is True
    _allow_entry(conn)
    fenced = _fence(repos, intent, engine_identity="nt-1", now_ms=NOW + 1_000).outcome
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
    assert _insert_test_intent(repos, intent) is True
    _allow_entry(conn)
    conn.commit()
    assert _fence(repos, intent, engine_identity="nt-1", now_ms=NOW + 1_000).granted
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
    assert _insert_test_intent(repos, intent) is True
    assert (
        repos.trading.mark_manual_review(
            intent.intent_id,
            reason_code="entry_outcome_unknown",
            now_ms=NOW + 500,
        )
        is None
    )
    _allow_entry(conn)
    assert _fence(repos, intent, engine_identity="nt-1", now_ms=NOW + 1_000).granted
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
    assert _insert_test_intent(repos, intent) is True
    _allow_entry(conn)
    assert _fence(repos, intent, engine_identity="nt-1", now_ms=NOW + 1_000).granted
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
        realized_pnl_amount=Decimal("0"),
        realized_pnl_currency="USDT",
        commissions_by_currency={},
        funding_by_currency={},
    )
    closed = repos.trading.record_closed_flat(
        intent.intent_id,
        position_id="position-1",
        authoritative_quantity=Decimal("0"),
        avg_exit_price=Decimal("60010"),
        closed_at_ms=NOW + 2_200,
        flat_verified_at_ms=NOW + 2_300,
        realized_pnl_amount=Decimal("0"),
        realized_pnl_currency="USDT",
        commissions_by_currency={},
        now_ms=NOW + 2_300,
        funding_by_currency={},
    )
    conn.commit()
    assert closed is not None
    assert (closed.execution_state, closed.terminal_outcome, closed.reason_code) == (
        "TERMINAL",
        "CLOSED_FLAT",
        None,
    )


def test_the_case_projection_links_its_intent_without_joining_the_execution_lifecycle(conn: Any) -> None:
    """#331: the Case aggregate answers with one nullable `intent_id`, never with an execution state."""

    _case(conn)
    repos = repositories_for_connection(conn)
    intent = _intent()
    assert _insert_test_intent(repos, intent) is True
    conn.commit()

    rows = repos.trading.console_cases(since_ms=NOW - 1, limit=10)

    row = next(item for item in rows if item["case_id"] == "case-intent-1")
    assert row["intent_id"] == intent.intent_id
    assert row["state"] == "RUNNING"
    assert row["strategy_id"] == "source_native_oi_smart_money_long_v3"
    for retired in ("execution_state", "terminal_outcome", "reason_code", "intent_version", "mode", "regime"):
        assert retired not in row


def _case_without_reset(connection: Any, *, case_id: str) -> None:
    connection.execute("UPDATE trading_cases SET state = 'ORDER_PREPARED' WHERE state = 'RUNNING'")
    connection.execute(
        """
        INSERT INTO trading_cases (
          case_id, underlying_key, trigger_kind, strategy_id, strategy_version,
          strategy_config_digest, primary_source_key, supplemental_source_keys,
          manifest, manifest_sha256, state, policy_decision, policy_reason,
          capital_disposition, capital_reason, observed_at_ms, created_at_ms, updated_at_ms
        ) VALUES (%s, 'crypto:BTC', 'oi', 'source_native_oi_smart_money_long_v3',
                  'source_native_oi_smart_money_long_v3', %s, %s, '[]'::jsonb,
                  '{}'::jsonb, %s, 'RUNNING', 'not_run', 'not_run',
                  'not_applicable', NULL, %s, %s, %s)
        """,
        (case_id, "0" * 64, f"source-{case_id}", "3" * 64, NOW, NOW, NOW),
    )
    connection.commit()


def test_fake_execution_closes_the_five_state_demo_loop_without_a_second_entry(conn: Any) -> None:
    _case(conn)
    intent = _intent()
    repos = repositories_for_connection(conn)
    assert _insert_test_intent(repos, intent) is True
    _allow_entry(conn)
    conn.commit()

    assert _fence(repos, intent, engine_identity="nt-1", now_ms=NOW + 1_000).granted
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
        funding_by_currency={},
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
            funding_by_currency={},
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
        funding_by_currency={},
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
    assert not _fence(repos, intent, engine_identity="nt-1", now_ms=NOW + 3_000).granted


def test_position_change_uses_a_new_deterministic_stop_generation(conn: Any) -> None:
    _case(conn)
    intent = _intent()
    repos = repositories_for_connection(conn)
    assert _insert_test_intent(repos, intent) is True
    _allow_entry(conn)
    conn.commit()
    assert _fence(repos, intent, engine_identity="nt-1", now_ms=NOW + 1_000).granted
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
