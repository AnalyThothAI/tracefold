from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from tracefold.market.radar.projection import (
    RadarProjectionService,
    RadarShardOversized,
    build_token_radar_current_closure,
    compute_token_radar_target_projection,
    rank_token_radar_closure,
)
from tracefold.platform.workers.projection_candidate import ProjectionShard
from tracefold.platform.workers.resource_errors import (
    CpuTaskProcessExpired,
    CpuTaskTimeout,
)
from tracefold.platform.workers.worker_result import WorkerResult

_CPU_TIMEOUT_SECONDS = 2.0
_SHARD_TIMEOUT_SECONDS = 5.0
_RANK_LIMIT = 100


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
        self.service = RadarProjectionService(db=db)

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
        key = {
            "target_type": str(row["target_type"]),
            "target_id": str(row["target_id"]),
            "window_key": str(row["window_key"]),
            "venue": str(row["venue"]),
        }
        return ProjectionShard(
            domain="radar",
            shard_key=_shard_key(key),
            deadline_at_ms=int(row["deadline_at_ms"]),
            stable_order=self.stable_order,
        )

    async def run_shard(self, shard: ProjectionShard) -> WorkerResult:
        now_ms = _now_ms()
        key = _parse_shard_key(shard.shard_key)
        claim = await self.resources.run_background_db(
            self.service.claim,
            key=key,
            runtime_id=self.runtime_id,
            now_ms=now_ms,
        )
        if claim is None:
            return WorkerResult(
                skipped=1,
                notes={"reason": "radar_shard_claim_lost"},
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
                failed=1,
                notes={
                    "domain": "radar",
                    "shard_key": shard.shard_key,
                    "reason": "full_shard_timeout",
                    "quarantined": bool(failed and failed["status"] == "quarantined"),
                },
            )

    async def _run_claimed(
        self,
        claim: Any,
        *,
        now_ms: int,
    ) -> WorkerResult:
        try:
            loaded = await self.resources.run_background_db(
                self.service.load_target,
                claim,
                now_ms=now_ms,
            )
            target_projection = await self.resources.run_cpu(
                compute_token_radar_target_projection,
                loaded,
                timeout_seconds=_CPU_TIMEOUT_SECONDS,
            )
            ranked = await self.resources.run_cpu(
                rank_token_radar_closure,
                {
                    **loaded,
                    "feature": target_projection["feature"],
                    "venues": [claim.venue],
                    "rank_limit": _RANK_LIMIT,
                },
                timeout_seconds=_CPU_TIMEOUT_SECONDS,
            )
            hydrated = await self.resources.run_background_db(
                self.service.load_hydration,
                claim,
                target_projection=target_projection,
                ranked=ranked,
            )
            closure = await self.resources.run_cpu(
                build_token_radar_current_closure,
                {
                    "feature": target_projection["feature"],
                    "selected_by_venue": ranked["selected_by_venue"],
                    "hydrated_inputs": hydrated,
                },
                timeout_seconds=_CPU_TIMEOUT_SECONDS,
            )
            result = await self.resources.run_background_db(
                self.service.publish,
                claim,
                target_projection=target_projection,
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
                failed=1,
                notes={
                    "reason": _error_code(exc),
                    "target_type": claim.target_type,
                    "target_id": claim.target_id,
                    "window": claim.window,
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
                    "target_type": claim.target_type,
                    "target_id": claim.target_id,
                    "window": claim.window,
                    "transient": True,
                },
            )
        return WorkerResult(
            processed=1,
            skipped=1 if int(result["rows_written"]) == 0 else 0,
            notes=result,
        )


def _shard_key(key: dict[str, str]) -> str:
    return json.dumps(
        key,
        sort_keys=True,
        separators=(",", ":"),
    )


def _parse_shard_key(value: str) -> dict[str, str]:
    payload = json.loads(value)
    if not isinstance(payload, dict):
        raise ValueError("radar_projection_shard_key_invalid")
    expected = {"target_type", "target_id", "window_key", "venue"}
    if set(payload) != expected:
        raise ValueError("radar_projection_shard_key_invalid")
    return {key: str(payload[key]) for key in sorted(expected)}


def _error_code(exc: BaseException) -> str:
    if isinstance(exc, RadarShardOversized):
        return "shard_oversized"
    if isinstance(exc, (CpuTaskTimeout, TimeoutError)):
        return "compute_timeout"
    if isinstance(exc, CpuTaskProcessExpired):
        return "compute_process_expired"
    return type(exc).__name__[:128]


def _now_ms() -> int:
    return int(time.time() * 1_000)


__all__ = ["RadarProjectionCandidate"]
