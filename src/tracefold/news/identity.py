from __future__ import annotations

import hashlib
import html
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from tracefold.news.models import (
    ARTICLE_IDENTITY_VERSION,
    STORY_IDENTITY_VERSION,
    NewsArticleFact,
    NewsFeedEntry,
    NewsSourceDefinition,
    StoryMatch,
)

_TRACKING_QUERY_KEYS = frozenset(
    {
        "fbclid",
        "gclid",
        "mc_cid",
        "mc_eid",
        "ref",
        "source",
        "utm_campaign",
        "utm_content",
        "utm_medium",
        "utm_source",
        "utm_term",
    }
)
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")
_TOKEN_RE = re.compile(r"[a-z0-9]+|[\u3400-\u9fff]|[\u0400-\u04ff]+|[\u0600-\u06ff]+")
_NUMBER_RE = re.compile(r"(?<!\d)\d+(?:[.,]\d+)?%?(?!\d)")
_HREF_RE = re.compile(r"""href=[\"']([^\"']+)[\"']""", re.IGNORECASE)
_PLAIN_URL_RE = re.compile(r"https?://[^\s<>'\"]+")
_ENTITY_RE = re.compile(r"\b(?:[A-Z][A-Za-z0-9&.-]+(?:\s+[A-Z][A-Za-z0-9&.-]+){0,3}|[A-Z]{2,})\b")
_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_CYRILLIC_RE = re.compile(r"[\u0400-\u04ff]")
_ARABIC_RE = re.compile(r"[\u0600-\u06ff]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_NO_FURTHER_REFRESH_AT_MS = 9_223_372_036_854_775_807
_ACTION_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"raise", "raises", "raised", "increase", "increases", "increased", "hike", "hikes", "hiked"}),
    frozenset({"cut", "cuts", "lower", "lowers", "lowered", "reduce", "reduces", "reduced"}),
    frozenset({"approve", "approves", "approved", "authorize", "authorizes", "authorized"}),
    frozenset({"reject", "rejects", "rejected", "deny", "denies", "denied"}),
    frozenset({"attack", "attacks", "attacked", "strike", "strikes", "struck"}),
    frozenset({"ceasefire", "truce", "peace"}),
    frozenset({"ban", "bans", "banned", "block", "blocks", "blocked"}),
    frozenset({"resume", "resumes", "resumed", "restart", "restarts", "restarted"}),
)
_ACTION_PHRASE_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"加息", "上调", "提高", "增加"}),
    frozenset({"降息", "下调", "降低", "减少"}),
    frozenset({"批准", "授权", "通过"}),
    frozenset({"拒绝", "否决", "驳回"}),
    frozenset({"袭击", "攻击", "空袭", "打击"}),
    frozenset({"停火", "休战", "和平"}),
    frozenset({"禁止", "封禁", "阻止"}),
    frozenset({"恢复", "重启", "重开"}),
)
_SEVERITY_TERMS = frozenset(
    {
        "attack",
        "bankruptcy",
        "ceasefire",
        "collapse",
        "conflict",
        "crisis",
        "default",
        "emergency",
        "explosion",
        "invasion",
        "missile",
        "sanction",
        "tariff",
        "war",
    }
)


@dataclass(frozen=True, slots=True)
class StoryProjection:
    primary_article_id: str
    title: str
    snippet: str
    first_seen_at_ms: int
    last_seen_at_ms: int
    source_count: int
    article_count: int
    trusted_source_count: int
    independent_origin_count: int
    verification_status: str
    phase: str
    importance_score: int
    importance_factors: dict[str, object]
    evidence_set_hash: str
    next_state_refresh_at_ms: int


