"""Venue identity for OI frames, and the source-native replay catalogue resolver.

Two jobs, deliberately separate:

* `signal_exchange_id` turns the provider's own venue tag on an OI frame into the exchange it names.
  Closed table on purpose: an unrecognised tag has to fail closed, because the alternative is the
  defect #211 names — a Hyperliquid frame quietly routed to a Binance book whose open interest did
  nothing of the kind.
* `resolve_instrument` picks one provider-native catalogue row for a replay. **Live routing does not use it
  (#331)**: the active execution capability snapshot is the live instrument universe, and resolving
  a Case's instrument from a second catalogue is how a Case came to be frozen against a contract the
  Intent writer would later refuse. Replay partitions both closed bindings independently.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import cast

from .bindings import binding_for_source_venue
from .contracts import ExchangeId, InstrumentCandidateRow, InstrumentRef, canonical_base_symbol

_NATIVE_PERP_VENUE: Mapping[str, str] = {"binance": "binance.perp", "hyperliquid": "hl.perp"}
_EXCHANGE_BY_BINDING: Mapping[str, ExchangeId] = {
    "BINANCE_USDM": "binance",
    "HYPERLIQUID_PERP": "hyperliquid",
}


def signal_exchange_id(venue: object) -> ExchangeId | None:
    """The venue an OI frame's own provider tag names, or `None` when it names nothing executable."""

    binding = binding_for_source_venue(venue)
    return None if binding is None else _EXCHANGE_BY_BINDING[binding]


def resolve_instrument(
    rows: Iterable[InstrumentCandidateRow],
    *,
    priority: Sequence[str],
    observed_at_ms: int,
) -> InstrumentRef | None:
    """Pick exactly one provider-native venue for a replay scenario, in caller order.

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
        binding = binding_for_source_venue(exchange_id)
        if binding is None:  # pragma: no cover - closed table above
            continue
        return InstrumentRef(
            exchange_id=cast(ExchangeId, exchange_id),
            binding=binding,
            venue=str(chosen.get("venue") or ""),
            provider_symbol=provider_symbol,
            base_symbol=canonical_base_symbol(chosen.get("base_symbol")),
            instrument_class=str(chosen.get("instrument_class") or "unknown"),
            quote_asset=(str(chosen["quote_asset"]) if chosen.get("quote_asset") else None),
            observed_at_ms=int(observed_at_ms),
        )
    return None


__all__ = ["resolve_instrument", "signal_exchange_id"]
