"""Two pure text-similarity primitives (no model, microseconds).

`similarity` is Jaccard over character bigrams, because short Chinese headlines have no whitespace tokens;
the market review uses it. `trigram_similarity` is the Python twin of PostgreSQL pg_trgm, so the reader
history's title band ranks with the same number PostgreSQL retrieves with. Both are deliberately crude: a
paraphrase with no shared characters scores 0, and neither sees negation.
"""

from __future__ import annotations

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


def word_trigrams(text: str) -> frozenset[str]:
    """pg_trgm's trigram set, so the reader-history title band ranks with the number PostgreSQL retrieves with.

    Lower-case the text, split it into runs of alphanumeric characters, pad each run with two leading spaces and
    one trailing space, and take every consecutive three characters. That is `show_trgm()` with its default
    build options (IGNORECASE, KEEPONLYALNUM, no DIVIDED_SIGNATURE): "cat" gives {"  c", " ca", "cat", "at "}.
    CJK characters are alphanumeric, so a Chinese title without spaces is one long word and its trigrams are
    consecutive characters. pg_trgm hashes a multibyte trigram to three bytes before comparing; equality on the
    characters themselves differs only by that hash's collisions.

    Character bigrams stay for Chinese headline against Chinese headline, where bigrams are the right grain.
    Word trigrams are for `comparison_title`, which is English 87% of the time and
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


__all__ = ["character_bigrams", "similarity", "trigram_similarity", "word_trigrams"]
