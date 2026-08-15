from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from tracefold.news.push import (
    PUSH_PAYLOAD_SCHEMA_VERSION,
    NewsPushExternalError,
    NewsPushReceipt,
)

from .feishu import (
    FeishuDeliveryError,
    FeishuWebhookClient,
)

_TRANSLATION_REQUEST_TIMEOUT_SECONDS = 2.25
_FEISHU = "feishu"
_TEXT_LIMIT = 500


class OpenAICompatibleNewsPushTranslator:
    """One title-only OpenAI-compatible request with no retry policy."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._model = str(model).strip()
        self._client = httpx.Client(
            base_url=str(base_url).strip().rstrip("/") + "/",
            timeout=httpx.Timeout(_TRANSLATION_REQUEST_TIMEOUT_SECONDS),
            headers={
                "Authorization": f"Bearer {str(api_key).strip()}",
                "Content-Type": "application/json",
            },
            follow_redirects=False,
            transport=transport,
        )

    def translate(self, title: str) -> str:
        try:
            response = self._client.post(
                "chat/completions",
                json={
                    "model": self._model,
                    "temperature": 0,
                    "max_tokens": 256,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "将金融新闻标题忠实翻译为简体中文。不得总结、解释、补充事实；"
                                "数字、百分比、金额、$TOKEN 和大写资产符号必须原样保留。"
                                '只输出 JSON：{"translated_title":"单行标题"}'
                            ),
                        },
                        {"role": "user", "content": str(title)},
                    ],
                },
            )
        except httpx.TimeoutException:
            raise NewsPushExternalError("news_item_push_translation_timeout") from None
        except httpx.HTTPError:
            raise NewsPushExternalError("news_item_push_translation_http_error") from None
        if response.status_code == 429:
            raise NewsPushExternalError("news_item_push_translation_rate_limited")
        try:
            response.raise_for_status()
            payload = response.json()
            choice = payload["choices"][0]
            if not isinstance(choice, Mapping) or choice.get("finish_reason") != "stop":
                raise ValueError("translation_choice_invalid")
            message = choice["message"]
            if not isinstance(message, Mapping):
                raise TypeError("translation_message_invalid")
            content = message["content"]
            if not isinstance(content, str):
                raise TypeError("translation_content_invalid")
            translated_payload = json.loads(content)
            if not isinstance(translated_payload, Mapping):
                raise TypeError("translation_payload_invalid")
            translated = translated_payload.get("translated_title")
            if not isinstance(translated, str):
                raise TypeError("translation_title_invalid")
            return translated
        except httpx.HTTPStatusError:
            raise NewsPushExternalError("news_item_push_translation_http_error") from None
        except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError):
            raise NewsPushExternalError("news_item_push_translation_response_invalid") from None

    def close(self) -> None:
        self._client.close()


class FeishuNewsPushSender:
    """Render and send one immutable Item snapshot; retries are intentionally absent."""

    def __init__(
        self,
        *,
        webhook_url: str,
        signing_secret: str | None = None,
        transport: httpx.BaseTransport | None = None,
        timestamp_seconds: Callable[[], int] | None = None,
    ) -> None:
        self._client = FeishuWebhookClient(
            webhook_url=webhook_url,
            signing_secret=signing_secret,
            transport=transport,
        )
        self._timestamp_seconds = timestamp_seconds or (lambda: int(time.time()))

    def send(
        self,
        source_payload: Mapping[str, Any],
        presentation_snapshot: Mapping[str, Any],
    ) -> NewsPushReceipt:
        card = _item_card(source_payload, presentation_snapshot)
        try:
            receipt = self._client.send(
                card,
                timestamp_seconds=self._timestamp_seconds(),
            )
        except FeishuDeliveryError as exc:
            raise NewsPushExternalError(f"news_item_push_{exc.code}") from None
        return NewsPushReceipt(
            provider=_FEISHU,
            details={"code": receipt.code, "status_code": receipt.status_code},
        )

    def close(self) -> None:
        self._client.close()


def _item_card(
    source: Mapping[str, Any],
    presentation: Mapping[str, Any],
) -> dict[str, Any]:
    if source.get("schema_version") != PUSH_PAYLOAD_SCHEMA_VERSION:
        raise NewsPushExternalError("news_item_push_source_schema_invalid")
    title = _required_text(source, "original_title")
    display_title = _required_text(presentation, "display_title")
    outcome = str(presentation.get("outcome") or "")
    if outcome not in {"translated", "fallback", "not_needed"}:
        raise NewsPushExternalError("news_item_push_presentation_invalid")

    facts = [
        f"原文：{_preview(title)}",
        f"来源：{_preview(_required_text(source, 'reporting_origin'), 120)}",
        f"发布时间：{_published_time(source.get('provider_published_at_ms'))}",
    ]
    strategy_labels = _string_list(source.get("strategy_labels"))
    if strategy_labels:
        facts.append(f"命中策略：{' · '.join(_preview(value, 120) for value in strategy_labels)}")
    assets = _asset_labels(source.get("assets"))
    if assets:
        facts.append(f"关联资产：{' · '.join(assets)}")
    if source.get("score") is not None:
        facts.append(f"OpenNews 评分：{source['score']}")
    for key, label in (("signal", "信号"), ("grade", "等级")):
        value = str(source.get(key) or "").strip()
        if value:
            facts.append(f"{label}：{_preview(value, 80)}")
    if outcome == "fallback":
        facts.append("中文翻译暂不可用，已发送原文")

    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "text": {"tag": "plain_text", "content": "\n".join(facts)},
        }
    ]
    source_url = _optional_http_url(source.get("source_url"))
    if source_url is not None:
        elements.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "查看原文"},
                "type": "default",
                "behaviors": [{"type": "open_url", "default_url": source_url}],
            }
        )
    return {
        "schema": "2.0",
        "header": {
            "title": {"tag": "plain_text", "content": _preview(display_title, 120)},
        },
        "body": {"elements": elements},
    }


def _required_text(payload: Mapping[str, Any], key: str) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise NewsPushExternalError("news_item_push_render_payload_invalid")
    return value


def _string_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(text for item in value if (text := str(item).strip()))


def _asset_labels(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    labels: list[str] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        symbol = str(item.get("symbol") or "").strip()
        market_type = str(item.get("market_type") or "").strip()
        if symbol:
            labels.append(_preview(f"{symbol} ({market_type})" if market_type else symbol, 80))
    return tuple(labels)


def _optional_http_url(value: object) -> str | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    parsed = urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise NewsPushExternalError("news_item_push_source_url_invalid")
    return normalized


def _published_time(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise NewsPushExternalError("news_item_push_render_payload_invalid")
    try:
        return datetime.fromtimestamp(float(value) / 1000, tz=UTC).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        raise NewsPushExternalError("news_item_push_render_payload_invalid") from None


def _preview(value: str, limit: int = _TEXT_LIMIT) -> str:
    normalized = " ".join(value.split())
    return normalized if len(normalized) <= limit else normalized[: limit - 1].rstrip() + "…"


__all__ = ["FeishuNewsPushSender", "OpenAICompatibleNewsPushTranslator"]
