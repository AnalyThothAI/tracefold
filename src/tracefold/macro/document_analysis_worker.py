from __future__ import annotations

from typing import Any

from tracefold.macro.fed_analysis import MacroDocumentAnalysisService
from tracefold.platform.workers.worker_base import WorkerBase
from tracefold.platform.workers.worker_result import WorkerResult


class MacroDocumentAnalysisWorker(WorkerBase):
    def __init__(
        self,
        *,
        name: str,
        telemetry: Any,
        service: MacroDocumentAnalysisService,
    ) -> None:
        super().__init__(
            name=name,
            interval_seconds=30.0,
            telemetry=telemetry,
        )
        self.service = service

    async def run_once(self) -> WorkerResult:
        result = await self.service.run_once()
        status = str(result["status"])
        return WorkerResult(
            processed=1 if status == "published" else 0,
            skipped=1 if status == "idle" else 0,
            failed=1 if status == "failed" else 0,
            notes=result,
        )


__all__ = ["MacroDocumentAnalysisWorker"]
