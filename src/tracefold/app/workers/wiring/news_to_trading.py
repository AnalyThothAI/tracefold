"""The one place a News projection row becomes a Trading candidate row.

`tracefold.news` and `tracefold.trading` are siblings: neither imports the other, and neither reads the
other's tables. Both nonetheless describe the same three facts — an OI telemetry verdict, an editorial
Triage verdict, and a listed instrument — because a capital decision has to be traceable to the exact
judgment that caused it. This module is where the two vocabularies meet, and it is deliberately dull:
every field is named on both sides, nothing is computed, nothing is defaulted, and nothing is dropped.

Field-by-field is the point. A `dict` passed straight through makes a News rename look like a Trading
bug months later; here the same rename fails `mypy` at this seam, next to the comment explaining what
the field is for. `MAPPED_NEWS_PROJECTION_VERSION` covers the other direction — a News change that
keeps every key but changes what one means.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from tracefold.news.storage.trade_projection import (
    LiquidationTradeProjectionRow,
    NewsTradeProjectionRow,
    OiTradeProjectionRow,
    TradeInstrumentProjectionRow,
)
from tracefold.trading.capabilities import ExecutionUniverseCandidateRow
from tracefold.trading.contracts import (
    InstrumentCandidateRow,
    LiquidationCandidateRow,
    NewsCandidateRow,
    OiCandidateRow,
)

# The `NEWS_TRADE_PROJECTION_VERSION` this mapping was written against; `tests/architecture` compares
# them, so a projection bump cannot reach Trading without someone reading these translations again.
MAPPED_NEWS_PROJECTION_VERSION = "news_trade_projection_v8"


def to_oi_candidate_row(row: OiTradeProjectionRow) -> OiCandidateRow:
    """Deterministic telemetry verdict -> the OI scanner's input.

    `venue` stays nullable across the seam. It is the frame's own provider `source`, read through a
    LEFT JOIN, and the OI research keys on it (Hyperliquid +1.35% vs Binance -0.26% at 4 h) — so an
    absent one has to reach the funnel's `other` bucket rather than being defaulted to a venue here.
    """

    return OiCandidateRow(
        event_id=row["event_id"],
        verdict_created_at_ms=row["verdict_created_at_ms"],
        final_decision=row["final_decision"],
        source_rule=row["source_rule"],
        learning_epoch=row["learning_epoch"],
        program_version=row["program_version"],
        program_sha256=row["program_sha256"],
        policy_version=row["policy_version"],
        editorial_origin=row["editorial_origin"],
        editorial_sha256=row["editorial_sha256"],
        scored_judgment_sha256=row["scored_judgment_sha256"],
        runtime_manifest_sha=row["runtime_manifest_sha"],
        metric_version=row["metric_version"],
        source_strategy_id=row["source_strategy_id"],
        source_contract_version=row["source_contract_version"],
        measurement_window_ms=row["measurement_window_ms"],
        symbol=row["symbol"],
        direction=row["direction"],
        oi_change_bps=row["oi_change_bps"],
        oi_value_usd=row["oi_value_usd"],
        whale_long_profit_bps=row["whale_long_profit_bps"],
        whale_oi_ratio_bps=row["whale_oi_ratio_bps"],
        rank_in_window=row["rank_in_window"],
        observed_at_ms=row["observed_at_ms"],
        ingest_mode=row["ingest_mode"],
        venue=row["venue"],
    )


def to_news_candidate_row(row: NewsTradeProjectionRow) -> NewsCandidateRow:
    """Editorial Triage verdict -> the News scanner's input.

    `verdict` and `grounded_assets` cross as the documents they are. Trading reads them with its own
    fail-closed rules — two primaries, an ungrounded primary and an unreadable verdict are each a named
    rejection — and re-shaping them here would move that judgment into the wiring.
    """

    return NewsCandidateRow(
        event_id=row["event_id"],
        verdict_created_at_ms=row["verdict_created_at_ms"],
        final_decision=row["final_decision"],
        evidence_version=row["evidence_version"],
        evidence_sha256=row["evidence_sha256"],
        focus_fact_id=row["focus_fact_id"],
        verdict=row["verdict"],
        learning_epoch=row["learning_epoch"],
        program_version=row["program_version"],
        program_sha256=row["program_sha256"],
        policy_version=row["policy_version"],
        editorial_origin=row["editorial_origin"],
        editorial_sha256=row["editorial_sha256"],
        scored_judgment_sha256=row["scored_judgment_sha256"],
        runtime_manifest_sha=row["runtime_manifest_sha"],
        opened_at_ms=row["opened_at_ms"],
        comparison_fingerprint=row["comparison_fingerprint"],
        asset_class=row["asset_class"],
        grounded_assets=row["grounded_assets"],
        ingest_mode=row["ingest_mode"],
        source_artifact_id=row["source_artifact_id"],
        source_published_at_ms=row["source_published_at_ms"],
    )


def to_liquidation_candidate_row(row: LiquidationTradeProjectionRow) -> LiquidationCandidateRow:
    return LiquidationCandidateRow(
        source_key=row["source_key"],
        item_id=row["item_id"],
        fact_id=row["fact_id"],
        symbol=row["symbol"],
        venue=row["venue"],
        liquidated_position_side=row["liquidated_position_side"],
        forced_order_side=row["forced_order_side"],
        notional_usd=row["notional_usd"],
        quantity=row["quantity"],
        price=row["price"],
        event_at_ms=row["event_at_ms"],
        received_at_ms=row["received_at_ms"],
        parser_version=row["parser_version"],
        provider_record_identity=row["provider_record_identity"],
        symbol_contract_identity=row["symbol_contract_identity"],
        position_side_semantics=row["position_side_semantics"],
        quantity_semantics=row["quantity_semantics"],
        notional_semantics=row["notional_semantics"],
        price_semantics=row["price_semantics"],
        completeness_assumption=row["completeness_assumption"],
        throttle_assumption=row["throttle_assumption"],
        source_contract_version=row["source_contract_version"],
        source_contract_complete=row["source_contract_complete"],
        ingest_mode=row["ingest_mode"],
    )


def to_instrument_candidate_row(row: TradeInstrumentProjectionRow) -> InstrumentCandidateRow:
    """Instrument universe row -> the venue resolver's input, in the reader's venue order."""

    return InstrumentCandidateRow(
        venue=row["venue"],
        venue_symbol=row["venue_symbol"],
        base_symbol=row["base_symbol"],
        instrument_class=row["instrument_class"],
        quote_asset=row["quote_asset"],
        status=row["status"],
        last_seen_ms=row["last_seen_ms"],
    )


def news_trade_candidates(
    repos: Any,
    metric_version: str,
    after_created_at_ms: int,
    until_created_at_ms: int,
) -> tuple[Sequence[OiCandidateRow], Sequence[NewsCandidateRow], Sequence[LiquidationCandidateRow]]:
    """`CandidateProjectionReader`: one point-in-time News read per lane, mapped into Trading's input.

    The window, the ordering and the generation gate belong to the News projection; this reader adds no
    filter of its own, so what Trading scans is exactly what the SQL froze.

    No Trading threshold crosses this seam any more (#264). `max_rank_in_window` and `min_oi_value_usd`
    used to be passed down into News's SELECT, which meant a frame Trading rejected and a frame that was
    never written were the same absence on this side.
    """

    return (
        [
            to_oi_candidate_row(row)
            for row in repos.news.trade_candidate_oi_rows(
                metric_version=metric_version,
                after_created_at_ms=after_created_at_ms,
                until_created_at_ms=until_created_at_ms,
            )
        ],
        [
            to_news_candidate_row(row)
            for row in repos.news.trade_candidate_news_rows(
                after_created_at_ms=after_created_at_ms,
                until_created_at_ms=until_created_at_ms,
            )
        ],
        [
            to_liquidation_candidate_row(row)
            for row in repos.news.trade_candidate_liquidation_rows(
                after_received_at_ms=after_created_at_ms,
                until_received_at_ms=until_created_at_ms,
            )
        ],
    )


def news_trade_instruments(
    repos: Any,
    base_symbol: str,
    venues: Sequence[str],
    *,
    observed_at_ms: int | None = None,
) -> Sequence[InstrumentCandidateRow]:
    """`InstrumentProjectionReader`: News instrument facts, mapped into Trading's venue resolver."""

    return [
        to_instrument_candidate_row(row)
        for row in repos.news.trade_candidate_instrument(
            base_symbol=base_symbol,
            venues=venues,
            observed_at_ms=observed_at_ms,
        )
    ]


def news_execution_instruments(repos: Any) -> list[ExecutionUniverseCandidateRow]:
    """All News-owned Binance instrument facts mapped field by field for one cold refresh."""

    return [
        ExecutionUniverseCandidateRow(
            venue=row["venue"],
            venue_symbol=row["venue_symbol"],
            base_symbol=row["base_symbol"],
            instrument_class=row["instrument_class"],
            quote_asset=row["quote_asset"],
            status=row["status"],
            last_seen_ms=row["last_seen_ms"],
        )
        for row in repos.news.trade_execution_instruments()
    ]


__all__ = [
    "MAPPED_NEWS_PROJECTION_VERSION",
    "news_execution_instruments",
    "news_trade_candidates",
    "news_trade_instruments",
    "to_instrument_candidate_row",
    "to_liquidation_candidate_row",
    "to_news_candidate_row",
    "to_oi_candidate_row",
]
