from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import yaml
from pydantic import ValidationError

from tracefold.app.cli.commands import config as config_command
from tracefold.integrations import news_push as news_push_integration
from tracefold.integrations.feishu import (
    FEISHU_WEBHOOK_REQUEST_MAX_BYTES,
    FeishuRetryableError,
    FeishuTerminalError,
    FeishuWebhookClient,
    generate_feishu_signature,
)
from tracefold.integrations.news_push import FeishuNewsPushDelivery
from tracefold.news import NewsPushDeliveryError
from tracefold.platform.config.settings import LlmConfig, NewsPushSettings, Settings, default_config_yaml
from tracefold.platform.resource import ResourceAdmissionTimeout, ResourceOperationOverrun

_FEISHU_TEST_URL = "https://open.feishu.cn/open-apis/bot/v2/hook/test-hook-id"
_PREPARE_DEADLINE_MS = 4_102_444_800_000


def test_news_push_settings_require_only_feishu_webhook_when_enabled() -> None:
    assert NewsPushSettings().enabled is False
    with pytest.raises(ValidationError, match="news_push_feishu_webhook_url_required"):
        NewsPushSettings(enabled=True)

    unsigned = NewsPushSettings(enabled=True, feishu_webhook_url=_FEISHU_TEST_URL)
    assert unsigned.feishu_webhook_url == _FEISHU_TEST_URL
    assert unsigned.feishu_signing_secret is None

    configured = NewsPushSettings(
        enabled=True,
        feishu_webhook_url=f"  {_FEISHU_TEST_URL}  ",
        feishu_signing_secret="  signing-secret  ",
    )

    assert configured.feishu_webhook_url == _FEISHU_TEST_URL
    assert configured.feishu_signing_secret == "signing-secret"


def test_news_push_configuration_has_no_duplicate_model_credentials() -> None:
    assert set(NewsPushSettings.model_fields) == {
        "enabled",
        "feishu_webhook_url",
        "feishu_signing_secret",
    }


def test_news_settings_reject_push_when_news_is_disabled_without_leaking_secrets() -> None:
    with pytest.raises(ValidationError, match="news_push_requires_news_enabled") as raised:
        Settings(
            news={
                "enabled": False,
                "push": {
                    "enabled": True,
                    "feishu_webhook_url": _FEISHU_TEST_URL,
                    "feishu_signing_secret": "must-not-leak",
                },
            }
        )

    rendered = str(raised.value)
    assert _FEISHU_TEST_URL not in rendered
    assert "must-not-leak" not in rendered


@pytest.mark.parametrize(
    "url",
    [
        "http://open.feishu.cn/open-apis/bot/v2/hook/test-hook-id",
        "https://example.test/open-apis/bot/v2/hook/test-hook-id",
        "https://open.feishu.cn/open-apis/bot/hook/test-hook-id",
        "https://open.feishu.cn/open-apis/bot/v2/hook/",
        "https://open.feishu.cn/open-apis/bot/v2/hook/test-hook-id?secret=1",
    ],
)
def test_news_push_settings_reject_noncanonical_feishu_webhooks(url: str) -> None:
    with pytest.raises(ValidationError, match="news_push_feishu_webhook_url_invalid") as raised:
        NewsPushSettings(feishu_webhook_url=url)

    assert url not in str(raised.value)


def test_default_config_keeps_news_push_disabled_and_credentials_empty() -> None:
    payload = yaml.safe_load(default_config_yaml())

    assert payload["news"]["push"] == {
        "enabled": False,
        "feishu_webhook_url": None,
        "feishu_signing_secret": None,
    }
    assert payload["llm"]["api_key"] is None
    assert payload["llm"]["base_url"] is None
    assert payload["llm"]["news_brief_model"] is None
    assert "openrouter_api_key" not in payload["llm"]


@pytest.mark.parametrize(
    "partial",
    [
        {"api_key": "secret"},
        {"base_url": "https://deepseek.test/v1"},
        {"news_brief_model": "deepseek-chat"},
        {"api_key": "secret", "base_url": "https://deepseek.test/v1"},
        {"api_key": "secret", "news_brief_model": "deepseek-chat"},
        {"base_url": "https://deepseek.test/v1", "news_brief_model": "deepseek-chat"},
    ],
)
def test_llm_direct_configuration_is_all_or_none(partial: dict[str, str]) -> None:
    with pytest.raises(ValidationError, match="llm_direct_configuration_incomplete"):
        LlmConfig(**partial)


def test_llm_direct_configuration_accepts_empty_or_complete() -> None:
    assert LlmConfig() == LlmConfig(api_key=None, base_url=None, news_brief_model=None)
    configured = LlmConfig(
        api_key="  secret  ",
        base_url="  https://deepseek.test/v1/  ",
        news_brief_model="  deepseek-chat  ",
    )

    assert configured.api_key == "secret"
    assert configured.base_url == "https://deepseek.test/v1"
    assert configured.news_brief_model == "deepseek-chat"


def test_llm_configuration_rejects_retired_openrouter_field() -> None:
    with pytest.raises(ValidationError, match="openrouter_api_key") as raised:
        LlmConfig.model_validate({"openrouter_api_key": "retired-secret"})

    assert "retired-secret" not in str(raised.value)


