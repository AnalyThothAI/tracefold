from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import httpx
import pytest
from psycopg import OperationalError

from tracefold.app import workers as workers_module
from tracefold.app.model_arbiter import run_model_arbiter
from tracefold.app.workers import (
    _MARKET_TICK_POLL_SECONDS,
    _run_due,
    _run_periodic,
)
from tracefold.integrations.macro_sources import MacroSourceClient
from tracefold.macro import MacroProjectionCandidate, acquisition_loop_policy, require_dataset
from tracefold.macro.domain import MacroSourceError
from tracefold.macro.runtime import MacroAcquisition
from tracefold.market.identity.resolution_refresh_worker import ResolutionRefresh
from tracefold.market.pricing.event_anchor_backfill_worker import (
    EventAnchorBackfill,
    _RescheduleOutcome,
    _TerminalOutcome,
)
from tracefold.market.pricing.market_tick_poll_worker import MarketTickPoll
from tracefold.market.profiles.asset_profile_refresh_worker import AssetProfileRefresh
from tracefold.market.profiles.token_image_mirror_worker import TokenImageMirror
from tracefold.market.provider_contracts import (
    DexProfileSource,
    DexProviderTemporarilyUnavailable,
    MarketProviderExpectedError,
)
from tracefold.news.push import NewsPushDeliveryError, NewsStoryPush, _payload_fingerprint
from tracefold.news.runtime import NewsBriefCandidate
from tracefold.platform.model_candidate import ModelCandidate
from tracefold.platform.resource import ResourceAdmissionTimeout, ResourceOperationOverrun


class _SubmittedOverrunFiniteOperations:
    async def run(self, operation_name, _function, /, *_args, **kwargs):
        before_submit = kwargs.get("before_submit")
        if before_submit is not None:
            await before_submit()
        on_submitted = kwargs.get("on_submitted")
        if on_submitted is not None:
            on_submitted()
        raise ResourceOperationOverrun(f"resource_operation_overrun:{operation_name}")


def test_macro_projection_candidate_runs_bounded_startup_reconciliation() -> None:
    class _Database:
        async def run_business(self, operation_name, function, /, *_args, **kwargs):
            assert operation_name == "macro_projection_reconcile"
            assert function.__name__ == "reconcile_frontiers"
            assert kwargs["operation_timeout_seconds"] == 3.0
            assert isinstance(kwargs["now_ms"], int)
            return 4

    async def scenario() -> int:
        candidate = MacroProjectionCandidate(
            db=_Database(),
            cpu=object(),
            runtime_id="a3b1f67c-6f83-4c7f-9ea4-aab4b652e343",
        )
        return await candidate.reconcile()

    assert asyncio.run(scenario()) == 4


def test_productive_due_loop_has_a_minimum_repoll_cadence() -> None:
    async def scenario() -> list[float]:
        stop_event = asyncio.Event()
        started_at: list[float] = []

        async def turn() -> bool:
            started_at.append(time.monotonic())
            if len(started_at) == 2:
                stop_event.set()
            return True

        await _run_due(turn, idle_seconds=1.0, stop_event=stop_event)
        return started_at

    started_at = asyncio.run(scenario())

    assert len(started_at) == 2
    assert started_at[1] - started_at[0] >= 0.20


def test_macro_idle_poll_never_sleeps_past_the_retry_clock() -> None:
    assert acquisition_loop_policy("intraday_market") == (300.0, 32)
    assert acquisition_loop_policy("daily_settlement") == (900.0, 32)
    assert acquisition_loop_policy("scheduled_release") == (900.0, 4)
    assert acquisition_loop_policy("official_state") == (900.0, 4)
    assert acquisition_loop_policy("official_document") == (900.0, 2)


