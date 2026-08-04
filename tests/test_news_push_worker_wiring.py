from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
from typing import Any

import pytest

from tracefold.app import workers
from tracefold.app.provider_types import AssetMarketProviders
from tracefold.news import NewsPushReceipt, NewsStoryPush
from tracefold.platform.config.settings import Settings
from tracefold.platform.resource import ResourceAdmissionTimeout


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
    assert workers._NEWS_PUSH_RECONCILE_SECONDS == 10.0


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


def test_startup_reconcile_initializes_push_even_without_candidates() -> None:
    calls: list[tuple[str, int | None]] = []

    class _News:
        async def reconcile(self) -> None:
            calls.append(("news", None))

    class _Push:
        async def reconcile(self, *, now_ms: int) -> dict[str, int]:
            calls.append(("push", now_ms))
            return {"inserted": 0}

    components = SimpleNamespace(
        news=_News(),
        news_push=_Push(),
        macro_turns=(),
        document_model=None,
    )

    asyncio.run(workers._reconcile_once(components))  # type: ignore[arg-type]

    assert [name for name, _value in calls] == ["news", "push"]
    assert calls[1][1] is not None


@pytest.mark.parametrize(
    ("news_enabled", "push_enabled", "translation_enabled", "expected_push"),
    (
        (False, False, False, False),
        (True, False, False, False),
        (True, True, False, True),
        (True, True, True, True),
    ),
)
def test_push_wiring_requires_both_news_and_push_enabled(
    monkeypatch: pytest.MonkeyPatch,
    news_enabled: bool,
    push_enabled: bool,
    translation_enabled: bool,
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
    monkeypatch.setattr(workers, "gmgn_upstream_factory", lambda _settings: None)
    monkeypatch.setattr(workers, "FeishuNewsPushDelivery", _Delivery)
    monkeypatch.setattr(workers, "ProviderChainNewsBriefPublisher", _BriefPublisher)

    settings = Settings(
        news={
            "enabled": news_enabled,
            "push": {
                "enabled": push_enabled,
                "feishu_webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/test-hook",
                "translation": (
                    {
                        "enabled": True,
                        "base_url": "https://translator.test/v1",
                        "api_key": "translation-secret",
                        "engine": "fast-title-translator",
                    }
                    if translation_enabled
                    else {"enabled": False}
                ),
            },
        },
        providers={"macro_sources": {"enabled": False}},
    )

    finite = object()
    components = asyncio.run(
        workers._wire_components(
            settings=settings,
            db=object(),  # type: ignore[arg-type]
            telemetry=object(),  # type: ignore[arg-type]
            finite=finite,  # type: ignore[arg-type]
            model_adapter=object(),  # type: ignore[arg-type]
            cpu=object(),  # type: ignore[arg-type]
            runtime_id="runtime-1",
        )
    )

    assert (components.news_push is not None) is expected_push
    if expected_push:
        assert [name for name, _kwargs in constructed] == ["delivery"]
        assert components.news_push is not None
        assert components.news_push not in components.models
        assert components.news_push in {getattr(turn, "__self__", None) for turn, _idle_seconds in components.due_turns}
        assert constructed[0][1] == {
            "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/test-hook",
            "signing_secret": None,
            "finite_operations": finite,
            "translation_enabled": translation_enabled,
            "translation_base_url": ("https://translator.test/v1" if translation_enabled else None),
            "translation_api_key": "translation-secret" if translation_enabled else None,
            "translation_engine": "fast-title-translator" if translation_enabled else None,
        }
    else:
        assert constructed == []


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
            cpu=_Capability("cpu"),  # type: ignore[arg-type]
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
