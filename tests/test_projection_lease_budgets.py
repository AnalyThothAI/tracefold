from __future__ import annotations

import tracefold.macro.projection as macro_projection
import tracefold.market.radar.microbatch as radar_projection
import tracefold.news.projection as news_projection
from tracefold.market.profiles import profile_projection

_ADMISSION_SECONDS = 1.0
_DB_COMPLETION_GRACE_SECONDS = 0.5
_CPU_COMPLETION_GRACE_SECONDS = 2.0
_CLAIM_DB_SECONDS = 0.5
_STEADY_DB_SECONDS = 3.0
_CPU_SECONDS = 2.0


def _db_stage_seconds(service_seconds: float) -> float:
    return _ADMISSION_SECONDS + service_seconds + _DB_COMPLETION_GRACE_SECONDS


def _cpu_stage_seconds() -> float:
    return _ADMISSION_SECONDS + _CPU_SECONDS + _CPU_COMPLETION_GRACE_SECONDS


def test_projection_leases_cover_the_bounded_sequential_stage_ladders() -> None:
    claim = _db_stage_seconds(_CLAIM_DB_SECONDS)
    radar_worst_seconds = claim + 4 * _db_stage_seconds(_STEADY_DB_SECONDS) + 3 * _cpu_stage_seconds()
    profile_worst_seconds = claim + 2 * _db_stage_seconds(_STEADY_DB_SECONDS) + _cpu_stage_seconds()
    macro_worst_seconds = claim + 2 * _db_stage_seconds(_STEADY_DB_SECONDS) + _cpu_stage_seconds()
    news_identity_worst_seconds = (
        claim
        + 3 * _db_stage_seconds(_STEADY_DB_SECONDS)
        + (3 + news_projection.NEWS_PAIR_BLOCK_COUNT_CAP) * _cpu_stage_seconds()
    )

    assert radar_projection._CLAIM_LEASE_MS == 45_000 > radar_worst_seconds * 1_000
    assert profile_projection._CLAIM_LEASE_MS == 30_000 > profile_worst_seconds * 1_000
    assert macro_projection._CLAIM_LEASE_MS == 30_000 > macro_worst_seconds * 1_000
    assert news_projection._CLAIM_LEASE_MS == 60_000 > news_identity_worst_seconds * 1_000
    assert news_projection.NEWS_PAIR_BLOCK_COUNT_CAP == 4
