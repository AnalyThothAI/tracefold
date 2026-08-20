"""Tradeable instrument universe (#75): pure normalization, aliasing and diff over venue listings.

The provider already tags Items with venue symbols — OpenNews emits a bare symbol *and* an ``XYZ-`` prefixed one
(``XYZ-CL``, ``XYZ-MU``, ``XYZ-HOOD``, ``XYZ-UNITREE``), where ``xyz`` is a Hyperliquid builder perp DEX carrying
equity/commodity/index perps. 96% of a full day's coin-tag volume lands on a Binance or Hyperliquid listing, so the
venue universe is the natural reference table for News: it normalizes symbols for the storyline throttle, tells the
Gate which tags name something real, and — by diffing two snapshots — yields listing/delisting facts that do not
depend on a news frame arriving at all.

Everything here is pure: fetching lives in ``tracefold.integrations.venues``, persistence in
``InstrumentsRepository``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

INSTRUMENT_UNIVERSE_VERSION: Final = "news_instrument_universe_v1"

InstrumentClass = Literal["crypto", "equity", "commodity", "index", "fx", "pre_ipo", "unknown"]
InstrumentStatus = Literal["trading", "delisted"]

# Quote assets stripped from a venue symbol to recover the base (Binance ships `UNITREEUSDT`, not `UNITREE`).
# Longest first so `BTCUSDT` -> `BTC` rather than `BTCUSD` + `T`.
_QUOTE_SUFFIXES: Final = ("USDT", "USDC", "FDUSD", "TUSD", "BUSD", "USD")
_SYMBOL_OK = re.compile(r"^[A-Z0-9][A-Z0-9._-]{0,31}$")

# Operator-owned aliases: different names for one issuer/underlying. Venue-derived aliases (the `XYZ-` prefix, the
# `dex:SYMBOL` form) are computed, not listed here. `SKHY` and `SKHX` are *both* real hl.xyz contracts for SK Hynix
# — keeping them apart at the venue level is correct, but the storyline throttle must treat them as one issuer or
# the same buyback ships nine cards (observed 2026-08-19).
ALIAS_SEEDS: Final[Mapping[str, str]] = {
    "XAU": "GOLD",
    "XAUT": "GOLD",
    "XAG": "SILVER",
    "WTI": "CL",
    "OIL": "CL",
    "USOIL": "CL",
    "BRENTOIL": "CL",
    "NATGAS": "GAS",
    "SKHX": "SKHY",
    "SKHYNIX": "SKHY",
    "SMSN": "SAMSUNG",
    "1810.HK": "XIAOMI",
    "HK0700": "TENCENT",
    "OAI": "OPENAI",
    "ANTH": "ANTHROPIC",
    "SPCX": "SPACEX",
    "NASDAQ": "USTECH",
    "USA100": "USTECH",
    "USA500": "SP500",
    "US500": "SP500",
    "XYZ100": "SP500",
}

# Instrument-class hints for venues whose symbols are not crypto. Anything not matched stays `crypto` on a crypto
# venue and `unknown` elsewhere — the class is a hint for policy, never an identity.
_COMMODITY: Final = frozenset(
    {"GOLD", "SILVER", "CL", "GAS", "COPPER", "PALLADIUM", "PLATINUM", "ALUMINIUM", "WHEAT", "CORN", "SOY", "URANIUM"}
)
_INDEX: Final = frozenset(
    {
        "SP500",
        "USTECH",
        "USBOND",
        "SMALL2000",
        "USENERGY",
        "SEMI",
        "SEMIS",
        "JPN225",
        "JP225",
        "NIFTY",
        "KR200",
        "IBOV",
        "VIX",
        "DXY",
        "MAG7",
        "MAGS",
        "ROBOT",
        "INFOTECH",
        "NUCLEAR",
        "DEFENSE",
        "ENERGY",
        "BIOTECH",
        "GLDMINE",
        "GOLDJM",
        "SILVERJM",
        "TOTAL2",
        "OTHERS",
        "BTCD",
        "10Y",
        "VOL",
    }
)
_FX: Final = frozenset({"EUR", "GBP", "JPY", "KRW"})
_PRE_IPO: Final = frozenset({"SPACEX", "OPENAI", "ANTHROPIC", "UNITREE", "ZHIPU", "MINIMAX", "MOONSHOT", "CXMT"})
# Hyperliquid builder DEXs that list equities/commodities/indices rather than crypto (HIP-3).
EQUITY_DEXS: Final = frozenset({"xyz", "para", "km", "mkts", "vntl", "cash", "flx", "io", "abcd"})


@dataclass(frozen=True, slots=True)
class Instrument:
    """One tradeable contract on one venue. ``base_symbol`` is the join key News uses."""

    venue: str  # binance.spot | binance.perp | hl.perp | hl.spot | hl.xyz | hl.vntl | ...
    venue_symbol: str  # UNITREEUSDT | xyz:UNITREE
    base_symbol: str  # UNITREE
    instrument_class: InstrumentClass
    quote_asset: str | None = None
    status: InstrumentStatus = "trading"


def normalize_symbol(raw: str) -> str:
    """Upper-case, strip the provider's ``XYZ-`` prefix and any ``dex:`` prefix. Not alias resolution."""

    symbol = str(raw or "").strip().upper()
    if symbol.startswith("XYZ-"):
        symbol = symbol[4:]
    if ":" in symbol:
        symbol = symbol.split(":", 1)[1]
    return symbol


