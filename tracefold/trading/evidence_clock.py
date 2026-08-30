"""Immutable contracts for the one Trading Production V3 evidence clock (#377).

Capture, drain, evaluation, human authority and release accounting are different clock stages.  These
models keep their artifacts content-addressed and make every missing provider input explicit.  They
grant no capital authority: PostgreSQL only links a positive future result to a promotion grant after
the result has been appended to the evidence ledger.
"""

from __future__ import annotations

from collections import Counter
from decimal import Decimal
from itertools import pairwise
from typing import Annotated, Any, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from .admission import ADMISSION_VERSION
from .bindings import binding_for_source_venue
from .contracts import (
    TRADING_MANIFEST_VERSION,
    OiTradeCandidate,
    canonical_sha256,
    oi_source_key,
    underlying_key,
)
from .replay import ReplayBarV1, ReplayTerminalOutcomeV1

MILLISECONDS_PER_DAY: Final = 86_400_000
EVIDENCE_BAR_INTERVAL_MS: Final = 300_000
DISCOVERY_CORPUS_TERMINAL: Final = "SOURCE_FEATURE_DISCOVERY_CORPUS_V1_SEALED"

EvidencePartition = Literal["discovery", "future"]
EvidenceBinding = Literal["BINANCE_USDM", "HYPERLIQUID_PERP"]
EvidenceVenue = Literal["binance.perp", "hl.perp"]
InputState = Literal["AVAILABLE", "MISSING", "STALE", "CORRECTED"]
EvidenceIncident = Literal[
    "venue_provider_outage",
    "source_mass_missingness",
    "catalog_reset_or_delist",
    "provider_correction",
    "bar_or_funding_missing",
    "protection_contract_invalid",
    "clock_or_known_at_violation",
]

_BINDING_BY_SOURCE_VENUE: Final[dict[str, EvidenceBinding]] = {
    "binance.perp": "BINANCE_USDM",
    "hl.perp": "HYPERLIQUID_PERP",
}
_SOURCE_VENUE_BY_BINDING: Final[dict[str, EvidenceVenue]] = {
    "BINANCE_USDM": "binance.perp",
    "HYPERLIQUID_PERP": "hl.perp",
}


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class EvidenceInputV1(_Frozen):
    """Availability of one capture input; missing is a value, never an implicit zero."""

    input_version: Literal["evidence_input_v1"] = "evidence_input_v1"
    state: InputState
    observed_at_ms: int | None = Field(default=None, ge=0)
    known_at_ms: int | None = Field(default=None, ge=0)
    value_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    reason: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        if self.state == "AVAILABLE":
            if self.observed_at_ms is None or self.known_at_ms is None or self.value_sha256 is None:
                raise ValueError("evidence_input_available_shape_invalid")
            if self.reason is not None:
                raise ValueError("evidence_input_available_reason_invalid")
        elif self.state == "CORRECTED":
            if (
                self.observed_at_ms is None
                or self.known_at_ms is None
                or self.value_sha256 is None
                or self.reason is None
            ):
                raise ValueError("evidence_input_corrected_shape_invalid")
        elif self.value_sha256 is not None or self.reason is None:
            raise ValueError("evidence_input_unavailable_shape_invalid")
        if self.observed_at_ms is not None and self.known_at_ms is not None and self.known_at_ms < self.observed_at_ms:
            raise ValueError("evidence_input_clock_inversion")
        return self


class PointInTimeCatalogRowV1(_Frozen):
    venue: EvidenceVenue
    venue_symbol: str = Field(min_length=1, max_length=128)
    base_symbol: str = Field(min_length=1, max_length=32)
    instrument_class: Literal["crypto"] = "crypto"
    quote_asset: str | None = Field(default=None, max_length=16)
    status: Literal["trading"] = "trading"
    observed_at_ms: int = Field(ge=0)


class PointInTimeCatalogV1(_Frozen):
    catalog_version: Literal["point_in_time_catalog_v1"] = "point_in_time_catalog_v1"
    source_observed_at_ms: int = Field(ge=0)
    rows: tuple[PointInTimeCatalogRowV1, ...]
    rows_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_catalog(self) -> Self:
        payloads = tuple(row.model_dump(mode="json") for row in self.rows)
        if payloads != tuple(sorted(payloads, key=lambda row: (row["venue"], row["venue_symbol"]))):
            raise ValueError("evidence_catalog_not_canonical")
        if len({(row.venue, row.venue_symbol) for row in self.rows}) != len(self.rows):
            raise ValueError("evidence_catalog_duplicate_instrument")
        if any(row.observed_at_ms > self.source_observed_at_ms for row in self.rows):
            raise ValueError("evidence_catalog_future_leakage")
        if self.rows_sha256 != canonical_sha256(payloads):
            raise ValueError("evidence_catalog_identity_invalid")
        return self


