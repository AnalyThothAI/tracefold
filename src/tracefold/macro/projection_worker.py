from __future__ import annotations

import asyncio
from typing import Any

from tracefold.macro.projection import MacroProjectionService
from tracefold.platform.workers.worker_base import WorkerBase
from tracefold.platform.workers.worker_result import WorkerResult


class MacroProjectionWorker(WorkerBase):
    def __init__(
        self,
        *,
        name: str,
        settings: Any,
        backfill_worker_enabled: bool,
        db: Any,
        telemetry: Any,
    ) -> None:
        super().__init__(name=name, settings=settings, db=db, telemetry=telemetry)
        self.service = MacroProjectionService(
            db=db,
            settings=settings,
            backfill_worker_enabled=backfill_worker_enabled,
            worker_name=name,
        )

    async def run_once(self) -> WorkerResult:
        summary = await asyncio.to_thread(self.service.rebuild)
        return WorkerResult(
            processed=int(summary["module_rows_written"]) + int(summary["feature_rows_written"]),
            skipped=1 if not summary["module_rows_written"] and not summary["feature_rows_written"] else 0,
            notes=summary,
        )


__all__ = ["MacroProjectionWorker"]
