"""Candidate-conditioned selection over one already-bounded reader-history snapshot."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from .artifact_identity import canonical_sha
from .models import base_symbol
from .reader_history import (
    READER_HISTORY_SHA256,
    RECENT_HISTORY_MAX,
    RECENT_HISTORY_WINDOW_MS,
    TARGETED_ASSET_MAX,
    TARGETED_EXACT_MAX,
    HistoryReason,
    HistoryScope,
    news_retrieval_sha256,
)
from .similarity import similarity

TOLD_WINDOW_MS: Final[int] = RECENT_HISTORY_WINDOW_MS
TOLD_SOURCE_MAX: Final[int] = RECENT_HISTORY_MAX + TARGETED_EXACT_MAX + TARGETED_ASSET_MAX
TOLD_MAX: Final[int] = 16
TOLD_STORYLINE_TIER_MAX: Final[int] = 8
TOLD_SYMBOLS_MAX: Final[int] = 6
TOLD_FACT_SIMILARITY_MIN: Final[float] = 0.25
ToldTier = Literal["exact_fact", "storyline", "asset_overlap", "fact_similarity", "recency"]
TOLD_TIER_ORDER: Final[tuple[ToldTier, ...]] = (
    "exact_fact",
    "storyline",
    "asset_overlap",
    "fact_similarity",
    "recency",
)
TOLD_SELECTOR_ID: Final[str] = "told_context_selector_v2"
TOLD_SELECTOR_SHA256: Final[str] = canonical_sha(
    {
        "selector": TOLD_SELECTOR_ID,
        "reader_history_sha256": READER_HISTORY_SHA256,
        "source_truth": "ReaderHistorySnapshot.told_source_rows",
        "source_projection": [
            "event_id",
            "at_ms",
            "storyline_key",
            "event_type",
            "magnitude",
            "direction",
            "headline_zh",
            "grounded_assets",
            "assets",
            "comparison_title",
            "history_scope",
            "retrieval_reason",
        ],
        "source_max": TOLD_SOURCE_MAX,
        "tier_order": list(TOLD_TIER_ORDER),
        "trusted_targeted_tiers": {
            "exact_fingerprint": "exact_fact",
            "canonical_asset_overlap": "asset_overlap",
        },
        "symbol_primitive": "base_symbol_v1",
        "similarity_primitive": "character_bigram_jaccard_v1",
        "similarity_field": "comparison_title",
        "similarity_min": TOLD_FACT_SIMILARITY_MIN,
        "rank_order": ["tier", "-similarity", "-at_ms", "event_id"],
        "storyline_tier_max": TOLD_STORYLINE_TIER_MAX,
        "dedup": "event_id",
        "excludes_candidate": True,
        "visible_cap": TOLD_MAX,
        "visible_fields": ["i", "ago_min", "key", "type", "sym", "m", "dir", "headline_zh"],
    }
)
NEWS_RETRIEVAL_SHA256: Final[str] = news_retrieval_sha256(told_selector_sha256=TOLD_SELECTOR_SHA256)


class _ExactContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ToldLedgerEntry(_ExactContractModel):
    """One selected card, including audit-only identity and retrieval metadata."""

    i: int = Field(ge=0)
    event_id: str
    at_ms: int = Field(ge=0)
    ago_min: int = Field(ge=0)
    storyline_key: str = ""
    event_type: str = ""
    symbols: tuple[str, ...] = Field(default=(), max_length=TOLD_SYMBOLS_MAX)
    magnitude: int = Field(ge=0, le=3)
    direction: str
    headline_zh: str = Field(max_length=60)
    tier: ToldTier = "recency"
    similarity: float = Field(default=0.0, ge=0.0, le=1.0)
    history_scope: HistoryScope = "recent"
    retrieval_reason: HistoryReason = "recent"


def _row_symbols(row: Mapping[str, Any]) -> frozenset[str]:
    symbols = {base_symbol(str(value)) for value in row.get("grounded_assets") or () if value}
    for asset in row.get("assets") or ():
        symbol = asset.get("symbol") if isinstance(asset, Mapping) else asset
        if symbol:
            symbols.add(base_symbol(str(symbol)))
    return frozenset(symbol for symbol in symbols if symbol)


def _take_with_tier_caps(
    ranked: Sequence[tuple[int, float, int, str, Mapping[str, Any], ToldTier, float]],
    *,
    limit: int,
) -> list[tuple[int, float, int, str, Mapping[str, Any], ToldTier, float]]:
    caps = {TOLD_TIER_ORDER.index("storyline"): TOLD_STORYLINE_TIER_MAX}
    filler_tier = TOLD_TIER_ORDER.index("recency")
    chosen: list[tuple[int, float, int, str, Mapping[str, Any], ToldTier, float]] = []
    overflow: list[tuple[int, float, int, str, Mapping[str, Any], ToldTier, float]] = []
    filler: list[tuple[int, float, int, str, Mapping[str, Any], ToldTier, float]] = []
    used: dict[int, int] = {}
    for item in ranked:
        tier_index = item[0]
        if tier_index == filler_tier:
            filler.append(item)
        elif used.get(tier_index, 0) >= caps.get(tier_index, limit):
            overflow.append(item)
        else:
            used[tier_index] = used.get(tier_index, 0) + 1
            chosen.append(item)
        if len(chosen) >= limit:
            return chosen[:limit]
    for item in (*overflow, *filler):
        if len(chosen) >= limit:
            break
        chosen.append(item)
    return chosen[:limit]


class ToldLedgerSnapshot(_ExactContractModel):
    """The candidate-conditioned slice of bounded reader history visible to EventSemantics."""

    storyline_key: str
    preliminary: bool = True
    entries: tuple[ToldLedgerEntry, ...] = Field(default=(), max_length=TOLD_MAX)
    source_count: int = Field(default=0, ge=0)

    @classmethod
    def select(
        cls,
        rows: Sequence[Mapping[str, Any]],
        *,
        now_ms: int,
        storyline_key: str,
        symbols: Sequence[str] = (),
        comparison_title: str = "",
        exclude_event_id: str = "",
        limit: int = TOLD_MAX,
    ) -> ToldLedgerSnapshot:
        bounded = max(0, min(int(limit), TOLD_MAX))
        candidate_symbols = frozenset(base_symbol(str(value)) for value in symbols if value)
        candidate_title = str(comparison_title or "")
        window = sorted(
            rows,
            key=lambda row: (-int(row.get("at_ms") or 0), str(row.get("event_id") or "")),
        )[:TOLD_SOURCE_MAX]
        ranked: list[tuple[int, float, int, str, Mapping[str, Any], ToldTier, float]] = []
        deduped: set[str] = set()
        for row in window:
            event_id = str(row.get("event_id") or "")
            if not event_id or event_id == exclude_event_id or event_id in deduped:
                continue
            deduped.add(event_id)
            at_ms = int(row.get("at_ms") or 0)
            row_key = str(row.get("storyline_key") or "")
            row_symbols = _row_symbols(row)
            score = similarity(candidate_title, str(row.get("comparison_title") or ""))
            fact_similarity = score if score >= TOLD_FACT_SIMILARITY_MIN else 0.0
            tier: ToldTier
            if row.get("history_scope") == "targeted" and row.get("retrieval_reason") == "exact_fingerprint":
                tier = "exact_fact"
            elif row_key and row_key == storyline_key:
                tier = "storyline"
            elif (
                row.get("history_scope") == "targeted" and row.get("retrieval_reason") == "canonical_asset_overlap"
            ) or (candidate_symbols and candidate_symbols & row_symbols):
                tier = "asset_overlap"
            elif fact_similarity:
                tier = "fact_similarity"
            else:
                tier = "recency"
            ranked.append((TOLD_TIER_ORDER.index(tier), -fact_similarity, -at_ms, event_id, row, tier, fact_similarity))
        ranked.sort(key=lambda item: item[:4])
        chosen = _take_with_tier_caps(ranked, limit=bounded)
        return cls(
            storyline_key=storyline_key,
            source_count=len(deduped),
            entries=tuple(
                ToldLedgerEntry(
                    i=index,
                    event_id=str(row.get("event_id") or ""),
                    at_ms=int(row.get("at_ms") or 0),
                    ago_min=max(0, int(now_ms) - int(row.get("at_ms") or 0)) // 60_000,
                    storyline_key=str(row.get("storyline_key") or ""),
                    event_type=str(row.get("event_type") or ""),
                    symbols=tuple(sorted(_row_symbols(row)))[:TOLD_SYMBOLS_MAX],
                    magnitude=int(row.get("magnitude") or row.get("m") or 0),
                    direction=str(row.get("direction") or row.get("dir") or ""),
                    headline_zh=str(row.get("headline_zh") or "")[:60],
                    tier=tier,
                    similarity=round(fact_similarity, 4),
                    history_scope=cast(HistoryScope, str(row.get("history_scope") or "recent")),
                    retrieval_reason=cast(HistoryReason, str(row.get("retrieval_reason") or "recent")),
                )
                for index, (_, _, _, _, row, tier, fact_similarity) in enumerate(chosen)
            ),
        )


__all__ = [
    "NEWS_RETRIEVAL_SHA256",
    "TOLD_FACT_SIMILARITY_MIN",
    "TOLD_MAX",
    "TOLD_SELECTOR_ID",
    "TOLD_SELECTOR_SHA256",
    "TOLD_SOURCE_MAX",
    "TOLD_STORYLINE_TIER_MAX",
    "TOLD_SYMBOLS_MAX",
    "TOLD_TIER_ORDER",
    "TOLD_WINDOW_MS",
    "ToldLedgerEntry",
    "ToldLedgerSnapshot",
]
