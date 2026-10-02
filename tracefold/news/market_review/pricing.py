"""Current quote contracts, source precedence and shared venue price selection.

Quote snapshots hold bounded latest display values with explicit clocks and freshness. Shared trade,
candle and return helpers serve delivery-time enrichment and other active venue readers. Provider I/O,
repositories and clocks remain outside these pure functions.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, DivisionByZero, InvalidOperation
from math import isfinite
from typing import Any, Final, Literal

from .instruments import InstrumentClass

# Reference tiers (`us.listed`) are excluded where candidates are selected — in the repository's SQL,
# which is the one place resolution happens. There is deliberately no second Python copy of that rule.
PriceKind = Literal["last", "mark", "mid"]
QuoteState = Literal["fresh", "stale", "unavailable", "unlisted"]
FreshnessBasis = Literal["source_and_received", "received_only"]

# Shared venue candle alignment and gap tolerance.
CANDLE_INTERVAL: Final = "1m"
CANDLE_INTERVAL_MS: Final = 60_000
# One interval plus provider timestamp jitter. Wide enough that a boundary rounding difference between two
# venues does not read as a hole, narrow enough that a halted or illiquid session never forward-fills.
CANDLE_GAP_TOLERANCE_MS: Final = CANDLE_INTERVAL_MS + 30_000

# ---------------------------------------------------------------------------- code-owned budgets
# 20 s, not 5. The five-second cadence was written for a freshness SLO the product never had: the browser
# reads over HTTP polling, so no price can reach a reader faster than that poll anyway. What it did buy was
# bandwidth — Binance's USD-M ticker has no `symbols=` filter, so every turn pulled the whole market (270 kB
# = 276,517 bytes), and at 5 s that measured 6.8 MB/min ≈ 9.8 GB/day in production. Quartering the cadence
# quarters that while leaving the console's actual refresh behaviour unchanged.
QUOTE_PERIOD_SECONDS: Final = 20.0
# How often a Binance source pays for the wider `ticker/24hr` (270 kB), after the mandatory current response
# has been stored. At most the spot and perpetual sources add one optional call each; they never enter the
# current phase's deadline. What ages between day reads is the 24 h window's open, not the percentage: it is
# recomputed from every turn's own price, so it never disagrees with the number beside it, and a window open
# moves 0.023% per turn. Five minutes of that is 0.35%, against the 6x payload it would cost to chase.
QUOTE_DAY_PERIOD_SECONDS: Final = 300.0
QUOTE_TURN_DEADLINE_SECONDS: Final = 10.0
# How long a *card* may wait for a display quote before going out without one. It belongs beside the
# other quote budgets rather than inside either renderer: the News first card and the market card
# have one answer between them, and each applies it itself (#562 §3).
QUOTE_READ_TIMEOUT_SECONDS: Final = 1.5
# The 24 h reference is read on the 300 s day cadence above, so a 360 s window left exactly one missed
# read between "the percentage is there" and "the card shows no 24 h change at all" -- 60 s of slack for
# a read that is optional by construction and never enters the turn deadline. 600 s gives it a whole
# missed read plus the same slack, and it does not widen what the number means: the window's open moves
# 0.023% per turn (see `QUOTE_DAY_PERIOD_SECONDS`), so ten minutes of drift is 0.46% of the open, well
# inside the rounding the card already prints (#562 §5 row 10).
QUOTE_REFERENCE_MAX_AGE_MS: Final = 600_000
QUOTE_MAX_FUTURE_SKEW_MS: Final = 5_000
QUOTE_LOOKBACK_MS: Final = 72 * 3_600_000
QUOTE_TARGET_MAX: Final = 256
QUOTE_SOURCE_GROUP_MAX: Final = 12
# A healthy 20 s start-based collector has one full missed-turn allowance before the display says so.
QUOTE_FRESH_MAX_AGE_MS: Final = 45_000
QUOTE_REQUEST_SYMBOL_MAX: Final = 100

EXTERNAL_CONCURRENCY: Final = 4

REVIEW_MAX_HOURS: Final = 720
REVIEW_POTENTIAL_MISS_LIMIT: Final = 50

# ---------------------------------------------------------------------------- source selection strategy
# The one place venue precedence is written down. `asset_refs` (the console chip), the Quote planner and the
# price readers all order candidates through the helpers below — #88 §2 forbids independent copies, and
# the SQL builders exist so a repository query cannot quietly grow a second ranking.
PRICE_SOURCE_ORDER: Final[tuple[str, ...]] = (
    "binance.perp",
    "binance.spot",
    "hl.perp",
    "hl.spot",
    "hl.xyz",
    "hl.*",
    "okx.perp",
    "okx.spot",
)
QUOTE_ASSET_ORDER: Final[tuple[str, ...]] = ("USDT", "USDC", "FDUSD")
_OTHER_SOURCE_RANK: Final = len(PRICE_SOURCE_ORDER)
_OTHER_QUOTE_RANK: Final = len(QUOTE_ASSET_ORDER)


def source_rank(venue: str) -> int:
    """Deterministic venue precedence; any new Hyperliquid builder DEX still ranks before OKX."""

    value = str(venue)
    try:
        return PRICE_SOURCE_ORDER.index(value)
    except ValueError:
        return PRICE_SOURCE_ORDER.index("hl.*") if value.startswith("hl.") else _OTHER_SOURCE_RANK


def quote_asset_rank(quote_asset: str | None) -> int:
    try:
        return QUOTE_ASSET_ORDER.index(str(quote_asset or "").upper())
    except ValueError:
        return _OTHER_QUOTE_RANK


def _rank_case_sql(column: str, values: Sequence[str], *, other: int) -> str:
    branches = " ".join(
        (
            # These fragments are embedded in psycopg parameterized queries. ``%%`` reaches PostgreSQL as
            # one literal LIKE wildcard; a lone ``%`` is rejected by psycopg's pyformat parser.
            f"WHEN {column} LIKE '{value[:-1]}%%' THEN {index}"
            if value.endswith(".*")
            else f"WHEN {column} = '{value}' THEN {index}"
        )
        for index, value in enumerate(values)
    )
    return f"CASE {branches} ELSE {other} END"


def source_rank_sql(column: str = "i.venue") -> str:
    """The SQL form of :func:`source_rank`, generated from the same tuple so the two cannot drift."""

    return _rank_case_sql(column, PRICE_SOURCE_ORDER, other=_OTHER_SOURCE_RANK)


def quote_asset_rank_sql(column: str = "i.quote_asset") -> str:
    return _rank_case_sql(f"upper({column})", QUOTE_ASSET_ORDER, other=_OTHER_QUOTE_RANK)


def price_kind_for(venue: str) -> PriceKind:
    """What the venue's current-quote endpoint publishes."""

    return "mid" if str(venue).startswith("hl.") else "last"


