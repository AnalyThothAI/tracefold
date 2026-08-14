from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tracefold.market.radar.current_worker import TokenRadarCurrentProjection

NOW_MS = 1_800_000_000_000


def test_token_radar_sample_runs_one_sequential_v5_pipeline() -> None:
    database = _Database()
    cpu = _Cpu()
    service = _Service()
    projection = TokenRadarCurrentProjection(db=database, cpu=cpu, clock=lambda: NOW_MS)
    projection.service = service  # type: ignore[assignment]

    asyncio.run(projection.sample())

    assert database.operations == [
        "token_radar_current_load",
        "token_radar_current_present",
        "token_radar_current_publish",
    ]
    assert cpu.service_timeouts == [None]
    assert service.published_snapshot == {
        "schema_version": "token_radar_snapshot_v5",
        "social_evidence_as_of_ms": 0,
        "eligible_total": 0,
        "items": [],
    }


@pytest.mark.parametrize(
    "failure_operation",
    [
        "token_radar_current_load",
        "token_radar_current_present",
        "token_radar_current_publish",
    ],
)
def test_token_radar_database_failure_is_local_and_the_next_sample_retries(
    failure_operation: str,
) -> None:
    database = _Database(fail_once=failure_operation)
    service = _Service()
    projection = TokenRadarCurrentProjection(db=database, cpu=_Cpu(), clock=lambda: NOW_MS)
    projection.service = service  # type: ignore[assignment]

    asyncio.run(projection.sample())
    asyncio.run(projection.sample())

    assert database.operations.count("token_radar_current_load") == 2
    assert service.published_snapshot is not None


def test_token_radar_reducer_failure_is_local_and_the_next_sample_retries() -> None:
    database = _Database()
    service = _Service()
    projection = TokenRadarCurrentProjection(db=database, cpu=_Cpu(fail_once=True), clock=lambda: NOW_MS)
    projection.service = service  # type: ignore[assignment]

    asyncio.run(projection.sample())
    asyncio.run(projection.sample())

    assert database.operations == [
        "token_radar_current_load",
        "token_radar_current_load",
        "token_radar_current_present",
        "token_radar_current_publish",
    ]
    assert service.published_snapshot is not None


def test_token_radar_does_not_swallow_worker_cancellation() -> None:
    projection = TokenRadarCurrentProjection(
        db=_CancellingDatabase(),
        cpu=_Cpu(),
        clock=lambda: NOW_MS,
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(projection.sample())


class _Database:
    def __init__(self, *, fail_once: str | None = None) -> None:
        self.fail_once = fail_once
        self.operations: list[str] = []

    async def run_business(
        self,
        operation_name: str,
        function: Any,
        /,
        *args: Any,
        operation_timeout_seconds: float,
        **kwargs: Any,
    ) -> Any:
        assert operation_timeout_seconds == 9.0
        self.operations.append(operation_name)
        if self.fail_once == operation_name:
            self.fail_once = None
            raise RuntimeError("private failure detail")
        return function(*args, **kwargs)


class _CancellingDatabase:
    async def run_business(self, *_args: Any, **_kwargs: Any) -> Any:
        raise asyncio.CancelledError


class _Cpu:
    def __init__(self, *, fail_once: bool = False) -> None:
        self.fail_once = fail_once
        self.service_timeouts: list[float | None] = []

    async def run(
        self,
        _operation_name: str,
        function: Any,
        payload: dict[str, Any],
        *,
        service_timeout_seconds: float | None,
    ) -> Any:
        self.service_timeouts.append(service_timeout_seconds)
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("reducer failed")
        return function(payload)


class _Service:
    def __init__(self) -> None:
        self.published_snapshot: dict[str, Any] | None = None

    def load(self, *, now_ms: int) -> list[Any]:
        assert now_ms == NOW_MS
        return []

    def load_presentation(self, _reduced: Any, *, now_ms: int) -> list[dict[str, Any]]:
        assert now_ms == NOW_MS
        return []

    def publish(self, reduced: Any, *, now_ms: int) -> dict[str, Any]:
        assert now_ms == NOW_MS
        self.published_snapshot = reduced.snapshot
        return {"status": "published", "rows_written": 1}