class CapturedSourceV1(_Frozen):
    capture_row_version: Literal["captured_oi_source_v1"] = "captured_oi_source_v1"
    source_identity: str = Field(min_length=1, max_length=256)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_source: dict[str, Any]
    venue: str = Field(max_length=128)
    binding: EvidenceBinding | None
    canonical_asset: str = Field(max_length=32)
    provider_instrument_id: str | None = Field(default=None, min_length=1, max_length=128)
    observed_at_ms: int = Field(ge=0)
    known_at_ms: int = Field(ge=0)
    available_at_ms: int = Field(ge=0)
    catalog: PointInTimeCatalogV1

    @model_validator(mode="after")
    def validate_source(self) -> Self:
        expected_binding = binding_for_source_venue(self.venue)
        if self.binding != expected_binding:
            raise ValueError("evidence_source_binding_mismatch")
        if self.binding is not None and self.venue != _SOURCE_VENUE_BY_BINDING[self.binding]:
            raise ValueError("evidence_source_venue_not_canonical")
        if self.source_sha256 != canonical_sha256(self.raw_source):
            raise ValueError("evidence_source_identity_invalid")
        expected_identity = oi_source_key(self.raw_source.get("event_id"), self.raw_source.get("metric_version"))
        if self.source_identity != expected_identity:
            raise ValueError("evidence_source_key_mismatch")
        if binding_for_source_venue(self.raw_source.get("venue")) != self.binding:
            raise ValueError("evidence_source_wrong_venue")
        raw_observed_at = self.raw_source.get("observed_at_ms")
        raw_known_at = self.raw_source.get("verdict_created_at_ms")
        if raw_observed_at is None or self.observed_at_ms != int(raw_observed_at):
            raise ValueError("evidence_source_observed_at_mismatch")
        if raw_known_at is None or self.known_at_ms != int(raw_known_at):
            raise ValueError("evidence_source_known_at_mismatch")
        if not self.observed_at_ms <= self.known_at_ms <= self.available_at_ms:
            raise ValueError("evidence_source_clock_inversion")
        if self.catalog.source_observed_at_ms != self.observed_at_ms:
            raise ValueError("evidence_source_catalog_cutoff_mismatch")
        if any(row.venue != self.venue or row.base_symbol != self.canonical_asset for row in self.catalog.rows):
            raise ValueError("evidence_source_catalog_partition_mismatch")
        matching = {row.venue_symbol for row in self.catalog.rows}
        if self.provider_instrument_id is not None and self.provider_instrument_id not in matching:
            raise ValueError("evidence_source_provider_instrument_unproven")
        if self.binding is None and (self.provider_instrument_id is not None or self.catalog.rows):
            raise ValueError("evidence_source_unsupported_venue_market_data")
        if self.binding is not None and underlying_key(self.canonical_asset) != f"crypto:{self.canonical_asset}":
            raise ValueError("evidence_source_canonical_asset_invalid")
        return self


class EvidenceCaptureSpecV1(_Frozen):
    spec_version: Literal["evidence_capture_spec_v1"] = "evidence_capture_spec_v1"
    partition: EvidencePartition
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    captured_at_ms: int = Field(gt=0)
    target_binding: EvidenceBinding | None = None
    source_query_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    protocol_receipt_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    protocol_locked_at_ms: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_window(self) -> Self:
        if not self.start_ms < self.end_ms <= self.captured_at_ms:
            raise ValueError("evidence_capture_window_invalid")
        if self.partition == "discovery":
            if (
                self.target_binding is not None
                or self.protocol_receipt_sha256 is not None
                or self.protocol_locked_at_ms is not None
            ):
                raise ValueError("evidence_discovery_protocol_forbidden")
        elif self.target_binding is None or self.protocol_receipt_sha256 is None or self.protocol_locked_at_ms is None:
            raise ValueError("evidence_future_protocol_required")
        elif self.protocol_locked_at_ms > self.start_ms:
            raise ValueError("evidence_future_lock_after_start")
        return self


class EvidenceCaptureArtifactV1(_Frozen):
    artifact_version: Literal["evidence_capture_artifact_v1"] = "evidence_capture_artifact_v1"
    spec: EvidenceCaptureSpecV1
    sources: tuple[CapturedSourceV1, ...]
    source_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_capture(self) -> Self:
        if self.source_count != len(self.sources):
            raise ValueError("evidence_capture_count_mismatch")
        if len({row.source_identity for row in self.sources}) != len(self.sources):
            raise ValueError("evidence_capture_duplicate_source")
        ordered = tuple(sorted(self.sources, key=lambda row: (row.observed_at_ms, row.source_identity)))
        if self.sources != ordered:
            raise ValueError("evidence_capture_not_canonical")
        for row in self.sources:
            if row.available_at_ms > self.spec.captured_at_ms:
                raise ValueError("evidence_capture_availability_clock_mismatch")
            if not self.spec.start_ms <= row.observed_at_ms < self.spec.end_ms:
                raise ValueError("evidence_capture_source_outside_window")
            if self.spec.partition == "future" and row.observed_at_ms < int(self.spec.protocol_locked_at_ms or 0):
                raise ValueError("evidence_future_source_before_lock")
            if self.spec.partition == "future" and row.known_at_ms > self.spec.end_ms:
                raise ValueError("evidence_future_source_known_after_capture_cutoff")
            if self.spec.partition == "future" and row.binding != self.spec.target_binding:
                raise ValueError("evidence_future_wrong_venue_source")
        return self

    @property
    def capture_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


class FutureCaptureBatchV1(_Frozen):
    """One contiguous, append-only blind-period source batch."""

    batch_version: Literal["future_capture_batch_v1"] = "future_capture_batch_v1"
    binding: EvidenceBinding
    candidate_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    protocol_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    batch_start_ms: int = Field(ge=0)
    batch_end_ms: int = Field(gt=0)
    captured_at_ms: int = Field(gt=0)
    capture_lag_ms: int = Field(ge=0)
    sources: tuple[CapturedSourceV1, ...]
    source_count: int = Field(ge=0)
    late_source_count: int = Field(ge=0)
    catalog_missing_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_batch(self) -> Self:
        if not self.batch_start_ms < self.batch_end_ms <= self.captured_at_ms:
            raise ValueError("evidence_future_batch_clock_invalid")
        if self.capture_lag_ms != self.captured_at_ms - self.batch_end_ms:
            raise ValueError("evidence_future_batch_lag_invalid")
        if self.source_count != len(self.sources):
            raise ValueError("evidence_future_batch_count_mismatch")
        expected_late = sum(row.available_at_ms > self.batch_end_ms for row in self.sources)
        expected_catalog_missing = sum(
            row.provider_instrument_id is None or not row.catalog.rows for row in self.sources
        )
        if self.late_source_count != expected_late or self.catalog_missing_count != expected_catalog_missing:
            raise ValueError("evidence_future_batch_health_invalid")
        if self.sources != tuple(sorted(self.sources, key=lambda row: (row.observed_at_ms, row.source_identity))):
            raise ValueError("evidence_future_batch_not_canonical")
        if len({row.source_identity for row in self.sources}) != len(self.sources):
            raise ValueError("evidence_future_batch_duplicate_source")
        if any(
            row.binding != self.binding
            or not self.batch_start_ms <= row.observed_at_ms < self.batch_end_ms
            or row.available_at_ms > self.captured_at_ms
            for row in self.sources
        ):
            raise ValueError("evidence_future_batch_source_scope_invalid")
        return self

    @property
    def batch_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


