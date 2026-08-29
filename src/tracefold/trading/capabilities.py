"""Frozen execution capabilities; current permission is snapshot minus blacklist.

The snapshot is the live instrument universe (#331). Nothing else resolves an instrument for a live
Case: a second catalogue lookup is how a Case came to be frozen against a contract the Intent writer
would later refuse, four stages after the frame that caused it had been consumed.

Every field an Intent depends on is proved here rather than defaulted (#331 §3). A capability that
carries capital authority because a Pydantic default said `True` is a capability nobody granted.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Literal, Self, TypedDict

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .contracts import LIVE_VENUE, canonical_sha256, underlying_key

EXECUTION_CAPABILITY_SNAPSHOT_VERSION: Final[Literal["execution_capability_snapshot_v1"]] = (
    "execution_capability_snapshot_v1"
)
# Every value here has a producer in `_exclusion_reason` and a test that reaches it. `provider_load_failed`
# was removed with #331: the refresh raises before a snapshot is built when the provider cannot be
# loaded, so no row could ever carry it and no persisted snapshot does.
CapabilityExclusionReason = Literal[
    "missing_news_projection",
    "missing_provider_instrument",
    "instrument_identity_mismatch",
    "not_binance_perp_venue",
    "not_active",
    "not_crypto",
    "not_linear_perpetual",
    "inverse_or_delivery",
    "unsupported_quote",
    "provider_parse_failed",
    "native_stop_unsupported",
]
SUPPORTED_QUOTE_CURRENCIES: Final[frozenset[str]] = frozenset({"USDT", "USDC"})


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


def capability_instrument_id(venue_symbol: object) -> str:
    """The one construction of a Binance Demo instrument identity.

    Single owner (#331 §3). The same `f"{symbol}-PERP.BINANCE"` string used to be assembled in the
    snapshot builder, in the Intent writer and in the console at once, so the three could drift apart
    one provider spelling at a time.
    """

    symbol = str(venue_symbol or "").strip()
    return f"{symbol}-PERP.BINANCE" if symbol else ""


def _positive_decimal(value: str, field: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"execution_capability_{field}_invalid") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"execution_capability_{field}_invalid")
    return parsed


def _non_negative_decimal(value: str | None, field: str) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"execution_capability_{field}_invalid") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"execution_capability_{field}_invalid")
    return parsed


class ProviderInstrumentCandidateV1(_Frozen):
    """One Binance USD-M product as the pinned official provider reported it.

    `supports_native_stop` has no default (#331 §3). It is a claim about the venue's order-type
    contract for this exact product, and a default would grant capital authority to an instrument the
    provider never said could carry a native stop — which is the only protection this lane has.
    """

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
    supports_native_stop: bool
    load_error: Literal["provider_parse_failed"] | None = None

    @model_validator(mode="after")
    def validate_increments(self) -> Self:
        """A tick or lot size that is absent, unparseable or non-positive is not a sizing rule.

        Quantity is `floor(notional / price)` rounded to `size_increment`; a zero or negative
        increment makes that arithmetic meaningless, and a snapshot carrying one would let the
        execution authority compute a quantity from a number nobody can divide by.
        """

        if self.load_error is None:
            _positive_decimal(self.price_increment, "price_increment")
            _positive_decimal(self.size_increment, "size_increment")
            _non_negative_decimal(self.min_quantity, "min_quantity")
            _non_negative_decimal(self.min_notional, "min_notional")
        return self


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

    @field_validator("price_increment", "size_increment")
    @classmethod
    def validate_increment(cls, value: str) -> str:
        _positive_decimal(value, "increment")
        return value


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

    def resolve(self, underlying: str) -> ExecutionInstrumentCapabilityV1 | None:
        """The one executable Binance Demo contract for an issuer, or None.

        Deterministic where an issuer is listed against more than one quote asset: `USDT` first, then
        the lexicographically smallest instrument id. An arbitrary `next(...)` would let two identical
        scans freeze two different contracts for the same frame.
        """

        if not underlying:
            return None
        matches = sorted(
            (row for row in self.included.values() if row.underlying_key == underlying),
            key=lambda row: (row.quote_currency != "USDT", row.instrument_id),
        )
        return matches[0] if matches else None


def build_execution_capability_snapshot(
    *,
    news_rows: Sequence[ExecutionUniverseCandidateRow],
    provider_rows: Sequence[ProviderInstrumentCandidateV1],
    app_revision: str,
    app_image_digest: str,
    nautilus_wheel_identity: str,
) -> ExecutionCapabilitySnapshotV1:
    """Partition the full mechanical candidate union into included or a closed exclusion.

    Duplicate rows for one instrument id fail closed unless they are byte-identical (#331 §3). The
    previous `setdefault` / dict-overwrite silently picked whichever row the query happened to return
    first, so two conflicting descriptions of the same contract produced a snapshot that depended on
    row order — and a snapshot digest that was not a function of the universe.
    """

    news_by_id = _unique_by_instrument(
        ((capability_instrument_id(row["venue_symbol"]), dict(row)) for row in news_rows),
        error="execution_capability_news_row_conflict",
    )
    provider_by_id = _unique_by_instrument(
        ((row.instrument_id, row.model_dump(mode="json")) for row in provider_rows),
        error="execution_capability_provider_row_conflict",
    )
    provider_models = {row.instrument_id: row for row in provider_rows}
    included: dict[str, ExecutionInstrumentCapabilityV1] = {}
    excluded: dict[str, StableCapabilityExclusionV1] = {}
    for instrument_id in sorted(set(news_by_id).union(provider_by_id)):
        news = news_by_id.get(instrument_id)
        provider = provider_models.get(instrument_id)
        reason = _exclusion_reason(news, provider)
        evidence = {
            "instrument_id": instrument_id,
            "news": news,
            "provider": None if provider is None else provider.model_dump(mode="json"),
        }
        if reason is not None:
            excluded[instrument_id] = StableCapabilityExclusionV1(
                instrument_id=instrument_id,
                reason=reason,
                evidence_sha256=canonical_sha256(evidence),
            )
            continue
        if news is None or provider is None:  # pragma: no cover - proved by `_exclusion_reason`
            raise RuntimeError("included_capability_evidence_missing")
        included[instrument_id] = ExecutionInstrumentCapabilityV1(
            instrument_id=instrument_id,
            native_symbol=provider.native_symbol,
            underlying_key=underlying_key(news["base_symbol"]),
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


def _unique_by_instrument(
    pairs: Iterable[tuple[str, dict[str, Any]]],
    *,
    error: str,
) -> dict[str, dict[str, Any]]:
    """Collapse identical duplicates; refuse conflicting ones.

    Byte-stability matters here beyond tidiness: the snapshot digest is what an Intent pins, so two
    runs over the same universe must produce the same document whatever order the rows arrived in.
    """

    out: dict[str, dict[str, Any]] = {}
    for instrument_id, payload in pairs:
        if not instrument_id:
            continue
        existing = out.get(instrument_id)
        if existing is None:
            out[instrument_id] = payload
            continue
        if canonical_sha256(existing) != canonical_sha256(payload):
            raise ValueError(f"{error}:{instrument_id}")
    return out


def _exclusion_reason(
    news: Mapping[str, object] | None,
    provider: ProviderInstrumentCandidateV1 | None,
) -> CapabilityExclusionReason | None:
    if news is None:
        return "missing_news_projection"
    if str(news.get("venue")) != LIVE_VENUE:
        # The caller's name is not a contract (#331 §3). A projection change that started returning a
        # second venue would otherwise have granted Binance Demo capital authority to an `hl.perp` row.
        return "not_binance_perp_venue"
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
    if provider.quote_currency not in SUPPORTED_QUOTE_CURRENCIES:
        return "unsupported_quote"
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
    "SUPPORTED_QUOTE_CURRENCIES",
    "CapabilityExclusionReason",
    "ExecutionCapabilitySnapshotV1",
    "ExecutionInstrumentCapabilityV1",
    "ExecutionUniverseCandidateRow",
    "ProviderInstrumentCandidateV1",
    "StableCapabilityExclusionV1",
    "build_execution_capability_snapshot",
    "capability_instrument_id",
]
