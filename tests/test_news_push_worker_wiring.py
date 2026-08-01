from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from tracefold.app import workers
from tracefold.app.provider_types import AssetMarketProviders
from tracefold.news import NewsPushReceipt, NewsPushTranslation, NewsStoryPush
from tracefold.platform.config.settings import Settings


def test_story_projection_completion_precedes_push_reconcile() -> None:
    calls: list[tuple[str, int | None]] = []

    class _Story:
        async def sample(self) -> None:
            calls.append(("story", None))

    class _Push:
        async def reconcile(self, *, now_ms: int) -> dict[str, int]:
            calls.append(("push", now_ms))
            return {"inserted": 0}

    asyncio.run(
        workers._sample_news_story(
            news_story=_Story(),  # type: ignore[arg-type]
            news_push=_Push(),  # type: ignore[arg-type]
        )
    )

    assert [name for name, _value in calls] == ["story", "push"]
    assert calls[1][1] is not None


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
    ("news_enabled", "push_enabled", "expected_push"),
    (
        (False, False, False),
        (True, False, False),
        (True, True, True),
    ),
)
def test_push_wiring_requires_both_news_and_push_enabled(
    monkeypatch: pytest.MonkeyPatch,
    news_enabled: bool,
    push_enabled: bool,
    expected_push: bool,
) -> None:
    constructed: list[tuple[str, dict[str, Any]]] = []

    class _Translator:
        def __init__(self, **kwargs: Any) -> None:
            constructed.append(("translator", kwargs))

        def translate_title(self, _title: str) -> NewsPushTranslation:
            return NewsPushTranslation(title_zh="标题", provider="deepseek", model="model")

        def close(self) -> None:
            return None

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
    monkeypatch.setattr(workers, "DeepSeekNewsPushTranslator", _Translator)
    monkeypatch.setattr(workers, "FeishuNewsPushDelivery", _Delivery)
    monkeypatch.setattr(workers, "ProviderChainNewsBriefPublisher", _BriefPublisher)

    settings = Settings(
        llm={"api_key": "test-key", "base_url": "https://deepseek.test/v1"},
        news={
            "enabled": news_enabled,
            "push": {
                "enabled": push_enabled,
                "feishu_webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/test-hook",
                "feishu_signing_secret": "test-signing-secret",
            },
        },
        providers={"macro_sources": {"enabled": False}},
    )

    components = asyncio.run(
        workers._wire_components(
            settings=settings,
            db=object(),  # type: ignore[arg-type]
            telemetry=object(),  # type: ignore[arg-type]
            finite=object(),  # type: ignore[arg-type]
            model_adapter=object(),  # type: ignore[arg-type]
            cpu=object(),  # type: ignore[arg-type]
            runtime_id="runtime-1",
        )
    )

    assert (components.news_push is not None) is expected_push
    if expected_push:
        assert [name for name, _kwargs in constructed] == ["translator", "delivery"]
        assert components.news_push is not None
        assert components.news_push.stable_order == 10
        assert components.news_push in components.models
        assert constructed[0][1] == {
            "api_key": "test-key",
            "base_url": "https://deepseek.test/v1",
        }
        assert constructed[1][1] == {
            "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/test-hook",
            "signing_secret": "test-signing-secret",
        }
    else:
        assert constructed == []


def test_news_story_push_closes_clients_through_shutdown_capabilities() -> None:
    calls: list[tuple[str, bool]] = []
    closed: list[str] = []

    class _Capability:
        async def run(self, operation_name: str, function, /, *_args: Any, **kwargs: Any) -> None:
            calls.append((operation_name, bool(kwargs["allow_shutdown"])))
            function()

    class _Translator:
        def translate_title(self, _title: str) -> NewsPushTranslation:
            raise AssertionError("not called")

        def close(self) -> None:
            closed.append("translator")

    class _Delivery:
        def render(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise AssertionError("not called")

        def deliver(self, *_args: Any, **_kwargs: Any) -> NewsPushReceipt:
            raise AssertionError("not called")

        def close(self) -> None:
            closed.append("delivery")

    push = NewsStoryPush(
        db=object(),
        model_adapter=_Capability(),
        finite_operations=_Capability(),
        translator=_Translator(),
        delivery=_Delivery(),
        runtime_id="runtime-1",
    )

    asyncio.run(push.close())

    assert calls == [
        ("news_story_push_translator_close", True),
        ("news_story_push_delivery_close", True),
    ]
    assert closed == ["translator", "delivery"]


def test_news_story_push_still_closes_delivery_when_translator_close_fails() -> None:
    closed: list[str] = []

    class _Capability:
        async def run(self, _operation_name: str, function, /, *_args: Any, **_kwargs: Any) -> None:
            function()

    class _Translator:
        def translate_title(self, _title: str) -> NewsPushTranslation:
            raise AssertionError("not called")

        def close(self) -> None:
            closed.append("translator")
            raise RuntimeError("translator_close_failed")

    class _Delivery:
        def render(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise AssertionError("not called")

        def deliver(self, *_args: Any, **_kwargs: Any) -> NewsPushReceipt:
            raise AssertionError("not called")

        def close(self) -> None:
            closed.append("delivery")

    push = NewsStoryPush(
        db=object(),
        model_adapter=_Capability(),
        finite_operations=_Capability(),
        translator=_Translator(),
        delivery=_Delivery(),
        runtime_id="runtime-1",
    )

    with pytest.raises(RuntimeError, match="translator_close_failed"):
        asyncio.run(push.close())

    assert closed == ["translator", "delivery"]


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
