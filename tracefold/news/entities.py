"""Source-backed entity features for retrieval, separate from exact identity.

Catalogue issuer/underlying aliases and venue pair bases widen candidate search only. They never
establish that two actors, contracts or propositions are equal and never enter Claim identity.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Literal

from .events.grounding import COMMODITY_CONTEXT
from .market_review.instruments import ALIAS_SEEDS, normalize_symbol, resolve_base_symbol

RELATED_ASSET_ALIASES: Final = {alias: resolve_base_symbol(alias) for alias in ALIAS_SEEDS}
CRYPTO_QUOTE_SUFFIXES: Final = ("USDT", "USDC", "FDUSD", "TUSD", "BUSD", "USD")
_COMMODITY_NAMES: Final = tuple((resolve_base_symbol(tag), pattern) for tag, pattern in COMMODITY_CONTEXT.items())
_SYMBOL_EDGES: Final = re.compile(r"^[\s$]+|\s+$")
_GENERIC_NAMES: Final = frozenset({"market", "markets", "data", "government", "company", "people"})
ADDRESS_PATTERN: Final = r"^(?:0[xX][0-9a-fA-F]{40}|[1-9A-HJ-NP-Za-km-z]{32,44}|solana:.*)$"
_ADDRESS: Final = re.compile(ADDRESS_PATTERN)
_CASHTAG: Final = re.compile(r"\$([A-Za-z][A-Za-z0-9]*(?:[.:-][A-Za-z0-9]+)*)")


@dataclass(frozen=True, slots=True)
class EntityKey:
    namespace: str
    identifier: str


@dataclass(frozen=True, slots=True)
class EntityFeature:
    key: EntityKey
    kind: Literal["exact", "underlying", "issuer", "catalogue", "venue_base", "contract_base"]
    basis_ref: str
    surface: str


def asset_features(symbol: str, market_type: str, *, basis_ref: str = "asset_field") -> tuple[EntityFeature, ...]:
    """Keep the exact tagged spelling and separately expose catalogue-backed retrieval relations.

    No chain/address normalization is inferred here. A venue-qualified asset retains the qualifier
    in its exact key; strip-prefix and quote-pair forms are candidate features only.
    """
    text = _SYMBOL_EDGES.sub("", symbol)
    # Asset fields do not declare a chain. Preserve an address spelling until a chain-specific owner
    # can establish its normalization; notably, a Solana address is case-sensitive.
    address = bool(_ADDRESS.fullmatch(text))
    tagged = text if address else text.upper()
    if not tagged:
        return ()
    namespace = f"asset:{market_type}"
    result = [EntityFeature(EntityKey(namespace, tagged), "exact", basis_ref, symbol)]
    if address:
        return tuple(result)
    base = normalize_symbol(text)
    if base != tagged:
        result.append(
            EntityFeature(
                EntityKey(f"venue_base:{market_type}", base), "venue_base", "instrument_symbol_format", symbol
            )
        )
    canonical = resolve_base_symbol(base)
    if canonical != base:
        kind: Literal["underlying", "issuer", "catalogue"] = (
            "underlying"
            if canonical in {"GOLD", "SILVER", "CL"}
            else "issuer"
            if base in {"SKHX", "SKHYNIX"}
            else "catalogue"
        )
        result.append(
            EntityFeature(
                EntityKey(
                    "underlying:commodity" if kind == "underlying" else f"{kind}:instrument_catalogue", canonical
                ),
                kind,
                "instrument_alias_seeds",
                symbol,
            )
        )
    if market_type == "commodity":
        result.extend(
            EntityFeature(EntityKey("underlying:commodity", name), "underlying", "commodity_context", symbol)
            for name, pattern in _COMMODITY_NAMES
            if name != base and pattern.search(text)
        )
    if market_type in {"crypto", "unknown"}:
        quote = next(
            (value for value in CRYPTO_QUOTE_SUFFIXES if base.endswith(value) and len(base) > len(value) + 1), None
        )
        if quote is not None:
            result.append(
                EntityFeature(
                    EntityKey(f"pair_base:{market_type}", base[: -len(quote)]),
                    "contract_base",
                    "venue_quote_suffix",
                    symbol,
                )
            )
    return tuple(dict.fromkeys(result))


def asset_retrieval_symbols(symbol: str, market_type: str) -> frozenset[str]:
    """All candidate spellings, including exact tags; overlap proves relevance, never equality."""
    return frozenset(feature.key.identifier for feature in asset_features(symbol, market_type))


def commodity_name_patterns(symbol: str) -> tuple[str, ...]:
    """The same commodity patterns for bound PostgreSQL ~* parameters."""
    return tuple(
        sorted({pattern.pattern.replace(r"\b", r"\y") for base, pattern in _COMMODITY_NAMES if base == symbol})
    )


def retrieval_name(text: str) -> str:
    """A source-written subject/object spelling, not a resolved actor ID."""
    value = text.strip().casefold()
    return "" if value in _GENERIC_NAMES else value


def identity_value(value: str) -> str:
    """Keep code-owned IDs case-sensitive; an unspecified chain cannot authorize case folding."""
    return value.strip()


def source_asset_symbols(text: str) -> tuple[str, ...]:
    """Literal source cashtags for candidate search, without inferring a market, actor or contract."""
    return tuple(dict.fromkeys(match.group(1) for match in _CASHTAG.finditer(text)))


def source_mentions_asset(symbol: str, market_type: str, text: str) -> bool:
    """Attribute a body-wide provider tag to a scoped task only through visible source evidence.

    Ticker spellings and the existing commodity name patterns prove retrieval relevance, never exact
    asset identity. A provider's grade alone cannot assign its digest-wide tags to a numbered sibling.
    """
    symbols = asset_retrieval_symbols(symbol, market_type)
    return any(
        re.search(
            rf"(?<![A-Za-z0-9]){re.escape(value)}(?![A-Za-z0-9])",
            text,
            0 if _ADDRESS.fullmatch(value) else re.IGNORECASE,
        )
        for value in symbols
    ) or any(name in symbols and pattern.search(text) for name, pattern in _COMMODITY_NAMES)