def test_periodic_loop_can_defer_its_first_sample_without_changing_cadence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waits: list[float] = []
    samples = 0

    async def _wait(_stop_event: asyncio.Event, seconds: float) -> None:
        waits.append(seconds)

    async def scenario() -> None:
        nonlocal samples
        stop_event = asyncio.Event()

        async def sample() -> None:
            nonlocal samples
            samples += 1
            stop_event.set()

        await _run_periodic(
            sample,
            period_seconds=30.0,
            initial_delay_seconds=30.0,
            stop_event=stop_event,
        )

    monkeypatch.setattr(workers_module, "_wait_or_stop", _wait)
    asyncio.run(scenario())

    assert samples == 1
    assert waits[0] == 30.0


def test_control_loop_retries_a_transient_pooled_heartbeat_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    waits: list[float] = []

    async def heartbeat(**_kwargs: object) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OperationalError("server closed the pooled connection")
        stop_event.set()
        return 2_000

    async def wait(_stop_event: asyncio.Event, seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(workers_module, "_control_liveness_and_heartbeat", heartbeat)
    monkeypatch.setattr(workers_module, "_wait_or_stop", wait)
    stop_event = asyncio.Event()
    probe_state = workers_module._ProbeState(
        runtime_id="runtime-control-retry",
        runtime_version="v2",
        started_at_ms=1_000,
        clock_ms=lambda: 2_000,
    )

    asyncio.run(
        workers_module._run_control(
            db=object(),  # type: ignore[arg-type]
            lock_conn=object(),
            runtime_id=probe_state.runtime_id,
            probe_state=probe_state,
            stop_event=stop_event,
        )
    )

    assert calls == 2
    assert waits == [workers_module._CONTROL_RETRY_SECONDS, workers_module._HEARTBEAT_SECONDS]
    assert probe_state.heartbeat_at_ms == 2_000


def test_persistent_transient_heartbeat_failures_remain_bounded_by_watchdog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def heartbeat(**_kwargs: object) -> int:
        raise OperationalError("server keeps closing the pooled connection")

    async def scenario() -> None:
        stop_event = asyncio.Event()
        probe_state = workers_module._ProbeState(
            runtime_id="runtime-control-watchdog",
            runtime_version="v2",
            started_at_ms=1_000,
            clock_ms=lambda: 2_000,
        )
        control = asyncio.create_task(
            workers_module._run_control(
                db=object(),  # type: ignore[arg-type]
                lock_conn=object(),
                runtime_id=probe_state.runtime_id,
                probe_state=probe_state,
                stop_event=stop_event,
            )
        )
        try:
            await asyncio.wait_for(
                workers_module._run_control_watchdog(
                    probe_state=probe_state,
                    stop_event=stop_event,
                ),
                timeout=0.5,
            )
        finally:
            stop_event.set()
            await control

    monkeypatch.setattr(workers_module, "_control_liveness_and_heartbeat", heartbeat)
    monkeypatch.setattr(workers_module, "_CONTROL_RETRY_SECONDS", 0.05)
    monkeypatch.setattr(workers_module, "_CONTROL_HEARTBEAT_STALE_SECONDS", 0.01)

    with pytest.raises(workers_module._ControlFailure, match="workers_control_heartbeat_stale"):
        asyncio.run(scenario())


def test_productive_model_candidate_has_a_minimum_repoll_cadence() -> None:
    class _BackloggedCandidate:
        def __init__(self, stop_event: asyncio.Event) -> None:
            self.stop_event = stop_event
            self.started_at: list[float] = []

        async def peek(self, *, now_ms: int) -> ModelCandidate:
            return ModelCandidate(
                kind="news_brief",
                target_key="fingerprint",
                due_at_ms=now_ms,
                stable_order=1,
            )

        async def execute(self, candidate: ModelCandidate) -> bool:
            del candidate
            self.started_at.append(time.monotonic())
            if len(self.started_at) == 2:
                self.stop_event.set()
            return True

    async def scenario() -> list[float]:
        stop_event = asyncio.Event()
        candidate = _BackloggedCandidate(stop_event)
        await run_model_arbiter((candidate,), stop_event=stop_event)
        return candidate.started_at

    started_at = asyncio.run(scenario())

    assert len(started_at) == 2
    assert started_at[1] - started_at[0] >= 0.20


def test_news_brief_candidate_uses_the_utc_slot_as_its_only_target_key() -> None:
    class _Database:
        async def run_business(self, operation_name, _function, /, *_args, **_kwargs):
            assert operation_name == "news_brief_peek"
            return {"slot_at_ms": 1_800_000, "next_due_at_ms": 1_800_000}

    async def scenario() -> ModelCandidate | None:
        candidate = NewsBriefCandidate(
            db=_Database(),
            model_adapter=object(),
            publisher=object(),
            runtime_id="runtime-1",
        )
        return await candidate.peek(now_ms=1_800_001)

    assert asyncio.run(scenario()) == ModelCandidate(
        kind="news_brief",
        target_key="1800000",
        due_at_ms=1_800_000,
        stable_order=20,
    )


def test_news_brief_prepare_watchdog_covers_both_bounded_database_sessions() -> None:
    class _Database:
        def __init__(self) -> None:
            self.calls: list[tuple[str, float]] = []

        async def run_business(self, operation_name, _function, /, *_args, **kwargs):
            self.calls.append((operation_name, float(kwargs["operation_timeout_seconds"])))

    async def scenario() -> list[tuple[str, float]]:
        database = _Database()
        candidate = NewsBriefCandidate(
            db=database,
            model_adapter=object(),
            publisher=object(),
            runtime_id="runtime-1",
        )
        completed = await candidate.execute(
            ModelCandidate(
                kind="news_brief",
                target_key="fingerprint",
                due_at_ms=1_000,
                stable_order=20,
            )
        )
        assert completed is False
        return database.calls

    assert asyncio.run(scenario()) == [("news_brief_prepare", 7.0)]


def test_news_brief_publish_retries_after_pre_submission_database_pressure() -> None:
    class _Database:
        def __init__(self) -> None:
            self.operations: list[str] = []

        async def run_business(self, operation_name, _function, /, *_args, **kwargs):
            self.operations.append(operation_name)
            if operation_name == "news_brief_prepare":
                return {
                    "completed_without_model": False,
                    "claim": {"fingerprint": "fingerprint"},
                    "stories": [],
                }
            if operation_name == "news_brief_start_model":
                return True
            if operation_name == "news_brief_publish":
                raise ResourceAdmissionTimeout("worker_database_admission_timeout:news_brief_publish")
            raise AssertionError(operation_name)

    class _ModelAdapter:
        async def run(self, _operation_name, _function, /, *_args, **kwargs):
            await kwargs["before_submit"]()
            kwargs["on_submitted"]()
            return {"summary": "generated"}

    async def scenario() -> tuple[bool, list[str]]:
        database = _Database()
        candidate = NewsBriefCandidate(
            db=database,
            model_adapter=_ModelAdapter(),
            publisher=object(),
            runtime_id="runtime-1",
        )
        completed = await candidate.execute(
            ModelCandidate(
                kind="news_brief",
                target_key="fingerprint",
                due_at_ms=1_000,
                stable_order=20,
            )
        )
        return completed, database.operations

    assert asyncio.run(scenario()) == (
        False,
        ["news_brief_prepare", "news_brief_start_model", "news_brief_publish"],
    )


def test_news_brief_publish_propagates_post_submission_database_failure() -> None:
    class _Database:
        async def run_business(self, operation_name, _function, /, *_args, **kwargs):
            if operation_name == "news_brief_prepare":
                return {
                    "completed_without_model": False,
                    "claim": {"fingerprint": "fingerprint"},
                    "stories": [],
                }
            if operation_name == "news_brief_start_model":
                return True
            if operation_name == "news_brief_publish":
                kwargs["on_submitted"]()
                raise ResourceAdmissionTimeout("worker_database_statement_timeout:news_brief_publish")
            raise AssertionError(operation_name)

    class _ModelAdapter:
        async def run(self, _operation_name, _function, /, *_args, **kwargs):
            await kwargs["before_submit"]()
            kwargs["on_submitted"]()
            return {"summary": "generated"}

    async def scenario() -> None:
        candidate = NewsBriefCandidate(
            db=_Database(),
            model_adapter=_ModelAdapter(),
            publisher=object(),
            runtime_id="runtime-1",
        )
        await candidate.execute(
            ModelCandidate(
                kind="news_brief",
                target_key="fingerprint",
                due_at_ms=1_000,
                stable_order=20,
            )
        )

    with pytest.raises(ResourceAdmissionTimeout, match="worker_database_statement_timeout"):
        asyncio.run(scenario())


def test_news_brief_model_operation_overrun_propagates_to_the_workers_root() -> None:
    class _Database:
        def __init__(self) -> None:
            self.operations: list[str] = []

        async def run_business(self, operation_name, _function, /, *_args, **kwargs):
            self.operations.append(operation_name)
            if operation_name == "news_brief_prepare":
                return {
                    "completed_without_model": False,
                    "claim": {"fingerprint": "fingerprint"},
                    "stories": [],
                }
            if operation_name == "news_brief_start_model":
                return True
            raise AssertionError(operation_name)

    class _ModelAdapter:
        async def run(self, _operation_name, _function, /, *_args, **kwargs):
            await kwargs["before_submit"]()
            kwargs["on_submitted"]()
            raise ResourceOperationOverrun("resource_operation_overrun:news_brief_inference")

    async def scenario(database: _Database) -> None:
        candidate = NewsBriefCandidate(
            db=database,
            model_adapter=_ModelAdapter(),
            publisher=object(),
            runtime_id="runtime-1",
        )
        await candidate.execute(
            ModelCandidate(
                kind="news_brief",
                target_key="fingerprint",
                due_at_ms=1_000,
                stable_order=20,
            )
        )

    database = _Database()
    with pytest.raises(ResourceOperationOverrun, match="news_brief_inference"):
        asyncio.run(scenario(database))
    assert database.operations == ["news_brief_prepare", "news_brief_start_model"]


def test_news_brief_lost_start_fence_never_submits_the_model() -> None:
    class _Database:
        def __init__(self) -> None:
            self.operations: list[str] = []

        async def run_business(self, operation_name, _function, /, *_args, **kwargs):
            self.operations.append(operation_name)
            if operation_name == "news_brief_prepare":
                return {
                    "completed_without_model": False,
                    "claim": {"fingerprint": "fingerprint"},
                    "stories": [],
                }
            if operation_name == "news_brief_start_model":
                return False
            raise AssertionError(operation_name)

    class _ModelAdapter:
        def __init__(self) -> None:
            self.submitted = False

        async def run(self, _operation_name, _function, /, *_args, **kwargs):
            await kwargs["before_submit"]()
            self.submitted = True
            raise AssertionError("model must remain unsubmitted")

    async def scenario() -> tuple[bool, _Database, _ModelAdapter]:
        database = _Database()
        model_adapter = _ModelAdapter()
        candidate = NewsBriefCandidate(
            db=database,
            model_adapter=model_adapter,
            publisher=object(),
            runtime_id="runtime-1",
        )
        result = await candidate.execute(
            ModelCandidate(
                kind="news_brief",
                target_key="fingerprint",
                due_at_ms=1_000,
                stable_order=20,
            )
        )
        return result, database, model_adapter

    result, database, model_adapter = asyncio.run(scenario())
    assert result is False
    assert model_adapter.submitted is False
    assert database.operations == ["news_brief_prepare", "news_brief_start_model"]


def test_due_loop_propagates_post_work_admission_timeout() -> None:
    async def turn() -> bool:
        raise ResourceAdmissionTimeout("publication_db_saturated")

    with pytest.raises(ResourceAdmissionTimeout, match="publication_db_saturated"):
        asyncio.run(_run_due(turn, idle_seconds=1.0, stop_event=asyncio.Event()))


def test_periodic_loop_propagates_post_work_admission_timeout() -> None:
    async def sample() -> None:
        raise ResourceAdmissionTimeout("sampler_publication_db_saturated")

    with pytest.raises(ResourceAdmissionTimeout, match="sampler_publication_db_saturated"):
        asyncio.run(_run_periodic(sample, period_seconds=1.0, stop_event=asyncio.Event()))


def test_market_poll_period_stays_below_one_hundred_thousand_monthly_calls() -> None:
    max_31_day_turns = 1 + int(31 * 24 * 60 * 60 / _MARKET_TICK_POLL_SECONDS)
    assert max_31_day_turns < 100_000


def test_claimless_domain_admission_timeouts_retry_without_killing_the_root() -> None:
    class _SaturatedDatabase:
        async def run_business(self, operation_name, function, /, *args, **kwargs):
            del function, args, kwargs
            raise ResourceAdmissionTimeout(f"saturated:{operation_name}")

    database = _SaturatedDatabase()

    macro = object.__new__(MacroAcquisition)
    macro.db = database
    macro.service = SimpleNamespace(claim_next=lambda: None)

    resolution = object.__new__(ResolutionRefresh)
    resolution.db = database

    profile = object.__new__(AssetProfileRefresh)
    profile.db = database
    profile.dex_profile_sources = (SimpleNamespace(provider="gmgn_dex_profile"),)
    profile._inactive_cleanup_complete = True
    profile._source_cursor = 0

    image = object.__new__(TokenImageMirror)
    image.db = database

    anchor = object.__new__(EventAnchorBackfill)
    anchor.db = database
    anchor.clock = lambda: 1

    poll = object.__new__(MarketTickPoll)
    poll.db = database

    async def scenario() -> None:
        assert await macro.turn() is None
        assert await resolution.turn(now_ms=1) is None
        assert await profile.turn(now_ms=1) is None
        assert await image.turn(now_ms=1) is None
        assert await anchor.turn() is None
        assert await poll.sample() is None

    asyncio.run(scenario())


def test_market_poll_provider_overrun_stays_inside_the_poll_lane() -> None:
    class _OverrunFiniteOperations:
        async def run(self, *_args, **_kwargs):
            raise ResourceOperationOverrun("resource_operation_overrun:market_tick_poll_dex")

    poll = object.__new__(MarketTickPoll)
    poll.dex_quote_market = SimpleNamespace(token_quotes=lambda _requests: ())
    poll.finite_operations = _OverrunFiniteOperations()

    result = asyncio.run(
        poll._poll_chain_targets_async(
            [
                SimpleNamespace(
                    target_id="sol:token-address",
                    chain_id="sol",
                    address="token-address",
                )
            ]
        )
    )

    assert result.ticks == []
    assert result.skipped_reasons == {"provider_timeout": 1}


def test_market_cex_poll_provider_overrun_stays_inside_the_poll_lane() -> None:
    class _OverrunFiniteOperations:
        async def run(self, *_args, **_kwargs):
            raise ResourceOperationOverrun("resource_operation_overrun:market_tick_poll_cex")

    poll = object.__new__(MarketTickPoll)
    poll.cex_market = SimpleNamespace(tickers=lambda **_kwargs: ())
    poll.finite_operations = _OverrunFiniteOperations()

    result = asyncio.run(
        poll._poll_cex_targets_async(
            [
                SimpleNamespace(
                    target_id="binance:BTCUSDT",
                    exchange="binance",
                    instrument="BTCUSDT",
                )
            ]
        )
    )

    assert result.ticks == []
    assert result.skipped_reasons == {"provider_timeout": 1}


def test_macro_provider_overrun_publishes_a_bounded_source_failure_without_releasing_claim() -> None:
    claim = {"target_key": "fred:gdp"}

    class _Database:
        def __init__(self) -> None:
            self.operations: list[str] = []
            self.failure: Exception | None = None

        async def run_business(self, operation_name, _function, /, *args, **_kwargs):
            self.operations.append(operation_name)
            if operation_name == "macro_target_claim":
                return claim
            if operation_name == "macro_publish_failure":
                self.failure = args[-1]
                return {"published": True}
            raise AssertionError(operation_name)

    database = _Database()
    macro = MacroAcquisition(
        db=database,
        finite_operations=_SubmittedOverrunFiniteOperations(),
        service=SimpleNamespace(
            claim_next=lambda: claim,
            fetch_claim=lambda _claim: None,
            publish_failure=lambda *_args: None,
        ),
    )

    assert asyncio.run(macro.turn()) is True
    assert isinstance(database.failure, MacroSourceError)
    assert str(database.failure) == "macro_fetch_total_timeout"
    assert database.operations == ["macro_target_claim", "macro_publish_failure"]


def test_macro_malformed_success_json_is_published_as_a_source_failure() -> None:
    claim = {"target_key": "bls:unemployment"}

    class _InlineFiniteOperations:
        async def run(self, _operation_name, function, /, *args, **kwargs):
            kwargs["on_submitted"]()
            return function(*args)

    class _Database:
        def __init__(self) -> None:
            self.operations: list[str] = []
            self.failure: Exception | None = None

        async def run_business(self, operation_name, function, /, *args, **_kwargs):
            self.operations.append(operation_name)
            if operation_name == "macro_target_claim":
                return claim
            if operation_name == "macro_publish_failure":
                self.failure = args[-1]
                return function(*args)
            raise AssertionError(operation_name)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"{", request=request)

    client = MacroSourceClient(transport=httpx.MockTransport(handler))
    database = _Database()
    macro = MacroAcquisition(
        db=database,
        finite_operations=_InlineFiniteOperations(),
        service=SimpleNamespace(
            claim_next=lambda: claim,
            fetch_claim=lambda _claim: client.fetch(
                require_dataset("bls.unemployment.release"),
                partition_key="latest",
                cursor={},
            ),
            publish_failure=lambda _claim, error: {
                "published": True,
                "error_code": str(error),
            },
        ),
    )

    try:
        assert asyncio.run(macro.turn()) is True
    finally:
        client.close()

    assert isinstance(database.failure, MacroSourceError)
    assert str(database.failure) == "bls_release_payload_invalid"
    assert database.operations == ["macro_target_claim", "macro_publish_failure"]


