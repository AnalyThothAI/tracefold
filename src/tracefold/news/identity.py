"""WorldMonitor-compatible lexical Story identity.

This is a direct Python port of WorldMonitor ``shared/story-identity.js`` and
the caller-owned canonical hash normalizer at commit
``0e8785c43e6a693990a14181ae0a16066c15fc8c``.  It is intentionally the only
answer to "are these titles the same story?" inside Tracefold News.
"""

from __future__ import annotations

import math
import re
from bisect import bisect_right
from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
from typing import Final

import regex

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

JAVASCRIPT_WHITESPACE: Final = (
    "\u0009\u000a\u000b\u000c\u000d\u0020\u00a0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000\ufeff"
)
JAVASCRIPT_WHITESPACE_PATTERN: Final = f"[{re.escape(JAVASCRIPT_WHITESPACE)}]"
_UNICODE_LETTER_OR_NUMBER_RE = regex.compile(r"[^\p{L}\p{N}]")
_UNICODE_NUMBER_RE = regex.compile(r"\p{N}")
_UNICODE_UPPERCASE_LETTER_RE = regex.compile(r"\p{Lu}")
_UNICODE_LOWERCASE_LETTER_RE = regex.compile(r"\p{Ll}")
_STORY_DISALLOWED_RE = regex.compile(rf"[^\p{{L}}\p{{N}}{regex.escape(JAVASCRIPT_WHITESPACE)}]")
# Node at the pinned WorldMonitor revision uses Unicode 17. The project still
# supports Python 3.13 / Unicode 15.1, so freeze every newer lowercase mapping.
_UNICODE_17_LOWER_TRANSLATION: Final = str.maketrans(
    {
        0x1C89: 0x1C8A,
        0xA7CB: 0x0264,
        0xA7CC: 0xA7CD,
        0xA7DA: 0xA7DB,
        0xA7DC: 0x019B,
        0xA7CE: 0xA7CF,
        0xA7D2: 0xA7D3,
        0xA7D4: 0xA7D5,
        **{codepoint: codepoint + 0x20 for codepoint in range(0x10D50, 0x10D66)},
        **{codepoint: codepoint + 0x1B for codepoint in range(0x16EA0, 0x16EB9)},
    }
)
_ATTRIBUTION_SUFFIX_RES = (
    re.compile(
        rf"{JAVASCRIPT_WHITESPACE_PATTERN}*[-–—|]{JAVASCRIPT_WHITESPACE_PATTERN}*"
        rf"(?:[A-Za-z0-9_.]|{JAVASCRIPT_WHITESPACE_PATTERN})+\."
        rf"(?:com|org|net|co\.uk){JAVASCRIPT_WHITESPACE_PATTERN}*$",
        re.IGNORECASE | re.ASCII,
    ),
    re.compile(
        rf"{JAVASCRIPT_WHITESPACE_PATTERN}*[-–—|]{JAVASCRIPT_WHITESPACE_PATTERN}*"
        rf"(?:reuters|ap news|bbc|cnn|al jazeera|france 24|dw news|"
        rf"pbs newshour|cbs news|nbc|abc|associated press|the guardian|nos nieuws|"
        rf"tagesschau|cnbc|the national){JAVASCRIPT_WHITESPACE_PATTERN}*$",
        re.IGNORECASE | re.ASCII,
    ),
)
_CANONICAL_ATTRIBUTION_SUFFIX_RES = (
    re.compile(
        rf"{JAVASCRIPT_WHITESPACE_PATTERN}*[-–—]{JAVASCRIPT_WHITESPACE_PATTERN}*"
        rf"(?:[A-Za-z0-9_.]|{JAVASCRIPT_WHITESPACE_PATTERN})+\."
        rf"(?:com|org|net|co\.uk){JAVASCRIPT_WHITESPACE_PATTERN}*$"
    ),
    re.compile(
        rf"{JAVASCRIPT_WHITESPACE_PATTERN}*[-–—]{JAVASCRIPT_WHITESPACE_PATTERN}*"
        rf"(?:reuters|ap news|bbc|cnn|al jazeera|france 24|dw news|"
        rf"pbs newshour|cbs news|nbc|abc|associated press|the guardian|nos nieuws|"
        rf"tagesschau|cnbc|the national){JAVASCRIPT_WHITESPACE_PATTERN}*$"
    ),
)
_JS_WHITESPACE_RE = re.compile(rf"{JAVASCRIPT_WHITESPACE_PATTERN}+")


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


def utf16_slice(text: str, stop: int) -> str:
    encoded = text.encode("utf-16-le", errors="surrogatepass")
    return encoded[: max(0, stop) * 2].decode("utf-16-le", errors="surrogatepass")


def web_usv_string(text: str) -> str:
    """Apply the scalar-value conversion used by Web string encoders."""

    utf16 = str(text).encode("utf-16-le", errors="surrogatepass")
    return utf16.decode("utf-16-le", errors="replace")


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le", errors="surrogatepass")) // 2


def javascript_trim(text: str) -> str:
    return str(text).strip(JAVASCRIPT_WHITESPACE)


def collapse_javascript_whitespace(text: str) -> str:
    return javascript_trim(_JS_WHITESPACE_RE.sub(" ", str(text)))


