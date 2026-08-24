"""News-owned observability contract for external-data worker boundaries."""

from __future__ import annotations

from typing import Literal, Protocol

NewsWorkSemantics = Literal["capital_truth", "derived_work", "durable_event", "latest_state"]
NewsExternalDataName = Literal["event_reaction", "instrument_snapshot", "opennews_recovery", "quote_snapshot"]
NewsExternalDataSource = Literal[
    "binance",
    "binance_perp",
    "binance_spot",
    "hyperliquid",
    "opennews",
    "other",
    "us_reference",
]
NewsExternalDataOutcome = Literal["error", "partial", "success"]
NewsExternalDataProviderOutcome = Literal["error", "success"]
NewsExternalDataSkipReason = Literal["coalesced", "disabled", "no_work"]


class NewsExternalDataTelemetryPort(Protocol):
    """Only the measurements News workers need from their process host."""

    def record_external_data_turn(
        self,
        name: NewsExternalDataName,
        outcome: NewsExternalDataOutcome,
        seconds: float,
        *,
        target_count: int | None = None,
        source_count: int | None = None,
        timestamp: float | None = None,
    ) -> None: ...

    def record_external_data_provider_call(
        self,
        name: NewsExternalDataName,
        source: NewsExternalDataSource,
        outcome: NewsExternalDataProviderOutcome,
        seconds: float,
        *,
        byte_count: int | None = None,
    ) -> None: ...

    def record_external_data_skipped(
        self,
        name: NewsExternalDataName,
        reason: NewsExternalDataSkipReason,
    ) -> None: ...


__all__ = [
    "NewsExternalDataName",
    "NewsExternalDataOutcome",
    "NewsExternalDataSource",
    "NewsExternalDataTelemetryPort",
    "NewsWorkSemantics",
]