def test_news_push_delivery_overrun_records_retryable_failure_without_releasing_claim() -> None:
    payload = {"schema_version": "news_story_push_v1", "story_id": "story-1"}

    class _Database:
        def __init__(self) -> None:
            self.operations: list[str] = []

        async def run_business(self, operation_name, _function, /, *_args, **_kwargs):
            self.operations.append(operation_name)
            if operation_name == "news_story_push_claim":
                return {
                    "selected_item_id": "item-1",
                    "source_payload": {
                        "provider_evidence": {
                            "published_at_ms": 9_000_000_000_000,
                        }
                    },
                    "delivery_payload": payload,
                    "payload_fingerprint": _payload_fingerprint(payload),
                    "push_baseline_at_ms": 0,
                }
            if operation_name == "news_story_push_current_eligibility":
                return {
                    "provider_score": 90,
                    "provider_metadata": {
                        "coins": [{"symbol": "BTC", "market_type": "spot"}],
                    },
                    "published_at_ms": 9_000_000_000_000,
                    "eligibility_observed_at_ms": 1,
                }
            if operation_name == "news_story_push_start_delivery":
                return {"delivery_attempts": 1}
            raise AssertionError(operation_name)

    database = _Database()
    push = NewsStoryPush(
        db=database,
        finite_operations=_SubmittedOverrunFiniteOperations(),
        delivery=SimpleNamespace(deliver=lambda *_args, **_kwargs: None),
        runtime_id="runtime",
    )
    failures: list[dict[str, object]] = []

    async def record_failure(**kwargs):
        failures.append(kwargs)
        return "retry"

    push._record_delivery_failure = record_failure  # type: ignore[method-assign]

    assert asyncio.run(push._execute_story("story-1", now_ms=1_000)) is True
    assert database.operations == [
        "news_story_push_claim",
        "news_story_push_current_eligibility",
        "news_story_push_start_delivery",
        "news_story_push_current_eligibility",
    ]
    assert len(failures) == 1
    error = failures[0]["error"]
    assert isinstance(error, NewsPushDeliveryError)
    assert error.code == "news_story_push_delivery_timeout"
    assert error.retryable is True


