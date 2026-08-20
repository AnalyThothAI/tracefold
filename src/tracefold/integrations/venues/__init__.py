"""Venue instrument-universe adapters (#75): read-only, unauthenticated listing catalogues.

One adapter per venue family. Each returns ``Instrument`` values for the pure layer in ``tracefold.news.instruments``
and raises ``VenueExpectedError`` for every anticipated failure so one unreachable venue never fails a snapshot.

``us_reference`` is the one adapter that does not describe a venue we could trade on (#91): it answers "is this
symbol a stock?" for the thousands of tickers no crypto exchange lists, and its rows live in a reference tier the
class map consults only when no real venue knows the symbol.
"""

from __future__ import annotations

from .binance import BINANCE_FUTURES_BASE_URL, BINANCE_SPOT_BASE_URL, fetch_binance_instruments
from .errors import VenueExpectedError
from .hyperliquid import HYPERLIQUID_BASE_URL, fetch_hyperliquid_instruments
from .us_reference import US_REFERENCE_BASE_URL, US_REFERENCE_VENUE, fetch_us_reference_instruments

__all__ = [
    "BINANCE_FUTURES_BASE_URL",
    "BINANCE_SPOT_BASE_URL",
    "HYPERLIQUID_BASE_URL",
    "US_REFERENCE_BASE_URL",
    "US_REFERENCE_VENUE",
    "VenueExpectedError",
    "fetch_binance_instruments",
    "fetch_hyperliquid_instruments",
    "fetch_us_reference_instruments",
]
