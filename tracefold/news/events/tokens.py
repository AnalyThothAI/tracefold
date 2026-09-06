"""Pinned comparison tokenizer shared by Event identity and MinHash."""

from __future__ import annotations

from typing import Final

import regex

MAX_TOKENS: Final = 256

_WORD_RE = regex.compile(r"[\p{L}\p{N}]+(?:['_-][\p{L}\p{N}]+)*")
_CJK_RE = regex.compile(r"^[\p{Han}\p{Hiragana}\p{Katakana}\p{Hangul}]+$")
_ACTION_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("increase", ("raise", "raises", "raised", "increase", "increases", "rise", "rises", "surge", "surges")),
    ("decrease", ("cut", "cuts", "reduce", "reduces", "fall", "falls", "drop", "drops", "decline", "declines")),
    ("acquire", ("acquire", "acquires", "buy", "buys", "purchase", "purchases")),
    ("sell", ("sell", "sells", "sold", "dispose", "disposes")),
    ("file", ("filing", "files", "filed")),
    ("list", ("listing", "lists", "listed")),
    ("delist", ("delisting", "delists", "delisted")),
    ("announce", ("announce", "announces", "announced", "report", "reports", "reported")),
    ("approve", ("approve", "approves", "approved", "authorize", "authorizes")),
    ("reject", ("reject", "rejects", "rejected", "deny", "denies")),
)
_ALIASES = {form: key for key, forms in _ACTION_GROUPS for form in forms}
_STOP: Final = frozenset(
    {
        "a",
        "an",
        "and",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "in",
        "into",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "was",
        "were",
        "will",
        "with",
        "over",
        "after",
        "before",
        "amid",
        "says",
        "say",
        "said",
        "new",
        "just",
        "now",
        "via",
        "than",
        "has",
        "have",
        "had",
        "are",
        "been",
        "被",
        "的",
        "了",
        "在",
        "和",
        "与",
        "及",
        "为",
        "对",
        "将",
        "已",
        "或",
        "等",
    }
)


def comparison_tokens(comparison: str) -> frozenset[str]:
    """Token set over an already-normalized comparison string (see exact_atom_identity)."""

    result: set[str] = set()
    for match in _WORD_RE.finditer(comparison):
        token = _ALIASES.get(match.group(0), match.group(0))
        if _CJK_RE.fullmatch(token):
            if len(token) == 1:
                result.add(token)
            else:
                result.update(token[index : index + 2] for index in range(len(token) - 1))
        elif (len(token) >= 2 or token[0].isdigit()) and token not in _STOP:
            result.add(token)
        if len(result) >= MAX_TOKENS:
            break
    return frozenset(sorted(result)[:MAX_TOKENS])


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    return inter / len(a | b)


__all__ = ["MAX_TOKENS", "comparison_tokens", "jaccard"]