@pytest.mark.parametrize(
    ("attempt_count", "expected_type"),
    [(1, _RescheduleOutcome), (3, _TerminalOutcome)],
)
def test_event_anchor_provider_overrun_uses_existing_retry_budget(
    attempt_count: int,
    expected_type: type[_RescheduleOutcome] | type[_TerminalOutcome],
) -> None:
    now_ms = 100_000
    row = {
        "event_id": "event-1",
        "intent_id": "intent-1",
        "resolution_id": "resolution-1",
        "target_type": "Asset",
        "target_id": "asset-1",
        "t_event_ms": now_ms,
        "active_until_ms": now_ms + 60_000,
        "attempt_count": attempt_count,
    }

    class _Database:
        async def run_business(self, operation_name, _function, /, *_args, **_kwargs):
            assert operation_name == "event_anchor_existing_tick"

    worker = object.__new__(EventAnchorBackfill)
    worker.db = _Database()
    worker.finite_operations = _SubmittedOverrunFiniteOperations()
    worker.max_attempts = 3
    worker.max_anchor_lag_ms = 60_000

    outcome = asyncio.run(worker._capture_one(row, now_ms=now_ms, on_submitted=lambda: None))

    assert isinstance(outcome, expected_type)
    assert outcome.reason == "provider_timeout"
    if isinstance(outcome, _RescheduleOutcome):
        assert outcome.next_run_at_ms == now_ms + 10_000
    else:
        assert outcome.status == "failed"