def normalize_feed_entry(
    *,
    source: NewsSourceDefinition,
    entry: NewsFeedEntry,
    observed_at_ms: int,
) -> NewsArticleFact:
    title = normalize_display_text(entry.title)
    snippet = normalize_display_text(entry.summary)
    canonical_url = normalize_url(entry.link)
    published_at_ms = int(entry.published_at_ms or observed_at_ms)
    identity_method, identity_key = _article_identity(
        source_id=source.source_id,
        canonical_url=canonical_url,
        guid=entry.guid,
        title=title,
        published_at_ms=published_at_ms,
    )
    article_id = "article_" + _sha256(
        f"{ARTICLE_IDENTITY_VERSION}\n{source.source_id}\n{identity_method}\n{identity_key}"
    )[:32]
    origin_url, origin_domain, origin_name, provenance_status = _provenance(
        source=source,
        canonical_url=canonical_url,
        title=title,
        summary=entry.summary,
    )
    language = _entry_language(
        explicit_language=entry.language,
        default_language=source.default_language,
        title=title,
        snippet=snippet,
    )
    content_hash = _sha256(
        _canonical_json(
            {
                "title": title,
                "snippet": snippet,
                "canonical_url": canonical_url,
                "published_at_ms": published_at_ms,
                "language": language,
                "origin_url": origin_url,
                "origin_domain": origin_domain,
                "origin_name": origin_name,
                "provenance_status": provenance_status,
            }
        )
    )
    return NewsArticleFact(
        article_id=article_id,
        source_id=source.source_id,
        identity_method=identity_method,
        identity_key=identity_key,
        source_guid=entry.guid,
        canonical_url=canonical_url,
        title=title,
        snippet=snippet,
        published_at_ms=published_at_ms,
        first_seen_at_ms=observed_at_ms,
        last_seen_at_ms=observed_at_ms,
        language=language,
        origin_url=origin_url,
        origin_domain=origin_domain,
        origin_name=origin_name,
        provenance_status=provenance_status,
        content_hash=content_hash,
        source_entry=dict(entry.raw),
    )


def choose_story(
    article: NewsArticleFact,
    candidates: Sequence[Mapping[str, object]],
) -> StoryMatch | None:
    ranked: list[tuple[float, str, dict[str, object]]] = []
    for candidate in candidates:
        result = story_similarity(
            article_title=article.title,
            article_snippet=article.snippet,
            candidate_title=str(candidate.get("title") or ""),
            candidate_snippet=str(candidate.get("snippet") or ""),
        )
        if not result["accepted"]:
            continue
        story_id = str(candidate["story_id"])
        ranked.append((_required_float(result["score"]), story_id, result))
    if not ranked:
        return None
    score, story_id, reason = max(ranked, key=lambda item: (item[0], item[1]))
    return StoryMatch(
        story_id=story_id,
        match_method="lexical_v1",
        match_score=score,
        reason=reason,
    )


