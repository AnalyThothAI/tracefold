"""WorldMonitor-compatible lexical Story identity.

This is a direct Python port of WorldMonitor ``shared/story-identity.js`` and
the caller-owned canonical hash normalizer at commit
``0e8785c43e6a693990a14181ae0a16066c15fc8c``.  It is intentionally the only
answer to "are these titles the same story?" inside Tracefold News.
"""

from __future__ import annotations

import math
import re
import unicodedata
from bisect import bisect_right
from dataclasses import dataclass
from functools import lru_cache
from typing import Final

DIM: Final = 512
STORY_SIMILARITY_THRESHOLD: Final = 0.615
MAX_CANDIDATE_BUCKET: Final = 250
MAX_IDENTITY_CHARS: Final = 300
MAX_CANONICAL_TITLE_CHARS: Final = 120

WEIGHT_TOKEN: Final = 2.0
WEIGHT_BIGRAM: Final = 1.5
WEIGHT_CHARGRAM: Final = 1.0
BOOST_ENTITY: Final = 3.0
BOOST_NUMBER: Final = 2.0

CONTAINMENT_RESCUE_MIN_TOKENS: Final = 4
CONTAINMENT_RESCUE_RATIO: Final = 0.9
CONTAINMENT_RESCUE_SCORE: Final = 0.9

_ATTRIBUTION_SUFFIX_RES = (
    re.compile(r"\s*[-–—|]\s*[A-Za-z0-9_\s.]+\.(?:com|org|net|co\.uk)\s*$", re.IGNORECASE),
    re.compile(
        r"\s*[-–—|]\s*(?:reuters|ap news|bbc|cnn|al jazeera|france 24|dw news|"
        r"pbs newshour|cbs news|nbc|abc|associated press|the guardian|nos nieuws|"
        r"tagesschau|cnbc|the national)\s*$",
        re.IGNORECASE,
    ),
)
_CANONICAL_ATTRIBUTION_SUFFIX_RES = (
    re.compile(r"\s*[-–—]\s*[A-Za-z0-9_\s.]+\.(?:com|org|net|co\.uk)\s*$"),
    re.compile(
        r"\s*[-–—]\s*(?:reuters|ap news|bbc|cnn|al jazeera|france 24|dw news|"
        r"pbs newshour|cbs news|nbc|abc|associated press|the guardian|nos nieuws|"
        r"tagesschau|cnbc|the national)\s*$"
    ),
)


@dataclass(frozen=True, slots=True)
class StoryVector:
    uniform: tuple[float, ...]
    boosted: tuple[float, ...]
    tokens: frozenset[str]


def strip_attribution_suffix(text: str) -> str:
    result = str(text or "")
    for pattern in _ATTRIBUTION_SUFFIX_RES:
        result = pattern.sub("", result)
    return result


def _utf16_slice(text: str, stop: int) -> str:
    encoded = text.encode("utf-16-le", errors="surrogatepass")
    return encoded[: max(0, stop) * 2].decode("utf-16-le", errors="surrogatepass")


def _utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le", errors="surrogatepass")) // 2


def _utf16_ngrams(text: str, width: int) -> tuple[str, ...]:
    encoded = text.encode("utf-16-le", errors="surrogatepass")
    unit_count = len(encoded) // 2
    return tuple(
        encoded[index * 2 : (index + width) * 2].decode("utf-16-le", errors="surrogatepass")
        for index in range(max(0, unit_count - width + 1))
    )


@lru_cache(maxsize=8192)
def normalize_story_text(text: str) -> str:
    lowered = str(text or "").lower()
    chars = (char if unicodedata.category(char).startswith(("L", "N")) or char.isspace() else " " for char in lowered)
    return " ".join("".join(chars).split())


@lru_cache(maxsize=8192)
def normalize_story_canonical_title(text: str) -> str:
    """Mirror list-feed-digest's caller-owned titleHash normalizer."""

    normalized = str(text or "").lower()
    for pattern in _CANONICAL_ATTRIBUTION_SUFFIX_RES:
        normalized = pattern.sub("", normalized)
    normalized = "".join(
        char for char in normalized if unicodedata.category(char).startswith(("L", "N")) or char.isspace()
    )
    return _utf16_slice(" ".join(normalized.split()), MAX_CANONICAL_TITLE_CHARS)


