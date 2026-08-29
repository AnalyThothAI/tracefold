from __future__ import annotations

import time
from typing import Any, Final, Literal, get_args

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram, generate_latest

PROMETHEUS_CONTENT_TYPE = CONTENT_TYPE_LATEST

ExternalDataName = Literal[
    "event_reaction",
    "instrument_snapshot",
    "opennews_recovery",
    "quote_snapshot",
    "trading_capital_lane",
    "trading_reconcile",
    "trading_venue_catalog",
]
ExternalDataSource = Literal[
    "binance",
    "binance_perp",
    "binance_spot",
    "hyperliquid",
    "okx",
    "model",
    "opennews",
    "other",
    "us_reference",
]
ExternalDataOutcome = Literal["error", "partial", "success"]
ExternalDataProviderOutcome = Literal["error", "success"]
ExternalDataSkipReason = Literal["coalesced", "disabled", "no_work"]
NewsSearchMode = Literal["asset", "text"]
NewsSearchResult = Literal["zero", "nonzero"]

_EXTERNAL_DATA_NAMES: Final[frozenset[str]] = frozenset(get_args(ExternalDataName))
_EXTERNAL_DATA_SOURCES: Final[frozenset[str]] = frozenset(get_args(ExternalDataSource))
_EXTERNAL_DATA_OUTCOMES: Final[frozenset[str]] = frozenset(get_args(ExternalDataOutcome))
_EXTERNAL_DATA_PROVIDER_OUTCOMES: Final[frozenset[str]] = frozenset(get_args(ExternalDataProviderOutcome))
_EXTERNAL_DATA_SKIP_REASONS: Final[frozenset[str]] = frozenset(get_args(ExternalDataSkipReason))
_NEWS_SEARCH_MODES: Final[frozenset[str]] = frozenset(get_args(NewsSearchMode))
_NEWS_SEARCH_RESULTS: Final[frozenset[str]] = frozenset(get_args(NewsSearchResult))


