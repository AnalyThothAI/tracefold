from __future__ import annotations

import asyncio
import json
import re
import time
import unicodedata
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from math import isfinite
from typing import Any
from urllib.parse import urlsplit

import httpx

from tracefold.news import (
    NewsPushDeliveryError,
    NewsPushReceipt,
    PreparedNewsPush,
)
from tracefold.platform.resource import ResourceAdmissionTimeout, ResourceOperationOverrun

from .feishu import (
    FEISHU_AUTH_MODE_SIGNED,
    FEISHU_AUTH_MODE_UNSIGNED,
    FeishuDeliveryError,
    FeishuRetryableError,
    FeishuTerminalError,
    FeishuWebhookClient,
)

_FEISHU_CHANNEL = "feishu"
_DELIVERY_SCHEMA_VERSION = "news_feishu_delivery_v2"
_TRANSLATION_PROVIDER = "openai_compatible"
_TRANSLATION_PROMPT_VERSION = "title_zh_v2"
_TRANSLATION_TARGET_LANGUAGE = "zh-CN"
_TRANSLATION_TOTAL_TIMEOUT_SECONDS = 8.0
_TRANSLATION_REQUEST_TIMEOUT_SECONDS = 7.5
_TRANSLATION_INPUT_GRAPHEME_CAP = 500
_TRANSLATION_OUTPUT_GRAPHEME_CAP = 600
_HEADER_GRAPHEME_CAP = 120
_BODY_GRAPHEME_CAP = 300

_NUMBER_ANCHOR = re.compile(r"(?<![\w])\d+(?:[.,]\d+)*(?:[%％])?(?![\w])")
_DOLLAR_TICKER_ANCHOR = re.compile(r"\$[A-Za-z][A-Za-z0-9]{0,15}")


class _TitleTranslationError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = str(code)[:500]
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class _TranslationOutcome:
    translated_title: str | None
    fallback_code: str | None
    attempted_at_ms: int | None = None
    duration_ms: int | None = None