def test_config_diagnostics_expose_only_news_push_configured_booleans(monkeypatch, tmp_path) -> None:
    settings = Settings(
        llm={
            "api_key": "translation-secret",
            "base_url": "https://translator.test/v1",
            "news_brief_model": "fast-title-translator",
            "macro_document_analysis_enabled": True,
            "macro_document_analysis_model": "policy-evidence-model",
        },
        news={
            "push": {
                "enabled": True,
                "feishu_webhook_url": _FEISHU_TEST_URL,
                "feishu_signing_secret": "test-signing-secret",
            }
        },
    )
    settings.set_config_dir(tmp_path)
    monkeypatch.setattr(config_command, "load_settings", lambda **_kwargs: settings)

    code, payload = config_command.handle_config(object())

    assert code == 0
    assert payload["data"]["news"]["rss_enabled"] is False
    assert payload["data"]["news"]["push"] == {
        "enabled": True,
        "feishu_webhook_url_configured": True,
        "feishu_signing_secret_configured": True,
        "translation_enabled": True,
        "translation_configured": True,
    }
    assert payload["data"]["news"]["brief"] == {
        "direct_configured": True,
        "groq_configured": False,
    }
    assert payload["data"]["macro"]["document_analysis"] == {
        "state": "active",
        "enabled": True,
        "configured": True,
        "worker_active": True,
        "model": "policy-evidence-model",
    }
    rendered = json.dumps(payload)
    assert _FEISHU_TEST_URL not in rendered
    assert "test-signing-secret" not in rendered
    assert "translator.test" not in rendered
    assert "translation-secret" not in rendered


def test_feishu_signature_matches_official_empty_message_hmac_shape() -> None:
    signature = generate_feishu_signature(
        timestamp_seconds=1_599_360_473,
        signing_secret="test-secret",
    )

    assert signature == "wSds2BzzFIIGf/WrhUO+NI1q/9j+FRJd3JNHKAq0NZY="


def test_feishu_webhook_sends_signed_interactive_card_and_requires_current_success_code() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "StatusCode": 999,
                "StatusMessage": "legacy field ignored",
                "code": 0,
                "msg": "success",
                "data": {},
            },
        )

    client = FeishuWebhookClient(
        webhook_url=_FEISHU_TEST_URL,
        signing_secret="test-secret",
        transport=httpx.MockTransport(handler),
    )
    try:
        receipt = client.send(
            {
                "schema": "2.0",
                "header": {"title": {"tag": "plain_text", "content": "中文标题"}},
            },
            timestamp_seconds=1_599_360_473,
        )
    finally:
        client.close()

    assert receipt.status_code == 200
    assert receipt.code == 0
    assert len(requests) == 1
    payload = json.loads(requests[0].content)
    assert payload["timestamp"] == "1599360473"
    assert payload["sign"] == "wSds2BzzFIIGf/WrhUO+NI1q/9j+FRJd3JNHKAq0NZY="
    assert payload["msg_type"] == "interactive"
    assert payload["card"]["schema"] == "2.0"


def test_feishu_webhook_without_secret_omits_signature_fields() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"code": 0})

    client = FeishuWebhookClient(
        webhook_url=_FEISHU_TEST_URL,
        transport=httpx.MockTransport(handler),
    )
    try:
        receipt = client.send(
            {
                "schema": "2.0",
                "header": {"title": {"tag": "plain_text", "content": "中文标题"}},
            }
        )
    finally:
        client.close()

    assert receipt.status_code == 200
    assert len(requests) == 1
    payload = json.loads(requests[0].content)
    assert payload == {
        "msg_type": "interactive",
        "card": {
            "schema": "2.0",
            "header": {"title": {"tag": "plain_text", "content": "中文标题"}},
        },
    }


@pytest.mark.parametrize("status_code", [429, 500, 503])
def test_feishu_webhook_classifies_rate_limit_and_server_statuses_as_retryable(status_code: int) -> None:
    client = FeishuWebhookClient(
        webhook_url=_FEISHU_TEST_URL,
        signing_secret="test-secret",
        transport=httpx.MockTransport(lambda _request: httpx.Response(status_code, json={})),
    )
    try:
        with pytest.raises(FeishuRetryableError) as raised:
            client.send({}, timestamp_seconds=1_599_360_473)
    finally:
        client.close()

    assert raised.value.status_code == status_code


def test_feishu_webhook_classifies_transport_failure_as_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    client = FeishuWebhookClient(
        webhook_url=_FEISHU_TEST_URL,
        signing_secret="test-secret",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(FeishuRetryableError, match="feishu_transport_failed"):
            client.send({}, timestamp_seconds=1_599_360_473)
    finally:
        client.close()


def test_feishu_webhook_classifies_official_business_rate_limit_as_retryable() -> None:
    client = FeishuWebhookClient(
        webhook_url=_FEISHU_TEST_URL,
        signing_secret="test-secret",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"code": 11232})),
    )
    try:
        with pytest.raises(FeishuRetryableError, match="feishu_business_rate_limited"):
            client.send({}, timestamp_seconds=1_599_360_473)
    finally:
        client.close()


@pytest.mark.parametrize(
    ("response", "error_code"),
    [
        (httpx.Response(400, json={"code": 9499}), "feishu_http_terminal"),
        (httpx.Response(200, json={"code": 19021}), "feishu_business_rejected"),
        (httpx.Response(200, json={"StatusCode": 0}), "feishu_response_invalid"),
    ],
)
def test_feishu_webhook_classifies_deterministic_rejections_as_terminal(
    response: httpx.Response,
    error_code: str,
) -> None:
    client = FeishuWebhookClient(
        webhook_url=_FEISHU_TEST_URL,
        signing_secret="test-secret",
        transport=httpx.MockTransport(lambda _request: response),
    )
    try:
        with pytest.raises(FeishuTerminalError, match=error_code):
            client.send({}, timestamp_seconds=1_599_360_473)
    finally:
        client.close()


