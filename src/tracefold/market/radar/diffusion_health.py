from __future__ import annotations

import html
import re
from collections import Counter, defaultdict
from typing import Any

from tracefold.market.radar.social_signal_features import author_entropy

TOP_AUTHOR_CONCENTRATION = 0.70
DUPLICATE_TEXT_CLUSTER = 0.50
MIN_HEALTHY_AUTHORS = 2

_URL_RE = re.compile(r"\b(?:https?://|www\.)\S+", re.IGNORECASE)
_EVM_ADDRESS_RE = re.compile(r"\b0x[a-f0-9]{40}\b", re.IGNORECASE)
_SOLANA_ADDRESS_RE = re.compile(r"(?<![A-Za-z0-9])[1-9a-z]{32,44}(?![A-Za-z0-9])")
_WHITESPACE_RE = re.compile(r"\s+")


def text_fingerprint(value: str | None) -> str:
    text = html.unescape(value or "").lower()
    text = _URL_RE.sub(" ", text)
    text = _EVM_ADDRESS_RE.sub(" ", text)
    text = _SOLANA_ADDRESS_RE.sub(" ", text)
    return _WHITESPACE_RE.sub(" ", text).strip()


def diffusion_health(mentions: list[dict[str, Any]]) -> dict[str, Any]:
    total_mentions = len(mentions)

    author_counts: Counter[str] = Counter()
    author_followers: dict[str, int] = defaultdict(int)
    author_latest_seen: dict[str, int] = defaultdict(int)
    author_fingerprints: dict[str, set[str]] = defaultdict(set)
    fingerprint_counts: Counter[str] = Counter()

    for mention in mentions:
        handle = _normalize_handle(mention.get("author_handle"))
        fingerprint = str(mention.get("text_fingerprint") or "").strip()
        if not fingerprint:
            fingerprint = text_fingerprint(mention.get("text_clean") or mention.get("search_text"))
        if fingerprint:
            fingerprint_counts[fingerprint] += 1
        if not handle:
            continue

        author_counts[handle] += 1
        author_followers[handle] = max(author_followers[handle], int(mention.get("author_followers") or 0))
        author_latest_seen[handle] = max(author_latest_seen[handle], int(mention.get("received_at_ms") or 0))
        if fingerprint:
            author_fingerprints[handle].add(fingerprint)

    independent_authors = len(author_counts)
    max_author_mentions = max(author_counts.values(), default=0)
    max_fingerprint_mentions = max(fingerprint_counts.values(), default=0)
    top_author_share = (max_author_mentions / total_mentions) if total_mentions else 0.0
    duplicate_text_share = (max_fingerprint_mentions / total_mentions) if total_mentions else 0.0
    repeated_cluster_count = sum(1 for count in fingerprint_counts.values() if count >= 2)
    shill_author_count = sum(
        1 for handle, count in author_counts.items() if count >= 3 and len(author_fingerprints.get(handle, set())) < 2
    )
    effective_authors = min(independent_authors, len(fingerprint_counts)) if total_mentions else 0

    risks = []
    if top_author_share >= TOP_AUTHOR_CONCENTRATION and total_mentions >= 3:
        risks.append("author_concentration_high")
    if duplicate_text_share >= DUPLICATE_TEXT_CLUSTER and total_mentions >= 3:
        risks.append("repeated_text_cluster")
    if shill_author_count > 0:
        risks.append("shill_author_pattern")
    if independent_authors < MIN_HEALTHY_AUTHORS:
        risks.append("thin_author_set")

    if duplicate_text_share >= DUPLICATE_TEXT_CLUSTER and total_mentions >= 3:
        status = "repeated"
    elif top_author_share >= TOP_AUTHOR_CONCENTRATION and total_mentions >= 3:
        status = "concentrated"
    elif shill_author_count > 0:
        status = "shill_risk"
    elif independent_authors < MIN_HEALTHY_AUTHORS:
        status = "thin"
    else:
        status = "healthy"

    reasons = []
    if independent_authors >= MIN_HEALTHY_AUTHORS:
        reasons.append("multi_author")

    return {
        "score": _score_from_risks(risks, total_mentions=total_mentions),
        "status": status,
        "independent_authors": independent_authors,
        "effective_authors": effective_authors,
        "top_author_share": top_author_share,
        "duplicate_text_share": duplicate_text_share,
        "author_entropy": author_entropy(mentions),
        "repeated_cluster_count": repeated_cluster_count,
        "shill_author_count": shill_author_count,
        "top_authors": _top_authors(author_counts, author_followers, author_latest_seen),
        "reasons": reasons,
        "risks": risks,
    }


def _top_authors(
    author_counts: Counter[str],
    author_followers: dict[str, int],
    author_latest_seen: dict[str, int],
) -> list[dict[str, Any]]:
    authors: list[dict[str, Any]] = [
        {
            "handle": handle,
            "count": count,
            "followers": int(author_followers.get(handle) or 0),
        }
        for handle, count in author_counts.items()
    ]
    authors.sort(
        key=lambda item: (
            -int(item["count"]),
            -int(item["followers"]),
            -int(author_latest_seen.get(str(item["handle"])) or 0),
            str(item["handle"]),
        )
    )
    return authors[:20]


def _score_from_risks(risks: list[str], *, total_mentions: int) -> int:
    if total_mentions == 0:
        return 0
    score = 100
    penalties = {
        "author_concentration_high": 30,
        "repeated_text_cluster": 35,
        "shill_author_pattern": 30,
        "thin_author_set": 20,
    }
    for risk in risks:
        score -= penalties.get(risk, 0)
    return max(0, min(100, score))


def _normalize_handle(value: Any) -> str:
    return str(value or "").strip().lstrip("@").lower()
