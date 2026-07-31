from __future__ import annotations

from tracefold.integrations.macro_sources import MacroSourceClient
from tracefold.macro import (
    MacroAcquisitionWorker,
)
from tracefold.platform.workers.factory import WorkerFactoryContext, mark_inactive
from tracefold.platform.workers.worker_base import WorkerBase

_ACQUISITION_WORKERS = {
    "macro_intraday_market": "intraday_market",
    "macro_settlements": "daily_settlement",
    "macro_economic_releases": "scheduled_release",
    "macro_official_state": "official_state",
    "macro_official_documents": "official_document",
}


def construct_macro_workers(ctx: WorkerFactoryContext) -> dict[str, WorkerBase]:
    source_config = ctx.settings.providers.macro_sources
    if not source_config.enabled:
        for worker_name in _ACQUISITION_WORKERS:
            mark_inactive(
                ctx,
                worker_name,
                effective_status="disabled",
                reason="macro_sources_disabled",
            )
        return {}

    constructed: dict[str, WorkerBase] = {}
    for worker_name, clock_kind in _ACQUISITION_WORKERS.items():
        constructed[worker_name] = MacroAcquisitionWorker(
            name=worker_name,
            clock_kind=clock_kind,
            db=ctx.db,
            telemetry=ctx.telemetry,
            source_client=MacroSourceClient(
                timeout_seconds=float(source_config.request_timeout_seconds),
                user_agent=str(source_config.user_agent),
                fred_enabled=source_config.fred_enabled,
                cboe_enabled=source_config.cboe_enabled,
                cftc_enabled=source_config.cftc_enabled,
                nasdaq_daily_enabled=source_config.nasdaq_daily_enabled,
                yfinance_enabled=source_config.yfinance_enabled,
            ),
            resources=ctx.resources,
            provider_governor=ctx.provider_governor,
            runtime_id=ctx.runtime_id,
        )

    return constructed


__all__ = ["construct_macro_workers"]