def test_resolution_provider_overrun_uses_existing_unavailable_branch_without_releasing_claims() -> None:
    claim = {
        "lookup_key": "symbol:BTC",
        "lookup_type": "dex_symbol_lookup",
        "attempt_count": 1,
        "error_count": 0,
    }

    class _Database:
        def __init__(self) -> None:
            self.operations: list[str] = []
            self.error: Exception | None = None

        async def run_business(self, operation_name, _function, /, *args, **_kwargs):
            self.operations.append(operation_name)
            if operation_name == "resolution_claim":
                return [claim], False
            if operation_name == "resolution_publish_unavailable":
                self.error = args[-1]
                return True
            raise AssertionError(operation_name)

    database = _Database()
    worker = object.__new__(ResolutionRefresh)
    worker.db = database
    worker.finite_operations = _SubmittedOverrunFiniteOperations()
    worker.dex_discovery_market = object()
    worker.chain_ids = ("solana",)

    assert asyncio.run(worker.turn(now_ms=1_000)) is True
    assert isinstance(database.error, DexProviderTemporarilyUnavailable)
    assert str(database.error) == "resolution_provider_lookup_timeout"
    assert database.operations == ["resolution_claim", "resolution_publish_unavailable"]


@pytest.mark.parametrize(("attempt_count", "expected"), [(1, "failed"), (3, "terminal")])
def test_token_image_provider_overrun_publishes_existing_bounded_error_result(
    attempt_count: int,
    expected: str,
) -> None:
    claim = {
        "source_url": "https://images.example/token.png",
        "source_provider": "gmgn",
        "source_kind": "logo",
        "raw_ref_json": {},
        "attempt_count": attempt_count,
    }

    class _Database:
        def __init__(self) -> None:
            self.operations: list[str] = []
            self.mirror_result: dict[str, object] | None = None

        async def run_business(self, operation_name, _function, /, *args, **_kwargs):
            self.operations.append(operation_name)
            if operation_name == "token_image_claim":
                return claim, None, 1
            if operation_name == "token_image_publish":
                self.mirror_result = args[1]
                return {"queue_rows_changed": 1, "asset_rows_changed": 1}
            raise AssertionError(operation_name)

    database = _Database()
    worker = TokenImageMirror(
        db=database,
        app_home=".",
        finite_operations=_SubmittedOverrunFiniteOperations(),
        runtime_id="runtime",
    )

    assert asyncio.run(worker.turn(now_ms=1_000)) == expected
    assert database.mirror_result == {
        "status": "error",
        "error": "token_image_fetch_timeout",
    }
    assert database.operations == ["token_image_claim", "token_image_publish"]


