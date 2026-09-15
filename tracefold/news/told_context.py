"""Candidate-conditioned selection over one already-bounded reader-history snapshot."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from .artifact_identity import canonical_sha
from .events.storyline import same_storyline_key
from .models import MarketAsset, base_symbol, market_assets_overlap
from .reader_history import (
    READER_HISTORY_SHA256,
    RECENT_HISTORY_MAX,
    SIMILAR_TITLE_MAX,
    TARGETED_ASSET_MAX,
    TARGETED_EXACT_MAX,
    HistoryReason,
    HistoryScope,
    news_retrieval_sha256,
)
from .similarity import trigram_similarity

# Every row the bounded history can hand over: the four bands' caps summed. Selection sorts the source newest
# first and truncates here, so the bound has to admit the whole title-similarity band or the oldest of its rows
# — the ones the 4 h ledger could not see, which is what the band exists for — would be the first dropped (#491).
TOLD_SOURCE_MAX: Final[int] = RECENT_HISTORY_MAX + TARGETED_EXACT_MAX + TARGETED_ASSET_MAX + SIMILAR_TITLE_MAX
TOLD_MAX: Final[int] = 16
TOLD_STORYLINE_TIER_MAX: Final[int] = 8
TOLD_SYMBOLS_MAX: Final[int] = 6
# The tier boundary for a cross-storyline row: at or above this pg_trgm score a row is shown as a same-fact
# title match rather than as recency filler. On the 2026-09-01 audit 0.15 admits 2.2% of random English title
# pairs and 0.25 admits 0.10%; the labelled duplicates sit at a median of 0.19 (cross-window) to 0.27 (already
# shown). Ranking inside every tier uses the raw score, never this floor: zeroing sub-threshold scores and then
# filling by recency is what let 2-3 English near-misses plus the last few minutes occupy a storyline tier of
# 360 same-key cards a day, while the card the candidate actually repeated sat 59 min back.
TOLD_FACT_SIMILARITY_MIN: Final[float] = 0.15
ToldTier = Literal["exact_fact", "storyline", "asset_overlap", "fact_similarity", "recency"]
TOLD_TIER_ORDER: Final[tuple[ToldTier, ...]] = (
    "exact_fact",
    "storyline",
    "asset_overlap",
    "fact_similarity",
    "recency",
)
TOLD_SELECTOR_ID: Final[str] = "told_context_selector_v5"
TOLD_SELECTOR_SHA256: Final[str] = canonical_sha(
    {
        "selector": TOLD_SELECTOR_ID,
        "reader_history_sha256": READER_HISTORY_SHA256,
        "source_truth": "ReaderHistorySnapshot.told_source_rows",
        "source_projection": [
            "event_id",
            "at_ms",
            "storyline_key",
            "magnitude",
            "direction",
            "headline_zh",
            "grounded_assets",
            "assets",
            "comparison_title",
            "comparison_fingerprint",
            "dedupe_family",
            "why_zh",
            "history_scope",
            "retrieval_reason",
        ],
        "source_max": TOLD_SOURCE_MAX,
        "tier_order": list(TOLD_TIER_ORDER),
        "trusted_targeted_tiers": {
            "exact_fingerprint": "exact_fact",
            "canonical_asset_overlap": "asset_overlap",
        },
        # #651 §6.2: an asset overlap is `(market_type, base_symbol)` when both sides name a market, and
        # base symbol alone when either says `unknown` — which every row written before #651 does. The
        # storyline tier compares keys the same way, so a preliminary untyped key still meets the typed
        # final key of the cards it is ranked against.
        "symbol_primitive": "market_asset_v1",
        "storyline_match": "same_storyline_key_market_aware_v1",
        "similarity_primitive": "pg_trgm_word_trigram_jaccard_v1",
        "similarity_field": "comparison_title",
        "similarity_min": TOLD_FACT_SIMILARITY_MIN,
        "similarity_ranking": "raw_score_in_every_tier",
        "rank_order": ["tier", "-similarity", "-at_ms", "event_id"],
        "storyline_tier_max": TOLD_STORYLINE_TIER_MAX,
        "dedup": "event_id",
        "excludes_candidate": True,
        "visible_cap": TOLD_MAX,
        "visible_fields": [
            "i",
            "ago_min",
            "storyline_key",
            "comparison_title",
            "symbols",
            "magnitude",
            "direction",
            "headline_zh",
            "why_zh",
        ],
    }
)
NEWS_RETRIEVAL_SHA256: Final[str] = news_retrieval_sha256(told_selector_sha256=TOLD_SELECTOR_SHA256)

_TOLD_SOURCE_FIELDS: Final = frozenset(
    {
        "event_id",
        "at_ms",
        "storyline_key",
        "comparison_title",
        "comparison_fingerprint",
        "dedupe_family",
        "grounded_assets",
        "assets",
        "canonical_assets",
        "magnitude",
        "direction",
        "headline_zh",
        "why_zh",
        "history_scope",
        "retrieval_reason",
    }
)


class _ExactContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ToldLedgerEntry(_ExactContractModel):
    """One selected card, including audit-only identity and retrieval metadata."""

    i: int = Field(ge=0)
    event_id: str
    at_ms: int = Field(ge=0)
    ago_min: int = Field(ge=0)
    storyline_key: str = ""
    comparison_title: str = ""
    comparison_fingerprint: str = ""
    symbols: tuple[str, ...] = Field(default=(), max_length=TOLD_SYMBOLS_MAX)
    magnitude: int = Field(ge=0, le=3)
    direction: str
    headline_zh: str = Field(max_length=60)
    why_zh: str = Field(default="", max_length=140)
    tier: ToldTier = "recency"
    similarity: float = Field(default=0.0, ge=0.0, le=1.0)
    history_scope: HistoryScope = "recent"
    retrieval_reason: HistoryReason = "recent"


def _row_assets(row: Mapping[str, Any]) -> frozenset[MarketAsset]:
    """Every instrument one delivered row was about, typed where the row can say so (#651 §6.2).

    A provider tag carries no market, so it enters as `unknown` and keeps matching on the symbol alone —
    which is exactly what it did before, and what every row written before #651 will always do. Only the
    judgment's own assets can contradict, and only when both sides say something.
    """

    assets = {MarketAsset(base_symbol(str(value)), "unknown") for value in row.get("grounded_assets") or () if value}
    for asset in row.get("assets") or ():
        typed = MarketAsset.of(asset)
        if typed.symbol:
            assets.add(typed)
    return frozenset(asset for asset in assets if asset.symbol)


def _row_symbols(row: Mapping[str, Any]) -> frozenset[str]:
    """The bare symbols one row names, which is what the model-visible `symbols` field renders."""

    return frozenset(asset.symbol for asset in _row_assets(row))


_Ranked = tuple[int, float, int, str, Mapping[str, Any], ToldTier, float]


def _take_with_tier_caps(ranked: Sequence[_Ranked], *, limit: int) -> list[_Ranked]:
    caps = {TOLD_TIER_ORDER.index("storyline"): TOLD_STORYLINE_TIER_MAX}
    filler_tier = TOLD_TIER_ORDER.index("recency")
    chosen: list[_Ranked] = []
    overflow: list[_Ranked] = []
    filler: list[_Ranked] = []
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
        candidate_assets = frozenset(MarketAsset.of(value) for value in symbols if value)
        candidate_title = str(comparison_title or "")
        window = sorted(
            rows,
            key=lambda row: (-int(row.get("at_ms") or 0), str(row.get("event_id") or "")),
        )[:TOLD_SOURCE_MAX]
        ranked: list[_Ranked] = []
        deduped: set[str] = set()
        for row in window:
            unexpected = set(row).difference(_TOLD_SOURCE_FIELDS)
            if unexpected:
                raise ValueError(f"news_told_context_fields_unexpected:{','.join(sorted(unexpected))}")
            required = {"dedupe_family", "magnitude", "direction", "headline_zh", "why_zh"}
            missing = required.difference(row)
            if missing:
                raise ValueError(f"news_told_context_fields_missing:{','.join(sorted(missing))}")
            event_id = str(row.get("event_id") or "")
            if not event_id or event_id == exclude_event_id or event_id in deduped:
                continue
            deduped.add(event_id)
            at_ms = int(row.get("at_ms") or 0)
            row_key = str(row.get("storyline_key") or "")
            row_assets = _row_assets(row)
            score = trigram_similarity(candidate_title, str(row.get("comparison_title") or ""))
            tier: ToldTier
            if row.get("history_scope") == "targeted" and row.get("retrieval_reason") == "exact_fingerprint":
                tier = "exact_fact"
            elif same_storyline_key(storyline_key, row_key):
                tier = "storyline"
            elif (
                row.get("history_scope") == "targeted" and row.get("retrieval_reason") == "canonical_asset_overlap"
            ) or (candidate_assets and market_assets_overlap(candidate_assets, row_assets)):
                tier = "asset_overlap"
            elif score >= TOLD_FACT_SIMILARITY_MIN:
                tier = "fact_similarity"
            else:
                tier = "recency"
            # The raw score orders every tier, recency included: the most title-similar of the rows that earned
            # no tier is a better use of a filler slot than the newest of them.
            ranked.append((TOLD_TIER_ORDER.index(tier), -score, -at_ms, event_id, row, tier, score))
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
                    comparison_title=str(row.get("comparison_title") or "")[:600],
                    comparison_fingerprint=str(row.get("comparison_fingerprint") or ""),
                    symbols=tuple(sorted(_row_symbols(row)))[:TOLD_SYMBOLS_MAX],
                    magnitude=int(row["magnitude"]),
                    direction=str(row["direction"]),
                    headline_zh=str(row["headline_zh"])[:60],
                    why_zh=str(row["why_zh"])[:140],
                    tier=tier,
                    similarity=round(score, 4),
                    history_scope=cast(HistoryScope, str(row.get("history_scope") or "recent")),
                    retrieval_reason=cast(HistoryReason, str(row.get("retrieval_reason") or "recent")),
                )
                for index, (_, _, _, _, row, tier, score) in enumerate(chosen)
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
    "ToldLedgerEntry",
    "ToldLedgerSnapshot",
]
