from __future__ import annotations

import asyncio
import time
from typing import Any

from tracefold.platform.workers.projection_candidate import ProjectionShard
from tracefold.platform.workers.resource_errors import (
    CpuTaskProcessExpired,
    CpuTaskTimeout,
)
from tracefold.platform.workers.worker_result import WorkerResult

from .projection import (
    NewsProjectionService,
    NewsShardOversized,
    compute_news_component_projection,
    compute_news_edge_block,
    compute_news_identity_feature,
    compute_news_score_bucket,
    merge_final_edges,
    plan_news_edge_pairs,
)

_CPU_TIMEOUT_SECONDS = 2.0
_SHARD_TIMEOUT_SECONDS = 5.0
_PAIR_BLOCK_CAP = 4_096


class NewsProjectionCandidate:
    def __init__(
        self,
        *,
        db: Any,
        resources: Any,
        runtime_id: str,
        stable_order: int = 40,
    ) -> None:
        self.resources = resources
        self.runtime_id = runtime_id
        self.stable_order = int(stable_order)
        self.service = NewsProjectionService(db=db)

    async def next_due_shard(self, *, now_ms: int) -> ProjectionShard | None:
        row = await self.resources.run_background_db(
            self.service.next_due,
            now_ms=now_ms,
        )
        if row is None:
            return None
        return ProjectionShard(
            domain="news",
            shard_key=str(row["bucket_id"]),
            deadline_at_ms=int(row["deadline_at_ms"]),
            stable_order=self.stable_order,
        )

    async def run_shard(self, shard: ProjectionShard) -> WorkerResult:
        now_ms = _now_ms()
        claim = await self.resources.run_background_db(
            self.service.claim,
            bucket_id=shard.shard_key,
            runtime_id=self.runtime_id,
            now_ms=now_ms,
        )
        if claim is None:
            return WorkerResult(skipped=1, notes={"reason": "news_shard_claim_lost"})
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
                    "domain": "news",
                    "bucket_id": shard.shard_key,
                    "reason": "full_shard_timeout",
                    "quarantined": bool(failed and failed["status"] == "quarantined"),
                },
            )

    async def _run_claimed(self, claim: Any, *, now_ms: int) -> WorkerResult:
        try:
            if claim.kind == "score-bucket":
                loaded_score_bucket = await self.resources.run_background_db(
                    self.service.load_score_bucket,
                    claim,
                    now_ms=now_ms,
                )
                projection = await self.resources.run_cpu(
                    compute_news_score_bucket,
                    loaded_score_bucket,
                    timeout_seconds=_CPU_TIMEOUT_SECONDS,
                )
                result = await self.resources.run_background_db(
                    self.service.publish_score_bucket,
                    claim,
                    projection=projection,
                    now_ms=_now_ms(),
                )
                return WorkerResult(
                    processed=1,
                    skipped=1 if int(result["rows_written"]) == 0 else 0,
                    notes=result,
                )
            target = await self.resources.run_background_db(
                self.service.load_target,
                claim,
                now_ms=now_ms,
            )
            if target["status"] == "stale_snapshot":
                await self.resources.run_background_db(
                    self.service.release_stale,
                    claim,
                    now_ms=_now_ms(),
                )
                return WorkerResult(skipped=1, notes={"reason": target["reason"]})
            feature = await self.resources.run_cpu(
                compute_news_identity_feature,
                target,
                timeout_seconds=_CPU_TIMEOUT_SECONDS,
            )
            context = await self.resources.run_background_db(
                self.service.load_context,
                claim,
                feature,
                now_ms=now_ms,
            )
            if context["status"] == "stale_snapshot":
                await self.resources.run_background_db(
                    self.service.release_stale,
                    claim,
                    now_ms=_now_ms(),
                )
                return WorkerResult(skipped=1, notes={"reason": context["reason"]})
            edge_plan = await self.resources.run_cpu(
                plan_news_edge_pairs,
                context,
                timeout_seconds=_CPU_TIMEOUT_SECONDS,
            )
            new_edges: list[dict[str, Any]] = []
            pairs = list(edge_plan["recompute_pairs"])
            for offset in range(0, len(pairs), _PAIR_BLOCK_CAP):
                block = pairs[offset : offset + _PAIR_BLOCK_CAP]
                new_edges.extend(
                    await self.resources.run_cpu(
                        compute_news_edge_block,
                        block,
                        timeout_seconds=_CPU_TIMEOUT_SECONDS,
                    )
                )
            edge_plan["new_edges"] = new_edges
            final_edges = merge_final_edges(
                existing_edges=context["existing_edges"],
                affected_pairs=edge_plan["affected_pairs"],
                new_edges=new_edges,
            )
            projection = await self.resources.run_cpu(
                compute_news_component_projection,
                {
                    **context,
                    "final_edges": final_edges,
                },
                timeout_seconds=_CPU_TIMEOUT_SECONDS,
            )
            result = await self.resources.run_background_db(
                self.service.publish,
                claim,
                feature=feature,
                context=context,
                edge_plan=edge_plan,
                projection=projection,
                now_ms=_now_ms(),
            )
        except (NewsShardOversized, CpuTaskTimeout, CpuTaskProcessExpired) as exc:
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
                    "bucket_id": claim.bucket_id,
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
                    "bucket_id": claim.bucket_id,
                    "transient": True,
                },
            )
        return WorkerResult(
            processed=1,
            skipped=1 if int(result["rows_written"]) == 0 else 0,
            notes=result,
        )


def _error_code(exc: BaseException) -> str:
    if isinstance(exc, NewsShardOversized):
        return "shard_oversized"
    if isinstance(exc, (CpuTaskTimeout, TimeoutError)):
        return "compute_timeout"
    if isinstance(exc, CpuTaskProcessExpired):
        return "compute_process_expired"
    return type(exc).__name__[:128]


def _now_ms() -> int:
    return int(time.time() * 1_000)


__all__ = ["NewsProjectionCandidate"]
