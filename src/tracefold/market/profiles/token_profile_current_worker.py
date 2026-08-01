from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from tracefold.market.profiles.profile_projection import (
    ProfileProjectionService,
    ProfileShardOversized,
    compute_profile_current_projection,
)
from tracefold.platform.projection import ProjectionShard
from tracefold.platform.resource import (
    CpuTaskTimeout,
    ResourceAdmissionTimeout,
    ResourceSubmissionTracker,
)

_CPU_TIMEOUT_SECONDS = 2.0


class ProfileProjectionCandidate:
    def __init__(
        self,
        *,
        db: Any,
        cpu: Any,
        runtime_id: str,
        stable_order: int = 20,
    ) -> None:
        self.db = db
        self.cpu = cpu
        self.runtime_id = runtime_id
        self.stable_order = int(stable_order)
        self.service = ProfileProjectionService(db=db)

    async def peek(
        self,
        *,
        now_ms: int,
    ) -> ProjectionShard | None:
        row = await self.db.run_business(
            "profile_projection_peek",
            self.service.next_due,
            operation_timeout_seconds=3.0,
            now_ms=now_ms,
        )
        if row is None:
            return None
        return ProjectionShard(
            domain="profile",
            shard_key=_shard_key(
                target_type=str(row["target_type"]),
                target_id=str(row["target_id"]),
            ),
            deadline_at_ms=int(row["deadline_at_ms"]),
            stable_order=self.stable_order,
        )

    async def execute(self, shard: ProjectionShard) -> bool:
        now_ms = _now_ms()
        key = _parse_shard_key(shard.shard_key)
        try:
            claim = await self.db.run_business(
                "profile_projection_claim",
                self.service.claim,
                operation_timeout_seconds=0.5,
                target_type=key["target_type"],
                target_id=key["target_id"],
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
                "profile_projection_release_prework",
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
            loaded = await submission.run(
                lambda on_submitted: self.db.run_business(
                    "profile_projection_load",
                    self.service.load_target,
                    claim,
                    operation_timeout_seconds=3.0,
                    on_submitted=on_submitted,
                    now_ms=now_ms,
                )
            )
            output = await submission.run(
                lambda on_submitted: self.cpu.run(
                    "profile_projection_compute",
                    compute_profile_current_projection,
                    loaded,
                    service_timeout_seconds=_CPU_TIMEOUT_SECONDS,
                    on_submitted=on_submitted,
                )
            )
            await submission.run(
                lambda on_submitted: self.db.run_business(
                    "profile_projection_publish",
                    self.service.publish,
                    claim,
                    operation_timeout_seconds=3.0,
                    on_submitted=on_submitted,
                    loaded=loaded,
                    output=output,
                    now_ms=_now_ms(),
                )
            )
        except (
            ProfileShardOversized,
            CpuTaskTimeout,
        ) as exc:
            error_code = _error_code(exc)
            await submission.run(
                lambda on_submitted: self.db.run_business(
                    "profile_projection_fail_deterministic",
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


def _shard_key(*, target_type: str, target_id: str) -> str:
    return json.dumps(
        {
            "target_type": target_type,
            "target_id": target_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _parse_shard_key(value: str) -> dict[str, str]:
    payload = json.loads(value)
    if not isinstance(payload, dict):
        raise ValueError("profile_projection_shard_key_invalid")
    expected = {"target_type", "target_id"}
    if set(payload) != expected:
        raise ValueError("profile_projection_shard_key_invalid")
    return {key: str(payload[key]) for key in sorted(expected)}


def _error_code(exc: BaseException) -> str:
    if isinstance(exc, ProfileShardOversized):
        return "shard_oversized"
    if isinstance(exc, (CpuTaskTimeout, TimeoutError)):
        return "compute_timeout"
    return type(exc).__name__[:128]


def _now_ms() -> int:
    return int(time.time() * 1_000)


__all__ = ["ProfileProjectionCandidate"]