def story_similarity(
    *,
    article_title: str,
    article_snippet: str,
    candidate_title: str,
    candidate_snippet: str,
) -> dict[str, object]:
    left_text = normalize_match_text(f"{article_title} {article_snippet}")
    right_text = normalize_match_text(f"{candidate_title} {candidate_snippet}")
    left_tokens = frozenset(_TOKEN_RE.findall(left_text))
    right_tokens = frozenset(_TOKEN_RE.findall(right_text))
    left_title = normalize_match_text(article_title)
    right_title = normalize_match_text(candidate_title)
    left_title_tokens = frozenset(_TOKEN_RE.findall(left_title))
    right_title_tokens = frozenset(_TOKEN_RE.findall(right_title))
    token_score = _jaccard(left_tokens, right_tokens)
    bigram_score = _jaccard(_bigrams(left_text), _bigrams(right_text))
    char_score = _jaccard(_chargrams(left_text), _chargrams(right_text))
    title_token_score = _jaccard(left_title_tokens, right_title_tokens)
    title_char_score = _jaccard(_chargrams(left_title), _chargrams(right_title))
    left_entities = _entities(article_title)
    right_entities = _entities(candidate_title)
    left_numbers = frozenset(_NUMBER_RE.findall(left_text))
    right_numbers = frozenset(_NUMBER_RE.findall(right_text))
    entity_overlap = _jaccard(left_entities, right_entities)
    number_overlap = _jaccard(left_numbers, right_numbers)
    number_conflict = bool(
        left_numbers
        and right_numbers
        and left_numbers.isdisjoint(right_numbers)
        and token_score >= 0.25
    )
    action_conflict = _action_conflict(left_text, left_tokens, right_text, right_tokens)
    subject_conflict = bool(
        left_entities
        and right_entities
        and left_entities.isdisjoint(right_entities)
        and token_score < 0.35
        and char_score < 0.4
    )
    hard_conflict = number_conflict or action_conflict or subject_conflict
    score = min(
        1.0,
        (0.45 * token_score)
        + (0.20 * bigram_score)
        + (0.25 * char_score)
        + (0.07 * entity_overlap)
        + (0.03 * number_overlap),
    )
    exact_title = left_title == right_title
    structured_title_match = bool(
        title_token_score >= 0.42
        and title_char_score >= 0.38
        and number_overlap > 0
        and _action_overlap(left_title, left_title_tokens, right_title, right_title_tokens)
    )
    accepted = not hard_conflict and (
        exact_title
        or score >= 0.54
        or (token_score >= 0.58 and char_score >= 0.5)
        or structured_title_match
    )
    return {
        "identity_version": STORY_IDENTITY_VERSION,
        "accepted": accepted,
        "score": round(score, 6),
        "token_score": round(token_score, 6),
        "bigram_score": round(bigram_score, 6),
        "char_score": round(char_score, 6),
        "title_token_score": round(title_token_score, 6),
        "title_char_score": round(title_char_score, 6),
        "entity_overlap": round(entity_overlap, 6),
        "number_overlap": round(number_overlap, 6),
        "hard_conflicts": {
            "number": number_conflict,
            "action": action_conflict,
            "subject": subject_conflict,
        },
    }


def project_story(articles: Sequence[Mapping[str, object]], *, now_ms: int) -> StoryProjection:
    if not articles:
        raise ValueError("news_story_articles_required")
    primary = max(
        articles,
        key=lambda row: (
            _trust_rank(str(row["trust_tier"])),
            _required_int(row["published_at_ms"]),
            str(row["article_id"]),
        ),
    )
    sources = {str(row["source_id"]) for row in articles}
    trusted_sources = {
        str(row["source_id"]) for row in articles if str(row["trust_tier"]) in {"authoritative", "trusted"}
    }
    verified_origins = {
        str(row["origin_domain"]).lower()
        for row in articles
        if str(row.get("provenance_status") or "") == "verified" and str(row.get("origin_domain") or "").strip()
    }
    first_seen_at_ms = min(_required_int(row["first_seen_at_ms"]) for row in articles)
    last_seen_at_ms = max(
        max(_required_int(row["published_at_ms"]), _required_int(row["first_seen_at_ms"]))
        for row in articles
    )
    if len(verified_origins) >= 2:
        verification_status = "corroborated"
    elif trusted_sources:
        verification_status = "trusted"
    elif any(str(row.get("provenance_status") or "") == "attributed" for row in articles):
        verification_status = "attributed"
    else:
        verification_status = "unverified"
    phase = story_phase(
        first_seen_at_ms=first_seen_at_ms,
        last_seen_at_ms=last_seen_at_ms,
        article_count=len(articles),
        now_ms=now_ms,
    )
    factors = importance_factors(
        articles=articles,
        independent_origin_count=len(verified_origins),
        last_seen_at_ms=last_seen_at_ms,
        now_ms=now_ms,
    )
    evidence_rows = [
        {
            "article_id": str(row["article_id"]),
            "content_hash": str(row["content_hash"]),
            "source_id": str(row["source_id"]),
            "source_name": str(row["source_name"]),
            "source_role": str(row["source_role"]),
            "trust_tier": str(row["trust_tier"]),
            "source_chain_id": str(row["source_chain_id"]),
            "provenance_status": str(row["provenance_status"]),
            "origin_domain": str(row.get("origin_domain") or ""),
        }
        for row in sorted(articles, key=lambda item: str(item["article_id"]))
    ]
    evidence_set = {
        "identity_version": STORY_IDENTITY_VERSION,
        "primary_article_id": str(primary["article_id"]),
        "first_seen_at_ms": first_seen_at_ms,
        "last_seen_at_ms": last_seen_at_ms,
        "source_count": len(sources),
        "article_count": len(articles),
        "trusted_source_count": len(trusted_sources),
        "independent_origin_count": len(verified_origins),
        "verification_status": verification_status,
        "phase": phase,
        "importance_score": _required_int(factors["score"]),
        "importance_factors": factors,
        "articles": evidence_rows,
    }
    return StoryProjection(
        primary_article_id=str(primary["article_id"]),
        title=str(primary["title"]),
        snippet=str(primary["snippet"]),
        first_seen_at_ms=first_seen_at_ms,
        last_seen_at_ms=last_seen_at_ms,
        source_count=len(sources),
        article_count=len(articles),
        trusted_source_count=len(trusted_sources),
        independent_origin_count=len(verified_origins),
        verification_status=verification_status,
        phase=phase,
        importance_score=_required_int(factors["score"]),
        importance_factors=factors,
        evidence_set_hash=_sha256(_canonical_json(evidence_set)),
        next_state_refresh_at_ms=next_story_state_refresh(
            first_seen_at_ms=first_seen_at_ms,
            last_seen_at_ms=last_seen_at_ms,
            article_count=len(articles),
            now_ms=now_ms,
        ),
    )


