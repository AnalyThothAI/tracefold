from __future__ import annotations

import json
import re
import time
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import httpx

from tracefold.news import (
    NewsBriefDraft,
    NewsBriefExpectedError,
    NewsBriefStory,
    NewsTitleTranslationExpectedError,
    NewsTitleTranslationResult,
    looks_zh_cn_title,
    normalize_news_display_text,
)

_TITLE_TRANSLATION_REQUEST_TIMEOUT_SECONDS = 7.5
_TITLE_TRANSLATION_INPUT_GRAPHEME_CAP = 500
_TITLE_TRANSLATION_OUTPUT_GRAPHEME_CAP = 600
_NUMBER_ANCHOR = re.compile(r"(?<![\w])\d+(?:[.,]\d+)*(?:[%％])?(?![\w])")
_MONEY_AMOUNT_ANCHOR = re.compile(r"[$€£¥]\s?\d+(?:[.,]\d+)*")
_DOLLAR_TICKER_ANCHOR = re.compile(r"\$[A-Za-z][A-Za-z0-9]{0,15}")
_UPPERCASE_TOKEN_ANCHOR = re.compile(r"(?<![A-Za-z0-9])[A-Z][A-Z0-9]{1,15}(?![A-Za-z0-9])")
_EXCHANGE_TICKER_ANCHOR = re.compile(
    r"\b(?:NASDAQ|NYSE|AMEX|OTC(?:QX|QB)?|LSE|TSX|ASX|HKEX)\s*:\s*([A-Z][A-Z0-9.-]{1,15})\b"
)
_PARENTHESIZED_TICKER_ANCHOR = re.compile(r"\(([A-Z][A-Z0-9.-]{1,9})\)")
_TRAILING_TICKER_ANCHOR = re.compile(r"[-\u2013\u2014]\s*([A-Z][A-Z0-9.-]{1,9})\s*$")
_PRESERVED_UPPERCASE_SYMBOLS = frozenset(
    {
        "AAVE",
        "ADA",
        "APT",
        "ARB",
        "ATOM",
        "AUD",
        "AVAX",
        "BCH",
        "BNB",
        "BONK",
        "BTC",
        "CAD",
        "CHF",
        "CNY",
        "DAI",
        "DOGE",
        "DOT",
        "ETH",
        "EUR",
        "FDUSD",
        "GBP",
        "HKD",
        "JPY",
        "LINK",
        "LTC",
        "MKR",
        "OP",
        "PEPE",
        "PYUSD",
        "RMB",
        "SHIB",
        "SOL",
        "SUI",
        "TON",
        "TRX",
        "UNI",
        "USD",
        "USDC",
        "USDT",
        "WIF",
        "WLD",
        "XRP",
    }
)


@dataclass(frozen=True, slots=True)
class OpenAiCompatibleProvider:
    name: str
    base_url: str
    model: str
    api_key: str | None


