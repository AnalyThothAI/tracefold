from __future__ import annotations

import re
from dataclasses import dataclass

from tracefold.market.capture.entity import normalize_ca

SYMBOL_QUERY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{1,20}$")


@dataclass(frozen=True, slots=True)
class SearchIntent:
    kind: str
    text: str
    normalized_text: str
    ca: str | None = None
    chain: str | None = None
    symbol: str | None = None
    handle: str | None = None
    lexical_query: str | None = None


def parse_search_query(text: str) -> SearchIntent:
    query = text.strip()
    normalized = _normalize_spaces(query)
    if not query:
        return SearchIntent(kind="empty", text="", normalized_text="", lexical_query="")
    if query.startswith("@") and len(query) > 1:
        return SearchIntent(
            kind="handle",
            text=query,
            normalized_text=normalized,
            handle=query.lstrip("@").lower(),
            lexical_query=query,
        )
    if query.startswith("$") and len(query) > 1:
        symbol = query.lstrip("$").strip()
        if SYMBOL_QUERY_RE.fullmatch(symbol):
            return SearchIntent(
                kind="symbol",
                text=query,
                normalized_text=normalized,
                symbol=symbol.upper(),
                lexical_query=normalized,
            )
    chain_prefixed_ca = _parse_chain_prefixed_ca(query)
    if chain_prefixed_ca is not None:
        prefixed_chain, prefixed_ca = chain_prefixed_ca
        return SearchIntent(
            kind="ca",
            text=query,
            normalized_text=normalized,
            ca=prefixed_ca,
            chain=prefixed_chain,
            lexical_query=prefixed_ca,
        )
    fallback_chain: str | None
    fallback_ca: str | None
    try:
        fallback_chain, fallback_ca = normalize_ca(query)
    except ValueError:
        fallback_chain = fallback_ca = None
    if fallback_ca:
        return SearchIntent(
            kind="ca",
            text=query,
            normalized_text=normalized,
            ca=fallback_ca,
            chain=fallback_chain,
            lexical_query=fallback_ca,
        )
    if SYMBOL_QUERY_RE.fullmatch(query):
        return SearchIntent(
            kind="symbol",
            text=query,
            normalized_text=normalized,
            symbol=query.upper(),
            lexical_query=normalized,
        )
    return SearchIntent(kind="text", text=query, normalized_text=normalized, lexical_query=normalized)


def _parse_chain_prefixed_ca(query: str) -> tuple[str, str] | None:
    chain_hint, separator, value = query.partition(":")
    if not separator or not chain_hint.strip() or not value.strip():
        return None
    try:
        return normalize_ca(value, chain=chain_hint)
    except ValueError:
        return None


def _normalize_spaces(value: str) -> str:
    return " ".join(value.split())