def test_feishu_webhook_rejects_card_over_official_request_limit_without_network() -> None:
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"code": 0})

    client = FeishuWebhookClient(
        webhook_url=_FEISHU_TEST_URL,
        signing_secret="test-secret",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(FeishuTerminalError, match="feishu_card_too_large"):
            client.send(
                {"body": "x" * FEISHU_WEBHOOK_REQUEST_MAX_BYTES},
                timestamp_seconds=1_599_360_473,
            )
    finally:
        client.close()

    assert called is False


def test_feishu_news_push_renders_compact_evidence_v2_card() -> None:
    delivery = FeishuNewsPushDelivery(
        _FEISHU_TEST_URL,
        "test-secret",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"code": 0})),
    )
    try:
        rendered = _prepare_payload(
            delivery,
            _news_push_source_payload(
                title="Original [alert](https://evil.test) <at id=all></at>",
                url="https://example.com/story/1",
            ),
        )
    finally:
        delivery.close()

    assert rendered["channel"] == "feishu"
    assert set(rendered) == {
        "schema_version",
        "channel",
        "auth_mode",
        "presentation",
        "card",
    }
    assert rendered["schema_version"] == "news_feishu_delivery_v2"
    assert rendered["presentation"] == {
        "headline_mode": "source",
        "target_language": "zh-CN",
        "provider": None,
        "engine": None,
        "prompt_version": "title_zh_v2",
        "fallback_code": None,
    }
    assert rendered["card"] == {
        "schema": "2.0",
        "body": {
            "direction": "vertical",
            "padding": "12px 12px 12px 12px",
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "plain_text",
                        "content": "关联资产：BTC · ETH\nOpenNews 评分：91",
                    },
                    "margin": "0px 0px 0px 0px",
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "查看原文"},
                    "type": "default",
                    "width": "default",
                    "size": "medium",
                    "behaviors": [
                        {
                            "type": "open_url",
                            "default_url": "https://example.com/story/1",
                        }
                    ],
                    "margin": "8px 0px 0px 0px",
                },
            ],
        },
        "header": {
            "title": {
                "tag": "plain_text",
                "content": "Original [alert](https://evil.test) <at id=all></at>",
            }
        },
    }


def test_feishu_news_push_translates_once_and_keeps_original_visible() -> None:
    translation_requests: list[httpx.Request] = []

    def translation_handler(request: httpx.Request) -> httpx.Response:
        translation_requests.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {"translated_title": "比特币 ETF 录得 10% 资金流入"},
                                ensure_ascii=False,
                            )
                        },
                    }
                ]
            },
        )

    delivery = FeishuNewsPushDelivery(
        _FEISHU_TEST_URL,
        "test-secret",
        finite_operations=_InlineFiniteOperations(),
        translation_enabled=True,
        translation_base_url="https://translator.test/v1",
        translation_api_key="translation-secret",
        translation_engine="fast-title-translator",
        translation_transport=httpx.MockTransport(translation_handler),
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"code": 0})),
    )
    try:
        prepared = asyncio.run(
            delivery.prepare(
                _news_push_source_payload(title="Bitcoin ETF records 10% inflows"),
                deadline_ms=_PREPARE_DEADLINE_MS,
            )
        )
    finally:
        delivery.close()

    assert prepared.translation_status == "translated"
    assert prepared.payload["schema_version"] == "news_feishu_delivery_v2"
    presentation = dict(prepared.payload["presentation"])
    attempted_at_ms = presentation.pop("translation_attempted_at_ms")
    duration_ms = presentation.pop("translation_duration_ms")
    assert presentation == {
        "headline_mode": "translated",
        "target_language": "zh-CN",
        "provider": "openai_compatible",
        "engine": "fast-title-translator",
        "prompt_version": "title_zh_v2",
        "fallback_code": None,
    }
    assert isinstance(attempted_at_ms, int) and attempted_at_ms > 0
    assert isinstance(duration_ms, int) and duration_ms >= 0
    card = prepared.payload["card"]
    assert card["header"]["title"]["content"] == "比特币 ETF 录得 10% 资金流入"
    body_text = [element["text"]["content"] for element in card["body"]["elements"] if element.get("tag") == "div"]
    assert body_text == [
        "自动翻译，仅供参考\n原文：Bitcoin ETF records 10% inflows",
        "关联资产：BTC · ETH\nOpenNews 评分：91",
    ]
    assert len(translation_requests) == 1
    request_payload = json.loads(translation_requests[0].content)
    assert request_payload["model"] == "fast-title-translator"
    assert "thinking" not in request_payload
    frozen_json = json.dumps(prepared.payload, ensure_ascii=False)
    assert "translation-secret" not in frozen_json
    assert "translator.test" not in frozen_json


def test_translation_request_lists_unique_required_anchors_in_source_order() -> None:
    translation_requests: list[httpx.Request] = []
    title = "BTC leads $ETH after BTC gains 10% and $ETH gains 10%"

    def translation_handler(request: httpx.Request) -> httpx.Response:
        translation_requests.append(request)
        return _translation_response("BTC 领先 $ETH，随后 BTC 上涨 10%")

    delivery = _translation_delivery(translation_handler)
    try:
        prepared = asyncio.run(
            delivery.prepare(
                _news_push_source_payload(
                    title=title,
                    symbols=("ETH", "BTC", "BTC"),
                ),
                deadline_ms=_PREPARE_DEADLINE_MS,
            )
        )
    finally:
        delivery.close()

    assert prepared.translation_status == "translated"
    assert prepared.payload["presentation"]["prompt_version"] == "title_zh_v2"
    request_payload = json.loads(translation_requests[0].content)
    user_payload = json.loads(request_payload["messages"][1]["content"])
    assert user_payload == {
        "source_title": title,
        "required_verbatim": ["BTC", "$ETH", "10%"],
    }


