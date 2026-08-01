from __future__ import annotations

import json

import httpx
import pytest
import yaml
from pydantic import ValidationError

from tracefold.app.cli.commands import config as config_command
from tracefold.integrations.feishu import (
    FEISHU_WEBHOOK_REQUEST_MAX_BYTES,
    FeishuRetryableError,
    FeishuTerminalError,
    FeishuWebhookClient,
    generate_feishu_signature,
)
from tracefold.integrations.news_ai import (
    DEEPSEEK_TITLE_TRANSLATION_MODEL,
    DeepSeekTitleTranslationError,
    DeepSeekTitleTranslator,
)
from tracefold.integrations.news_push import (
    DeepSeekNewsPushTranslator,
    FeishuNewsPushDelivery,
)
from tracefold.news import NewsPushDeliveryError, NewsPushTranslationError
from tracefold.platform.config.settings import NewsPushSettings, Settings, default_config_yaml

_FEISHU_TEST_URL = "https://open.feishu.cn/open-apis/bot/v2/hook/test-hook-id"


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


def test_config_diagnostics_expose_only_news_push_configured_booleans(monkeypatch, tmp_path) -> None:
    settings = Settings(
        news={
            "push": {
                "enabled": True,
                "feishu_webhook_url": _FEISHU_TEST_URL,
                "feishu_signing_secret": "test-signing-secret",
            }
        }
    )
    settings.set_config_dir(tmp_path)
    monkeypatch.setattr(config_command, "load_settings", lambda **_kwargs: settings)

    code, payload = config_command.handle_config(object())

    assert code == 0
    assert payload["data"]["news"]["push"] == {
        "enabled": True,
        "feishu_webhook_url_configured": True,
        "feishu_signing_secret_configured": True,
    }
    rendered = json.dumps(payload)
    assert _FEISHU_TEST_URL not in rendered
    assert "test-signing-secret" not in rendered


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


def test_deepseek_title_translator_uses_v4_flash_non_thinking_and_bounded_json_output() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": '{"translated_title":"美联储维持利率不变"}'},
                    }
                ]
            },
        )

    translator = DeepSeekTitleTranslator(
        api_key="test-api-key",
        transport=httpx.MockTransport(handler),
    )
    try:
        translated = translator.translate("Fed holds rates steady")
    finally:
        translator.close()

    assert translated == "美联储维持利率不变"
    assert len(requests) == 1
    assert str(requests[0].url) == "https://api.deepseek.com/chat/completions"
    assert requests[0].headers["authorization"] == "Bearer test-api-key"
    payload = json.loads(requests[0].content)
    assert payload["model"] == DEEPSEEK_TITLE_TRANSLATION_MODEL == "deepseek-v4-flash"
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["max_tokens"] == 128
    assert payload["response_format"] == {"type": "json_object"}
    assert json.loads(payload["messages"][1]["content"]) == {"source_title": "Fed holds rates steady"}


@pytest.mark.parametrize(
    "choice",
    [
        {"finish_reason": "length", "message": {"content": '{"translated_title":"截断"}'}},
        {"finish_reason": "stop", "message": {"content": "not-json"}},
        {"finish_reason": "stop", "message": {"content": '{"translated_title":"line 1\\nline 2"}'}},
    ],
)
def test_deepseek_title_translator_fails_closed_on_invalid_model_output(choice: dict[str, object]) -> None:
    translator = DeepSeekTitleTranslator(
        api_key="test-api-key",
        base_url="https://deepseek.test/v1",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"choices": [choice]})),
    )
    try:
        with pytest.raises(DeepSeekTitleTranslationError, match="news_push_translation_failed"):
            translator.translate("Fed holds rates steady")
    finally:
        translator.close()


def test_deepseek_news_push_translator_returns_domain_translation() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": '{"translated_title":"比特币 ETF 录得资金流入"}'},
                    }
                ]
            },
        )

    translator = DeepSeekNewsPushTranslator(
        "test-api-key",
        transport=httpx.MockTransport(handler),
    )
    try:
        result = translator.translate_title("Bitcoin ETF records inflows")
    finally:
        translator.close()

    assert result.title_zh == "比特币 ETF 录得资金流入"
    assert result.provider == "deepseek"
    assert result.model == "deepseek-v4-flash"
    assert len(requests) == 1


def test_deepseek_news_push_translator_without_key_defers_to_english_fallback() -> None:
    translator = DeepSeekNewsPushTranslator(None)
    try:
        with pytest.raises(
            NewsPushTranslationError,
            match="news_push_translation_api_key_unavailable",
        ):
            translator.translate_title("Bitcoin ETF records inflows")
    finally:
        translator.close()


def test_deepseek_news_push_translator_maps_sanitized_raw_failure() -> None:
    translator = DeepSeekNewsPushTranslator(
        "test-api-key",
        transport=httpx.MockTransport(lambda _request: httpx.Response(503)),
    )
    try:
        with pytest.raises(NewsPushTranslationError) as raised:
            translator.translate_title("Bitcoin ETF records inflows")
    finally:
        translator.close()

    assert raised.value.code == "news_push_translation_failed"


def test_feishu_news_push_renders_title_only_v2_card() -> None:
    delivery = FeishuNewsPushDelivery(
        _FEISHU_TEST_URL,
        "test-secret",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"code": 0})),
    )
    try:
        rendered = delivery.render(
            _news_push_source_payload(
                title="Original [alert](https://evil.test) <at id=all></at>",
                url="https://example.com/story/1",
            ),
            _news_push_translation(),
        )
    finally:
        delivery.close()

    assert rendered["channel"] == "feishu"
    assert rendered["translation"] == _news_push_translation()
    assert rendered["card"] == {
        "schema": "2.0",
        "header": {
            "title": {
                "tag": "plain_text",
                "content": "[BTC · ETH] 比特币 ETF 录得资金流入",
            }
        },
    }


