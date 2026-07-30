from __future__ import annotations

import asyncio
import time
from typing import Any

from tracefold.macro.projection import (
    MacroProjectionService,
    MacroShardOversized,
    compute_macro_module_projection,
)
from tracefold.platform.workers.projection_candidate import ProjectionShard
from tracefold.platform.workers.resource_errors import (
    CpuTaskProcessExpired,
    CpuTaskTimeout,
)
from tracefold.platform.workers.worker_result import WorkerResult

_CPU_TIMEOUT_SECONDS = 2.0
_SHARD_TIMEOUT_SECONDS = 5.0


class MacroProjectionCandidate:
    def __init__(
        self,
        *,
        settings: Any,
        db: Any,
        resources: Any,
        runtime_id: str,
        stable_order: int = 30,
    ) -> None:
        self.resources = resources
        self.runtime_id = runtime_id
        self.stable_order = int(stable_order)
        self.service = MacroProjectionService(
            db=db,
            settings=settings,
            backfill_worker_enabled=False,
        )

    async def next_due_shard(self, *, now_ms: int) -> ProjectionShard | None:
        row = await self.resources.run_background_db(
            self.service.next_due_module,
            now_ms=now_ms,
        )
        if row is None:
            return None
        return ProjectionShard(
            domain="macro",
            shard_key=str(row["module_id"]),
            deadline_at_ms=int(row["deadline_at_ms"]),
            stable_order=self.stable_order,
        )

    async def run_shard(self, shard: ProjectionShard) -> WorkerResult:
        now_ms = _now_ms()
        claim = await self.resources.run_background_db(
            self.service.claim_module,
            module_id=shard.shard_key,
            runtime_id=self.runtime_id,
            now_ms=now_ms,
        )
        if claim is None:
            return WorkerResult(skipped=1, notes={"reason": "macro_shard_claim_lost"})
        try:
            async with asyncio.timeout(_SHARD_TIMEOUT_SECONDS):
                return await self._run_claimed_shard(claim, now_ms=now_ms)
        except TimeoutError:
            failed = await self.resources.run_background_db(
                self.service.fail_deterministic,
                claim,
                error_code="full_shard_timeout",
                now_ms=_now_ms(),
            )
            return WorkerResult(
                failed=1,
                notes={
                    "domain": "macro",
                    "module_id": shard.shard_key,
                    "reason": "full_shard_timeout",
                    "quarantined": bool(failed and failed["status"] == "quarantined"),
                },
            )

    async def _run_claimed_shard(self, claim: Any, *, now_ms: int) -> WorkerResult:
        try:
            loaded = await self.resources.run_background_db(
                self.service.load_module,
                claim,
                now_ms=now_ms,
            )
            if loaded["status"] == "stale_snapshot":
                await self.resources.run_background_db(
                    self.service.release_stale,
                    claim,
                    now_ms=_now_ms(),
                )
                return WorkerResult(
                    skipped=1,
                    notes={
                        "reason": "stale_snapshot",
                        "module_id": claim.module_id,
                    },
                )
            output = await self.resources.run_cpu(
                compute_macro_module_projection,
                loaded,
                timeout_seconds=_CPU_TIMEOUT_SECONDS,
            )
            result = await self.resources.run_background_db(
                self.service.publish_module,
                claim,
                output,
                now_ms=_now_ms(),
            )
        except (MacroShardOversized, CpuTaskTimeout, CpuTaskProcessExpired) as exc:
            failed = await self.resources.run_background_db(
                self.service.fail_deterministic,
                claim,
                error_code=_error_code(exc),
                now_ms=_now_ms(),
            )
            return WorkerResult(
                failed=1,
                notes={
                    "reason": _error_code(exc),
                    "module_id": claim.module_id,
                    "quarantined": bool(failed and failed["status"] == "quarantined"),
                },
            )
        except Exception as exc:
            await self.resources.run_background_db(
                self.service.fail_transient,
                claim,
                error_code=_error_code(exc),
                now_ms=_now_ms(),
            )
            return WorkerResult(
                failed=1,
                notes={
                    "reason": _error_code(exc),
                    "module_id": claim.module_id,
                    "transient": True,
                },
            )
        return WorkerResult(
            processed=1,
            skipped=1 if int(result["rows_written"]) == 0 else 0,
            notes=result,
        )


def _error_code(exc: BaseException) -> str:
    if isinstance(exc, MacroShardOversized):
        return "shard_oversized"
    if isinstance(exc, (CpuTaskTimeout, TimeoutError)):
        return "compute_timeout"
    if isinstance(exc, CpuTaskProcessExpired):
        return "compute_process_expired"
    return type(exc).__name__[:128]


def _now_ms() -> int:
    return int(time.time() * 1_000)


__all__ = ["MacroProjectionCandidate"]
