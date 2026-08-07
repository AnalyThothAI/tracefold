from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from tracefold.news import (
    INSIGHTS_SYNTHESIS_GATE,
    INSIGHTS_SYNTHESIS_MISSING_CLUSTER,
    INSIGHTS_SYNTHESIS_PARSE,
    INSIGHTS_SYNTHESIS_PROVIDER,
    NewsBriefStory,
    NewsBriefSynthesisResult,
    brief_system_prompt,
    brief_user_prompt,
    compose_l1_brief,
    compose_l2_brief,
    compose_none_brief,
    is_brief_lead_eligible,
    parse_brief_synthesis,
    synthesis_system_prompt,
    synthesis_user_prompt,
)


@dataclass(frozen=True, slots=True)
class _BriefProvider:
    name: str
    url: str
    model: str
    timeout_seconds: float
    api_key: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class _LlmCandidate:
    text: str
    provider: str
    model: str


@dataclass(frozen=True, slots=True)
class _ChainOutcome:
    candidate: _LlmCandidate | None
    accepted: NewsBriefSynthesisResult | None
    first_rejection_code: str | None


class _BudgetExhausted(RuntimeError):
    pass


class ProviderChainNewsBriefPublisher:
    """Pinned public WorldMonitor L1/L2 provider waterfall."""

    def __init__(
        self,
        *,
        ollama_base_url: str,
        openrouter_api_key: str | None,
        groq_api_key: str | None,
        total_timeout_seconds: float = 60.0,
        transport: httpx.BaseTransport | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        normalized_ollama = str(ollama_base_url or "").strip().rstrip("/")
        if total_timeout_seconds <= 0:
            raise ValueError("news_brief_total_timeout_invalid")
        self._total_timeout_seconds = float(total_timeout_seconds)
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._sleep = sleep
        self._client = httpx.Client(follow_redirects=False, transport=transport)
        providers: list[_BriefProvider] = []
        if normalized_ollama:
            providers.append(
                _BriefProvider(
                    name="ollama",
                    url=f"{normalized_ollama}/chat/completions",
                    model="llama3.1:8b",
                    timeout_seconds=25.0,
                )
            )
        if str(openrouter_api_key or "").strip():
            providers.append(
                _BriefProvider(
                    name="openrouter",
                    url="https://openrouter.ai/api/v1/chat/completions",
                    model="deepseek/deepseek-v4-flash",
                    timeout_seconds=20.0,
                    api_key=str(openrouter_api_key).strip(),
                )
            )
        if str(groq_api_key or "").strip():
            providers.append(
                _BriefProvider(
                    name="groq",
                    url="https://api.groq.com/openai/v1/chat/completions",
                    model="llama-3.3-70b-versatile",
                    timeout_seconds=15.0,
                    api_key=str(groq_api_key).strip(),
                )
            )
        self._providers = tuple(providers)

    def publish(
        self,
        stories: Sequence[NewsBriefStory],
        *,
        date_iso: str | None = None,
    ) -> NewsBriefSynthesisResult:
        top_stories = tuple(stories)
        if not top_stories:
            raise ValueError("news_brief_stories_required")
        brief_story = next((story for story in top_stories if is_brief_lead_eligible(story)), None)
        if brief_story is None:
            return compose_none_brief(None, failure_code=INSIGHTS_SYNTHESIS_MISSING_CLUSTER)

        prompt_date = date_iso or datetime.now(UTC).date().isoformat()
        deadline = self._monotonic() + self._total_timeout_seconds

        def accept_l1(candidate: _LlmCandidate) -> tuple[NewsBriefSynthesisResult | None, str | None]:
            if parse_brief_synthesis(candidate.text, len(top_stories)) is None:
                return None, INSIGHTS_SYNTHESIS_PARSE
            try:
                composed = compose_l1_brief(
                    candidate.text,
                    top_stories,
                    provider=candidate.provider,
                    model=candidate.model,
                )
            except (TypeError, ValueError):
                return None, INSIGHTS_SYNTHESIS_GATE
            return composed, None if composed is not None else INSIGHTS_SYNTHESIS_GATE

        l1 = self._call_chain(
            system_prompt=synthesis_system_prompt(prompt_date),
            user_prompt=synthesis_user_prompt(top_stories),
            max_tokens=900,
            deadline=deadline,
            accept=accept_l1,
        )
        if l1.accepted is not None:
            return l1.accepted
        failure_code = l1.first_rejection_code or INSIGHTS_SYNTHESIS_PROVIDER

        l2 = self._call_chain(
            system_prompt=brief_system_prompt(prompt_date),
            user_prompt=brief_user_prompt(brief_story.primary_title),
            max_tokens=300,
            deadline=deadline,
            accept=None,
        )
        if l2.candidate is None:
            return compose_none_brief(brief_story, failure_code=failure_code)
        return compose_l2_brief(
            l2.candidate.text,
            brief_story,
            provider=l2.candidate.provider,
            model=l2.candidate.model,
            failure_code=failure_code,
        )

    def close(self) -> None:
        self._client.close()

    def _call_chain(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        deadline: float,
        accept: Callable[[_LlmCandidate], tuple[NewsBriefSynthesisResult | None, str | None]] | None,
    ) -> _ChainOutcome:
        first_rejection_code: str | None = None
        for provider in self._providers:
            try:
                candidate = self._call_provider(
                    provider,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    max_tokens=max_tokens,
                    deadline=deadline,
                )
            except _BudgetExhausted:
                break
            if candidate is None:
                continue
            if accept is None:
                return _ChainOutcome(candidate=candidate, accepted=None, first_rejection_code=None)
            try:
                accepted, rejection_code = accept(candidate)
            except (TypeError, ValueError):
                accepted, rejection_code = None, INSIGHTS_SYNTHESIS_GATE
            if accepted is not None:
                return _ChainOutcome(candidate=candidate, accepted=accepted, first_rejection_code=None)
            if first_rejection_code is None:
                first_rejection_code = rejection_code or INSIGHTS_SYNTHESIS_GATE
        return _ChainOutcome(candidate=None, accepted=None, first_rejection_code=first_rejection_code)

    def _call_provider(
        self,
        provider: _BriefProvider,
        *,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        deadline: float,
    ) -> _LlmCandidate | None:
        body: dict[str, object] = {
            "model": provider.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": max_tokens,
            "temperature": 0.1,
        }
        if provider.name == "ollama":
            body["think"] = False
        elif provider.name == "openrouter":
            body["reasoning"] = {"enabled": False}
        headers = _provider_headers(provider)
        response: httpx.Response | None = None
        for attempt in range(3):
            usable = deadline - self._monotonic() - 5.0
            if usable <= 0:
                raise _BudgetExhausted
            try:
                response = self._client.post(
                    provider.url,
                    headers=headers,
                    json=body,
                    timeout=max(0.001, min(provider.timeout_seconds, usable)),
                )
            except httpx.HTTPError:
                if attempt >= 2:
                    return None
                self._sleep(2**attempt)
                continue
            if response.status_code < 400:
                break
            if response.status_code not in {408, 429} and not 500 <= response.status_code <= 599:
                return None
            if attempt >= 2:
                return None
            retry_after = _retry_after_seconds(response.headers.get("Retry-After"), now_seconds=self._wall_clock())
            remaining = max(0.0, deadline - self._monotonic() - 5.0)
            if retry_after is not None and retry_after >= remaining:
                return None
            bounded_hint = min(retry_after, 10.0) if retry_after is not None else 0.0
            self._sleep(max(float(2**attempt), bounded_hint))
        if response is None or response.status_code >= 400:
            return None
        try:
            payload = response.json()
            content = payload["choices"][0]["message"]["content"]
        except (json.JSONDecodeError, IndexError, KeyError, TypeError):
            return None
        if not isinstance(content, str) or not content.strip():
            return None
        text = _clean_provider_text(content)
        if len(text) < 20:
            return None
        returned_model = payload.get("model")
        model = returned_model.strip() if isinstance(returned_model, str) and returned_model.strip() else provider.model
        return _LlmCandidate(text=text, provider=provider.name, model=model)


_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
)
_TASK_NARRATION = re.compile(
    r"^(we need to|i need to|let me|i'll |i should|i will |the task is|the instructions|according to the rules|"
    r"so we need to|okay[,.]\s*(i'll|let me|so|we need|the task|i should|i will)|sure[,.]\s*(i'll|let me|so|"
    r"we need|the task|i should|i will|here)|first[, ]+(i|we|let)|to summarize (the headlines|the task|this)|"
    r"my task (is|was|:)|step \d)",
    re.IGNORECASE,
)
_PROMPT_ECHO = re.compile(
    r"^(summarize the top story|summarize the key|rules:|here are the rules|the top story is likely)",
    re.IGNORECASE,
)


