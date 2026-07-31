from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping
from datetime import datetime
from datetime import time as clock_time
from typing import Any
from zoneinfo import ZoneInfo

from tracefold.macro import is_us_market_session
from tracefold.platform.postgres.projection_frontier import MODEL_FRONTIER
from tracefold.platform.workers.worker_base import WorkerBase
from tracefold.platform.workers.worker_result import WorkerResult

_NEW_YORK = ZoneInfo("America/New_York")
_THESIS_RESERVE_AT = clock_time(8, 45)
_THESIS_WINS_AT = clock_time(8, 50)
_THESIS_DEADLINE_AT = clock_time(9, 0)
_MODEL_LEASE_MS = 15 * 60 * 1000
_MODEL_ESTIMATED_MS = {
    "macro_thesis": 8 * 60 * 1000,
    "news_brief": 60 * 1000,
    "macro_document_analysis": 3 * 60 * 1000,
}
_MODEL_STABLE_ORDER = {
    "macro_thesis": 10,
    "news_brief": 20,
    "macro_document_analysis": 30,
}


class ModelFrontierController:
    def __init__(self, *, db: Any, worker_name: str) -> None:
        self.db = db
        self.worker_name = worker_name

    def ensure_thesis_frontier(self, *, now_ms: int) -> dict[str, Any] | None:
        local = datetime.fromtimestamp(now_ms / 1000, tz=_NEW_YORK)
        session_date = local.date()
        if not is_us_market_session(session_date):
            return None
        deadline = datetime.combine(
            session_date,
            _THESIS_DEADLINE_AT,
            tzinfo=_NEW_YORK,
        )
        deadline_at_ms = int(deadline.timestamp() * 1000)
        with self._session() as repos, repos.transaction():
            repos.projection_frontiers.mark_dirty(
                MODEL_FRONTIER,
                key={
                    "candidate_kind": "macro_thesis",
                    "shard_key": session_date.isoformat(),
                },
                dirty_at_ms=now_ms,
                deadline_at_ms=deadline_at_ms,
                input_fingerprint=_stable_hash(
                    {
                        "candidate_kind": "macro_thesis",
                        "session_date": session_date.isoformat(),
                    }
                ),
                version="macro_thesis_v2",
            )
        return {
            "session_date": session_date.isoformat(),
            "deadline_at_ms": deadline_at_ms,
        }

    def candidates(
        self,
        *,
        allowed_kinds: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        if not allowed_kinds:
            return []
        with self._session() as repos:
            return [
                dict(row)
                for row in repos.conn.execute(
                    """
                    SELECT *
                      FROM model_generation_frontiers
                     WHERE candidate_kind = ANY(%s)
                       AND status IN ('dirty', 'retry_wait', 'running')
                     ORDER BY deadline_at_ms, candidate_kind, shard_key
                    """,
                    (list(allowed_kinds),),
                ).fetchall()
            ]

    def claim(
        self,
        *,
        candidate_kind: str,
        shard_key: str,
        runtime_id: str,
        now_ms: int,
    ) -> dict[str, Any] | None:
        with self._session() as repos, repos.transaction():
            return repos.projection_frontiers.claim(
                MODEL_FRONTIER,
                key={
                    "candidate_kind": candidate_kind,
                    "shard_key": shard_key,
                },
                runtime_id=runtime_id,
                now_ms=now_ms,
                lease_ms=_MODEL_LEASE_MS,
            )

    def complete(
        self,
        *,
        claim: Mapping[str, Any],
        runtime_id: str,
        now_ms: int,
    ) -> bool:
        with self._session() as repos, repos.transaction():
            return repos.projection_frontiers.complete(
                MODEL_FRONTIER,
                key={
                    "candidate_kind": str(claim["candidate_kind"]),
                    "shard_key": str(claim["shard_key"]),
                },
                runtime_id=runtime_id,
                input_fingerprint=str(claim["input_fingerprint"]),
                version=str(claim["workflow_version"]),
                now_ms=now_ms,
            )

    def fail_transient(
        self,
        *,
        claim: Mapping[str, Any],
        runtime_id: str,
        error_code: str,
        now_ms: int,
    ) -> bool:
        with self._session() as repos, repos.transaction():
            return repos.projection_frontiers.fail_transient(
                MODEL_FRONTIER,
                key={
                    "candidate_kind": str(claim["candidate_kind"]),
                    "shard_key": str(claim["shard_key"]),
                },
                runtime_id=runtime_id,
                error_code=error_code,
                now_ms=now_ms,
            )

    def refresh_document_frontier(self, *, now_ms: int) -> int:
        with self._session() as repos, repos.transaction():
            rows = repos.conn.execute(
                """
                SELECT analysis_job_id, document_hash, status,
                       next_due_at_ms, created_at_ms, updated_at_ms
                  FROM macro_document_analysis_jobs
                 WHERE status IN ('pending', 'retryable', 'claimed')
                 ORDER BY next_due_at_ms, analysis_job_id
                 LIMIT 10001
                """
            ).fetchall()
            if len(rows) > 10_000:
                raise RuntimeError("macro_document_model_frontier_oversized")
            if not rows:
                return 0
            deadline_at_ms = min(
                max(
                    int(row["next_due_at_ms"]),
                    int(row["created_at_ms"]) + 60 * 60 * 1000,
                )
                for row in rows
            )
            return repos.projection_frontiers.mark_dirty(
                MODEL_FRONTIER,
                key={
                    "candidate_kind": "macro_document_analysis",
                    "shard_key": "ready",
                },
                dirty_at_ms=now_ms,
                deadline_at_ms=deadline_at_ms,
                input_fingerprint=_stable_hash(
                    [
                        {
                            "analysis_job_id": row["analysis_job_id"],
                            "document_hash": row["document_hash"],
                            "status": row["status"],
                            "next_due_at_ms": row["next_due_at_ms"],
                            "updated_at_ms": row["updated_at_ms"],
                        }
                        for row in rows
                    ]
                ),
                version="macro_document_analysis_v1",
            )

    def _session(self) -> Any:
        return self.db.worker_session(
            self.worker_name,
            statement_timeout_seconds=3.0,
        )


class ModelGenerationCoordinator(WorkerBase):
    """Single-capacity EDF model arbiter with the Thesis reservation."""

    def __init__(
        self,
        *,
        db: Any,
        telemetry: Any,
        runtime_id: str,
        resources: Any,
        runners: Mapping[str, WorkerBase],
        name: str = "model_generation_coordinator",
        clock_ms: Any | None = None,
    ) -> None:
        super().__init__(
            name=name,
            interval_seconds=0.25,
            telemetry=telemetry,
        )
        self.resources = resources
        self.runtime_id = runtime_id
        self.runners = dict(runners)
        self.controller = ModelFrontierController(db=db, worker_name=name)
        self.clock_ms = clock_ms or _now_ms

    async def on_close(self) -> None:
        for runner in self.runners.values():
            await runner.aclose()

    async def run_once(self) -> WorkerResult:
        now_ms = int(self.clock_ms())
        if "macro_thesis" in self.runners:
            await self.resources.run_background_db(
                self.controller.ensure_thesis_frontier,
                now_ms=now_ms,
            )
        rows = await self.resources.run_background_db(
            self.controller.candidates,
            allowed_kinds=tuple(sorted(self.runners)),
        )
        selected, reason = select_model_frontier(rows, now_ms=now_ms)
        if selected is None:
            return WorkerResult(skipped=1, notes={"reason": reason})
        claim = await self.resources.run_background_db(
            self.controller.claim,
            candidate_kind=str(selected["candidate_kind"]),
            shard_key=str(selected["shard_key"]),
            runtime_id=self.runtime_id,
            now_ms=now_ms,
        )
        if claim is None:
            return WorkerResult(skipped=1, notes={"reason": "model_frontier_claim_lost"})
        kind = str(claim["candidate_kind"])
        try:
            result = await self.runners[kind].run_once()
        except Exception as exc:
            await self.resources.run_background_db(
                self.controller.fail_transient,
                claim=claim,
                runtime_id=self.runtime_id,
                error_code=type(exc).__name__[:128],
                now_ms=int(self.clock_ms()),
            )
            return WorkerResult(
                failed=1,
                notes={
                    "candidate_kind": kind,
                    "reason": type(exc).__name__,
                    "transient": True,
                },
            )
        if result.failed:
            await self.resources.run_background_db(
                self.controller.fail_transient,
                claim=claim,
                runtime_id=self.runtime_id,
                error_code=str(result.notes.get("reason") or result.notes.get("error_code") or "model_failed")[:128],
                now_ms=int(self.clock_ms()),
            )
            return result
        completed = await self.resources.run_background_db(
            self.controller.complete,
            claim=claim,
            runtime_id=self.runtime_id,
            now_ms=int(self.clock_ms()),
        )
        if not completed:
            return WorkerResult(
                skipped=1,
                notes={
                    "candidate_kind": kind,
                    "reason": "model_source_fingerprint_changed",
                },
            )
        if kind == "macro_document_analysis":
            await self.resources.run_background_db(
                self.controller.refresh_document_frontier,
                now_ms=int(self.clock_ms()),
            )
        return WorkerResult(
            processed=result.processed,
            skipped=result.skipped,
            notes={
                **result.notes,
                "candidate_kind": kind,
                "shard_key": str(claim["shard_key"]),
            },
        )


def select_model_frontier(
    rows: list[dict[str, Any]],
    *,
    now_ms: int,
) -> tuple[dict[str, Any] | None, str]:
    local = datetime.fromtimestamp(now_ms / 1000, tz=_NEW_YORK)
    current_time = local.timetz().replace(tzinfo=None)
    thesis_rows = [
        row
        for row in rows
        if str(row["candidate_kind"]) == "macro_thesis" and str(row["shard_key"]) == local.date().isoformat()
    ]
    thesis = thesis_rows[0] if thesis_rows else None
    thesis_unresolved = thesis is not None and str(thesis["status"]) in {
        "dirty",
        "retry_wait",
        "running",
    }
    if thesis_unresolved and current_time >= _THESIS_WINS_AT:
        if _eligible(thesis, now_ms=now_ms, ignore_deadline=True):
            return thesis, "macro_thesis_wins"
        return None, "macro_thesis_reserved_retry_wait"
    if thesis_unresolved and current_time >= _THESIS_RESERVE_AT:
        return None, "macro_thesis_capacity_reserved"

    due = [row for row in rows if _eligible(row, now_ms=now_ms, ignore_deadline=False)]
    if not due:
        return None, "no_model_candidate_due"
    reserve_at = datetime.combine(
        local.date(),
        _THESIS_RESERVE_AT,
        tzinfo=_NEW_YORK,
    )
    reserve_at_ms = int(reserve_at.timestamp() * 1000)
    safe = [
        row
        for row in due
        if not thesis_unresolved
        or str(row["candidate_kind"]) == "macro_thesis"
        or now_ms + _MODEL_ESTIMATED_MS[str(row["candidate_kind"])] <= reserve_at_ms
        or current_time >= _THESIS_WINS_AT
    ]
    if not safe:
        return None, "macro_thesis_capacity_reserved"
    return (
        min(
            safe,
            key=lambda row: (
                int(row["deadline_at_ms"]),
                _MODEL_STABLE_ORDER[str(row["candidate_kind"])],
                str(row["shard_key"]),
            ),
        ),
        "edf",
    )


def _eligible(
    row: Mapping[str, Any],
    *,
    now_ms: int,
    ignore_deadline: bool,
) -> bool:
    status = str(row["status"])
    if not ignore_deadline and int(row["deadline_at_ms"]) > now_ms:
        return False
    if status == "dirty":
        return True
    if status == "retry_wait":
        return int(row.get("next_attempt_at_ms") or row["deadline_at_ms"]) <= now_ms
    if status == "running":
        return int(row.get("claimed_until_ms") or 0) <= now_ms
    return False


def _stable_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()


def _now_ms() -> int:
    return int(time.time() * 1000)


__all__ = [
    "ModelFrontierController",
    "ModelGenerationCoordinator",
    "select_model_frontier",
]
