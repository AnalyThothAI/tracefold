from __future__ import annotations

from collections.abc import Mapping, Sequence

import httpx

from tracefold.news.title_presentation import TitleTranslationError

_DEEPL_FREE_BASE_URL = "https://api-free.deepl.com/v2/"
_DEEPL_PRO_BASE_URL = "https://api.deepl.com/v2/"


class DeepLTitleTranslationProvider:
    """One DeepL request per Item with process-local future-key rotation."""

    def __init__(
        self,
        *,
        api_keys: Sequence[str],
        free_transport: httpx.AsyncBaseTransport | None = None,
        pro_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_keys = tuple(str(value).strip() for value in api_keys if str(value).strip())
        if not self._api_keys:
            raise ValueError("news_title_presentation_deepl_keys_required")
        self._active_index = 0
        self._free_client = httpx.AsyncClient(
            base_url=_DEEPL_FREE_BASE_URL,
            timeout=None,  # noqa: S113 -- owning module enforces the absolute deadline
            follow_redirects=False,
            transport=free_transport,
        )
        self._pro_client = httpx.AsyncClient(
            base_url=_DEEPL_PRO_BASE_URL,
            timeout=None,  # noqa: S113 -- owning module enforces the absolute deadline
            follow_redirects=False,
            transport=pro_transport,
        )

    async def translate(self, title: str) -> str:
        if self._active_index >= len(self._api_keys):
            raise TitleTranslationError("news_title_presentation_deepl_keys_exhausted")
        key = self._api_keys[self._active_index]
        client = self._free_client if key.endswith(":fx") else self._pro_client
        try:
            response = await client.post(
                "translate",
                headers={
                    "Authorization": f"DeepL-Auth-Key {key}",
                    "Content-Type": "application/json",
                },
                json={"text": [str(title)], "target_lang": "ZH-HANS"},
            )
        except httpx.TimeoutException:
            raise TitleTranslationError("news_title_presentation_deepl_timeout") from None
        except httpx.HTTPError:
            raise TitleTranslationError("news_title_presentation_deepl_http_error") from None

        if response.status_code in {401, 403}:
            self._active_index += 1
            raise TitleTranslationError("news_title_presentation_deepl_key_rejected")
        if response.status_code == 456:
            self._active_index += 1
            raise TitleTranslationError("news_title_presentation_deepl_quota_exhausted")
        if response.status_code == 429:
            raise TitleTranslationError("news_title_presentation_deepl_rate_limited")
        try:
            response.raise_for_status()
            payload = response.json()
            translations = payload["translations"]
            if not isinstance(translations, list) or len(translations) != 1:
                raise TypeError("deepl_translations_invalid")
            translation = translations[0]
            if not isinstance(translation, Mapping):
                raise TypeError("deepl_translation_invalid")
            text = translation["text"]
            if not isinstance(text, str) or not text.strip():
                raise TypeError("deepl_text_invalid")
            return text
        except httpx.HTTPStatusError:
            raise TitleTranslationError("news_title_presentation_deepl_http_error") from None
        except (KeyError, TypeError, ValueError):
            raise TitleTranslationError("news_title_presentation_deepl_response_invalid") from None

    async def close(self) -> None:
        await self._free_client.aclose()
        await self._pro_client.aclose()


class OpenAICompatibleDeepSeekTitleProvider:
    """One plain-text DeepSeek-compatible request with no adapter retry."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._model = str(model).strip()
        self._client = httpx.AsyncClient(
            base_url=str(base_url).strip().rstrip("/") + "/",
            timeout=None,  # noqa: S113 -- owning module enforces the absolute deadline
            headers={
                "Authorization": f"Bearer {str(api_key).strip()}",
                "Content-Type": "application/json",
            },
            follow_redirects=False,
            transport=transport,
        )

    async def translate(self, title: str) -> str:
        try:
            response = await self._client.post(
                "chat/completions",
                json={
                    "model": self._model,
                    "temperature": 0,
                    "max_tokens": 256,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "将金融新闻标题忠实翻译为简体中文。不得总结、解释、补充事实；"
                                "只输出翻译后的单行标题，不要 JSON、引号、Markdown 或解释。"
                            ),
                        },
                        {"role": "user", "content": str(title)},
                    ],
                },
            )
        except httpx.TimeoutException:
            raise TitleTranslationError("news_title_presentation_deepseek_timeout") from None
        except httpx.HTTPError:
            raise TitleTranslationError("news_title_presentation_deepseek_http_error") from None
        if response.status_code == 429:
            raise TitleTranslationError("news_title_presentation_deepseek_rate_limited")
        try:
            response.raise_for_status()
            payload = response.json()
            choice = payload["choices"][0]
            if not isinstance(choice, Mapping) or choice.get("finish_reason") != "stop":
                raise ValueError("deepseek_choice_invalid")
            message = choice["message"]
            if not isinstance(message, Mapping):
                raise TypeError("deepseek_message_invalid")
            content = message["content"]
            if not isinstance(content, str) or not content.strip():
                raise TypeError("deepseek_content_invalid")
            return content
        except httpx.HTTPStatusError:
            raise TitleTranslationError("news_title_presentation_deepseek_http_error") from None
        except (KeyError, IndexError, TypeError, ValueError):
            raise TitleTranslationError("news_title_presentation_deepseek_response_invalid") from None

    async def close(self) -> None:
        await self._client.aclose()


__all__ = [
    "DeepLTitleTranslationProvider",
    "OpenAICompatibleDeepSeekTitleProvider",
]
