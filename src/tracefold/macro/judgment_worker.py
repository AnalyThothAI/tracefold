from __future__ import annotations

import asyncio
from typing import Any

from tracefold.macro.judgment import MacroJudgmentService
from tracefold.platform.workers.worker_base import WorkerBase
from tracefold.platform.workers.worker_result import WorkerResult


class MacroJudgmentWorker(WorkerBase):
    def __init__(
        self,
        *,
        name: str,
        settings: Any,
        db: Any,
        telemetry: Any,
    ) -> None:
        super().__init__(name=name, settings=settings, db=db, telemetry=telemetry)
        self.service = MacroJudgmentService(
            db=db,
            settings=settings,
            worker_name=name,
        )

    async def run_once(self) -> WorkerResult:
        result = await asyncio.to_thread(self.service.publish_due)
        status = str(result["status"])
        return WorkerResult(
            processed=1 if status == "published" else 0,
            skipped=1 if status in {"not_due", "exists", "blocked"} else 0,
            failed=1 if status == "failed" else 0,
            notes=result,
        )


__all__ = ["MacroJudgmentWorker"]
