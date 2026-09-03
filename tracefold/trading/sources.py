"""One projected OI row in, one typed Source out, or one named source-contract failure.

Pure functions over rows — no database, no clock, no network. The News package owns the read; this
module owns whether the row is a usable market fact at all. Keeping the rule here rather than in SQL
is deliberate: the admission ledger and the check must be the same code, or the ledger eventually
describes a filter the lane no longer applies.

It fails closed on everything it cannot prove. A symbol that canonicalises to nothing, a missing clock,
an unknown direction — each is a named rejection, never a default. Nothing here reads an upstream
judge, Program, policy or cohort: since #510 there is no such field on the row to read.

**Age is not one of these rules.** `normalize_oi_source` answers "is this a usable fact", which is a
property of the row. Whether it is fresh enough to open a Case is Admission's, with its own budget.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .contracts import (
    OiCandidateRow,
    OiTradeCandidate,
    canonical_base_symbol,
)

_SOURCE_VENUES = {
    "binance": "binance.usdm",
    "binance.perp": "binance.usdm",
    "binance.usdm": "binance.usdm",
    "hyperliquid": "hyperliquid.perp",
    "hl.perp": "hyperliquid.perp",
    "hyperliquid.perp": "hyperliquid.perp",
    "hl.xyz": "hyperliquid.xyz",
    "hyperliquid.xyz": "hyperliquid.xyz",
}
_PERPETUAL_BASE_SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9:_-]{0,110}$")


def normalize_source_venue(value: object) -> str | None:
    """Normalize a provider fact's venue without choosing an execution route."""

    return _SOURCE_VENUES.get(str(value or "").strip().lower())


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
    """One projected telemetry fact, or a named source-contract failure. No clock, no policy (#264).

    This is the **source** stage and nothing else: is the row a usable, live OI fact at all? The
    liquidity floor, the deny list, freshness and idempotency belong to Admission, and they used to be
    here as well as in News's SELECT — which is how the same threshold came to be executed in three
    places and a rejection came to be indistinguishable from a row that never existed. (A rank ceiling
    and a per-symbol cooldown were on that list until #348 retired both, and #458 then removed the rank
    itself along with the News push rule that spent it.)
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

    direction = str(row.get("direction") or "").strip().lower()
    if direction not in ("rise", "fall"):
        return SourceRejected(rule="oi_direction_unknown", symbol=symbol)

    # The provider's own venue text decides which book this frame is a claim about, including whether
    # it is Hyperliquid's `hl.xyz` builder DEX. It used to be inferred from an `XYZ-` prefix on a title
    # token the projection carried alongside the fact; the ledger's own `symbol` is already canonical,
    # so the venue field is the only thing that can answer this, and it is the field that means it.
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


__all__ = ["SourceRejected", "normalize_oi_source", "normalize_source_venue"]
