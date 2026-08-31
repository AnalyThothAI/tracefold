"""Small exact Production V3 values shared by Trading tests."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from tracefold.trading import (
    BINANCE_USDM_ADAPTER_CONTRACT_SHA256,
    BlacklistSnapshotV1,
    CapitalAuthorizationReceiptV1,
    CapitalRiskReservationV1,
    DailyRiskPolicyV1,
    ExecutionBindingV1,
    ExecutionCapabilitySnapshotV2,
    ExecutionInstrumentEvidenceV1,
    OperatorArmReceiptV1,
    ProductionPromotionGrantV1,
    SettlementRiskLimitV1,
    TradeIntent,
    VenueInstrumentCatalogEntryV1,
    VenueInstrumentCatalogSnapshotV1,
    build_execution_capability_snapshot,
    build_venue_catalog_snapshot,
    canonical_sha256,
)
from tracefold.trading.admission import AdmissionConfig
from tracefold.trading.capital_authority import risk_day_bounds
from tracefold.trading.catalog import prepare_venue_catalog_snapshot
from tracefold.trading.evidence_clock import (
    CandidateDecisionReceiptV1,
    CandidateExecutionProtocolV1,
    CandidateLockedV1,
    DiscoveryCorpusReceiptV1,
    FutureCaptureBatchV1,
    FutureCaptureReceiptV1,
    FutureDrainReceiptV1,
    FutureHoldoutMetricsV1,
    FutureHoldoutResultReceiptV1,
    FutureHoldoutResultV1,
    FutureStatisticalProtocolV1,
    candidate_selection_program_sha256,
    future_capture_health_summary,
    prepare_future_capture_batch,
)
from tracefold.trading.evidence_clock import (
    feature_contract_sha256 as evidence_feature_contract_sha256,
)
from tracefold.trading.evidence_clock import (
    source_contract_sha256 as evidence_source_contract_sha256,
)
from tracefold.trading.evidence_research import (
    BlindMarketHealthSummaryV1,
    FutureCollectorObservationV1,
    FutureWorkersObservationV1,
    build_future_capture_collection_health,
)
from tracefold.trading.execution_policy import EXECUTION_POLICY_SHA256, PROTECTION_CONTRACT_SHA256
from tracefold.trading.market_context import PriceWindow
from tracefold.trading.policy import CapitalPolicy
from tracefold.trading.quote_authority import QUOTE_CONTRACT_SHA256

NOW = 1_900_000_000_000
ADAPTER_SHA = BINANCE_USDM_ADAPTER_CONTRACT_SHA256
TEST_RELEASE = "test-release"
TEST_COST_MODEL = {
    "fee_model": {"version": "nautilus_bar_taker_fee_v1", "per_side_bps": "5"},
    "funding_model": {
        "version": "provider_history_replay_holding_v1",
        "mark_price": "first_5m_close_at_or_after_funding_event",
    },
    "spread_slippage_model": {"version": "additional_stress_bps_v1"},
    "latency_model": {"version": "first_closed_5m_bar_at_or_after_known_at_v1"},
    "additional_stressed_cost_bps": "2",
}
TEST_COST_MODEL_SHA256 = canonical_sha256(TEST_COST_MODEL)


def set_evidence_database_clock(repos: Any, now_ms: int) -> None:
    """Test-only PostgreSQL clock control; production roles cannot replace the owned function."""

    connection = getattr(repos, "conn", None)
    if connection is None:
        raise RuntimeError("test_evidence_database_connection_missing")
    connection.execute(
        "CREATE OR REPLACE FUNCTION trading_evidence_now_ms() RETURNS BIGINT "
        f"LANGUAGE sql VOLATILE AS 'SELECT {int(now_ms)}::BIGINT'"
    )


def _evidence_feature_contract_sha256() -> str:
    admission = AdmissionConfig()
    policy = CapitalPolicy()
    return evidence_feature_contract_sha256(
        admission_config_sha256=admission.digest,
        price_window=PriceWindow().as_dict(),
        policy_id=policy.policy_id,
        policy_config_sha256=policy.config_digest,
    )


def capital_evidence_fixture(
    *,
    source_contract_sha256: str = evidence_source_contract_sha256(),
    feature_contract_sha256: str = _evidence_feature_contract_sha256(),
    policy_config_sha256: str = CapitalPolicy().config_digest,
) -> tuple[
    DiscoveryCorpusReceiptV1,
    CandidateDecisionReceiptV1,
    CandidateLockedV1,
    FutureHoldoutResultReceiptV1,
    FutureHoldoutResultV1,
]:
    corpus = DiscoveryCorpusReceiptV1(
        corpus_sha256="4" * 64,
        artifact_sha256="4" * 64,
        artifact_path="test-evidence/discovery-corpus.json",
        capture_sha256="a" * 64,
        drain_sha256="b" * 64,
        execution_contract_receipt_sha256="c" * 64,
        source_count=1,
        created_at_ms=2,
    )
    execution = CandidateExecutionProtocolV1(
        intent_ttl_ms=60_000,
        target_notional="10",
        max_risk_amount="0.25",
        quote_contract_sha256=QUOTE_CONTRACT_SHA256,
        max_quote_age_ms=2_000,
        max_spread_bps=30,
        max_entry_drift_bps=25,
        stop_loss_bps=200,
        protection_contract_sha256=PROTECTION_CONTRACT_SHA256,
        max_holding_ms=180_000,
        execution_policy_sha256=EXECUTION_POLICY_SHA256,
        adapter_contract_sha256=ADAPTER_SHA,
        capability_requirements=("execution_eligible", "protection_eligible"),
        fee_model=TEST_COST_MODEL["fee_model"],
        funding_model=TEST_COST_MODEL["funding_model"],
        spread_slippage_model=TEST_COST_MODEL["spread_slippage_model"],
        latency_model=TEST_COST_MODEL["latency_model"],
        additional_stressed_cost_bps="2",
        cost_model_sha256=TEST_COST_MODEL_SHA256,
        benchmark="zero_return_bps_v1",
        max_symbol_concentration_bps=10_000,
        max_day_concentration_bps=10_000,
    )
    statistics = FutureStatisticalProtocolV1(
        future_start_ms=4,
        future_end_ms=300_004,
        capture_cutoff_ms=300_004,
        capture_interval_ms=300_000,
        maximum_capture_lag_ms=300_000,
        max_horizon_ms=1,
        data_finalization_lag_ms=1,
        drain_cutoff_ms=300_006,
        secondary_diagnostics=("concentration",),
        stressed_hurdle_bps="0",
        confidence_level_bps=9_500,
        bootstrap_block_days=1,
        bootstrap_samples=100,
        bootstrap_seed=377,
        minimum_effective_n=1,
        minimum_detectable_excess_bps=100,
        assumed_standard_deviation_bps=1,
        minimum_power_bps=9_000,
        minimum_coverage_bps=10_000,
        maximum_missingness_bps=0,
        incident_handling={
            "venue_provider_outage": "INSUFFICIENT_EVIDENCE",
            "source_mass_missingness": "INSUFFICIENT_EVIDENCE",
            "catalog_reset_or_delist": "INSUFFICIENT_EVIDENCE",
            "provider_correction": "INSUFFICIENT_EVIDENCE",
            "bar_or_funding_missing": "INSUFFICIENT_EVIDENCE",
            "protection_contract_invalid": "INSUFFICIENT_EVIDENCE",
            "clock_or_known_at_violation": "INSUFFICIENT_EVIDENCE",
        },
    )
    candidate = CandidateLockedV1(
        binding="BINANCE_USDM",
        venue="binance.usdm",
        sealed_corpus_sha256=corpus.corpus_sha256,
        corpus_artifact_sha256=corpus.artifact_sha256,
        discovery_start_ms=1,
        discovery_end_ms=2,
        source_contract_sha256=source_contract_sha256,
        feature_contract_sha256=feature_contract_sha256,
        point_in_time_catalog_sha256="d" * 64,
        eligible_universe_sha256="e" * 64,
        selection_program_sha256=candidate_selection_program_sha256(),
        selection_evidence_sha256="9" * 64,
        policy_id="source_native_oi_smart_money_long_v3",
        policy_config_sha256=policy_config_sha256,
        execution_contract_receipt_sha256="c" * 64,
        execution=execution,
        statistics=statistics,
        evaluator_program_sha256="f" * 64,
        locked_at_ms=3,
        preregistered_by="test-suite",
    )
    candidate_receipt = CandidateDecisionReceiptV1(
        terminal="CANDIDATE_LOCKED",
        binding=candidate.binding,
        sealed_corpus_sha256=candidate.sealed_corpus_sha256,
        artifact_sha256=candidate.protocol_sha256,
        artifact_path="test-evidence/candidate.json",
        protocol_sha256=candidate.protocol_sha256,
        created_at_ms=3,
    )
    metrics = FutureHoldoutMetricsV1(
        source_count=1,
        effective_n=1,
        estimated_power_bps=10_000,
        coverage_bps=10_000,
        missingness_bps=0,
        mean_net_including_funding_return_bps="10",
        benchmark_excess_bps="10",
        primary_confidence_lower_bound_bps="1",
        mfe_bps={"p50": "10"},
        mae_bps={"p50": "-1"},
        max_drawdown_bps="1",
        tail_loss_bps="1",
        turnover="1",
        capacity_proxy="1",
        concentration_bps={"symbol": 10_000, "day": 10_000},
        missing_by_reason={},
        sensitivity={"double_cost": "1"},
    )
    result = FutureHoldoutResultV1(
        terminal="PROMOTE",
        binding="BINANCE_USDM",
        venue="binance.usdm",
        candidate_receipt_sha256=candidate_receipt.receipt_sha256,
        protocol_sha256=candidate.protocol_sha256,
        sealed_corpus_sha256=corpus.corpus_sha256,
        future_capture_sha256="6" * 64,
        future_drain_sha256="7" * 64,
        evaluator_program_sha256=candidate.evaluator_program_sha256,
        evaluated_at_ms=300_006,
        metrics=metrics,
        reasons=("confidence_lower_bound_above_hurdle",),
    )
    result_receipt = FutureHoldoutResultReceiptV1(
        terminal="PROMOTE",
        binding="BINANCE_USDM",
        candidate_receipt_sha256=candidate_receipt.receipt_sha256,
        protocol_sha256=candidate.protocol_sha256,
        sealed_corpus_sha256=corpus.corpus_sha256,
        report_sha256=result.report_sha256,
        artifact_sha256=result.report_sha256,
        artifact_path="test-evidence/future-result.json",
        created_at_ms=300_006,
    )
    return corpus, candidate_receipt, candidate, result_receipt, result


def append_capital_evidence_fixture(
    repos: Any,
    *,
    source_contract_sha256: str = evidence_source_contract_sha256(),
    feature_contract_sha256: str = _evidence_feature_contract_sha256(),
    policy_config_sha256: str = CapitalPolicy().config_digest,
) -> FutureHoldoutResultV1:
    trading = getattr(repos, "trading", repos)
    corpus, candidate_receipt, candidate, result_receipt, result = capital_evidence_fixture(
        source_contract_sha256=source_contract_sha256,
        feature_contract_sha256=feature_contract_sha256,
        policy_config_sha256=policy_config_sha256,
    )
    set_evidence_database_clock(trading, 2)
    trading.append_discovery_corpus_receipt(corpus)
    set_evidence_database_clock(trading, 3)
    trading.append_candidate_decision_receipt(candidate_receipt, candidate)
    health = build_future_capture_collection_health(
        (),
        collector=FutureCollectorObservationV1(
            connected=True,
            last_frame_at_ms=candidate.statistics.future_end_ms,
            last_error_code=None,
            expected_source_count=0,
            batch_end_ms=candidate.statistics.future_end_ms,
        ),
        workers=FutureWorkersObservationV1(
            lifecycle_state="running",
            heartbeat_at_ms=candidate.statistics.future_end_ms,
        ),
        market=BlindMarketHealthSummaryV1(
            market_instrument_count=0,
            bar_continuous_count=0,
            funding_probe_ok_count=0,
        ),
    )
    batch = FutureCaptureBatchV1(
        binding=candidate.binding,
        candidate_receipt_sha256=candidate_receipt.receipt_sha256,
        protocol_sha256=candidate.protocol_sha256,
        batch_start_ms=candidate.statistics.future_start_ms,
        batch_end_ms=candidate.statistics.future_end_ms,
        captured_at_ms=candidate.statistics.future_end_ms,
        capture_lag_ms=0,
        sources=(),
        source_count=0,
        late_source_count=0,
        catalog_missing_count=0,
        health=health,
    )
    set_evidence_database_clock(trading, 300_004)
    trading.append_future_capture_batch(prepare_future_capture_batch(batch))
    batch_health_sha256, collection_incidents = future_capture_health_summary(
        (batch,), maximum_missingness_bps=candidate.statistics.maximum_missingness_bps
    )
    capture_receipt = FutureCaptureReceiptV1(
        binding=candidate.binding,
        candidate_receipt_sha256=candidate_receipt.receipt_sha256,
        protocol_sha256=candidate.protocol_sha256,
        sealed_corpus_sha256=candidate.sealed_corpus_sha256,
        capture_sha256=result.future_capture_sha256,
        artifact_sha256=result.future_capture_sha256,
        artifact_path="test-evidence/future-capture.json",
        batch_count=1,
        batch_health_sha256=batch_health_sha256,
        collection_incidents=collection_incidents,
        created_at_ms=300_004,
    )
    set_evidence_database_clock(trading, 300_004)
    trading.append_future_capture_receipt(capture_receipt)
    set_evidence_database_clock(trading, 300_005)
    trading.append_future_drain_receipt(
        FutureDrainReceiptV1(
            binding=candidate.binding,
            candidate_receipt_sha256=candidate_receipt.receipt_sha256,
            capture_receipt_sha256=capture_receipt.receipt_sha256,
            protocol_sha256=candidate.protocol_sha256,
            sealed_corpus_sha256=candidate.sealed_corpus_sha256,
            capture_sha256=result.future_capture_sha256,
            drain_sha256=result.future_drain_sha256,
            artifact_sha256=result.future_drain_sha256,
            artifact_path="test-evidence/future-drain.json",
            created_at_ms=300_005,
        )
    )
    set_evidence_database_clock(trading, 300_006)
    trading.append_future_holdout_result_receipt(result_receipt, result)
    return result


def capital_risk_policy_fixture() -> DailyRiskPolicyV1:
    return DailyRiskPolicyV1(
        approved_release=TEST_RELEASE,
        cost_model_sha256=TEST_COST_MODEL_SHA256,
        max_committed_entry_attempts=100,
        max_target_notional=Decimal("10"),
        settlement_limits=(
            SettlementRiskLimitV1(
                settlement_asset="USDT",
                max_planned_risk_amount=Decimal("100"),
                max_realized_loss_amount=Decimal("100"),
                fee_slippage_reserve_bps=50,
            ),
        ),
        issuer="test-suite",
        issued_at_ms=1,
        effective_from_ms=1,
        expires_at_ms=4_000_000_000_000,
    )


def capital_grant_fixture(
    *,
    catalog: VenueInstrumentCatalogSnapshotV1,
    capability: ExecutionCapabilitySnapshotV2,
    binding: ExecutionBindingV1,
    allowed_capability_entry_id: str | None = None,
) -> ProductionPromotionGrantV1:
    policy = capital_risk_policy_fixture()
    future_result = capital_evidence_fixture()[-1]
    allowed_entry_id = allowed_capability_entry_id or sorted(capability.included)[0]
    return ProductionPromotionGrantV1(
        binding="BINANCE_USDM",
        venue="binance.usdm",
        source_contract_sha256=evidence_source_contract_sha256(),
        feature_contract_sha256=_evidence_feature_contract_sha256(),
        policy_id="source_native_oi_smart_money_long_v3",
        policy_config_sha256=CapitalPolicy().config_digest,
        cost_model_sha256=policy.cost_model_sha256,
        catalog_snapshot_sha256=catalog.snapshot_sha256,
        capability_snapshot_sha256=capability.snapshot_sha256,
        execution_binding_sha256=binding.binding_sha256,
        adapter_contract_sha256=binding.adapter_contract_sha256,
        execution_policy_sha256=EXECUTION_POLICY_SHA256,
        quote_contract_sha256=binding.quote_contract_sha256,
        protection_contract_sha256=binding.protection_contract_sha256,
        sealed_corpus_sha256="4" * 64,
        locked_future_report_sha256=future_result.report_sha256,
        risk_policy_sha256=policy.risk_policy_sha256,
        approved_release=TEST_RELEASE,
        allowed_capability_entry_ids=(allowed_entry_id,),
        max_target_notional=Decimal("10"),
        approver="test-suite",
        issued_at_ms=1,
        review_at_ms=3_999_999_999_999,
        expires_at_ms=4_000_000_000_000,
    )


def capital_arm_fixture(
    *,
    catalog: VenueInstrumentCatalogSnapshotV1,
    capability: ExecutionCapabilitySnapshotV2,
    binding: ExecutionBindingV1,
    allowed_capability_entry_id: str | None = None,
) -> OperatorArmReceiptV1:
    policy = capital_risk_policy_fixture()
    grant = capital_grant_fixture(
        catalog=catalog,
        capability=capability,
        binding=binding,
        allowed_capability_entry_id=allowed_capability_entry_id,
    )
    return OperatorArmReceiptV1(
        arm_epoch=1,
        binding="BINANCE_USDM",
        venue="binance.usdm",
        approved_release=TEST_RELEASE,
        account_generation=binding.account_generation,
        credential_fingerprint=binding.credential_fingerprint,
        catalog_snapshot_sha256=catalog.snapshot_sha256,
        capability_snapshot_sha256=capability.snapshot_sha256,
        execution_binding_sha256=binding.binding_sha256,
        grant_sha256=grant.grant_sha256,
        risk_policy_sha256=policy.risk_policy_sha256,
        reconciliation_receipt_sha256="6" * 64,
        reconciled_at_ms=1,
        operator="test-suite",
        armed_at_ms=2,
        expires_at_ms=4_000_000_000_000,
    )


def capital_bundle_fixture(
    intent: TradeIntent,
    *,
    catalog: VenueInstrumentCatalogSnapshotV1,
    capability: ExecutionCapabilitySnapshotV2,
    binding: ExecutionBindingV1,
) -> tuple[CapitalRiskReservationV1, CapitalAuthorizationReceiptV1]:
    policy = capital_risk_policy_fixture()
    grant = capital_grant_fixture(
        catalog=catalog,
        capability=capability,
        binding=binding,
        allowed_capability_entry_id=intent.capability_entry_id,
    )
    arm = capital_arm_fixture(
        catalog=catalog,
        capability=capability,
        binding=binding,
        allowed_capability_entry_id=intent.capability_entry_id,
    )
    day_start, day_end = risk_day_bounds(intent.created_at_ms)
    reservation = CapitalRiskReservationV1(
        case_id=intent.case_id,
        source_identity=intent.source_identity,
        economic_lifecycle_id=intent.economic_lifecycle_id,
        binding=intent.binding,
        settlement_asset="USDT",
        risk_policy_sha256=policy.risk_policy_sha256,
        grant_sha256=grant.grant_sha256,
        arm_receipt_sha256=arm.arm_receipt_sha256,
        risk_day_start_ms=day_start,
        risk_day_end_ms=day_end,
        target_notional=intent.target_notional,
        planned_stop_risk_amount=intent.target_notional * Decimal("0.02"),
        fee_slippage_reserve_amount=intent.target_notional * Decimal("0.005"),
        planned_risk_amount=intent.target_notional * Decimal("0.025"),
        created_at_ms=intent.created_at_ms,
    )
    receipt = CapitalAuthorizationReceiptV1(
        case_id=intent.case_id,
        reservation_sha256=reservation.reservation_sha256,
        binding=intent.binding,
        account_generation=intent.account_generation,
        execution_binding_sha256=intent.execution_binding_sha256,
        grant_sha256=grant.grant_sha256,
        arm_receipt_sha256=arm.arm_receipt_sha256,
        risk_policy_sha256=policy.risk_policy_sha256,
        risk_day_start_ms=day_start,
        risk_day_end_ms=day_end,
        settlement_asset="USDT",
        committed_attempts_before=0,
        committed_attempts_limit=policy.max_committed_entry_attempts,
        open_planned_risk_before=Decimal("0"),
        open_planned_risk_after=reservation.planned_risk_amount,
        planned_risk_limit=Decimal("100"),
        realized_loss_to_date=Decimal("0"),
        realized_loss_limit=Decimal("100"),
        approved_release=TEST_RELEASE,
        evaluated_at_ms=intent.created_at_ms,
    )
    return reservation, receipt


def binance_catalog(
    *,
    captured_at_ms: int = NOW,
    symbol: str = "SOLUSDT",
    symbols: Sequence[str] | None = None,
) -> VenueInstrumentCatalogSnapshotV1:
    selected = tuple(symbols or (symbol,))
    rows = tuple(
        VenueInstrumentCatalogEntryV1(
            provider_instrument_id=value,
            provider_symbol=value,
            venue="binance.usdm",
            canonical_asset=value.removesuffix("USDT"),
            canonical_namespace="native",
            product_kind="linear_perpetual",
            active=True,
            settlement_asset="USDT",
            margin_asset="USDT",
            multiplier="1",
            price_increment="0.01",
            size_increment="0.001",
            min_quantity="0.001",
            min_notional="5",
            raw_metadata_sha256=f"{index:x}" * 64,
        )
        for index, value in enumerate(selected, start=1)
    )
    return build_venue_catalog_snapshot(
        binding="BINANCE_USDM",
        captured_at_ms=captured_at_ms,
        stale_after_ms=60_000,
        instruments=rows,
    )


def store_catalog_fixture(storage: Any, snapshot: VenueInstrumentCatalogSnapshotV1, *, now_ms: int) -> None:
    """Persist a catalog directly when an integration fixture does not exercise `VenueCatalog`."""

    storage.store_venue_catalog_snapshot(
        prepared=prepare_venue_catalog_snapshot(snapshot),
        now_ms=now_ms,
    )


def binance_capability(
    *,
    catalog: VenueInstrumentCatalogSnapshotV1 | None = None,
    app_revision: str = "revision",
    symbol: str = "SOLUSDT",
    symbols: Sequence[str] | None = None,
    adapter_contract_sha256: str = BINANCE_USDM_ADAPTER_CONTRACT_SHA256,
) -> ExecutionCapabilitySnapshotV2:
    catalog = catalog or binance_catalog(symbol=symbol, symbols=symbols)
    return build_execution_capability_snapshot(
        catalog=catalog,
        execution_rows=[
            ExecutionInstrumentEvidenceV1(
                provider_instrument_id=row.provider_instrument_id,
                catalog_raw_metadata_sha256=row.raw_metadata_sha256,
                instrument_id=f"{row.provider_symbol}-PERP.BINANCE",
                native_symbol=row.provider_symbol,
                price_precision=2,
                size_precision=3,
                price_increment="0.01",
                size_increment="0.001",
                min_quantity="0.001",
                min_notional="5",
                execution_eligible=True,
                protection_eligible=True,
            )
            for row in catalog.instruments
        ],
        app_revision=app_revision,
        app_image_digest="sha256:image",
        adapter_contract_sha256=adapter_contract_sha256,
        quote_contract_sha256=QUOTE_CONTRACT_SHA256,
        protection_contract_sha256=PROTECTION_CONTRACT_SHA256,
        client_runtime_identity="nautilus-trader==1.231.0",
    )


def binance_binding(
    *,
    catalog: VenueInstrumentCatalogSnapshotV1 | None = None,
    capability: ExecutionCapabilitySnapshotV2 | None = None,
    created_at_ms: int = NOW,
) -> ExecutionBindingV1:
    catalog = catalog or binance_catalog(captured_at_ms=created_at_ms)
    capability = capability or binance_capability(catalog=catalog)
    return ExecutionBindingV1(
        binding="BINANCE_USDM",
        venue="binance.usdm",
        account_identity_sha256="c" * 64,
        account_generation=1,
        credential_fingerprint="d" * 64,
        catalog_snapshot_sha256=catalog.snapshot_sha256,
        capability_snapshot_sha256=capability.snapshot_sha256,
        adapter_contract_sha256=capability.adapter_contract_sha256,
        quote_contract_sha256=capability.quote_contract_sha256,
        protection_contract_sha256=capability.protection_contract_sha256,
        client_runtime_identity=capability.client_runtime_identity,
        created_at_ms=created_at_ms,
    )


def trade_intent(
    *,
    case_id: str = "case-1",
    case_manifest_sha256: str = "1" * 64,
    catalog: VenueInstrumentCatalogSnapshotV1 | None = None,
    capability: ExecutionCapabilitySnapshotV2 | None = None,
    binding: ExecutionBindingV1 | None = None,
    provider_instrument_id: str = "SOLUSDT",
    created_at_ms: int = NOW,
    **overrides: Any,
) -> TradeIntent:
    catalog = catalog or binance_catalog(captured_at_ms=created_at_ms)
    capability = capability or binance_capability(catalog=catalog)
    binding = binding or binance_binding(catalog=catalog, capability=capability, created_at_ms=created_at_ms)
    instrument = next(
        row for row in capability.included.values() if row.provider_instrument_id == provider_instrument_id
    )
    values: dict[str, Any] = {
        "case_id": case_id,
        "case_manifest_sha256": case_manifest_sha256,
        "source_venue": "binance.usdm",
        "source_identity": f"oi:{case_id}:oi_signal_v1",
        "canonical_asset": instrument.canonical_asset,
        "binding": "BINANCE_USDM",
        "account_generation": binding.account_generation,
        "execution_binding_sha256": binding.binding_sha256,
        "venue_catalog_snapshot_sha256": catalog.snapshot_sha256,
        "execution_capability_snapshot_sha256": capability.snapshot_sha256,
        "capability_entry_id": instrument.catalog_entry_id,
        "provider_instrument_id": instrument.provider_instrument_id,
        "instrument_id": instrument.instrument_id,
        "settlement_asset": instrument.settlement_asset,
        "capital_authorization_receipt_sha256": "0" * 64,
        "blacklist_snapshot": BlacklistSnapshotV1(revision=0, active_rows=()),
        "created_at_ms": created_at_ms,
        "reference_price": Decimal("200"),
        "target_notional": Decimal("10"),
        "max_risk_amount": Decimal("0.25"),
        "risk_currency": "USDT",
    }
    selected = values | overrides
    if "capital_authorization_receipt_sha256" not in overrides:
        provisional = TradeIntent.create(**selected)
        _, receipt = capital_bundle_fixture(
            provisional,
            catalog=catalog,
            capability=capability,
            binding=binding,
        )
        selected["capital_authorization_receipt_sha256"] = receipt.authorization_receipt_sha256
    return TradeIntent.create(**selected)


__all__ = [
    "ADAPTER_SHA",
    "NOW",
    "append_capital_evidence_fixture",
    "binance_binding",
    "binance_capability",
    "binance_catalog",
    "capital_arm_fixture",
    "capital_bundle_fixture",
    "capital_evidence_fixture",
    "capital_grant_fixture",
    "capital_risk_policy_fixture",
    "set_evidence_database_clock",
    "trade_intent",
]
