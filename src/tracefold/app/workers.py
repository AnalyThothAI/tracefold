from __future__ import annotations

from tracefold.app.database import DBPoolBundle
from tracefold.app.provider_types import (
    AssetMarketProviders,
    WiredProviders,
)
from tracefold.app.worker_manifest import worker_names
from tracefold.market import EventPublisherProtocol
from tracefold.platform.config.settings import Settings
from tracefold.platform.observability import TelemetryRegistry
from tracefold.platform.workers.factory import (
    InactiveWorker,
    WorkerFactory,
    WorkerFactoryContext,
    disabled_worker,
    intentionally_not_started_worker,
    unavailable_worker,
)
from tracefold.platform.workers.worker_base import WorkerBase


def construct_workers(
    *,
    settings: Settings,
    db: DBPoolBundle,
    telemetry: TelemetryRegistry,
    providers: WiredProviders,
    hub: EventPublisherProtocol,
    collector: WorkerBase,
    collector_enabled: bool,
    collector_start_requested: bool = True,
) -> dict[str, WorkerBase]:
    ctx = WorkerFactoryContext(
        settings=settings,
        db=db,
        telemetry=telemetry,
        asset_market=providers.asset_market,
        hub=hub,
        collector=collector,
        collector_enabled=collector_enabled,
        collector_start_requested=collector_start_requested,
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
    actual = frozenset(constructed)
    if actual != canonical:
        missing = sorted(canonical - actual)
        unknown = sorted(actual - canonical)
        raise RuntimeError(f"worker_composition_mismatch:missing={missing}:unknown={unknown}")
    return {name: constructed[name] for name in canonical_names}


def construct_worker(
    *,
    worker_name: str,
    settings: Settings,
    db: DBPoolBundle,
    telemetry: TelemetryRegistry,
    asset_market: AssetMarketProviders | None,
    hub: EventPublisherProtocol | None,
    collector: WorkerBase | None,
    collector_enabled: bool,
    collector_start_requested: bool = False,
) -> WorkerBase:
    """Construct one worker from the same domain factories used by bootstrap."""
    ctx = WorkerFactoryContext(
        settings=settings,
        db=db,
        telemetry=telemetry,
        asset_market=asset_market,
        hub=hub,
        collector=collector,
        collector_enabled=collector_enabled,
        collector_start_requested=collector_start_requested,
    )
    candidates = [worker for factory in worker_factories() if (worker := factory(ctx).get(worker_name)) is not None]
    if len(candidates) != 1:
        raise RuntimeError(f"worker_composition_expected_one:{worker_name}:{len(candidates)}")
    return candidates[0]


def worker_factories() -> tuple[WorkerFactory, ...]:
    from tracefold.app.macro_workers import construct_macro_workers
    from tracefold.app.news_workers import construct_news_workers
    from tracefold.market import (
        construct_ingestion_workers,
        construct_market_workers,
        construct_radar_workers,
    )
    from tracefold.notifications import construct_notification_workers

    return (
        construct_ingestion_workers,
        construct_radar_workers,
        construct_market_workers,
        construct_macro_workers,
        construct_news_workers,
        construct_notification_workers,
    )


__all__ = [
    "InactiveWorker",
    "WorkerFactoryContext",
    "construct_worker",
    "construct_workers",
    "disabled_worker",
    "intentionally_not_started_worker",
    "unavailable_worker",
    "worker_factories",
]