def story_phase(*, first_seen_at_ms: int, last_seen_at_ms: int, article_count: int, now_ms: int) -> str:
    age_ms = max(0, now_ms - first_seen_at_ms)
    idle_ms = max(0, now_ms - last_seen_at_ms)
    if idle_ms >= 24 * 60 * 60 * 1000:
        return "fading"
    if age_ms <= 6 * 60 * 60 * 1000:
        return "breaking"
    if article_count >= 2 and age_ms <= 36 * 60 * 60 * 1000:
        return "developing"
    return "sustained"


def next_story_state_refresh(
    *,
    first_seen_at_ms: int,
    last_seen_at_ms: int,
    article_count: int,
    now_ms: int,
) -> int:
    thresholds = {
        first_seen_at_ms + (6 * 60 * 60 * 1000) + 1,
        last_seen_at_ms + (2 * 60 * 60 * 1000) + 1,
        last_seen_at_ms + (8 * 60 * 60 * 1000) + 1,
        last_seen_at_ms + (24 * 60 * 60 * 1000),
        last_seen_at_ms + (24 * 60 * 60 * 1000) + 1,
    }
    if article_count >= 2:
        thresholds.add(first_seen_at_ms + (36 * 60 * 60 * 1000) + 1)
    future = [threshold for threshold in thresholds if threshold > now_ms]
    return min(future) if future else _NO_FURTHER_REFRESH_AT_MS


def importance_factors(
    *,
    articles: Sequence[Mapping[str, object]],
    independent_origin_count: int,
    last_seen_at_ms: int,
    now_ms: int,
) -> dict[str, object]:
    authority = max(_trust_rank(str(row["trust_tier"])) for row in articles) * 10
    corroboration = min(25, max(0, independent_origin_count - 1) * 12)
    age_hours = max(0.0, (now_ms - last_seen_at_ms) / 3_600_000)
    recency = 20 if age_hours <= 2 else 15 if age_hours <= 8 else 10 if age_hours <= 24 else 3
    combined = normalize_match_text(" ".join(f"{row['title']} {row.get('snippet') or ''}" for row in articles))
    severity_hits = sorted(_SEVERITY_TERMS.intersection(_TOKEN_RE.findall(combined)))
    severity = min(15, len(severity_hits) * 5)
    score = min(100, authority + corroboration + recency + severity)
    return {
        "version": "news_story_importance_v1",
        "score": score,
        "authority": authority,
        "independent_corroboration": corroboration,
        "recency": recency,
        "severity": severity,
        "severity_terms": severity_hits,
    }