class MarketDrainRowV1(_Frozen):
    drain_row_version: Literal["market_drain_row_v1"] = "market_drain_row_v1"
    source_identity: str = Field(min_length=1, max_length=256)
    venue: str = Field(max_length=128)
    provider_instrument_id: str | None = Field(default=None, min_length=1, max_length=128)
    requested_start_ms: int = Field(ge=0)
    requested_end_ms: int = Field(gt=0)
    bar_interval_ms: Literal[300_000] = EVIDENCE_BAR_INTERVAL_MS
    funding_requested_start_ms: int = Field(ge=0)
    funding_requested_end_ms: int = Field(gt=0)
    bars: tuple[ReplayBarV1, ...]
    bars_input: EvidenceInputV1
    funding_rates: tuple[FundingRateV1, ...] = ()
    funding_input: EvidenceInputV1
    funding_window_sum_bps: Decimal | None = None
    bid_ask_input: EvidenceInputV1
    liquidity_input: EvidenceInputV1
    corrections: tuple[str, ...] = ()
    incidents: tuple[EvidenceIncident, ...] = ()

    @model_validator(mode="after")
    def validate_market_inputs(self) -> Self:
        if self.requested_end_ms <= self.requested_start_ms:
            raise ValueError("evidence_drain_window_invalid")
        if self.incidents != tuple(sorted(set(self.incidents))):
            raise ValueError("evidence_drain_incidents_not_canonical")
        if not (
            self.requested_start_ms
            <= self.funding_requested_start_ms
            < self.funding_requested_end_ms
            <= self.requested_end_ms
        ):
            raise ValueError("evidence_drain_funding_window_invalid")
        if self.bars:
            if self.venue not in _BINDING_BY_SOURCE_VENUE:
                raise ValueError("evidence_drain_unsupported_venue_bars")
            if self.provider_instrument_id is None:
                raise ValueError("evidence_drain_instrument_missing")
        elif self.bars_input.state == "AVAILABLE":
            raise ValueError("evidence_drain_empty_bars_available")
        funding_available = self.funding_input.state in {"AVAILABLE", "CORRECTED"}
        if funding_available != (self.funding_window_sum_bps is not None):
            raise ValueError("evidence_drain_funding_shape_invalid")
        if not funding_available and self.funding_rates:
            raise ValueError("evidence_drain_unavailable_funding_rows")
        if self.funding_rates != tuple(sorted(self.funding_rates, key=lambda row: row.funding_at_ms)):
            raise ValueError("evidence_drain_funding_not_canonical")
        if any(
            row.venue != self.venue
            or row.provider_instrument_id != self.provider_instrument_id
            or not self.funding_requested_start_ms <= row.funding_at_ms < self.funding_requested_end_ms
            for row in self.funding_rates
        ):
            raise ValueError("evidence_drain_funding_scope_invalid")
        if funding_available:
            funding_payload = tuple(row.model_dump(mode="json") for row in self.funding_rates)
            expected_return = -sum((row.funding_rate for row in self.funding_rates), start=Decimal(0)) * Decimal(10_000)
            if self.funding_input.value_sha256 != canonical_sha256(funding_payload):
                raise ValueError("evidence_drain_funding_identity_invalid")
            if self.funding_window_sum_bps != expected_return:
                raise ValueError("evidence_drain_funding_return_invalid")
        if tuple(sorted(self.bars, key=lambda row: (row.open_at_ms, row.close_at_ms))) != self.bars:
            raise ValueError("evidence_drain_bars_not_canonical")
        if any(
            bar.venue != self.venue
            or bar.open_at_ms < self.requested_start_ms
            or bar.close_at_ms > self.requested_end_ms
            for bar in self.bars
        ):
            raise ValueError("evidence_drain_bar_scope_invalid")
        continuous = (
            bool(self.bars)
            and all(bar.close_at_ms - bar.open_at_ms == self.bar_interval_ms for bar in self.bars)
            and all(previous.close_at_ms == current.open_at_ms for previous, current in pairwise(self.bars))
        )
        complete = (
            continuous
            and self.bars[0].open_at_ms <= self.requested_start_ms
            and self.bars[-1].close_at_ms >= self.requested_end_ms
        )
        if self.bars_input.state in {"AVAILABLE", "CORRECTED"} and not complete:
            raise ValueError("evidence_drain_bar_continuity_invalid")
        if self.bars_input.state in {"AVAILABLE", "CORRECTED"}:
            bars_payload = tuple(bar.model_dump(mode="json") for bar in self.bars)
            if self.bars_input.value_sha256 != canonical_sha256(bars_payload):
                raise ValueError("evidence_drain_bar_identity_invalid")
        return self


class FundingRateV1(_Frozen):
    funding_rate_version: Literal["provider_funding_rate_v1"] = "provider_funding_rate_v1"
    venue: EvidenceVenue
    provider_instrument_id: str = Field(min_length=1, max_length=128)
    funding_at_ms: int = Field(ge=0)
    funding_rate: Decimal

    @model_validator(mode="after")
    def validate_rate(self) -> Self:
        if abs(self.funding_rate) > Decimal("0.1"):
            raise ValueError("evidence_funding_rate_out_of_range")
        return self