@dataclass(frozen=True, slots=True)
class QuoteRequest:
    """One priceable question: a raw symbol, and the market that symbol has to be priced in.

    A symbol on its own is not a priceable question. `V` is Visa on the NYSE and also a base symbol a
    crypto venue lists; `SEI` is a Cosmos token and a NYSE-listed insurer; 352 of the catalogue's crypto
    bases are also US tickers. Resolving by symbol alone returned whichever contract the venue ranking
    happened to put first, so an equity Event could be quoted at a coin's price under the same three
    letters and nothing in the row said so (#651 §6.2).

    `unknown` is the untyped question and keeps the untyped answer — any contract the catalogue lists —
    because a caller that genuinely does not know the market (an operator typing symbols into the quotes
    endpoint) must not have a market invented for it.
    """

    symbol: str
    market_type: InstrumentClass = "unknown"

    def accepts(self, instrument_class: str) -> bool:
        """Whether a catalogue row's class answers this question."""

        return self.market_type in {"unknown", instrument_class}


@dataclass(frozen=True, slots=True)
class PriceInstrument:
    """One provider-queryable contract. `(venue, venue_symbol)` is the identity — `base_symbol` is a join hint."""

    venue: str
    venue_symbol: str
    base_symbol: str
    instrument_class: str = "unknown"
    quote_asset: str | None = None

    @property
    def price_kind(self) -> PriceKind:
        return price_kind_for(self.venue)

    @property
    def source_key(self) -> str:
        """One provider request family: a Binance market or a Hyperliquid main/dex source."""

        return self.venue

    @property
    def quote_key(self) -> tuple[str, str]:
        return (self.venue_symbol, self.price_kind)


