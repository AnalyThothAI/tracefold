"""Deterministic WorldMonitor threat/category classification."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Final, cast

from .identity import JAVASCRIPT_WHITESPACE_PATTERN
from .models import EventCategory, NewsClassification, ThreatLevel

SEVERITY_VALUES: Final[dict[ThreatLevel, int]] = {
    "critical": 100,
    "high": 75,
    "medium": 50,
    "low": 25,
    "info": 0,
}

CRITICAL_KEYWORDS: Final = {
    "nuclear strike": "military",
    "nuclear attack": "military",
    "nuclear war": "military",
    "invasion": "conflict",
    "declaration of war": "conflict",
    "martial law": "military",
    "coup": "military",
    "coup attempt": "military",
    "genocide": "conflict",
    "ethnic cleansing": "conflict",
    "chemical attack": "terrorism",
    "biological attack": "terrorism",
    "dirty bomb": "terrorism",
    "mass casualty": "conflict",
    "pandemic declared": "health",
    "health emergency": "health",
    "nato article 5": "military",
    "evacuation order": "disaster",
    "meltdown": "disaster",
    "nuclear meltdown": "disaster",
}
HIGH_KEYWORDS: Final = {
    "war": "conflict",
    "armed conflict": "conflict",
    "airstrike": "conflict",
    "air strike": "conflict",
    "drone strike": "conflict",
    "missile": "military",
    "missile launch": "military",
    "troops deployed": "military",
    "military escalation": "military",
    "bombing": "conflict",
    "casualties": "conflict",
    "hostage": "terrorism",
    "terrorist": "terrorism",
    "terror attack": "terrorism",
    "assassination": "crime",
    "cyber attack": "cyber",
    "ransomware": "cyber",
    "data breach": "cyber",
    "sanctions": "economic",
    "embargo": "economic",
    "earthquake": "disaster",
    "tsunami": "disaster",
    "hurricane": "disaster",
    "typhoon": "disaster",
}
MEDIUM_KEYWORDS: Final = {
    "protest": "protest",
    "protests": "protest",
    "riot": "protest",
    "riots": "protest",
    "unrest": "protest",
    "demonstration": "protest",
    "strike action": "protest",
    "military exercise": "military",
    "naval exercise": "military",
    "arms deal": "military",
    "weapons sale": "military",
    "diplomatic crisis": "diplomatic",
    "ambassador recalled": "diplomatic",
    "expel diplomats": "diplomatic",
    "trade war": "economic",
    "tariff": "economic",
    "recession": "economic",
    "inflation": "economic",
    "market crash": "economic",
    "flood": "disaster",
    "flooding": "disaster",
    "wildfire": "disaster",
    "volcano": "disaster",
    "eruption": "disaster",
    "outbreak": "health",
    "epidemic": "health",
    "infection spread": "health",
    "oil spill": "environmental",
    "ceasefire": "diplomatic",
    "pipeline explosion": "infrastructure",
    "blackout": "infrastructure",
    "power outage": "infrastructure",
    "internet outage": "infrastructure",
    "derailment": "infrastructure",
}
LOW_KEYWORDS: Final = {
    "election": "diplomatic",
    "vote": "diplomatic",
    "referendum": "diplomatic",
    "summit": "diplomatic",
    "treaty": "diplomatic",
    "agreement": "diplomatic",
    "negotiation": "diplomatic",
    "talks": "diplomatic",
    "peacekeeping": "diplomatic",
    "humanitarian aid": "diplomatic",
    "peace treaty": "diplomatic",
    "climate change": "environmental",
    "emissions": "environmental",
    "pollution": "environmental",
    "deforestation": "environmental",
    "drought": "environmental",
    "vaccine": "health",
    "vaccination": "health",
    "disease": "health",
    "virus": "health",
    "public health": "health",
    "covid": "health",
    "interest rate": "economic",
    "gdp": "economic",
    "unemployment": "economic",
    "regulation": "economic",
}
EXCLUSIONS: Final = (
    "protein",
    "couples",
    "relationship",
    "dating",
    "diet",
    "fitness",
    "recipe",
    "cooking",
    "shopping",
    "fashion",
    "celebrity",
    "movie",
    "tv show",
    "sports",
    "game",
    "concert",
    "festival",
    "wedding",
    "vacation",
    "travel tips",
    "life hack",
    "self-care",
    "wellness",
)
SHORT_KEYWORDS: Final = {
    "war",
    "coup",
    "ban",
    "vote",
    "riot",
    "riots",
    "hack",
    "talks",
    "ipo",
    "gdp",
    "virus",
    "disease",
    "flood",
}

_HISTORICAL_ANCHORED = re.compile(
    rf"^(?:science history|throwback|flashback){JAVASCRIPT_WHITESPACE_PATTERN}*:?",
    re.I | re.ASCII,
)
_HISTORICAL_BRAND = re.compile(
    rf"^(?:[A-Z][A-Za-z0-9_'&-]*{JAVASCRIPT_WHITESPACE_PATTERN}+){{1,4}}"
    rf"(?:[Tt]hrowback|[Ff]lashback)(?:{JAVASCRIPT_WHITESPACE_PATTERN}+[A-Za-z]+)?"
    rf"{JAVASCRIPT_WHITESPACE_PATTERN}*:",
    re.ASCII,
)
_HISTORICAL_YEAR_PREFIX = re.compile(
    rf"^on this day in{JAVASCRIPT_WHITESPACE_PATTERN}+(?:19|20)[0-9]{{2}}(?![A-Za-z0-9_])",
    re.I | re.ASCII,
)
_THIS_DAY_HISTORY = re.compile(r"^this day in history(?![A-Za-z0-9_])", re.I | re.ASCII)
_HISTORICAL_PHRASE = re.compile(
    rf"(?<![A-Za-z0-9_])(?:[0-9]+{JAVASCRIPT_WHITESPACE_PATTERN}+(?:years?|decades?|months?)"
    rf"{JAVASCRIPT_WHITESPACE_PATTERN}+(?:ago|after|later)|anniversary|"
    rf"in memoriam|remembering|remembered|commemorat(?:e|es|ed|ion)|retrospective)"
    rf"(?![A-Za-z0-9_])",
    re.I | re.ASCII,
)
_FULL_DATE = re.compile(
    rf"(?<![A-Za-z0-9_])(?:january|february|march|april|may|june|july|august|september|october|"
    rf"november|december){JAVASCRIPT_WHITESPACE_PATTERN}+[0-9]{{1,2}},?"
    rf"{JAVASCRIPT_WHITESPACE_PATTERN}+((?:19|20)[0-9]{{2}})(?![A-Za-z0-9_])",
    re.I | re.ASCII,
)
_ISO_DATE = re.compile(r"(?<![A-Za-z0-9_])((?:19|20)[0-9]{2})-[0-9]{1,2}-[0-9]{1,2}(?![A-Za-z0-9_])")


def has_historical_marker(title: str, *, now_ms: int | None = None) -> bool:
    if any(
        pattern.search(title)
        for pattern in (
            _HISTORICAL_ANCHORED,
            _HISTORICAL_BRAND,
            _HISTORICAL_YEAR_PREFIX,
            _THIS_DAY_HISTORY,
            _HISTORICAL_PHRASE,
        )
    ):
        return True
    current_year = datetime.fromtimestamp(
        (now_ms / 1000) if now_ms is not None else datetime.now(UTC).timestamp(),
        tz=UTC,
    ).year
    for pattern in (_FULL_DATE, _ISO_DATE):
        match = pattern.search(title)
        if match and int(match.group(1)) < current_year - 1:
            return True
    return False


def _matches(text: str, keyword: str) -> bool:
    if keyword in SHORT_KEYWORDS:
        start = text.find(keyword)
        while start >= 0:
            end = start + len(keyword)
            before_is_word = start > 0 and _is_javascript_word_char(text[start - 1])
            after_is_word = end < len(text) and _is_javascript_word_char(text[end])
            if not before_is_word and not after_is_word:
                return True
            start = text.find(keyword, start + 1)
        return False
    return keyword in text


def _is_javascript_word_char(char: str) -> bool:
    return char == "_" or "a" <= char <= "z" or "A" <= char <= "Z" or "0" <= char <= "9"


def _first_match(
    title: str,
    keywords: dict[str, str],
) -> tuple[str, EventCategory] | None:
    for keyword, category in keywords.items():
        if _matches(title, keyword):
            return keyword, category  # type: ignore[return-value]
    return None


def classify_by_keyword(title: str, *, now_ms: int | None = None) -> NewsClassification:
    lowered = title.lower()
    if any(exclusion in lowered for exclusion in EXCLUSIONS):
        return NewsClassification(level="info", category="general", confidence=0.3, source="keyword")
    retrospective = has_historical_marker(title, now_ms=now_ms)
    for keywords, level, confidence in (
        (CRITICAL_KEYWORDS, "critical", 0.9),
        (HIGH_KEYWORDS, "high", 0.8),
        (MEDIUM_KEYWORDS, "medium", 0.7),
        (LOW_KEYWORDS, "low", 0.6),
    ):
        matched = _first_match(lowered, keywords)
        if matched is None:
            continue
        if retrospective and level in {"critical", "high"}:
            return NewsClassification(
                level="info",
                category="general",
                confidence=0.85,
                source="keyword-historical-downgrade",
            )
        return NewsClassification(
            level=cast(ThreatLevel, level),
            category=matched[1],
            confidence=confidence,
            source="keyword",
        )
    return NewsClassification(level="info", category="general", confidence=0.3, source="keyword")


__all__ = [
    "SEVERITY_VALUES",
    "classify_by_keyword",
    "has_historical_marker",
]