class EvidenceDrainArtifactV1(_Frozen):
    artifact_version: Literal["evidence_drain_artifact_v1"] = "evidence_drain_artifact_v1"
    capture_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    partition: EvidencePartition
    drained_at_ms: int = Field(gt=0)
    max_horizon_ms: int = Field(gt=0)
    bar_interval_ms: Literal[300_000] = EVIDENCE_BAR_INTERVAL_MS
    funding_horizon_ms: int = Field(gt=0)
    finalization_lag_ms: int = Field(ge=0)
    rows: tuple[MarketDrainRowV1, ...]
    cost_model: dict[str, Any]
    cost_model_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_drain(self) -> Self:
        if len({row.source_identity for row in self.rows}) != len(self.rows):
            raise ValueError("evidence_drain_duplicate_source")
        if self.rows != tuple(sorted(self.rows, key=lambda row: row.source_identity)):
            raise ValueError("evidence_drain_not_canonical")
        if any(bar.close_at_ms > self.drained_at_ms for row in self.rows for bar in row.bars):
            raise ValueError("evidence_drain_future_bar")
        inputs = (
            evidence_input
            for row in self.rows
            for evidence_input in (row.bars_input, row.funding_input, row.bid_ask_input, row.liquidity_input)
        )
        if any(value.known_at_ms is not None and value.known_at_ms > self.drained_at_ms for value in inputs):
            raise ValueError("evidence_drain_input_known_after_drain")
        if any(
            row.funding_requested_end_ms - row.funding_requested_start_ms != self.funding_horizon_ms
            for row in self.rows
        ):
            raise ValueError("evidence_drain_funding_horizon_mismatch")
        if any(row.bar_interval_ms != self.bar_interval_ms for row in self.rows):
            raise ValueError("evidence_drain_bar_interval_mismatch")
        if self.cost_model_sha256 != canonical_sha256(self.cost_model):
            raise ValueError("evidence_cost_model_identity_invalid")
        return self

    @property
    def drain_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


class NormalizedEvidenceObservationV1(_Frozen):
    source_identity: str
    disposition: Literal["VALID", "EXCLUDED"]
    reason: str
    venue: str
    binding: EvidenceBinding | None
    canonical_asset: str
    provider_instrument_id: str | None = None
    observed_at_ms: int = Field(ge=0)
    known_at_ms: int = Field(ge=0)
    available_at_ms: int = Field(ge=0)
    source_contract_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    catalog_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    missing_inputs: tuple[str, ...]


class DiscoveryFeatureV1(_Frozen):
    feature_version: Literal["source_native_oi_feature_v1"] = "source_native_oi_feature_v1"
    source_identity: str
    venue: EvidenceVenue
    observed_at_ms: int = Field(ge=0)
    canonical_asset: str
    cross_sectional_rank: int = Field(ge=1)
    eligible_universe_size: int = Field(ge=1)
    returns_bps: dict[Literal["3m", "30m", "1h", "4h"], int | None]
    mfe_bps: int | None
    mae_bps: int | None
    missing_horizons: tuple[Literal["3m", "30m", "1h", "4h"], ...]


class EvidenceCoverageV1(_Frozen):
    coverage_version: Literal["evidence_coverage_v1"] = "evidence_coverage_v1"
    source_count: int = Field(ge=0)
    valid_count: int = Field(ge=0)
    excluded_count: int = Field(ge=0)
    complete_market_count: int = Field(ge=0)
    missing_by_reason: dict[str, int]
    coverage_bps: int = Field(ge=0, le=10_000)

    @model_validator(mode="after")
    def validate_conservation(self) -> Self:
        if self.valid_count + self.excluded_count != self.source_count:
            raise ValueError("evidence_coverage_conservation_failed")
        expected = 0 if self.source_count == 0 else self.complete_market_count * 10_000 // self.source_count
        if self.coverage_bps != expected:
            raise ValueError("evidence_coverage_rate_invalid")
        return self


class DiscoveryCorpusArtifactV1(_Frozen):
    artifact_version: Literal["source_feature_discovery_corpus_v1"] = "source_feature_discovery_corpus_v1"
    terminal: Literal["SOURCE_FEATURE_DISCOVERY_CORPUS_V1_SEALED"] = DISCOVERY_CORPUS_TERMINAL
    capture_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    drain_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    discovery_start_ms: int = Field(ge=0)
    discovery_end_ms: int = Field(gt=0)
    execution_contract_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    feature_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    admission_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_id: str = Field(min_length=1, max_length=128)
    policy_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    price_window_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    cost_model_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_notional: Decimal = Field(gt=0)
    evaluator_program_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_observations: tuple[CapturedSourceV1, ...]
    normalized_observations: tuple[NormalizedEvidenceObservationV1, ...]
    features: tuple[DiscoveryFeatureV1, ...]
    episodes: tuple[ReplayTerminalOutcomeV1, ...]
    coverage: EvidenceCoverageV1
    raw_observations_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    normalized_observations_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    features_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    episodes_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    coverage_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_components(self) -> Self:
        if self.discovery_end_ms <= self.discovery_start_ms:
            raise ValueError("evidence_corpus_window_invalid")
        components = {
            "raw_observations_sha256": self.raw_observations,
            "normalized_observations_sha256": self.normalized_observations,
            "features_sha256": self.features,
            "episodes_sha256": self.episodes,
        }
        for field, rows in components.items():
            payload = tuple(row.model_dump(mode="json") for row in rows)
            if getattr(self, field) != canonical_sha256(payload):
                raise ValueError(f"evidence_corpus_{field}_invalid")
        if self.coverage_sha256 != canonical_sha256(self.coverage.model_dump(mode="json")):
            raise ValueError("evidence_corpus_coverage_identity_invalid")
        raw_ids = {row.source_identity for row in self.raw_observations}
        if raw_ids != {row.source_identity for row in self.normalized_observations}:
            raise ValueError("evidence_corpus_normalized_conservation_failed")
        if raw_ids != {row.source_identity for row in self.episodes}:
            raise ValueError("evidence_corpus_episode_conservation_failed")
        valid_ids = {row.source_identity for row in self.normalized_observations if row.disposition == "VALID"}
        feature_ids = {row.source_identity for row in self.features}
        if feature_ids != valid_ids or len(feature_ids) != len(self.features):
            raise ValueError("evidence_corpus_feature_conservation_failed")
        expected_valid = len(valid_ids)
        expected_complete = sum(
            row.disposition == "VALID" and "bars" not in row.missing_inputs for row in self.normalized_observations
        )
        expected_missing: Counter[str] = Counter()
        for row in self.normalized_observations:
            if row.disposition == "EXCLUDED":
                expected_missing[f"excluded:{row.reason}"] += 1
            for missing_input in row.missing_inputs:
                expected_missing[f"missing:{missing_input}"] += 1
        if (
            self.coverage.source_count != len(raw_ids)
            or self.coverage.valid_count != expected_valid
            or self.coverage.excluded_count != len(raw_ids) - expected_valid
            or self.coverage.complete_market_count != expected_complete
            or self.coverage.missing_by_reason != dict(sorted(expected_missing.items()))
        ):
            raise ValueError("evidence_corpus_coverage_conservation_failed")
        return self

    @property
    def corpus_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