# ---------------------------------------------------------------------------- quotes
@dataclass(frozen=True, slots=True)
class ProviderQuote:
    """What a venue adapter returns: the provider's own numbers, already normalized into our vocabulary.

    Identity (base symbol, instrument class) is not the provider's to give — the loop attaches it from the
    resolved Price Instrument, so a venue can never rename a News asset.
    """

    venue_symbol: str
    price: Decimal
    change_basis: str | None = None
    # What the day change is measured against, when the venue publishes it. The loop owns the only percentage
    # calculation so current and reference freshness cannot diverge behind a cached provider percentage.
    reference_price: Decimal | None = None
    source_at_ms: int | None = None


@dataclass(frozen=True, slots=True)
class Quote:
    """One normalized current value. `change_pct` always declares the window it came from."""

    venue: str
    venue_symbol: str
    base_symbol: str
    price: Decimal
    price_kind: PriceKind
    instrument_class: str = "unknown"
    quote_asset: str | None = None
    change_pct: float | None = None
    change_basis: str | None = None
    source_at_ms: int | None = None
    reference_at_ms: int | None = None

    def as_entry(self) -> dict[str, Any]:
        return {
            "venue": self.venue,
            "venue_symbol": self.venue_symbol,
            "base_symbol": self.base_symbol,
            "instrument_class": self.instrument_class,
            "quote_asset": self.quote_asset,
            "price": str(self.price),
            "price_kind": self.price_kind,
            "change_pct": self.change_pct,
            "change_basis": self.change_basis,
            "source_at_ms": self.source_at_ms,
            "reference_at_ms": self.reference_at_ms,
        }


@dataclass(frozen=True, slots=True)
class QuoteFreshness:
    """Read-time current freshness, preserving exposed ages separately from raw clock validity."""

    received_age_ms: int
    source_age_ms: int | None
    effective_age_ms: int
    freshness_basis: FreshnessBasis
    state: Literal["fresh", "stale"]


def quote_freshness(*, measured_at_ms: int, received_at_ms: int, source_at_ms: int | None) -> QuoteFreshness:
    """Use the oldest applicable current clock; a far-future timestamp is stale rather than clamped fresh."""

    received_raw_age = int(measured_at_ms) - int(received_at_ms)
    source_raw_age = None if source_at_ms is None else int(measured_at_ms) - int(source_at_ms)
    received_age_ms = max(0, received_raw_age)
    source_age_ms = None if source_raw_age is None else max(0, source_raw_age)
    effective_age_ms = max(received_age_ms, source_age_ms or 0)
    clocks_valid = received_raw_age >= -QUOTE_MAX_FUTURE_SKEW_MS and (
        source_raw_age is None or source_raw_age >= -QUOTE_MAX_FUTURE_SKEW_MS
    )
    return QuoteFreshness(
        received_age_ms=received_age_ms,
        source_age_ms=source_age_ms,
        effective_age_ms=effective_age_ms,
        freshness_basis="received_only" if source_at_ms is None else "source_and_received",
        state="fresh" if clocks_valid and effective_age_ms <= QUOTE_FRESH_MAX_AGE_MS else "stale",
    )


