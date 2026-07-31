from __future__ import annotations

from tracefold.platform.workers.factory import WorkerFactoryContext, mark_inactive
from tracefold.platform.workers.worker_base import WorkerBase


def construct_ingestion_workers(ctx: WorkerFactoryContext) -> dict[str, WorkerBase]:
    if not ctx.collector_enabled:
        mark_inactive(
            ctx,
            "collector",
            effective_status="unavailable",
            reason="missing_ingestion_upstream_client_factory",
        )
        return {}
    if ctx.collector is None:
        mark_inactive(
            ctx,
            "collector",
            effective_status="unavailable",
            reason="missing_collector_service",
        )
        return {}
    return {"collector": ctx.collector}