def test_asset_profile_provider_overrun_uses_existing_failure_branch_without_releasing_claim() -> None:
    claim = {
        "provider": "gmgn_dex_profile",
        "target_id": "asset-1",
        "attempt_count": 1,
    }

    class _Database:
        def __init__(self) -> None:
            self.operations: list[str] = []
            self.error: Exception | None = None

        async def run_business(self, operation_name, _function, /, *args, **_kwargs):
            self.operations.append(operation_name)
            if operation_name == "asset_profile_claim":
                return claim, {"due": 1}
            if operation_name == "asset_profile_publish_unavailable":
                self.error = args[1]
                return {"terminal": 0}
            raise AssertionError(operation_name)

    database = _Database()
    worker = AssetProfileRefresh(
        db=database,
        finite_operations=_SubmittedOverrunFiniteOperations(),
        runtime_id="runtime",
        dex_profile_sources=(DexProfileSource(provider="gmgn_dex_profile", market=object()),),
    )
    worker._inactive_cleanup_complete = True

    assert asyncio.run(worker.turn(now_ms=1_000)) == "failed"
    assert isinstance(database.error, MarketProviderExpectedError)
    assert str(database.error) == "asset_profile_fetch_timeout"
    assert database.operations == ["asset_profile_claim", "asset_profile_publish_unavailable"]


