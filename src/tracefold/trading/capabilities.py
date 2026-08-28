"""Frozen execution capabilities; current permission is snapshot minus blacklist."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final, Literal, Self, TypedDict

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .contracts import canonical_sha256

EXECUTION_CAPABILITY_SNAPSHOT_VERSION: Final[Literal["execution_capability_snapshot_v1"]] = (
    "execution_capability_snapshot_v1"
)
CapabilityExclusionReason = Literal[
    "missing_news_projection",
    "missing_provider_instrument",
    "instrument_identity_mismatch",
    "not_active",
    "not_crypto",
    "not_linear_perpetual",
    "inverse_or_delivery",
    "provider_parse_failed",
    "provider_load_failed",
    "native_stop_unsupported",
]


class ExecutionUniverseCandidateRow(TypedDict):
    venue: str
    venue_symbol: str
    base_symbol: str
    instrument_class: str
    quote_asset: str | None
    status: str
    last_seen_ms: int


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProviderInstrumentCandidateV1(_Frozen):
    instrument_id: str
    native_symbol: str
    base_currency: str
    quote_currency: str
    active: bool
    linear: bool
    inverse: bool
    perpetual: bool
    price_precision: int = Field(ge=0)
    size_precision: int = Field(ge=0)
    price_increment: str
    size_increment: str
    min_quantity: str | None = None
    min_notional: str | None = None
    supports_native_stop: bool = True
    load_error: Literal["provider_parse_failed"] | None = None


class ExecutionInstrumentCapabilityV1(_Frozen):
    instrument_id: str
    native_symbol: str
    underlying_key: str
    quote_currency: str
    venue: Literal["binance.perp"] = "binance.perp"
    product: Literal["binance_usdm_crypto_perpetual"] = "binance_usdm_crypto_perpetual"
    active: Literal[True] = True
    linear: Literal[True] = True
    inverse: Literal[False] = False
    price_precision: int = Field(ge=0)
    size_precision: int = Field(ge=0)
    price_increment: str
    size_increment: str
    min_quantity: str | None = None
    min_notional: str | None = None
    loadable: Literal[True] = True
    executable: Literal[True] = True
    supports_native_stop: Literal[True] = True


class StableCapabilityExclusionV1(_Frozen):
    instrument_id: str
    reason: CapabilityExclusionReason
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ExecutionCapabilitySnapshotV1(_Frozen):
    snapshot_version: Literal["execution_capability_snapshot_v1"] = EXECUTION_CAPABILITY_SNAPSHOT_VERSION
    execution_environment: Literal["BINANCE_USDM_DEMO"] = "BINANCE_USDM_DEMO"
    app_revision: str
    app_image_digest: str
    nautilus_version: Literal["1.231.0"] = "1.231.0"
    nautilus_wheel_identity: str
    news_universe_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_universe_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    included: dict[str, ExecutionInstrumentCapabilityV1]
    excluded: dict[str, StableCapabilityExclusionV1]

    @model_validator(mode="after")
    def validate_partition(self) -> Self:
        if not self.included:
            raise ValueError("execution_capability_snapshot_empty")
        overlap = set(self.included).intersection(self.excluded)
        if overlap:
            raise ValueError("execution_capability_snapshot_overlap")
        if any(key != value.instrument_id for key, value in self.included.items()):
            raise ValueError("execution_capability_included_key_mismatch")
        if any(key != value.instrument_id for key, value in self.excluded.items()):
            raise ValueError("execution_capability_excluded_key_mismatch")
        return self

    @property
    def snapshot_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


def build_execution_capability_snapshot(
    *,
    news_rows: Sequence[ExecutionUniverseCandidateRow],
    provider_rows: Sequence[ProviderInstrumentCandidateV1],
    app_revision: str,
    app_image_digest: str,
    nautilus_wheel_identity: str,
) -> ExecutionCapabilitySnapshotV1:
    """Partition the full mechanical candidate union into included or a closed exclusion."""

    news_by_id: dict[str, ExecutionUniverseCandidateRow] = {}
    for row in sorted(news_rows, key=lambda item: (item["venue_symbol"], item["base_symbol"])):
        instrument_id = f"{row['venue_symbol']}-PERP.BINANCE"
        news_by_id.setdefault(instrument_id, row)
    provider_by_id = {row.instrument_id: row for row in provider_rows}
    included: dict[str, ExecutionInstrumentCapabilityV1] = {}
    excluded: dict[str, StableCapabilityExclusionV1] = {}
    provider_active_ids = {row.instrument_id for row in provider_rows if row.active}
    for instrument_id in sorted(set(news_by_id).union(provider_active_ids)):
        news = news_by_id.get(instrument_id)
        provider = provider_by_id.get(instrument_id)
        reason = _exclusion_reason(news, provider)
        evidence = {
            "instrument_id": instrument_id,
            "news": None if news is None else dict(news),
            "provider": None if provider is None else provider.model_dump(mode="json"),
        }
        if reason is not None:
            excluded[instrument_id] = StableCapabilityExclusionV1(
                instrument_id=instrument_id,
                reason=reason,
                evidence_sha256=canonical_sha256(evidence),
            )
            continue
        if news is None or provider is None:
            raise RuntimeError("included_capability_evidence_missing")
        included[instrument_id] = ExecutionInstrumentCapabilityV1(
            instrument_id=instrument_id,
            native_symbol=provider.native_symbol,
            underlying_key=f"crypto:{str(news['base_symbol']).upper()}",
            quote_currency=provider.quote_currency,
            price_precision=provider.price_precision,
            size_precision=provider.size_precision,
            price_increment=provider.price_increment,
            size_increment=provider.size_increment,
            min_quantity=provider.min_quantity,
            min_notional=provider.min_notional,
        )
    return ExecutionCapabilitySnapshotV1(
        app_revision=app_revision,
        app_image_digest=app_image_digest,
        nautilus_wheel_identity=nautilus_wheel_identity,
        news_universe_digest=canonical_sha256(
            [
                dict(row)
                for row in sorted(
                    news_rows,
                    key=lambda item: (
                        item["venue"],
                        item["venue_symbol"],
                        item["base_symbol"],
                    ),
                )
            ]
        ),
        provider_universe_digest=canonical_sha256(
            [row.model_dump(mode="json") for row in sorted(provider_rows, key=lambda item: item.instrument_id)]
        ),
        included=included,
        excluded=excluded,
    )


def _exclusion_reason(
    news: Mapping[str, object] | None,
    provider: ProviderInstrumentCandidateV1 | None,
) -> CapabilityExclusionReason | None:
    if news is None:
        return "missing_news_projection"
    if provider is None:
        return "missing_provider_instrument"
    if provider.load_error is not None:
        return provider.load_error
    if str(news.get("status")) != "trading" or not provider.active:
        return "not_active"
    if str(news.get("instrument_class")) != "crypto":
        return "not_crypto"
    if not provider.perpetual:
        return "not_linear_perpetual"
    if provider.inverse or not provider.linear:
        return "inverse_or_delivery"
    if (
        provider.native_symbol != str(news.get("venue_symbol"))
        or provider.base_currency != str(news.get("base_symbol"))
        or provider.quote_currency != str(news.get("quote_asset"))
    ):
        return "instrument_identity_mismatch"
    if not provider.supports_native_stop:
        return "native_stop_unsupported"
    return None


__all__ = [
    "EXECUTION_CAPABILITY_SNAPSHOT_VERSION",
    "CapabilityExclusionReason",
    "ExecutionCapabilitySnapshotV1",
    "ExecutionInstrumentCapabilityV1",
    "ExecutionUniverseCandidateRow",
    "ProviderInstrumentCandidateV1",
    "StableCapabilityExclusionV1",
    "build_execution_capability_snapshot",
]