class OpenAiCompatibleNewsTitleTranslator:
    """Translate one exact News display title through the global LLM endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        normalized_base_url = str(base_url or "").strip().rstrip("/")
        normalized_api_key = str(api_key or "").strip()
        normalized_model = str(model or "").strip()
        if not normalized_base_url or not normalized_api_key or not normalized_model:
            raise ValueError("news_title_translation_configuration_required")
        self._model = normalized_model
        self._client = httpx.Client(
            timeout=httpx.Timeout(_TITLE_TRANSLATION_REQUEST_TIMEOUT_SECONDS),
            headers={
                "Authorization": f"Bearer {normalized_api_key}",
                "Content-Type": "application/json",
            },
            follow_redirects=False,
            transport=transport,
        )
        self._url = f"{normalized_base_url}/chat/completions"

    def translate(self, source_title: str) -> NewsTitleTranslationResult:
        exact_source_title = str(source_title)
        if (
            not exact_source_title
            or normalize_news_display_text(exact_source_title) != exact_source_title
            or _grapheme_count(exact_source_title) > _TITLE_TRANSLATION_INPUT_GRAPHEME_CAP
        ):
            raise NewsTitleTranslationExpectedError(
                "news_title_translation_source_invalid",
                retryable=False,
            )
        required_verbatim = _required_title_anchors(exact_source_title)
        try:
            response = self._client.post(
                self._url,
                json={
                    "model": self._model,
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
                                    "source_title": exact_source_title,
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
            raise NewsTitleTranslationExpectedError(
                "news_title_translation_timeout",
                retryable=True,
            ) from None
        except httpx.HTTPError:
            raise NewsTitleTranslationExpectedError(
                "news_title_translation_transport_error",
                retryable=True,
            ) from None
        if response.status_code == 429 or response.status_code >= 500:
            raise NewsTitleTranslationExpectedError(
                "news_title_translation_provider_unavailable",
                retryable=True,
            )
        if response.status_code >= 400:
            raise NewsTitleTranslationExpectedError(
                "news_title_translation_provider_rejected",
                retryable=False,
            )
        try:
            payload = response.json()
            choice = payload["choices"][0]
            if not isinstance(choice, Mapping) or choice.get("finish_reason") != "stop":
                raise ValueError("news_title_translation_incomplete")
            message = choice["message"]
            if not isinstance(message, Mapping):
                raise TypeError("news_title_translation_message_invalid")
            content = message["content"]
            if not isinstance(content, str):
                raise TypeError("news_title_translation_content_invalid")
            translated_payload = json.loads(content)
            if not isinstance(translated_payload, Mapping):
                raise TypeError("news_title_translation_shape_invalid")
            translated = translated_payload["translated_title"]
            if not isinstance(translated, str):
                raise TypeError("news_title_translation_title_invalid")
            title_zh = _validate_title_translation(
                translated,
                required_verbatim=required_verbatim,
            )
        except NewsTitleTranslationExpectedError:
            raise
        except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError):
            raise NewsTitleTranslationExpectedError(
                "news_title_translation_response_invalid",
                retryable=False,
            ) from None
        return NewsTitleTranslationResult(
            title_zh=title_zh,
            provider="openai_compatible",
            model=self._model,
        )

    def close(self) -> None:
        self._client.close()


class ProviderChainNewsBriefPublisher:
    """One bounded pass through the configured World Brief provider chain."""

    def __init__(
        self,
        *,
        configured_base_url: str,
        configured_api_key: str | None,
        configured_model: str,
        ollama_base_url: str,
        ollama_model: str,
        openrouter_base_url: str,
        openrouter_model: str,
        openrouter_api_key: str | None,
        groq_base_url: str,
        groq_model: str,
        groq_api_key: str | None,
        total_timeout_seconds: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._total_timeout_seconds = total_timeout_seconds
        self._client = httpx.Client(
            timeout=min(30.0, total_timeout_seconds),
            transport=transport,
        )
        self._providers = _configured_providers(
            (
                OpenAiCompatibleProvider(
                    "ollama",
                    ollama_base_url,
                    ollama_model,
                    None,
                ),
                OpenAiCompatibleProvider(
                    "deepseek",
                    configured_base_url,
                    configured_model,
                    configured_api_key,
                ),
                OpenAiCompatibleProvider(
                    "openrouter",
                    openrouter_base_url,
                    openrouter_model,
                    openrouter_api_key,
                ),
                OpenAiCompatibleProvider(
                    "groq",
                    groq_base_url,
                    groq_model,
                    groq_api_key,
                ),
            )
        )

    def publish(
        self,
        stories: list[NewsBriefStory] | tuple[NewsBriefStory, ...],
    ) -> NewsBriefDraft:
        if not stories:
            raise ValueError("news_brief_stories_required")
        deadline = time.monotonic() + self._total_timeout_seconds
        failures: list[str] = []
        for provider in self._providers:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                raw = self._call(
                    provider,
                    stories=stories,
                    timeout_seconds=min(remaining, 30.0),
                )
                lead, lines = _parse(raw, expected_count=len(stories))
                return NewsBriefDraft(
                    lead=lead,
                    lines=tuple(lines),
                    provider=provider.name,
                    model=provider.model,
                    raw_response=raw,
                )
            except (httpx.HTTPError, ValueError) as exc:
                failures.append(f"{provider.name}:{type(exc).__name__}")
        raise NewsBriefExpectedError(
            "news_brief_provider_chain_exhausted:" + (",".join(failures) if failures else "no_configured_provider")
        )

    def close(self) -> None:
        self._client.close()

    def _call(
        self,
        provider: OpenAiCompatibleProvider,
        *,
        stories: list[NewsBriefStory] | tuple[NewsBriefStory, ...],
        timeout_seconds: float,
    ) -> str:
        headers = {"Content-Type": "application/json"}
        if provider.api_key:
            headers["Authorization"] = f"Bearer {provider.api_key}"
        response = self._client.post(
            f"{provider.base_url.rstrip('/')}/chat/completions",
            headers=headers,
            json={
                "model": provider.model,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": _system_prompt(len(stories))},
                    {"role": "user", "content": _user_prompt(stories)},
                ],
            },
            timeout=timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        try:
            content = payload["choices"][0]["message"]["content"]
        except (IndexError, KeyError, TypeError):
            raise ValueError("news_brief_model_response_invalid") from None
        if not isinstance(content, str) or not content.strip():
            raise ValueError("news_brief_empty_model_response")
        return content


def _configured_providers(
    providers: Sequence[OpenAiCompatibleProvider],
) -> tuple[OpenAiCompatibleProvider, ...]:
    return tuple(
        provider
        for provider in providers
        if provider.base_url and provider.model and (provider.api_key or provider.name == "ollama")
    )


def _system_prompt(story_count: int) -> str:
    return f"""你是 WORLD BRIEF 编辑。只输出 JSON：
{{"lead":"...","lines":[{{"n":1,"text":"..."}}, ...]}}