def test_translation_enabled_skips_chinese_and_translates_japanese() -> None:
    translation_requests: list[httpx.Request] = []

    def translation_handler(request: httpx.Request) -> httpx.Response:
        translation_requests.append(request)
        return _translation_response("日本银行加息")

    delivery = _translation_delivery(translation_handler)
    try:
        chinese = asyncio.run(
            delivery.prepare(
                _news_push_source_payload(title="央行维持利率不变"),
                deadline_ms=_PREPARE_DEADLINE_MS,
            )
        )
        japanese = asyncio.run(
            delivery.prepare(
                _news_push_source_payload(title="日本銀行が金利を引き上げる"),
                deadline_ms=_PREPARE_DEADLINE_MS,
            )
        )
    finally:
        delivery.close()

    assert chinese.translation_status == "not_needed"
    assert chinese.payload["presentation"]["headline_mode"] == "source"
    assert japanese.translation_status == "translated"
    assert japanese.payload["card"]["header"]["title"]["content"] == "日本银行加息"
    assert len(translation_requests) == 1


@pytest.mark.parametrize(
    ("symbol", "title", "translated_title"),
    (
        ("ON", "Markets move on policy news", "市场因政策消息而波动"),
        ("NEAR", "Bitcoin trades near record high", "比特币交易价格接近历史高点"),
        ("LINK", "Funds link custody to settlement", "基金将托管与结算联系起来"),
    ),
)
def test_translation_does_not_treat_lowercase_words_as_provider_symbol_anchors(
    symbol: str,
    title: str,
    translated_title: str,
) -> None:
    delivery = _translation_delivery(lambda _request: _translation_response(translated_title))
    try:
        prepared = asyncio.run(
            delivery.prepare(
                _news_push_source_payload(title=title, symbols=(symbol,)),
                deadline_ms=_PREPARE_DEADLINE_MS,
            )
        )
    finally:
        delivery.close()

    assert prepared.translation_status == "translated"
    assert prepared.payload["card"]["header"]["title"]["content"] == translated_title


@pytest.mark.parametrize(
    ("title", "symbols"),
    (
        ("LINK rallies", ("LINK",)),
        ("$link rallies", ()),
    ),
)
def test_translation_falls_back_when_an_explicit_ticker_is_missing(
    title: str,
    symbols: tuple[str, ...],
) -> None:
    delivery = _translation_delivery(lambda _request: _translation_response("代币上涨"))
    try:
        prepared = asyncio.run(
            delivery.prepare(
                _news_push_source_payload(title=title, symbols=symbols),
                deadline_ms=_PREPARE_DEADLINE_MS,
            )
        )
    finally:
        delivery.close()

    assert prepared.translation_status == "unavailable"
    assert prepared.payload["presentation"]["fallback_code"] == ("news_push_translation_anchors_changed")
    assert prepared.payload["card"]["header"]["title"]["content"] == title


def test_overlong_title_skips_translation_and_marks_the_visible_excerpt() -> None:
    called = False

    def translation_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        raise AssertionError("overlong title must not call translation")

    delivery = _translation_delivery(translation_handler)
    try:
        prepared = asyncio.run(
            delivery.prepare(
                _news_push_source_payload(title="x" * 501),
                deadline_ms=_PREPARE_DEADLINE_MS,
            )
        )
    finally:
        delivery.close()

    assert called is False
    assert prepared.translation_status == "unavailable"
    assert prepared.payload["presentation"]["fallback_code"] == "news_push_translation_title_too_long"
    assert "translation_attempted_at_ms" not in prepared.payload["presentation"]
    assert "translation_duration_ms" not in prepared.payload["presentation"]
    assert prepared.payload["card"]["header"]["title"]["content"].endswith("…")
    assert prepared.payload["card"]["body"]["elements"][0]["text"]["content"].startswith(
        "标题过长，未自动翻译\n原文节选："
    )


@pytest.mark.parametrize(
    ("response", "expected_code"),
    (
        (httpx.Response(429), "news_push_translation_rate_limited"),
        (httpx.Response(200, json={"choices": []}), "news_push_translation_response_invalid"),
        (httpx.Response(200, json={"choices": [[]]}), "news_push_translation_response_invalid"),
        (
            httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "content": '{"translated_title":"比特币上涨"}',
                            },
                        }
                    ]
                },
            ),
            "news_push_translation_anchors_changed",
        ),
    ),
)
def test_translation_failure_freezes_original_fallback(
    response: httpx.Response,
    expected_code: str,
) -> None:
    delivery = _translation_delivery(lambda _request: response)
    try:
        prepared = asyncio.run(
            delivery.prepare(
                _news_push_source_payload(title="BTC rises 10%"),
                deadline_ms=_PREPARE_DEADLINE_MS,
            )
        )
    finally:
        delivery.close()

    assert prepared.translation_status == "unavailable"
    assert prepared.payload["presentation"]["headline_mode"] == "fallback_original"
    assert prepared.payload["presentation"]["fallback_code"] == expected_code
    assert isinstance(prepared.payload["presentation"]["translation_attempted_at_ms"], int)
    assert isinstance(prepared.payload["presentation"]["translation_duration_ms"], int)
    assert prepared.payload["card"]["header"]["title"]["content"] == "BTC rises 10%"


