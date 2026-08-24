"""#160: the learning metric and runtime share policy-v10 action truth."""

from __future__ import annotations

from typing import Any

import dspy  # type: ignore[import-untyped]
import pytest

from tests.support.news_judgment import recorded_decision, scored_judgment, trade_relevance
from tracefold.news.artifact_identity import canonical_sha
from tracefold.news.learning.baseline import BaselineCase, run_baseline
from tracefold.news.learning.metric import DevelopmentEpisode, accepted_review_metric, build_compile_example
from tracefold.news.models import TRIAGE_POLICY_VERSION
from tracefold.news.program.artifact import load_stable_program_artifact
from tracefold.news.program.contracts import TriageContext
from tracefold.news.triage_rules import DEFAULT_POLICY

_CARD: dict[str, Any] = {
    "event_id": "e" * 64,
    "evidence_version": 1,
    "evidence_sha256": "a" * 64,
    "focus_fact_id": "f" * 64,
    "leader_title": "Issuer publishes a material update",
    "leader_description": "",
    "reporting_origin": "wire",
    "family": "general",
    "admission": "candidate",
    "queue_priority": "normal",
    "asset_class": "equity_or_commodity",
    "engine_type": "news",
    "storyline_key": "asset:TSLA",
    "comparison_title": "issuer publishes a material update",
    "raw_first_line": "Issuer publishes a material update",
    "grounded_assets": ["TSLA"],
    "watchlist_hits": [],
    "member_count": 1,
    "opened_at_ms": 1787000000000,
    "provenance": ["1018"],
    "provider_metadata": {},
}

_VERDICT: dict[str, Any] = {
    "novelty": "new_fact",
    "restates": -1,
    "event_type": "filing",
    "assets": [{"symbol": "TSLA", "role": "primary"}],
    "magnitude": 2,
    "direction": "bullish",
    "actionable": True,
    "audience": "us_equity",
    "scope": "single_name",
    # Compatibility intent is deliberately contrary to the relevance result.
    "decision": "drop",
    "confidence": 0.9,
    "headline_zh": "发行人发布重大更新",
    "why_zh": "该更新改变盈利和现金流预期",
    "title_zh": "",
}


def _episode(
    policy_values: dict[str, Any] | None,
    *,
    relevance: dict[str, Any] | None = None,
    watchlist: bool = False,
) -> DevelopmentEpisode:
    projection: dict[str, Any] = {
        "gate": {
            "grounded_assets": ["TSLA"],
            "watchlist_symbols": ["TSLA"] if watchlist else [],
            "admission": "candidate",
        },
        "storyline": {"title": "Issuer", "family": "general"},
        "seen": [],
    }
    if policy_values is not None:
        projection.update(
            {
                "policy_version": TRIAGE_POLICY_VERSION,
                "policy_values": policy_values,
                "policy_sha256": canonical_sha(policy_values),
            }
        )
    production_relevance = trade_relevance(**(relevance or {}))
    return DevelopmentEpisode(
        case_id="c" * 64,
        cluster_id="k" * 64,
        stratum="delivered",
        context=TriageContext.from_card(_CARD, watchlist=(), told_rows=[], now_ms=1787000000000, queue_lag_ms=0),
        accepted_review={
            "should_push": "should_push",
            "dimensions": {"factual_fidelity": "pass"},
            "novelty": {"judgment": "new_fact", "duplicate_of": ""},
        },
        production_judgment=scored_judgment(_VERDICT, relevance=production_relevance),
        policy_metric=projection,
    )


def _action(
    *,
    relevance: dict[str, Any] | None = None,
    policy_values: dict[str, Any] | None = None,
    watchlist: bool = False,
) -> str:
    values = DEFAULT_POLICY.as_dict() if policy_values is None else policy_values
    episode = _episode(values, relevance=relevance, watchlist=watchlist)
    judgment = scored_judgment(_VERDICT, relevance=trade_relevance(**(relevance or {})))
    outcome = accepted_review_metric(
        build_compile_example(episode),
        dspy.Prediction(
            verdict=judgment.verdict.model_dump(mode="json"),
            editorial=judgment.editorial.model_dump(mode="json"),
        ),
        None,
        None,
        None,
    )
    return str(outcome.production_action)


@pytest.mark.parametrize(
    ("relevance", "expected"),
    [
        ({"reader_value": "realtime"}, "push"),
        ({"reader_value": "escalate"}, "escalate"),
        (
            {
                "reader_value": "background",
                "tradability": "contextual",
                "channels": [],
                "affected_markets": [],
            },
            "drop",
        ),
    ],
)
def test_trade_relevance_owns_action_not_compatibility_intent(relevance: dict[str, Any], expected: str) -> None:
    assert _VERDICT["decision"] == "drop"
    assert _action(relevance=relevance) == expected


def test_realtime_requires_the_exact_material_direct_surface() -> None:
    assert _action(relevance={"development_delta": "color_only"}) == "drop"
    assert _action(relevance={"reader_value": "realtime"}) == "push"


def test_objective_watchlist_guard_overrides_background_editorial_value() -> None:
    background = {
        "reader_value": "background",
        "tradability": "contextual",
        "channels": [],
        "affected_markets": [],
    }
    assert _action(relevance=background, watchlist=False) == "drop"
    assert _action(relevance=background, watchlist=True) == "push"


def test_a_policy_scored_example_without_a_policy_fails_closed() -> None:
    episode = _episode(None)
    judgment = scored_judgment(_VERDICT)
    with pytest.raises(ValueError, match="news_program_metric_policy_values_missing"):
        accepted_review_metric(
            build_compile_example(episode),
            dspy.Prediction(
                verdict=judgment.verdict.model_dump(mode="json"),
                editorial=judgment.editorial.model_dump(mode="json"),
            ),
        )


def test_a_tampered_policy_hash_fails_closed() -> None:
    values = DEFAULT_POLICY.as_dict()
    episode = _episode(values)
    tampered = dict(episode.policy_metric)
    tampered["policy_values"] = {**values, "similarity_max": 0.9}
    judgment = scored_judgment(_VERDICT)
    with pytest.raises(ValueError, match=r"news_program_metric_policy_sha256_mismatch:[0-9a-f]{16}!=[0-9a-f]{16}"):
        accepted_review_metric(
            build_compile_example(episode.model_copy(update={"policy_metric": tampered})),
            dspy.Prediction(
                verdict=judgment.verdict.model_dump(mode="json"),
                editorial=judgment.editorial.model_dump(mode="json"),
            ),
        )


def test_recorded_scoring_uses_the_complete_shipped_decision() -> None:
    case = BaselineCase(
        episode=_episode(None),
        recorded_decision_result=recorded_decision("push"),
    )
    report = run_baseline([case], mode="recorded", artifact=load_stable_program_artifact())
    assert report.cases[0].action == "push"
    assert report.identity["policy_sha256"] is None
