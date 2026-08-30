"""Production V3 execution capability compiler.

The public venue catalogue is complete provider truth. This module compiles every catalogue row
against one closed binding's exact adapter evidence and produces one immutable included/excluded
partition. Missing, malformed, inactive, unsupported, or unprotected instruments remain visible as
typed exclusions; a static allow-list cannot make them disappear.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .bindings import ExecutionVenue, venue_for_binding
from .catalog import VenueInstrumentCatalogEntryV1, VenueInstrumentCatalogSnapshotV1
from .contracts import VenueBinding, canonical_sha256, underlying_key

EXECUTION_CAPABILITY_SNAPSHOT_VERSION: Final[Literal["execution_capability_snapshot_v2"]] = (
    "execution_capability_snapshot_v2"
)
SUPPORTED_SETTLEMENT_ASSETS: Final[frozenset[str]] = frozenset({"USDT", "USDC"})

CapabilityExclusionReason = Literal[
    "CATALOG_NORMALIZATION_FAILED",
    "DUPLICATE_PROVIDER_INSTRUMENT",
    "INACTIVE",
    "PRODUCT_UNSUPPORTED",
    "SETTLEMENT_ASSET_UNSUPPORTED",
    "ADAPTER_EVIDENCE_MISSING",
    "ADAPTER_EVIDENCE_CONFLICT",
    "INSTRUMENT_IDENTITY_MISMATCH",
    "PRECISION_CONTRACT_UNPROVEN",
    "EXECUTION_CONTRACT_UNPROVEN",
    "PROTECTION_CONTRACT_UNPROVEN",
]


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _positive_decimal(value: str | None, field: str) -> Decimal:
    try:
        parsed = Decimal(value or "")
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"execution_capability_{field}_invalid") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"execution_capability_{field}_invalid")
    return parsed


def _nonnegative_decimal(value: str | None, field: str) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"execution_capability_{field}_invalid") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"execution_capability_{field}_invalid")
    return parsed


class ExecutionInstrumentEvidenceV1(_Frozen):
    """Exact client/runtime evidence for one row in one public catalogue snapshot."""

    evidence_version: Literal["execution_instrument_evidence_v1"] = "execution_instrument_evidence_v1"
    provider_instrument_id: str = Field(min_length=1)
    catalog_raw_metadata_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    instrument_id: str = Field(min_length=1)
    native_symbol: str = Field(min_length=1)
    price_precision: int = Field(ge=0)
    size_precision: int = Field(ge=0)
    price_increment: str
    size_increment: str
    min_quantity: str | None = None
    min_notional: str | None = None
    execution_eligible: bool
    protection_eligible: bool
    error: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def validate_mechanics(self) -> Self:
        if self.error is None:
            _positive_decimal(self.price_increment, "price_increment")
            _positive_decimal(self.size_increment, "size_increment")
            _nonnegative_decimal(self.min_quantity, "min_quantity")
            _nonnegative_decimal(self.min_notional, "min_notional")
        return self


class ExecutionInstrumentCapabilityV2(_Frozen):
    catalog_entry_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_instrument_id: str = Field(min_length=1)
    instrument_id: str = Field(min_length=1)
    native_symbol: str = Field(min_length=1)
    binding: VenueBinding
    venue: ExecutionVenue
    canonical_asset: str = Field(min_length=1, max_length=32)
    canonical_namespace: str = Field(min_length=1, max_length=64)
    underlying_key: str = Field(pattern=r"^crypto:[A-Z0-9][A-Z0-9._-]{0,31}$")
    settlement_asset: str
    price_precision: int = Field(ge=0)
    size_precision: int = Field(ge=0)
    price_increment: str
    size_increment: str
    min_quantity: str | None = None
    min_notional: str | None = None
    execution_eligible: Literal[True] = True
    protection_eligible: Literal[True] = True

    @field_validator("price_increment", "size_increment")
    @classmethod
    def validate_increment(cls, value: str) -> str:
        _positive_decimal(value, "increment")
        return value

    @model_validator(mode="after")
    def binding_matches_venue(self) -> Self:
        if self.venue != venue_for_binding(self.binding):
            raise ValueError("execution_capability_binding_venue_mismatch")
        if self.underlying_key != underlying_key(self.canonical_asset):
            raise ValueError("execution_capability_underlying_mismatch")
        return self


class ExecutionCapabilityExclusionV2(_Frozen):
    catalog_entry_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_instrument_id: str = Field(min_length=1)
    reason: CapabilityExclusionReason
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ExecutionCapabilitySnapshotV2(_Frozen):
    snapshot_version: Literal["execution_capability_snapshot_v2"] = EXECUTION_CAPABILITY_SNAPSHOT_VERSION
    binding: VenueBinding
    venue: ExecutionVenue
    catalog_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    catalog_instrument_count: int = Field(ge=0)
    included_count: int = Field(ge=0)
    excluded_count: int = Field(ge=0)
    partition_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    app_revision: str = Field(min_length=1, max_length=128)
    app_image_digest: str = Field(min_length=1, max_length=256)
    adapter_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    quote_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    protection_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    client_runtime_identity: str = Field(min_length=1, max_length=256)
    included: dict[str, ExecutionInstrumentCapabilityV2]
    excluded: dict[str, ExecutionCapabilityExclusionV2]

    @model_validator(mode="after")
    def validate_partition(self) -> Self:
        if self.venue != venue_for_binding(self.binding):
            raise ValueError("execution_capability_binding_venue_mismatch")
        if set(self.included).intersection(self.excluded):
            raise ValueError("execution_capability_snapshot_overlap")
        if any(key != value.catalog_entry_id for key, value in self.included.items()):
            raise ValueError("execution_capability_included_key_mismatch")
        if any(key != value.catalog_entry_id for key, value in self.excluded.items()):
            raise ValueError("execution_capability_excluded_key_mismatch")
        if self.included_count != len(self.included) or self.excluded_count != len(self.excluded):
            raise ValueError("execution_capability_snapshot_count_mismatch")
        if self.catalog_instrument_count != self.included_count + self.excluded_count:
            raise ValueError("execution_capability_snapshot_conservation_failed")
        if any(row.binding != self.binding or row.venue != self.venue for row in self.included.values()):
            raise ValueError("execution_capability_snapshot_binding_mismatch")
        if self.partition_sha256 != canonical_sha256(self.partition_payload):
            raise ValueError("execution_capability_partition_identity_invalid")
        return self

    @property
    def partition_payload(self) -> dict[str, object]:
        return {
            "included": {key: value.model_dump(mode="json") for key, value in sorted(self.included.items())},
            "excluded": {key: value.model_dump(mode="json") for key, value in sorted(self.excluded.items())},
        }

    @property
    def snapshot_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))

    def resolve(self, canonical_asset: str) -> ExecutionInstrumentCapabilityV2 | None:
        wanted = underlying_key(canonical_asset)
        matches = sorted(
            (row for row in self.included.values() if row.underlying_key == wanted),
            key=lambda row: (
                row.settlement_asset != "USDT",
                row.canonical_namespace,
                row.provider_instrument_id,
                row.catalog_entry_id,
            ),
        )
        return matches[0] if matches else None

    def capability_for_instrument(self, instrument_id: str) -> ExecutionInstrumentCapabilityV2 | None:
        matches = [row for row in self.included.values() if row.instrument_id == instrument_id]
        if len(matches) > 1:
            raise ValueError("execution_capability_instrument_identity_ambiguous")
        return matches[0] if matches else None


def build_execution_capability_snapshot(
    *,
    catalog: VenueInstrumentCatalogSnapshotV1,
    execution_rows: Sequence[ExecutionInstrumentEvidenceV1],
    app_revision: str,
    app_image_digest: str,
    adapter_contract_sha256: str,
    quote_contract_sha256: str,
    protection_contract_sha256: str,
    client_runtime_identity: str,
) -> ExecutionCapabilitySnapshotV2:
    """Compile every catalogue occurrence into exactly one included or excluded disposition."""

    evidence = _execution_evidence(execution_rows)
    provider_counts = Counter(row.provider_instrument_id for row in catalog.instruments)
    occurrences: Counter[tuple[str, str]] = Counter()
    included: dict[str, ExecutionInstrumentCapabilityV2] = {}
    excluded: dict[str, ExecutionCapabilityExclusionV2] = {}
    for row in catalog.instruments:
        identity = (row.provider_instrument_id, row.raw_metadata_sha256)
        occurrence = occurrences[identity]
        occurrences[identity] += 1
        catalog_entry_id = canonical_sha256(
            {
                "provider_instrument_id": row.provider_instrument_id,
                "raw_metadata_sha256": row.raw_metadata_sha256,
                "occurrence": occurrence,
            }
        )
        execution = evidence.get(identity)
        reason = _exclusion_reason(row, execution, duplicate=provider_counts[row.provider_instrument_id] > 1)
        disposition_evidence = {
            "catalog": row.model_dump(mode="json"),
            "execution": None if execution is None else execution.model_dump(mode="json"),
        }
        if reason is not None:
            excluded[catalog_entry_id] = ExecutionCapabilityExclusionV2(
                catalog_entry_id=catalog_entry_id,
                provider_instrument_id=row.provider_instrument_id,
                reason=reason,
                evidence_sha256=canonical_sha256(disposition_evidence),
            )
            continue
        if execution is None or row.canonical_asset is None or row.canonical_namespace is None:
            raise RuntimeError("execution_capability_included_evidence_missing")
        included[catalog_entry_id] = ExecutionInstrumentCapabilityV2(
            catalog_entry_id=catalog_entry_id,
            provider_instrument_id=row.provider_instrument_id,
            instrument_id=execution.instrument_id,
            native_symbol=execution.native_symbol,
            binding=catalog.binding,
            venue=venue_for_binding(catalog.binding),
            canonical_asset=row.canonical_asset,
            canonical_namespace=row.canonical_namespace,
            underlying_key=underlying_key(row.canonical_asset),
            settlement_asset=str(row.settlement_asset),
            price_precision=execution.price_precision,
            size_precision=execution.size_precision,
            price_increment=execution.price_increment,
            size_increment=execution.size_increment,
            min_quantity=execution.min_quantity,
            min_notional=execution.min_notional,
        )
    partition_payload = {
        "included": {key: value.model_dump(mode="json") for key, value in sorted(included.items())},
        "excluded": {key: value.model_dump(mode="json") for key, value in sorted(excluded.items())},
    }
    return ExecutionCapabilitySnapshotV2(
        binding=catalog.binding,
        venue=venue_for_binding(catalog.binding),
        catalog_snapshot_sha256=catalog.snapshot_sha256,
        catalog_instrument_count=catalog.provider_instrument_count,
        included_count=len(included),
        excluded_count=len(excluded),
        partition_sha256=canonical_sha256(partition_payload),
        app_revision=app_revision,
        app_image_digest=app_image_digest,
        adapter_contract_sha256=adapter_contract_sha256,
        quote_contract_sha256=quote_contract_sha256,
        protection_contract_sha256=protection_contract_sha256,
        client_runtime_identity=client_runtime_identity,
        included=included,
        excluded=excluded,
    )


def _execution_evidence(
    rows: Sequence[ExecutionInstrumentEvidenceV1],
) -> dict[tuple[str, str], ExecutionInstrumentEvidenceV1]:
    out: dict[tuple[str, str], ExecutionInstrumentEvidenceV1] = {}
    for row in rows:
        key = (row.provider_instrument_id, row.catalog_raw_metadata_sha256)
        previous = out.get(key)
        if previous is not None and previous != row:
            raise ValueError(f"execution_capability_evidence_conflict:{row.provider_instrument_id}")
        out[key] = row
    return out


def _exclusion_reason(
    catalog: VenueInstrumentCatalogEntryV1,
    execution: ExecutionInstrumentEvidenceV1 | None,
    *,
    duplicate: bool,
) -> CapabilityExclusionReason | None:
    if duplicate:
        return "DUPLICATE_PROVIDER_INSTRUMENT"
    if catalog.normalization_error is not None:
        return "CATALOG_NORMALIZATION_FAILED"
    if not catalog.active:
        return "INACTIVE"
    if catalog.product_kind != "linear_perpetual":
        return "PRODUCT_UNSUPPORTED"
    if catalog.settlement_asset not in SUPPORTED_SETTLEMENT_ASSETS:
        return "SETTLEMENT_ASSET_UNSUPPORTED"
    if execution is None:
        return "ADAPTER_EVIDENCE_MISSING"
    if execution.error is not None:
        return "ADAPTER_EVIDENCE_CONFLICT"
    if (
        execution.provider_instrument_id != catalog.provider_instrument_id
        or execution.catalog_raw_metadata_sha256 != catalog.raw_metadata_sha256
        or execution.native_symbol != catalog.provider_symbol
    ):
        return "INSTRUMENT_IDENTITY_MISMATCH"
    try:
        _positive_decimal(execution.price_increment, "price_increment")
        _positive_decimal(execution.size_increment, "size_increment")
        _nonnegative_decimal(execution.min_quantity, "min_quantity")
        _nonnegative_decimal(execution.min_notional, "min_notional")
    except ValueError:
        return "PRECISION_CONTRACT_UNPROVEN"
    if not execution.execution_eligible:
        return "EXECUTION_CONTRACT_UNPROVEN"
    if not execution.protection_eligible:
        return "PROTECTION_CONTRACT_UNPROVEN"
    return None


__all__ = [
    "EXECUTION_CAPABILITY_SNAPSHOT_VERSION",
    "SUPPORTED_SETTLEMENT_ASSETS",
    "CapabilityExclusionReason",
    "ExecutionCapabilityExclusionV2",
    "ExecutionCapabilitySnapshotV2",
    "ExecutionInstrumentCapabilityV2",
    "ExecutionInstrumentEvidenceV1",
    "build_execution_capability_snapshot",
]
