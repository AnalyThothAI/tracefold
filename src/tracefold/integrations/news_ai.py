from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from math import isfinite

import httpx

from tracefold.news import NewsBriefStory, NewsBriefSynthesisResult
from tracefold.news.brief import (
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
from tracefold.news.identity import (
    JAVASCRIPT_WHITESPACE_PATTERN,
    javascript_trim,
    parse_javascript_number,
    utf16_length,
    web_usv_string,
)
from tracefold.news.models import (
    INSIGHTS_SYNTHESIS_GATE,
    INSIGHTS_SYNTHESIS_MISSING_CLUSTER,
    INSIGHTS_SYNTHESIS_PARSE,
    INSIGHTS_SYNTHESIS_PROVIDER,
)

_MAX_PROVIDER_RESPONSE_BYTES = 128 * 1024


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
    """Pinned WorldMonitor L1/L2 flow over Tracefold's configured provider lanes."""

    def __init__(
        self,
        *,
        ollama_base_url: str,
        configured_base_url: str | None = None,
        configured_api_key: str | None = None,
        configured_model: str | None = None,
        groq_api_key: str | None,
        total_timeout_seconds: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        normalized_ollama = str(ollama_base_url or "").strip().rstrip("/")
        normalized_configured_base_url = str(configured_base_url or "").strip().rstrip("/")
        normalized_configured_api_key = str(configured_api_key or "").strip()
        normalized_configured_model = str(configured_model or "").strip()
        configured_parts = (
            normalized_configured_base_url,
            normalized_configured_api_key,
            normalized_configured_model,
        )
        if any(configured_parts) and not all(configured_parts):
            raise ValueError("news_brief_direct_configuration_incomplete")
        if total_timeout_seconds <= 0:
            raise ValueError("news_brief_total_timeout_invalid")
        self._total_timeout_seconds = float(total_timeout_seconds)
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._sleep = sleep
        self._transport = transport
        self._runner = asyncio.Runner()
        self._closed = False
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
        if all(configured_parts):
            providers.append(
                _BriefProvider(
                    name="deepseek",
                    url=f"{normalized_configured_base_url}/chat/completions",
                    model=normalized_configured_model,
                    timeout_seconds=20.0,
                    api_key=normalized_configured_api_key,
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
        if self._closed:
            return
        self._closed = True
        self._runner.close()

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
            except (OverflowError, RecursionError, TypeError, ValueError):
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
        headers = _provider_headers(provider)
        response: httpx.Response | None = None
        for attempt in range(3):
            usable = deadline - self._monotonic() - 5.0
            if usable <= 0:
                raise _BudgetExhausted
            try:
                response = self._runner.run(
                    self._post_provider(
                        provider.url,
                        headers=headers,
                        body=body,
                        timeout_seconds=max(0.001, min(provider.timeout_seconds, usable)),
                    )
                )
            except httpx.HTTPError:
                if attempt >= 2:
                    return None
                wait = float(2**attempt)
                self._sleep(wait)
                continue
            if 200 <= response.status_code < 300:
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
            wait = max(float(2**attempt), bounded_hint)
            self._sleep(wait)
        if response is None or not 200 <= response.status_code < 300:
            return None
        if len(response.content) > _MAX_PROVIDER_RESPONSE_BYTES:
            return None
        try:
            payload = json.loads(response.content, parse_constant=_reject_json_constant)
            content = payload["choices"][0]["message"]["content"]
        except (UnicodeDecodeError, ValueError, RecursionError, IndexError, KeyError, TypeError):
            return None
        if not isinstance(content, str) or not javascript_trim(content) or "\x00" in content:
            return None
        text = _clean_provider_text(content)
        if utf16_length(text) < 20:
            return None
        returned_model = payload.get("model")
        normalized_model = javascript_trim(web_usv_string(returned_model)) if isinstance(returned_model, str) else ""
        model = normalized_model if normalized_model and "\x00" not in normalized_model else provider.model
        return _LlmCandidate(text=text, provider=provider.name, model=model)

    async def _post_provider(
        self,
        url: str,
        *,
        headers: dict[str, str],
        body: dict[str, object],
        timeout_seconds: float,
    ) -> httpx.Response:
        try:
            async with asyncio.timeout(timeout_seconds):
                async with httpx.AsyncClient(
                    follow_redirects=True,
                    transport=self._transport,
                ) as client:
                    return await client.post(
                        url,
                        headers=headers,
                        json=body,
                        timeout=httpx.Timeout(timeout_seconds),
                    )
        except TimeoutError as exc:
            raise httpx.ReadTimeout("news_brief_provider_timeout") from exc


_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
)
_TASK_NARRATION = re.compile(
    r"^(we need to|i need to|let me|i'll |i should|i will |the task is|the instructions|according to the rules|"
    rf"so we need to|okay[,.]{JAVASCRIPT_WHITESPACE_PATTERN}*(i'll|let me|so|we need|the task|i should|i will)|"
    rf"sure[,.]{JAVASCRIPT_WHITESPACE_PATTERN}*(i'll|let me|so|"
    r"we need|the task|i should|i will|here)|first[, ]+(i|we|let)|to summarize (the headlines|the task|this)|"
    r"my task (is|was|:)|step [0-9])",
    re.IGNORECASE | re.ASCII,
)
_PROMPT_ECHO = re.compile(
    r"^(summarize the top story|summarize the key|rules:|here are the rules|the top story is likely)",
    re.IGNORECASE | re.ASCII,
)


def _provider_headers(provider: _BriefProvider) -> dict[str, str]:
    headers = {"Content-Type": "application/json", "User-Agent": _CHROME_UA}
    if provider.api_key:
        headers["Authorization"] = f"Bearer {provider.api_key}"
    return headers


def _retry_after_seconds(value: str | None, *, now_seconds: float) -> float | None:
    if not value:
        return None
    seconds = parse_javascript_number(value)
    if isfinite(seconds) and seconds == 0:
        return 1.0
    if not isfinite(seconds) or seconds <= 0:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            return max(retry_at.timestamp() - now_seconds, 1.0)
        except (TypeError, ValueError, OverflowError):
            return None
    return seconds


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid_json_constant:{value}")


def _clean_provider_text(value: str) -> str:
    trimmed = javascript_trim(web_usv_string(value))
    if _TASK_NARRATION.match(trimmed) or _PROMPT_ECHO.match(trimmed):
        lines = [line for line in trimmed.split("\n") if javascript_trim(line)]
        clean = [
            line
            for line in lines
            if not _TASK_NARRATION.match(javascript_trim(line)) and not _PROMPT_ECHO.match(javascript_trim(line))
        ]
        trimmed = javascript_trim("\n".join(clean)) or trimmed
    return javascript_trim(
        re.sub(
            r"<think>[\s\S]*",
            "",
            re.sub(
                r"<\|thinking\|>[\s\S]*?<\|/thinking\|>",
                "",
                re.sub(r"<think>[\s\S]*?</think>", "", trimmed, flags=re.IGNORECASE | re.ASCII),
                flags=re.IGNORECASE | re.ASCII,
            ),
            flags=re.IGNORECASE | re.ASCII,
        )
    )


__all__ = [
    "ProviderChainNewsBriefPublisher",
]
