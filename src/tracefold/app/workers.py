from __future__ import annotations

from tracefold.app.database import WorkerDatabase
from tracefold.app.provider_types import WiredProviders
from tracefold.app.worker_manifest import worker_names
from tracefold.platform.config.settings import Settings
from tracefold.platform.observability import TelemetryRegistry
from tracefold.platform.workers.factory import WorkerFactory, WorkerFactoryContext
from tracefold.platform.workers.worker_base import WorkerBase


def construct_workers(
    *,
    settings: Settings,
    db: WorkerDatabase,
    telemetry: TelemetryRegistry,
    providers: WiredProviders,
    collector: WorkerBase,
    collector_enabled: bool,
    resources: object,
    provider_governor: object,
    runtime_id: str,
) -> tuple[dict[str, WorkerBase], dict[str, dict[str, object]]]:
    inactive_statuses: dict[str, dict[str, object]] = {}
    ctx = WorkerFactoryContext(
        settings=settings,
        db=db,
        telemetry=telemetry,
        asset_market=providers.asset_market,
        collector=collector,
        collector_enabled=collector_enabled,
        resources=resources,
        provider_governor=provider_governor,
        runtime_id=runtime_id,
        inactive_statuses=inactive_statuses,
    )
    constructed: dict[str, WorkerBase] = {}
    for factory in worker_factories():
        for name, worker in factory(ctx).items():
            if name in constructed:
                raise ValueError(f"worker_composition_duplicate:{name}")
            if not isinstance(worker, WorkerBase):
                raise TypeError(f"worker_composition_invalid:{name}:{type(worker).__name__}")
            constructed[name] = worker

    canonical_names = worker_names()
    canonical = frozenset(canonical_names)
    actual = frozenset(constructed) | frozenset(inactive_statuses)
    if actual != canonical:
        missing = sorted(canonical - actual)
        unknown = sorted(actual - canonical)
        raise RuntimeError(f"worker_composition_mismatch:missing={missing}:unknown={unknown}")
    runnable = {name: constructed[name] for name in canonical_names if name in constructed}
    inactive = {name: inactive_statuses[name] for name in canonical_names if name in inactive_statuses}
    return runnable, inactive


def worker_factories() -> tuple[WorkerFactory, ...]:
    from tracefold.app.coordinator_workers import construct_coordinator_workers
    from tracefold.app.macro_workers import construct_macro_workers
    from tracefold.app.news_workers import construct_news_workers
    from tracefold.market import (
        construct_ingestion_workers,
        construct_market_workers,
    )

    return (
        construct_ingestion_workers,
        construct_market_workers,
        construct_macro_workers,
        construct_news_workers,
        construct_coordinator_workers,
    )


__all__ = [
    "WorkerFactoryContext",
    "construct_workers",
    "worker_factories",
]
