from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import UTC, datetime
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
_MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+.!|~<>-])")


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
    story_value = source_payload.get("tracefold_story")
    if not isinstance(evidence_value, Mapping) or not isinstance(story_value, Mapping):
        raise ValueError("news_push_source_payload_invalid")
    evidence = dict(evidence_value)
    story = dict(story_value)
    metadata_value = evidence.get("provider_metadata")
    if not isinstance(metadata_value, Mapping):
        raise ValueError("news_push_provider_metadata_invalid")
    metadata = dict(metadata_value)

    original_title = _required_text(evidence, "title")
    translation_status = _required_text(translation, "status")
    title_zh = _optional_text(translation.get("title_zh"))
    displayed_title = title_zh or original_title
    header_title = displayed_title if translation_status != "unavailable" else f"{displayed_title}（中文翻译不可用）"
    displayed_title_label = (
        "**中文标题**" if translation_status != "unavailable" else "**标题（中文翻译不可用，使用英文原题）**"
    )
    provider_score = _finite_number(evidence.get("provider_score"), "provider_score")
    importance_score = _integer(story.get("importance_score"), "importance_score")
    item_count = _nonnegative_integer(story.get("item_count"), "item_count")
    source_count = _nonnegative_integer(story.get("source_count"), "source_count")
    source = _required_text(evidence, "reporting_origin")
    published_at = _published_at_utc(evidence.get("published_at_ms"))
    signal = _optional_text(metadata.get("signal")) or "—"
    grade = _optional_text(metadata.get("grade")) or "—"
    coins = _coin_symbols(metadata.get("coins"))
    url = _safe_http_url(evidence.get("url"))

    markdown = "\n".join(
        (
            displayed_title_label,
            _escape_lark_markdown(displayed_title),
            "",
            "**原始标题**",
            _escape_lark_markdown(original_title),
            "",
            "**OpenNews 提供方证据**",
            f"评分：{_display_number(provider_score)}",
            f"信号：{_escape_lark_markdown(signal)}",
            f"等级：{_escape_lark_markdown(grade)}",
            "",
            "**Tracefold Story（独立评分）**",
            f"重要性：{importance_score}",
            "",
            "**新闻事实**",
            f"来源：{_escape_lark_markdown(source)}",
            f"时间（UTC）：{published_at}",
            f"代币：{_escape_lark_markdown(coins)}",
            f"Story 成员：{item_count} 条",
            f"独立来源：{source_count} 个",
            *(() if url else ("原文链接：不可用（linkless）",)),
        )
    )
    elements: list[dict[str, Any]] = [
        {
            "tag": "markdown",
            "content": markdown,
            "text_align": "left",
            "text_size": "normal_v2",
            "margin": "0px 0px 0px 0px",
        }
    ]
    if url:
        elements.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "查看原文"},
                "type": "primary",
                "width": "default",
                "size": "medium",
                "behaviors": [
                    {
                        "type": "open_url",
                        "default_url": url,
                        "pc_url": "",
                        "ios_url": "",
                        "android_url": "",
                    }
                ],
                "margin": "0px 0px 0px 0px",
            }
        )
    return {
        "schema": "2.0",
        "config": {
            "update_multi": True,
            "style": {
                "text_size": {
                    "normal_v2": {
                        "default": "normal",
                        "pc": "normal",
                        "mobile": "heading",
                    }
                }
            },
        },
        "body": {
            "direction": "vertical",
            "padding": "12px 12px 12px 12px",
            "elements": elements,
        },
        "header": {
            "title": {"tag": "plain_text", "content": header_title},
            "subtitle": {"tag": "plain_text", "content": "高分 News Story"},
            "template": "blue",
            "padding": "12px 12px 12px 12px",
        },
    }


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


def _finite_number(value: object, field: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"news_push_{field}_invalid")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ValueError(f"news_push_{field}_invalid")
    return int(numeric) if numeric.is_integer() else numeric


def _integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"news_push_{field}_invalid")
    return value


def _nonnegative_integer(value: object, field: str) -> int:
    result = _integer(value, field)
    if result < 0:
        raise ValueError(f"news_push_{field}_invalid")
    return result


def _published_at_utc(value: object) -> str:
    published_at_ms = _nonnegative_integer(value, "published_at_ms")
    return datetime.fromtimestamp(published_at_ms / 1_000, tz=UTC).strftime("%Y-%m-%d %H:%M:%S")


def _coin_symbols(value: object) -> str:
    if not isinstance(value, list):
        return "—"
    symbols: list[str] = []
    seen: set[str] = set()
    for coin in value:
        if not isinstance(coin, Mapping):
            continue
        symbol = _optional_text(coin.get("symbol"))
        if symbol is None or symbol in seen:
            continue
        seen.add(symbol)
        symbols.append(symbol)
    return " · ".join(symbols) if symbols else "—"


def _safe_http_url(value: object) -> str | None:
    url = _optional_text(value)
    if url is None:
        return None
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 80, 443}
    ):
        return None
    return url


def _display_number(value: int | float) -> str:
    return str(value)


def _escape_lark_markdown(value: str) -> str:
    return _MARKDOWN_SPECIAL.sub(r"\\\1", value)


__all__ = [
    "DeepSeekNewsPushTranslator",
    "FeishuNewsPushDelivery",
]