要求：
- 使用简体中文；lead 为 2-3 句，概括最重要的 2-3 条线索。
- lines 必须正好 {story_count} 条，严格保持输入顺序，一条新闻一句话。
- 每个事实都只能来自对应编号的标题；不得补充背景、数字、人物、地点或因果。
- lead 的每个主张必须带来源编号 [n]；第 n 条 line 必须只以 [n] 结尾。
- 不得合并不同 Story 的事实；不得重排、增加或删除 Story。
- 专有名词必须能在输入标题中逐字找到。"""


def _user_prompt(
    stories: list[NewsBriefStory] | tuple[NewsBriefStory, ...],
) -> str:
    rows = [
        f"{index}. {story.title}（{story.source}，{story.source_count} 个独立物理来源）"
        for index, story in enumerate(stories, start=1)
    ]
    return "编号 Story：\n" + "\n".join(rows)


def _parse(raw: str, *, expected_count: int) -> tuple[str, list[str]]:
    cleaned = raw.replace("```json", "").replace("```", "").strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end < start:
        raise ValueError("news_brief_json_missing")
    payload = json.loads(cleaned[start : end + 1])
    lead = str(payload.get("lead") or "").strip()
    raw_lines = payload.get("lines")
    if not lead or not isinstance(raw_lines, list):
        raise ValueError("news_brief_shape_invalid")
    by_index: dict[int, str] = {}
    for entry in raw_lines:
        if not isinstance(entry, dict):
            continue
        try:
            index = int(entry.get("n"))
        except (TypeError, ValueError):
            continue
        text = str(entry.get("text") or "").strip()
        if 1 <= index <= expected_count and text and index not in by_index:
            by_index[index] = text
    return lead, [by_index.get(index, "") for index in range(1, expected_count + 1)]


def _required_title_anchors(source_title: str) -> tuple[str, ...]:
    candidates = [
        (match.start(), match.group(0))
        for pattern in (_NUMBER_ANCHOR, _MONEY_AMOUNT_ANCHOR, _DOLLAR_TICKER_ANCHOR)
        for match in pattern.finditer(source_title)
    ]
    candidates.extend(
        (match.start(), match.group(0))
        for match in _UPPERCASE_TOKEN_ANCHOR.finditer(source_title)
        if match.group(0) in _PRESERVED_UPPERCASE_SYMBOLS
    )
    for pattern in (
        _EXCHANGE_TICKER_ANCHOR,
        _PARENTHESIZED_TICKER_ANCHOR,
        _TRAILING_TICKER_ANCHOR,
    ):
        candidates.extend((match.start(1), match.group(1)) for match in pattern.finditer(source_title))
    anchors: list[str] = []
    seen: set[str] = set()
    for _, anchor in sorted(candidates):
        identity = anchor.casefold()
        if identity in seen:
            continue
        seen.add(identity)
        anchors.append(anchor)
    return tuple(anchors)


def _validate_title_translation(
    value: object,
    *,
    required_verbatim: tuple[str, ...],
) -> str:
    translated = str(value or "").strip()
    if (
        not translated
        or normalize_news_display_text(translated) != translated
        or not looks_zh_cn_title(translated)
        or _grapheme_count(translated) > _TITLE_TRANSLATION_OUTPUT_GRAPHEME_CAP
    ):
        raise NewsTitleTranslationExpectedError(
            "news_title_translation_output_invalid",
            retryable=False,
        )
    translated_casefold = translated.casefold()
    if any(anchor.casefold() not in translated_casefold for anchor in required_verbatim):
        raise NewsTitleTranslationExpectedError(
            "news_title_translation_anchors_changed",
            retryable=False,
        )
    return translated


def _grapheme_count(value: str) -> int:
    count = 0
    current = False
    join_next = False
    for character in value:
        codepoint = ord(character)
        extends = (
            bool(unicodedata.combining(character)) or 0xFE00 <= codepoint <= 0xFE0F or 0x1F3FB <= codepoint <= 0x1F3FF
        )
        if not current:
            current = True
            count += 1
        elif character == "\u200d":
            join_next = True
        elif extends or join_next:
            join_next = False
        else:
            count += 1
    return count


__all__ = [
    "OpenAiCompatibleNewsTitleTranslator",
    "ProviderChainNewsBriefPublisher",
]
