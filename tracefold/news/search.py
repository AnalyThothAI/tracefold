"""Deterministic News feed search planning (#336).

One small interface hides whitespace/Unicode normalization, token-versus-text classification, and the
catalog lookup. The returned plan is the only search vocabulary FeedStorage executes; it cannot represent
the retired mixed asset/text OR.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from .market_review.instruments import normalize_symbol

if TYPE_CHECKING:
    from .market_review.instrument_storage import InstrumentsRepository

SearchMode = Literal["asset", "text"]


@dataclass(frozen=True, slots=True)
class NewsSearchPlan:
    mode: SearchMode
    normalized_query: str
    event_symbols: tuple[str, ...]
    resolved_symbols: tuple[str, ...]
    q: str | None
    symbol: str | None

    def __post_init__(self) -> None:
        if bool(self.q) == bool(self.symbol):
            raise ValueError("news_feed_search_source_invalid")
        if self.mode == "asset" and not self.event_symbols:
            raise ValueError("news_feed_asset_search_empty")
        if self.mode == "text" and (self.event_symbols or self.resolved_symbols):
            raise ValueError("news_feed_text_search_symbols_invalid")

    def public_metadata(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "normalized_query": self.normalized_query,
            "resolved_symbols": list(self.resolved_symbols),
        }


def compile_news_search(
    *,
    q: str | None,
    symbol: str | None,
    instruments: InstrumentsRepository,
) -> NewsSearchPlan | None:
    """Compile one request into exactly one search path.

    ``instruments`` is the existing concrete PostgreSQL repository. It is deliberately not wrapped in a
    one-implementation protocol: Postgres is the local adapter and this interface is tested through it.
    """

    raw_query = str(q) if q is not None else None
    raw_symbol = str(symbol) if symbol is not None else None
    query = _normalized_text(raw_query)
    structured_symbol = _normalized_text(raw_symbol)
    if query and structured_symbol:
        raise ValueError("news_feed_search_conflict")
    if structured_symbol:
        # ``symbol`` is an explicit exact filter even when an input only resembles a provider prefix.
        # Preserve a non-empty normalized token instead of ever constructing ``ANY([''])``.
        token = normalize_symbol(structured_symbol) or structured_symbol.upper()
        identity = instruments.search_identity(structured_symbol)
        if identity is None:
            return NewsSearchPlan(
                mode="asset",
                normalized_query=token,
                event_symbols=(token,),
                resolved_symbols=(),
                q=None,
                symbol=raw_symbol,
            )
        return NewsSearchPlan(
            mode="asset",
            normalized_query=token,
            event_symbols=identity.event_symbols,
            resolved_symbols=(identity.base_symbol,),
            q=None,
            symbol=raw_symbol,
        )
    if not query:
        return None

    text_query = query
    if not any(character.isspace() for character in query):
        token = query[1:] if query.startswith("$") and len(query) > 1 else query
        identity = instruments.search_identity(token.upper())
        if identity is not None:
            return NewsSearchPlan(
                mode="asset",
                normalized_query=token.upper(),
                event_symbols=identity.event_symbols,
                resolved_symbols=(identity.base_symbol,),
                q=raw_query,
                symbol=None,
            )
        if token != query:
            text_query = token
    return NewsSearchPlan(
        mode="text",
        normalized_query=text_query,
        event_symbols=(),
        resolved_symbols=(),
        q=raw_query,
        symbol=None,
    )


def _normalized_text(value: str | None) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(normalized.split())


__all__ = ["NewsSearchPlan", "SearchMode", "compile_news_search"]