def test_recursive_translation_json_is_an_invalid_provider_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def recursive_json(_response: httpx.Response, **_kwargs: object) -> object:
        raise RecursionError("provider_json_too_deep")

    monkeypatch.setattr(httpx.Response, "json", recursive_json)
    delivery = _translation_delivery(lambda _request: httpx.Response(200, content=b"{}"))
    try:
        prepared = asyncio.run(
            delivery.prepare(
                _news_push_source_payload(title="BTC rises 10%"),
                deadline_ms=_PREPARE_DEADLINE_MS,
            )
        )
    finally:
        delivery.close()

    assert prepared.translation_status == "unavailable"
    assert prepared.payload["presentation"]["fallback_code"] == ("news_push_translation_response_invalid")


def test_translation_propagates_unexpected_runtime_errors() -> None:
    def translation_handler(_request: httpx.Request) -> httpx.Response:
        raise RuntimeError("translation_programming_error")

    delivery = _translation_delivery(translation_handler)
    try:
        with pytest.raises(RuntimeError, match="translation_programming_error"):
            asyncio.run(
                delivery.prepare(
                    _news_push_source_payload(),
                    deadline_ms=_PREPARE_DEADLINE_MS,
                )
            )
    finally:
        delivery.close()


def test_translation_admission_timeout_immediately_freezes_original_fallback() -> None:
    fenced = False

    async def persist_fence() -> None:
        nonlocal fenced
        fenced = True

    delivery = _translation_delivery(
        lambda _request: _translation_response("比特币 ETF 录得资金流入"),
        finite_operations=_UnavailableFiniteOperations(),
    )
    try:
        prepared = asyncio.run(
            delivery.prepare(
                _news_push_source_payload(),
                deadline_ms=_PREPARE_DEADLINE_MS,
                before_translation_submit=persist_fence,
            )
        )
    finally:
        delivery.close()

    assert prepared.translation_status == "unavailable"
    assert prepared.payload["presentation"]["fallback_code"] == ("news_push_translation_admission_timeout")
    assert "translation_attempted_at_ms" not in prepared.payload["presentation"]
    assert "translation_duration_ms" not in prepared.payload["presentation"]
    assert fenced is False


def test_translation_operation_overrun_freezes_original_fallback_without_retry() -> None:
    class _OverrunFiniteOperations:
        async def run(self, operation_name, _function, /, *_args, **kwargs):
            kwargs["on_submitted"]()
            raise ResourceOperationOverrun(f"resource_operation_overrun:{operation_name}")

    delivery = _translation_delivery(
        lambda _request: _translation_response("比特币 ETF 录得资金流入"),
        finite_operations=_OverrunFiniteOperations(),
    )
    try:
        prepared = asyncio.run(
            delivery.prepare(
                _news_push_source_payload(),
                deadline_ms=_PREPARE_DEADLINE_MS,
            )
        )
    finally:
        delivery.close()

    assert prepared.translation_status == "unavailable"
    assert prepared.payload["presentation"]["fallback_code"] == "news_push_translation_timeout"
    assert isinstance(prepared.payload["presentation"]["translation_attempted_at_ms"], int)
    assert isinstance(prepared.payload["presentation"]["translation_duration_ms"], int)
    assert prepared.payload["card"]["header"]["title"]["content"] == "Bitcoin ETF records inflows"


def test_interrupted_translation_fallback_never_calls_the_provider_again() -> None:
    provider_called = False

    def translation_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal provider_called
        provider_called = True
        raise AssertionError("interrupted translation must not be resubmitted")

    delivery = _translation_delivery(translation_handler)
    try:
        prepared = asyncio.run(
            delivery.prepare(
                _news_push_source_payload(),
                deadline_ms=_PREPARE_DEADLINE_MS,
                interrupted_translation_attempted_at_ms=1_785_560_400_123,
            )
        )
    finally:
        delivery.close()

    assert provider_called is False
    assert prepared.translation_status == "unavailable"
    assert prepared.payload["presentation"] == {
        "headline_mode": "fallback_original",
        "target_language": "zh-CN",
        "provider": None,
        "engine": None,
        "prompt_version": "title_zh_v2",
        "fallback_code": "news_push_translation_interrupted_after_dispatch",
        "translation_attempted_at_ms": 1_785_560_400_123,
        "translation_duration_ms": None,
    }
    assert prepared.payload["card"]["header"]["title"]["content"] == ("Bitcoin ETF records inflows")


def test_translation_total_budget_includes_resource_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        news_push_integration,
        "_TRANSLATION_TOTAL_TIMEOUT_SECONDS",
        0.01,
    )
    delivery = _translation_delivery(
        lambda _request: _translation_response("比特币 ETF 录得资金流入"),
        finite_operations=_BlockingFiniteOperations(),
    )
    try:
        prepared = asyncio.run(
            delivery.prepare(
                _news_push_source_payload(),
                deadline_ms=_PREPARE_DEADLINE_MS,
            )
        )
    finally:
        delivery.close()

    assert prepared.translation_status == "unavailable"
    assert prepared.payload["presentation"]["fallback_code"] == ("news_push_translation_total_timeout")


def test_translation_budget_allows_observed_provider_latency() -> None:
    delivery = _translation_delivery(
        lambda _request: _translation_response("比特币 ETF 录得资金流入"),
        finite_operations=_ObservedLatencyFiniteOperations(),
    )
    try:
        prepared = asyncio.run(
            delivery.prepare(
                _news_push_source_payload(),
                deadline_ms=_PREPARE_DEADLINE_MS,
            )
        )
    finally:
        delivery.close()

    assert prepared.translation_status == "translated"
    assert prepared.payload["card"]["header"]["title"]["content"] == ("比特币 ETF 录得资金流入")


