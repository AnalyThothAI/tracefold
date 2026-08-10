from __future__ import annotations

import asyncio
import time

import pytest

from tracefold.market import TokenRadarCurrentProjection
from tracefold.market.radar.current_worker import _phase_timeout
from tracefold.market.radar.reducer import reduce_token_radar
from tracefold.platform.resource import ResourceOperationOverrun


def test_resource_operation_overrun_is_fatal() -> None:
    projection = TokenRadarCurrentProjection(
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
            if operation == "token_radar_current_present":
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
            total_timeout_seconds: float | None = None,
        ):
            assert total_timeout_seconds == service_timeout_seconds
            timeouts[operation] = service_timeout_seconds
            return reduce_token_radar(payload["rows"], now_ms=payload["now_ms"])

    projection = TokenRadarCurrentProjection(
        db=_Database(),
        cpu=_Cpu(),
        clock=lambda: 1_800_000_000_000,
    )

    asyncio.run(projection.sample())

    assert 2.9 < timeouts["token_radar_current_load"] <= 3.0
    assert 2.4 < timeouts["token_radar_current_reduce"] <= 2.5
    assert 0.2 < timeouts["token_radar_current_present"] <= 0.25
    assert 0.2 < timeouts["token_radar_current_publish"] <= 0.25


def test_token_radar_compute_borrows_only_unused_time_and_preserves_the_tail() -> None:
    full_budget = _phase_timeout(
        time.monotonic() + 4.0,
        cap=2.5,
        reserve_seconds=0.5,
    )
    cold_load_budget = _phase_timeout(
        time.monotonic() + 2.0,
        cap=2.5,
        reserve_seconds=0.5,
    )

    assert 2.49 < full_budget <= 2.5
    assert 1.49 < cold_load_budget <= 1.5


def test_token_radar_enriches_selected_targets_before_publishing() -> None:
    now_ms = 1_800_000_000_000
    published = []

    class _Database:
        async def run_business(
            self,
            operation: str,
            _function,
            *args,
            operation_timeout_seconds: float,
            **_kwargs,
        ):
            del operation_timeout_seconds
            if operation == "token_radar_current_load":
                return [
                    {
                        "target_type": "Asset",
                        "target_id": "asset-1",
                        "symbol": "PEPE",
                        "chain": "solana",
                        "exchange": None,
                        "address": "mint-1",
                        "resolution_status": "EXACT",
                        "event_id": f"event-{index}",
                        "received_at_ms": now_ms - minutes_ago * 60_000,
                        "author_handle": f"author-{index}",
                        "text": f"independent-{index}",
                        "signal_price_usd": "10" if index == 2 else None,
                    }
                    for index, minutes_ago in enumerate((30, 20, 10))
                ]
            if operation == "token_radar_current_present":
                assert _kwargs["now_ms"] == now_ms
                return [
                    {
                        "target_type": "Asset",
                        "target_id": "asset-1",
                        "name": "Pepe",
                        "logo_url": f"/api/token-images/{'a' * 64}",
                        "price_usd": "12",
                        "price_observed_at_ms": now_ms - 60_000,
                        "market_cap_usd": "12000000",
                        "market_cap_observed_at_ms": now_ms - 60_000,
                    }
                ]
            if operation == "token_radar_current_publish":
                published.append(args[0])
                return {"status": "published"}
            raise AssertionError(operation)

    class _Cpu:
        async def run(
            self,
            _operation,
            function,
            payload,
            *,
            service_timeout_seconds: float,
            total_timeout_seconds: float | None = None,
        ):
            assert total_timeout_seconds == service_timeout_seconds
            return function(payload)

    projection = TokenRadarCurrentProjection(
        db=_Database(),
        cpu=_Cpu(),
        clock=lambda: now_ms,
    )

    asyncio.run(projection.sample())

    assert len(published) == 1
    item = published[0].snapshot["items"][0]
    assert item["target"]["name"] == "Pepe"
    assert item["market"] == {
        "status": "confirmed",
        "price_change_since_signal": pytest.approx(0.2),
        "price_usd": 12.0,
        "market_cap_usd": 12_000_000.0,
        "observed_at_ms": now_ms - 60_000,
    }


class _OverrunningDatabase:
    async def run_business(self, *_args, **_kwargs):
        raise ResourceOperationOverrun("operation_overrun")
