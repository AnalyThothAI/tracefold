"""Small exact Production V3 values shared by Trading tests."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from tracefold.trading import (
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
from tracefold.trading.capital_authority import risk_day_bounds
from tracefold.trading.execution_policy import EXECUTION_POLICY_SHA256, PROTECTION_CONTRACT_SHA256
from tracefold.trading.quote_authority import QUOTE_CONTRACT_SHA256

NOW = 1_900_000_000_000
ADAPTER_SHA = "a" * 64
TEST_RELEASE = "test-release"
TEST_COST_MODEL_SHA256 = canonical_sha256({"version": "test_cost_model_v1"})


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
) -> ProductionPromotionGrantV1:
    policy = capital_risk_policy_fixture()
    return ProductionPromotionGrantV1(
        binding="BINANCE_USDM",
        venue="binance.usdm",
        source_contract_sha256="1" * 64,
        feature_contract_sha256="2" * 64,
        policy_id="source_native_oi_smart_money_long_v3",
        policy_config_sha256="3" * 64,
        cost_model_sha256=policy.cost_model_sha256,
        catalog_snapshot_sha256=catalog.snapshot_sha256,
        capability_snapshot_sha256=capability.snapshot_sha256,
        execution_binding_sha256=binding.binding_sha256,
        adapter_contract_sha256=binding.adapter_contract_sha256,
        execution_policy_sha256=EXECUTION_POLICY_SHA256,
        quote_contract_sha256=binding.quote_contract_sha256,
        protection_contract_sha256=binding.protection_contract_sha256,
        sealed_corpus_sha256="4" * 64,
        locked_future_report_sha256="5" * 64,
        risk_policy_sha256=policy.risk_policy_sha256,
        approved_release=TEST_RELEASE,
        allowed_capability_entry_ids=tuple(sorted(capability.included)),
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
) -> OperatorArmReceiptV1:
    policy = capital_risk_policy_fixture()
    grant = capital_grant_fixture(catalog=catalog, capability=capability, binding=binding)
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
    grant = capital_grant_fixture(catalog=catalog, capability=capability, binding=binding)
    arm = capital_arm_fixture(catalog=catalog, capability=capability, binding=binding)
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


def binance_capability(
    *,
    catalog: VenueInstrumentCatalogSnapshotV1 | None = None,
    app_revision: str = "revision",
    symbol: str = "SOLUSDT",
    symbols: Sequence[str] | None = None,
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
        adapter_contract_sha256=ADAPTER_SHA,
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
    "binance_binding",
    "binance_capability",
    "binance_catalog",
    "capital_arm_fixture",
    "capital_bundle_fixture",
    "capital_grant_fixture",
    "capital_risk_policy_fixture",
    "trade_intent",
]
