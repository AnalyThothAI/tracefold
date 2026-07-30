from __future__ import annotations

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
        self.service.lease_owner = self.claim_owner
        resources = self.require_runtime_resources()
        target_rows_written = await resources.run_background_db(self.service.ensure_targets)
        results: list[dict[str, Any]] = []
        for _ in range(int(self.settings.batch_size)):
            claim = await resources.run_background_db(self.service.claim_next)
            if claim is None:
                break
            try:
                async with self.require_provider_governor().acquire(host=claim.spec.source_id):
                    batch = await resources.run_provider_io(
                        self.service.fetch_claim,
                        claim,
                    )
            except Exception as exc:
                result = await resources.run_background_db(
                    self.service.publish_failure,
                    claim,
                    exc,
                )
            else:
                result = await resources.run_background_db(
                    self.service.publish_success,
                    claim,
                    batch,
                )
            results.append(result)
        return _worker_result(
            clock_kind=self.clock_kind,
            target_rows_written=target_rows_written,
            results=results,
        )

    async def on_close(self) -> None:
        await self.require_runtime_resources().run_provider_cleanup(self.source_client.close)


def _worker_result(
    *,
    clock_kind: str,
    target_rows_written: int,
    results: list[dict[str, Any]],
) -> WorkerResult:
    processed = sum(1 for result in results if result["status"] == "current")
    failed = sum(1 for result in results if result["status"] == "failed")
    unavailable = sum(1 for result in results if result["status"] == "unavailable")
    return WorkerResult(
        processed=processed,
        failed=failed,
        skipped=1 if not results else 0,
        notes={
            "clock_kind": clock_kind,
            "claimed": len(results),
            "targets_written": target_rows_written,
            "rows_seen": sum(int(result["rows_seen"]) for result in results),
            "rows_inserted": sum(int(result["rows_inserted"]) for result in results),
            "unavailable": unavailable,
            "datasets": [result["dataset_id"] for result in results],
        },
    )


__all__ = ["MacroAcquisitionWorker"]
