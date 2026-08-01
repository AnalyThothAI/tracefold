from __future__ import annotations

import asyncio
import time
from typing import Any

from tracefold.macro.projection import (
    MacroProjectionService,
    MacroShardOversized,
    compute_macro_module_projection,
)
from tracefold.platform.projection import ProjectionShard
from tracefold.platform.resource import (
    CpuTaskTimeout,
    ResourceAdmissionTimeout,
    ResourceOperationOverrun,
    ResourceSubmissionTracker,
)

_CPU_TIMEOUT_SECONDS = 2.0
_SHARD_TIMEOUT_SECONDS = 5.0


class MacroProjectionCandidate:
    def __init__(
        self,
        *,
        db: Any,
        cpu: Any,
        runtime_id: str,
        stable_order: int = 30,
    ) -> None:
        self.db = db
        self.cpu = cpu
        self.runtime_id = runtime_id
        self.stable_order = int(stable_order)
        self.service = MacroProjectionService(
            db=db,
        )

    async def peek(self, *, now_ms: int) -> ProjectionShard | None:
        row = await self.db.run_business(
            "macro_projection_peek",
            self.service.next_due_module,
            operation_timeout_seconds=0.5,
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

    async def execute(self, shard: ProjectionShard) -> bool:
        now_ms = _now_ms()
        try:
            claim = await self.db.run_business(
                "macro_projection_claim",
                self.service.claim_module,
                operation_timeout_seconds=0.5,
                module_id=shard.shard_key,
                runtime_id=self.runtime_id,
                now_ms=now_ms,
            )
        except ResourceAdmissionTimeout:
            return False
        if claim is None:
            return False
        submission = ResourceSubmissionTracker()

        try:
            async with asyncio.timeout(_SHARD_TIMEOUT_SECONDS):
                return await self._run_claimed_shard(
                    claim,
                    now_ms=now_ms,
                    submission=submission,
                )
        except asyncio.CancelledError:
            if not submission.submitted:
                await asyncio.shield(self._release_prework(claim))
            raise
        except ResourceAdmissionTimeout:
            await self._release_prework(claim)
            return False
        except TimeoutError as exc:
            if submission.submitted:
                raise ResourceOperationOverrun("resource_operation_overrun:macro_projection_turn") from exc
            await self.db.run_business(
                "macro_projection_timeout",
                self.service.fail_deterministic,
                claim,
                operation_timeout_seconds=3.0,
                error_code="full_shard_timeout",
                now_ms=_now_ms(),
            )
            return True

    async def _release_prework(self, claim: Any) -> bool:
        return bool(
            await self.db.run_business(
                "macro_projection_release_prework",
                self.service.release_prework,
                claim,
                operation_timeout_seconds=3.0,
                now_ms=_now_ms(),
            )
        )

    async def _run_claimed_shard(
        self,
        claim: Any,
        *,
        now_ms: int,
        submission: ResourceSubmissionTracker,
    ) -> bool:
        try:
            loaded = await submission.run(
                lambda on_submitted: self.db.run_business(
                    "macro_projection_load",
                    self.service.load_module,
                    claim,
                    operation_timeout_seconds=3.0,
                    on_submitted=on_submitted,
                    now_ms=now_ms,
                )
            )
            if loaded["status"] == "stale_snapshot":
                await submission.run(
                    lambda on_submitted: self.db.run_business(
                        "macro_projection_release_stale",
                        self.service.release_stale,
                        claim,
                        operation_timeout_seconds=3.0,
                        on_submitted=on_submitted,
                        now_ms=_now_ms(),
                    )
                )
                return True
            output = await submission.run(
                lambda on_submitted: self.cpu.run(
                    "macro_projection_compute",
                    compute_macro_module_projection,
                    loaded,
                    service_timeout_seconds=_CPU_TIMEOUT_SECONDS,
                    operation_timeout_seconds=_CPU_TIMEOUT_SECONDS,
                    on_submitted=on_submitted,
                )
            )
            await submission.run(
                lambda on_submitted: self.db.run_business(
                    "macro_projection_publish",
                    self.service.publish_module,
                    claim,
                    output,
                    operation_timeout_seconds=3.0,
                    on_submitted=on_submitted,
                    now_ms=_now_ms(),
                )
            )
        except (MacroShardOversized, CpuTaskTimeout) as exc:
            error_code = _error_code(exc)
            await submission.run(
                lambda on_submitted: self.db.run_business(
                    "macro_projection_fail_deterministic",
                    self.service.fail_deterministic,
                    claim,
                    operation_timeout_seconds=3.0,
                    on_submitted=on_submitted,
                    error_code=error_code,
                    now_ms=_now_ms(),
                )
            )
            return True
        return True


def _error_code(exc: BaseException) -> str:
    if isinstance(exc, MacroShardOversized):
        return "shard_oversized"
    if isinstance(exc, (CpuTaskTimeout, TimeoutError)):
        return "compute_timeout"
    return type(exc).__name__[:128]


def _now_ms() -> int:
    return int(time.time() * 1_000)


__all__ = ["MacroProjectionCandidate"]