class TelemetryRegistry:
    def __init__(self, *, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry(auto_describe=True)
        self.processing_seconds = Histogram(
            "tracefold_worker_processing_seconds",
            "Worker processing duration in seconds.",
            ("worker",),
            buckets=(0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1.0, 2.0, 5.0, 8.0, 12.0),
            registry=self.registry,
        )
        self.jobs_total = Counter(
            "tracefold_worker_jobs_total",
            "Worker jobs by terminal status.",
            ("worker", "status"),
            registry=self.registry,
        )
        self.last_run_timestamp = Gauge(
            "tracefold_worker_last_run_timestamp_seconds",
            "Unix timestamp of the last worker run.",
            ("worker",),
            registry=self.registry,
        )
        self.lag_seconds = Gauge(
            "tracefold_worker_lag_seconds",
            "Worker lag in seconds.",
            ("worker",),
            registry=self.registry,
        )
        self.pool_wait_ms = Histogram(
            "tracefold_db_pool_wait_ms",
            "Database pool checkout wait in milliseconds.",
            ("pool",),
            registry=self.registry,
        )
        self.queue_depth = Gauge(
            "tracefold_worker_queue_depth",
            "Worker queue depth.",
            ("worker", "queue", "status"),
            registry=self.registry,
        )
        self.transaction_seconds = Histogram(
            "tracefold_worker_transaction_seconds",
            "Duration of explicit worker database transactions in seconds.",
            ("worker",),
            registry=self.registry,
        )
        self.projection_rows = Gauge(
            "tracefold_worker_projection_rows",
            "Rows observed at bounded projection stages.",
            ("worker", "stage"),
            registry=self.registry,
        )
        self.projection_bytes = Gauge(
            "tracefold_worker_projection_bytes",
            "Bytes observed at bounded projection boundaries.",
            ("worker", "direction"),
            registry=self.registry,
        )
        self.projection_cache_total = Counter(
            "tracefold_worker_projection_cache_total",
            "Change-driven projection cache outcomes.",
            ("worker", "outcome"),
            registry=self.registry,
        )
        self.news_story_projection_value = Gauge(
            "tracefold_news_story_projection_value",
            "Aggregate, content-free diagnostics for the current News Story projection.",
            ("measure",),
            registry=self.registry,
        )
        self.news_search_requests = Counter(
            "tracefold_news_search_requests",
            "Successful first-page News feed searches by deterministic mode and result shape.",
            ("mode", "result"),
            registry=self.registry,
        )
        self.news_search_duration_seconds = Histogram(
            "tracefold_news_search_duration_seconds",
            "Successful first-page News feed search request duration in seconds.",
            ("mode",),
            buckets=(0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1.0),
            registry=self.registry,
        )
        self.queue_oldest_delay_seconds = Gauge(
            "tracefold_worker_queue_oldest_delay_seconds",
            "Age of the oldest due queue item.",
            ("worker", "queue"),
            registry=self.registry,
        )
        self.resource_admission_seconds = Histogram(
            "tracefold_worker_resource_admission_seconds",
            "Time spent waiting for a bounded Workers capability before submission.",
            ("capability", "operation", "outcome"),
            registry=self.registry,
        )
        self.resource_service_seconds = Histogram(
            "tracefold_worker_resource_service_seconds",
            "Underlying future service time for a bounded Workers capability.",
            ("capability", "operation", "outcome"),
            registry=self.registry,
        )
        self.resource_active = Gauge(
            "tracefold_worker_resource_active",
            "Underlying futures currently occupying a bounded Workers capability.",
            ("capability",),
            registry=self.registry,
        )
        self.external_data_turn_duration_seconds = Histogram(
            "tracefold_external_data_turn_duration_seconds",
            "External-data turn duration in seconds.",
            ("name",),
            buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0),
            registry=self.registry,
        )
        self.external_data_turn_total = Counter(
            "tracefold_external_data_turn_total",
            "External-data turns by bounded outcome.",
            ("name", "outcome"),
            registry=self.registry,
        )
        self.external_data_target_count = Gauge(
            "tracefold_external_data_target_count",
            "Targets observed by the latest external-data turn.",
            ("name",),
            registry=self.registry,
        )
        self.external_data_source_count = Gauge(
            "tracefold_external_data_source_count",
            "Source groups observed by the latest external-data turn.",
            ("name",),
            registry=self.registry,
        )
        self.external_data_last_success_age_seconds = Gauge(
            "tracefold_external_data_last_success_age_seconds",
            "Age of the last fully successful external-data turn in seconds.",
            ("name",),
            registry=self.registry,
        )
        self._external_data_last_success_timestamps: dict[str, float] = {}
        self.external_data_provider_call_duration_seconds = Histogram(
            "tracefold_external_data_provider_call_duration_seconds",
            "External provider call duration in seconds.",
            ("name", "source"),
            buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 8.0, 20.0, 30.0),
            registry=self.registry,
        )
        self.external_data_provider_call_total = Counter(
            "tracefold_external_data_provider_call_total",
            "External provider calls by bounded outcome.",
            ("name", "source", "outcome"),
            registry=self.registry,
        )
        self.external_data_provider_bytes_total = Counter(
            "tracefold_external_data_provider_bytes_total",
            "Provider response bytes where an adapter exposes that value.",
            ("name", "source"),
            registry=self.registry,
        )
        self.external_data_skipped_or_coalesced_total = Counter(
            "tracefold_external_data_skipped_or_coalesced_total",
            "External-data work deliberately skipped or coalesced.",
            ("name", "reason"),
            registry=self.registry,
        )
        for capability in (
            "database_business",
            "database_control",
            "finite_operation",
        ):
            self.resource_active.labels(capability=capability).set(0)

    def record_processing_seconds(self, worker: str, seconds: float) -> None:
        self.processing_seconds.labels(worker=_label(worker)).observe(max(0.0, float(seconds)))

    def record_job(self, worker: str, status: str, count: int = 1) -> None:
        self.jobs_total.labels(worker=_label(worker), status=_label(status)).inc(max(0, int(count)))

    def mark_last_run(self, worker: str, *, timestamp: float | None = None) -> None:
        resolved_timestamp = float(timestamp if timestamp is not None else time.time())
        self.last_run_timestamp.labels(worker=_label(worker)).set(resolved_timestamp)

    def set_lag_seconds(self, worker: str, seconds: float) -> None:
        self.lag_seconds.labels(worker=_label(worker)).set(max(0.0, float(seconds)))

    def record_pool_wait(self, pool: str, wait_ms: float) -> None:
        pool_label = _label(pool)
        normalized_wait_ms = max(0.0, float(wait_ms))
        self.pool_wait_ms.labels(pool=pool_label).observe(normalized_wait_ms)

    def set_queue_depth(self, worker: str, queue: str, status: str, depth: int) -> None:
        self.queue_depth.labels(
            worker=_label(worker),
            queue=_label(queue),
            status=_label(status),
        ).set(max(0, int(depth)))

    def record_transaction_seconds(self, worker: str, seconds: float) -> None:
        self.transaction_seconds.labels(worker=_label(worker)).observe(max(0.0, float(seconds)))

    def set_projection_rows(self, worker: str, stage: str, rows: int) -> None:
        self.projection_rows.labels(
            worker=_label(worker),
            stage=_label(stage),
        ).set(max(0, int(rows)))

    def set_projection_bytes(self, worker: str, direction: str, byte_count: int) -> None:
        self.projection_bytes.labels(
            worker=_label(worker),
            direction=_label(direction),
        ).set(max(0, int(byte_count)))

    def record_projection_cache(self, worker: str, outcome: str) -> None:
        self.projection_cache_total.labels(
            worker=_label(worker),
            outcome=_label(outcome),
        ).inc()

    def set_news_story_projection_value(self, measure: str, value: int) -> None:
        self.news_story_projection_value.labels(measure=_label(measure)).set(max(0, int(value)))

    def record_news_search(
        self,
        mode: NewsSearchMode,
        *,
        result: NewsSearchResult,
        seconds: float,
    ) -> None:
        mode_label = _bounded_label(mode, allowed=_NEWS_SEARCH_MODES, field="news_search_mode")
        result_label = _bounded_label(result, allowed=_NEWS_SEARCH_RESULTS, field="news_search_result")
        self.news_search_requests.labels(mode=mode_label, result=result_label).inc()
        self.news_search_duration_seconds.labels(mode=mode_label).observe(max(0.0, float(seconds)))

    def set_queue_oldest_delay_seconds(self, worker: str, queue: str, seconds: float) -> None:
        self.queue_oldest_delay_seconds.labels(
            worker=_label(worker),
            queue=_label(queue),
        ).set(max(0.0, float(seconds)))

    def record_resource_admission(
        self,
        capability: str,
        operation: str,
        outcome: str,
        seconds: float,
    ) -> None:
        self.resource_admission_seconds.labels(
            capability=_label(capability),
            operation=_label(operation),
            outcome=_label(outcome),
        ).observe(max(0.0, float(seconds)))

    def record_resource_service(
        self,
        capability: str,
        operation: str,
        outcome: str,
        seconds: float,
    ) -> None:
        self.resource_service_seconds.labels(
            capability=_label(capability),
            operation=_label(operation),
            outcome=_label(outcome),
        ).observe(max(0.0, float(seconds)))

    def change_resource_active(self, capability: str, delta: int) -> None:
        self.resource_active.labels(capability=_label(capability)).inc(int(delta))

    def record_external_data_turn(
        self,
        name: ExternalDataName,
        outcome: ExternalDataOutcome,
        seconds: float,
        *,
        target_count: int | None = None,
        source_count: int | None = None,
        timestamp: float | None = None,
    ) -> None:
        name_label = _bounded_label(name, allowed=_EXTERNAL_DATA_NAMES, field="external_data_name")
        outcome_label = _bounded_label(outcome, allowed=_EXTERNAL_DATA_OUTCOMES, field="external_data_outcome")
        self.external_data_turn_duration_seconds.labels(name=name_label).observe(max(0.0, float(seconds)))
        self.external_data_turn_total.labels(name=name_label, outcome=outcome_label).inc()
        if target_count is not None:
            self.external_data_target_count.labels(name=name_label).set(max(0, int(target_count)))
        if source_count is not None:
            self.external_data_source_count.labels(name=name_label).set(max(0, int(source_count)))
        if outcome == "success":
            resolved_timestamp = float(timestamp if timestamp is not None else time.time())
            self._external_data_last_success_timestamps[name_label] = resolved_timestamp

            def _success_age(label: str = name_label) -> float:
                return max(0.0, time.time() - self._external_data_last_success_timestamps[label])

            self.external_data_last_success_age_seconds.labels(name=name_label).set_function(_success_age)

    def record_external_data_provider_call(
        self,
        name: ExternalDataName,
        source: ExternalDataSource,
        outcome: ExternalDataProviderOutcome,
        seconds: float,
        *,
        byte_count: int | None = None,
    ) -> None:
        name_label = _bounded_label(name, allowed=_EXTERNAL_DATA_NAMES, field="external_data_name")
        source_label = _bounded_label(source, allowed=_EXTERNAL_DATA_SOURCES, field="external_data_source")
        outcome_label = _bounded_label(
            outcome,
            allowed=_EXTERNAL_DATA_PROVIDER_OUTCOMES,
            field="external_data_provider_outcome",
        )
        self.external_data_provider_call_duration_seconds.labels(name=name_label, source=source_label).observe(
            max(0.0, float(seconds))
        )
        self.external_data_provider_call_total.labels(
            name=name_label,
            source=source_label,
            outcome=outcome_label,
        ).inc()
        if byte_count is not None:
            self.external_data_provider_bytes_total.labels(name=name_label, source=source_label).inc(
                max(0, int(byte_count))
            )

    def record_external_data_skipped(
        self,
        name: ExternalDataName,
        reason: ExternalDataSkipReason,
    ) -> None:
        name_label = _bounded_label(name, allowed=_EXTERNAL_DATA_NAMES, field="external_data_name")
        reason_label = _bounded_label(reason, allowed=_EXTERNAL_DATA_SKIP_REASONS, field="external_data_skip_reason")
        self.external_data_skipped_or_coalesced_total.labels(name=name_label, reason=reason_label).inc()

    def render_prometheus_text(self) -> str:
        return generate_latest(self.registry).decode("utf-8")


def _label(value: Any) -> str:
    return str(value).strip() or "unknown"


def _bounded_label(value: Any, *, allowed: frozenset[str], field: str) -> str:
    label = _label(value)
    if label not in allowed:
        raise ValueError(f"{field}_invalid:{label}")
    return label