def story_id_for_anchor(article_id: str) -> str:
    return "story_" + _sha256(f"{STORY_IDENTITY_VERSION}\n{article_id}")[:32]


def normalize_url(value: str | None) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    split = urlsplit(html.unescape(raw))
    if split.scheme.lower() not in {"http", "https"} or not split.hostname:
        return None
    host = split.hostname.lower()
    if split.port and not (
        (split.scheme.lower() == "http" and split.port == 80)
        or (split.scheme.lower() == "https" and split.port == 443)
    ):
        host = f"{host}:{split.port}"
    path = split.path or "/"
    if path != "/":
        path = path.rstrip("/")
    query = urlencode(
        sorted(
            (key, item)
            for key, item in parse_qsl(split.query, keep_blank_values=True)
            if key.lower() not in _TRACKING_QUERY_KEYS
        ),
        doseq=True,
    )
    return urlunsplit((split.scheme.lower(), host, path, query, ""))


def normalize_display_text(value: object) -> str:
    unescaped = html.unescape(str(value or ""))
    without_tags = _TAG_RE.sub(" ", unescaped)
    return _SPACE_RE.sub(" ", without_tags).strip()


def normalize_match_text(value: object) -> str:
    return " ".join(_TOKEN_RE.findall(normalize_display_text(value).lower()))


def _article_identity(
    *,
    source_id: str,
    canonical_url: str | None,
    guid: str | None,
    title: str,
    published_at_ms: int,
) -> tuple[str, str]:
    if canonical_url:
        return "canonical_url", canonical_url
    normalized_guid = str(guid or "").strip()
    if normalized_guid:
        return "source_guid", f"{source_id}:{normalized_guid}"
    time_bucket = published_at_ms // (60 * 60 * 1000)
    return "title_time_bucket", f"{source_id}:{_sha256(normalize_match_text(title))}:{time_bucket}"


def _provenance(
    *,
    source: NewsSourceDefinition,
    canonical_url: str | None,
    title: str,
    summary: str,
) -> tuple[str | None, str | None, str | None, str]:
    if source.source_role == "original_publisher":
        origin_url = canonical_url
        origin_domain = source.source_domain
        return origin_url, origin_domain, source.name, "verified"
    acquisition_domains = {source.source_domain.lower(), "t.me", "telegram.me", "rsshub.app", "localhost"}
    for candidate in _urls_from_text(summary):
        normalized = normalize_url(candidate)
        domain = _url_domain(normalized)
        if normalized and domain and domain not in acquisition_domains and not domain.endswith(".rsshub.app"):
            return normalized, domain, domain, "verified"
    attribution = _attributed_origin_name(f"{title} {normalize_display_text(summary)}")
    if attribution:
        return None, None, attribution, "attributed"
    return None, None, None, "unknown"


def _entry_language(
    *,
    explicit_language: str | None,
    default_language: str,
    title: str,
    snippet: str,
) -> str:
    explicit = _normalize_language_tag(explicit_language)
    if explicit:
        return explicit
    sample = f"{title} {snippet}"
    script_counts = {
        "zh": len(_CJK_RE.findall(sample)),
        "ru": len(_CYRILLIC_RE.findall(sample)),
        "ar": len(_ARABIC_RE.findall(sample)),
        "en": len(_LATIN_RE.findall(sample)),
    }
    language, count = max(script_counts.items(), key=lambda item: (item[1], item[0]))
    if count >= 4:
        return language
    return _normalize_language_tag(default_language) or "en"


def _urls_from_text(value: str) -> tuple[str, ...]:
    candidates = [*_HREF_RE.findall(str(value or "")), *_PLAIN_URL_RE.findall(str(value or ""))]
    return tuple(dict.fromkeys(html.unescape(candidate).rstrip(".,);]") for candidate in candidates))


