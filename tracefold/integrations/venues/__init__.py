"""Venue adapters: read-only, unauthenticated catalogues (#75), current quotes and candle history (#88).

One adapter per venue family. The catalogue half returns ``Instrument`` values for the pure layer in
``tracefold.news.market_review.instruments``; the price half returns ``ProviderQuote`` / ``Candle`` for
``tracefold.news.market_review.pricing``. All of them raise ``VenueExpectedError`` for every anticipated failure, so one
unreachable venue never fails a snapshot and never clears another venue's quotes.

``us_reference`` is the one adapter that does not describe a venue we could trade on (#91): it answers "is this
symbol a stock?" for the thousands of tickers no crypto exchange lists, and its rows live in a reference tier the
class map consults only when no real venue knows the symbol. It is never a price source.

This root re-exports exactly the names a composition seam imports *from the package*. Nineteen more
were re-exported here for no such caller -- base URLs, ``VenueExpectedError``, and the per-venue trade
and candle fetchers the price layer reaches through ``candle_fetcher_for`` or by module. Each still
lives in the module that owns it, which is where the tests and the adapters already import it from,
so a second spelling here only made the package root a mirror of its own contents (#589 PR-2).
"""

from __future__ import annotations

from .binance import fetch_binance_instruments
from .candles import (
    candle_fetcher_for,
    fetch_binance_candles,
    fetch_hyperliquid_candles,
)
from .delivery_prices import fetch_delivery_price_points
from .hyperliquid import fetch_hyperliquid_instruments
from .okx import fetch_okx_instruments
from .quotes import (
    fetch_binance_futures_day_quotes,
    fetch_binance_futures_quotes,
    fetch_binance_spot_day_quotes,
    fetch_binance_spot_quotes,
    fetch_hyperliquid_quotes,
    fetch_okx_quotes,
)
from .tradability import VenueCatalogTradabilityVerifier
from .us_reference import fetch_us_reference_instruments

__all__ = [
    "VenueCatalogTradabilityVerifier",
    "candle_fetcher_for",
    "fetch_binance_candles",
    "fetch_binance_futures_day_quotes",
    "fetch_binance_futures_quotes",
    "fetch_binance_instruments",
    "fetch_binance_spot_day_quotes",
    "fetch_binance_spot_quotes",
    "fetch_delivery_price_points",
    "fetch_hyperliquid_candles",
    "fetch_hyperliquid_instruments",
    "fetch_hyperliquid_quotes",
    "fetch_okx_instruments",
    "fetch_okx_quotes",
    "fetch_us_reference_instruments",
]