def reference_freshness(*, measured_at_ms: int, reference_at_ms: int | None) -> tuple[int | None, bool]:
    """Return the exposed reference age and whether the 24H reference may still author a percentage."""

    if reference_at_ms is None:
        return None, False
    raw_age = int(measured_at_ms) - int(reference_at_ms)
    age_ms = max(0, raw_age)
    valid = -QUOTE_MAX_FUTURE_SKEW_MS <= raw_age <= QUOTE_REFERENCE_MAX_AGE_MS
    return age_ms, valid


def parse_price(value: Any) -> Decimal | None:
    """A price is a positive finite Decimal or it is not a price. Never 0, never NaN, never a float."""

    if value is None or isinstance(value, bool):
        return None
    try:
        price = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not price.is_finite() or price <= 0:
        return None
    return price


def parse_change_pct(current: Decimal | None, previous: Any) -> float | None:
    """The one derivation of a day change from two prices.

    Hyperliquid publishes yesterday's close rather than a percentage, which is why this exists at all;
    #562 made it the only copy. The price loop calls it for a snapshot quote and the delivery read calls
    it for a contract first priced at push time, so a card and the console cannot disagree about the
    number because two functions rounded it differently.
    """

    prior = parse_price(previous)
    if current is None or prior is None:
        return None
    try:
        return float((current / prior - 1) * 100)
    except (InvalidOperation, DivisionByZero):
        return None


