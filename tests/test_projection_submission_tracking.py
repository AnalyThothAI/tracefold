from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tracefold.macro.projection_worker import MacroProjectionCandidate
from tracefold.platform.projection import ProjectionShard


class _Cpu:
    async def run(self, operation_name: str, *args: Any, on_submitted=None, **kwargs: Any) -> Any:
        del args, kwargs
        if on_submitted is not None:
            on_submitted()
        return {}


def _candidate(candidate_type: Any, *, db: Any) -> Any:
    return candidate_type(db=db, cpu=_Cpu(), runtime_id="runtime-1")


class _AdmissionBlockingDb:
    def __init__(self, *, block_operation: str, claim: Any) -> None:
        self.block_operation = block_operation
        self.claim = claim
        self.waiting = asyncio.Event()
        self.release = asyncio.Event()
        self.calls: list[str] = []

    async def run_business(
        self,
        operation_name: str,
        *args: Any,
        on_submitted=None,
        **kwargs: Any,
    ) -> Any:
        del args, kwargs
        self.calls.append(operation_name)
        if operation_name.endswith("_claim"):
            return self.claim
        if operation_name == self.block_operation:
            self.waiting.set()
            await self.release.wait()
            raise AssertionError("blocked admission unexpectedly resumed")
        if on_submitted is not None:
            on_submitted()
        if operation_name == "macro_projection_load":
            return {"status": "ready"}
        if operation_name.endswith("_release_prework"):
            return len(getattr(self.claim, "targets", (True,)))
        return {}


@pytest.mark.parametrize("candidate_type", (MacroProjectionCandidate,))
def test_projection_peek_watchdog_outlives_native_statement_timeout(candidate_type: Any) -> None:
    class _RecordingDb:
        def __init__(self) -> None:
            self.timeout_seconds: float | None = None

        async def run_business(self, _operation_name: str, *_args: Any, **kwargs: Any) -> None:
            self.timeout_seconds = float(kwargs["operation_timeout_seconds"])

    async def scenario() -> float | None:
        database = _RecordingDb()
        candidate = _candidate(candidate_type, db=database)
        assert await candidate.peek(now_ms=1_000) is None
        return database.timeout_seconds

    assert asyncio.run(scenario()) == 3.0


@pytest.mark.parametrize(
    ("candidate_type", "shard", "claim", "block_operation", "release_operation"),
    (
        (
            MacroProjectionCandidate,
            ProjectionShard("macro", "rates_fed", 100, 30),
            object(),
            "macro_projection_publish",
            "macro_projection_release_prework",
        ),
    ),
)
def test_cancellation_during_later_admission_releases_claim_exactly(
    candidate_type,
    shard: ProjectionShard,
    claim: Any,
    block_operation: str,
    release_operation: str,
) -> None:
    async def scenario() -> list[str]:
        db = _AdmissionBlockingDb(block_operation=block_operation, claim=claim)
        candidate = _candidate(candidate_type, db=db)
        task = asyncio.create_task(candidate.execute(shard))
        await asyncio.wait_for(db.waiting.wait(), timeout=1.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return db.calls

    calls = asyncio.run(scenario())
    assert calls.count(release_operation) == 1