def strip_quote_suffix(symbol: str, *, quote_asset: str | None = None) -> str:
    """`UNITREEUSDT` -> `UNITREE`. Uses the venue's declared quote when it has one."""

    upper = normalize_symbol(symbol)
    if quote_asset:
        quote = str(quote_asset).strip().upper()
        if quote and upper.endswith(quote) and len(upper) > len(quote):
            return upper[: -len(quote)]
    for suffix in _QUOTE_SUFFIXES:
        if upper.endswith(suffix) and len(upper) > len(suffix):
            return upper[: -len(suffix)]
    return upper


def is_valid_symbol(symbol: str) -> bool:
    return bool(_SYMBOL_OK.match(str(symbol or "")))


def classify(base_symbol: str, *, venue: str) -> InstrumentClass:
    """Instrument class from the symbol and its venue. A hint for policy, never identity."""

    symbol = base_symbol.upper()
    if symbol in _PRE_IPO:
        return "pre_ipo"
    if symbol in _COMMODITY:
        return "commodity"
    if symbol in _INDEX:
        return "index"
    if symbol in _FX:
        return "fx"
    dex = venue.split(".", 1)[1] if venue.startswith("hl.") else ""
    if dex in EQUITY_DEXS:
        return "equity"
    if venue.startswith("binance.") or venue in {"hl.perp", "hl.spot"}:
        return "crypto"
    return "unknown"


def resolve_base_symbol(symbol: str, aliases: Mapping[str, str] | None = None) -> str:
    """Normalize, then follow the alias map to the canonical issuer symbol (one hop, cycle-safe)."""

    base = normalize_symbol(symbol)
    table = aliases if aliases is not None else ALIAS_SEEDS
    seen: set[str] = set()
    while base in table and base not in seen:
        seen.add(base)
        base = str(table[base]).strip().upper()
    return base


def instruments_from_rows(rows: Iterable[Mapping[str, object]]) -> tuple[Instrument, ...]:
    """Build Instruments from repository rows (or any mapping with the same keys)."""

    out: list[Instrument] = []
    for row in rows:
        venue = str(row.get("venue") or "")
        venue_symbol = str(row.get("venue_symbol") or "")
        base = str(row.get("base_symbol") or "")
        if not venue or not venue_symbol or not base:
            continue
        quote = row.get("quote_asset")
        status = str(row.get("status") or "trading")
        out.append(
            Instrument(
                venue=venue,
                venue_symbol=venue_symbol,
                base_symbol=base,
                instrument_class=classify(base, venue=venue),
                quote_asset=str(quote) if quote else None,
                status="delisted" if status == "delisted" else "trading",
            )
        )
    return tuple(out)


@dataclass(frozen=True, slots=True)
class UniverseDiff:
    """What changed between two snapshots. `listed`/`delisted` are the material facts a listing card is built from."""

    listed: tuple[Instrument, ...]
    delisted: tuple[Instrument, ...]
    unchanged: int

    @property
    def empty(self) -> bool:
        return not self.listed and not self.delisted


def diff_universe(previous: Sequence[Instrument], current: Sequence[Instrument]) -> UniverseDiff:
    """Compare two snapshots by ``(venue, venue_symbol)``.

    A first-ever snapshot has no previous rows, and every instrument would read as "listed" — the caller must treat
    an empty ``previous`` as a seed, not as thousands of listings. ``seed_only`` in the repository does that.
    """

    prev = {(i.venue, i.venue_symbol): i for i in previous if i.status == "trading"}
    curr = {(i.venue, i.venue_symbol): i for i in current if i.status == "trading"}
    listed = tuple(curr[k] for k in sorted(curr.keys() - prev.keys()))
    delisted = tuple(prev[k] for k in sorted(prev.keys() - curr.keys()))
    return UniverseDiff(listed=listed, delisted=delisted, unchanged=len(curr.keys() & prev.keys()))


def grounding_rollup(
    usage: Mapping[str, Sequence[str]],
    refs: Mapping[str, Mapping[str, object]],
) -> dict[str, object]:
    """Fold "which Events named something that exists" out of the two halves each repository owns (#87).

    ``usage`` is ``event_id -> coin tags`` from ``news_event_assets``; ``refs`` is ``tag -> resolution`` from the
    instrument universe. An Event counts as grounded when *any* of its tags resolves to a listed instrument —
    the same "at least one grounded asset" the Gate admits on, so the console's funnel segment and the Gate
    cannot drift apart. An Event carrying no tags at all never appears in ``usage`` and is not grounded.

    The count is deliberately not clamped against the window's Event total. If the two ever disagree, the real
    number is the one worth showing: a funnel segment wider than the one above it is a visible bug, and a
    silently clamped one is an invisible one.

    ``ungrounded_by_symbol`` is per-symbol rather than per-Event: the operator question is which provider tag
    keeps failing (SPOT is Spot Gold, NEAR came from "near-instant"), and one bad tag can cost dozens of Events.
    """

    grounded = 0
    ungrounded: dict[str, int] = {}
    for symbols in usage.values():
        hit = False
        for symbol in symbols:
            tag = str(symbol).upper()
            ref = refs.get(tag)
            if ref is not None and ref.get("listed"):
                hit = True
            else:
                ungrounded[tag] = ungrounded.get(tag, 0) + 1
        if hit:
            grounded += 1
    return {
        "grounded_24h": grounded,
        "ungrounded_by_symbol_24h": dict(sorted(ungrounded.items(), key=lambda kv: (-kv[1], kv[0]))[:10]),
    }


__all__ = [
    "ALIAS_SEEDS",
    "EQUITY_DEXS",
    "INSTRUMENT_UNIVERSE_VERSION",
    "Instrument",
    "InstrumentClass",
    "InstrumentStatus",
    "UniverseDiff",
    "classify",
    "diff_universe",
    "grounding_rollup",
    "instruments_from_rows",
    "is_valid_symbol",
    "normalize_symbol",
    "resolve_base_symbol",
    "strip_quote_suffix",
]