def test_asset_profile_releases_claim_when_publication_admission_is_saturated() -> None:
    claim = {"provider": "gmgn_dex_profile", "target_id": "asset:one"}

    class _Database:
        def __init__(self) -> None:
            self.operations: list[str] = []

        async def run_business(self, operation_name, function, /, *args, **kwargs):
            del function, args, kwargs
            self.operations.append(operation_name)
            if operation_name == "asset_profile_claim":
                return claim, {"due": 1}
            if operation_name == "asset_profile_publish":
                raise ResourceAdmissionTimeout("publication_db_saturated")
            if operation_name == "asset_profile_release_prework":
                return True
            raise AssertionError(operation_name)

    class _FiniteOperations:
        async def run(self, operation_name, function, /, *args, **kwargs):
            del operation_name, function, args
            kwargs["on_submitted"]()
            return object()

    class _ProfileMarket:
        def token_profile(self, *, chain_id: str, address: str):
            del chain_id, address

        def close(self) -> None:
            return None

    database = _Database()
    profile = AssetProfileRefresh(
        db=database,
        finite_operations=_FiniteOperations(),
        runtime_id="runtime",
        dex_profile_sources=(
            DexProfileSource(
                provider="gmgn_dex_profile",
                market=_ProfileMarket(),
            ),
        ),
    )
    profile._inactive_cleanup_complete = True

    assert asyncio.run(profile.turn(now_ms=1)) is None
    assert database.operations == [
        "asset_profile_claim",
        "asset_profile_publish",
        "asset_profile_release_prework",
    ]


def test_release_prework_admission_timeout_uses_lease_recovery() -> None:
    class _SaturatedDatabase:
        async def run_business(self, operation_name, function, /, *args, **kwargs):
            del function, args, kwargs
            raise ResourceAdmissionTimeout(f"saturated:{operation_name}")

    database = _SaturatedDatabase()

    resolution = object.__new__(ResolutionRefresh)
    resolution.db = database

    async def scenario() -> None:
        assert await resolution._release_prework([{}]) is False

    asyncio.run(scenario())


def test_model_arbiter_propagates_post_model_admission_timeout() -> None:
    class _Candidate:
        async def peek(self, *, now_ms: int) -> ModelCandidate:
            return ModelCandidate(
                kind="news_brief",
                target_key="fingerprint",
                due_at_ms=now_ms,
                stable_order=1,
            )

        async def execute(self, candidate: ModelCandidate) -> bool:
            del candidate
            raise ResourceAdmissionTimeout("model_publication_db_saturated")

    async def scenario() -> None:
        with pytest.raises(ResourceAdmissionTimeout, match="model_publication_db_saturated"):
            await run_model_arbiter((_Candidate(),), stop_event=asyncio.Event())

    asyncio.run(scenario())
