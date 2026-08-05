"""The OpenNews source and deterministic reporting-origin quality tiers."""

from __future__ import annotations

from typing import Final

from .models import NewsSourceDefinition

OPENNEWS_SOURCE_ID: Final = "news-opennews"


def opennews_source() -> NewsSourceDefinition:
    """Return the one code-owned News acquisition source."""

    return NewsSourceDefinition(
        source_id=OPENNEWS_SOURCE_ID,
        name="OpenNews",
        tier=4,
        lang="en",
    )


# Frozen from the retired WorldMonitor/RSS inventory. These names still score
# persisted OpenNews reporting origins exactly as before; acquisition URLs and
# category memberships are deliberately not retained.
_REPORTING_ORIGIN_TIERS: Final = {
    "6551news": 2,
    "ai news": 4,
    "al jazeera": 2,
    "ap": 1,
    "ap news": 1,
    "ars technica": 3,
    "arxiv ai": 4,
    "associated press": 1,
    "axios": 2,
    "bbc": 2,
    "bbc world": 2,
    "bellingcat": 3,
    "bitcoin magazine": 3,
    "bloomberg": 1,
    "bloomberg crypto": 1,
    "breaking defense": 3,
    "cisa": 1,
    "cnbc": 2,
    "coindesk": 3,
    "cointelegraph": 3,
    "crisiswatch": 3,
    "cryptoslate": 3,
    "decrypt": 3,
    "defense news": 3,
    "defense one": 3,
    "defi news": 4,
    "dfrlab": 2,
    "dl news": 3,
    "doj": 2,
    "federal reserve": 3,
    "financial times": 2,
    "ft": 2,
    "gcaptain": 3,
    "hacker news": 4,
    "iaea": 1,
    "krebs security": 3,
    "layoffs news": 4,
    "layoffs.fyi": 3,
    "marketwatch": 2,
    "messari": 3,
    "military times": 2,
    "mit tech review": 3,
    "nikkei asia": 2,
    "nuclear energy": 4,
    "oil & gas": 4,
    "oryx osint": 2,
    "pentagon": 1,
    "politico": 2,
    "reuters": 1,
    "reuters business": 1,
    "reuters crypto": 1,
    "reuters energy": 4,
    "reuters us": 1,
    "reuters world": 1,
    "sec": 3,
    "south china morning post": 4,
    "stablecoin policy": 4,
    "state dept": 1,
    "task & purpose": 3,
    "techcrunch layoffs": 4,
    "the block": 3,
    "the defiant": 3,
    "the verge": 4,
    "the verge ai": 4,
    "the war zone": 3,
    "treasury": 2,
    "trump - truth social": 1,
    "un news": 1,
    "unchained": 3,
    "usni news": 2,
    "venturebeat ai": 4,
    "wall street journal": 1,
    "wallstengine": 4,
    "white house": 1,
    "white house actions": 1,
    "who": 1,
    "wsj": 1,
    "wu blockchain": 3,
    "xinhua": 3,
}


def reporting_origin_tier(reporting_origin: str, *, fallback_tier: int) -> int:
    """Resolve source quality from the reporting outlet, never the wrapper."""

    normalized = str(reporting_origin or "").strip().lower()
    return int(_REPORTING_ORIGIN_TIERS.get(normalized, fallback_tier))


__all__ = ["OPENNEWS_SOURCE_ID", "opennews_source", "reporting_origin_tier"]
