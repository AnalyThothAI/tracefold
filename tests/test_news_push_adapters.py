from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from tracefold.integrations.feishu import generate_feishu_signature
from tracefold.integrations.news_push import (
    FeishuNewsPushSender,
    OpenAICompatibleNewsPushTranslator,
)
from tracefold.news.push import PUSH_TRANSLATION_DEADLINE_SECONDS, NewsPushExternalError

_FEISHU_TEST_URL = "https://open.feishu.cn/open-apis/bot/v2/hook/test-hook-id"


def test_translator_makes_one_plain_text_openai_compatible_request() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": "比特币上涨 5%"},
                    }
                ]
            },
        )

    translator = OpenAICompatibleNewsPushTranslator(
        base_url="https://translator.test/v1/",
        api_key="secret",
        model="fast-translator",
        transport=httpx.MockTransport(respond),
    )

    async def scenario() -> str:
        try:
            return await translator.translate("Bitcoin rises 5%")
        finally:
            await translator.close()

    assert asyncio.run(scenario()) == "比特币上涨 5%"

    assert len(requests) == 1
    assert requests[0].url == "https://translator.test/v1/chat/completions"
    assert requests[0].headers["authorization"] == "Bearer secret"
    body = json.loads(requests[0].content)
    assert body["model"] == "fast-translator"
    assert body["temperature"] == 0
    assert PUSH_TRANSLATION_DEADLINE_SECONDS == 5.0
    assert "response_format" not in body
    assert "只输出 JSON" not in body["messages"][0]["content"]
    assert "必须原样保留" not in body["messages"][0]["content"]


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (httpx.Response(429, json={"error": "limit"}), "news_item_push_translation_rate_limited"),
        (httpx.Response(500, text="secret upstream body"), "news_item_push_translation_http_error"),
        (httpx.Response(200, json={"choices": []}), "news_item_push_translation_response_invalid"),
    ],
)
def test_translator_errors_are_sanitized(response: httpx.Response, code: str) -> None:
    translator = OpenAICompatibleNewsPushTranslator(
        base_url="https://translator.test/v1",
        api_key="secret",
        model="translator",
        transport=httpx.MockTransport(lambda _request: response),
    )

    async def scenario() -> None:
        try:
            await translator.translate("Bitcoin rises")
        finally:
            await translator.close()

    with pytest.raises(NewsPushExternalError, match=code) as raised:
        asyncio.run(scenario())

    assert "secret upstream body" not in str(raised.value)


def test_sender_renders_translated_title_and_keeps_original_visible() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"code": 0})

    sender = FeishuNewsPushSender(
        webhook_url=_FEISHU_TEST_URL,
        signing_secret="signing-secret",
        transport=httpx.MockTransport(respond),
        timestamp_seconds=lambda: 1_700_000_000,
    )
    try:
        receipt = sender.send(
            _source_payload(),
            {
                "display_title": "比特币 ETF 资金流入加速",
                "outcome": "translated",
                "translation_policy_version": "title_zh_v3",
                "translation_duration_ms": 800,
            },
        )
    finally:
        sender.close()

    assert receipt.provider == "feishu"
    assert receipt.details == {"code": 0, "status_code": 200}
    assert len(requests) == 1
    payload = json.loads(requests[0].content)
    assert payload["timestamp"] == "1700000000"
    assert payload["sign"] == generate_feishu_signature(
        timestamp_seconds=1_700_000_000,
        signing_secret="signing-secret",
    )
    card = payload["card"]
    assert card["header"]["title"]["content"] == "比特币 ETF 资金流入加速"
    rendered = json.dumps(card, ensure_ascii=False)
    assert "Bitcoin ETF inflows accelerate" in rendered
    assert "News Score > 70" in rendered
    assert "https://example.test/news/1" in rendered


