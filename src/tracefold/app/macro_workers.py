from __future__ import annotations

from tracefold.integrations.macro_sources import MacroSourceClient
from tracefold.macro import (
    MacroAcquisitionWorker,
)
from tracefold.platform.workers.factory import WorkerFactoryContext, disabled_worker
from tracefold.platform.workers.worker_base import WorkerBase

_ACQUISITION_WORKERS = {
    "macro_intraday_market": "intraday_market",
    "macro_settlements": "daily_settlement",
    "macro_economic_releases": "scheduled_release",
    "macro_official_state": "official_state",
    "macro_official_documents": "official_document",
}


def construct_macro_workers(ctx: WorkerFactoryContext) -> dict[str, WorkerBase]:
    constructed: dict[str, WorkerBase] = {}
    source_config = ctx.settings.providers.macro_sources
    for worker_name, clock_kind in _ACQUISITION_WORKERS.items():
        worker_settings = getattr(ctx.settings.workers, worker_name)
        if not worker_settings.enabled or not source_config.enabled:
            constructed[worker_name] = disabled_worker(ctx, worker_name)
            continue
        constructed[worker_name] = MacroAcquisitionWorker(
            name=worker_name,
            clock_kind=clock_kind,
            settings=worker_settings,
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
        )

    return constructed


__all__ = ["construct_macro_workers"]
