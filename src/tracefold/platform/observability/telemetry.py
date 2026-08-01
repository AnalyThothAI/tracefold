from __future__ import annotations

import time
from typing import Any

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram, generate_latest

PROMETHEUS_CONTENT_TYPE = CONTENT_TYPE_LATEST


class TelemetryRegistry:
    def __init__(self, *, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry(auto_describe=True)
        self.processing_seconds = Histogram(
            "tracefold_worker_processing_seconds",
            "Worker processing duration in seconds.",
            ("worker",),
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
        self.projection_cache_total = Counter(
            "tracefold_worker_projection_cache_total",
            "Change-driven projection cache outcomes.",
            ("worker", "outcome"),
            registry=self.registry,
        )
        self.projection_deadline_misses_total = Counter(
            "tracefold_worker_projection_deadline_misses_total",
            "Projection shards completed after their freshness deadline.",
            ("worker", "domain"),
            registry=self.registry,
        )
        self.projection_soft_slo_overruns_total = Counter(
            "tracefold_worker_projection_soft_slo_overruns_total",
            "Projection turns exceeding a domain soft service-level objective.",
            ("domain",),
            registry=self.registry,
        )
        self.projection_transitions_total = Counter(
            "tracefold_worker_projection_transitions_total",
            "Committed executable arrivals and completions for deterministic projection frontiers.",
            ("domain", "transition"),
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
        for capability in (
            "database_business",
            "database_control",
            "finite_operation",
            "model_adapter",
            "cpu_process",
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

    def record_projection_cache(self, worker: str, outcome: str) -> None:
        self.projection_cache_total.labels(
            worker=_label(worker),
            outcome=_label(outcome),
        ).inc()

    def record_projection_deadline_miss(
        self,
        worker: str,
        domain: str,
    ) -> None:
        self.projection_deadline_misses_total.labels(
            worker=_label(worker),
            domain=_label(domain),
        ).inc()

    def record_projection_soft_slo_overrun(self, domain: str) -> None:
        self.projection_soft_slo_overruns_total.labels(domain=_label(domain)).inc()

    def record_projection_transition(self, domain: str, transition: str, count: int = 1) -> None:
        normalized_count = int(count)
        if normalized_count <= 0:
            return
        self.projection_transitions_total.labels(
            domain=_label(domain),
            transition=_label(transition),
        ).inc(normalized_count)

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

    def render_prometheus_text(self) -> str:
        return generate_latest(self.registry).decode("utf-8")


def _label(value: Any) -> str:
    return str(value).strip() or "unknown"
