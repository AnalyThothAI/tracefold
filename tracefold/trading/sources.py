"""One projected OI row in, one typed Source out, or one named source-contract failure.

Pure functions over rows — no database, no clock, no network. The News package owns the read; this
module owns whether the row is a usable market fact at all. Keeping the rule here rather than in SQL
is deliberate: the admission ledger and the check must be the same code, or the ledger eventually
describes a filter the lane no longer applies.

It fails closed on everything it cannot prove. A symbol that canonicalises to nothing, a missing clock,
an unknown direction — each is a named rejection, never a default. Nothing here reads an upstream
judge, Program, policy or cohort: no such field exists on the row.

**Age is not one of these rules.** `normalize_oi_source` answers "is this a usable fact", which is a
property of the row. Whether it is fresh enough to open a Case is Admission's, with its own budget.

**Neither is direction.** The rule here proves the provider wrote a direction this system can read at
all — `rise` or `fall` — and stops. Which direction may be *traded* is one rule in one place, the
policy's `not_oi_rise`, so a `fall` frame reaches a Case and is refused there by name with the rest of
its evidence. A second refusal here would make "the provider said nothing" and "the strategy is long
only" the same absence.

**The venue table lives here.** `SOURCE_VENUES` is the single answer to every question this package
asks about a source venue: which provider spellings name it, which provider family answers its public
reads, and how that venue spells the market.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Final

from .contracts import (
    OiCandidateRow,
    OiTradeCandidate,
    canonical_base_symbol,
)
from .telemetry import TradingExternalDataSource

_PERPETUAL_BASE_SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9:_-]{0,110}$")


@dataclass(frozen=True, slots=True)
class SourceVenue:
    """One supported source venue: every provider spelling of it, and the reads it answers.

    Four tables used to answer that: the alias map here, the candle fetcher's `if` ladder in the
    Workers wiring, a SQL `CASE` in the News trade projection that had already drifted (no `hl.xyz`),
    and two literal lists in the admission digest and the telemetry vocabulary. A venue that four
    places spell is a venue three of them can forget, so this is the one table and every caller reads
    a field off it.

    None of these fields is an execution route. `price_venue` and `price_symbol_format` say where the
    *public* bars behind this frame are read and how that venue spells the market; an order's venue is
    the Runtime's own catalogue and is decided nowhere in this package.
    """

    key: str
    aliases: tuple[str, ...]
    telemetry_source: TradingExternalDataSource
    price_venue: str
    price_symbol_format: str

    def price_symbol(self, base_symbol: str) -> str:
        """This venue's own spelling of the market: `SOLUSDT`, `SOL`, or `xyz:AAPL`."""

        return self.price_symbol_format.format(base=base_symbol)


SOURCE_VENUES: Final[tuple[SourceVenue, ...]] = (
    SourceVenue(
        key="binance.usdm",
        aliases=("binance", "binance.perp", "binance.usdm"),
        telemetry_source="binance",
        price_venue="binance.perp",
        price_symbol_format="{base}USDT",
    ),
    SourceVenue(
        key="hyperliquid.perp",
        aliases=("hl.perp", "hyperliquid", "hyperliquid.perp"),
        telemetry_source="hyperliquid",
        price_venue="hl.perp",
        price_symbol_format="{base}",
    ),
    SourceVenue(
        key="hyperliquid.xyz",
        aliases=("hl.xyz", "hyperliquid.xyz"),
        telemetry_source="hyperliquid",
        price_venue="hl.xyz",
        price_symbol_format="xyz:{base}",
    ),
)
SOURCE_VENUE_KEYS: Final[tuple[str, ...]] = tuple(sorted(venue.key for venue in SOURCE_VENUES))
_BY_ALIAS: Final[dict[str, SourceVenue]] = {
    alias: venue for venue in SOURCE_VENUES for alias in (venue.key, *venue.aliases)
}


def source_venue(value: object) -> SourceVenue | None:
    """The supported venue a provider spelling names, or `None` when nothing here supports it."""

    return _BY_ALIAS.get(str(value or "").strip().lower())


def normalize_source_venue(value: object) -> str | None:
    """Normalize a provider fact's venue without choosing an execution route."""

    resolved = source_venue(value)
    return None if resolved is None else resolved.key


