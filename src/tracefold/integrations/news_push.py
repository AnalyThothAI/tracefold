from __future__ import annotations

from collections.abc import Mapping
from math import isfinite
from typing import Any
from urllib.parse import urlsplit

import httpx

from tracefold.news import (
    NewsPushDeliveryError,
    NewsPushReceipt,
    NewsPushTranslation,
    NewsPushTranslationError,
)

from .feishu import (
    FEISHU_AUTH_MODE_SIGNED,
    FEISHU_AUTH_MODE_UNSIGNED,
    FeishuDeliveryError,
    FeishuRetryableError,
    FeishuTerminalError,
    FeishuWebhookClient,
)
from .news_ai import (
    DEEPSEEK_TITLE_TRANSLATION_MODEL,
    DeepSeekTitleTranslationError,
    DeepSeekTitleTranslator,
)

_FEISHU_CHANNEL = "feishu"


class DeepSeekNewsPushTranslator:
    """Adapt the raw DeepSeek title client to the News push contract."""

    def __init__(
        self,
        api_key: str | None,
        base_url: str = "",
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        normalized_key = str(api_key or "").strip()
        self._client = (
            DeepSeekTitleTranslator(
                api_key=normalized_key,
                base_url=base_url,
                transport=transport,
            )
            if normalized_key
            else None
        )

    def translate_title(self, title: str) -> NewsPushTranslation:
        if self._client is None:
            raise NewsPushTranslationError("news_push_translation_api_key_unavailable")
        try:
            translated = self._client.translate(title)
        except DeepSeekTitleTranslationError as error:
            raise NewsPushTranslationError(str(error)) from None
        return NewsPushTranslation(
            title_zh=translated,
            provider="deepseek",
            model=DEEPSEEK_TITLE_TRANSLATION_MODEL,
        )

    def close(self) -> None:
        if self._client is not None:
            self._client.close()


class FeishuNewsPushDelivery:
    """Render one fixed News card, then deliver only its frozen envelope."""

    def __init__(
        self,
        webhook_url: str,
        signing_secret: str | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = FeishuWebhookClient(
            webhook_url=webhook_url,
            signing_secret=signing_secret,
            transport=transport,
        )

    def render(
        self,
        source_payload: Mapping[str, Any],
        translation: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        try:
            frozen_translation = _translation_payload(translation)
            card = _news_story_card(source_payload, frozen_translation)
        except NewsPushDeliveryError:
            raise
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            raise NewsPushDeliveryError(
                "news_push_feishu_render_payload_invalid",
                retryable=False,
            ) from None
        return {
            "channel": _FEISHU_CHANNEL,
            "auth_mode": self._client.auth_mode,
            "translation": frozen_translation,
            "card": card,
        }

    def deliver(
        self,
        payload: Mapping[str, Any],
        *,
        idempotency_key: str,
    ) -> NewsPushReceipt:
        del idempotency_key  # Feishu custom-bot webhooks provide no idempotency field.
        try:
            if payload.get("channel") != _FEISHU_CHANNEL:
                raise NewsPushDeliveryError(
                    "news_push_feishu_frozen_channel_invalid",
                    retryable=False,
                )
            card = payload.get("card")
            if not isinstance(card, Mapping) or card.get("schema") != "2.0":
                raise NewsPushDeliveryError(
                    "news_push_feishu_frozen_card_invalid",
                    retryable=False,
                )
            auth_mode = payload.get("auth_mode")
            if not isinstance(auth_mode, str) or auth_mode not in (
                FEISHU_AUTH_MODE_SIGNED,
                FEISHU_AUTH_MODE_UNSIGNED,
            ):
                raise NewsPushDeliveryError(
                    "news_push_feishu_frozen_auth_mode_invalid",
                    retryable=False,
                )
            if auth_mode != self._client.auth_mode:
                raise NewsPushDeliveryError(
                    "news_push_feishu_auth_mode_mismatch",
                    retryable=False,
                )
            receipt = self._client.send(card)
        except NewsPushDeliveryError:
            raise
        except FeishuRetryableError as error:
            raise NewsPushDeliveryError(error.code, retryable=True) from None
        except (FeishuTerminalError, FeishuDeliveryError) as error:
            raise NewsPushDeliveryError(error.code, retryable=False) from None
        return NewsPushReceipt(
            provider=_FEISHU_CHANNEL,
            details={
                "status_code": receipt.status_code,
                "code": receipt.code,
            },
        )

    def close(self) -> None:
        self._client.close()


def _translation_payload(translation: Mapping[str, Any]) -> dict[str, Any]:
    status = _required_text(translation, "status")
    if status not in {"translated", "not_needed", "unavailable"}:
        raise ValueError("news_push_translation_status_invalid")
    title_zh_value = translation.get("title_zh")
    title_zh = str(title_zh_value).strip() if title_zh_value is not None else None
    if status in {"translated", "not_needed"} and not title_zh:
        raise ValueError("news_push_translation_title_required")
    if status == "unavailable" and title_zh is not None:
        raise ValueError("news_push_translation_unavailable_title_invalid")
    return {
        "status": status,
        "title_zh": title_zh,
        "provider": _optional_text(translation.get("provider")),
        "model": _optional_text(translation.get("model")),
        "error_code": _optional_text(translation.get("error_code")),
    }


def _news_story_card(
    source_payload: Mapping[str, Any],
    translation: Mapping[str, Any],
) -> dict[str, Any]:
    evidence_value = source_payload.get("provider_evidence")
    if not isinstance(evidence_value, Mapping):
        raise ValueError("news_push_source_payload_invalid")

    original_title = _required_text(evidence_value, "title")
    title_zh = _optional_text(translation.get("title_zh"))
    headline = title_zh or original_title
    symbols = _provider_coin_symbols(evidence_value)
    score = _provider_score_text(evidence_value)
    coins_text = " · ".join(symbols) if symbols else "未提供"
    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "text": {
                "tag": "plain_text",
                "content": f"代币：{coins_text}\nOpenNews 评分：{score}",
            },
            "margin": "0px 0px 0px 0px",
        }
    ]
    source_url = _optional_http_url(evidence_value.get("url"))
    if source_url is not None:
        elements.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "查看原文"},
                "type": "default",
                "width": "default",
                "size": "medium",
                "behaviors": [
                    {
                        "type": "open_url",
                        "default_url": source_url,
                    }
                ],
                "margin": "8px 0px 0px 0px",
            }
        )
    return {
        "schema": "2.0",
        "body": {
            "direction": "vertical",
            "padding": "12px 12px 12px 12px",
            "elements": elements,
        },
        "header": {
            "title": {"tag": "plain_text", "content": headline},
        },
    }


