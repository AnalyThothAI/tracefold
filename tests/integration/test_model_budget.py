"""News and Trading contend for one real PostgreSQL-backed provider budget."""

from __future__ import annotations

import asyncio
import multiprocessing
import time
from typing import Any

import dspy
import pytest

from tests.postgres_test_utils import postgres_migration_test_dsn
from tracefold.app.learning_runtime import compose_news_models, generative_lm
from tracefold.app.llm import configured_lm_endpoint
from tracefold.app.model_budget import ModelBudget
from tracefold.platform.config.models import PostgresConfig, Settings

pytestmark = pytest.mark.integration


def _settings(dsn: str) -> Settings:
    settings = Settings()
    settings.storage.postgres = PostgresConfig(dsn=dsn, password_file=None)
    return settings


def _process_holder(dsn: str, ready: Any) -> None:
    settings = _settings(dsn)
    settings.llm.max_shared_concurrent_calls = 1
    budget = ModelBudget(settings, resource="process-crash-fixture", timeout_s=3)
    with budget.acquire():
        ready.put(True)
        time.sleep(30)


def test_news_and_trading_models_share_total_capacity(postgres_clone_dsn: str, monkeypatch) -> None:
    async def run() -> None:
        settings = _settings(postgres_migration_test_dsn(postgres_clone_dsn))
        settings.llm.api_key = "fixture"
        settings.llm.base_url = "https://model.fixture/v1"
        settings.llm.news_triage_model = "news-model"
        settings.llm.max_shared_concurrent_calls = 2
        news = compose_news_models(settings)
        assert news is not None
        news_lm = news.extraction.lms()[0]
        trading_lm = generative_lm(
            configured_lm_endpoint(settings, model_name="trading-model"),
            settings=settings,
            max_tokens=1000,
            timeout=3,
        )
        active = peak = calls = 0

        async def provider(_self: Any, *_args: Any, **_kwargs: Any) -> list[str]:
            nonlocal active, peak, calls
            active += 1
            calls += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0.08)
                return ["fixture"]
            finally:
                active -= 1

        monkeypatch.setattr(dspy.LM, "acall", provider)
        await asyncio.gather(*(lm.acall("fixture") for lm in (news_lm, trading_lm) * 4))
        assert calls == 8 and peak == 2 and active == 0
        # A cancelled waiter cannot retain a session or an acquired slot.
        owner = ModelBudget(settings, resource="cancel-fixture", timeout_s=3)
        owner.capacity = 1
        owner.keys = owner.keys[:1]
        async with owner.acquire_async():
            waiter = asyncio.create_task(owner.acquire_async().__aenter__())
            await asyncio.sleep(0.03)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
        async with owner.acquire_async():
            pass

    asyncio.run(run())


def test_process_crash_releases_provider_slot(postgres_clone_dsn: str) -> None:
    dsn = postgres_migration_test_dsn(postgres_clone_dsn)
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    process = context.Process(target=_process_holder, args=(dsn, ready))
    process.start()
    try:
        assert ready.get(timeout=5) is True
        settings = _settings(dsn)
        settings.llm.max_shared_concurrent_calls = 1
        blocked = ModelBudget(settings, resource="process-crash-fixture", timeout_s=0.2)
        with pytest.raises(TimeoutError, match="model_shared_budget_timeout"), blocked.acquire():
            pass
        process.terminate()
        process.join(timeout=3)
        assert not process.is_alive()
        recovered = ModelBudget(settings, resource="process-crash-fixture", timeout_s=3)
        with recovered.acquire():
            pass
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=3)
        ready.close()
