from __future__ import annotations

import asyncio
import inspect
import math
from types import SimpleNamespace
from typing import Any

import pytest

from tracefold.app import workers
from tracefold.app.market_providers import AssetMarketProviders
from tracefold.macro import MacroProjectionCandidate
from tracefold.news import NewsPushReceipt, NewsStoryProjection
from tracefold.news.projection import NewsProjectionSnapshot
from tracefold.news.push import NewsStoryPush
from tracefold.news.query_specs import story_push_reconcile_page_query
from tracefold.news.story_store import NEWS_STORY_INPUT_ROW_CAP
from tracefold.platform.config.settings import Settings
from tracefold.platform.resource import ResourceAdmissionTimeout


class _WiringDatabase:
    def __init__(self) -> None:
        self.heavy = object()

    def heavy_business(self) -> object:
        return self.heavy


def test_story_projection_and_push_reconcile_have_independent_periodic_samples() -> None:
    calls: list[tuple[str, int | None]] = []

    class _Story:
        async def sample(self) -> None:
            calls.append(("story", None))

    class _Push:
        async def reconcile(self, *, now_ms: int) -> dict[str, int]:
            calls.append(("push", now_ms))
            return {"inserted": 0}

    asyncio.run(workers._sample_news_story(news_story=_Story()))  # type: ignore[arg-type]
    asyncio.run(workers._sample_news_push(news_push=_Push()))  # type: ignore[arg-type]

    assert [name for name, _value in calls] == ["story", "push"]
    assert calls[1][1] is not None
    assert workers._NEWS_PUSH_RECONCILE_SECONDS == 2.5


def test_unchanged_story_sample_refreshes_only_the_projection_clock() -> None:
    operations: list[tuple[str, tuple[object, ...]]] = []
    snapshot = NewsProjectionSnapshot(
        input_fingerprint="same",
        scoring_epoch_ms=0,
        current_input_fingerprint="same",
        rows=(),
    )

    class _Database:
        async def run_business(self, operation_name, function, /, *args, **kwargs):
            del function, kwargs
            operations.append((operation_name, args))
            return snapshot if operation_name == "news_story_load" else {"projection_status": "unchanged_input"}

    projection = NewsStoryProjection(db=_Database(), heavy_db=_Database(), cpu=object())
    asyncio.run(projection.sample())

    assert [name for name, _args in operations] == ["news_story_load", "news_story_publish"]
    assert operations[1][1] == (snapshot, {})


def test_push_reconcile_full_cursor_cycle_has_a_bounded_nominal_cadence() -> None:
    page_query = story_push_reconcile_page_query()
    probe_limit, page_size = page_query.params

    assert probe_limit == page_size + 1
    assert page_size <= 1_000
    assert math.ceil(NEWS_STORY_INPUT_ROW_CAP / page_size) * workers._NEWS_PUSH_RECONCILE_SECONDS <= 25.0


def test_push_periodic_sample_retries_after_database_admission_timeout() -> None:
    class _Push:
        async def reconcile(self, *, now_ms: int) -> dict[str, int]:
            assert now_ms > 0
            raise ResourceAdmissionTimeout("test_news_push_reconcile_admission_timeout")

    asyncio.run(workers._sample_news_push(news_push=_Push()))  # type: ignore[arg-type]


