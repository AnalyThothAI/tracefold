"""Venue identity for OI frames, and the research-only catalogue resolver.

Two jobs, deliberately separate:

* `signal_exchange_id` turns the provider's own venue tag on an OI frame into the exchange it names.
  Closed table on purpose: an unrecognised tag has to fail closed, because the alternative is the
  defect #211 names — a Hyperliquid frame quietly routed to a Binance book whose open interest did
  nothing of the kind.
* `resolve_instrument` picks one catalogue row for a research replay. **Live routing does not use it
  (#331)**: the active execution capability snapshot is the live instrument universe, and resolving
  a Case's instrument from a second catalogue is how a Case came to be frozen against a contract the
  Intent writer would later refuse. Replay is allowed both venues, because research may study a book
  this lane will never trade.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import cast

from .contracts import ExchangeId, InstrumentCandidateRow, InstrumentRef, canonical_base_symbol

_NATIVE_PERP_VENUE: Mapping[str, str] = {"binance": "binance.perp", "hyperliquid": "hl.perp"}
_SIGNAL_VENUE: Mapping[str, ExchangeId] = {"binance": "binance", "hyperliquid": "hyperliquid"}


def signal_exchange_id(venue: object) -> ExchangeId | None:
    """The venue an OI frame's own provider tag names, or `None` when it names nothing executable."""

    return _SIGNAL_VENUE.get(str(venue or "").strip().lower())


def resolve_instrument(
    rows: Iterable[InstrumentCandidateRow],
    *,
    priority: Sequence[str],
    observed_at_ms: int,
) -> InstrumentRef | None:
    """Pick exactly one venue for a research scenario, in the caller's stated order.

    One venue, frozen before anything is evaluated. No split scenario, no simultaneous dual-venue
    evaluation, and no automatic fallback to the other venue. The exact provider symbol comes from the
    catalogue row; a display symbol has never been safe to submit.
    """

    by_venue: dict[str, InstrumentCandidateRow] = {}
    for row in rows:
        venue = str(row.get("venue") or "")
        for exchange_id, native in _NATIVE_PERP_VENUE.items():
            if venue == native:
                by_venue.setdefault(exchange_id, row)
    for exchange_id in priority:
        chosen = by_venue.get(exchange_id)
        if chosen is None:
            continue
        provider_symbol = str(chosen.get("venue_symbol") or "").strip()
        if not provider_symbol:
            continue
        return InstrumentRef(
            exchange_id=cast(ExchangeId, exchange_id),
            venue=str(chosen.get("venue") or ""),
            provider_symbol=provider_symbol,
            base_symbol=canonical_base_symbol(chosen.get("base_symbol")),
            instrument_class=str(chosen.get("instrument_class") or "unknown"),
            quote_asset=(str(chosen["quote_asset"]) if chosen.get("quote_asset") else None),
            observed_at_ms=int(observed_at_ms),
        )
    return None


__all__ = ["resolve_instrument", "signal_exchange_id"]
