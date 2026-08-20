"""US listed-symbol reference directory (#91): every ticker on Nasdaq / NYSE / NYSE American / Cboe / IEX.

This is not a venue we can trade on, and the rows say so — ``venue = "us.listed"`` puts them in a reference tier
that only answers "is this symbol a stock?". The instrument universe needs it because Binance and Hyperliquid
between them list about 200 equities, while the provider tags thousands: a week of live traffic had 133 Events
whose only grounded tag named a company with no crypto perp (`UWMC`, `HUBG`, `TLX`, `PLD`), every one of them read
as a crypto headline. 95 of those 133 are in this directory.

Index membership is not enough and was measured: S&P 500 covers 4.6% of the unmatched tag volume, because the
large caps already have Binance TradFi perps — what is missing is the small-cap tail no index carries.

Nasdaq Trader publishes the two files below with no credentials and no key, which is also the only key-free path
inside data platforms that wrap this (`openbb-nasdaq`, `openbb-sec`); SEC's `company_tickers.json` is an
equivalent source that recognises exactly the same symbols but ships no ETF flag.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from typing import Final

import httpx

from tracefold.news.instruments import Instrument, is_valid_symbol

from .errors import VenueExpectedError

US_REFERENCE_BASE_URL: Final = "https://www.nasdaqtrader.com"
US_REFERENCE_VENUE: Final = "us.listed"
_TIMEOUT_SECONDS: Final = 20.0
# The two files are ~350 KB and ~520 KB; the cap is for a pathological response, not for these.
_MAX_BYTES: Final = 16 * 1024 * 1024
# Each file carries thousands of rows. A header-and-trailer answer is a broken file, not a delisting of everything
# it used to hold — and without this floor `apply_snapshot` would mark ~6.5k reference rows delisted in one turn,
# switching the whole tier off until a good snapshot lands. Same reflex as "a venue that did not answer is not a
# mass delisting", one level down.
_MIN_ROWS_PER_FILE: Final = 100
# (path, symbol column). The rest of the header is identical enough to read by name.
_FILES: Final[tuple[tuple[str, str], ...]] = (
    ("/dynamic/SymDir/nasdaqlisted.txt", "Symbol"),
    ("/dynamic/SymDir/otherlisted.txt", "ACT Symbol"),
)


async def fetch_us_reference_instruments(
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    base_url: str = US_REFERENCE_BASE_URL,
) -> tuple[Instrument, ...]:
    """Both symbol directories, test issues excluded. One venue string for both, since neither is a venue."""

    out: list[Instrument] = []
    seen: set[str] = set()
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(_TIMEOUT_SECONDS), follow_redirects=False, transport=transport
    ) as client:
        for path, symbol_column in _FILES:
            text = await _get(client, f"{base_url.rstrip('/')}{path}")
            kept = 0
            for row in _rows(text):
                symbol = str(row.get(symbol_column) or "").strip().upper()
                if not symbol or symbol in seen or not is_valid_symbol(symbol):
                    continue
                if str(row.get("Test Issue") or "").strip().upper() == "Y":
                    continue
                seen.add(symbol)
                kept += 1
                out.append(
                    Instrument(
                        venue=US_REFERENCE_VENUE,
                        venue_symbol=symbol,
                        base_symbol=symbol,
                        # The file's own ETF flag, for the same reason the Binance adapter reads `underlyingType`:
                        # a source that classifies its own rows should not be re-guessed. Both classes are "not
                        # crypto" to the only consumer, so the distinction is descriptive, not load-bearing.
                        instrument_class="index" if str(row.get("ETF") or "").strip().upper() == "Y" else "equity",
                    )
                )
            if kept < _MIN_ROWS_PER_FILE:
                raise VenueExpectedError("venue_payload_empty", venue=US_REFERENCE_VENUE)
    return tuple(out)


def _rows(text: str) -> Iterator[Mapping[str, str]]:
    """Pipe-delimited, one header line, and a `File Creation Time` trailer.

    The trailer is padded to the full field count, so counting fields does not catch it — it has to be named. It
    would otherwise arrive as a symbol, and only the no-whitespace rule in `is_valid_symbol` would stop it.
    """

    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise VenueExpectedError("venue_payload_invalid", venue=US_REFERENCE_VENUE)
    header = lines[0].split("|")
    if len(header) < 3:
        raise VenueExpectedError("venue_payload_invalid", venue=US_REFERENCE_VENUE)
    for line in lines[1:]:
        if line.startswith("File Creation Time"):
            continue
        fields: Sequence[str] = line.split("|")
        if len(fields) != len(header):
            continue  # a truncated row
        yield dict(zip(header, fields, strict=True))


async def _get(client: httpx.AsyncClient, url: str) -> str:
    try:
        response = await client.get(url)
    except httpx.TimeoutException:
        raise VenueExpectedError("venue_timeout", venue=US_REFERENCE_VENUE) from None
    except httpx.HTTPError:
        raise VenueExpectedError("venue_http_error", venue=US_REFERENCE_VENUE) from None
    if response.status_code in {403, 451}:
        raise VenueExpectedError("venue_blocked", venue=US_REFERENCE_VENUE, status_code=response.status_code)
    if response.status_code == 429:
        raise VenueExpectedError("venue_rate_limited", venue=US_REFERENCE_VENUE, status_code=response.status_code)
    # Redirects are not followed, so a 3xx would otherwise arrive as a short HTML body and be reported as a
    # corrupt directory. The status code is the diagnostic that matters on a host that moves its paths around.
    if response.status_code >= 300:
        raise VenueExpectedError("venue_http_error", venue=US_REFERENCE_VENUE, status_code=response.status_code)
    if len(response.content) > _MAX_BYTES:
        raise VenueExpectedError("venue_payload_too_large", venue=US_REFERENCE_VENUE)
    return response.text


__all__ = ["US_REFERENCE_BASE_URL", "US_REFERENCE_VENUE", "fetch_us_reference_instruments"]
