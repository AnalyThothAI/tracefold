"""How close one card is to a card the reader already received (pure, no model, microseconds).

The Deduper merges Items that *look* alike (comparison tokens, MinHash/LSH) before Triage sees them. It cannot
merge a Reuters wire in English with a 金十 line in Chinese about the same fact — different bytes, different
family windows — so the same event reaches `decide()` twice as two Events, and until policy v5 the only thing
between it and the reader was a per-storyline *count* cap, which is uncorrelated with whether the second card
says anything new (#81: 74% of everything the throttle stopped was a fact the reader never received in any form).

This module gives `decide()` the one piece of evidence that cap was missing: how much of this card's Chinese
headline the reader has already read. Character bigrams because the text is Chinese (no whitespace tokens) and
short (<= 60 chars); Jaccard because it is symmetric and scale-free. It is deliberately crude — a paraphrase with
no shared characters scores 0.

Through policy v5 it was used only in the direction where a mistake is cheap: a card that looked new was
*released* from the count cap, never dropped for it. Policy v6 (#100) changed that — it is now the primary reason
a card is withheld, including on storylines no count rule ever touched — so `decide()` carries two guards the
metric itself cannot provide: it never withholds an `escalate`, and it never withholds a card whose direction
contradicts the ledger entry it matched (character bigrams are blind to negation: "SEC 批准…" and "SEC 拒绝…"
score 0.60). Anyone raising `similarity_max` should read those guards first.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

_MIN_LENGTH: Final = 2


def character_bigrams(text: str) -> frozenset[str]:
    """Whitespace-insensitive character bigrams. Empty for anything shorter than two non-space characters."""

    compact = "".join(str(text or "").split())
    if len(compact) < _MIN_LENGTH:
        return frozenset()
    return frozenset(compact[index : index + 2] for index in range(len(compact) - 1))


def similarity(left: str, right: str) -> float:
    """Jaccard over character bigrams, in [0, 1]. Two identical headlines score 1.0; unrelated ones score ~0."""

    a, b = character_bigrams(left), character_bigrams(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def max_similarity(text: str, others: Sequence[str]) -> tuple[float, int]:
    """The closest of ``others`` to ``text``: its score and its index, or ``(0.0, -1)`` when there is nothing to
    compare against. The index is what the trace records so ``news why`` can name the card it resembled."""

    mine = character_bigrams(text)
    if not mine:
        return 0.0, -1
    best, best_index = 0.0, -1
    for index, other in enumerate(others):
        theirs = character_bigrams(other)
        if not theirs:
            continue
        score = len(mine & theirs) / len(mine | theirs)
        if score > best:
            best, best_index = score, index
    return best, best_index


__all__ = ["character_bigrams", "max_similarity", "similarity"]
