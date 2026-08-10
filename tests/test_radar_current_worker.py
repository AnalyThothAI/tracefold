from __future__ import annotations

import asyncio

import pytest

from tracefold.market import StocksRadarCurrentProjection, TokenRadarCurrentProjection
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


class _OverrunningDatabase:
    async def run_business(self, *_args, **_kwargs):
        raise ResourceOperationOverrun("operation_overrun")
