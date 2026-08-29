"""Small exact Production V3 values shared by Trading tests."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from tracefold.trading import (
    BlacklistSnapshotV1,
    ExecutionBindingV1,
    ExecutionCapabilitySnapshotV2,
    ExecutionInstrumentEvidenceV1,
    TradeIntent,
    VenueInstrumentCatalogEntryV1,
    VenueInstrumentCatalogSnapshotV1,
    build_execution_capability_snapshot,
    build_venue_catalog_snapshot,
)
from tracefold.trading.execution_policy import PROTECTION_CONTRACT_SHA256
from tracefold.trading.quote_authority import QUOTE_CONTRACT_SHA256

NOW = 1_900_000_000_000
ADAPTER_SHA = "a" * 64


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
        "capital_authorization_receipt_sha256": "e" * 64,
        "blacklist_snapshot": BlacklistSnapshotV1(revision=0, active_rows=()),
        "created_at_ms": created_at_ms,
        "reference_price": Decimal("200"),
        "target_notional": Decimal("10"),
        "max_risk_amount": Decimal("0.25"),
        "risk_currency": "USDT",
    }
    return TradeIntent.create(**(values | overrides))


__all__ = [
    "ADAPTER_SHA",
    "NOW",
    "binance_binding",
    "binance_capability",
    "binance_catalog",
    "trade_intent",
]