def test_translation_request_and_total_timeouts_stay_within_the_push_budget() -> None:
    request_timeouts: list[dict[str, float]] = []

    def translation_handler(request: httpx.Request) -> httpx.Response:
        request_timeouts.append(request.extensions["timeout"])
        return _translation_response("比特币 ETF 录得资金流入")

    delivery = _translation_delivery(translation_handler)
    try:
        prepared = asyncio.run(
            delivery.prepare(
                _news_push_source_payload(),
                deadline_ms=_PREPARE_DEADLINE_MS,
            )
        )
    finally:
        delivery.close()

    assert prepared.translation_status == "translated"
    assert request_timeouts == [
        {
            "connect": 7.5,
            "read": 7.5,
            "write": 7.5,
            "pool": 7.5,
        }
    ]
    assert news_push_integration._TRANSLATION_TOTAL_TIMEOUT_SECONDS == 8.0


def test_feishu_news_push_uses_english_original_without_model_work() -> None:
    delivery = FeishuNewsPushDelivery(
        _FEISHU_TEST_URL,
        "test-secret",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"code": 0})),
    )
    try:
        rendered = _prepare_payload(delivery, _news_push_source_payload(title="Fed holds rates steady", url=None))
    finally:
        delivery.close()

    card = rendered["card"]
    assert card["header"]["title"]["content"] == "Fed holds rates steady"
    assert card["body"]["elements"] == [
        {
            "tag": "div",
            "text": {
                "tag": "plain_text",
                "content": "关联资产：BTC · ETH\nOpenNews 评分：91",
            },
            "margin": "0px 0px 0px 0px",
        },
    ]


def test_feishu_news_push_uses_chinese_original_unchanged() -> None:
    delivery = FeishuNewsPushDelivery(
        _FEISHU_TEST_URL,
        "test-secret",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"code": 0})),
    )
    try:
        rendered = _prepare_payload(delivery, _news_push_source_payload(title="比特币 ETF 录得资金流入", url=None))
    finally:
        delivery.close()

    card = rendered["card"]
    assert card["header"]["title"]["content"] == "比特币 ETF 录得资金流入"
    assert card["body"]["elements"][0]["text"]["content"] == ("关联资产：BTC · ETH\nOpenNews 评分：91")


def test_feishu_news_push_asset_body_preserves_order_and_deduplicates() -> None:
    delivery = FeishuNewsPushDelivery(
        _FEISHU_TEST_URL,
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"code": 0})),
    )
    source = _news_push_source_payload()
    source["provider_evidence"]["provider_metadata"]["coins"] = [
        {"symbol": " NEAR "},
        {"symbol": "BTC"},
        {"symbol": "near"},
        {"symbol": "  "},
        {},
        "invalid",
        None,
    ]
    try:
        rendered = _prepare_payload(delivery, source)
    finally:
        delivery.close()

    assert rendered["card"]["header"]["title"]["content"] == "Bitcoin ETF records inflows"
    assert rendered["card"]["body"]["elements"][0]["text"]["content"] == ("关联资产：NEAR · BTC\nOpenNews 评分：91")


@pytest.mark.parametrize("coins", (None, [], "BTC", [{"market_type": "spot"}]))
def test_feishu_news_push_without_valid_assets_marks_them_unavailable(coins: object) -> None:
    delivery = FeishuNewsPushDelivery(
        _FEISHU_TEST_URL,
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"code": 0})),
    )
    source = _news_push_source_payload()
    source["provider_evidence"]["provider_metadata"]["coins"] = coins
    try:
        rendered = _prepare_payload(delivery, source)
    finally:
        delivery.close()

    assert rendered["card"]["header"]["title"]["content"] == "Bitcoin ETF records inflows"
    assert rendered["card"]["body"]["elements"][0]["text"]["content"] == ("关联资产：未提供\nOpenNews 评分：91")


@pytest.mark.parametrize(
    "score",
    (None, True, "91", -1, 101, float("nan"), float("inf")),
)
def test_feishu_news_push_rejects_invalid_provider_score(score: object) -> None:
    delivery = FeishuNewsPushDelivery(
        _FEISHU_TEST_URL,
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"code": 0})),
    )
    source = _news_push_source_payload()
    source["provider_evidence"]["provider_score"] = score
    try:
        with pytest.raises(
            NewsPushDeliveryError,
            match="news_push_feishu_render_payload_invalid",
        ):
            _prepare_payload(delivery, source)
    finally:
        delivery.close()


@pytest.mark.parametrize("url", ("javascript:alert(1)", "//example.com/story", "not-a-url"))
def test_feishu_news_push_rejects_non_http_source_url(url: str) -> None:
    delivery = FeishuNewsPushDelivery(
        _FEISHU_TEST_URL,
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"code": 0})),
    )
    source = _news_push_source_payload(url=url)
    try:
        with pytest.raises(
            NewsPushDeliveryError,
            match="news_push_feishu_render_payload_invalid",
        ):
            _prepare_payload(delivery, source)
    finally:
        delivery.close()


