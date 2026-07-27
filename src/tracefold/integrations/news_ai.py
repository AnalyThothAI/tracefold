from __future__ import annotations

import json
import time
from collections.abc import Sequence
from dataclasses import dataclass

import httpx

from tracefold.news import NewsBriefDraft, NewsBriefStory


@dataclass(frozen=True, slots=True)
class OpenAiCompatibleProvider:
    name: str
    base_url: str
    model: str
    api_key: str | None


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
            except Exception as exc:
                failures.append(f"{provider.name}:{type(exc).__name__}")
        raise RuntimeError(
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
        content = payload["choices"][0]["message"]["content"]
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


__all__ = ["ProviderChainNewsBriefPublisher"]
