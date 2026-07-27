from __future__ import annotations

from typing import Any

from tracefold.macro.research.completed_session import (
    CompletedSessionMacro,
    MacroResearchNotReady,
    MacroSessionView,
)
from tracefold.platform.config.settings import MacroResearchWorkerSettings
from tracefold.platform.workers.worker_base import WorkerBase
from tracefold.platform.workers.worker_result import WorkerResult


class MacroResearchWorker(WorkerBase):
    def __init__(
        self,
        *,
        settings: MacroResearchWorkerSettings,
        db: Any,
        telemetry: Any,
        completed_session_macro: CompletedSessionMacro,
        name: str = "macro_research",
    ) -> None:
        if db is None:
            raise RuntimeError("macro_research_db_required")
        super().__init__(
            name=name,
            settings=settings,
            db=db,
            telemetry=telemetry,
        )
        self._completed_session_macro = completed_session_macro

    async def run_once(self) -> WorkerResult:
        try:
            result = await self._completed_session_macro.run()
        except MacroResearchNotReady as exc:
            return WorkerResult(
                skipped=1,
                notes={
                    "session_date": exc.session_date.isoformat(),
                    "status": "not_ready",
                    "reason": exc.reason,
                },
            )
        return _worker_result(result)


def _worker_result(result: MacroSessionView) -> WorkerResult:
    notes = {
        "session_date": result.session_date.isoformat(),
        "publication": result.status,
        "model_calls": result.model_calls,
        "run_rows_written": result.run_rows_written,
        "publication_rows_written": result.publication_rows_written,
        "artifact_hash": result.artifact_hash,
        "error_code": result.last_error_code,
        "error": result.last_error_message,
    }
    if result.publication_rows_written:
        return WorkerResult(processed=1, notes=notes)
    if result.status == "failed" or (result.status == "retryable" and result.model_calls > 0):
        return WorkerResult(failed=1, notes=notes)
    return WorkerResult(skipped=1, notes=notes)


__all__ = ["MacroResearchWorker"]
