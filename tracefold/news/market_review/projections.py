"""Public quote/reaction/review projections for Market Review storage."""

from __future__ import annotations

from typing import Any

from .instruments import normalize_symbol
from .pricing import (
    PriceInstrument,
    price_kind_for,
    price_kind_zh,
    quote_state_zh,
)


def _unlisted_quote(symbol: str) -> dict[str, Any]:
    return {
        "requested_symbol": symbol,
        "symbol": normalize_symbol(symbol),
        "base_symbol": normalize_symbol(symbol),
        "venue": None,
        "venue_symbol": None,
        "instrument_class": None,
        "quote_asset": None,
        "price": None,
        "price_kind": None,
        "price_kind_zh": "",
        "change_pct": None,
        "change_basis": None,
        "change_basis_zh": "",
        "source_at_ms": None,
        "received_at_ms": None,
        "received_age_ms": None,
        "source_age_ms": None,
        "effective_age_ms": None,
        "freshness_basis": None,
        "reference_at_ms": None,
        "reference_age_ms": None,
        "state": "unlisted",
        "state_zh": quote_state_zh("unlisted"),
    }


def _directory_only_quote(symbol: str, market_type: str) -> dict[str, Any]:
    """A typed question the catalogue answers only from a reference directory (#651 §6.2).

    `V/equity` is a real NYSE ticker in `us.listed` and no venue we poll prices it. `unlisted` would say
    the symbol names nothing, which is false; the same-name coin would be a different instrument's price
    under this Event's ticker, which is the failure this whole cut is about. The honest third answer is
    `unavailable` with the market the question asked for, so a reader and an operator both see that the
    instrument is real and the price is not available here.
    """

    return {
        **_unlisted_quote(symbol),
        "instrument_class": market_type,
        "state": "unavailable",
        "state_zh": quote_state_zh("unavailable"),
    }


def _unavailable_quote(symbol: str, instrument: PriceInstrument) -> dict[str, Any]:
    return {
        "requested_symbol": symbol,
        "symbol": instrument.base_symbol,
        "base_symbol": instrument.base_symbol,
        "venue": instrument.venue,
        "venue_symbol": instrument.venue_symbol,
        "instrument_class": instrument.instrument_class,
        "quote_asset": instrument.quote_asset,
        "price": None,
        "price_kind": price_kind_for(instrument.venue),
        "price_kind_zh": price_kind_zh(price_kind_for(instrument.venue)),
        "change_pct": None,
        "change_basis": None,
        "change_basis_zh": "",
        "source_at_ms": None,
        "received_at_ms": None,
        "received_age_ms": None,
        "source_age_ms": None,
        "effective_age_ms": None,
        "freshness_basis": None,
        "reference_at_ms": None,
        "reference_age_ms": None,
        "state": "unavailable",
        "state_zh": quote_state_zh("unavailable"),
    }
