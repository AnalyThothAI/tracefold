from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from tracefold.trading import (
    ExecutionCapabilitySnapshotV2,
    ExecutionInstrumentEvidenceV1,
    VenueBinding,
    VenueInstrumentCatalogEntryV1,
    build_execution_capability_snapshot,
    build_venue_catalog_snapshot,
)

SHA = "a" * 64
IDENTITIES = {
    "app_revision": "revision",
    "app_image_digest": "sha256:image",
    "adapter_contract_sha256": "1" * 64,
    "quote_contract_sha256": "2" * 64,
    "protection_contract_sha256": "3" * 64,
    "client_runtime_identity": "client-runtime",
}


def _catalog_row(
    provider_id: str = "BTCUSDT",
    *,
    binding: VenueBinding = "BINANCE_USDM",
    raw_sha: str = SHA,
    **overrides: Any,
) -> VenueInstrumentCatalogEntryV1:
    values: dict[str, Any] = {
        "provider_instrument_id": provider_id,
        "provider_symbol": "BTCUSDT" if binding == "BINANCE_USDM" else "BTC",
        "venue": "binance.usdm" if binding == "BINANCE_USDM" else "hyperliquid.perp",
        "canonical_asset": "BTC",
        "canonical_namespace": "native" if binding == "BINANCE_USDM" else "main",
        "product_kind": "linear_perpetual",
        "active": True,
        "settlement_asset": "USDT" if binding == "BINANCE_USDM" else "USDC",
        "margin_asset": "USDT" if binding == "BINANCE_USDM" else "USDC",
        "multiplier": "1",
        "price_increment": "0.1",
        "size_increment": "0.001",
        "min_quantity": "0.001",
        "min_notional": "5",
        "raw_metadata_sha256": raw_sha,
    }
    return VenueInstrumentCatalogEntryV1.model_validate(values | overrides)


def _evidence(row: VenueInstrumentCatalogEntryV1, **overrides: Any) -> ExecutionInstrumentEvidenceV1:
    values: dict[str, Any] = {
        "provider_instrument_id": row.provider_instrument_id,
        "catalog_raw_metadata_sha256": row.raw_metadata_sha256,
        "instrument_id": (
            f"{row.provider_symbol}-PERP.BINANCE"
            if row.venue == "binance.usdm"
            else f"{row.provider_symbol}-PERP.HYPERLIQUID"
        ),
        "native_symbol": row.provider_symbol,
        "price_precision": 1,
        "size_precision": 3,
        "price_increment": "0.1",
        "size_increment": "0.001",
        "min_quantity": "0.001",
        "min_notional": "5",
        "execution_eligible": True,
        "protection_eligible": True,
    }
    return ExecutionInstrumentEvidenceV1.model_validate(values | overrides)


def _build(
    rows: list[VenueInstrumentCatalogEntryV1],
    evidence: list[ExecutionInstrumentEvidenceV1],
    *,
    binding: VenueBinding = "BINANCE_USDM",
) -> ExecutionCapabilitySnapshotV2:
    catalog = build_venue_catalog_snapshot(
        binding=binding,
        captured_at_ms=1_000,
        stale_after_ms=10_000,
        instruments=rows,
    )
    return build_execution_capability_snapshot(catalog=catalog, execution_rows=evidence, **IDENTITIES)


@pytest.mark.parametrize("binding", ["BINANCE_USDM", "HYPERLIQUID_PERP"])
def test_each_closed_binding_compiles_its_own_source_native_partition(binding: VenueBinding) -> None:
    row = _catalog_row(
        "BTCUSDT" if binding == "BINANCE_USDM" else "main:BTC",
        binding=binding,
    )

    snapshot = _build([row], [_evidence(row)], binding=binding)

    capability = next(iter(snapshot.included.values()))
    assert snapshot.binding == binding
    assert snapshot.venue == ("binance.usdm" if binding == "BINANCE_USDM" else "hyperliquid.perp")
    assert capability.binding == binding
    assert capability.canonical_asset == "BTC"
    assert snapshot.catalog_instrument_count == snapshot.included_count + snapshot.excluded_count == 1


def test_every_catalog_occurrence_has_one_visible_disposition() -> None:
    included = _catalog_row()
    missing_evidence = _catalog_row("ETHUSDT", raw_sha="b" * 64, provider_symbol="ETHUSDT", canonical_asset="ETH")
    unprotected = _catalog_row("SOLUSDT", raw_sha="c" * 64, provider_symbol="SOLUSDT", canonical_asset="SOL")
    duplicate_a = _catalog_row("DOGEUSDT", raw_sha="d" * 64, provider_symbol="DOGEUSDT", canonical_asset="DOGE")
    duplicate_b = _catalog_row("DOGEUSDT", raw_sha="e" * 64, provider_symbol="DOGEUSDT", canonical_asset="DOGE")

    snapshot = _build(
        [included, missing_evidence, unprotected, duplicate_a, duplicate_b],
        [_evidence(included), _evidence(unprotected, protection_eligible=False)],
    )

    assert snapshot.catalog_instrument_count == 5
    assert snapshot.included_count == 1
    assert snapshot.excluded_count == 4
    assert sorted(row.reason for row in snapshot.excluded.values()) == [
        "ADAPTER_EVIDENCE_MISSING",
        "DUPLICATE_PROVIDER_INSTRUMENT",
        "DUPLICATE_PROVIDER_INSTRUMENT",
        "PROTECTION_CONTRACT_UNPROVEN",
    ]


def test_partition_and_snapshot_are_provider_order_independent() -> None:
    btc = _catalog_row()
    eth = _catalog_row("ETHUSDT", raw_sha="b" * 64, provider_symbol="ETHUSDT", canonical_asset="ETH")

    forward = _build([btc, eth], [_evidence(btc), _evidence(eth)])
    reverse = _build([eth, btc], [_evidence(eth), _evidence(btc)])

    assert forward == reverse
    assert forward.snapshot_sha256 == reverse.snapshot_sha256


def test_zero_included_is_an_honest_complete_partition() -> None:
    row = _catalog_row()

    snapshot = _build([row], [_evidence(row, protection_eligible=False)])

    assert snapshot.included_count == 0
    assert snapshot.excluded_count == 1
    assert next(iter(snapshot.excluded.values())).reason == "PROTECTION_CONTRACT_UNPROVEN"


def test_conflicting_adapter_evidence_fails_closed() -> None:
    row = _catalog_row()

    with pytest.raises(ValueError, match="execution_capability_evidence_conflict:BTCUSDT"):
        _build([row], [_evidence(row), _evidence(row, instrument_id="other")])


def test_snapshot_rejects_manufactured_conservation_or_partition_digest() -> None:
    row = _catalog_row()
    snapshot = _build([row], [_evidence(row)])
    values = snapshot.model_dump(mode="json")

    with pytest.raises(ValidationError, match="execution_capability_snapshot_conservation_failed"):
        ExecutionCapabilitySnapshotV2.model_validate(values | {"catalog_instrument_count": 2})
    with pytest.raises(ValidationError, match="execution_capability_partition_identity_invalid"):
        ExecutionCapabilitySnapshotV2.model_validate(values | {"partition_sha256": "f" * 64})


@pytest.mark.parametrize("field", ["price_increment", "size_increment"])
@pytest.mark.parametrize("value", ["0", "-1", "nan", ""])
def test_unusable_mechanical_increment_never_enters_adapter_evidence(field: str, value: str) -> None:
    row = _catalog_row()

    with pytest.raises(ValidationError, match=r"execution_capability_.*_invalid"):
        _evidence(row, **{field: value})