def _is_non_ascii(token: str) -> bool:
    return any(ord(char) > 127 for char in token)


@lru_cache(maxsize=8192)
def candidate_tokens(text: str) -> frozenset[str]:
    result: set[str] = set()
    clamped = _utf16_slice(strip_attribution_suffix(text), MAX_IDENTITY_CHARS)
    for token in normalize_story_text(clamped).split():
        if _is_non_ascii(token):
            result.add(token)
            result.update(_utf16_ngrams(token, 2))
        elif _utf16_length(token) >= 3:
            result.add(token)
    return frozenset(result)


def _content_tokens(text: str) -> list[tuple[str, float]]:
    kept: list[tuple[str, float]] = []
    clamped = _utf16_slice(strip_attribution_suffix(text), MAX_IDENTITY_CHARS)
    for raw in clamped.split():
        clean = "".join(char for char in raw if unicodedata.category(char).startswith(("L", "N")))
        if not clean:
            continue
        token = clean.lower()
        if not _is_non_ascii(token) and _utf16_length(token) < 3:
            continue
        has_digit = any(unicodedata.category(char).startswith("N") for char in clean)
        capitalized = bool(clean) and unicodedata.category(clean[0]) == "Lu"
        boost = BOOST_NUMBER if has_digit else BOOST_ENTITY if capitalized else 1.0
        kept.append((token, boost))
    return kept


def _fnv1a(value: str, seed: int) -> int:
    result = (0x811C9DC5 ^ seed) & 0xFFFFFFFF
    # JavaScript charCodeAt hashes UTF-16 code units, not Unicode code points.
    encoded = value.encode("utf-16-le", errors="surrogatepass")
    for index in range(0, len(encoded), 2):
        code_unit = encoded[index] | (encoded[index + 1] << 8)
        result ^= code_unit
        result = (result * 0x01000193) & 0xFFFFFFFF
    return result


def _add_feature(vector: list[float], feature: str, weight: float) -> None:
    index = _fnv1a(feature, 0) % DIM
    sign = 1.0 if _fnv1a(feature, 0x9E3779B9) & 1 else -1.0
    vector[index] += sign * weight


def _l2_normalize(vector: list[float]) -> tuple[float, ...] | None:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        return None
    return tuple(value / norm for value in vector)


@lru_cache(maxsize=8192)
def story_vector(text: str) -> StoryVector | None:
    tokens = _content_tokens(text)
    if not tokens:
        return None
    uniform = [0.0] * DIM
    boosted = [0.0] * DIM
    for index, (token, boost) in enumerate(tokens):
        _add_feature(uniform, f"w:{token}", WEIGHT_TOKEN)
        _add_feature(boosted, f"w:{token}", WEIGHT_TOKEN * boost)
        if index + 1 < len(tokens):
            bigram = f"b:{token} {tokens[index + 1][0]}"
            _add_feature(uniform, bigram, WEIGHT_BIGRAM)
            _add_feature(boosted, bigram, WEIGHT_BIGRAM)
        if _is_non_ascii(token):
            for gram in _utf16_ngrams(token, 2):
                feature = f"c2:{gram}"
                _add_feature(uniform, feature, WEIGHT_CHARGRAM)
                _add_feature(boosted, feature, WEIGHT_CHARGRAM)
        if _utf16_length(token) >= 4:
            padded = f"<{token}>"
            for gram in _utf16_ngrams(padded, 4):
                feature = f"c4:{gram}"
                _add_feature(uniform, feature, WEIGHT_CHARGRAM)
                _add_feature(boosted, feature, WEIGHT_CHARGRAM)
    normalized_uniform = _l2_normalize(uniform)
    normalized_boosted = _l2_normalize(boosted)
    if normalized_uniform is None or normalized_boosted is None:
        return None
    return StoryVector(
        uniform=normalized_uniform,
        boosted=normalized_boosted,
        tokens=frozenset(token for token, _ in tokens),
    )


