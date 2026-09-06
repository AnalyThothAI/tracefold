"""News-owned observability contract for external-data worker boundaries."""

from __future__ import annotations

from typing import Literal, Protocol

NewsWorkSemantics = Literal["capital_truth", "derived_work", "durable_event", "latest_state"]
NewsExternalDataName = Literal[
    "chain_tape",
    "event_reaction",
    "instrument_snapshot",
    "opennews_recovery",
    "quote_snapshot",
]
NewsExternalDataSource = Literal[
    "binance",
    "binance_perp",
    "binance_spot",
    "hyperliquid",
    "okx",
    "opennews",
    "other",
    # The wallet tape's two authorities: the chain's public JSON-RPC, and the site that publishes the
    # tracked-trader list. Two labels because they fail independently and only one of them is the chain.
    "robinhood_rpc",
    "robinhoodtrenches",
    "us_reference",
]
NewsExternalDataOutcome = Literal["error", "partial", "success"]
NewsExternalDataProviderOutcome = Literal["error", "success"]
NewsExternalDataSkipReason = Literal[
    # What the tape read and deliberately did not store: an inbound token nobody asked for, and a
    # movement the receipt could not explain (#572 PR-1).
    "airdrop_ignored",
    "coalesced",
    "disabled",
    "no_work",
    "unclassified",
]
NewsHandoffStage = Literal["event", "verdict"]
NewsHandoffRepairOutcome = Literal["marker_pending", "published", "transient"]
NewsRabbitQueue = Literal["news.deliver", "news.raw", "news.triage"]
NewsRabbitConsumerFatalReason = Literal["handler", "settlement"]
NewsRabbitPublishFailureReason = Literal["backpressure", "confirm_timeout", "transport", "unroutable"]
NewsOpenNewsIncidentCause = Literal[
    "authentication",
    "broker_backpressure",
    "broker_unavailable",
    "idle_timeout",
    "network_connect",
    "planned_shutdown",
    "process_outage",
    "protocol_error",
    "provider_close",
    "triage_circuit_open",
    "unknown",
]
NewsRecoveryOutcome = Literal["budget", "no_work", "partial", "success", "transient"]
NewsRecoveryBudget = Literal["provider_calls", "published_messages", "wall_time"]


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


class NewsDurableEventTelemetryPort(Protocol):
    """Low-cardinality measurements for the durable-event correctness boundaries."""

    def set_news_handoff_state(
        self,
        stage: NewsHandoffStage,
        *,
        pending: int,
        oldest_age_seconds: float,
        expired: int,
    ) -> None: ...

    def record_news_handoff_repair(
        self,
        stage: NewsHandoffStage,
        outcome: NewsHandoffRepairOutcome,
    ) -> None: ...

    def record_news_raw_retention(
        self,
        *,
        deleted_rows: int,
        batches: int,
        wall_seconds: float,
        backlog_rows: int,
        backlog_capped: bool,
        oldest_age_seconds: float,
    ) -> None: ...

    def record_news_rabbitmq_consumer_fatal(
        self,
        queue: str,
        reason_class: NewsRabbitConsumerFatalReason,
    ) -> None: ...

    def record_news_rabbitmq_publish_failure(
        self,
        reason_class: NewsRabbitPublishFailureReason,
    ) -> None: ...

    def set_news_opennews_incident(
        self,
        *,
        provider: Literal["opennews"],
        cause: NewsOpenNewsIncidentCause,
        count: int,
        oldest_age_seconds: float,
    ) -> None: ...

    def record_news_opennews_recovery_turn(
        self,
        outcome: NewsRecoveryOutcome,
        *,
        provider_calls: int,
        published_messages: int,
        exhausted_budget: NewsRecoveryBudget | None = None,
    ) -> None: ...


class NewsTelemetryPort(NewsExternalDataTelemetryPort, NewsDurableEventTelemetryPort, Protocol):
    pass


__all__ = [
    "NewsDurableEventTelemetryPort",
    "NewsExternalDataName",
    "NewsExternalDataOutcome",
    "NewsExternalDataSource",
    "NewsExternalDataTelemetryPort",
    "NewsHandoffRepairOutcome",
    "NewsHandoffStage",
    "NewsOpenNewsIncidentCause",
    "NewsRabbitConsumerFatalReason",
    "NewsRabbitPublishFailureReason",
    "NewsRabbitQueue",
    "NewsRecoveryBudget",
    "NewsRecoveryOutcome",
    "NewsTelemetryPort",
    "NewsWorkSemantics",
]
