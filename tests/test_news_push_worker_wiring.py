from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
from typing import Any

import pytest

from tracefold.app import workers
from tracefold.app.market_providers import AssetMarketProviders
from tracefold.macro import MacroProjectionCandidate
from tracefold.news import NewsStoryProjection
from tracefold.news.projection import NewsProjectionSnapshot
from tracefold.news.push import NewsItemPush, NewsPushReceipt
from tracefold.platform.config.settings import Settings
from tracefold.platform.resource import ResourceAdmissionTimeout


class _WiringDatabase:
    def __init__(self) -> None:
        self.heavy = object()

    def heavy_business(self) -> object:
        return self.heavy


def test_story_projection_coalesces_a_dirty_burst_into_one_additional_sample() -> None:
    async def scenario() -> int:
        dirty = asyncio.Event()
        stop = asyncio.Event()
        projection = NewsStoryProjection(
            db=object(),
            heavy_db=object(),
            cpu=object(),
            dirty=dirty,
            debounce_seconds=0.010,
            safety_seconds=10.0,
        )
        samples = 0

        async def sample() -> None:
            nonlocal samples
            samples += 1
            if samples == 2:
                stop.set()

        projection.sample = sample  # type: ignore[method-assign]
        task = asyncio.create_task(projection.run(stop_event=stop))
        await asyncio.sleep(0)
        dirty.set()
        await asyncio.sleep(0.002)
        dirty.set()
        await task
        return samples

    assert asyncio.run(scenario()) == 2


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


def test_story_projection_retries_after_database_admission_timeout() -> None:
    async def scenario() -> tuple[int, int]:
        dirty = asyncio.Event()
        stop = asyncio.Event()
        snapshot = NewsProjectionSnapshot(
            input_fingerprint="same",
            scoring_epoch_ms=0,
            current_input_fingerprint="same",
            rows=(),
        )

        class _Database:
            def __init__(self) -> None:
                self.loads = 0
                self.publishes = 0

            async def run_business(self, operation_name, function, /, *args, **kwargs):
                del function, args, kwargs
                if operation_name == "news_story_load":
                    self.loads += 1
                    if self.loads == 1:
                        raise ResourceAdmissionTimeout("news_story_load_busy")
                    return snapshot
                self.publishes += 1
                stop.set()
                return {"projection_status": "unchanged_input"}

        database = _Database()
        projection = NewsStoryProjection(
            db=database,
            heavy_db=database,
            cpu=object(),
            dirty=dirty,
            debounce_seconds=0.001,
            safety_seconds=10.0,
        )
        await projection.run(stop_event=stop)
        return database.loads, database.publishes

    assert asyncio.run(scenario()) == (2, 1)


def test_push_startup_reconcile_propagates_database_admission_timeout() -> None:
    class _Push:
        async def reconcile(self) -> dict[str, int]:
            raise ResourceAdmissionTimeout("test_news_push_reconcile_admission_timeout")

    push = _Push()
    with pytest.raises(ResourceAdmissionTimeout):
        asyncio.run(push.reconcile())


def test_push_due_turn_retries_after_database_admission_timeout() -> None:
    class _Database:
        async def run_business(self, *_args: Any, **_kwargs: Any) -> object:
            raise ResourceAdmissionTimeout("test_news_push_turn_admission_timeout")

    class _Sender:
        def send(self, *_args: Any, **_kwargs: Any) -> NewsPushReceipt:
            raise AssertionError("not called")

        def close(self) -> None:
            raise AssertionError("not called")

    push = NewsItemPush(
        db=_Database(),
        finite_operations=object(),
        translator=None,
        sender=_Sender(),
        delivery_available=True,
    )

    assert asyncio.run(push.turn()) is None


def test_translation_absolute_deadline_cancels_without_using_finite_operations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Database:
        def __init__(self) -> None:
            self.presentation: dict[str, Any] | None = None

        async def run_business(
            self,
            operation_name: str,
            _function: Any,
            /,
            *args: Any,
            **_kwargs: Any,
        ) -> object:
            if operation_name == "news_item_push_peek":
                return {
                    "item_id": "news_item_0123456789abcdef0123456789abcdef",
                    "source_payload": {
                        "schema_version": "news_item_push_v1",
                        "original_title": "Bitcoin rises 5%",
                    },
                }
            if operation_name == "news_item_push_fence":
                self.presentation = dict(args[1])
                return {"item_id": args[0]}
            if operation_name == "news_item_push_complete":
                return True
            raise AssertionError(operation_name)

    class _Finite:
        def __init__(self) -> None:
            self.operations: list[str] = []

        async def run(
            self,
            operation_name: str,
            function: Any,
            /,
            *args: Any,
            **kwargs: Any,
        ) -> object:
            self.operations.append(operation_name)
            assert operation_name == "news_item_push_feishu_send"
            on_submitted = kwargs.get("on_submitted")
            if on_submitted is not None:
                on_submitted()
            return function(*args)

    class _Translator:
        def __init__(self) -> None:
            self.cancelled = False

        async def translate(self, _title: str) -> str:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise

        async def close(self) -> None:
            return None

    class _Sender:
        def send(self, *_args: Any, **_kwargs: Any) -> NewsPushReceipt:
            return NewsPushReceipt(provider="feishu")

        def close(self) -> None:
            return None

    monkeypatch.setattr("tracefold.news.push._TRANSLATION_TOTAL_TIMEOUT_SECONDS", 0.01)
    database = _Database()
    finite = _Finite()
    translator = _Translator()
    push = NewsItemPush(
        db=database,
        finite_operations=finite,
        translator=translator,
        sender=_Sender(),
        delivery_available=True,
    )

    assert asyncio.run(push.turn()) is True
    assert translator.cancelled is True
    assert finite.operations == ["news_item_push_feishu_send"]
    assert database.presentation is not None
    duration_ms = database.presentation.pop("translation_duration_ms")
    assert isinstance(duration_ms, int) and duration_ms >= 0
    assert database.presentation == {
        "display_title": "Bitcoin rises 5%",
        "fallback_code": "news_item_push_translation_timeout",
        "outcome": "fallback",
        "translation_policy_version": "title_zh_v4",
    }


