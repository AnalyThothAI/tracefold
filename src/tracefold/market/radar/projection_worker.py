from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from tracefold.market.radar.microbatch import (
    RadarMicroBatchClaim,
    RadarMicroBatchService,
    RadarShardOversized,
    compute_radar_target_batch,
    hydrate_radar_microbatch,
    rank_radar_microbatch,
)
from tracefold.platform.projection import ProjectionShard
from tracefold.platform.resource import (
    CpuTaskTimeout,
    ResourceAdmissionTimeout,
    ResourceSubmissionTracker,
)

_CPU_TIMEOUT_SECONDS = 2.0


class RadarProjectionCandidate:
    def __init__(
        self,
        *,
        db: Any,
        cpu: Any,
        runtime_id: str,
        stable_order: int = 10,
    ) -> None:
        self.db = db
        self.cpu = cpu
        self.runtime_id = runtime_id
        self.stable_order = int(stable_order)
        self.service = RadarMicroBatchService(db=db)

    async def peek(
        self,
        *,
        now_ms: int,
    ) -> ProjectionShard | None:
        row = await self.db.run_business(
            "radar_projection_peek",
            self.service.next_due,
            operation_timeout_seconds=3.0,
            now_ms=now_ms,
        )
        if row is None:
            return None
        return ProjectionShard(
            domain="radar",
            shard_key=_shard_key(
                window=str(row["window_key"]),
                venue=str(row["venue"]),
            ),
            deadline_at_ms=int(row["deadline_at_ms"]),
            stable_order=self.stable_order,
        )

    async def execute(self, shard: ProjectionShard) -> bool:
        now_ms = _now_ms()
        key = _parse_shard_key(shard.shard_key)
        try:
            claim = await self.db.run_business(
                "radar_projection_claim",
                self.service.claim_batch,
                operation_timeout_seconds=0.5,
                window=key["window"],
                venue=key["venue"],
                runtime_id=self.runtime_id,
                now_ms=now_ms,
            )
        except ResourceAdmissionTimeout:
            return False
        if claim is None:
            return False
        submission = ResourceSubmissionTracker()

        try:
            return await self._run_claimed(
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

    async def _release_prework(self, claim: RadarMicroBatchClaim) -> bool:
        released = await self.db.run_business(
            "radar_projection_release_prework",
            self.service.release_prework,
            claim,
            operation_timeout_seconds=3.0,
            now_ms=_now_ms(),
        )
        return int(released) == len(claim.targets)

    async def _run_claimed(
        self,
        claim: RadarMicroBatchClaim,
        *,
        now_ms: int,
        submission: ResourceSubmissionTracker,
    ) -> bool:
        try:
            loaded = await submission.run(
                lambda on_submitted: self.db.run_business(
                    "radar_projection_load",
                    self.service.load_targets,
                    claim,
                    operation_timeout_seconds=3.0,
                    on_submitted=on_submitted,
                    now_ms=now_ms,
                )
            )
            projections = await submission.run(
                lambda on_submitted: self.cpu.run(
                    "radar_projection_features",
                    compute_radar_target_batch,
                    loaded,
                    service_timeout_seconds=_CPU_TIMEOUT_SECONDS,
                    on_submitted=on_submitted,
                )
            )
            rank_inputs = await submission.run(
                lambda on_submitted: self.db.run_business(
                    "radar_projection_rank_input",
                    self.service.load_rank_inputs,
                    claim,
                    operation_timeout_seconds=3.0,
                    on_submitted=on_submitted,
                    projections=projections,
                    now_ms=now_ms,
                )
            )
            ranked = await submission.run(
                lambda on_submitted: self.cpu.run(
                    "radar_projection_rank",
                    rank_radar_microbatch,
                    rank_inputs,
                    service_timeout_seconds=_CPU_TIMEOUT_SECONDS,
                    on_submitted=on_submitted,
                )
            )
            hydrated = await submission.run(
                lambda on_submitted: self.db.run_business(
                    "radar_projection_hydration_input",
                    self.service.load_hydration,
                    claim,
                    operation_timeout_seconds=3.0,
                    on_submitted=on_submitted,
                    ranked=ranked,
                )
            )
            closure = await submission.run(
                lambda on_submitted: self.cpu.run(
                    "radar_projection_hydration",
                    _hydrate,
                    {
                        "ranked": ranked,
                        "hydrated_inputs": hydrated,
                    },
                    service_timeout_seconds=_CPU_TIMEOUT_SECONDS,
                    on_submitted=on_submitted,
                )
            )
            await submission.run(
                lambda on_submitted: self.db.run_business(
                    "radar_projection_publish",
                    self.service.publish,
                    claim,
                    operation_timeout_seconds=3.0,
                    on_submitted=on_submitted,
                    projections=projections,
                    ranked=ranked,
                    closure=closure,
                    now_ms=_now_ms(),
                )
            )
        except (
            RadarShardOversized,
            CpuTaskTimeout,
        ) as exc:
            error_code = _error_code(exc)
            await submission.run(
                lambda on_submitted: self.db.run_business(
                    "radar_projection_fail_deterministic",
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


def _hydrate(payload: dict[str, Any]) -> dict[str, Any]:
    return hydrate_radar_microbatch(
        ranked=payload["ranked"],
        hydrated_inputs=payload["hydrated_inputs"],
    )


def _shard_key(*, window: str, venue: str) -> str:
    return json.dumps(
        {"window": window, "venue": venue},
        sort_keys=True,
        separators=(",", ":"),
    )


def _parse_shard_key(value: str) -> dict[str, str]:
    payload = json.loads(value)
    if not isinstance(payload, dict) or set(payload) != {"window", "venue"}:
        raise ValueError("radar_projection_shard_key_invalid")
    return {
        "window": str(payload["window"]),
        "venue": str(payload["venue"]),
    }


def _error_code(exc: BaseException) -> str:
    if isinstance(exc, RadarShardOversized):
        return "shard_oversized"
    if isinstance(exc, CpuTaskTimeout | TimeoutError):
        return "compute_timeout"
    return type(exc).__name__[:128]


def _now_ms() -> int:
    return int(time.time() * 1_000)


__all__ = ["RadarProjectionCandidate"]
