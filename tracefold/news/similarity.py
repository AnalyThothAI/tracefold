"""How close one card is to a card the reader already received (pure, no model, microseconds).

The Deduper merges Items that *look* alike (comparison tokens, MinHash/LSH) before Triage sees them. It cannot
merge a Reuters wire in English with a 金十 line in Chinese about the same fact — different bytes, different
family windows — so the same event can reach `decide()` twice as two Events.

This module gives `decide()` the one piece of evidence that cap was missing: how much of this card's Chinese
headline the reader has already read. Character bigrams because the text is Chinese (no whitespace tokens) and
short (<= 60 chars); Jaccard because it is symmetric and scale-free. It is deliberately crude — a paraphrase with
no shared characters scores 0.

Policy v7 uses this evidence directly and has no count quota. `decide()` carries two guards the metric itself
cannot provide: it never withholds an `escalate`, and it never withholds a card whose direction contradicts the
ledger entry it matched (character bigrams are blind to negation: "SEC 批准…" and "SEC 拒绝…" score 0.60).
Anyone raising `similarity_max` should read those guards first.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

_MIN_LENGTH: Final = 2
_TRIGRAM_PAD_LEFT: Final = "  "
_TRIGRAM_PAD_RIGHT: Final = " "


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


def word_trigrams(text: str) -> frozenset[str]:
    """pg_trgm's trigram set, so the told selector ranks with the same number PostgreSQL retrieves with (#491).

    Lower-case the text, split it into runs of alphanumeric characters, pad each run with two leading spaces and
    one trailing space, and take every consecutive three characters. That is `show_trgm()` with its default
    build options (IGNORECASE, KEEPONLYALNUM, no DIVIDED_SIGNATURE): "cat" gives {"  c", " ca", "cat", "at "}.
    CJK characters are alphanumeric, so a Chinese title without spaces is one long word and its trigrams are
    consecutive characters. pg_trgm hashes a multibyte trigram to three bytes before comparing; equality on the
    characters themselves differs only by that hash's collisions.

    Character bigrams stay for `decide()`: that comparison is Chinese headline against Chinese headline, where
    bigrams are the right grain. Word trigrams are for `comparison_title`, which is English 87% of the time and
    where the word-boundary padding is what separates "same wire, other outlet" from "shares three letters":
    on 22k random English title pairs 4.6% score >= 0.25 on bigrams and 0.10% on trigrams, while the labelled
    duplicates keep a median of 0.19-0.27 either way.
    """

    result: set[str] = set()
    word: list[str] = []

    def flush() -> None:
        if word:
            padded = f"{_TRIGRAM_PAD_LEFT}{''.join(word)}{_TRIGRAM_PAD_RIGHT}"
            result.update(padded[index : index + 3] for index in range(len(padded) - 2))
            word.clear()

    for char in str(text or "").lower():
        if char.isalnum():
            word.append(char)
        else:
            flush()
    flush()
    return frozenset(result)


def trigram_similarity(left: str, right: str) -> float:
    """pg_trgm `similarity(left, right)`: shared trigrams over the union, in [0, 1]."""

    a, b = word_trigrams(left), word_trigrams(right)
    if not a or not b:
        return 0.0
    shared = len(a & b)
    return shared / (len(a) + len(b) - shared)


__all__ = ["character_bigrams", "max_similarity", "similarity", "trigram_similarity", "word_trigrams"]
