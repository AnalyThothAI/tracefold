"""Primary OpenNews identity plus pinned public WorldMonitor corroboration feeds."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Final, cast

from .models import NewsSourceDefinition

WORLDMONITOR_PUBLIC_SOURCE_COMMIT: Final = "0e8785c43e6a693990a14181ae0a16066c15fc8c"
WORLDMONITOR_PUBLIC_SOURCE_CATALOG_SHA256: Final = "483b68c3cab85bba0ef7258476dcf89d1439687c36449e46e4432b2e7125a979"
OPENNEWS_SOURCE_ID: Final = "news-opennews"

_CATALOG_PATH: Final = Path(__file__).with_name("worldmonitor_public_sources.json")
_SOURCE_TIERS_PATH: Final = Path(__file__).with_name("source_tiers.json")
_EXPECTED_COUNTS: Final = {
    "physical_feeds": 179,
    "category_memberships": 183,
    "reporting_source_names": 178,
    "categories": 17,
}
_CATEGORY_ORDER: Final = (
    "politics",
    "us",
    "europe",
    "middleeast",
    "tech",
    "ai",
    "finance",
    "commodities",
    "gov",
    "africa",
    "latam",
    "asia",
    "energy",
    "thinktanks",
    "crisis",
    "layoffs",
    "intel",
)
_DUPLICATE_MEMBERSHIP_POSITIONS: Final = {
    ("energy", "Oil & Gas"): 0,
    ("intel", "Foreign Policy"): 9,
    ("intel", "Foreign Affairs"): 10,
    ("intel", "Atlantic Council"): 11,
}

_PINNED_SOURCE_TIERS = cast(dict[str, int], json.loads(_SOURCE_TIERS_PATH.read_text(encoding="utf-8")))
_REPORTING_ORIGIN_TIERS: Final = {source.strip().lower(): int(tier) for source, tier in _PINNED_SOURCE_TIERS.items()}


def reporting_origin_tier(reporting_origin: str, *, fallback_tier: int) -> int:
    """Resolve source quality from the reporting outlet, never the wrapper."""

    normalized = str(reporting_origin or "").strip().lower()
    return int(_REPORTING_ORIGIN_TIERS.get(normalized, fallback_tier))


def public_rss_sources() -> tuple[NewsSourceDefinition, ...]:
    """Return the frozen public ``full/en + INTEL`` physical feed catalog."""

    catalog = _load_public_catalog()
    return tuple(
        NewsSourceDefinition(
            source_id=_rss_source_id(str(row["url"])),
            name=str(row["name"]),
            tier=reporting_origin_tier(str(row["name"]), fallback_tier=4),
            lang=str(row["lang"]),
            source_kind="rss",
            feed_url=str(row["url"]),
            memberships=tuple(str(category) for category in cast(list[object], row["memberships"])),
            refresh_interval_seconds=1800,
        )
        for row in cast(list[dict[str, Any]], catalog["sources"])
    )


def public_rss_membership_sources() -> tuple[tuple[str, NewsSourceDefinition], ...]:
    """Return the pinned category-major feed population order."""

    sources = public_rss_sources()
    ordered: list[tuple[str, NewsSourceDefinition]] = []
    for category in _CATEGORY_ORDER:
        category_sources = [source for source in sources if category in source.memberships]
        positioned = sorted(
            (
                (position, name, source)
                for (membership, name), position in _DUPLICATE_MEMBERSHIP_POSITIONS.items()
                if membership == category
                for source in category_sources
                if source.name == name
            ),
            key=lambda value: value[0],
        )
        if positioned:
            positioned_ids = {source.source_id for _, _, source in positioned}
            category_sources = [source for source in category_sources if source.source_id not in positioned_ids]
            for position, _name, source in positioned:
                category_sources.insert(position, source)
        ordered.extend((category, source) for source in category_sources)
    if len(ordered) != _EXPECTED_COUNTS["category_memberships"]:
        raise RuntimeError("worldmonitor_public_source_membership_order_mismatch")
    return tuple(ordered)


def opennews_source() -> NewsSourceDefinition:
    """Return the primary low-latency public News acquisition source."""

    return NewsSourceDefinition(
        source_id=OPENNEWS_SOURCE_ID,
        name="OpenNews",
        tier=4,
        lang="en",
        source_kind="opennews",
    )


def _load_public_catalog() -> dict[str, Any]:
    encoded = _CATALOG_PATH.read_bytes()
    if hashlib.sha256(encoded).hexdigest() != WORLDMONITOR_PUBLIC_SOURCE_CATALOG_SHA256:
        raise RuntimeError("worldmonitor_public_source_catalog_hash_mismatch")
    value = json.loads(encoded)
    if not isinstance(value, dict):
        raise RuntimeError("worldmonitor_public_source_catalog_invalid")
    if value.get("authority_commit") != WORLDMONITOR_PUBLIC_SOURCE_COMMIT:
        raise RuntimeError("worldmonitor_public_source_catalog_commit_mismatch")
    if value.get("variant") != "full" or value.get("language") != "en":
        raise RuntimeError("worldmonitor_public_source_catalog_scope_mismatch")
    if value.get("counts") != _EXPECTED_COUNTS or not isinstance(value.get("sources"), list):
        raise RuntimeError("worldmonitor_public_source_catalog_counts_mismatch")
    return cast(dict[str, Any], value)


def _rss_source_id(url: str) -> str:
    return f"news-rss-{hashlib.sha256(url.encode()).hexdigest()[:24]}"


__all__ = [
    "OPENNEWS_SOURCE_ID",
    "WORLDMONITOR_PUBLIC_SOURCE_CATALOG_SHA256",
    "WORLDMONITOR_PUBLIC_SOURCE_COMMIT",
    "opennews_source",
    "public_rss_membership_sources",
    "public_rss_sources",
    "reporting_origin_tier",
]