def _dot(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    if len(left) != len(right):
        return 0.0
    return float(math.sumprod(left, right))


def _has_containment_rescue(left: StoryVector, right: StoryVector) -> bool:
    small, large = (left.tokens, right.tokens) if len(left.tokens) <= len(right.tokens) else (right.tokens, left.tokens)
    return len(small) >= CONTAINMENT_RESCUE_MIN_TOKENS and len(small & large) / len(small) >= CONTAINMENT_RESCUE_RATIO


def cosine_similarity(left: StoryVector | None, right: StoryVector | None) -> float:
    if left is None or right is None:
        return 0.0
    score = min(
        _dot(left.uniform, right.uniform),
        _dot(left.boosted, right.boosted),
    )
    if score < CONTAINMENT_RESCUE_SCORE and _has_containment_rescue(left, right):
        return CONTAINMENT_RESCUE_SCORE
    return max(0.0, min(1.0, score))


def _meets_similarity_threshold(
    left: StoryVector | None,
    right: StoryVector | None,
    *,
    threshold: float,
) -> bool:
    """Compare one candidate without calculating a second losing dense dot."""

    if math.isnan(threshold):
        return False
    if threshold <= 0.0:
        return True
    if threshold > 1.0 or left is None or right is None:
        return False
    uniform_score = _dot(left.uniform, right.uniform)
    if uniform_score < threshold:
        return threshold <= CONTAINMENT_RESCUE_SCORE and _has_containment_rescue(left, right)
    boosted_score = _dot(left.boosted, right.boosted)
    score = min(uniform_score, boosted_score)
    if score >= threshold:
        return True
    return (
        score < CONTAINMENT_RESCUE_SCORE
        and threshold <= CONTAINMENT_RESCUE_SCORE
        and _has_containment_rescue(left, right)
    )


def story_similarity(left: str, right: str) -> float:
    return cosine_similarity(story_vector(left), story_vector(right))


def is_same_story(
    left: str,
    right: str,
    threshold: float = STORY_SIMILARITY_THRESHOLD,
) -> bool:
    return story_similarity(left, right) >= threshold


def cluster_texts(
    texts: list[str] | tuple[str, ...],
    *,
    threshold: float = STORY_SIMILARITY_THRESHOLD,
) -> list[list[int]]:
    """Return deterministic connected components over WorldMonitor edges."""

    values = list(texts)
    vectors = [story_vector(text) for text in values]
    token_sets = [candidate_tokens(text) for text in values]
    inverted: dict[str, list[int]] = {}
    for index, tokens in enumerate(token_sets):
        for token in tokens:
            inverted.setdefault(token, []).append(index)

    parent = list(range(len(values)))

    def find(index: int) -> int:
        root = index
        while parent[root] != root:
            root = parent[root]
        while parent[index] != root:
            next_index = parent[index]
            parent[index] = root
            index = next_index
        return root

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return
        if left_root < right_root:
            parent[right_root] = left_root
        else:
            parent[left_root] = right_root

    exact: dict[str, int] = {}
    for index, text in enumerate(values):
        normalized = normalize_story_text(text)
        if not normalized:
            continue
        first = exact.setdefault(normalized, index)
        if first != index:
            union(first, index)

    for index, vector in enumerate(vectors):
        if vector is None:
            continue
        candidates: set[int] = set()
        for token in token_sets[index]:
            bucket = inverted.get(token, ())
            if len(bucket) > MAX_CANDIDATE_BUCKET:
                continue
            candidates.update(bucket[bisect_right(bucket, index) :])
        for candidate in sorted(candidates):
            if find(index) == find(candidate):
                continue
            if _meets_similarity_threshold(
                vector,
                vectors[candidate],
                threshold=threshold,
            ):
                union(index, candidate)

    grouped: dict[int, list[int]] = {}
    for index in range(len(values)):
        grouped.setdefault(find(index), []).append(index)
    return [grouped[root] for root in sorted(grouped)]


__all__ = [
    "DIM",
    "STORY_SIMILARITY_THRESHOLD",
    "StoryVector",
    "candidate_tokens",
    "cluster_texts",
    "cosine_similarity",
    "is_same_story",
    "normalize_story_canonical_title",
    "normalize_story_text",
    "story_similarity",
    "story_vector",
    "strip_attribution_suffix",
]
