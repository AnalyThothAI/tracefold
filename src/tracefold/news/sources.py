"""The OpenNews source and deterministic reporting-origin quality tiers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final, cast

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


# Exact public source registry from WorldMonitor commit 0e8785c43e6a693990a14181ae0a16066c15fc8c.
_SOURCE_TIERS_PATH: Final = Path(__file__).with_name("source_tiers.json")
_PINNED_SOURCE_TIERS = cast(dict[str, int], json.loads(_SOURCE_TIERS_PATH.read_text(encoding="utf-8")))
_REPORTING_ORIGIN_TIERS: Final = {source.strip().lower(): int(tier) for source, tier in _PINNED_SOURCE_TIERS.items()}


def reporting_origin_tier(reporting_origin: str, *, fallback_tier: int) -> int:
    """Resolve source quality from the reporting outlet, never the wrapper."""

    normalized = str(reporting_origin or "").strip().lower()
    return int(_REPORTING_ORIGIN_TIERS.get(normalized, fallback_tier))


__all__ = ["OPENNEWS_SOURCE_ID", "opennews_source", "reporting_origin_tier"]
