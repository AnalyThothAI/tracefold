"""The sole field-by-field News OI projection to Trading Source seam."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from tracefold.news.storage.trade_projection import OiTradeProjectionRow
from tracefold.trading.contracts import OiCandidateRow

MAPPED_NEWS_PROJECTION_VERSION = "news_trade_projection_v13"


def to_oi_candidate_row(row: OiTradeProjectionRow) -> OiCandidateRow:
    """Map one persisted News OI fact without adding defaults or execution identity."""

    return OiCandidateRow(
        event_id=row["event_id"],
        source_item_id=row["source_item_id"],
        verdict_created_at_ms=row["verdict_created_at_ms"],
        final_decision=row["final_decision"],
        source_rule=row["source_rule"],
        learning_epoch=row["learning_epoch"],
        program_version=row["program_version"],
        program_sha256=row["program_sha256"],
        policy_version=row["policy_version"],
        judgment_contract_version=row["judgment_contract_version"],
        judgment_origin=row["judgment_origin"],
        judgment_sha256=row["judgment_sha256"],
        runtime_manifest_sha=row["runtime_manifest_sha"],
        metric_version=row["metric_version"],
        source_strategy_id=row["source_strategy_id"],
        source_contract_version=row["source_contract_version"],
        measurement_window_ms=row["measurement_window_ms"],
        provider_symbol=row["provider_symbol"],
        symbol=row["symbol"],
        direction=row["direction"],
        oi_change_bps=row["oi_change_bps"],
        oi_value_usd=row["oi_value_usd"],
        whale_long_profit_bps=row["whale_long_profit_bps"],
        whale_oi_ratio_bps=row["whale_oi_ratio_bps"],
        observed_at_ms=row["observed_at_ms"],
        source_available_at_ms=row["source_available_at_ms"],
        ingest_mode=row["ingest_mode"],
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


__all__ = ["MAPPED_NEWS_PROJECTION_VERSION", "news_oi_sources", "to_oi_candidate_row"]
