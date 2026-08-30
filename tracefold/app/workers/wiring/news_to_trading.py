"""The one place a News projection row becomes a Trading candidate row.

`tracefold.news` and `tracefold.trading` are siblings: neither imports the other, and neither reads the
other's tables. Both nonetheless describe the same two facts — a deterministic OI telemetry frame and a
listed instrument — because a capital decision has to be traceable to the exact measurement that caused
it. This module is where the two vocabularies meet, and it is deliberately dull: every field is named
on both sides, nothing is computed, nothing is defaulted, and nothing is dropped.

**One live trigger crosses this seam (#331).** The editorial-verdict and liquidation mappers are gone
with the projections behind them: editorial News no longer triggers automatic capital, and there is no
online liquidation consumer. What is left is the OI frame the lane triggers on and the catalogue rows
the *research* replay resolves. Execution capabilities compile only from Trading's complete
provider-native catalogues.

Field-by-field is the point. A `dict` passed straight through makes a News rename look like a Trading
bug months later; here the same rename fails `mypy` at this seam, next to the comment explaining what
the field is for. `MAPPED_NEWS_PROJECTION_VERSION` covers the other direction — a News change that
keeps every key but changes what one means.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from tracefold.news.storage.trade_projection import (
    OiTradeProjectionRow,
    TradeInstrumentProjectionRow,
)
from tracefold.trading.contracts import (
    InstrumentCandidateRow,
    OiCandidateRow,
)

# The `NEWS_TRADE_PROJECTION_VERSION` this mapping was written against; `tests/architecture` compares
# them, so a projection bump cannot reach Trading without someone reading these translations again.
MAPPED_NEWS_PROJECTION_VERSION = "news_trade_projection_v11"


def to_oi_candidate_row(row: OiTradeProjectionRow) -> OiCandidateRow:
    """Deterministic telemetry verdict -> the OI scanner's input.

    `venue` stays nullable across the seam. It is the frame's own provider `source`, read through a
    LEFT JOIN, and the OI research keys on it (Hyperliquid +1.35% vs Binance -0.26% at 4 h) — so an
    absent one has to reach the funnel's `other` bucket rather than being defaulted to a venue here.
    """

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
        symbol=row["symbol"],
        direction=row["direction"],
        oi_change_bps=row["oi_change_bps"],
        oi_value_usd=row["oi_value_usd"],
        whale_long_profit_bps=row["whale_long_profit_bps"],
        whale_oi_ratio_bps=row["whale_oi_ratio_bps"],
        rank_in_window=row["rank_in_window"],
        observed_at_ms=row["observed_at_ms"],
        source_available_at_ms=row["source_available_at_ms"],
        ingest_mode=row["ingest_mode"],
        venue=row["venue"],
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


def news_oi_sources(
    repos: Any,
    metric_version: str,
    after_created_at_ms: int,
    until_created_at_ms: int,
) -> Sequence[OiCandidateRow]:
    """`OiProjectionReader`: one point-in-time News read, mapped into the capital lane's input.

    The window, the ordering and the generation gate belong to the News projection; this reader adds no
    filter of its own, so what the lane scans is exactly what the SQL froze. No Trading threshold
    crosses this seam (#264): a frame the lane rejects and a frame that was never written must not be
    the same absence on this side.
    """

    return [
        to_oi_candidate_row(row)
        for row in repos.news.trade_candidate_oi_rows(
            metric_version=metric_version,
            after_created_at_ms=after_created_at_ms,
            until_created_at_ms=until_created_at_ms,
        )
    ]


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


__all__ = [
    "MAPPED_NEWS_PROJECTION_VERSION",
    "news_oi_sources",
    "news_trade_instruments",
    "to_instrument_candidate_row",
    "to_oi_candidate_row",
]
