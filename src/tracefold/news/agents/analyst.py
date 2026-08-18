"""Analyst: one structured LangChain call over a code-prefetched evidence bundle, verified by verify_verdict()."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from tracefold.news.analyst_evidence import EvidenceBundle
from tracefold.news.analyst_rules import VerifyResult, verify_verdict
from tracefold.news.models import ANALYST_PROMPT_VERSION, AnalystVerdict

from .prompts import ANALYST_PROMPT_SHA256, ANALYST_SYSTEM_PROMPT

MAX_ATTEMPTS = 2  # one bounded correction round after a verify_verdict rejection


@dataclass(frozen=True, slots=True)
class AnalystRunResult:
    verdict: AnalystVerdict | None
    verify: VerifyResult
    latency_ms: int
    attempts: int
    error_code: str | None
    evidence_count: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_tokens: int | None = None


class Analyst:
    def __init__(self, *, model: BaseChatModel, model_name: str, deadline_seconds: float) -> None:
        self._structured = model.with_structured_output(AnalystVerdict, method="function_calling", include_raw=True)
        self.model_name = model_name
        self.deadline_seconds = float(deadline_seconds)
        self.prompt_sha256 = ANALYST_PROMPT_SHA256

    async def analyze(self, *, bundle: EvidenceBundle, triage_direction: str) -> AnalystRunResult:
        started = time.perf_counter()
        deadline = started + self.deadline_seconds
        rejected: str | None = None
        verify = VerifyResult(False, "not_run")
        verdict: AnalystVerdict | None = None
        usage: dict[str, int | None] = {"input_tokens": None, "output_tokens": None, "cached_tokens": None}
        attempts = 0
        for attempts in range(1, MAX_ATTEMPTS + 1):
            remaining = deadline - time.perf_counter()
            if remaining <= 0.5:
                return AnalystRunResult(
                    None,
                    VerifyResult(False, "timeout"),
                    _ms(started),
                    attempts - 1,
                    "news_analyst_timeout",
                    len(bundle.evidence),
                    **usage,
                )
            human = bundle.human_message(rejected_reason=rejected)
            try:
                out = await asyncio.wait_for(
                    self._structured.ainvoke([SystemMessage(ANALYST_SYSTEM_PROMPT), HumanMessage(human)]),
                    timeout=remaining,
                )
            except TimeoutError:
                return AnalystRunResult(
                    None,
                    VerifyResult(False, "timeout"),
                    _ms(started),
                    attempts,
                    "news_analyst_timeout",
                    len(bundle.evidence),
                    **usage,
                )
            except Exception as exc:  # provider/network/schema failures are expected here
                return AnalystRunResult(
                    None,
                    VerifyResult(False, "model_failed"),
                    _ms(started),
                    attempts,
                    f"news_analyst_failed:{type(exc).__name__}",
                    len(bundle.evidence),
                    **usage,
                )
            usage = _accumulate_usage(usage, out)
            parsed = out.get("parsed") if isinstance(out, Mapping) else None
            if not isinstance(parsed, AnalystVerdict):
                return AnalystRunResult(
                    None,
                    VerifyResult(False, "no_structured_output"),
                    _ms(started),
                    attempts,
                    "news_analyst_no_structured_output",
                    len(bundle.evidence),
                    **usage,
                )
            verdict = parsed
            verify = verify_verdict(verdict, tool_evidence=bundle.evidence, triage_direction=triage_direction)
            if verify.ok:
                break
            rejected = verify.reason
        return AnalystRunResult(
            verdict,
            verify,
            _ms(started),
            attempts,
            None if verify.ok else f"news_analyst_verify:{verify.reason}",
            len(bundle.evidence),
            **usage,
        )


def _accumulate_usage(current: dict[str, int | None], out: Any) -> dict[str, int | None]:
    raw = out.get("raw") if isinstance(out, Mapping) else None
    meta = getattr(raw, "usage_metadata", None) or {}
    details = meta.get("input_token_details") or {}
    cached = details.get("cache_read") if isinstance(details, Mapping) else None
    return {
        "input_tokens": _add(current["input_tokens"], meta.get("input_tokens")),
        "output_tokens": _add(current["output_tokens"], meta.get("output_tokens")),
        "cached_tokens": _add(current["cached_tokens"], cached),
    }


def _add(a: int | None, b: Any) -> int | None:
    if b is None:
        return a
    return int(a or 0) + int(b)


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


__all__ = ["ANALYST_PROMPT_VERSION", "MAX_ATTEMPTS", "Analyst", "AnalystRunResult"]
