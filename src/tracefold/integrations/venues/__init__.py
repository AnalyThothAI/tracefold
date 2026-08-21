"""Venue adapters: read-only, unauthenticated catalogues (#75), current quotes and candle history (#88).

One adapter per venue family. The catalogue half returns ``Instrument`` values for the pure layer in
``tracefold.news.instruments``; the price half returns ``ProviderQuote`` / ``Candle`` for
``tracefold.news.pricing``. All of them raise ``VenueExpectedError`` for every anticipated failure, so one
unreachable venue never fails a snapshot and never clears another venue's quotes.

``us_reference`` is the one adapter that does not describe a venue we could trade on (#91): it answers "is this
symbol a stock?" for the thousands of tickers no crypto exchange lists, and its rows live in a reference tier the
class map consults only when no real venue knows the symbol. It is never a price source.
"""

from __future__ import annotations

from .binance import BINANCE_FUTURES_BASE_URL, BINANCE_SPOT_BASE_URL, fetch_binance_instruments
from .candles import fetch_binance_candles, fetch_hyperliquid_candles
from .errors import VenueExpectedError
from .hyperliquid import HYPERLIQUID_BASE_URL, fetch_hyperliquid_instruments
from .quotes import (
    fetch_binance_futures_day_quotes,
    fetch_binance_futures_quotes,
    fetch_binance_spot_day_quotes,
    fetch_binance_spot_quotes,
    fetch_hyperliquid_quotes,
)
from .us_reference import US_REFERENCE_BASE_URL, US_REFERENCE_VENUE, fetch_us_reference_instruments

__all__ = [
    "BINANCE_FUTURES_BASE_URL",
    "BINANCE_SPOT_BASE_URL",
    "HYPERLIQUID_BASE_URL",
    "US_REFERENCE_BASE_URL",
    "US_REFERENCE_VENUE",
    "VenueExpectedError",
    "fetch_binance_candles",
    "fetch_binance_futures_day_quotes",
    "fetch_binance_futures_quotes",
    "fetch_binance_instruments",
    "fetch_binance_spot_day_quotes",
    "fetch_binance_spot_quotes",
    "fetch_hyperliquid_candles",
    "fetch_hyperliquid_instruments",
    "fetch_hyperliquid_quotes",
    "fetch_us_reference_instruments",
]
