from __future__ import annotations

import hashlib
import json
from importlib.resources import files

from tracefold.news.sources import reporting_origin_tier


def test_public_source_tiers_are_the_exact_pinned_worldmonitor_registry() -> None:
    encoded = files("tracefold.news").joinpath("source_tiers.json").read_bytes()
    tiers = json.loads(encoded)

    assert len(tiers) == 343
    assert hashlib.sha256(encoded).hexdigest() == "c7c295f0e4edb55c21c91bf8c7b28847138d2e175805156c697fc7896e94bb2e"
    assert reporting_origin_tier(" Reuters ", fallback_tier=4) == 1
    assert reporting_origin_tier("CBC News", fallback_tier=4) == 1
    assert reporting_origin_tier("Arctic Today", fallback_tier=4) == 2
    assert reporting_origin_tier("unknown outlet", fallback_tier=3) == 3