def test_feishu_news_push_delivers_the_frozen_card_without_rerendering() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"code": 0})

    delivery = FeishuNewsPushDelivery(
        _FEISHU_TEST_URL,
        "test-secret",
        transport=httpx.MockTransport(handler),
    )
    source = _news_push_source_payload(title="Bitcoin ETF records inflows")
    frozen = _prepare_payload(delivery, source)
    source["provider_evidence"]["title"] = "MUTATED SOURCE"
    source["provider_evidence"]["provider_metadata"]["coins"][0]["symbol"] = "SOL"
    try:
        first = delivery.deliver(frozen, idempotency_key="story-1")
        second = delivery.deliver(frozen, idempotency_key="story-1")
    finally:
        delivery.close()

    assert first.provider == second.provider == "feishu"
    assert first.details == {"status_code": 200, "code": 0}
    assert frozen["auth_mode"] == "signed"
    assert set(frozen) == {
        "schema_version",
        "channel",
        "auth_mode",
        "presentation",
        "card",
    }
    sent_cards = [json.loads(request.content)["card"] for request in requests]
    assert sent_cards == [frozen["card"], frozen["card"]]
    assert all(card["header"]["title"]["content"] == "Bitcoin ETF records inflows" for card in sent_cards)
    assert all(
        card["body"]["elements"][0]["text"]["content"] == "关联资产：BTC · ETH\nOpenNews 评分：91" for card in sent_cards
    )
    assert all("MUTATED SOURCE" not in json.dumps(card) for card in sent_cards)


def test_feishu_news_push_rejects_missing_null_and_stale_schema_without_network() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"code": 0})

    delivery = FeishuNewsPushDelivery(
        _FEISHU_TEST_URL,
        "test-secret",
        transport=httpx.MockTransport(handler),
    )
    try:
        frozen = _prepare_payload(delivery, _news_push_source_payload())
        invalid_payloads = []
        missing_schema = dict(frozen)
        missing_schema.pop("schema_version")
        invalid_payloads.append(missing_schema)
        invalid_payloads.append({**frozen, "schema_version": None})
        invalid_payloads.append({**frozen, "schema_version": "news_feishu_delivery_v1"})
        for payload in invalid_payloads:
            with pytest.raises(
                NewsPushDeliveryError,
                match="news_push_feishu_frozen_schema_invalid",
            ) as raised:
                delivery.deliver(payload, idempotency_key="stale-story")
            assert raised.value.retryable is False
    finally:
        delivery.close()

    assert requests == []