def test_sender_fallback_uses_original_and_sends_exactly_once_on_failure() -> None:
    calls = 0

    def reject(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, text="do-not-leak")

    sender = FeishuNewsPushSender(
        webhook_url=_FEISHU_TEST_URL,
        transport=httpx.MockTransport(reject),
    )
    try:
        with pytest.raises(NewsPushExternalError, match="news_item_push_feishu_http_failed") as raised:
            sender.send(
                _source_payload(),
                {
                    "display_title": "Bitcoin ETF inflows accelerate",
                    "outcome": "fallback",
                    "fallback_code": "news_item_push_translation_timeout",
                    "translation_policy_version": "title_zh_v3",
                },
            )
    finally:
        sender.close()

    assert calls == 1
    assert "do-not-leak" not in str(raised.value)


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (httpx.Response(400, text="bad card secret"), "news_item_push_feishu_http_rejected"),
        (httpx.Response(429, text="rate secret"), "news_item_push_feishu_http_failed"),
        (httpx.Response(500, text="server secret"), "news_item_push_feishu_http_failed"),
        (httpx.Response(200, text="not-json secret"), "news_item_push_feishu_response_invalid"),
        (
            httpx.Response(200, json={"code": 11232, "msg": "rate secret"}),
            "news_item_push_feishu_business_rate_limited",
        ),
        (
            httpx.Response(200, json={"code": 19001, "msg": "auth secret"}),
            "news_item_push_feishu_business_rejected",
        ),
    ],
)
def test_sender_http_and_business_failures_are_single_sanitized_attempt(
    response: httpx.Response,
    code: str,
) -> None:
    calls = 0

    def reject(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return response

    sender = FeishuNewsPushSender(
        webhook_url=_FEISHU_TEST_URL,
        transport=httpx.MockTransport(reject),
    )
    try:
        with pytest.raises(NewsPushExternalError, match=code) as raised:
            sender.send(
                _source_payload(),
                {
                    "display_title": "Bitcoin ETF inflows accelerate",
                    "outcome": "not_needed",
                    "translation_policy_version": "title_zh_v3",
                },
            )
    finally:
        sender.close()

    assert calls == 1
    assert "secret" not in str(raised.value)


@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ReadTimeout])
def test_sender_transport_failures_are_single_sanitized_attempt(
    error_type: type[httpx.TransportError],
) -> None:
    calls = 0

    def reject(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise error_type("secret", request=request)

    sender = FeishuNewsPushSender(
        webhook_url=_FEISHU_TEST_URL,
        transport=httpx.MockTransport(reject),
    )
    try:
        with pytest.raises(
            NewsPushExternalError,
            match="news_item_push_feishu_transport_failed",
        ) as raised:
            sender.send(
                _source_payload(),
                {
                    "display_title": "Bitcoin ETF inflows accelerate",
                    "outcome": "not_needed",
                    "translation_policy_version": "title_zh_v3",
                },
            )
    finally:
        sender.close()

    assert calls == 1
    assert "secret" not in str(raised.value)


def test_sender_rejects_render_auth_and_oversize_before_network() -> None:
    calls = 0

    def accept(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"code": 0})

    presentation = {
        "display_title": "Bitcoin ETF inflows accelerate",
        "outcome": "not_needed",
        "translation_policy_version": "title_zh_v3",
    }
    sender = FeishuNewsPushSender(
        webhook_url=_FEISHU_TEST_URL,
        transport=httpx.MockTransport(accept),
    )
    signed_sender = FeishuNewsPushSender(
        webhook_url=_FEISHU_TEST_URL,
        signing_secret="signing-secret",
        timestamp_seconds=lambda: 0,
        transport=httpx.MockTransport(accept),
    )
    try:
        invalid_url = _source_payload()
        invalid_url["source_url"] = "file:///etc/passwd"
        with pytest.raises(NewsPushExternalError, match="news_item_push_source_url_invalid"):
            sender.send(invalid_url, presentation)

        with pytest.raises(NewsPushExternalError, match="news_item_push_feishu_timestamp_invalid"):
            signed_sender.send(_source_payload(), presentation)

        oversized = _source_payload()
        oversized["strategy_labels"] = [f"strategy-{index}-" + "x" * 120 for index in range(300)]
        with pytest.raises(NewsPushExternalError, match="news_item_push_feishu_card_too_large"):
            sender.send(oversized, presentation)
    finally:
        sender.close()
        signed_sender.close()

    assert calls == 0


def _source_payload() -> dict[str, object]:
    return {
        "schema_version": "news_item_push_v1",
        "item_id": "news_item_0123456789abcdef0123456789abcdef",
        "provider_event_id": "provider-1",
        "live_observed_at_ms": 1_700_000_000_000,
        "original_title": "Bitcoin ETF inflows accelerate",
        "reporting_origin": "OpenNews",
        "provider_published_at_ms": 1_699_999_999_000,
        "strategy_labels": ["1018 News Score > 70"],
        "assets": [{"symbol": "BTC", "market_type": "spot"}],
        "source_url": "https://example.test/news/1",
        "score": 91,
    }
