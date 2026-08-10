from __future__ import annotations

import asyncio

import pytest

from tracefold.market import StocksRadarCurrentProjection, TokenRadarCurrentProjection
from tracefold.market.radar.reducer import reduce_token_radar
from tracefold.platform.resource import ResourceOperationOverrun


@pytest.mark.parametrize(
    "projection_type",
    [
        TokenRadarCurrentProjection,
        StocksRadarCurrentProjection,
    ],
)
def test_resource_operation_overrun_is_fatal(projection_type) -> None:
    projection = projection_type(
        db=_OverrunningDatabase(),
        cpu=object(),
        clock=lambda: 1_800_000_000_000,
    )

    with pytest.raises(ResourceOperationOverrun, match="operation_overrun"):
        asyncio.run(projection.sample())


def test_token_radar_outer_timeout_records_operational_failure() -> None:
    projection = TokenRadarCurrentProjection(
        db=object(),
        cpu=object(),
        clock=lambda: 1_800_000_000_000,
    )
    failures: list[tuple[str, int]] = []

    async def _timeout(*, now_ms: int, deadline: float) -> tuple[str, bool]:
        del now_ms, deadline
        raise TimeoutError

    async def _record(error_code: str, *, now_ms: int, deadline: float) -> None:
        del deadline
        failures.append((error_code, now_ms))

    projection._sample_with_deadline = _timeout  # type: ignore[method-assign]
    projection._mark_failed = _record  # type: ignore[method-assign]

    asyncio.run(projection.sample())

    assert failures == [("token_radar_sample_budget_exceeded", 1_800_000_000_000)]


def test_token_radar_allocates_its_five_second_budget_from_live_phase_costs() -> None:
    timeouts: dict[str, float] = {}

    class _Database:
        async def run_business(
            self,
            operation: str,
            _function,
            *_args,
            operation_timeout_seconds: float,
            **_kwargs,
        ):
            timeouts[operation] = operation_timeout_seconds
            if operation == "token_radar_current_load":
                return []
            return {"status": "unchanged"}

    class _Cpu:
        async def run(
            self,
            operation: str,
            _function,
            payload,
            *,
            service_timeout_seconds: float,
        ):
            timeouts[operation] = service_timeout_seconds
            return reduce_token_radar(payload["rows"], now_ms=payload["now_ms"])

    projection = TokenRadarCurrentProjection(
        db=_Database(),
        cpu=_Cpu(),
        clock=lambda: 1_800_000_000_000,
    )

    asyncio.run(projection.sample())

    assert 2.9 < timeouts["token_radar_current_load"] <= 3.0
    assert 1.4 < timeouts["token_radar_current_reduce"] <= 1.5
    assert 0.4 < timeouts["token_radar_current_publish"] <= 0.5
    assert 4.9 < sum(timeouts.values()) <= 5.0


class _OverrunningDatabase:
    async def run_business(self, *_args, **_kwargs):
        raise ResourceOperationOverrun("operation_overrun")
