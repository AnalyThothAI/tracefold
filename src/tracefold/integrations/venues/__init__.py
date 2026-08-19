"""Venue instrument-universe adapters (#75): read-only, unauthenticated listing catalogues.

One adapter per venue family. Each returns ``Instrument`` values for the pure layer in ``tracefold.news.instruments``
and raises ``VenueExpectedError`` for every anticipated failure so one unreachable venue never fails a snapshot.
"""

from __future__ import annotations

from .binance import BINANCE_FUTURES_BASE_URL, BINANCE_SPOT_BASE_URL, fetch_binance_instruments
from .errors import VenueExpectedError
from .hyperliquid import HYPERLIQUID_BASE_URL, fetch_hyperliquid_instruments

__all__ = [
    "BINANCE_FUTURES_BASE_URL",
    "BINANCE_SPOT_BASE_URL",
    "HYPERLIQUID_BASE_URL",
    "VenueExpectedError",
    "fetch_binance_instruments",
    "fetch_hyperliquid_instruments",
]
