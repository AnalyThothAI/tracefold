"""Triage: one structured LangChain call with a byte-frozen system prompt and an end-of-message status bar.

With a configured fallback endpoint (issue #65) the call is a two-link chain: the primary model gets
``deadline_seconds``; when it raises a Triage model error (timeout, transport, truncated or invalid output) the
fallback model gets one call under its own deadline, and the verdict records which model answered. A small primary
breaker skips a primary that keeps failing so Events stop paying the primary deadline before the fallback.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from tracefold.news.models import TRIAGE_PROMPT_VERSION, TriageVerdict

from .prompts import TRIAGE_SYSTEM_PROMPT

_RETRYABLE_MARKERS = ("timeout", "ratelimit", "connect", "serviceunavailable", "internalserver", "remoteprotocol")
_MIN_RETRY_BUDGET_SECONDS = 1.5
_DETAIL_CHARS = 400
# The told ledger: cards the reader received in the last 4 h, at most 12 entries in the status bar (same preliminary
# storyline first, then the newest global pushes). Code-owned, not policy: it shapes the model input.
TOLD_WINDOW_MS = 4 * 3600_000
TOLD_MAX = 12
TOLD_SAME_KEY_MAX = 6


class TriageModelError(RuntimeError):
    """One classified model failure: retryable (transport/limits) or not (schema, 4xx); the trace records the class.

    ``output_failure`` marks a call that *reached* the model but returned no usable verdict (truncated by
    ``max_tokens``, or a schema mismatch); it carries ``finish_reason``/``output_tokens``/``detail`` for the trace and
    never counts toward the transport circuit breaker.
    """

    def __init__(
        self,
        code: str,
        *,
        retryable: bool = False,
        attempts: int = 1,
        output_failure: bool = False,
        finish_reason: str | None = None,
        output_tokens: int | None = None,
        detail: str | None = None,
        primary_code: str | None = None,
    ) -> None:
        self.code = code
        self.retryable = retryable
        self.attempts = attempts
        self.output_failure = output_failure
        self.finish_reason = finish_reason
        self.output_tokens = output_tokens
        self.detail = detail
        self.primary_code = primary_code  # set when the fallback link failed too: the primary's error code
        super().__init__(code)


def _finish_reason(raw: Any) -> str | None:
    meta = getattr(raw, "response_metadata", None) or {}
    value = meta.get("finish_reason") if isinstance(meta, Mapping) else None
    return str(value) if value else None


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
    novelty_defaulted: bool = False
    # The primary's error code (or ``primary_circuit_open``) when the fallback model answered.
    fallback_from: str | None = None


def told_ledger_for_prompt(
    rows: Sequence[Mapping[str, Any]], *, now_ms: int, prefer_key: str, limit: int = TOLD_MAX
) -> list[dict[str, Any]]:
    """Order and trim ledger rows (``repository.told_ledger``) for the status bar: entries on the preliminary
    storyline first (up to ``TOLD_SAME_KEY_MAX``), then the newest of the rest, all newest-first; ``i`` is the index
    the model cites in ``restates`` and ``event_id``/``at_ms`` stay for the trace (they are not sent)."""

    ordered = sorted(rows, key=lambda r: -int(r.get("at_ms") or 0))
    same = [r for r in ordered if str(r.get("storyline_key") or "") == prefer_key]
    others = [r for r in ordered if str(r.get("storyline_key") or "") != prefer_key]
    # Reserve up to TOLD_SAME_KEY_MAX slots for the preliminary storyline, fill the rest with the newest cards on
    # other storylines (cross-key restatements are the point), then top up with more same-key cards if slots remain.
    chosen = same[:TOLD_SAME_KEY_MAX]
    chosen += others[: max(0, limit - len(chosen))]
    chosen += same[TOLD_SAME_KEY_MAX:][: max(0, limit - len(chosen))]
    chosen.sort(key=lambda r: -int(r.get("at_ms") or 0))
    return [
        {
            "i": i,
            "event_id": str(r.get("event_id") or ""),
            "at_ms": int(r.get("at_ms") or 0),
            "ago_min": max(0, int(now_ms) - int(r.get("at_ms") or 0)) // 60_000,
            "m": int(r.get("magnitude") or 0),
            "dir": str(r.get("direction") or ""),
            "headline_zh": str(r.get("headline_zh") or "")[:60],
        }
        for i, r in enumerate(chosen[:limit])
    ]


def build_triage_input(
    *,
    event: Mapping[str, Any],
    gate: Mapping[str, Any],
    event_status: Mapping[str, Any],
    watchlist: Sequence[str],
    told: Sequence[Mapping[str, Any]] = (),
) -> str:
    """Fixed order: <event> (untrusted material) -> <gate> facts -> <event_status> last (status bar, incl. told)."""

    event_block = {
        "focus_fact_id": str(event.get("focus_fact_id") or ""),
        "source": event.get("reporting_origin") or "",
        "strategies": list(event.get("provenance") or []),
        "engine_type": event.get("engine_type"),
        "title": str(event.get("leader_title") or "")[:600],
        "raw_first_line": str(event.get("raw_first_line") or "")[:300],
        "content": str(event.get("leader_description") or "")[:600],
        "published_utc": time.strftime("%Y-%m-%d %H:%M", time.gmtime(int(event.get("opened_at_ms") or 0) / 1000)),
        "member_count": int(event.get("member_count") or 1),
    }
    gate_block = {
        "family": event.get("family"),
        "asset_class": gate.get("asset_class"),
        "grounded_assets": list(gate.get("grounded_assets") or []),
        "provider_score": event.get("provider_score_max"),
        "provider_coins": [
            f"{c.get('symbol')}:{c.get('grade') or '-'}"
            for c in ((event.get("provider_metadata") or {}).get("coins") or [])
            if c.get("symbol")
        ][:10],
        "macro_lexicon": bool(gate.get("macro_lexicon")),
        "pr_template": bool(gate.get("pr_template")),
        "priority": event.get("priority"),
        "watchlist": list(watchlist),
    }
    status_block = {
        "storyline_key": event_status.get("storyline_key"),
        "preliminary": bool(event_status.get("preliminary", True)),
        "queue_lag_s": int(event_status.get("queue_lag_ms") or 0) // 1000,
        "told": [
            {
                "i": int(t.get("i", i)),
                "ago_min": int(t.get("ago_min") or 0),
                "m": int(t.get("m") or 0),
                "dir": str(t.get("dir") or ""),
                "headline_zh": str(t.get("headline_zh") or ""),
            }
            for i, t in enumerate(told)
        ],
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


class _PrimaryBreaker:
    """Consecutive primary failures open the breaker for ``open_seconds``; the fallback answers alone meanwhile."""

    def __init__(self, *, threshold: int, open_seconds: float) -> None:
        self.threshold = max(1, int(threshold))
        self.open_seconds = float(open_seconds)
        self.failures = 0
        self.open_until = 0.0

    def is_open(self, now: float) -> bool:
        return now < self.open_until

    def record_failure(self, now: float) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            self.open_until = now + self.open_seconds
            self.failures = 0

    def record_success(self) -> None:
        self.failures = 0
        self.open_until = 0.0


class TriageModel:
    def __init__(
        self,
        *,
        model: BaseChatModel,
        model_name: str,
        deadline_seconds: float,
        system_prompt: str = TRIAGE_SYSTEM_PROMPT,
        fallback: TriageModel | None = None,
        primary_breaker_failures: int = 3,
        primary_breaker_open_seconds: float = 60.0,
    ) -> None:
        self._structured = model.with_structured_output(TriageVerdict, method="function_calling", include_raw=True)
        self.system_prompt = str(system_prompt)
        self.model_name = model_name
        self.deadline_seconds = float(deadline_seconds)
        self.fallback = fallback
        self._breaker = _PrimaryBreaker(threshold=primary_breaker_failures, open_seconds=primary_breaker_open_seconds)

    async def triage(self, human_text: str) -> TriageCallResult:
        """Primary call, then — only with a configured fallback — one fallback call when the primary fails."""

        if self.fallback is None:
            return await self._call(human_text)
        if self._breaker.is_open(time.monotonic()):
            return await self._fallback_call(human_text, primary_code="primary_circuit_open", primary_attempts=0)
        try:
            result = await self._call(human_text)
        except TriageModelError as exc:
            self._breaker.record_failure(time.monotonic())
            return await self._fallback_call(human_text, primary_code=exc.code, primary_attempts=exc.attempts)
        self._breaker.record_success()
        return result

    async def _fallback_call(self, human_text: str, *, primary_code: str, primary_attempts: int) -> TriageCallResult:
        if self.fallback is None:  # pragma: no cover - guarded by triage()
            raise TriageModelError(primary_code)
        try:
            result = await self.fallback._call(human_text)
        except TriageModelError as exc:
            raise TriageModelError(
                exc.code,
                retryable=exc.retryable,
                attempts=primary_attempts + exc.attempts,
                output_failure=exc.output_failure,
                finish_reason=exc.finish_reason,
                output_tokens=exc.output_tokens,
                detail=exc.detail,
                primary_code=primary_code,
            ) from exc
        return replace(result, attempts=primary_attempts + result.attempts, fallback_from=primary_code)

    async def _call(self, human_text: str) -> TriageCallResult:
        """One structured call within ``deadline_seconds``; a fast retryable failure — transport, or an unusable
        answer (empty tool call, missing field) — earns one more attempt inside the deadline."""

        started = time.perf_counter()
        deadline = started + self.deadline_seconds
        messages = [SystemMessage(self.system_prompt), HumanMessage(human_text)]
        attempts = 0
        while True:
            attempts += 1
            remaining = deadline - time.perf_counter()
            try:
                out = await asyncio.wait_for(self._structured.ainvoke(messages), timeout=max(0.001, remaining))
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
            parsed = out.get("parsed") if isinstance(out, Mapping) else None
            raw = out.get("raw") if isinstance(out, Mapping) else None
            usage = getattr(raw, "usage_metadata", None) or {}
            novelty_defaulted = False
            if isinstance(parsed, TriageVerdict):
                break
            finish_reason = _finish_reason(raw)
            if (
                finish_reason != "length"
                and attempts < 2
                and (deadline - time.perf_counter()) >= _MIN_RETRY_BUDGET_SECONDS
            ):
                continue  # an empty/invalid tool call is usually transient at temperature 0 (#61 probe: 31/44 recover)
            parsed = _verdict_without_novelty(raw)
            if parsed is not None:
                # The retry (or the deadline) is spent and the answer is a complete verdict minus the novelty field:
                # use it as new_fact (prompt-v5 quality, no novelty judgment) rather than fall back on rules.
                novelty_defaulted = True
                break
            parsing_error = out.get("parsing_error") if isinstance(out, Mapping) else None
            raise TriageModelError(
                "news_triage_output_truncated" if finish_reason == "length" else "news_triage_output_invalid",
                attempts=attempts,
                output_failure=True,
                finish_reason=finish_reason,
                output_tokens=usage.get("output_tokens"),
                detail=(f"{type(parsing_error).__name__}: {parsing_error}"[:_DETAIL_CHARS] if parsing_error else None),
            )
        latency_ms = int((time.perf_counter() - started) * 1000)
        details = usage.get("input_token_details") or {}
        return TriageCallResult(
            verdict=parsed,
            latency_ms=latency_ms,
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            cached_tokens=details.get("cache_read") if isinstance(details, Mapping) else None,
            model=self.model_name,
            attempts=attempts,
            novelty_defaulted=novelty_defaulted,
        )


def _verdict_without_novelty(raw: Any) -> TriageVerdict | None:
    """The one lenient parse, tried only once the retry budget is spent: a tool call that is a full verdict except
    for the required ``novelty`` field. Such an answer carries a usable judgment (it is exactly what prompt v5
    returned), so it is accepted as ``new_fact`` and traced as ``novelty_defaulted`` instead of being counted as an
    output failure; anything else stays a failure."""

    calls = getattr(raw, "tool_calls", None) or []
    args = calls[0].get("args") if calls and isinstance(calls[0], Mapping) else None
    if not isinstance(args, Mapping) or "novelty" in args or not args:
        return None
    try:
        return TriageVerdict.model_validate({"novelty": "new_fact", **dict(args)})
    except ValueError:
        return None


__all__ = [
    "TOLD_MAX",
    "TOLD_SAME_KEY_MAX",
    "TOLD_WINDOW_MS",
    "TRIAGE_PROMPT_VERSION",
    "TriageCallResult",
    "TriageModel",
    "TriageModelError",
    "build_triage_input",
    "is_retryable_model_failure",
    "told_ledger_for_prompt",
]