def _provider_coin_symbols(evidence: Mapping[str, Any]) -> tuple[str, ...]:
    metadata = evidence.get("provider_metadata")
    if not isinstance(metadata, Mapping):
        return ()
    coins = metadata.get("coins")
    if not isinstance(coins, list):
        return ()
    symbols: list[str] = []
    seen: set[str] = set()
    for coin in coins:
        if not isinstance(coin, Mapping):
            continue
        symbol_value = coin.get("symbol")
        if not isinstance(symbol_value, str):
            continue
        symbol = symbol_value.strip()
        identity = symbol.casefold()
        if not symbol or identity in seen:
            continue
        seen.add(identity)
        symbols.append(symbol)
    return tuple(symbols)


def _provider_score_text(evidence: Mapping[str, Any]) -> str:
    value = evidence.get("provider_score")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("news_push_provider_score_invalid")
    score = float(value)
    if not isfinite(score) or score < 0 or score > 100:
        raise ValueError("news_push_provider_score_invalid")
    return f"{score:g}"


def _optional_http_url(value: Any) -> str | None:
    url = _optional_text(value)
    if url is None:
        return None
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ValueError("news_push_source_url_invalid")
    return url


def _required_text(payload: Mapping[str, Any], key: str) -> str:
    value = _optional_text(payload.get(key))
    if value is None:
        raise ValueError(f"news_push_{key}_required")
    return value


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


__all__ = [
    "DeepSeekNewsPushTranslator",
    "FeishuNewsPushDelivery",
]
