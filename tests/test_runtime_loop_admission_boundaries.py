from __future__ import annotations

import asyncio
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
from tracefold.news.runtime import NewsAcquisition
from tracefold.platform.model_candidate import ModelCandidate
from tracefold.platform.resource import ResourceAdmissionTimeout


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

    news = object.__new__(NewsAcquisition)
    news.db = database

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
        assert await news.turn() is None
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
