from __future__ import annotations

import asyncio
import time
from functools import partial
from typing import Any

from tracefold.platform.projection import ProjectionShard
from tracefold.platform.resource import (
    CpuTaskTimeout,
    ResourceAdmissionTimeout,
    ResourceSubmissionTracker,
)

from .projection import (
    NEWS_PAIR_BLOCK_CAP,
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


class NewsProjectionCandidate:
    def __init__(
        self,
        *,
        db: Any,
        cpu: Any,
        runtime_id: str,
        stable_order: int = 40,
    ) -> None:
        self.db = db
        self.cpu = cpu
        self.runtime_id = runtime_id
        self.stable_order = int(stable_order)
        self.service = NewsProjectionService(db=db)

    async def peek(self, *, now_ms: int) -> ProjectionShard | None:
        row = await self.db.run_business(
            "news_projection_peek",
            self.service.next_due,
            operation_timeout_seconds=3.0,
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

    async def execute(self, shard: ProjectionShard) -> bool:
        now_ms = _now_ms()
        try:
            claim = await self.db.run_business(
                "news_projection_claim",
                self.service.claim,
                operation_timeout_seconds=0.5,
                bucket_id=shard.shard_key,
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

    async def _release_prework(self, claim: Any) -> bool:
        return bool(
            await self.db.run_business(
                "news_projection_release_prework",
                self.service.release_prework,
                claim,
                operation_timeout_seconds=3.0,
                now_ms=_now_ms(),
            )
        )

    async def _run_claimed(
        self,
        claim: Any,
        *,
        now_ms: int,
        submission: ResourceSubmissionTracker,
    ) -> bool:
        try:
            if claim.kind == "score-bucket":
                loaded_score_bucket = await submission.run(
                    lambda on_submitted: self.db.run_business(
                        "news_projection_load_score_bucket",
                        self.service.load_score_bucket,
                        claim,
                        operation_timeout_seconds=3.0,
                        on_submitted=on_submitted,
                        now_ms=now_ms,
                    )
                )
                projection = await submission.run(
                    lambda on_submitted: self.cpu.run(
                        "news_projection_score_bucket",
                        compute_news_score_bucket,
                        loaded_score_bucket,
                        service_timeout_seconds=_CPU_TIMEOUT_SECONDS,
                        on_submitted=on_submitted,
                    )
                )
                await submission.run(
                    lambda on_submitted: self.db.run_business(
                        "news_projection_publish_score_bucket",
                        self.service.publish_score_bucket,
                        claim,
                        operation_timeout_seconds=3.0,
                        on_submitted=on_submitted,
                        projection=projection,
                        now_ms=_now_ms(),
                    )
                )
                return True
            target = await submission.run(
                lambda on_submitted: self.db.run_business(
                    "news_projection_load_target",
                    self.service.load_target,
                    claim,
                    operation_timeout_seconds=3.0,
                    on_submitted=on_submitted,
                    now_ms=now_ms,
                )
            )
            if target["status"] == "stale_snapshot":
                await submission.run(
                    lambda on_submitted: self.db.run_business(
                        "news_projection_release_stale_target",
                        self.service.release_stale,
                        claim,
                        operation_timeout_seconds=3.0,
                        on_submitted=on_submitted,
                        now_ms=_now_ms(),
                    )
                )
                return True
            feature = await submission.run(
                lambda on_submitted: self.cpu.run(
                    "news_projection_identity_feature",
                    compute_news_identity_feature,
                    target,
                    service_timeout_seconds=_CPU_TIMEOUT_SECONDS,
                    on_submitted=on_submitted,
                )
            )
            context = await submission.run(
                lambda on_submitted: self.db.run_business(
                    "news_projection_load_context",
                    self.service.load_context,
                    claim,
                    feature,
                    operation_timeout_seconds=3.0,
                    on_submitted=on_submitted,
                    now_ms=now_ms,
                )
            )
            if context["status"] == "stale_snapshot":
                await submission.run(
                    lambda on_submitted: self.db.run_business(
                        "news_projection_release_stale_context",
                        self.service.release_stale,
                        claim,
                        operation_timeout_seconds=3.0,
                        on_submitted=on_submitted,
                        now_ms=_now_ms(),
                    )
                )
                return True
            edge_plan = await submission.run(
                lambda on_submitted: self.cpu.run(
                    "news_projection_plan_edges",
                    plan_news_edge_pairs,
                    context,
                    service_timeout_seconds=_CPU_TIMEOUT_SECONDS,
                    on_submitted=on_submitted,
                )
            )
            new_edges: list[dict[str, Any]] = []
            pairs = list(edge_plan["recompute_pairs"])
            for offset in range(0, len(pairs), NEWS_PAIR_BLOCK_CAP):
                block = pairs[offset : offset + NEWS_PAIR_BLOCK_CAP]
                new_edges.extend(await submission.run(partial(_compute_edge_block, self.cpu, block)))
            edge_plan["new_edges"] = new_edges
            projection = await submission.run(
                lambda on_submitted: self.cpu.run(
                    "news_projection_component",
                    _compute_component_projection,
                    {
                        "context": context,
                        "edge_plan": edge_plan,
                    },
                    service_timeout_seconds=_CPU_TIMEOUT_SECONDS,
                    on_submitted=on_submitted,
                )
            )
            await submission.run(
                lambda on_submitted: self.db.run_business(
                    "news_projection_publish",
                    self.service.publish,
                    claim,
                    operation_timeout_seconds=3.0,
                    on_submitted=on_submitted,
                    feature=feature,
                    context=context,
                    edge_plan=edge_plan,
                    projection=projection,
                    now_ms=_now_ms(),
                )
            )
        except (NewsShardOversized, CpuTaskTimeout) as exc:
            error_code = _error_code(exc)
            await submission.run(
                lambda on_submitted: self.db.run_business(
                    "news_projection_fail_deterministic",
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
    if isinstance(exc, NewsShardOversized):
        return "shard_oversized"
    if isinstance(exc, (CpuTaskTimeout, TimeoutError)):
        return "compute_timeout"
    return type(exc).__name__[:128]


async def _compute_edge_block(
    cpu: Any,
    block: list[Any],
    on_submitted: Any,
) -> Any:
    return await cpu.run(
        "news_projection_edge_block",
        compute_news_edge_block,
        block,
        service_timeout_seconds=_CPU_TIMEOUT_SECONDS,
        on_submitted=on_submitted,
    )


def _compute_component_projection(payload: dict[str, Any]) -> dict[str, Any]:
    context = dict(payload["context"])
    edge_plan = dict(payload["edge_plan"])
    final_edges = merge_final_edges(
        existing_edges=context["existing_edges"],
        affected_pairs=edge_plan["affected_pairs"],
        new_edges=edge_plan["new_edges"],
    )
    return compute_news_component_projection(
        {
            **context,
            "final_edges": final_edges,
        }
    )


def _now_ms() -> int:
    return int(time.time() * 1_000)


__all__ = ["NewsProjectionCandidate"]