class DiscoveryCorpusReceiptV1(_Frozen):
    receipt_version: Literal["discovery_corpus_receipt_v1"] = "discovery_corpus_receipt_v1"
    terminal: Literal["SOURCE_FEATURE_DISCOVERY_CORPUS_V1_SEALED"] = DISCOVERY_CORPUS_TERMINAL
    corpus_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_path: str = Field(min_length=1, max_length=1024)
    capture_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    drain_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_contract_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_count: int = Field(ge=0)
    created_at_ms: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_artifact_identity(self) -> Self:
        if self.artifact_sha256 != self.corpus_sha256:
            raise ValueError("evidence_corpus_receipt_artifact_identity_invalid")
        return self

    @property
    def receipt_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


class CandidateExecutionProtocolV1(_Frozen):
    """The executable economics frozen before the future window begins."""

    execution_protocol_version: Literal["candidate_execution_protocol_v1"] = "candidate_execution_protocol_v1"
    side: Literal["long"] = "long"
    trigger_timing: Literal["source_observed_at"] = "source_observed_at"
    trigger_cutoff: Literal["half_open_future_window"] = "half_open_future_window"
    intent_ttl_ms: int = Field(gt=0)
    target_notional: Decimal = Field(gt=0, le=10)
    max_risk_amount: Decimal = Field(gt=0)
    sizing_rule: Literal["floor_to_binding_increment(target_notional/fresh_side_price)"] = (
        "floor_to_binding_increment(target_notional/fresh_side_price)"
    )
    quote_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    max_quote_age_ms: int = Field(gt=0)
    max_spread_bps: int = Field(ge=0)
    max_entry_drift_bps: int = Field(ge=0)
    stop_loss_bps: int = Field(gt=0)
    protection_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    protection_assumption: Literal["native_reduce_only_fixed_quantity"] = "native_reduce_only_fixed_quantity"
    max_holding_ms: int = Field(gt=0)
    exit_rule: Literal["native_stop_or_max_holding_full_close"] = "native_stop_or_max_holding_full_close"
    execution_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    adapter_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    capability_requirements: tuple[str, ...] = Field(min_length=1)
    fee_model: dict[str, str]
    funding_model: dict[str, str]
    spread_slippage_model: dict[str, str]
    latency_model: dict[str, str]
    additional_stressed_cost_bps: Decimal = Field(ge=0)
    cost_model_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    benchmark: Literal["zero_return_bps_v1"] = "zero_return_bps_v1"
    max_symbol_concentration_bps: int = Field(gt=0, le=10_000)
    max_day_concentration_bps: int = Field(gt=0, le=10_000)

    @model_validator(mode="after")
    def validate_execution_protocol(self) -> Self:
        if self.max_risk_amount > self.target_notional:
            raise ValueError("evidence_candidate_risk_exceeds_notional")
        requirements = self.capability_requirements
        if requirements != tuple(sorted(requirements)) or len(set(requirements)) != len(requirements):
            raise ValueError("evidence_candidate_capabilities_not_canonical")
        cost_payload = {
            "fee_model": self.fee_model,
            "funding_model": self.funding_model,
            "spread_slippage_model": self.spread_slippage_model,
            "latency_model": self.latency_model,
            "additional_stressed_cost_bps": str(self.additional_stressed_cost_bps),
        }
        if self.cost_model_sha256 != canonical_sha256(cost_payload):
            raise ValueError("evidence_candidate_cost_model_identity_invalid")
        expected_models = {
            "fee_model": {"version": "nautilus_bar_taker_fee_v1", "per_side_bps": "5"},
            "funding_model": {
                "version": "provider_history_replay_holding_v1",
                "mark_price": "first_5m_close_at_or_after_funding_event",
            },
            "spread_slippage_model": {"version": "additional_stress_bps_v1"},
            "latency_model": {"version": "first_closed_5m_bar_at_or_after_known_at_v1"},
        }
        if any(getattr(self, name) != value for name, value in expected_models.items()):
            raise ValueError("evidence_candidate_cost_model_unsupported")
        return self


