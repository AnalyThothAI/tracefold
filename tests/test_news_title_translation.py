from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from tracefold.integrations.news_ai import OpenAiCompatibleNewsTitleTranslator
from tracefold.news.title_translation import (
    NewsStoryTitleTranslationCandidate,
    NewsTitleTranslationExpectedError,
    NewsTitleTranslationResult,
    looks_zh_cn_title,
)
from tracefold.platform.model_candidate import ModelCandidate


def _response(title: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": json.dumps(
                            {"translated_title": title},
                            ensure_ascii=False,
                        )
                    },
                }
            ]
        },
    )


def test_title_translator_uses_exact_display_title_and_preserves_anchors() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return _response("BTC 和 ETH 上涨 10%")

    translator = OpenAiCompatibleNewsTitleTranslator(
        base_url="https://llm.example/v1",
        api_key="secret",
        model="model-a",
        transport=httpx.MockTransport(handler),
    )
    try:
        result = translator.translate("BTC and ETH climb 10%")
    finally:
        translator.close()

    user_payload = json.loads(captured["messages"][1]["content"])
    assert user_payload == {
        "source_title": "BTC and ETH climb 10%",
        "required_verbatim": ["BTC", "ETH", "10%"],
    }
    assert result.title_zh == "BTC 和 ETH 上涨 10%"
    assert result.provider == "openai_compatible"
    assert result.model == "model-a"


def test_title_translator_does_not_treat_news_acronyms_as_token_symbols() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return _response("美国天然气价格在第四季度库存报告前跌至14周低点")

    translator = OpenAiCompatibleNewsTitleTranslator(
        base_url="https://llm.example/v1",
        api_key="secret",
        model="model-a",
        transport=httpx.MockTransport(handler),
    )
    try:
        result = translator.translate("US natgas prices fall to 14-week low before Q4 storage report")
    finally:
        translator.close()

    user_payload = json.loads(captured["messages"][1]["content"])
    assert user_payload["required_verbatim"] == ["14"]
    assert result.title_zh == "美国天然气价格在第四季度库存报告前跌至14周低点"


def test_title_translator_preserves_contextual_exchange_and_crypto_tickers() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return _response("Eightco（NASDAQ: ORBS）持有约 $378 百万美元、16,000 ETH 和 302 百万 WLD")

    translator = OpenAiCompatibleNewsTitleTranslator(
        base_url="https://llm.example/v1",
        api_key="secret",
        model="model-a",
        transport=httpx.MockTransport(handler),
    )
    try:
        translator.translate(
            "Eightco Holdings (NASDAQ: ORBS) reports about $378 million, 16,000 ETH and 302 million WLD"
        )
    finally:
        translator.close()

    user_payload = json.loads(captured["messages"][1]["content"])
    assert user_payload["required_verbatim"] == ["ORBS", "$378", "378", "16,000", "ETH", "302", "WLD"]


@pytest.mark.parametrize(
    ("translated", "expected_code"),
    [
        ("BTC and ETH climb 10%", "news_title_translation_output_invalid"),
        ("BTC 和以太坊上涨 11%", "news_title_translation_anchors_changed"),
    ],
)
def test_title_translator_preserves_specific_validation_error_codes(
    translated: str,
    expected_code: str,
) -> None:
    translator = OpenAiCompatibleNewsTitleTranslator(
        base_url="https://llm.example/v1",
        api_key="secret",
        model="model-a",
        transport=httpx.MockTransport(lambda _request: _response(translated)),
    )
    try:
        with pytest.raises(NewsTitleTranslationExpectedError) as raised:
            translator.translate("BTC and ETH climb 10%")
    finally:
        translator.close()

    assert raised.value.code == expected_code
    assert raised.value.retryable is False


def test_zh_cn_detection_does_not_bypass_japanese_or_korean() -> None:
    assert looks_zh_cn_title("比特币价格上涨") is True
    assert looks_zh_cn_title("ビットコイン価格上昇") is False
    assert looks_zh_cn_title("비트코인 가격 상승") is False


def test_candidate_exports_now_as_due_time_instead_of_backlog_age() -> None:
    class _Database:
        async def run_business(
            self,
            operation: str,
            _callback: object,
            *_args: object,
            **_kwargs: object,
        ) -> dict[str, str]:
            assert operation == "news_title_translation_peek"
            return {
                "story_id": "a" * 64,
                "source_title_fingerprint": "b" * 64,
            }

    candidate = NewsStoryTitleTranslationCandidate(
        db=_Database(),
        model_adapter=object(),
        translator=None,
        runtime_id="runtime",
    )

    observed = asyncio.run(candidate.peek(now_ms=1_234_567))

    assert observed is not None
    assert observed.due_at_ms == 1_234_567
    assert observed.stable_order == 25


def test_candidate_performs_model_io_between_separate_database_operations() -> None:
    calls: list[str] = []

    class _Database:
        active = False

        async def run_business(
            self,
            operation: str,
            _callback: object,
            *_args: object,
            **_kwargs: object,
        ) -> object:
            assert self.active is False
            self.active = True
            calls.append(operation)
            try:
                if operation == "news_title_translation_claim":
                    return {
                        "story_id": "a" * 64,
                        "source_title_fingerprint": "b" * 64,
                        "source_title": "Bitcoin rises",
                        "lease_owner": "owner",
                        "lease_token": "token",
                    }
                if operation == "news_title_translation_complete":
                    return True
                raise AssertionError(operation)
            finally:
                self.active = False

    database = _Database()

    class _Translator:
        def translate(self, source_title: str) -> NewsTitleTranslationResult:
            assert database.active is False
            calls.append("model_io")
            assert source_title == "Bitcoin rises"
            return NewsTitleTranslationResult(
                title_zh="比特币上涨",
                provider="test",
                model="test-model",
            )

        def close(self) -> None:
            return None

    class _ModelAdapter:
        async def run(self, _operation: str, function, *args: object, **_kwargs: object):
            return function(*args)

    candidate = NewsStoryTitleTranslationCandidate(
        db=database,
        model_adapter=_ModelAdapter(),
        translator=_Translator(),
        runtime_id="runtime",
    )

    progressed = asyncio.run(
        candidate.execute(
            ModelCandidate(
                kind="news_story_title_translation",
                target_key=f"{'a' * 64}:{'b' * 64}",
                due_at_ms=1,
                stable_order=25,
            )
        )
    )

    assert progressed is True
    assert calls == [
        "news_title_translation_claim",
        "model_io",
        "news_title_translation_complete",
    ]
