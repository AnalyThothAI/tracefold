"""Small builders for the #160 typed editorial-judgment test contract."""

from __future__ import annotations

from typing import Any

from tracefold.news.models import TriageVerdict
from tracefold.news.program.contracts import (
    EditorialEnvelope,
    ProgramCallTrace,
    ProgramTrace,
    ProgramUsage,
    ScoredJudgment,
    SemanticJudgment,
    TradeRelevanceV1,
    canonical_sha,
)
from tracefold.news.taxonomy import NewsTaxonomyV1


def news_taxonomy(**overrides: Any) -> NewsTaxonomyV1:
    values: dict[str, Any] = {
        "subject_codes": (),
        "event_family": "other",
        "change_state": "unknown",
        "assertion_status": "unknown",
        "source_authority": "unknown",
    }
    values.update(overrides)
    return NewsTaxonomyV1.model_validate(values)


def trade_relevance(**overrides: Any) -> TradeRelevanceV1:
    values: dict[str, Any] = {
        "impact_breadth": "single_instrument",
        "tradability": "direct",
        "surprise": "material_vs_expectation",
        "development_delta": "state_change",
        "channels": ["earnings_cashflow"],
        "affected_markets": ["single_asset"],
        "reader_value": "realtime",
    }
    values.update(overrides)
    return TradeRelevanceV1.model_validate(values)


def scored_judgment(
    verdict: dict[str, Any] | TriageVerdict,
    *,
    relevance: TradeRelevanceV1 | None = None,
    taxonomy: NewsTaxonomyV1 | None = None,
) -> ScoredJudgment:
    typed_verdict = verdict if isinstance(verdict, TriageVerdict) else TriageVerdict.model_validate(verdict)
    return ScoredJudgment.issue(
        verdict=typed_verdict,
        editorial=EditorialEnvelope.issue(
            relevance=relevance or trade_relevance(),
            taxonomy=taxonomy or news_taxonomy(),
        ),
    )


def recorded_decision(final: str, *, rule_baseline: str = "drop") -> dict[str, Any]:
    """Complete persisted ``DecisionResult`` projection used by recorded baselines."""

    return {
        "final": final,
        "override_rule": "recorded_fixture",
        "throttled_by": None,
        "rule_baseline": rule_baseline,
        "watchlist_hits": [],
        "seen_similarity": None,
        "seen_against": -1,
        "seen_scope": "",
    }


def semantic_judgment(
    verdict: dict[str, Any] | TriageVerdict,
    *,
    program_version: str,
    program_sha256: str,
    model: str = "fake",
) -> SemanticJudgment:
    """One complete, non-degraded `SemanticJudgment` for a test that needs the model seam to answer.

    `SemanticJudgment` validates that its trace and usage agree with the judgment it carries — the
    verdict hash, the editorial hash, and the aggregate of the call traces all have to line up — so
    a hand-written literal does not survive construction. Building it here keeps that arithmetic in
    one place for callers that only care that the model answered at all.
    """

    typed = verdict if isinstance(verdict, TriageVerdict) else TriageVerdict.model_validate(verdict)
    editorial = EditorialEnvelope.issue(
        relevance=trade_relevance(),
        taxonomy=news_taxonomy(),
    )
    calls = tuple(
        ProgramCallTrace(
            predictor=predictor,
            route="primary",
            route_slot="primary",
            attempt=1,
            request_sha256="0" * 64,
            input_sha256="0" * 64,
            model_binding="primary",
            physical_provider_call=True,
            runtime_provider="test",
            runtime_model=model,
            runtime_model_sha256="e" * 64,
            runtime_binding_sha256="f" * 64,
            output_sha256="d" * 64,
            validated_output={"marker": marker},
            provider="test",
            model=model,
            model_sha256="e" * 64,
            latency_ms=7,
            input_tokens=1,
            output_tokens=1,
            cached_tokens=0,
            total_tokens=2,
            provider_cost_microusd=1,
            finish_reason="stop",
            terminal_disposition="provider_success",
            invocation_sha256=marker * 64,
        )
        for predictor, marker in (("event_semantics", "8"), ("reader_card", "9"))
    )
    trace = ProgramTrace(
        program_version=program_version,
        program_sha256=program_sha256,
        context_sha256="1" * 64,
        envelope_sha256="0" * 64,
        event_semantics_sha256="5" * 64,
        reader_card_sha256="6" * 64,
        verdict_sha256=canonical_sha(typed.model_dump(mode="json")),
        editorial_sha256=editorial.editorial_sha256,
        answering_route="primary",
        fallback_from=None,
        calls=calls,
    )
    physical = tuple(call for call in trace.calls if call.physical_provider_call)
    return SemanticJudgment(
        verdict=typed,
        editorial=editorial,
        program_version=program_version,
        program_sha256=program_sha256,
        trace=trace,
        usage=ProgramUsage(
            wall_latency_ms=10,
            call_count=len(trace.calls),
            physical_call_count=len(physical),
            input_tokens=sum(call.input_tokens for call in trace.calls),
            output_tokens=sum(call.output_tokens for call in trace.calls),
            cached_tokens=sum(call.cached_tokens for call in trace.calls),
            total_tokens=sum(call.total_tokens for call in trace.calls),
            provider_cost_microusd=sum(int(call.provider_cost_microusd or 0) for call in physical),
        ),
        answering_model=model,
        fallback_from=None,
    )


def triage_verdict(**overrides: Any) -> TriageVerdict:
    """A complete, valid model verdict. Overrides name only the field a test is actually about."""

    values: dict[str, Any] = {
        "novelty": "new_fact",
        "restates": -1,
        "assets": [{"symbol": "NVDA", "role": "primary"}],
        "direction": "bullish",
        "scope": "single_name",
        "magnitude": 2,
        "confidence": 0.8,
        "audience": "us_equity",
        "headline_zh": "\u82f1\u4f1f\u8fbe\u6295\u8d44 OpenAI",
        "why_zh": "\u91cd\u5927\u6295\u8d44\u4f1a\u6539\u53d8\u7b97\u529b\u9700\u6c42",
    }
    values.update(overrides)
    return TriageVerdict(**values)