def test_push_due_turn_retries_after_database_admission_timeout() -> None:
    class _Database:
        async def run_business(self, *_args: Any, **_kwargs: Any) -> object:
            raise ResourceAdmissionTimeout("test_news_push_turn_admission_timeout")

    class _Delivery:
        def render(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise AssertionError("not called")

        def deliver(self, *_args: Any, **_kwargs: Any) -> NewsPushReceipt:
            raise AssertionError("not called")

        def close(self) -> None:
            raise AssertionError("not called")

    push = NewsStoryPush(
        db=_Database(),
        finite_operations=object(),
        delivery=_Delivery(),
        runtime_id="runtime-1",
    )

    assert asyncio.run(push.turn()) is None


def test_worker_root_wires_exactly_one_dedicated_push_reconcile_task() -> None:
    source = inspect.getsource(workers.run_workers)

    assert source.count('name="news-push-reconcile"') == 1
    assert source.count("lambda: _sample_news_push") == 1
    assert "if components.news_push is not None:" in source
    assert "period_seconds=_NEWS_PUSH_RECONCILE_SECONDS" in source
    assert "news_push" not in inspect.getsource(workers._sample_news_story)


def test_news_push_composition_has_no_llm_or_title_translator_dependency() -> None:
    wiring = inspect.getsource(workers._wire_components)
    parameters = inspect.signature(NewsStoryPush).parameters

    assert "DeepSeekNewsPushTranslator" not in wiring
    assert "model_adapter" not in parameters
    assert "translator" not in parameters


def test_news_story_uses_a_dedicated_cpu_lane() -> None:
    class _BriefPublisher:
        def __init__(self, **_kwargs: Any) -> None:
            return None

        def close(self) -> None:
            return None

    short_cpu = object()
    news_cpu = object()
    database = _WiringDatabase()
    settings = Settings(
        upstream={"channels": []},
        news={"enabled": True},
        providers={"binance": {"enabled": False}, "macro_sources": {"enabled": False}},
    )

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(workers, "wire_asset_market", lambda _settings: AssetMarketProviders())
        monkeypatch.setattr(workers, "ProviderChainNewsBriefPublisher", _BriefPublisher)
        components = asyncio.run(
            workers._wire_components(
                settings=settings,
                db=database,  # type: ignore[arg-type]
                telemetry=object(),  # type: ignore[arg-type]
                finite=object(),  # type: ignore[arg-type]
                model_adapter=object(),  # type: ignore[arg-type]
                projection_cpu=short_cpu,  # type: ignore[arg-type]
                news_cpu=news_cpu,  # type: ignore[arg-type]
                runtime_id="runtime-dedicated-news-cpu",
            )
        )

    assert components.news_story is not None
    assert components.news_story.cpu is news_cpu
    assert components.news_story.db is database
    assert components.news_story.heavy_db is database.heavy
    assert components.radar_current.cpu is short_cpu
    assert components.radar_current.db is database
    assert components.radar_current.heavy_db is database.heavy
    assert all(projection.cpu is short_cpu for projection in components.projections)


def test_world_brief_wiring_uses_only_the_pinned_public_provider_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publisher_kwargs: list[dict[str, Any]] = []

    class _BriefPublisher:
        def __init__(self, **kwargs: Any) -> None:
            publisher_kwargs.append(kwargs)

        def close(self) -> None:
            return None

    monkeypatch.setattr(workers, "wire_asset_market", lambda _settings: AssetMarketProviders())
    monkeypatch.setattr(workers, "ProviderChainNewsBriefPublisher", _BriefPublisher)
    settings = Settings(
        upstream={"channels": []},
        llm={
            "api_key": "deepseek-secret",
            "base_url": "https://deepseek.test/v1",
            "news_brief_model": "deepseek-chat",
            "groq_api_key": "groq-secret",
        },
        news={"enabled": True},
        providers={"binance": {"enabled": False}, "macro_sources": {"enabled": False}},
    )

    components = asyncio.run(
        workers._wire_components(
            settings=settings,
            db=_WiringDatabase(),  # type: ignore[arg-type]
            telemetry=object(),  # type: ignore[arg-type]
            finite=object(),  # type: ignore[arg-type]
            model_adapter=object(),  # type: ignore[arg-type]
            projection_cpu=object(),  # type: ignore[arg-type]
            news_cpu=object(),  # type: ignore[arg-type]
            runtime_id="runtime-public-brief",
        )
    )

    assert components.news_brief is not None
    assert publisher_kwargs == [
        {
            "ollama_base_url": "http://host.docker.internal:11434/v1",
            "configured_base_url": "https://deepseek.test/v1",
            "configured_api_key": "deepseek-secret",
            "configured_model": "deepseek-chat",
            "groq_api_key": "groq-secret",
            "total_timeout_seconds": 60.0,
        }
    ]


@pytest.mark.parametrize(("rss_enabled", "expected_source_count"), ((False, 0), (True, 179)))
def test_rss_catalog_is_wired_only_when_explicitly_enabled(
    monkeypatch: pytest.MonkeyPatch,
    rss_enabled: bool,
    expected_source_count: int,
) -> None:
    class _BriefPublisher:
        def __init__(self, **_kwargs: Any) -> None:
            return None

        def close(self) -> None:
            return None

    monkeypatch.setattr(workers, "wire_asset_market", lambda _settings: AssetMarketProviders())
    monkeypatch.setattr(workers, "ProviderChainNewsBriefPublisher", _BriefPublisher)
    settings = Settings(
        upstream={"channels": []},
        news={"enabled": True, "rss_enabled": rss_enabled},
        providers={"binance": {"enabled": False}, "macro_sources": {"enabled": False}},
    )

    components = asyncio.run(
        workers._wire_components(
            settings=settings,
            db=_WiringDatabase(),  # type: ignore[arg-type]
            telemetry=object(),  # type: ignore[arg-type]
            finite=object(),  # type: ignore[arg-type]
            model_adapter=object(),  # type: ignore[arg-type]
            projection_cpu=object(),  # type: ignore[arg-type]
            news_cpu=object(),  # type: ignore[arg-type]
            runtime_id=f"runtime-rss-{rss_enabled}",
        )
    )

    assert components.news is not None
    assert len(components.news.rss_sources) == expected_source_count
    # The acquisition turn remains scheduled for deterministic Item expiry,
    # but an empty catalog cannot produce an RSS network claim.
    assert any(getattr(turn, "__self__", None) is components.news for turn, _idle in components.due_turns)


def test_startup_reconcile_publishes_token_radar_before_competing_business_work() -> None:
    calls: list[tuple[str, int | None]] = []

    class _Radar:
        async def sample(self) -> None:
            calls.append(("radar", None))

    class _News:
        async def reconcile(self) -> None:
            calls.append(("news", None))

    class _Push:
        async def reconcile(self, *, now_ms: int) -> dict[str, int]:
            calls.append(("push", now_ms))
            return {"inserted": 0}

    class _ProfileRefresh:
        async def reconcile(self) -> None:
            calls.append(("profile", None))

    class _Database:
        async def run_business(self, operation_name, _function, /, *_args, **_kwargs):
            assert operation_name == "macro_projection_reconcile"
            calls.append(("macro_projection", None))
            return 0

    macro_projection = MacroProjectionCandidate(
        db=_Database(),
        cpu=object(),
        runtime_id="a3b1f67c-6f83-4c7f-9ea4-aab4b652e343",
    )

    components = SimpleNamespace(
        radar_current=_Radar(),
        asset_profile_refresh=_ProfileRefresh(),
        news=_News(),
        news_push=_Push(),
        macro_turns=(),
        projections=(macro_projection,),
        document_model=None,
    )

    asyncio.run(workers._reconcile_once(components))  # type: ignore[arg-type]

    assert [name for name, _value in calls] == [
        "radar",
        "profile",
        "news",
        "push",
        "macro_projection",
    ]
    assert calls[3][1] is not None


@pytest.mark.parametrize(
    ("news_enabled", "push_enabled", "llm_configured", "expected_push"),
    (
        (False, False, False, False),
        (True, False, True, False),
        (True, True, False, True),
        (True, True, True, True),
    ),
)
def test_push_wiring_requires_news_and_push_and_reuses_global_llm(
    monkeypatch: pytest.MonkeyPatch,
    news_enabled: bool,
    push_enabled: bool,
    llm_configured: bool,
    expected_push: bool,
) -> None:
    constructed: list[tuple[str, dict[str, Any]]] = []

    class _Delivery:
        def __init__(self, **kwargs: Any) -> None:
            constructed.append(("delivery", kwargs))

        def render(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {"card": True}

        def deliver(self, *_args: Any, **_kwargs: Any) -> NewsPushReceipt:
            return NewsPushReceipt(provider="feishu")

        def close(self) -> None:
            return None

    class _BriefPublisher:
        def __init__(self, **_kwargs: Any) -> None:
            return None

        def close(self) -> None:
            return None

    monkeypatch.setattr(workers, "wire_asset_market", lambda _settings: AssetMarketProviders())
    monkeypatch.setattr(workers, "configured_profile_provider_ids", lambda _settings: ())
    monkeypatch.setattr(workers, "gmgn_upstream_client", lambda _settings, **_kwargs: None)
    monkeypatch.setattr(workers, "FeishuNewsPushDelivery", _Delivery)
    monkeypatch.setattr(workers, "ProviderChainNewsBriefPublisher", _BriefPublisher)

    settings = Settings(
        llm=(
            {
                "api_key": "translation-secret",
                "base_url": "https://translator.test/v1",
                "news_brief_model": "fast-title-translator",
            }
            if llm_configured
            else {}
        ),
        news={
            "enabled": news_enabled,
            "push": {
                "enabled": push_enabled,
                "feishu_webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/test-hook",
            },
        },
        providers={"macro_sources": {"enabled": False}},
    )

    finite = object()
    components = asyncio.run(
        workers._wire_components(
            settings=settings,
            db=_WiringDatabase(),  # type: ignore[arg-type]
            telemetry=object(),  # type: ignore[arg-type]
            finite=finite,  # type: ignore[arg-type]
            model_adapter=object(),  # type: ignore[arg-type]
            projection_cpu=object(),  # type: ignore[arg-type]
            news_cpu=object() if news_enabled else None,  # type: ignore[arg-type]
            runtime_id="runtime-1",
        )
    )

    assert (components.news_push is not None) is expected_push
    assert not hasattr(components, "news_title_translation")
    if expected_push:
        assert [name for name, _kwargs in constructed] == ["delivery"]
        assert components.news_push is not None
        assert components.news_push not in components.models
        assert components.news_push in {getattr(turn, "__self__", None) for turn, _idle_seconds in components.due_turns}
        assert constructed[0][1] == {
            "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/test-hook",
            "signing_secret": None,
            "finite_operations": finite,
            "translation_enabled": llm_configured,
            "translation_base_url": "https://translator.test/v1" if llm_configured else None,
            "translation_api_key": "translation-secret" if llm_configured else None,
            "translation_engine": "fast-title-translator" if llm_configured else None,
        }
    else:
        assert constructed == []


def test_empty_gmgn_channels_do_not_construct_a_collector(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workers, "wire_asset_market", lambda _settings: AssetMarketProviders())

    def fail_if_constructed(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("disabled GMGN stream must not construct a client")

    monkeypatch.setattr(workers, "gmgn_upstream_client", fail_if_constructed)
    settings = Settings(
        upstream={"channels": []},
        news={"enabled": False},
        providers={
            "binance": {"enabled": False},
            "macro_sources": {"enabled": False},
        },
    )

    components = asyncio.run(
        workers._wire_components(
            settings=settings,
            db=_WiringDatabase(),  # type: ignore[arg-type]
            telemetry=object(),  # type: ignore[arg-type]
            finite=object(),  # type: ignore[arg-type]
            model_adapter=object(),  # type: ignore[arg-type]
            projection_cpu=object(),  # type: ignore[arg-type]
            news_cpu=None,
            runtime_id="runtime-disabled-gmgn",
        )
    )

    assert components.collector is None


def test_news_story_push_closes_delivery_through_finite_capability() -> None:
    calls: list[tuple[str, bool]] = []
    closed: list[str] = []

    class _Capability:
        async def run(self, operation_name: str, function, /, *_args: Any, **kwargs: Any) -> None:
            calls.append((operation_name, bool(kwargs["allow_shutdown"])))
            function()

    class _Delivery:
        def render(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise AssertionError("not called")

        def deliver(self, *_args: Any, **_kwargs: Any) -> NewsPushReceipt:
            raise AssertionError("not called")

        def close(self) -> None:
            closed.append("delivery")

    push = NewsStoryPush(
        db=object(),
        finite_operations=_Capability(),
        delivery=_Delivery(),
        runtime_id="runtime-1",
    )

    asyncio.run(push.close())

    assert calls == [("news_story_push_delivery_close", True)]
    assert closed == ["delivery"]


def test_graceful_cleanup_closes_news_push_before_capability_drain() -> None:
    calls: list[str] = []

    class _Database:
        def close_business_admission(self) -> None:
            calls.append("db_close_admission")

        async def drain_business(self, *, timeout_seconds: float) -> bool:
            assert timeout_seconds > 0
            calls.append("db_drain")
            return True

    class _Capability:
        def __init__(self, name: str) -> None:
            self.name = name

        def close_admission(self) -> None:
            calls.append(f"{self.name}_close_admission")

        async def drain(self, *, timeout_seconds: float) -> bool:
            assert timeout_seconds > 0
            calls.append(f"{self.name}_drain")
            return True

        def close(self) -> None:
            calls.append(f"{self.name}_close")

    class _Push:
        async def close(self) -> None:
            calls.append("push_close")

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        await workers._graceful_cleanup(
            started_at=loop.time(),
            db=_Database(),  # type: ignore[arg-type]
            finite=_Capability("finite"),  # type: ignore[arg-type]
            model_adapter=_Capability("model"),  # type: ignore[arg-type]
            projection_cpu=_Capability("cpu"),  # type: ignore[arg-type]
            news_cpu=_Capability("news_cpu"),  # type: ignore[arg-type]
            components=SimpleNamespace(
                providers=AssetMarketProviders(),
                collector=None,
                news=None,
                news_brief=None,
                news_push=_Push(),
                macro_source=None,
            ),  # type: ignore[arg-type]
        )

    asyncio.run(scenario())

    assert "push_close" in calls
    assert calls.index("push_close") < calls.index("db_drain")
    assert calls.index("push_close") < calls.index("finite_drain")
    assert calls.index("push_close") < calls.index("model_drain")
    assert calls.index("news_cpu_close_admission") < calls.index("news_cpu_drain")
    assert calls.index("news_cpu_drain") < calls.index("news_cpu_close")
