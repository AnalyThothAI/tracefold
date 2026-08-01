from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from tracefold.app.model_arbiter import run_model_arbiter
from tracefold.app.workers import _run_due, _run_periodic
from tracefold.macro.runtime import MacroAcquisition
from tracefold.market.identity.resolution_refresh_worker import ResolutionRefresh
from tracefold.market.pricing.event_anchor_backfill_worker import EventAnchorBackfill
from tracefold.market.pricing.market_tick_poll_worker import MarketTickPoll
from tracefold.market.profiles.asset_profile_refresh_worker import AssetProfileRefresh
from tracefold.market.profiles.token_image_mirror_worker import TokenImageMirror
from tracefold.market.provider_contracts import DexProfileSource
from tracefold.market.radar.projection_worker import RadarProjectionCandidate
from tracefold.news.runtime import NewsBriefCandidate
from tracefold.platform.model_candidate import ModelCandidate
from tracefold.platform.resource import ResourceAdmissionTimeout


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

    radar = object.__new__(RadarProjectionCandidate)
    radar.db = database
    radar.service = SimpleNamespace(release_prework=lambda: None)

    async def scenario() -> None:
        assert await resolution._release_prework([{}]) is False
        assert await radar._release_prework(SimpleNamespace(targets=())) is False

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