def test_feishu_news_push_english_fallback_is_title_only() -> None:
    delivery = FeishuNewsPushDelivery(
        _FEISHU_TEST_URL,
        "test-secret",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"code": 0})),
    )
    try:
        rendered = delivery.render(
            _news_push_source_payload(title="Fed holds rates steady", url=None),
            {
                "status": "unavailable",
                "title_zh": None,
                "provider": None,
                "model": None,
                "error_code": "news_push_translation_failed",
            },
        )
    finally:
        delivery.close()

    assert rendered["card"] == {
        "schema": "2.0",
        "header": {
            "title": {
                "tag": "plain_text",
                "content": "[BTC · ETH] Fed holds rates steady",
            }
        },
    }


def test_feishu_news_push_chinese_original_needs_no_translation() -> None:
    delivery = FeishuNewsPushDelivery(
        _FEISHU_TEST_URL,
        "test-secret",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"code": 0})),
    )
    try:
        rendered = delivery.render(
            _news_push_source_payload(title="比特币 ETF 录得资金流入", url=None),
            {
                "status": "not_needed",
                "title_zh": "比特币 ETF 录得资金流入",
                "provider": None,
                "model": None,
                "error_code": None,
            },
        )
    finally:
        delivery.close()

    assert rendered["card"] == {
        "schema": "2.0",
        "header": {
            "title": {
                "tag": "plain_text",
                "content": "[BTC · ETH] 比特币 ETF 录得资金流入",
            }
        },
    }


def test_feishu_news_push_coin_prefix_preserves_order_and_deduplicates() -> None:
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
        rendered = delivery.render(source, _news_push_translation())
    finally:
        delivery.close()

    assert rendered["card"]["header"]["title"]["content"] == ("[NEAR · BTC] 比特币 ETF 录得资金流入")


@pytest.mark.parametrize("coins", (None, [], "BTC", [{"market_type": "spot"}]))
def test_feishu_news_push_without_valid_coins_keeps_plain_headline(coins: object) -> None:
    delivery = FeishuNewsPushDelivery(
        _FEISHU_TEST_URL,
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"code": 0})),
    )
    source = _news_push_source_payload()
    source["provider_evidence"]["provider_metadata"]["coins"] = coins
    try:
        rendered = delivery.render(source, _news_push_translation())
    finally:
        delivery.close()

    assert rendered["card"]["header"]["title"]["content"] == "比特币 ETF 录得资金流入"


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
    translation = _news_push_translation()
    frozen = delivery.render(source, translation)
    source["provider_evidence"]["title"] = "MUTATED SOURCE"
    source["provider_evidence"]["provider_metadata"]["coins"][0]["symbol"] = "SOL"
    translation["title_zh"] = "已变更翻译"
    try:
        first = delivery.deliver(frozen, idempotency_key="story-1")
        second = delivery.deliver(frozen, idempotency_key="story-1")
    finally:
        delivery.close()

    assert first.provider == second.provider == "feishu"
    assert first.details == {"status_code": 200, "code": 0}
    assert frozen["auth_mode"] == "signed"
    assert set(frozen) == {"channel", "auth_mode", "translation", "card"}
    sent_cards = [json.loads(request.content)["card"] for request in requests]
    assert sent_cards == [frozen["card"], frozen["card"]]
    assert all(card["header"]["title"]["content"] == "[BTC · ETH] 比特币 ETF 录得资金流入" for card in sent_cards)
    assert all("MUTATED SOURCE" not in json.dumps(card) for card in sent_cards)
    assert all("已变更翻译" not in json.dumps(card, ensure_ascii=False) for card in sent_cards)


def test_feishu_news_push_delivers_legacy_frozen_v2_card_unchanged() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"code": 0})

    legacy_card = {
        "schema": "2.0",
        "config": {"update_multi": True},
        "body": {
            "elements": [
                {"tag": "markdown", "content": "legacy source facts"},
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "查看原文"},
                },
            ]
        },
        "header": {
            "title": {"tag": "plain_text", "content": "旧卡片标题"},
            "subtitle": {"tag": "plain_text", "content": "高分 News Story"},
            "template": "blue",
        },
    }
    frozen = {
        "channel": "feishu",
        "auth_mode": "signed",
        "translation": _news_push_translation(),
        "card": legacy_card,
    }
    delivery = FeishuNewsPushDelivery(
        _FEISHU_TEST_URL,
        "test-secret",
        transport=httpx.MockTransport(handler),
    )
    try:
        receipt = delivery.deliver(frozen, idempotency_key="legacy-story")
    finally:
        delivery.close()

    assert receipt.provider == "feishu"
    assert len(requests) == 1
    assert json.loads(requests[0].content)["card"] == legacy_card


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
    frozen = rendered_by.render(_news_push_source_payload(), _news_push_translation())
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
    frozen = first_runtime.render(_news_push_source_payload(), _news_push_translation())
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
    assert set(frozen) == {"channel", "auth_mode", "translation", "card"}
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
    frozen = delivery.render(_news_push_source_payload(), _news_push_translation())
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
                {"channel": "feishu", "card": {"schema": "1.0"}},
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
                "coins": [
                    {"symbol": "BTC", "market_type": "spot"},
                    {"symbol": "ETH", "market_type": "spot"},
                ],
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


def _news_push_translation() -> dict[str, object]:
    return {
        "status": "translated",
        "title_zh": "比特币 ETF 录得资金流入",
        "provider": "deepseek",
        "model": "deepseek-v4-flash",
        "error_code": None,
    }
