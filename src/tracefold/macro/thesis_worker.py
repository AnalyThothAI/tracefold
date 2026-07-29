from __future__ import annotations

from typing import Any

from tracefold.macro.thesis_service import MacroThesisRunView, MacroThesisService
from tracefold.platform.config.settings import MacroThesisWorkerSettings
from tracefold.platform.workers.worker_base import WorkerBase
from tracefold.platform.workers.worker_result import WorkerResult


class MacroThesisWorker(WorkerBase):
    def __init__(
        self,
        *,
        settings: MacroThesisWorkerSettings,
        db: Any,
        telemetry: Any,
        service: MacroThesisService,
        name: str = "macro_thesis",
    ) -> None:
        super().__init__(name=name, settings=settings, db=db, telemetry=telemetry)
        self._service = service

    async def run_once(self) -> WorkerResult:
        return _worker_result(await self._service.run_due())


def _worker_result(view: MacroThesisRunView) -> WorkerResult:
    notes = {
        "session_date": view.session_date.isoformat(),
        "status": view.status,
        "evidence_pack_id": view.evidence_pack_id,
        "publication_id": view.publication_id,
        "model_calls": view.model_calls,
        "reviews": view.reviews,
        "publication_rows_written": view.publication_rows_written,
        "live_delta_rows_written": view.live_delta_rows_written,
        "outcome_rows_written": view.outcome_rows_written,
        "error_code": view.error_code,
        "error": view.error_message,
    }
    if view.publication_rows_written:
        return WorkerResult(processed=1, notes=notes)
    if view.status in {
        "failed",
        "config_error",
        "not_published",
        "retryable",
    }:
        return WorkerResult(failed=1, notes=notes)
    return WorkerResult(skipped=1, notes=notes)


__all__ = ["MacroThesisWorker"]
