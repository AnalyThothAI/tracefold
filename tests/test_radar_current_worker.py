from __future__ import annotations

import asyncio
import time
from threading import Event

import pytest

from tracefold.app import database as database_module
from tracefold.app.database import WorkerDatabase
from tracefold.market import TokenRadarCurrentProjection
from tracefold.market.radar import current_worker as radar_worker_module
from tracefold.market.radar.current_worker import _phase_timeout
from tracefold.market.radar.reducer import (
    RadarEvidenceRevision,
    reduce_token_radar,
    token_radar_text_fingerprint,
)
from tracefold.platform.resource import ResourceCapability, ResourceOperationOverrun


def test_resource_operation_overrun_is_fatal() -> None:
    projection = TokenRadarCurrentProjection(
        db=_OverrunningDatabase(),
        heavy_db=_OverrunningDatabase(),
        cpu=object(),
        source_is_streaming=lambda: True,
        clock=lambda: 1_800_000_000_000,
    )

    with pytest.raises(ResourceOperationOverrun, match="operation_overrun"):
        asyncio.run(projection.sample())


def test_token_radar_outer_timeout_records_operational_failure() -> None:
    projection = TokenRadarCurrentProjection(
        db=object(),
        heavy_db=object(),
        cpu=object(),
        source_is_streaming=lambda: True,
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


def test_token_radar_absolute_deadline_does_not_wait_for_an_owned_heavy_future(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        database = WorkerDatabase(worker_pool=object(), telemetry=None)
        heavy = database.heavy_business()
        release = Event()
        started = Event()
        failures: list[str] = []

        def blocking_heavy() -> None:
            started.set()
            release.wait(timeout=2.0)

        try:
            with pytest.raises(ResourceOperationOverrun):
                await heavy.run_business(
                    "already_owned_heavy",
                    blocking_heavy,
                    operation_timeout_seconds=0.001,
                )
            assert started.wait(timeout=1.0)

            projection = TokenRadarCurrentProjection(
                db=database,
                heavy_db=heavy,
                cpu=object(),
                source_is_streaming=lambda: True,
                clock=lambda: 1_800_000_000_000,
            )
            projection.service.mark_failed = (  # type: ignore[method-assign]
                lambda **kwargs: failures.append(str(kwargs["error_code"])) or 1
            )

            turn_started = time.monotonic()
            await projection.sample()
            elapsed = time.monotonic() - turn_started

            assert 0.035 <= elapsed <= 0.08
            assert failures == ["token_radar_sample_budget_exceeded"]
        finally:
            release.set()
            await database.drain_business(timeout_seconds=1.0)
            database.close_executors()

    monkeypatch.setattr(radar_worker_module, "TOKEN_RADAR_TURN_BUDGET_SECONDS", 0.05)
    monkeypatch.setattr(radar_worker_module, "_TOKEN_RADAR_FAILURE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(radar_worker_module, "_TOKEN_RADAR_PRESENT_TIMEOUT_SECONDS", 0.001)
    monkeypatch.setattr(radar_worker_module, "_TOKEN_RADAR_PUBLISH_TIMEOUT_SECONDS", 0.001)
    monkeypatch.setattr(database_module, "_WORKER_BUSINESS_OPERATION_COMPLETION_GRACE_SECONDS", 0.001)
    monkeypatch.setattr(database_module, "_WORKER_HEAVY_ADMISSION_TIMEOUT_SECONDS", 0.2)
    asyncio.run(scenario())


def test_token_radar_disconnected_at_sample_start_marks_source_stale_without_loading() -> None:
    now_ms = 1_800_000_000_000
    failures: list[str] = []
    loaded = False
    published = False

    class _Database:
        async def run_business(
            self,
            operation: str,
            _function,
            *_args,
            operation_timeout_seconds: float,
            **kwargs,
        ):
            nonlocal loaded, published
            del operation_timeout_seconds
            if operation == "token_radar_current_fail":
                failures.append(str(kwargs["error_code"]))
                return 1
            if operation == "token_radar_current_load":
                loaded = True
            if operation == "token_radar_current_publish":
                published = True
            raise AssertionError(operation)

    projection = TokenRadarCurrentProjection(
        db=_Database(),
        heavy_db=_Database(),
        cpu=object(),
        clock=lambda: now_ms,
        source_is_streaming=lambda: False,
    )

    asyncio.run(projection.sample())

    assert loaded is False
    assert published is False
    assert failures == ["token_radar_source_unavailable"]


def test_token_radar_disconnect_before_publish_discards_computed_snapshot() -> None:
    now_ms = 1_800_000_000_000
    streaming = iter((True, False))
    failures: list[str] = []
    published = False

    class _Database:
        async def run_business(
            self,
            operation: str,
            _function,
            *_args,
            operation_timeout_seconds: float,
            **kwargs,
        ):
            nonlocal published
            del operation_timeout_seconds
            if operation == "token_radar_current_load":
                return []
            if operation == "token_radar_current_present":
                return []
            if operation == "token_radar_current_fail":
                failures.append(str(kwargs["error_code"]))
                return 1
            if operation == "token_radar_current_publish":
                published = True
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
            del service_timeout_seconds, total_timeout_seconds
            return function(payload)

    projection = TokenRadarCurrentProjection(
        db=_Database(),
        heavy_db=_Database(),
        cpu=_Cpu(),
        clock=lambda: now_ms,
        source_is_streaming=lambda: next(streaming),
    )

    asyncio.run(projection.sample())

    assert published is False
    assert failures == ["token_radar_source_unavailable"]


@pytest.mark.parametrize(
    "failed_operation",
    (
        "token_radar_current_load",
        "token_radar_current_present",
        "token_radar_current_publish",
    ),
)
def test_token_radar_ordinary_projection_failure_marks_stale_then_remains_fatal(
    failed_operation: str,
) -> None:
    now_ms = 1_800_000_000_000
    primary_error = RuntimeError(f"{failed_operation}_failed")
    failures: list[str] = []

    class _Database:
        async def run_business(
            self,
            operation: str,
            _function,
            *_args,
            operation_timeout_seconds: float,
            **kwargs,
        ):
            del operation_timeout_seconds
            if operation == failed_operation:
                raise primary_error
            if operation == "token_radar_current_load":
                return []
            if operation == "token_radar_current_present":
                return []
            if operation == "token_radar_current_publish":
                return {"status": "published"}
            if operation == "token_radar_current_fail":
                failures.append(str(kwargs["error_code"]))
                return 1
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
            del service_timeout_seconds, total_timeout_seconds
            return function(payload)

    projection = TokenRadarCurrentProjection(
        db=_Database(),
        heavy_db=_Database(),
        cpu=_Cpu(),
        clock=lambda: now_ms,
        source_is_streaming=lambda: True,
    )

    with pytest.raises(RuntimeError) as caught:
        asyncio.run(projection.sample())

    assert caught.value is primary_error
    assert failures == ["token_radar_projection_failed"]


def test_token_radar_failure_recording_cannot_replace_the_fatal_projection_error() -> None:
    primary_error = RuntimeError("load_failed")
    failure_recording_error = RuntimeError("failure_recording_failed")

    class _Database:
        async def run_business(
            self,
            operation: str,
            _function,
            *_args,
            operation_timeout_seconds: float,
            **_kwargs,
        ):
            del operation_timeout_seconds
            if operation == "token_radar_current_load":
                raise primary_error
            if operation == "token_radar_current_fail":
                raise failure_recording_error
            raise AssertionError(operation)

    projection = TokenRadarCurrentProjection(
        db=_Database(),
        heavy_db=_Database(),
        cpu=object(),
        clock=lambda: 1_800_000_000_000,
        source_is_streaming=lambda: True,
    )

    with pytest.raises(RuntimeError) as caught:
        asyncio.run(projection.sample())

    assert caught.value is primary_error


def test_token_radar_projection_failure_gets_a_bounded_failure_write_budget() -> None:
    primary_error = RuntimeError("publish_failed")
    failure_timeout: list[float] = []

    class _Database:
        async def run_business(
            self,
            operation: str,
            _function,
            *_args,
            operation_timeout_seconds: float,
            **_kwargs,
        ):
            if operation in {"token_radar_current_load", "token_radar_current_present"}:
                return []
            if operation == "token_radar_current_publish":
                raise primary_error
            if operation == "token_radar_current_fail":
                failure_timeout.append(operation_timeout_seconds)
                return 1
            raise AssertionError(operation)

    class _Cpu:
        async def run(
            self,
            _operation,
            _function,
            payload,
            *,
            service_timeout_seconds: float,
            total_timeout_seconds: float | None = None,
        ):
            del service_timeout_seconds, total_timeout_seconds
            return reduce_token_radar(payload["rows"], now_ms=payload["now_ms"])

    projection = TokenRadarCurrentProjection(
        db=_Database(),
        heavy_db=_Database(),
        cpu=_Cpu(),
        clock=lambda: 1_800_000_000_000,
        source_is_streaming=lambda: True,
    )

    with pytest.raises(RuntimeError) as caught:
        asyncio.run(projection.sample())

    assert caught.value is primary_error
    assert len(failure_timeout) == 1
    assert 0.49 < failure_timeout[0] <= 0.5


def test_token_radar_allocates_its_twelve_second_budget_from_live_phase_costs() -> None:
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
        heavy_db=_Database(),
        cpu=_Cpu(),
        clock=lambda: 1_800_000_000_000,
        source_is_streaming=lambda: True,
    )

    asyncio.run(projection.sample())

    assert 8.9 < timeouts["token_radar_current_load"] <= 9.0
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
                rows = []
                for index, minutes_ago in enumerate((30, 20, 10)):
                    source_event_at_ms = now_ms - minutes_ago * 60_000
                    rows.append(
                        RadarEvidenceRevision(
                            event_id=f"event-{index}",
                            intent_id=f"intent-{index}",
                            resolution_id=f"resolution-{index}",
                            source_event_at_ms=source_event_at_ms,
                            received_at_ms=source_event_at_ms + 1_000,
                            event_created_at_ms=source_event_at_ms + 2_000,
                            action="tweet",
                            author_key=f"author-{index}",
                            text_fingerprint=token_radar_text_fingerprint(f"independent-{index}"),
                            resolution_status="EXACT",
                            target_type="Asset",
                            target_id="asset-1",
                            resolution_decision_at_ms=source_event_at_ms + 2_000,
                            resolution_created_at_ms=source_event_at_ms + 3_000,
                        )
                    )
                return rows
            if operation == "token_radar_current_present":
                assert _kwargs["now_ms"] == now_ms
                return [
                    {
                        "target_type": "Asset",
                        "target_id": "asset-1",
                        "symbol": "PEPE",
                        "chain": "solana",
                        "exchange": None,
                        "address": "mint-1",
                        "name": "Pepe",
                        "logo_url": f"/api/token-images/{'a' * 64}",
                        "signal_price_usd": "10",
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
        heavy_db=_Database(),
        cpu=_Cpu(),
        clock=lambda: now_ms,
        source_is_streaming=lambda: True,
    )

    asyncio.run(projection.sample())

    assert len(published) == 1
    item = published[0].snapshot["items"][0]
    assert item["target"]["name"] == "Pepe"
    assert item["market"] == {
        "price_usd": 12.0,
        "price_observed_at_ms": now_ms - 60_000,
        "price_change_since_signal": pytest.approx(0.2),
        "market_cap_usd": 12_000_000.0,
        "market_cap_observed_at_ms": now_ms - 60_000,
    }


class _OverrunningDatabase:
    async def run_business(self, *_args, **_kwargs):
        raise ResourceOperationOverrun(
            capability=ResourceCapability.DATABASE_BUSINESS,
            operation_name="operation_overrun",
        )