def parse_javascript_number(text: str) -> float:
    normalized = javascript_trim(str(text))
    if not normalized:
        return 0.0
    for prefix, pattern, base in (
        ("0x", r"0[xX][0-9A-Fa-f]+", 16),
        ("0b", r"0[bB][01]+", 2),
        ("0o", r"0[oO][0-7]+", 8),
    ):
        if re.fullmatch(pattern, normalized):
            try:
                return float(int(normalized[len(prefix) :], base))
            except OverflowError:
                return math.inf
    if (
        re.fullmatch(
            r"[+-]?(?:(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?|Infinity)",
            normalized,
        )
        is None
    ):
        return math.nan
    try:
        return float(normalized)
    except (OverflowError, ValueError):
        return math.nan


def javascript_lower(text: str) -> str:
    """Mirror the pinned Node runtime's Unicode 17 ``toLowerCase``."""

    return str(text).lower().translate(_UNICODE_17_LOWER_TRANSLATION)


def javascript_is_letter_or_number(value: str) -> bool:
    r"""Mirror pinned JavaScript ``/[\p{L}\p{N}]/u.test(value)``."""

    return bool(value) and _UNICODE_LETTER_OR_NUMBER_RE.search(value) is None


def javascript_starts_with_uppercase_letter(value: str) -> bool:
    r"""Mirror pinned JavaScript ``/^\p{Lu}/u.test(value)``."""

    return bool(value) and _UNICODE_UPPERCASE_LETTER_RE.match(value) is not None


def javascript_starts_with_lowercase_letter(value: str) -> bool:
    r"""Mirror pinned JavaScript ``/^\p{Ll}/u.test(value)``."""

    return bool(value) and _UNICODE_LOWERCASE_LETTER_RE.match(value) is not None


def utf16_sort_key(text: str) -> tuple[int, ...]:
    encoded = str(text).encode("utf-16-le", errors="surrogatepass")
    return tuple(encoded[index] | (encoded[index + 1] << 8) for index in range(0, len(encoded), 2))


def _utf16_ngrams(text: str, width: int) -> tuple[str, ...]:
    encoded = text.encode("utf-16-le", errors="surrogatepass")
    unit_count = len(encoded) // 2
    return tuple(
        encoded[index * 2 : (index + width) * 2].decode("utf-16-le", errors="surrogatepass")
        for index in range(max(0, unit_count - width + 1))
    )


@lru_cache(maxsize=8192)
def normalize_story_text(text: str) -> str:
    lowered = javascript_lower(str(text or ""))
    return collapse_javascript_whitespace(_STORY_DISALLOWED_RE.sub(" ", lowered))


@lru_cache(maxsize=8192)
def normalize_story_canonical_title(text: str) -> str:
    """Mirror list-feed-digest's caller-owned titleHash normalizer."""

    normalized = javascript_lower(str(text or ""))
    for pattern in _CANONICAL_ATTRIBUTION_SUFFIX_RES:
        normalized = pattern.sub("", normalized)
    normalized = _STORY_DISALLOWED_RE.sub("", normalized)
    return utf16_slice(collapse_javascript_whitespace(normalized), MAX_CANONICAL_TITLE_CHARS)


def public_story_title_hash(normalized_title: str) -> str:
    """Hash one canonical title with Web ``TextEncoder`` USVString semantics."""

    return sha256(web_usv_string(normalized_title).encode("utf-8")).hexdigest()


def _is_non_ascii(token: str) -> bool:
    return any(ord(char) > 127 for char in token)


@lru_cache(maxsize=8192)
def candidate_tokens(text: str) -> frozenset[str]:
    result: set[str] = set()
    clamped = utf16_slice(strip_attribution_suffix(text), MAX_IDENTITY_CHARS)
    for token in normalize_story_text(clamped).split(" "):
        if _is_non_ascii(token):
            result.add(token)
            result.update(_utf16_ngrams(token, 2))
        elif utf16_length(token) >= 3:
            result.add(token)
    return frozenset(result)


def _content_tokens(text: str) -> list[tuple[str, float]]:
    kept: list[tuple[str, float]] = []
    clamped = utf16_slice(strip_attribution_suffix(text), MAX_IDENTITY_CHARS)
    for raw in _JS_WHITESPACE_RE.split(clamped):
        clean = _UNICODE_LETTER_OR_NUMBER_RE.sub("", raw)
        if not clean:
            continue
        token = javascript_lower(clean)
        if not _is_non_ascii(token) and utf16_length(token) < 3:
            continue
        has_digit = _UNICODE_NUMBER_RE.search(clean) is not None
        capitalized = _UNICODE_UPPERCASE_LETTER_RE.match(clean) is not None
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
        if utf16_length(token) >= 4:
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
    "JAVASCRIPT_WHITESPACE",
    "JAVASCRIPT_WHITESPACE_PATTERN",
    "STORY_SIMILARITY_THRESHOLD",
    "StoryVector",
    "candidate_tokens",
    "cluster_texts",
    "collapse_javascript_whitespace",
    "cosine_similarity",
    "javascript_is_letter_or_number",
    "javascript_lower",
    "javascript_starts_with_lowercase_letter",
    "javascript_starts_with_uppercase_letter",
    "javascript_trim",
    "normalize_story_canonical_title",
    "normalize_story_text",
    "parse_javascript_number",
    "public_story_title_hash",
    "story_similarity",
    "story_vector",
    "strip_attribution_suffix",
    "utf16_length",
    "utf16_slice",
    "utf16_sort_key",
    "web_usv_string",
]