class FutureStatisticalProtocolV1(_Frozen):
    statistical_protocol_version: Literal["future_statistical_protocol_v1"] = "future_statistical_protocol_v1"
    future_start_ms: int = Field(gt=0)
    future_end_ms: int = Field(gt=0)
    capture_cutoff_ms: int = Field(gt=0)
    capture_interval_ms: int = Field(gt=0)
    maximum_capture_lag_ms: int = Field(ge=0)
    max_horizon_ms: int = Field(gt=0)
    bar_interval_ms: Literal[300_000] = EVIDENCE_BAR_INTERVAL_MS
    data_finalization_lag_ms: int = Field(ge=0)
    drain_cutoff_ms: int = Field(gt=0)
    primary_statistic: Literal["mean_net_including_funding_return_bps"] = "mean_net_including_funding_return_bps"
    secondary_diagnostics: tuple[str, ...] = Field(min_length=1)
    stressed_hurdle_bps: Decimal
    confidence_level_bps: int = Field(gt=5_000, lt=10_000)
    bootstrap_unit: Literal["utc_calendar_day"] = "utc_calendar_day"
    bootstrap_block_days: int = Field(ge=1)
    bootstrap_samples: int = Field(ge=100, le=100_000)
    bootstrap_seed: int = Field(ge=0)
    autocorrelation_treatment: Literal["moving_day_block_bootstrap"] = "moving_day_block_bootstrap"
    multiple_testing_treatment: Literal["one_locked_candidate_per_venue_no_window_reuse"] = (
        "one_locked_candidate_per_venue_no_window_reuse"
    )
    minimum_effective_n: int = Field(ge=1)
    power_method: Literal["normal_approximation_one_sided_mde_v1"] = "normal_approximation_one_sided_mde_v1"
    minimum_detectable_excess_bps: Decimal = Field(gt=0)
    assumed_standard_deviation_bps: Decimal = Field(gt=0)
    minimum_power_bps: int = Field(ge=0, le=10_000)
    minimum_coverage_bps: int = Field(ge=1, le=10_000)
    maximum_missingness_bps: int = Field(ge=0, lt=10_000)
    required_inputs: tuple[Literal["bars", "funding"], ...] = ("bars", "funding")
    decision_logic: Literal["INSUFFICIENT_on_rules_else_PROMOTE_if_lower_bound_above_hurdle_else_HOLD"] = (
        "INSUFFICIENT_on_rules_else_PROMOTE_if_lower_bound_above_hurdle_else_HOLD"
    )
    incident_handling: dict[
        Literal[
            "venue_provider_outage",
            "source_mass_missingness",
            "catalog_reset_or_delist",
            "provider_correction",
            "bar_or_funding_missing",
            "protection_contract_invalid",
            "clock_or_known_at_violation",
        ],
        Literal["INSUFFICIENT_EVIDENCE", "KEEP_AS_PREDECLARED_MISSING"],
    ]

    @model_validator(mode="after")
    def validate_statistical_protocol(self) -> Self:
        if not self.future_start_ms < self.future_end_ms:
            raise ValueError("evidence_future_window_invalid")
        if self.capture_cutoff_ms != self.future_end_ms:
            raise ValueError("evidence_future_capture_cutoff_invalid")
        if self.capture_interval_ms < self.bar_interval_ms:
            raise ValueError("evidence_future_capture_interval_invalid")
        if self.maximum_capture_lag_ms > self.capture_interval_ms:
            raise ValueError("evidence_future_capture_lag_invalid")
        expected_drain = self.future_end_ms + self.max_horizon_ms + self.data_finalization_lag_ms
        if self.drain_cutoff_ms != expected_drain:
            raise ValueError("evidence_future_drain_cutoff_invalid")
        if self.secondary_diagnostics != tuple(sorted(self.secondary_diagnostics)):
            raise ValueError("evidence_future_diagnostics_not_canonical")
        if self.required_inputs != tuple(sorted(set(self.required_inputs))):
            raise ValueError("evidence_future_required_inputs_not_canonical")
        required_incidents: set[EvidenceIncident] = {
            "venue_provider_outage",
            "source_mass_missingness",
            "catalog_reset_or_delist",
            "provider_correction",
            "bar_or_funding_missing",
            "protection_contract_invalid",
            "clock_or_known_at_violation",
        }
        if set(self.incident_handling) != required_incidents:
            raise ValueError("evidence_future_incident_protocol_incomplete")
        integrity_incidents: tuple[EvidenceIncident, ...] = (
            "protection_contract_invalid",
            "clock_or_known_at_violation",
        )
        if any(self.incident_handling[name] != "INSUFFICIENT_EVIDENCE" for name in integrity_incidents):
            raise ValueError("evidence_future_integrity_incident_must_fail_closed")
        return self


class CandidateLockedV1(_Frozen):
    candidate_version: Literal["production_v3_candidate_v1"] = "production_v3_candidate_v1"
    terminal: Literal["CANDIDATE_LOCKED"] = "CANDIDATE_LOCKED"
    binding: EvidenceBinding
    venue: Literal["binance.usdm", "hyperliquid.perp"]
    sealed_corpus_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    corpus_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    discovery_start_ms: int = Field(ge=0)
    discovery_end_ms: int = Field(gt=0)
    source_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    feature_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    point_in_time_catalog_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    eligible_universe_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    selection_program_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    selection_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_id: str = Field(min_length=1, max_length=128)
    policy_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_contract_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution: CandidateExecutionProtocolV1
    statistics: FutureStatisticalProtocolV1
    evaluator_program_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    locked_at_ms: int = Field(gt=0)
    preregistered_by: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_candidate(self) -> Self:
        expected_venue = "binance.usdm" if self.binding == "BINANCE_USDM" else "hyperliquid.perp"
        if self.venue != expected_venue:
            raise ValueError("evidence_candidate_binding_venue_mismatch")
        if not self.discovery_start_ms < self.discovery_end_ms <= self.locked_at_ms:
            raise ValueError("evidence_candidate_discovery_clock_invalid")
        if self.locked_at_ms >= self.statistics.future_start_ms:
            raise ValueError("evidence_candidate_lock_not_before_future")
        if self.corpus_artifact_sha256 != self.sealed_corpus_sha256:
            raise ValueError("evidence_candidate_corpus_identity_mismatch")
        return self

    @property
    def protocol_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


class NoCandidateV1(_Frozen):
    candidate_version: Literal["production_v3_no_candidate_v1"] = "production_v3_no_candidate_v1"
    terminal: Literal["NO_CANDIDATE"] = "NO_CANDIDATE"
    binding: EvidenceBinding
    venue: Literal["binance.usdm", "hyperliquid.perp"]
    sealed_corpus_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    corpus_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    selection_program_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    selection_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: Literal["no_complete_directional_discovery_episode"]
    decided_at_ms: int = Field(gt=0)
    decided_by: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_no_candidate(self) -> Self:
        expected_venue = "binance.usdm" if self.binding == "BINANCE_USDM" else "hyperliquid.perp"
        if self.venue != expected_venue:
            raise ValueError("evidence_candidate_binding_venue_mismatch")
        if self.corpus_artifact_sha256 != self.sealed_corpus_sha256:
            raise ValueError("evidence_candidate_corpus_identity_mismatch")
        return self

    @property
    def protocol_sha256(self) -> None:
        return None


