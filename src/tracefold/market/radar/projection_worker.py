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
from tracefold.platform.workers.projection_candidate import ProjectionShard
from tracefold.platform.workers.resource_errors import (
    CpuTaskProcessExpired,
    CpuTaskTimeout,
)
from tracefold.platform.workers.worker_result import WorkerResult

_CPU_TIMEOUT_SECONDS = 2.0
_SHARD_TIMEOUT_SECONDS = 5.0


class RadarProjectionCandidate:
    def __init__(
        self,
        *,
        db: Any,
        resources: Any,
        runtime_id: str,
        stable_order: int = 10,
    ) -> None:
        self.resources = resources
        self.runtime_id = runtime_id
        self.stable_order = int(stable_order)
        self.service = RadarMicroBatchService(db=db)

    async def next_due_shard(
        self,
        *,
        now_ms: int,
    ) -> ProjectionShard | None:
        row = await self.resources.run_background_db(
            self.service.next_due,
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

    async def run_shard(self, shard: ProjectionShard) -> WorkerResult:
        now_ms = _now_ms()
        key = _parse_shard_key(shard.shard_key)
        claim = await self.resources.run_background_db(
            self.service.claim_batch,
            window=key["window"],
            venue=key["venue"],
            runtime_id=self.runtime_id,
            now_ms=now_ms,
        )
        if claim is None:
            return WorkerResult(
                skipped=1,
                notes={"reason": "radar_microbatch_claim_lost"},
            )
        try:
            async with asyncio.timeout(_SHARD_TIMEOUT_SECONDS):
                return await self._run_claimed(claim, now_ms=now_ms)
        except TimeoutError:
            failed = await self.resources.run_background_db(
                self.service.fail_deterministic,
                claim,
                error_code="full_shard_timeout",
                now_ms=_now_ms(),
            )
            return WorkerResult(
                failed=int(failed["failed_targets"]),
                notes={
                    "domain": "radar",
                    "shard_key": shard.shard_key,
                    "reason": "full_shard_timeout",
                    **failed,
                },
            )

    async def _run_claimed(
        self,
        claim: RadarMicroBatchClaim,
        *,
        now_ms: int,
    ) -> WorkerResult:
        try:
            loaded = await self.resources.run_background_db(
                self.service.load_targets,
                claim,
                now_ms=now_ms,
            )
            projections = await self.resources.run_cpu(
                compute_radar_target_batch,
                loaded,
                timeout_seconds=_CPU_TIMEOUT_SECONDS,
            )
            rank_inputs = await self.resources.run_background_db(
                self.service.load_rank_inputs,
                claim,
                projections=projections,
                now_ms=now_ms,
            )
            ranked = await self.resources.run_cpu(
                rank_radar_microbatch,
                rank_inputs,
                timeout_seconds=_CPU_TIMEOUT_SECONDS,
            )
            hydrated = await self.resources.run_background_db(
                self.service.load_hydration,
                claim,
                ranked=ranked,
            )
            closure = await self.resources.run_cpu(
                _hydrate,
                {
                    "ranked": ranked,
                    "hydrated_inputs": hydrated,
                },
                timeout_seconds=_CPU_TIMEOUT_SECONDS,
            )
            result = await self.resources.run_background_db(
                self.service.publish,
                claim,
                projections=projections,
                ranked=ranked,
                closure=closure,
                now_ms=_now_ms(),
            )
        except (
            RadarShardOversized,
            CpuTaskTimeout,
            CpuTaskProcessExpired,
        ) as exc:
            failed = await self.resources.run_background_db(
                self.service.fail_deterministic,
                claim,
                error_code=_error_code(exc),
                now_ms=_now_ms(),
            )
            return WorkerResult(
                failed=int(failed["failed_targets"]),
                notes={
                    "reason": _error_code(exc),
                    "window": claim.window,
                    "venue": claim.venue,
                    **failed,
                },
            )
        except Exception as exc:
            failed_targets = await self.resources.run_background_db(
                self.service.fail_transient,
                claim,
                error_code=_error_code(exc),
                now_ms=_now_ms(),
            )
            return WorkerResult(
                failed=max(1, int(failed_targets)),
                notes={
                    "reason": _error_code(exc),
                    "window": claim.window,
                    "venue": claim.venue,
                    "transient": True,
                    "failed_targets": int(failed_targets),
                },
            )
        return WorkerResult(
            processed=len(claim.targets),
            skipped=1 if int(result["rows_written"]) == 0 else 0,
            notes=result,
        )


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
    if isinstance(exc, CpuTaskProcessExpired):
        return "compute_process_expired"
    return type(exc).__name__[:128]


def _now_ms() -> int:
    return int(time.time() * 1_000)


__all__ = ["RadarProjectionCandidate"]