def quote_change_24h_bps(quote: Mapping[str, Any]) -> int | None:
    """The reader's 24 h number in integer basis points, read off the quote that already carries it.

    The percentage is computed once, by `parse_change_pct`, where the quote is built. Every reader
    surface asks this instead of dividing two prices a second time, re-rounding a stored percentage of
    its own accord, or parsing `24h +7.91%` back out of a rendered card line (#562).

    Only a fresh quote whose change declares the rolling 24 h window has one. A stale price, a
    provider-day window and a missing percentage all answer None, and the caller then leaves the line
    empty rather than printing a zero.
    """

    if str(quote.get("state") or "") != "fresh" or str(quote.get("change_basis") or "") != "rolling_24h":
        return None
    value = quote.get("change_pct")
    if isinstance(value, bool) or not isinstance(value, int | float) or not isfinite(float(value)):
        return None
    return int((Decimal(str(value)) * Decimal(100)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


# ---------------------------------------------------------------------------- candles and returns
@dataclass(frozen=True, slots=True)
class Candle:
    """One closed interval.

    `close_at_ms` is the exclusive end, so each provider's off-by-one convention stays inside its adapter.
    """

    open_at_ms: int
    close_at_ms: int
    close: Decimal


@dataclass(frozen=True, slots=True)
class Trade:
    """One public venue trade, retaining the provider's millisecond event time."""

    traded_at_ms: int
    price: Decimal


@dataclass(frozen=True, slots=True)
class PricePoint:
    """The price selected for one delivery-time anchor and how it was obtained."""

    at_ms: int
    price: Decimal
    basis: Literal["trade", "candle_1m"]


def select_trade(trades: Sequence[Trade], *, target_ms: int, max_gap_ms: int = 60_000) -> Trade | None:
    """Last trade at or before the anchor, provided it is no more than one minute old."""

    best: Trade | None = None
    for trade in trades:
        if trade.traded_at_ms <= int(target_ms) and (best is None or trade.traded_at_ms > best.traded_at_ms):
            best = trade
    if best is None or int(target_ms) - best.traded_at_ms > int(max_gap_ms):
        return None
    return best


def select_candle(
    candles: Sequence[Candle],
    *,
    target_ms: int,
    max_gap_ms: int = CANDLE_GAP_TOLERANCE_MS,
) -> Candle | None:
    """The last candle closed at or before `target_ms`, or None when the nearest one is too far back.

    No forward fill: a halted session, a delisted contract or an illiquid gap must read as missing data, not
    as an unchanged price. Because every target is floored onto the same grid, two selected endpoints are
    exactly one horizon apart whenever both exist.
    """

    best: Candle | None = None
    for candle in candles:
        if candle.close_at_ms <= int(target_ms) and (best is None or candle.close_at_ms > best.close_at_ms):
            best = candle
    if best is None or int(target_ms) - best.close_at_ms > int(max_gap_ms):
        return None
    return best


def return_bps(p0: Decimal, price: Decimal) -> int | None:
    """`(pH / p0) - 1` in integer basis points. Decimal all the way so the stored number is reproducible."""

    if p0 is None or price is None or p0 <= 0:
        return None
    try:
        return int(((price / p0 - 1) * 10_000).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))
    except (InvalidOperation, DivisionByZero):
        return None


# ---------------------------------------------------------------------------- server-owned reader copy
# The browser formats numbers and picks a tone; it never owns a vocabulary table. A current quote and an
# rolling day change retain their own explicit vocabulary.
QUOTE_STATE_ZH: Final[Mapping[str, str]] = {
    "fresh": "报价正常",
    "stale": "报价陈旧",
    "unavailable": "暂无报价",
    "unlisted": "无可交易合约",
}
PRICE_KIND_ZH: Final[Mapping[str, str]] = {"last": "最新成交价", "mark": "标记价", "mid": "盘口中价"}
CHANGE_BASIS_ZH: Final[Mapping[str, str]] = {"rolling_24h": "滚动 24H", "provider_day": "场所日内"}
# Stable semantic reasons only. A timeout or a 429 is loop health, never a permanent row reason.


def quote_state_zh(state: str | None) -> str:
    return QUOTE_STATE_ZH.get(str(state or ""), "")


def price_kind_zh(kind: str | None) -> str:
    return PRICE_KIND_ZH.get(str(kind or ""), "")


def change_basis_zh(basis: str | None) -> str:
    return CHANGE_BASIS_ZH.get(str(basis or ""), "")


__all__ = [
    "CANDLE_GAP_TOLERANCE_MS",
    "CANDLE_INTERVAL",
    "CANDLE_INTERVAL_MS",
    "CHANGE_BASIS_ZH",
    "EXTERNAL_CONCURRENCY",
    "PRICE_KIND_ZH",
    "PRICE_SOURCE_ORDER",
    "QUOTE_ASSET_ORDER",
    "QUOTE_DAY_PERIOD_SECONDS",
    "QUOTE_FRESH_MAX_AGE_MS",
    "QUOTE_LOOKBACK_MS",
    "QUOTE_MAX_FUTURE_SKEW_MS",
    "QUOTE_PERIOD_SECONDS",
    "QUOTE_READ_TIMEOUT_SECONDS",
    "QUOTE_REFERENCE_MAX_AGE_MS",
    "QUOTE_REQUEST_SYMBOL_MAX",
    "QUOTE_SOURCE_GROUP_MAX",
    "QUOTE_STATE_ZH",
    "QUOTE_TARGET_MAX",
    "QUOTE_TURN_DEADLINE_SECONDS",
    "REVIEW_MAX_HOURS",
    "REVIEW_POTENTIAL_MISS_LIMIT",
    "Candle",
    "PriceInstrument",
    "PriceKind",
    "ProviderQuote",
    "Quote",
    "QuoteFreshness",
    "QuoteRequest",
    "QuoteState",
    "change_basis_zh",
    "parse_change_pct",
    "parse_price",
    "price_kind_for",
    "price_kind_zh",
    "quote_asset_rank",
    "quote_asset_rank_sql",
    "quote_change_24h_bps",
    "quote_freshness",
    "quote_state_zh",
    "reference_freshness",
    "return_bps",
    "select_candle",
    "source_rank",
    "source_rank_sql",
]
