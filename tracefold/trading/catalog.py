"""Credential-free, content-addressed venue instrument catalogues (#350).

The catalogue records public provider truth.  It does not grant execution permission: #355 compiles
an execution capability partition from one of these snapshots and a closed execution binding.
Every provider row has one entry, including malformed and duplicate rows, so a collector cannot make
an instrument disappear by failing to normalise it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import ClassVar, Final, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .contracts import VenueBinding, canonical_sha256, underlying_key
from .telemetry import (
    TradingExternalDataSource,
    TradingExternalDataTelemetryPort,
    TradingWorkSemantics,
    observe_provider_call,
)

ProductKind = Literal["linear_perpetual", "inverse_perpetual", "delivery_future", "spot", "option", "unknown"]

CATALOG_SNAPSHOT_VERSION: Final[Literal["venue_instrument_catalog_snapshot_v1"]] = (
    "venue_instrument_catalog_snapshot_v1"
)
_BINDING_VENUE: Final[dict[VenueBinding, str]] = {
    "BINANCE_USDM": "binance.usdm",
    "HYPERLIQUID_PERP": "hyperliquid.perp",
}


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class VenueInstrumentCatalogEntryV1(_Frozen):
    """One provider instrument, including the exact reason it could not be normalised."""

    provider_instrument_id: str = Field(min_length=1)
    provider_symbol: str
    venue: str = Field(min_length=1)
    canonical_asset: str | None = None
    canonical_namespace: str | None = None
    product_kind: ProductKind
    active: bool
    listed_at_ms: int | None = None
    delisted_at_ms: int | None = None
    settlement_asset: str | None = None
    margin_asset: str | None = None
    multiplier: str | None = None
    aliases: tuple[str, ...] = ()
    price_increment: str | None = None
    size_increment: str | None = None
    min_quantity: str | None = None
    min_notional: str | None = None
    raw_metadata_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    normalization_error: str | None = None

    @model_validator(mode="after")
    def normalisation_is_explicit(self) -> Self:
        identity_complete = bool(self.canonical_asset and self.canonical_namespace)
        identity_partial = bool(self.canonical_asset) != bool(self.canonical_namespace)
        if identity_partial or (not identity_complete and not self.normalization_error):
            raise ValueError("venue_catalog_normalization_disposition_invalid")
        return self

    @property
    def underlying_key(self) -> str:
        return underlying_key(self.canonical_asset)


class VenueInstrumentCatalogSnapshotV1(_Frozen):
    snapshot_version: Literal["venue_instrument_catalog_snapshot_v1"] = CATALOG_SNAPSHOT_VERSION
    binding: VenueBinding
    venue: str
    captured_at_ms: int
    stale_after_ms: int = Field(gt=0)
    provider_instrument_count: int = Field(ge=0)
    instruments: tuple[VenueInstrumentCatalogEntryV1, ...]

    @model_validator(mode="after")
    def validate_conservation(self) -> Self:
        if self.venue != _BINDING_VENUE[self.binding]:
            raise ValueError("venue_catalog_binding_venue_mismatch")
        if self.provider_instrument_count != len(self.instruments):
            raise ValueError("venue_catalog_instrument_conservation_failed")
        return self

    @property
    def snapshot_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))

    @property
    def normalised_count(self) -> int:
        return sum(row.normalization_error is None for row in self.instruments)

    def resolve(self, canonical_asset: str) -> VenueInstrumentCatalogEntryV1 | None:
        """Deterministically resolve one active linear perp; execution eligibility is not implied."""

        wanted = underlying_key(canonical_asset)
        matches = sorted(
            (
                row
                for row in self.instruments
                if row.normalization_error is None
                and row.active
                and row.product_kind == "linear_perpetual"
                and row.underlying_key == wanted
            ),
            key=lambda row: (
                row.settlement_asset != "USDT",
                row.canonical_namespace or "",
                row.provider_instrument_id,
                row.raw_metadata_sha256,
            ),
        )
        return matches[0] if matches else None


@dataclass(frozen=True, slots=True)
class PreparedVenueCatalogSnapshot:
    """Validated storage input whose CPU-heavy identity work is already complete."""

    snapshot_sha256: str
    binding: VenueBinding
    captured_at_ms: int
    stale_after_ms: int
    provider_instrument_count: int
    payload_json: str

    def __post_init__(self) -> None:
        """Make an unbound digest or metadata tuple impossible to pass to storage."""

        try:
            snapshot = VenueInstrumentCatalogSnapshotV1.model_validate_json(self.payload_json)
        except ValueError as exc:
            raise ValueError("venue_catalog_prepared_identity_invalid") from exc
        canonical_payload = json.dumps(
            snapshot.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        identity = (
            hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest(),
            snapshot.binding,
            snapshot.captured_at_ms,
            snapshot.stale_after_ms,
            snapshot.provider_instrument_count,
            canonical_payload,
        )
        if identity != (
            self.snapshot_sha256,
            self.binding,
            self.captured_at_ms,
            self.stale_after_ms,
            self.provider_instrument_count,
            self.payload_json,
        ):
            raise ValueError("venue_catalog_prepared_identity_invalid")


def build_venue_catalog_snapshot(
    *,
    binding: VenueBinding,
    captured_at_ms: int,
    stale_after_ms: int,
    instruments: Sequence[VenueInstrumentCatalogEntryV1],
) -> VenueInstrumentCatalogSnapshotV1:
    """Freeze provider-order-independent public truth without collapsing duplicate rows."""

    rows = tuple(
        sorted(
            instruments,
            key=lambda row: (
                row.provider_instrument_id,
                row.provider_symbol,
                row.raw_metadata_sha256,
            ),
        )
    )
    return VenueInstrumentCatalogSnapshotV1(
        binding=binding,
        venue=_BINDING_VENUE[binding],
        captured_at_ms=int(captured_at_ms),
        stale_after_ms=int(stale_after_ms),
        provider_instrument_count=len(rows),
        instruments=rows,
    )


def prepare_venue_catalog_snapshot(snapshot: VenueInstrumentCatalogSnapshotV1) -> PreparedVenueCatalogSnapshot:
    """Serialize once before a database transaction and preserve the canonical identity contract."""

    payload_json = json.dumps(
        snapshot.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return PreparedVenueCatalogSnapshot(
        snapshot_sha256=hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
        binding=snapshot.binding,
        captured_at_ms=snapshot.captured_at_ms,
        stale_after_ms=snapshot.stale_after_ms,
        provider_instrument_count=snapshot.provider_instrument_count,
        payload_json=payload_json,
    )


class CatalogRepository(Protocol):
    def store_venue_catalog_snapshot(self, *, prepared: PreparedVenueCatalogSnapshot, now_ms: int) -> bool: ...

    def mark_venue_catalog_unavailable(self, *, binding: VenueBinding, reason: str, now_ms: int) -> None: ...


class CatalogRepositories(Protocol):
    @property
    def trading(self) -> CatalogRepository: ...


class CatalogDatabasePort[RepositoriesT: CatalogRepositories](Protocol):
    async def tx[T](self, name: str, fn: Callable[[RepositoriesT], T], *, timeout_seconds: float) -> T: ...


class VenueCatalog[RepositoriesT: CatalogRepositories]:
    """The catalogue's whole write interface: publish public truth or retain last-good as stale."""

    work_semantics: ClassVar[tuple[TradingWorkSemantics, ...]] = ("latest_state",)

    def __init__(
        self,
        *,
        db: CatalogDatabasePort[RepositoriesT],
        clock: Callable[[], int],
        stale_after_ms: int,
        telemetry: TradingExternalDataTelemetryPort | None = None,
    ) -> None:
        self._db = db
        self._clock = clock
        self._stale_after_ms = int(stale_after_ms)
        self._telemetry = telemetry

    async def observe_provider[T](
        self,
        *,
        source: TradingExternalDataSource,
        call: Awaitable[T],
    ) -> T:
        return await observe_provider_call(
            self._telemetry,
            name="trading_venue_catalog",
            source=source,
            call=call,
        )

    def record_turn(
        self,
        outcome: Literal["error", "partial", "success"],
        seconds: float,
        *,
        source_count: int,
        target_count: int,
    ) -> None:
        if self._telemetry is not None:
            self._telemetry.record_external_data_turn(
                "trading_venue_catalog",
                outcome,
                seconds,
                target_count=target_count,
                source_count=source_count,
            )

    async def publish(
        self,
        *,
        binding: VenueBinding,
        instruments: Sequence[VenueInstrumentCatalogEntryV1],
    ) -> VenueInstrumentCatalogSnapshotV1:
        captured_at_ms = self._clock()
        snapshot = build_venue_catalog_snapshot(
            binding=binding,
            captured_at_ms=captured_at_ms,
            stale_after_ms=self._stale_after_ms,
            instruments=instruments,
        )
        prepared = prepare_venue_catalog_snapshot(snapshot)
        await self._db.tx(
            "trading_venue_catalog_publish",
            lambda repos: repos.trading.store_venue_catalog_snapshot(
                prepared=prepared,
                now_ms=captured_at_ms,
            ),
            timeout_seconds=10.0,
        )
        return snapshot

    async def unavailable(self, *, binding: VenueBinding, reason: str) -> None:
        now_ms = self._clock()
        await self._db.tx(
            "trading_venue_catalog_unavailable",
            lambda repos: repos.trading.mark_venue_catalog_unavailable(
                binding=binding,
                reason=reason,
                now_ms=now_ms,
            ),
            timeout_seconds=10.0,
        )


__all__ = [
    "CATALOG_SNAPSHOT_VERSION",
    "CatalogDatabasePort",
    "PreparedVenueCatalogSnapshot",
    "ProductKind",
    "VenueBinding",
    "VenueCatalog",
    "VenueInstrumentCatalogEntryV1",
    "VenueInstrumentCatalogSnapshotV1",
    "build_venue_catalog_snapshot",
    "prepare_venue_catalog_snapshot",
]