@pytest.mark.parametrize(
    ("render_secret", "delivery_secret"),
    (("render-secret", None), (None, "delivery-secret")),
)
def test_feishu_news_push_rejects_cross_restart_auth_mode_change_without_network(
    render_secret: str | None,
    delivery_secret: str | None,
) -> None:
    rendered_by = FeishuNewsPushDelivery(
        _FEISHU_TEST_URL,
        render_secret,
        transport=httpx.MockTransport(lambda _request: httpx.Response(500)),
    )
    frozen = _prepare_payload(rendered_by, _news_push_source_payload())
    rendered_by.close()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"code": 0})

    restarted = FeishuNewsPushDelivery(
        _FEISHU_TEST_URL,
        delivery_secret,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(NewsPushDeliveryError) as raised:
            restarted.deliver(frozen, idempotency_key="story-1")
    finally:
        restarted.close()

    assert raised.value.code == "news_push_feishu_auth_mode_mismatch"
    assert raised.value.retryable is False
    assert requests == []


@pytest.mark.parametrize(
    ("signing_secret", "expected_auth_mode", "expected_signed"),
    (("test-secret", "signed", True), (None, "unsigned", False)),
)
def test_feishu_news_push_cross_restart_same_auth_mode_retries_frozen_card(
    signing_secret: str | None,
    expected_auth_mode: str,
    expected_signed: bool,
) -> None:
    first_requests: list[httpx.Request] = []

    def first_handler(request: httpx.Request) -> httpx.Response:
        first_requests.append(request)
        return httpx.Response(503)

    first_runtime = FeishuNewsPushDelivery(
        _FEISHU_TEST_URL,
        signing_secret,
        transport=httpx.MockTransport(first_handler),
    )
    frozen = _prepare_payload(first_runtime, _news_push_source_payload())
    try:
        with pytest.raises(NewsPushDeliveryError) as first_failure:
            first_runtime.deliver(frozen, idempotency_key="story-1")
    finally:
        first_runtime.close()

    retry_requests: list[httpx.Request] = []

    def retry_handler(request: httpx.Request) -> httpx.Response:
        retry_requests.append(request)
        return httpx.Response(200, json={"code": 0})

    restarted = FeishuNewsPushDelivery(
        _FEISHU_TEST_URL,
        signing_secret,
        transport=httpx.MockTransport(retry_handler),
    )
    try:
        receipt = restarted.deliver(frozen, idempotency_key="story-1")
    finally:
        restarted.close()

    assert first_failure.value.retryable is True
    assert frozen["auth_mode"] == expected_auth_mode
    assert set(frozen) == {
        "schema_version",
        "channel",
        "auth_mode",
        "presentation",
        "card",
    }
    assert signing_secret is None or signing_secret not in json.dumps(frozen)
    assert receipt.provider == "feishu"
    assert len(first_requests) == len(retry_requests) == 1
    for request in (*first_requests, *retry_requests):
        request_payload = json.loads(request.content)
        assert ("timestamp" in request_payload) is expected_signed
        assert ("sign" in request_payload) is expected_signed


@pytest.mark.parametrize(
    ("response", "retryable", "code"),
    [
        (httpx.Response(503), True, "feishu_http_retryable"),
        (httpx.Response(400), False, "feishu_http_terminal"),
        (httpx.Response(200, json={"code": 11232}), True, "feishu_business_rate_limited"),
        (httpx.Response(200, json={"code": 19021}), False, "feishu_business_rejected"),
    ],
)
def test_feishu_news_push_maps_raw_failures_to_domain_retry_policy(
    response: httpx.Response,
    retryable: bool,
    code: str,
) -> None:
    delivery = FeishuNewsPushDelivery(
        _FEISHU_TEST_URL,
        "test-secret",
        transport=httpx.MockTransport(lambda _request: response),
    )
    frozen = _prepare_payload(delivery, _news_push_source_payload())
    try:
        with pytest.raises(NewsPushDeliveryError) as raised:
            delivery.deliver(frozen, idempotency_key="story-1")
    finally:
        delivery.close()

    assert raised.value.retryable is retryable
    assert raised.value.code == code


def test_feishu_news_push_rejects_invalid_frozen_payload_as_terminal() -> None:
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"code": 0})

    delivery = FeishuNewsPushDelivery(
        _FEISHU_TEST_URL,
        "test-secret",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(NewsPushDeliveryError) as raised:
            delivery.deliver(
                {
                    "schema_version": "news_feishu_delivery_v2",
                    "channel": "feishu",
                    "card": {"schema": "1.0"},
                },
                idempotency_key="story-1",
            )
    finally:
        delivery.close()

    assert raised.value.retryable is False
    assert raised.value.code == "news_push_feishu_frozen_card_invalid"
    assert called is False


@pytest.mark.parametrize("auth_mode", (None, "other", [], {}))
def test_feishu_news_push_rejects_missing_or_invalid_frozen_auth_mode_without_network(
    auth_mode: object,
) -> None:
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"code": 0})

    delivery = FeishuNewsPushDelivery(
        _FEISHU_TEST_URL,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(NewsPushDeliveryError) as raised:
            delivery.deliver(
                {
                    "schema_version": "news_feishu_delivery_v2",
                    "channel": "feishu",
                    "auth_mode": auth_mode,
                    "card": {"schema": "2.0"},
                },
                idempotency_key="story-1",
            )
    finally:
        delivery.close()

    assert raised.value.retryable is False
    assert raised.value.code == "news_push_feishu_frozen_auth_mode_invalid"
    assert called is False


def _news_push_source_payload(
    *,
    title: str = "Bitcoin ETF records inflows",
    url: str | None = "https://example.com/story/1",
    symbols: tuple[str, ...] = ("BTC", "ETH"),
) -> dict[str, object]:
    return {
        "schema_version": "news_story_push_v1",
        "story_id": "story-1",
        "provider_evidence": {
            "item_id": "item-1",
            "url": url,
            "provider_metadata": {
                "source": "Reuters_Wire",
                "score": 91,
                "signal": "long",
                "grade": "A+",
                "coins": [{"symbol": symbol, "market_type": "spot"} for symbol in symbols],
            },
            "reporting_origin": "reuters",
            "title": title,
            "description": "Provider description is not rendered.",
            "lang": "en",
            "published_at_ms": 1_785_542_400_000,
            "provider_score": 91,
        },
        "tracefold_story": {
            "importance_score": 64,
            "item_count": 3,
            "source_count": 2,
            "first_published_at_ms": 1_785_542_300_000,
            "last_published_at_ms": 1_785_542_400_000,
        },
    }


def _translation_response(title: str) -> httpx.Response:
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


def _translation_delivery(
    handler,
    *,
    finite_operations=None,
) -> FeishuNewsPushDelivery:
    return FeishuNewsPushDelivery(
        _FEISHU_TEST_URL,
        "test-secret",
        finite_operations=finite_operations or _InlineFiniteOperations(),
        translation_enabled=True,
        translation_base_url="https://translator.test/v1",
        translation_api_key="translation-secret",
        translation_engine="fast-title-translator",
        translation_transport=httpx.MockTransport(handler),
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"code": 0})),
    )


def _prepare_payload(
    delivery: FeishuNewsPushDelivery,
    source_payload: dict[str, object],
) -> dict[str, object]:
    prepared = asyncio.run(
        delivery.prepare(
            source_payload,
            deadline_ms=_PREPARE_DEADLINE_MS,
        )
    )
    return dict(prepared.payload)


class _InlineFiniteOperations:
    async def run(self, _operation_name, function, /, *args, **kwargs):
        kwargs.pop("timeout_seconds")
        before_submit = kwargs.pop("before_submit", None)
        on_submitted = kwargs.pop("on_submitted", None)
        if before_submit is not None:
            await before_submit()
        if on_submitted is not None:
            on_submitted()
        return function(*args, **kwargs)


class _UnavailableFiniteOperations:
    async def run(self, *_args, **_kwargs):
        raise ResourceAdmissionTimeout("test_translation_admission_timeout")


class _BlockingFiniteOperations:
    async def run(self, *_args, **kwargs):
        before_submit = kwargs.get("before_submit")
        on_submitted = kwargs.get("on_submitted")
        if before_submit is not None:
            await before_submit()
        if on_submitted is not None:
            on_submitted()
        await asyncio.Event().wait()


class _ObservedLatencyFiniteOperations:
    async def run(self, _operation_name, function, /, *args, **kwargs):
        kwargs.pop("timeout_seconds")
        before_submit = kwargs.pop("before_submit", None)
        on_submitted = kwargs.pop("on_submitted", None)
        if before_submit is not None:
            await before_submit()
        if on_submitted is not None:
            on_submitted()
        await asyncio.sleep(1.6)
        return function(*args, **kwargs)
