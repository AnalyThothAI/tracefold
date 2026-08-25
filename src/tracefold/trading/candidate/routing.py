"""Deterministic native-perp routing for one eligible underlying."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from ..contracts import InstrumentCandidateRow, InstrumentRef, LiveExchangeId, canonical_base_symbol

_NATIVE_PERP_VENUE: Mapping[str, str] = {"binance": "binance.perp", "hyperliquid": "hl.perp"}
# The provider's own venue tag on an OI frame, mapped to the venue that would execute it. Deliberately
# a closed table: an unrecognised tag has to fail closed, because the alternative is the defect #211
# names — a Hyperliquid frame quietly routed to a Binance book whose open interest did nothing of the
# kind. The measured venue split (Hyperliquid +1.35% vs Binance -0.26% at 4 h) is why that matters.
_SIGNAL_VENUE: Mapping[str, LiveExchangeId] = {"binance": "binance", "hyperliquid": "hyperliquid"}


def signal_exchange_id(venue: object) -> LiveExchangeId | None:
    """The venue an OI frame's own provider tag names, or `None` when it names nothing executable."""

    return _SIGNAL_VENUE.get(str(venue or "").strip().lower())


def resolve_instrument(
    rows: Iterable[InstrumentCandidateRow],
    *,
    priority: Sequence[str],
    observed_at_ms: int,
) -> InstrumentRef | None:
    """Pick exactly one venue, in the operator's static priority order.

    One venue, frozen before any write. No split order, no simultaneous dual-venue order, and — the
    rule that matters after a timeout — no automatic fallback to the other venue. The exact provider
    symbol comes from the catalogue row; a display symbol has never been safe to submit.
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
            exchange_id=exchange_id,
            venue=str(chosen.get("venue") or ""),
            provider_symbol=provider_symbol,
            base_symbol=canonical_base_symbol(chosen.get("base_symbol")),
            instrument_class=str(chosen.get("instrument_class") or "unknown"),
            quote_asset=(str(chosen["quote_asset"]) if chosen.get("quote_asset") else None),
            observed_at_ms=int(observed_at_ms),
        )
    return None


__all__ = ["resolve_instrument", "signal_exchange_id"]
