"""Deterministic native-perp routing for one eligible underlying."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from ..contracts import InstrumentCandidateRow, InstrumentRef, canonical_base_symbol

_NATIVE_PERP_VENUE: Mapping[str, str] = {"binance": "binance.perp", "hyperliquid": "hl.perp"}


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


__all__ = ["resolve_instrument"]