def _attributed_origin_name(value: str) -> str | None:
    english = re.search(
        r"(?:source|via|according to)\s*[:：-]?\s*([A-Z][A-Za-z0-9 .&-]{1,48})",
        value,
    )
    if english is not None:
        return _SPACE_RE.sub(" ", english.group(1)).strip(" .:-") or None
    chinese = re.search(
        r"(?:来源|消息来自|据)\s*[:：-]?\s*([\u3400-\u9fffA-Za-z0-9 .&-]{2,48})",
        value,
    )
    if chinese is None:
        return None
    normalized = _SPACE_RE.sub(" ", chinese.group(1)).strip(" .:-")
    normalized = re.sub(r"(?:报道|消息|称)$", "", normalized).strip()
    return normalized or None


def _normalize_language_tag(value: object) -> str | None:
    normalized = str(value or "").strip().lower().replace("_", "-")
    if not normalized:
        return None
    primary = normalized.split("-", 1)[0]
    return {
        "chi": "zh",
        "zho": "zh",
        "eng": "en",
        "rus": "ru",
        "ara": "ar",
    }.get(primary, primary)


def _url_domain(value: str | None) -> str | None:
    if not value:
        return None
    hostname = str(urlsplit(value).hostname or "").lower()
    return hostname.removeprefix("www.") or None


def _entities(value: str) -> frozenset[str]:
    return frozenset(normalize_match_text(match.group(0)) for match in _ENTITY_RE.finditer(value) if match.group(0))


def _bigrams(value: str) -> frozenset[str]:
    return frozenset(f"{left}:{right}" for left, right in pairwise(_TOKEN_RE.findall(value)))


def _chargrams(value: str, size: int = 3) -> frozenset[str]:
    compact = value.replace(" ", "")
    if len(compact) <= size:
        return frozenset({compact}) if compact else frozenset()
    return frozenset(compact[index : index + size] for index in range(0, len(compact) - size + 1))


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left.intersection(right)) / len(left.union(right))


def _action_conflict(
    left_text: str,
    left_tokens: frozenset[str],
    right_text: str,
    right_tokens: frozenset[str],
) -> bool:
    left_groups = _action_group_ids(left_text, left_tokens)
    right_groups = _action_group_ids(right_text, right_tokens)
    opposing = ({0, 1}, {2, 3}, {4, 5}, {6, 7})
    return any(
        bool(left_groups.intersection(pair)) and bool(right_groups.intersection(pair)) and left_groups != right_groups
        for pair in opposing
    )


def _action_overlap(
    left_text: str,
    left_tokens: frozenset[str],
    right_text: str,
    right_tokens: frozenset[str],
) -> bool:
    return bool(
        _action_group_ids(left_text, left_tokens).intersection(
            _action_group_ids(right_text, right_tokens)
        )
    )


def _action_group_ids(text: str, tokens: frozenset[str]) -> frozenset[int]:
    compact = text.replace(" ", "")
    return frozenset(
        index
        for index, (token_group, phrase_group) in enumerate(
            zip(_ACTION_GROUPS, _ACTION_PHRASE_GROUPS, strict=True)
        )
        if tokens.intersection(token_group)
        or any(phrase in compact for phrase in phrase_group)
    )


def _trust_rank(value: str) -> int:
    return {
        "authoritative": 4,
        "trusted": 3,
        "standard": 2,
        "low": 1,
    }.get(value, 0)


def _required_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("news_integer_required")
    return value


def _required_float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("news_number_required")
    return float(value)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


__all__ = [
    "StoryProjection",
    "choose_story",
    "importance_factors",
    "next_story_state_refresh",
    "normalize_display_text",
    "normalize_feed_entry",
    "normalize_match_text",
    "normalize_url",
    "project_story",
    "story_id_for_anchor",
    "story_phase",
    "story_similarity",
]
