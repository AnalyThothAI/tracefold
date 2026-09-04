"""The sole field-by-field News OI projection to Trading Source seam."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from tracefold.news.storage.trade_projection import OiTradeProjectionRow
from tracefold.trading.contracts import OiCandidateRow


def to_oi_candidate_row(row: OiTradeProjectionRow) -> OiCandidateRow:
    """Map one persisted News OI fact without adding defaults or execution identity.

    Sixteen keys either side and the same sixteen names: the two contracts are independent, so the map
    stays explicit rather than a `dict(row)`, and a rename on either side fails here at type-check time.
    """

    return OiCandidateRow(
        event_id=row["event_id"],
        metric_version=row["metric_version"],
        source_item_id=row["source_item_id"],
        symbol=row["symbol"],
        direction=row["direction"],
        oi_change_bps=row["oi_change_bps"],
        oi_value_usd=row["oi_value_usd"],
        whale_long_profit_bps=row["whale_long_profit_bps"],
        whale_oi_ratio_bps=row["whale_oi_ratio_bps"],
        observed_at_ms=row["observed_at_ms"],
        available_at_ms=row["available_at_ms"],
        ingest_mode=row["ingest_mode"],
        source_strategy_id=row["source_strategy_id"],
        source_contract_version=row["source_contract_version"],
        measurement_window_ms=row["measurement_window_ms"],
        venue=row["venue"],
    )


def news_oi_sources(
    repos: Any,
    metric_version: str,
    after_created_at_ms: int,
    until_created_at_ms: int,
) -> Sequence[OiCandidateRow]:
    """Read News-owned rows and cross the sibling boundary once at App composition."""

    return [
        to_oi_candidate_row(row)
        for row in repos.news.trade_candidate_oi_rows(
            metric_version=metric_version,
            after_created_at_ms=after_created_at_ms,
            until_created_at_ms=until_created_at_ms,
        )
    ]


__all__ = ["news_oi_sources", "to_oi_candidate_row"]