def test_stale_item_push_turn_that_loses_fence_sends_nothing() -> None:
    class _Database:
        def __init__(self) -> None:
            self.operations: list[str] = []

        async def run_business(
            self,
            operation_name: str,
            _function: Any,
            /,
            *args: Any,
            **_kwargs: Any,
        ) -> object:
            self.operations.append(operation_name)
            if operation_name == "news_item_push_peek":
                return {
                    "item_id": "news_item_0123456789abcdef0123456789abcdef",
                    "source_payload": {
                        "schema_version": "news_item_push_v1",
                        "original_title": "Bitcoin rises",
                    },
                }
            if operation_name == "news_item_push_fence":
                return None
            raise AssertionError(operation_name)

    class _Sender:
        def send(self, *_args: Any, **_kwargs: Any) -> NewsPushReceipt:
            raise AssertionError("stale turn must not send")

        def close(self) -> None:
            return None

    database = _Database()
    push = NewsItemPush(
        db=database,
        finite_operations=object(),
        translator=None,
        sender=_Sender(),
        delivery_available=True,
    )

    assert asyncio.run(push.turn()) is False
    assert database.operations == ["news_item_push_peek", "news_item_push_fence"]


def test_chinese_item_title_skips_translation_and_freezes_not_needed() -> None:
    class _Database:
        def __init__(self) -> None:
            self.presentation: dict[str, Any] | None = None

        async def run_business(
            self,
            operation_name: str,
            _function: Any,
            /,
            *args: Any,
            **_kwargs: Any,
        ) -> object:
            if operation_name == "news_item_push_peek":
                return {
                    "item_id": "news_item_0123456789abcdef0123456789abcdef",
                    "source_payload": {
                        "schema_version": "news_item_push_v1",
                        "original_title": "比特币现货 ETF 资金流入",
                    },
                }
            if operation_name == "news_item_push_fence":
                self.presentation = dict(args[1])
                return {"item_id": args[0]}
            if operation_name == "news_item_push_complete":
                return True
            raise AssertionError(operation_name)

    class _InlineFinite:
        async def run(self, _name: str, function: Any, /, *args: Any, **_kwargs: Any) -> object:
            return function(*args)

    class _Translator:
        async def translate(self, _title: str) -> str:
            raise AssertionError("Chinese title must not be translated")

        async def close(self) -> None:
            return None

    class _Sender:
        def send(self, *_args: Any, **_kwargs: Any) -> NewsPushReceipt:
            return NewsPushReceipt(provider="feishu")

        def close(self) -> None:
            return None

    database = _Database()
    push = NewsItemPush(
        db=database,
        finite_operations=_InlineFinite(),
        translator=_Translator(),
        sender=_Sender(),
        delivery_available=True,
    )

    assert asyncio.run(push.turn()) is True
    assert database.presentation == {
        "display_title": "比特币现货 ETF 资金流入",
        "outcome": "not_needed",
        "translation_policy_version": "title_zh_v4",
    }


def test_long_item_title_skips_translation_and_falls_back_without_blocking() -> None:
    title = "Bitcoin " + "x" * 501

    class _Database:
        def __init__(self) -> None:
            self.presentation: dict[str, Any] | None = None

        async def run_business(
            self,
            operation_name: str,
            _function: Any,
            /,
            *args: Any,
            **_kwargs: Any,
        ) -> object:
            if operation_name == "news_item_push_peek":
                return {
                    "item_id": "news_item_0123456789abcdef0123456789abcdef",
                    "source_payload": {
                        "schema_version": "news_item_push_v1",
                        "original_title": title,
                    },
                }
            if operation_name == "news_item_push_fence":
                self.presentation = dict(args[1])
                return {"item_id": args[0]}
            if operation_name == "news_item_push_complete":
                return True
            raise AssertionError(operation_name)

    class _InlineFinite:
        async def run(self, _name: str, function: Any, /, *args: Any, **_kwargs: Any) -> object:
            return function(*args)

    class _Translator:
        async def translate(self, _title: str) -> str:
            raise AssertionError("oversized title must not be translated")

        async def close(self) -> None:
            return None

    class _Sender:
        def send(self, *_args: Any, **_kwargs: Any) -> NewsPushReceipt:
            return NewsPushReceipt(provider="feishu")

        def close(self) -> None:
            return None

    database = _Database()
    push = NewsItemPush(
        db=database,
        finite_operations=_InlineFinite(),
        translator=_Translator(),
        sender=_Sender(),
        delivery_available=True,
    )

    assert asyncio.run(push.turn()) is True
    assert database.presentation == {
        "display_title": title,
        "fallback_code": "news_item_push_translation_input_too_long",
        "outcome": "fallback",
        "translation_policy_version": "title_zh_v4",
    }