def _provider_headers(provider: _BriefProvider) -> dict[str, str]:
    headers = {"Content-Type": "application/json", "User-Agent": _CHROME_UA}
    if provider.api_key:
        headers["Authorization"] = f"Bearer {provider.api_key}"
    if provider.name == "openrouter":
        headers["HTTP-Referer"] = "https://worldmonitor.app"
        headers["X-Title"] = "World Monitor"
    return headers


def _retry_after_seconds(value: str | None, *, now_seconds: float) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            return max(retry_at.timestamp() - now_seconds, 1.0)
        except (TypeError, ValueError, OverflowError):
            return None
    return seconds if seconds > 0 else None


def _clean_provider_text(value: str) -> str:
    trimmed = value.strip()
    if _TASK_NARRATION.match(trimmed) or _PROMPT_ECHO.match(trimmed):
        lines = [line for line in trimmed.splitlines() if line.strip()]
        clean = [
            line for line in lines if not _TASK_NARRATION.match(line.strip()) and not _PROMPT_ECHO.match(line.strip())
        ]
        trimmed = "\n".join(clean).strip() or trimmed
    return re.sub(
        r"<think>[\s\S]*",
        "",
        re.sub(
            r"<\|thinking\|>[\s\S]*?<\|/thinking\|>",
            "",
            re.sub(r"<think>[\s\S]*?</think>", "", trimmed, flags=re.IGNORECASE),
            flags=re.IGNORECASE,
        ),
        flags=re.IGNORECASE,
    ).strip()


__all__ = [
    "ProviderChainNewsBriefPublisher",
]
