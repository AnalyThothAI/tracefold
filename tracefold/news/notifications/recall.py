"""Linked receipts first; ordinary candidates use the shared proposition ranker."""

from __future__ import annotations

from dataclasses import dataclass

from ..claim_recall import Ranking
from .novelty import ReaderNovelty
from .reader import READER_MESSAGES_MAX


@dataclass(frozen=True, slots=True)
class ClaimSelection:
    intent_ids: tuple[str, ...]
    reasons: tuple[tuple[str, tuple[str, ...]], ...]


def select_for_claim(novelty: ReaderNovelty, ranking: Ranking, *, available: frozenset[str]) -> ClaimSelection:
    linked = tuple(i for i in novelty.linked_intents if i in available)
    hits = {h.key: h for h in ranking.hits if h.key in available}
    selected = tuple(dict.fromkeys((*linked, *hits)))[:READER_MESSAGES_MAX]
    return ClaimSelection(selected, tuple((i, ("semantic",) if i in linked else hits[i].routes) for i in selected))