def telemetry_source(value: object) -> TradingExternalDataSource:
    """Which provider family answers this venue's public reads. Resolved venues only."""

    resolved = source_venue(value)
    if resolved is None:
        raise ValueError("trading_source_venue_unresolved")
    return resolved.telemetry_source


@dataclass(frozen=True, slots=True)
class SourceRejected:
    """A row that is not a usable OI fact, and the rule that says so."""

    rule: str
    symbol: str = ""


def _int(value: Any, default: int | None = None) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize_oi_source(row: OiCandidateRow) -> OiTradeCandidate | SourceRejected:
    """One projected telemetry fact, or a named source-contract failure. No clock, no policy.

    This is the **source** stage and nothing else: is the row a usable, live OI fact at all? The
    liquidity floor, freshness and idempotency belong to Admission, and each rule has exactly one of
    the two homes — a threshold executed in both makes a rejection indistinguishable from a row that
    never existed.
    """

    symbol = canonical_base_symbol(row.get("symbol"))
    if not symbol:
        return SourceRejected(rule="symbol_not_canonicalisable")
    # The engine-neutral key is `crypto:perp:{symbol}:USDT`, whose public contract is at most 128
    # ASCII identity characters. Refuse an unusable key while the source can still receive a durable
    # admission answer; discovering it after bars are fetched would fault the shared Workers process.
    if _PERPETUAL_BASE_SYMBOL.fullmatch(symbol) is None:
        return SourceRejected(rule="market_key_invalid", symbol=symbol)
    if str(row.get("ingest_mode") or "") != "live":
        return SourceRejected(rule="not_live_ingest", symbol=symbol)

    observed = _int(row.get("observed_at_ms"))
    if observed is None:
        return SourceRejected(rule="observed_at_missing", symbol=symbol)

    # The Case freezes both clocks, so a row that cannot say when it became readable cannot be frozen.
    available = _int(row.get("available_at_ms"))
    if available is None:
        return SourceRejected(rule="available_at_missing", symbol=symbol)

    measurements = {
        "oi_change_bps": _int(row.get("oi_change_bps")),
        "oi_value_usd": _int(row.get("oi_value_usd")),
        "whale_long_profit_bps": _int(row.get("whale_long_profit_bps")),
        "whale_oi_ratio_bps": _int(row.get("whale_oi_ratio_bps")),
    }
    missing_measurement = next((name for name, value in measurements.items() if value is None), None)
    if missing_measurement is not None:
        return SourceRejected(rule=f"{missing_measurement}_missing", symbol=symbol)

    # A word this system can read, not a word it trades: `fall` passes here and the policy refuses it.
    direction = str(row.get("direction") or "").strip().lower()
    if direction not in ("rise", "fall"):
        return SourceRejected(rule="oi_direction_unknown", symbol=symbol)

    # The provider's own venue text decides which book this frame is a claim about, including whether
    # it is Hyperliquid's `hl.xyz` builder DEX. The ledger's own `symbol` is already canonical, so the
    # venue field is the only field that can answer this, and it is the field that means it.
    raw_venue = str(row.get("venue") or "").strip().lower()
    venue = normalize_source_venue(raw_venue) or raw_venue
    return OiTradeCandidate(
        event_id=str(row.get("event_id") or ""),
        metric_version=str(row.get("metric_version") or ""),
        source_item_id=str(row.get("source_item_id") or ""),
        observed_at_ms=observed,
        available_at_ms=available,
        base_symbol=symbol,
        venue=venue,
        oi_direction=direction,
        oi_change_bps=measurements["oi_change_bps"],
        oi_value_usd=measurements["oi_value_usd"],
        whale_long_profit_bps=measurements["whale_long_profit_bps"],
        whale_oi_ratio_bps=measurements["whale_oi_ratio_bps"],
        # Carried, never defaulted. A frame whose measurement window the provider contract could
        # not prove reaches the policy as `None`, and the policy refuses it by name (#265).
        source_strategy_id=(str(row["source_strategy_id"]) if row.get("source_strategy_id") else None),
        source_contract_version=(str(row["source_contract_version"]) if row.get("source_contract_version") else None),
        measurement_window_ms=_int(row.get("measurement_window_ms")),
    )


__all__ = [
    "SOURCE_VENUES",
    "SOURCE_VENUE_KEYS",
    "SourceRejected",
    "SourceVenue",
    "normalize_oi_source",
    "normalize_source_venue",
    "source_venue",
    "telemetry_source",
]
