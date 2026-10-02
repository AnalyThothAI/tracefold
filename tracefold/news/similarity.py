"""Two pure text-similarity primitives (no model, microseconds).

`similarity` is Jaccard over character bigrams, because short Chinese headlines have no whitespace tokens;
the market review uses it. A paraphrase with no shared characters scores 0; this primitive does not establish identity.
"""

from __future__ import annotations

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


__all__ = ["character_bigrams", "similarity"]
