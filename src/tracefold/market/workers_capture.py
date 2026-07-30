from __future__ import annotations

from tracefold.platform.workers.factory import (
    WorkerFactoryContext,
    disabled_worker,
    unavailable_worker,
)
from tracefold.platform.workers.worker_base import WorkerBase


def construct_ingestion_workers(ctx: WorkerFactoryContext) -> dict[str, WorkerBase]:
    if not ctx.settings.workers.collector.enabled:
        return {"collector": disabled_worker(ctx, "collector")}
    if not ctx.collector_enabled:
        return {"collector": unavailable_worker(ctx, "collector", "missing_ingestion_upstream_client_factory")}
    if ctx.collector is None:
        return {"collector": unavailable_worker(ctx, "collector", "missing_collector_service")}
    return {"collector": ctx.collector}
