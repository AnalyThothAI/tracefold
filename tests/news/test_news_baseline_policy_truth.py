"""#160: the learning metric and runtime share policy-v16 action truth."""

from __future__ import annotations

from typing import Any

import pytest

from tests.support.news_judgment import news_taxonomy, recorded_decision, scored_judgment
from tracefold.news.artifact_identity import canonical_sha
from tracefold.news.learning.baseline import BaselineCase, run_baseline
from tracefold.news.learning.metric import CandidatePrediction, accepted_review_metric, build_compile_example
from tracefold.news.learning.objective import DevelopmentEpisode
from tracefold.news.models import TRIAGE_POLICY_VERSION
from tracefold.news.program.artifact import load_stable_program_state
from tracefold.news.program.contracts import ScoredJudgment, TriageContext
from tracefold.news.taxonomy import SourceAuthority
from tracefold.news.triage_rules import DEFAULT_POLICY

_CARD: dict[str, Any] = {
    "event_id": "e" * 64,
    "evidence_version": 1,
    "evidence_sha256": "a" * 64,
    "focus_fact_id": "f" * 64,
    "leader_title": "Issuer publishes a material update",
    "leader_description": "",
    "reporting_origin": "wire",
    "dedupe_family": "general",
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
    "assets": [{"symbol": "TSLA", "role": "primary"}],
    "direction": "bullish",
    "scope": "single_name",
    "fact_kind": "state_change",
    "evidence_ref": "c1",
    "confidence": 0.9,
    "headline_zh": "发行人发布重大更新",
    "why_zh": "该更新改变盈利和现金流预期",
}


def _judgment(
    fact_kind: str,
    event_family: str,
    source_authority: SourceAuthority,
) -> ScoredJudgment:
    """One production judgment whose three action-bearing observations the caller names."""

    return scored_judgment(
        {**_VERDICT, "fact_kind": fact_kind},
        taxonomy=news_taxonomy(event_family=event_family),
        source_authority=source_authority,
    )


def _episode(
    policy_values: dict[str, Any] | None,
    *,
    fact_kind: str = "state_change",
    event_family: str = "other",
    source_authority: SourceAuthority = "unknown",
    watchlist: bool = False,
) -> DevelopmentEpisode:
    projection: dict[str, Any] = {
        "gate": {
            "grounded_assets": ["TSLA"],
            "watchlist_symbols": ["TSLA"] if watchlist else [],
            "admission": "candidate",
        },
        "storyline": {"title": "Issuer", "dedupe_family": "general"},
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
    return DevelopmentEpisode(
        case_id="c" * 64,
        cluster_id="k" * 64,
        stratum="delivered",
        context=TriageContext.from_card(_CARD, watchlist=(), told_rows=[], now_ms=1787000000000, queue_lag_ms=0),
        accepted_review={
            "should_push": "should_push",
            "dimensions": {"factual_fidelity": "pass"},
            "novelty": {"judgment": "new_fact", "duplicate_of": ""},
            "taxonomy": {
                "subject_codes": [],
                "event_family": event_family,
                "change_state": "unknown",
                "assertion_status": "unknown",
            },
        },
        production_judgment=_judgment(fact_kind, event_family, source_authority),
        policy_metric=projection,
    )


def _action(
    *,
    fact_kind: str = "state_change",
    event_family: str = "other",
    source_authority: SourceAuthority = "unknown",
    policy_values: dict[str, Any] | None = None,
    watchlist: bool = False,
) -> str:
    values = DEFAULT_POLICY.as_dict() if policy_values is None else policy_values
    episode = _episode(
        values,
        fact_kind=fact_kind,
        event_family=event_family,
        source_authority=source_authority,
        watchlist=watchlist,
    )
    judgment = _judgment(fact_kind, event_family, source_authority)
    outcome = accepted_review_metric(
        build_compile_example(episode),
        CandidatePrediction(
            verdict=judgment.verdict.model_dump(mode="json"),
            editorial=judgment.editorial.model_dump(mode="json"),
        ),
    )
    return str(outcome.production_action)


@pytest.mark.parametrize(
    ("fact_kind", "event_family", "source_authority", "expected"),
    [
        ("state_change", "other", "unknown", "push"),
        ("new_quantity", "financial_results", "unknown", "push"),
        # #675 §1 row 4: a material change in one of the four loud families escalates only once the code
        # can corroborate it. The registry naming the source is one of the two ways it can.
        ("official_measure", "macro_policy_data", "reputable_secondary", "escalate"),
        ("official_measure", "macro_policy_data", "unknown", "push"),
        # Row 1: four kinds of text that state no new fact about the world, whatever else is true of them.
        ("statement", "macro_policy_data", "reputable_secondary", "drop"),
        ("promotion", "other", "unknown", "drop"),
    ],
)
def test_fact_kind_owns_action(
    fact_kind: str, event_family: str, source_authority: SourceAuthority, expected: str
) -> None:
    assert _action(fact_kind=fact_kind, event_family=event_family, source_authority=source_authority) == expected


def test_objective_watchlist_guard_overrides_a_non_fact_kind() -> None:
    assert _action(fact_kind="statement", watchlist=False) == "drop"
    assert _action(fact_kind="statement", watchlist=True) == "push"


def test_a_policy_scored_example_without_a_policy_fails_closed() -> None:
    episode = _episode(None)
    judgment = scored_judgment(_VERDICT)
    with pytest.raises(ValueError, match="news_program_metric_policy_values_missing"):
        accepted_review_metric(
            build_compile_example(episode),
            CandidatePrediction(
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
            CandidatePrediction(
                verdict=judgment.verdict.model_dump(mode="json"),
                editorial=judgment.editorial.model_dump(mode="json"),
            ),
        )


def test_recorded_scoring_uses_the_complete_shipped_decision() -> None:
    case = BaselineCase(
        episode=_episode(None),
        recorded_decision_result=recorded_decision("push"),
    )
    report = run_baseline([case], mode="recorded", artifact=load_stable_program_state())
    assert report.cases[0].action == "push"
    assert report.identity["policy_sha256"] is None