CandidateDecisionV1 = Annotated[CandidateLockedV1 | NoCandidateV1, Field(discriminator="terminal")]
CANDIDATE_DECISION_ADAPTER: Final[TypeAdapter[CandidateDecisionV1]] = TypeAdapter(CandidateDecisionV1)


class CandidateDecisionReceiptV1(_Frozen):
    receipt_version: Literal["candidate_decision_receipt_v1"] = "candidate_decision_receipt_v1"
    terminal: Literal["CANDIDATE_LOCKED", "NO_CANDIDATE"]
    binding: EvidenceBinding
    sealed_corpus_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_path: str = Field(min_length=1, max_length=1024)
    protocol_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    created_at_ms: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_receipt(self) -> Self:
        if (self.terminal == "CANDIDATE_LOCKED") != (self.protocol_sha256 is not None):
            raise ValueError("evidence_candidate_receipt_protocol_shape_invalid")
        if self.protocol_sha256 is not None and self.artifact_sha256 != self.protocol_sha256:
            raise ValueError("evidence_candidate_receipt_artifact_identity_invalid")
        return self

    @property
    def receipt_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


class FutureCaptureReceiptV1(_Frozen):
    """Durable one-shot commitment to the exact post-lock source population."""

    receipt_version: Literal["future_capture_receipt_v1"] = "future_capture_receipt_v1"
    terminal: Literal["FUTURE_CAPTURE_SEALED"] = "FUTURE_CAPTURE_SEALED"
    binding: EvidenceBinding
    candidate_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    protocol_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sealed_corpus_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    capture_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_path: str = Field(min_length=1, max_length=1024)
    created_at_ms: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_artifact_identity(self) -> Self:
        if self.artifact_sha256 != self.capture_sha256:
            raise ValueError("evidence_future_capture_receipt_artifact_identity_invalid")
        return self

    @property
    def receipt_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


class FutureDrainReceiptV1(_Frozen):
    """Durable one-shot commitment to the exact future labels exposed after cutoff."""

    receipt_version: Literal["future_drain_receipt_v1"] = "future_drain_receipt_v1"
    terminal: Literal["FUTURE_DRAIN_SEALED"] = "FUTURE_DRAIN_SEALED"
    binding: EvidenceBinding
    candidate_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    capture_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    protocol_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sealed_corpus_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    capture_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    drain_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_path: str = Field(min_length=1, max_length=1024)
    created_at_ms: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_artifact_identity(self) -> Self:
        if self.artifact_sha256 != self.drain_sha256:
            raise ValueError("evidence_future_drain_receipt_artifact_identity_invalid")
        return self

    @property
    def receipt_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


class FutureHoldoutMetricsV1(_Frozen):
    metrics_version: Literal["future_holdout_metrics_v1"] = "future_holdout_metrics_v1"
    source_count: int = Field(ge=0)
    effective_n: int = Field(ge=0)
    estimated_power_bps: int = Field(ge=0, le=10_000)
    coverage_bps: int = Field(ge=0, le=10_000)
    missingness_bps: int = Field(ge=0, le=10_000)
    mean_net_including_funding_return_bps: Decimal | None
    benchmark_excess_bps: Decimal | None
    primary_confidence_lower_bound_bps: Decimal | None
    mfe_bps: dict[str, Decimal | None]
    mae_bps: dict[str, Decimal | None]
    max_drawdown_bps: Decimal | None
    tail_loss_bps: Decimal | None
    turnover: Decimal | None
    capacity_proxy: Decimal | None
    concentration_bps: dict[str, int]
    missing_by_reason: dict[str, int]
    sensitivity: dict[str, Decimal | None]


class FutureHoldoutResultV1(_Frozen):
    result_version: Literal["future_holdout_result_v1"] = "future_holdout_result_v1"
    terminal: Literal["PROMOTE", "HOLD", "INSUFFICIENT_EVIDENCE"]
    binding: EvidenceBinding
    venue: Literal["binance.usdm", "hyperliquid.perp"]
    candidate_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    protocol_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sealed_corpus_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    future_capture_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    future_drain_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluator_program_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluated_at_ms: int = Field(gt=0)
    metrics: FutureHoldoutMetricsV1
    reasons: tuple[str, ...]

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        expected_venue = "binance.usdm" if self.binding == "BINANCE_USDM" else "hyperliquid.perp"
        if self.venue != expected_venue:
            raise ValueError("evidence_result_binding_venue_mismatch")
        if not self.reasons or self.reasons != tuple(sorted(set(self.reasons))):
            raise ValueError("evidence_result_reasons_not_canonical")
        complete = (
            self.metrics.mean_net_including_funding_return_bps is not None
            and self.metrics.primary_confidence_lower_bound_bps is not None
        )
        if self.terminal in {"PROMOTE", "HOLD"} and not complete:
            raise ValueError("evidence_result_statistic_missing")
        return self

    @property
    def report_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


class FutureHoldoutResultReceiptV1(_Frozen):
    receipt_version: Literal["future_holdout_result_receipt_v1"] = "future_holdout_result_receipt_v1"
    terminal: Literal["PROMOTE", "HOLD", "INSUFFICIENT_EVIDENCE"]
    binding: EvidenceBinding
    candidate_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    protocol_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sealed_corpus_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_path: str = Field(min_length=1, max_length=1024)
    created_at_ms: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_artifact_identity(self) -> Self:
        if self.artifact_sha256 != self.report_sha256:
            raise ValueError("evidence_future_receipt_artifact_identity_invalid")
        return self

    @property
    def receipt_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


