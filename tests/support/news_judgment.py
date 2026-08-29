"""Small builders for the #160 typed editorial-judgment test contract."""

from __future__ import annotations

from typing import Any

from tracefold.news.models import TriageVerdict
from tracefold.news.program.contracts import EditorialEnvelope, ScoredJudgment, TradeRelevanceV1
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