class _OpenAiCompatibleTitleTranslator:
    """One code-owned request shape for one configured low-latency endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        engine: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.engine = str(engine).strip()
        self._url = f"{str(base_url).strip().rstrip('/')}/chat/completions"
        self._client = httpx.Client(
            timeout=httpx.Timeout(_TRANSLATION_REQUEST_TIMEOUT_SECONDS),
            headers={
                "Authorization": f"Bearer {str(api_key).strip()}",
                "Content-Type": "application/json",
            },
            follow_redirects=False,
            transport=transport,
        )

    def translate(self, title: str, required_verbatim: tuple[str, ...]) -> str:
        try:
            response = self._client.post(
                self._url,
                json={
                    "model": self.engine,
                    "temperature": 0,
                    "max_tokens": 256,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "你是金融新闻标题翻译器。只将输入标题忠实翻译为简体中文；"
                                "不得总结、解释、补充背景、因果、评价或新事实。保留数字、百分比、"
                                "货币单位、$TOKEN 和代币符号。required_verbatim 数组中的每一项"
                                "都必须原样出现在译文中。只输出 JSON："
                                '{"translated_title":"单行简体中文标题"}'
                            ),
                        },
                        {
                            "role": "user",
                            "content": json.dumps(
                                {
                                    "source_title": title,
                                    "required_verbatim": list(required_verbatim),
                                },
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        },
                    ],
                },
            )
        except httpx.TimeoutException:
            raise _TitleTranslationError("news_push_translation_timeout") from None
        except httpx.HTTPError:
            raise _TitleTranslationError("news_push_translation_http_error") from None

        if response.status_code == 429:
            raise _TitleTranslationError("news_push_translation_rate_limited")
        try:
            response.raise_for_status()
            payload = response.json()
            choice = payload["choices"][0]
            if not isinstance(choice, Mapping):
                raise TypeError("translation_choice_invalid")
            if choice.get("finish_reason") != "stop":
                raise ValueError("translation_incomplete")
            content = choice["message"]["content"]
            if not isinstance(content, str):
                raise TypeError("translation_content_invalid")
            translated_payload = json.loads(content)
            if not isinstance(translated_payload, Mapping):
                raise TypeError("translation_shape_invalid")
            translated = translated_payload.get("translated_title")
            if not isinstance(translated, str):
                raise TypeError("translation_title_invalid")
            return translated.strip()
        except httpx.HTTPStatusError:
            raise _TitleTranslationError("news_push_translation_http_error") from None
        except (
            json.JSONDecodeError,
            KeyError,
            IndexError,
            RecursionError,
            TypeError,
            ValueError,
        ):
            raise _TitleTranslationError("news_push_translation_response_invalid") from None

    def close(self) -> None:
        self._client.close()


class FeishuNewsPushDelivery:
    """Prepare one immutable bilingual News card, then deliver only that card."""

    def __init__(
        self,
        webhook_url: str,
        signing_secret: str | None = None,
        *,
        finite_operations: Any | None = None,
        translation_enabled: bool = False,
        translation_base_url: str | None = None,
        translation_api_key: str | None = None,
        translation_engine: str | None = None,
        transport: httpx.BaseTransport | None = None,
        translation_transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = FeishuWebhookClient(
            webhook_url=webhook_url,
            signing_secret=signing_secret,
            transport=transport,
        )
        self._finite_operations = finite_operations
        self._translator: _OpenAiCompatibleTitleTranslator | None = None
        if translation_enabled:
            if not (
                finite_operations is not None
                and str(translation_base_url or "").strip()
                and str(translation_api_key or "").strip()
                and str(translation_engine or "").strip()
            ):
                self._client.close()
                raise ValueError("news_push_translation_configuration_required")
            self._translator = _OpenAiCompatibleTitleTranslator(
                base_url=str(translation_base_url),
                api_key=str(translation_api_key),
                engine=str(translation_engine),
                transport=translation_transport,
            )

    async def prepare(
        self,
        source_payload: Mapping[str, Any],
        *,
        deadline_ms: int,
        before_translation_submit: Callable[[], Awaitable[None]] | None = None,
        interrupted_translation_attempted_at_ms: int | None = None,
    ) -> PreparedNewsPush:
        try:
            evidence_value = source_payload.get("provider_evidence")
            if not isinstance(evidence_value, Mapping):
                raise ValueError("news_push_source_payload_invalid")
            original_title = _required_text(evidence_value, "title")
            source_url = _optional_http_url(evidence_value.get("url"))
            symbols = _provider_coin_symbols(evidence_value)
            score = _provider_score_text(evidence_value)
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            raise NewsPushDeliveryError(
                "news_push_feishu_render_payload_invalid",
                retryable=False,
            ) from None

        translated_title: str | None = None
        translation_status = "not_needed"
        fallback_code: str | None = None
        translation_attempted_at_ms: int | None = None
        translation_duration_ms: int | None = None
        if interrupted_translation_attempted_at_ms is not None:
            if int(interrupted_translation_attempted_at_ms) < 0:
                raise ValueError("news_push_translation_attempted_at_ms_invalid")
            translation_status = "unavailable"
            fallback_code = "news_push_translation_interrupted_after_dispatch"
            translation_attempted_at_ms = int(interrupted_translation_attempted_at_ms)
        elif self._translator is not None and not _looks_chinese_original(original_title):
            if _grapheme_count_exceeds(original_title, _TRANSLATION_INPUT_GRAPHEME_CAP):
                translation_status = "unavailable"
                fallback_code = "news_push_translation_title_too_long"
            else:
                outcome = await self._translate_once(
                    original_title,
                    symbols=symbols,
                    deadline_ms=int(deadline_ms),
                    before_submit=before_translation_submit,
                )
                translated_title = outcome.translated_title
                fallback_code = outcome.fallback_code
                translation_attempted_at_ms = outcome.attempted_at_ms
                translation_duration_ms = outcome.duration_ms
                translation_status = "translated" if translated_title is not None else "unavailable"

        headline_mode = (
            "translated"
            if translation_status == "translated"
            else "fallback_original"
            if translation_status == "unavailable"
            else "source"
        )
        card = _news_story_card(
            original_title=original_title,
            translated_title=translated_title,
            translation_status=translation_status,
            fallback_code=fallback_code,
            symbols=symbols,
            score=score,
            source_url=source_url,
        )
        presentation: dict[str, Any] = {
            "headline_mode": headline_mode,
            "target_language": _TRANSLATION_TARGET_LANGUAGE,
            "provider": _TRANSLATION_PROVIDER if translated_title is not None else None,
            "engine": self._translator.engine if translated_title is not None else None,
            "prompt_version": _TRANSLATION_PROMPT_VERSION,
            "fallback_code": fallback_code,
        }
        if translation_attempted_at_ms is not None:
            presentation["translation_attempted_at_ms"] = translation_attempted_at_ms
            presentation["translation_duration_ms"] = translation_duration_ms
        return PreparedNewsPush(
            payload={
                "schema_version": _DELIVERY_SCHEMA_VERSION,
                "channel": _FEISHU_CHANNEL,
                "auth_mode": self._client.auth_mode,
                "presentation": presentation,
                "card": card,
            },
            translation_status=translation_status,
        )

    async def _translate_once(
        self,
        original_title: str,
        *,
        symbols: tuple[str, ...],
        deadline_ms: int,
        before_submit: Callable[[], Awaitable[None]] | None,
    ) -> _TranslationOutcome:
        translator = self._translator
        finite_operations = self._finite_operations
        if translator is None or finite_operations is None:
            return _TranslationOutcome(None, "news_push_translation_unavailable")
        remaining_seconds = (int(deadline_ms) - _now_ms()) / 1_000
        total_seconds = min(_TRANSLATION_TOTAL_TIMEOUT_SECONDS, remaining_seconds)
        if total_seconds <= 0:
            return _TranslationOutcome(None, "news_push_translation_freshness_budget_exhausted")
        attempted_at_ms: int | None = None
        attempted_monotonic: float | None = None

        def mark_submitted() -> None:
            nonlocal attempted_at_ms, attempted_monotonic
            attempted_at_ms = _now_ms()
            attempted_monotonic = time.monotonic()

        def outcome(translated: str | None, fallback: str | None) -> _TranslationOutcome:
            duration_ms = (
                max(0, round((time.monotonic() - attempted_monotonic) * 1_000))
                if attempted_monotonic is not None
                else None
            )
            return _TranslationOutcome(
                translated,
                fallback,
                attempted_at_ms=attempted_at_ms,
                duration_ms=duration_ms,
            )

        try:
            required_verbatim = _required_translation_anchors(
                original_title,
                symbols=symbols,
            )
            async with asyncio.timeout(total_seconds):
                translated = await finite_operations.run(
                    "news_story_push_translation",
                    translator.translate,
                    original_title,
                    required_verbatim,
                    timeout_seconds=min(_TRANSLATION_REQUEST_TIMEOUT_SECONDS, total_seconds),
                    before_submit=before_submit,
                    on_submitted=mark_submitted,
                )
            return outcome(
                _validated_translation(
                    translated,
                    required_verbatim=required_verbatim,
                ),
                None,
            )
        except asyncio.CancelledError:
            raise
        except ResourceAdmissionTimeout:
            return outcome(None, "news_push_translation_admission_timeout")
        except ResourceOperationOverrun:
            return outcome(None, "news_push_translation_timeout")
        except TimeoutError:
            return outcome(None, "news_push_translation_total_timeout")
        except _TitleTranslationError as error:
            return outcome(None, error.code)

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
            if payload.get("schema_version") != _DELIVERY_SCHEMA_VERSION:
                raise NewsPushDeliveryError(
                    "news_push_feishu_frozen_schema_invalid",
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
        try:
            if self._translator is not None:
                self._translator.close()
        finally:
            self._client.close()


def _validated_translation(
    value: object,
    *,
    required_verbatim: tuple[str, ...],
) -> str:
    translated = str(value or "").strip()
    if (
        not translated
        or "\n" in translated
        or "\r" in translated
        or not _contains_han(translated)
        or _grapheme_count_exceeds(translated, _TRANSLATION_OUTPUT_GRAPHEME_CAP)
    ):
        raise _TitleTranslationError("news_push_translation_output_invalid")
    translated_casefold = translated.casefold()
    if any(anchor.casefold() not in translated_casefold for anchor in required_verbatim):
        raise _TitleTranslationError("news_push_translation_anchors_changed")
    return translated


def _required_translation_anchors(
    original_title: str,
    *,
    symbols: tuple[str, ...],
) -> tuple[str, ...]:
    candidates: list[tuple[int, str]] = [
        (match.start(), match.group(0))
        for pattern in (_NUMBER_ANCHOR, _DOLLAR_TICKER_ANCHOR)
        for match in pattern.finditer(original_title)
    ]
    for symbol in symbols:
        for match in re.finditer(
            rf"(?<![a-z0-9])\$?{re.escape(symbol)}(?![a-z0-9])",
            original_title,
            flags=re.IGNORECASE,
        ):
            source_token = match.group(0)
            if not source_token.startswith("$") and source_token.isupper():
                candidates.append((match.start(), source_token))

    anchors: list[str] = []
    seen: set[str] = set()
    for _, anchor in sorted(candidates, key=lambda candidate: candidate[0]):
        identity = anchor.casefold()
        if identity in seen:
            continue
        seen.add(identity)
        anchors.append(anchor)
    return tuple(anchors)


def _news_story_card(
    *,
    original_title: str,
    translated_title: str | None,
    translation_status: str,
    fallback_code: str | None,
    symbols: tuple[str, ...],
    score: str,
    source_url: str | None,
) -> dict[str, Any]:
    headline = translated_title or original_title
    headline_preview, headline_clipped = _text_preview(headline, _HEADER_GRAPHEME_CAP)
    original_preview, original_clipped = _text_preview(original_title, _BODY_GRAPHEME_CAP)
    elements: list[dict[str, Any]] = []
    if translation_status == "translated":
        original_label = "原文节选" if original_clipped else "原文"
        content_lines = ["自动翻译，仅供参考"]
        if headline_clipped and translated_title is not None:
            translated_preview, translated_clipped = _text_preview(translated_title, _BODY_GRAPHEME_CAP)
            translated_label = "译文节选" if translated_clipped else "译文"
            content_lines.append(f"{translated_label}：{translated_preview}")
        content_lines.append(f"{original_label}：{original_preview}")
        elements.append(_plain_div("\n".join(content_lines)))
    elif translation_status == "unavailable":
        if fallback_code == "news_push_translation_title_too_long":
            content = f"标题过长，未自动翻译\n原文节选：{original_preview}"
        elif headline_clipped:
            content = f"中文翻译暂不可用，已显示原文节选\n原文节选：{original_preview}"
        else:
            content = "中文翻译暂不可用，已显示原文"
        elements.append(_plain_div(content))
    elif headline_clipped:
        elements.append(_plain_div(f"原文节选：{original_preview}"))

    coins_text = " · ".join(symbols) if symbols else "未提供"
    elements.append(_plain_div(f"代币：{coins_text}\nOpenNews 评分：{score}"))
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
            "title": {"tag": "plain_text", "content": headline_preview},
        },
    }


def _plain_div(content: str) -> dict[str, Any]:
    return {
        "tag": "div",
        "text": {"tag": "plain_text", "content": content},
        "margin": "0px 0px 0px 0px",
    }


def _looks_chinese_original(value: str) -> bool:
    for character in value:
        codepoint = ord(character)
        if (
            0x3040 <= codepoint <= 0x30FF
            or 0x31F0 <= codepoint <= 0x31FF
            or 0xFF66 <= codepoint <= 0xFF9F
            or 0x1100 <= codepoint <= 0x11FF
            or 0x3130 <= codepoint <= 0x318F
            or 0xAC00 <= codepoint <= 0xD7AF
        ):
            return False
    return _contains_han(value)


def _contains_han(value: str) -> bool:
    return any(
        0x3400 <= ord(character) <= 0x4DBF or 0x4E00 <= ord(character) <= 0x9FFF or 0xF900 <= ord(character) <= 0xFAFF
        for character in value
    )


def _text_preview(value: str, limit: int) -> tuple[str, bool]:
    normalized = " ".join(str(value).split())
    clusters = _graphemes(normalized)
    if len(clusters) <= limit:
        return normalized, False
    return "".join(clusters[:limit]).rstrip() + "…", True


def _grapheme_count_exceeds(value: str, limit: int) -> bool:
    return len(_graphemes(" ".join(str(value).split()))) > limit


def _graphemes(value: str) -> list[str]:
    clusters: list[str] = []
    current = ""
    join_next = False
    for character in value:
        codepoint = ord(character)
        extends = (
            bool(unicodedata.combining(character)) or 0xFE00 <= codepoint <= 0xFE0F or 0x1F3FB <= codepoint <= 0x1F3FF
        )
        if not current:
            current = character
        elif character == "\u200d":
            current += character
            join_next = True
        elif extends or join_next:
            current += character
            join_next = False
        else:
            clusters.append(current)
            current = character
    if current:
        clusters.append(current)
    return clusters


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


def _now_ms() -> int:
    return int(time.time() * 1_000)


__all__ = ["FeishuNewsPushDelivery"]
