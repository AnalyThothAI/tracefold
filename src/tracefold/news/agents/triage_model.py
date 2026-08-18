"""Triage: one structured LangChain call with a byte-frozen system prompt and an end-of-message status bar."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from tracefold.news.models import TRIAGE_PROMPT_VERSION, TriageVerdict

from .prompts import TRIAGE_SYSTEM_PROMPT

_RETRYABLE_MARKERS = ("timeout", "ratelimit", "connect", "serviceunavailable", "internalserver", "remoteprotocol")
_MIN_RETRY_BUDGET_SECONDS = 1.5


class TriageModelError(RuntimeError):
    """One classified model failure: retryable (transport/limits) or not (schema, 4xx); the trace records the class."""

    def __init__(self, code: str, *, retryable: bool = False, attempts: int = 1) -> None:
        self.code = code
        self.retryable = retryable
        self.attempts = attempts
        super().__init__(code)


def is_retryable_model_failure(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    name = type(exc).__name__.lower()
    return any(marker in name for marker in _RETRYABLE_MARKERS)


@dataclass(frozen=True, slots=True)
class TriageCallResult:
    verdict: TriageVerdict
    latency_ms: int
    input_tokens: int | None
    output_tokens: int | None
    cached_tokens: int | None
    model: str
    attempts: int = 1


def build_triage_input(
    *,
    event: Mapping[str, Any],
    gate: Mapping[str, Any],
    event_status: Mapping[str, Any],
    watchlist: Sequence[str],
) -> str:
    """Fixed order: <event> (untrusted material) -> <gate> facts -> <event_status> last (status bar)."""

    event_block = {
        "source": event.get("reporting_origin") or "",
        "strategies": list(event.get("provenance") or []),
        "engine_type": event.get("engine_type"),
        "title": str(event.get("leader_title") or "")[:600],
        "content": str(event.get("leader_description") or "")[:600],
        "published_utc": time.strftime("%Y-%m-%d %H:%M", time.gmtime(int(event.get("opened_at_ms") or 0) / 1000)),
        "member_count": int(event.get("member_count") or 1),
    }
    gate_block = {
        "family": event.get("family"),
        "asset_class": gate.get("asset_class"),
        "grounded_assets": list(gate.get("grounded_assets") or []),
        "provider_score": event.get("provider_score_max"),
        "provider_coins": [c.get("symbol") for c in ((event.get("provider_metadata") or {}).get("coins") or [])][:8],
        "macro_lexicon": bool(gate.get("macro_lexicon")),
        "pr_template": bool(gate.get("pr_template")),
        "priority": event.get("priority"),
        "watchlist": list(watchlist),
    }
    status_block = {
        "storyline_key": event_status.get("storyline_key"),
        "same_key_2h": {
            "events": int(event_status.get("events_2h") or 0),
            "pushed": int(event_status.get("pushed_2h") or 0),
            "max_magnitude": int(event_status.get("max_magnitude_2h") or 0),
            "directions": list(event_status.get("directions_2h") or []),
            "last_push_ago_s": (
                int(event_status["last_push_ago_ms"]) // 1000
                if event_status.get("last_push_ago_ms") is not None
                else None
            ),
        },
        "queue_lag_s": int(event_status.get("queue_lag_ms") or 0) // 1000,
    }
    return (
        "<event source=opennews>\n"
        + json.dumps(event_block, ensure_ascii=False, sort_keys=True)
        + "\n</event>\n<gate>\n"
        + json.dumps(gate_block, ensure_ascii=False, sort_keys=True)
        + "\n</gate>\n<event_status>\n"
        + json.dumps(status_block, ensure_ascii=False, sort_keys=True)
        + "\n</event_status>"
    )


class TriageModel:
    def __init__(self, *, model: BaseChatModel, model_name: str, deadline_seconds: float) -> None:
        self._structured = model.with_structured_output(TriageVerdict, method="function_calling", include_raw=True)
        self.model_name = model_name
        self.deadline_seconds = float(deadline_seconds)

    async def triage(self, human_text: str) -> TriageCallResult:
        """One structured call within ``deadline_seconds``; a fast retryable failure earns one more attempt."""

        started = time.perf_counter()
        deadline = started + self.deadline_seconds
        messages = [SystemMessage(TRIAGE_SYSTEM_PROMPT), HumanMessage(human_text)]
        attempts = 0
        while True:
            attempts += 1
            remaining = deadline - time.perf_counter()
            try:
                out = await asyncio.wait_for(self._structured.ainvoke(messages), timeout=max(0.001, remaining))
                break
            except Exception as exc:  # provider/network failures are expected here
                retryable = is_retryable_model_failure(exc)
                if retryable and attempts < 2 and (deadline - time.perf_counter()) >= _MIN_RETRY_BUDGET_SECONDS:
                    continue
                code = (
                    "news_triage_timeout"
                    if isinstance(exc, TimeoutError)
                    else (f"news_triage_model_failed:{type(exc).__name__}")
                )
                raise TriageModelError(code, retryable=retryable, attempts=attempts) from exc
        latency_ms = int((time.perf_counter() - started) * 1000)
        parsed = out.get("parsed") if isinstance(out, Mapping) else None
        if not isinstance(parsed, TriageVerdict):
            raise TriageModelError("news_triage_output_invalid", attempts=attempts)
        raw = out.get("raw") if isinstance(out, Mapping) else None
        usage = getattr(raw, "usage_metadata", None) or {}
        details = usage.get("input_token_details") or {}
        return TriageCallResult(
            verdict=parsed,
            latency_ms=latency_ms,
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            cached_tokens=details.get("cache_read") if isinstance(details, Mapping) else None,
            model=self.model_name,
            attempts=attempts,
        )


__all__ = [
    "TRIAGE_PROMPT_VERSION",
    "TriageCallResult",
    "TriageModel",
    "TriageModelError",
    "build_triage_input",
    "is_retryable_model_failure",
]