def test_worker_root_has_no_push_reconcile_ring() -> None:
    source = inspect.getsource(workers.run_workers)

    assert 'name="news-push-reconcile"' not in source
    assert "_NEWS_PUSH_RECONCILE_SECONDS" not in source
    assert "story_push_reconcile_page" not in source


def test_news_item_push_exposes_only_one_turn_operation() -> None:
    wiring = inspect.getsource(workers._wire_components)
    source = inspect.getsource(NewsItemPush)

    assert "DeepSeekNewsPushTranslator" not in wiring
    assert "prepare(" not in source
    assert "deliver(" not in source


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
    assert not hasattr(components.radar_current, "heavy_db")
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


def test_startup_reconcile_excludes_token_radar_and_reconciles_other_business_work() -> None:
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
        "profile",
        "news",
        "push",
        "macro_projection",
    ]
    assert calls[2][1] is not None


@pytest.mark.parametrize(
    ("news_enabled", "push_enabled", "llm_configured", "expected_delivery"),
    (
        (False, False, False, False),
        (True, False, True, False),
        (True, True, False, True),
        (True, True, True, True),
    ),
)
def test_push_wiring_is_always_reconciled_and_reuses_global_llm(
    monkeypatch: pytest.MonkeyPatch,
    news_enabled: bool,
    push_enabled: bool,
    llm_configured: bool,
    expected_delivery: bool,
) -> None:
    constructed: list[tuple[str, dict[str, Any]]] = []

    class _Sender:
        def __init__(self, **kwargs: Any) -> None:
            constructed.append(("sender", kwargs))

        def send(self, *_args: Any, **_kwargs: Any) -> NewsPushReceipt:
            return NewsPushReceipt(provider="feishu")

        def close(self) -> None:
            return None

    class _Translator:
        def __init__(self, **kwargs: Any) -> None:
            constructed.append(("translator", kwargs))

        async def translate(self, title: str) -> str:
            return title

        async def close(self) -> None:
            return None

    class _BriefPublisher:
        def __init__(self, **_kwargs: Any) -> None:
            return None

        def close(self) -> None:
            return None

    monkeypatch.setattr(workers, "wire_asset_market", lambda _settings: AssetMarketProviders())
    monkeypatch.setattr(workers, "configured_profile_provider_ids", lambda _settings: ())
    monkeypatch.setattr(workers, "gmgn_upstream_client", lambda _settings, **_kwargs: None)
    monkeypatch.setattr(workers, "FeishuNewsPushSender", _Sender)
    monkeypatch.setattr(workers, "OpenAICompatibleNewsPushTranslator", _Translator)
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

    assert components.news_push is not None
    assert not hasattr(components, "news_title_translation")
    if expected_delivery:
        assert [name for name, _kwargs in constructed] == (["translator", "sender"] if llm_configured else ["sender"])
        assert components.news_push not in components.models
        assert components.news_push in {getattr(turn, "__self__", None) for turn, _idle_seconds in components.due_turns}
        assert constructed[-1][1] == {
            "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/test-hook",
            "signing_secret": None,
        }
    else:
        assert constructed == []
        assert components.news_push not in {
            getattr(turn, "__self__", None) for turn, _idle_seconds in components.due_turns
        }


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


def test_news_item_push_closes_async_translator_directly_and_sender_through_finite() -> None:
    calls: list[tuple[str, bool]] = []
    closed: list[str] = []

    class _Capability:
        async def run(self, operation_name: str, function, /, *_args: Any, **kwargs: Any) -> None:
            calls.append((operation_name, bool(kwargs["allow_shutdown"])))
            function()

    class _Sender:
        def send(self, *_args: Any, **_kwargs: Any) -> NewsPushReceipt:
            raise AssertionError("not called")

        def close(self) -> None:
            closed.append("sender")

    class _Translator:
        async def translate(self, _title: str) -> str:
            raise AssertionError("not called")

        async def close(self) -> None:
            closed.append("translator")

    push = NewsItemPush(
        db=object(),
        finite_operations=_Capability(),
        translator=_Translator(),
        sender=_Sender(),
        delivery_available=True,
    )

    asyncio.run(push.close())

    assert calls == [("news_item_push_sender_close", True)]
    assert closed == ["translator", "sender"]


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
