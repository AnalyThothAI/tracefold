"""Instrument universe (#75, consolidated in #89): pure normalization, aliasing and classification.

The provider already tags Items with venue symbols — OpenNews emits a bare symbol *and* an ``XYZ-`` prefixed one
(``XYZ-CL``, ``XYZ-MU``, ``XYZ-HOOD``, ``XYZ-UNITREE``), where ``xyz`` is a Hyperliquid builder perp DEX carrying
equity/commodity/index perps. ~95% of a full day's coin-tag volume lands on a Binance or Hyperliquid listing, so
the venue universe is the natural reference table for News, and it does exactly two jobs:

* **symbol normalization** — the several names one issuer trades under collapse to one stable storyline identity
* **asset class** — whether a headline is about a coin or a stock, which the Gate used to guess from the ``XYZ-``
  prefix alone. Since #91 the table also carries a *reference* tier (``us.listed``) for the thousands of equities
  no crypto venue lists; it can only answer that second question, never the first.

It is deliberately *not* a filter: existence on a venue is not evidence that a headline matters (#75), and its
snapshot boundaries are not reader-facing listing news — OpenNews pushes those frames and the pipeline admits them
(``listing_deterministic``, #72). The catalogue stores its own immutable validity events only so historical Trading
replay cannot resolve an instrument before a listing or through a delisted interval.

Everything here is pure: fetching lives in ``tracefold.integrations.venues``, persistence in
``market_review.instrument_storage.InstrumentsRepository``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal, cast

InstrumentClass = Literal["crypto", "equity", "commodity", "index", "fx", "pre_ipo", "unknown"]
InstrumentStatus = Literal["trading", "delisted"]
# Runtime view of the Literal above: what a stored row or a venue declaration is allowed to say.
INSTRUMENT_CLASSES: Final[frozenset[str]] = frozenset(
    {"crypto", "equity", "commodity", "index", "fx", "pre_ipo", "unknown"}
)
# Classes that are not crypto. The Gate collapses them into one `equity_or_commodity` asset class (#89).
NON_CRYPTO_CLASSES: Final[frozenset[str]] = frozenset({"equity", "commodity", "index", "fx", "pre_ipo"})
# Venues in the table that nobody trades on: they answer "is this symbol a stock?" and nothing else (#91). The
# distinction is load-bearing because 352 of the 1,244 crypto base symbols are also US tickers — `ATOM` is
# Atomera, `APT` is Alpha Pro Tech, `BCH` is Banco de Chile, `A` is Agilent. A real venue always wins; the
# reference tier only speaks for symbols no venue lists.
REFERENCE_VENUES: Final[frozenset[str]] = frozenset({"us.listed"})

# Quote assets stripped from a venue symbol to recover the base (Binance ships `UNITREEUSDT`, not `UNITREE`).
# Longest first so `BTCUSDT` -> `BTC` rather than `BTCUSD` + `T`.
_QUOTE_SUFFIXES: Final = ("USDT", "USDC", "FDUSD", "TUSD", "BUSD", "USD")
# Venue catalogues are authoritative, so the only thing worth rejecting is a symbol that cannot be a symbol:
# empty, whitespace-bearing, control characters, or absurdly long. Restricting this to ASCII silently dropped five
# real Binance contracts (`币安人生USDT`, `我踏马来了USDT`, `龙虾USDT`) — and the provider tags those too (`牛来`).
_SYMBOL_OK = re.compile(r"^[^\s\x00-\x1f]{1,32}$")

# Code-owned seed aliases: different names for one issuer/underlying, reconciled into `news_symbol_aliases` on
# every snapshot (this file is the source of truth — see `InstrumentsRepository.reconcile_seed_aliases`). Venue-
# derived aliases (the `XYZ-` prefix, the `dex:SYMBOL` form) are computed, not listed here. `SKHY` and `SKHX` are
# *both* real hl.xyz contracts for SK Hynix — keeping them apart at the venue level is correct, but the storyline
# identity must treat them as one issuer so same-issuer duplicate evidence shares one group.
#
# Every value must be a symbol a venue actually lists, or the alias resolves to nothing: `1810.HK -> XIAOMI` was
# such a dead end (Binance lists Xiaomi as `HK1810`), which cost 13 tags and 3 pushed cards in one week.
# `dangling_seed_aliases()` reports any that stop resolving.
#
# `XAUT` is Tether Gold: a token, but one whose underlying *is* gold, so it belongs in gold's storyline. There is
# deliberately no `NATGAS -> GAS` seed — `GAS` is Neo's gas token on three crypto venues, a different asset.
ALIAS_SEEDS: Final[Mapping[str, str]] = {
    "XAU": "GOLD",
    "XAUT": "GOLD",
    "XAG": "SILVER",
    "WTI": "CL",
    "OIL": "CL",
    "USOIL": "CL",
    "BRENTOIL": "CL",
    "SKHX": "SKHY",
    "SKHYNIX": "SKHY",
    "SMSN": "SAMSUNG",
    "XIAOMI": "HK1810",
    "1810.HK": "HK1810",
    "HK0700": "TENCENT",
    "NOKIA": "NOK",
    "GIGADEVICE": "GIGADEV",
    "RAYDIUM": "RAY",
    "BTT": "BTTC",
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
#
# These names are checked *before* the venue default, so a symbol listed here must not also be a real crypto
# listing. `GAS` was exactly that mistake: Neo's gas token trades on binance.spot, binance.perp and hl.perp, while
# natural gas trades as `NATGAS`. Since #89 the class reaches the Gate, so a collision here mislabels live cards.
_COMMODITY: Final = frozenset(
    {
        "GOLD",
        "SILVER",
        "CL",
        "NATGAS",
        "BRENTOIL",
        "COPPER",
        "PALLADIUM",
        "PLATINUM",
        "ALUMINIUM",
        "WHEAT",
        "CORN",
        "SOY",
        "URANIUM",
    }
)
# Crypto-market gauges that live on venues whose default is not crypto (`hl.para` is a HIP-3 builder DEX, so its
# default is equity). They are indices, but of the crypto market — calling them `index` would make the Gate read a
# "total2 breaks out" headline as an equity headline.
_CRYPTO_GAUGES: Final = frozenset({"BTCD", "BTCDOM", "TOTAL2", "OTHERS", "VOL", "ALL"})
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
        "10Y",
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


@dataclass(frozen=True, slots=True)
class InstrumentSearchIdentity:
    """One catalog-confirmed identity and every spelling the Event ledger may store for it."""

    base_symbol: str
    event_symbols: tuple[str, ...]


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


def pair_base_symbol(raw: str) -> str | None:
    """Return the base of one exact ``BASE/QUOTE`` or ``BASEQUOTE`` token.

    The quote vocabulary is the same one used while ingesting venue catalogues. This parser does not prove
    that the base exists; ``InstrumentsRepository`` performs that catalog lookup before search may call the
    token an asset.
    """

    token = str(raw or "").strip().upper()
    if not token or any(character.isspace() for character in token):
        return None
    for separator in ("/", "-", "_"):
        if token.count(separator) != 1:
            continue
        base, quote = token.split(separator, 1)
        if base and quote in _QUOTE_SUFFIXES:
            return normalize_symbol(base) or None
    for quote in _QUOTE_SUFFIXES:
        if token.endswith(quote) and len(token) > len(quote):
            return normalize_symbol(token[: -len(quote)]) or None
    return None


def is_valid_symbol(symbol: str) -> bool:
    return bool(_SYMBOL_OK.match(str(symbol or "")))


def classify(base_symbol: str, *, venue: str) -> InstrumentClass:
    """Instrument class from the symbol and its venue — the fallback for venues that declare nothing.

    Binance declares the class itself (``underlyingType`` on its ``TRADIFI_PERPETUAL`` contracts) and its adapter
    passes that through; Hyperliquid declares nothing, so its HIP-3 builder DEXs are classified by the
    ``EQUITY_DEXS`` allowlist plus the symbol sets above. A hint for the Gate, never identity.
    """

    symbol = base_symbol.upper()
    if symbol in _CRYPTO_GAUGES:
        return "crypto"
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
    """Build Instruments from repository rows (or any mapping with the same keys).

    The stored ``instrument_class`` wins: it may carry what the venue itself declared (Binance ships
    ``underlyingType`` for its TradFi perps), which ``classify()`` cannot re-derive from the symbol. Recomputing it
    here silently reset every declared class to the venue default on read — the bug this docstring replaces.
    """

    out: list[Instrument] = []
    for row in rows:
        venue = str(row.get("venue") or "")
        venue_symbol = str(row.get("venue_symbol") or "")
        base = str(row.get("base_symbol") or "")
        if not venue or not venue_symbol or not base:
            continue
        quote = row.get("quote_asset")
        status = str(row.get("status") or "trading")
        stored = str(row.get("instrument_class") or "")
        instrument_class = (
            cast("InstrumentClass", stored) if stored in INSTRUMENT_CLASSES else classify(base, venue=venue)
        )
        out.append(
            Instrument(
                venue=venue,
                venue_symbol=venue_symbol,
                base_symbol=base,
                instrument_class=instrument_class,
                quote_asset=str(quote) if quote else None,
                status="delisted" if status == "delisted" else "trading",
            )
        )
    return tuple(out)


def grounding_rollup(
    usage: Mapping[str, Sequence[str]],
    refs: Mapping[str, Mapping[str, object]],
) -> dict[str, object]:
    """Fold "which Events named something that exists" out of the two halves each repository owns (#87).

    ``usage`` is ``event_id -> coin tags`` from ``news_event_assets``; ``refs`` is ``tag -> resolution`` from the
    instrument universe. An Event counts as grounded when *any* of its tags resolves to a listed instrument —
    the same "at least one grounded asset" the Gate admits on, so the console's funnel segment and the Gate
    cannot drift apart. An Event carrying no tags at all never appears in ``usage``, so it is neither tagged
    nor grounded — it is not a symbol that failed to land, it is a headline that named none.

    The count is deliberately not clamped against the window's Event total. If the two ever disagree, the real
    number is the one worth showing: a funnel segment wider than the one above it is a visible bug, and a
    silently clamped one is an invisible one.

    ``ungrounded_by_symbol`` is per-symbol rather than per-Event: the operator question is which provider tag
    keeps failing (SPOT is Spot Gold, NEAR came from "near-instant"), and one bad tag can cost dozens of Events.
    """

    tagged = len(usage)
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
        # `tagged_24h` is the only honest denominator for "how many failed to land": an Event that carried no
        # coin tag at all — a macro headline, most of a day's geopolitics — never offered a symbol, so
        # counting it as one that failed to resolve would blame the instrument table for the Gate's own
        # admission rule (#87 review).
        "tagged_24h": tagged,
        "grounded_24h": grounded,
        "ungrounded_by_symbol_24h": dict(sorted(ungrounded.items(), key=lambda kv: (-kv[1], kv[0]))[:10]),
    }


__all__ = [
    "ALIAS_SEEDS",
    "EQUITY_DEXS",
    "INSTRUMENT_CLASSES",
    "NON_CRYPTO_CLASSES",
    "REFERENCE_VENUES",
    "Instrument",
    "InstrumentClass",
    "InstrumentStatus",
    "classify",
    "grounding_rollup",
    "instruments_from_rows",
    "is_valid_symbol",
    "normalize_symbol",
    "resolve_base_symbol",
    "strip_quote_suffix",
]