def source_contract_sha256() -> str:
    """Exact current OI fact contract shared by evidence capture and the capital lane."""

    return canonical_sha256(
        {
            "version": "trading_oi_source_contract_v2",
            "metric_version": "oi_signal_v1",
            "upstream_program_version": "news_oi_signal_v2",
            "upstream_policy_version": "news_triage_policy_v11",
            "upstream_judgment_contract_version": "news_judgment_v2",
            "upstream_judgment_origin": "oi",
            "candidate_schema_sha256": canonical_sha256(OiTradeCandidate.model_json_schema()),
            "manifest_version": TRADING_MANIFEST_VERSION,
            "source_native": True,
        }
    )


def candidate_selection_program_sha256() -> str:
    """Exact finite selection policy allowed to consume a sealed discovery corpus."""

    return canonical_sha256(
        {
            "version": "finite_candidate_selection_v1",
            "policy_ids": ("source_native_oi_smart_money_long_v3",),
            "bindings": ("BINANCE_USDM", "HYPERLIQUID_PERP"),
            "max_candidates_per_binding": 1,
            "inputs": "sealed_discovery_corpus_only",
            "eligibility": "valid_feature_and_directional_closed_episode",
            "terminals": ("CANDIDATE_LOCKED", "NO_CANDIDATE"),
            "no_window_reuse": True,
        }
    )


def candidate_selection_evidence_sha256(
    corpus: DiscoveryCorpusArtifactV1,
    binding: EvidenceBinding,
) -> tuple[Literal["CANDIDATE_LOCKED", "NO_CANDIDATE"], str]:
    """Run the only finite discovery selector and bind its exact input population."""

    raw = {row.source_identity: row for row in corpus.raw_observations}
    normalized = {row.source_identity: row for row in corpus.normalized_observations}
    features = {row.source_identity: row for row in corpus.features}
    episodes = {row.source_identity: row for row in corpus.episodes}
    eligible = tuple(
        source_id
        for source_id in sorted(raw)
        if raw[source_id].binding == binding
        and normalized[source_id].disposition == "VALID"
        and source_id in features
        and episodes[source_id].decision == "DIRECTIONAL"
        and episodes[source_id].execution == "CLOSED"
    )
    terminal: Literal["CANDIDATE_LOCKED", "NO_CANDIDATE"] = "CANDIDATE_LOCKED" if eligible else "NO_CANDIDATE"
    return terminal, canonical_sha256(
        {
            "selection_program_sha256": candidate_selection_program_sha256(),
            "sealed_corpus_sha256": corpus.corpus_sha256,
            "binding": binding,
            "eligible_universe_sha256": eligible_universe_sha256(corpus, binding),
            "eligible_source_identities": eligible,
            "terminal": terminal,
        }
    )


def feature_contract_sha256(
    *,
    admission_config_sha256: str,
    price_window: dict[str, int],
    policy_id: str,
    policy_config_sha256: str,
) -> str:
    return canonical_sha256(
        {
            "version": "trading_oi_feature_contract_v2",
            "admission_version": ADMISSION_VERSION,
            "admission_config_sha256": admission_config_sha256,
            "price_window": price_window,
            "policy_id": policy_id,
            "policy_config_sha256": policy_config_sha256,
            "discovery_feature_schema_sha256": canonical_sha256(DiscoveryFeatureV1.model_json_schema()),
        }
    )


def point_in_time_catalog_sha256(corpus: DiscoveryCorpusArtifactV1, binding: EvidenceBinding) -> str:
    payload = tuple(
        {
            "source_identity": row.source_identity,
            "catalog": row.catalog.model_dump(mode="json"),
        }
        for row in corpus.raw_observations
        if row.binding == binding
    )
    return canonical_sha256(payload)


def eligible_universe_sha256(corpus: DiscoveryCorpusArtifactV1, binding: EvidenceBinding) -> str:
    raw = {row.source_identity: row for row in corpus.raw_observations}
    normalized = {row.source_identity: row for row in corpus.normalized_observations}
    features = {row.source_identity: row for row in corpus.features}
    episodes = {row.source_identity: row for row in corpus.episodes}
    source_ids = sorted(
        source_id
        for source_id, row in raw.items()
        if row.binding == binding and normalized[source_id].disposition == "VALID"
    )
    payload = tuple(
        {
            "source_identity": source_id,
            "normalized": normalized[source_id].model_dump(mode="json"),
            "feature": None if source_id not in features else features[source_id].model_dump(mode="json"),
            "episode": episodes[source_id].model_dump(mode="json"),
        }
        for source_id in source_ids
    )
    return canonical_sha256(payload)


__all__ = [
    "CANDIDATE_DECISION_ADAPTER",
    "DISCOVERY_CORPUS_TERMINAL",
    "CandidateDecisionReceiptV1",
    "CandidateDecisionV1",
    "CandidateExecutionProtocolV1",
    "CandidateLockedV1",
    "CapturedSourceV1",
    "DiscoveryCorpusArtifactV1",
    "DiscoveryCorpusReceiptV1",
    "DiscoveryFeatureV1",
    "EvidenceCaptureArtifactV1",
    "EvidenceCaptureSpecV1",
    "EvidenceCoverageV1",
    "EvidenceDrainArtifactV1",
    "EvidenceInputV1",
    "FundingRateV1",
    "FutureCaptureBatchV1",
    "FutureCaptureReceiptV1",
    "FutureDrainReceiptV1",
    "FutureHoldoutMetricsV1",
    "FutureHoldoutResultReceiptV1",
    "FutureHoldoutResultV1",
    "FutureStatisticalProtocolV1",
    "MarketDrainRowV1",
    "NoCandidateV1",
    "NormalizedEvidenceObservationV1",
    "PointInTimeCatalogRowV1",
    "PointInTimeCatalogV1",
    "candidate_selection_evidence_sha256",
    "candidate_selection_program_sha256",
    "eligible_universe_sha256",
    "feature_contract_sha256",
    "point_in_time_catalog_sha256",
    "source_contract_sha256",
]
