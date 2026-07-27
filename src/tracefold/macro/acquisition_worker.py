from __future__ import annotations

import asyncio
from typing import Any

from tracefold.macro.acquisition import MacroAcquisitionService
from tracefold.macro.domain import MacroSourceClientProtocol
from tracefold.platform.workers.worker_base import WorkerBase
from tracefold.platform.workers.worker_result import WorkerResult


class MacroAcquisitionWorker(WorkerBase):
    def __init__(
        self,
        *,
        name: str,
        clock_kind: str,
        settings: Any,
        db: Any,
        telemetry: Any,
        source_client: MacroSourceClientProtocol,
    ) -> None:
        super().__init__(name=name, settings=settings, db=db, telemetry=telemetry)
        self.clock_kind = clock_kind
        self.source_client = source_client
        self.service = MacroAcquisitionService(
            db=db,
            worker_name=name,
            clock_kind=clock_kind,
            settings=settings,
            source_client=self.source_client,
        )

    async def run_once(self) -> WorkerResult:
        return await asyncio.to_thread(self._run_once_sync)

    async def on_close(self) -> None:
        await asyncio.to_thread(self.source_client.close)

    def _run_once_sync(self) -> WorkerResult:
        target_rows_written = self.service.ensure_targets()
        results: list[dict[str, Any]] = []
        for _ in range(int(self.settings.batch_size)):
            result = self.service.run_once()
            if result is None:
                break
            results.append(result)
        processed = sum(1 for result in results if result["status"] == "current")
        failed = sum(1 for result in results if result["status"] == "failed")
        unavailable = sum(1 for result in results if result["status"] == "unavailable")
        return WorkerResult(
            processed=processed,
            failed=failed,
            skipped=1 if not results else 0,
            notes={
                "clock_kind": self.clock_kind,
                "claimed": len(results),
                "targets_written": target_rows_written,
                "rows_seen": sum(int(result["rows_seen"]) for result in results),
                "rows_inserted": sum(int(result["rows_inserted"]) for result in results),
                "unavailable": unavailable,
                "datasets": [result["dataset_id"] for result in results],
            },
        )


__all__ = ["MacroAcquisitionWorker"]
