from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from tracefold.market.radar.token_radar_rank_source_query import (
    TokenRadarFeatureSourceRequest,
    TokenRadarRankSourceQuery,
)


class RadarProjectionSourceRepository:
    """Bounded material-source reads for incremental Radar projection."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def load_rows_for_requests(
        self,
        requests: Sequence[TokenRadarFeatureSourceRequest],
        *,
        row_cap: int,
    ) -> dict[str, list[dict[str, Any]]]:
        return TokenRadarRankSourceQuery(self.conn).load_rows_for_requests(
            requests,
            row_cap=row_cap,
        )

    def latest_market_context_for_targets(
        self,
        targets: Sequence[Mapping[str, Any]],
    ) -> dict[tuple[str, str], dict[str, Any]]:
        return TokenRadarRankSourceQuery(self.conn).latest_market_context_for_targets(targets)


__all__ = ["RadarProjectionSourceRepository"]
