from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from typing import Any

import pytest

import tracefold.news.learning.evaluate as candidate_evaluator_module
from tests.support.news_judgment import news_taxonomy
from tracefold.news.learning.evaluate import ArmManifest
from tracefold.news.learning.profile import _PROFILE
from tracefold.news.models import TriageVerdict
from tracefold.news.program.artifact import NewsProgramStateV1
from tracefold.news.program.contracts import EditorialEnvelope, ScoredJudgment, TradeRelevanceV1
from tracefold.news.program.identity import EXECUTION_ENVELOPE_SHA256
from tracefold.news.program.runtime import PROGRAM_VERSION
from tracefold.news.triage_rules import DEFAULT_POLICY


def test_arm_manifest_identity_is_program_native() -> None:
    policy = DEFAULT_POLICY.as_dict()
    arm = ArmManifest(
        program_version=PROGRAM_VERSION,
        program_sha256="a" * 64,
        envelope_sha256="d" * 64,
        runtime_model_bindings_sha256="c" * 64,
        retrieval_sha256="b" * 64,
        policy=policy,
        policy_sha256=_sha(policy),
    )

    assert arm.bundle_sha == _sha(arm.model_dump(mode="json"))
    assert set(arm.model_dump()) == {
        "program_version",
        "program_sha256",
        "envelope_sha256",
        "runtime_model_bindings_sha256",
        "retrieval_sha256",
        "policy",
        "policy_sha256",
    }


@pytest.mark.parametrize(
    ("executions", "error_code"),
    [
        (
            [
                {"execution_index": 0, "trace": None, "recording_call_indices": []},
                {"execution_index": 2, "trace": None, "recording_call_indices": []},
            ],
            "news_program_execution_index_mismatch",
        ),
        (
            [
                {"execution_index": 1, "trace": None, "recording_call_indices": []},
                {"execution_index": 0, "trace": None, "recording_call_indices": []},
            ],
            "news_program_execution_index_mismatch",
        ),
        (
            [
                {
                    "execution_index": 0,
                    "context_sha256": "a" * 64,
                    "context": {"marker": "context-mismatch"},
                    "trace": {"context_sha256": "b" * 64, "calls": []},
                    "recording_call_indices": [],
                }
            ],
            "news_program_execution_context_mismatch",
        ),
        (
            [
                {
                    "execution_index": 0,
                    "context_sha256": "a" * 64,
                    "context": {"marker": "call-index"},
                    "trace": {"context_sha256": "a" * 64, "calls": [{}]},
                    "recording_call_indices": [1],
                }
            ],
            "news_program_execution_call_index_mismatch",
        ),
        (
            [
                {
                    "execution_index": 0,
                    "context_sha256": "a" * 64,
                    "context": [],
                    "trace": {"context_sha256": "a" * 64, "calls": []},
                    "recording_call_indices": [],
                }
            ],
            "news_program_execution_context_mismatch",
        ),
    ],
)
def test_observed_program_execution_identity_fails_closed(executions: list[dict[str, object]], error_code: str) -> None:
    if error_code == "news_program_execution_call_index_mismatch":
        context = dict(executions[0]["context"])  # type: ignore[arg-type]
        context_sha = _sha(context)
        executions[0]["context_sha256"] = context_sha
        executions[0]["trace"]["context_sha256"] = context_sha  # type: ignore[index]
    row: dict[str, object] = {"trace": {"program_executions": executions}}
    if error_code != "news_program_execution_index_mismatch":
        verdict = _verdict()
        observed_fields = _observed_judgment_fields(verdict)
        selected_trace = dict(executions[0]["trace"])  # type: ignore[arg-type]
        selected_trace["verdict_sha256"] = _sha(verdict)
        executions[0]["trace"] = selected_trace
        row = {
            **observed_fields,
            "trace": {
                "program_execution_index": 0,
                "program_trace": selected_trace,
                "program_executions": executions,
            },
        }
    with pytest.raises(ValueError, match=error_code):
        candidate_evaluator_module._observed_production_output(row)


def test_partial_provider_cost_and_incomplete_call_identity_are_not_complete() -> None:
    assert (
        candidate_evaluator_module._usage_from_trace(
            {
                "calls": [
                    {"physical_provider_call": True, "provider_cost_microusd": 10},
                    {"physical_provider_call": True, "provider_cost_microusd": None},
                ]
            }
        )["provider_cost_microusd"]
        is None
    )
    assert (
        candidate_evaluator_module._usage_from_trace(
            {
                "calls": [
                    {"physical_provider_call": True, "provider_cost_microusd": 10},
                    {"physical_provider_call": True, "provider_cost_microusd": 20},
                ]
            }
        )["provider_cost_microusd"]
        == 30
    )
    assert (
        candidate_evaluator_module._usage_from_trace(
            {
                "calls": [
                    {"physical_provider_call": True},
                    {"physical_provider_call": False},
                    {"physical_provider_call": True},
                ]
            }
        )["physical_call_count"]
        == 2
    )

    runtime_model_sha = _sha({"provider": "fixture-provider", "model": "configured-model"})
    runtime_binding_sha = _sha(
        {
            "provider": "fixture-provider",
            "model": "configured-model",
            "model_sha256": runtime_model_sha,
        }
    )
    call = {
        "predictor": "event_semantics",
        "route": "primary",
        "attempt": 1,
        "request_sha256": "1" * 64,
        "input_sha256": "2" * 64,
        "model_binding": "news_triage_primary",
        "physical_provider_call": True,
        "runtime_provider": "fixture-provider",
        "runtime_model": "configured-model",
        "runtime_model_sha256": runtime_model_sha,
        "runtime_binding_sha256": runtime_binding_sha,
        "provider": "fixture-provider",
        "model": "resolved-model",
        "model_sha256": _sha({"provider": "fixture-provider", "model": "resolved-model"}),
        "validated_output": {"decision": "push"},
    }
    # The trace-level identity a physical call must carry is the execution envelope hash: it is computed
    # from the whole code-owned surface, so an observation produced under any other envelope — including one
    # nobody declared a version for — cannot be scored against this one.
    assert candidate_evaluator_module._program_call_provenance_complete(
        {
            "trace": {"envelope_sha256": EXECUTION_ENVELOPE_SHA256},
            "calls": [call],
            "usage": {"physical_call_count": 1},
        }
    )
    assert not candidate_evaluator_module._program_call_provenance_complete(
        {
            "trace": {"envelope_sha256": "9" * 64},
            "calls": [call],
            "usage": {"physical_call_count": 1},
        }
    )
    assert not candidate_evaluator_module._program_call_provenance_complete(
        {
            "calls": [{key: value for key, value in call.items() if key != "runtime_binding_sha256"}],
            "trace": {"envelope_sha256": EXECUTION_ENVELOPE_SHA256},
            "usage": {"physical_call_count": 1},
        }
    )

    synthetic = {
        "predictor": "event_semantics",
        "route": "primary",
        "physical_provider_call": False,
        "error_code": "news_program_model_binding_unresolved",
    }
    fallback_semantics = {**call, "route": "fallback", "provider_cost_microusd": 10}
    fallback_card = {
        **call,
        "predictor": "reader_card",
        "route": "fallback",
        "provider_cost_microusd": 20,
    }
    trace = {"envelope_sha256": EXECUTION_ENVELOPE_SHA256, "calls": [synthetic, fallback_semantics, fallback_card]}
    usage = candidate_evaluator_module._usage_from_trace(trace)
    observation = {"trace": trace, "calls": trace["calls"], "usage": usage}

    assert usage == {
        "wall_latency_ms": None,
        "call_count": 3,
        "physical_call_count": 2,
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_tokens": 0,
        "total_tokens": 0,
        "provider_cost_microusd": 30,
    }
    assert candidate_evaluator_module._program_metric(observation)["call_count"] == 2
    assert candidate_evaluator_module._program_metric(observation)["trace_entry_count"] == 3
    assert candidate_evaluator_module._provider_cost_observation_complete(observation)
    assert candidate_evaluator_module._program_call_provenance_complete(observation)
    costs = candidate_evaluator_module._program_cost_by_predictor(
        [{"stable": {"program": [observation]}, "candidate": {"program": []}}]
    )["stable"]
    assert costs["event_semantics:primary"]["trace_entry_n"] == 1
    assert costs["event_semantics:primary"]["call_n"] == 0
    assert costs["event_semantics:fallback"]["call_n"] == 1
    assert costs["reader_card:fallback"]["call_n"] == 1


def test_observed_program_selected_trace_and_verdict_fail_closed() -> None:
    verdict = {"decision": "push"}
    selected_trace = {
        "context_sha256": "a" * 64,
        "verdict_sha256": _sha(verdict),
        "calls": [],
    }
    execution = {
        "execution_index": 0,
        "context_sha256": "a" * 64,
        "trace": selected_trace,
        "recording_call_indices": [],
    }
    with pytest.raises(ValueError, match="news_program_selected_execution_mismatch"):
        candidate_evaluator_module._observed_production_output(
            {
                "verdict": verdict,
                "trace": {
                    "program_execution_index": 0,
                    "program_trace": {**selected_trace, "answering_route": "fallback"},
                    "program_executions": [execution],
                },
            }
        )

    mismatched_verdict_trace = {**selected_trace, "verdict_sha256": "f" * 64}
    with pytest.raises(ValueError, match="news_program_selected_verdict_mismatch"):
        candidate_evaluator_module._observed_production_output(
            {
                "verdict": verdict,
                "trace": {
                    "program_execution_index": 0,
                    "program_trace": mismatched_verdict_trace,
                    "program_executions": [{**execution, "trace": mismatched_verdict_trace}],
                },
            }
        )


def test_observed_non_degraded_program_requires_a_selected_execution() -> None:
    context = {"event_id": "event-nondegraded", "phase": "initial"}
    context_sha = _sha(context)
    execution = {
        "execution_index": 0,
        "phase": "initial",
        "status": "completed",
        "context_sha256": context_sha,
        "context": context,
        "trace": {"context_sha256": context_sha, "calls": []},
        "usage": {"call_count": 0, "physical_call_count": 0},
        "recording_call_indices": [],
    }

    with pytest.raises(ValueError, match="news_program_selected_execution_mismatch"):
        candidate_evaluator_module._observed_production_output(
            {
                "verdict": _verdict(),
                "degraded": False,
                "verdict_error_code": "provider_unavailable",
                "trace": {"program_executions": [execution]},
            }
        )


def test_the_release_profile_holds_no_development_corpus_quota_at_all() -> None:
    """#651 §9: the 30/100/50 cluster floors, the strata minimum and the safety case are gone.

    Written against the key set rather than five deleted names, for the same reason #259 wrote the
    previous version of this test that way: the failure it guards against is not "somebody restored
    `boundary_clusters_min`", it is "somebody added `min_cases` and called it a different rule". Whether
    a corpus can be optimized is now a question about one target's split, and `GepaObjectivePlan`
    answers it with four structural codes; a profile threshold could only re-introduce the quota that
    refused a good explanation corpus for holding little taxonomy Gold.
    """

    assert "development" not in _PROFILE
    assert set(_PROFILE) == {"profile_id", "validation", "guardrails", "bootstrap", "supported_candidates"}
    assert not any(
        word in key for key in _PROFILE for word in ("boundary", "retention", "negative", "strata", "safety", "cluster")
    )


def test_out_of_time_generalization_still_belongs_entirely_to_the_future_holdout() -> None:
    """#259 §5.3: removing the development calendar gate must not loosen the one that measures time.

    The Future Holdout is the only place this system claims out-of-sample evidence, and it claims it with
    a window that opens after the candidate was registered, runs at least a day and carries real reviewed
    clusters. A development diagnostic may never be quoted in its place.
    """

    assert _PROFILE["validation"]["duration_hours_min"] == 24
    assert _PROFILE["validation"]["eligible_events_min"] == 200
    assert _PROFILE["validation"]["primary_clusters_min"] == 30


def _sha(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _verdict() -> dict[str, object]:
    return {
        "novelty": "new_fact",
        "restates": -1,
        "assets": [],
        "direction": "bullish",
        "scope": "sector",
        "magnitude": 2,
        "confidence": 0.8,
        "audience": "us_equity",
        "headline_zh": "DRAM 合约价续涨",
        "why_zh": "行业价格继续改善，但持续性仍需后续数据确认。",
    }


def _observed_judgment_fields(verdict: dict[str, object]) -> dict[str, object]:
    relevance = TradeRelevanceV1(
        impact_breadth="sector",
        tradability="direct",
        surprise="material_vs_expectation",
        development_delta="state_change",
        channels=("commodity_demand",),
        affected_markets=("us_equity_broad",),
        reader_value="realtime",
    )
    editorial = EditorialEnvelope.issue(relevance=relevance, source_authority="unknown", taxonomy=news_taxonomy())
    scored = ScoredJudgment.issue(
        verdict=TriageVerdict.model_validate(verdict),
        editorial=editorial,
    )
    return {
        "verdict": verdict,
        "model_editorial": editorial.model_dump(mode="json"),
        "judgment_sha256": scored.scored_judgment_sha256,
    }


def test_stable_or_common_execution_blocks_only_past_the_shared_rate_cap() -> None:
    """#294: a handful of transient stable/common failures cannot veto a corpus-scale live comparison.

    The affected pairs are excluded from every comparison denominator; what must keep blocking is a
    mass failure, which would otherwise turn into a vacuous PASS. The cap is deliberately the same
    `candidate_degraded_or_error_rate_max` that bounds candidate degradation — one knob, one concern —
    so the boundary cases below are derived from the sealed profile value rather than pinned to it.
    """

    unavailability = candidate_evaluator_module.stable_or_common_execution_unavailability
    cap = float(_PROFILE["guardrails"]["candidate_degraded_or_error_rate_max"])
    pair_n = 477
    at_cap = math.floor(cap * pair_n)  # the largest count whose rate does not exceed the cap

    rate, blocked = unavailability(0, pair_n)
    assert (rate, blocked) == (0.0, False)
    rate, blocked = unavailability(at_cap, pair_n)
    assert not blocked and rate == at_cap / pair_n
    rate, blocked = unavailability(at_cap + 1, pair_n)
    assert blocked and rate == (at_cap + 1) / pair_n
    # every pair failing is exactly the vacuous-PASS shape the blocker exists for
    assert unavailability(pair_n, pair_n) == (1.0, True)
    # no assigned pairs at all is an evidence gap, never a pass
    assert unavailability(1, 0) == (1.0, True)


def _stable_artifact() -> NewsProgramStateV1:
    from tracefold.news.program.artifact import load_stable_program_state

    return load_stable_program_state()


def _state(**overrides: str) -> NewsProgramStateV1:
    parent = _stable_artifact()
    instructions = {name: parent.instruction_for(name) for name in ("event_semantics", "taxonomy", "reader_card")}
    instructions.update(overrides)
    return NewsProgramStateV1.from_instructions(instructions)


def test_taxonomy_only_is_read_off_the_state_document_not_declared() -> None:
    """#548: the class is the byte difference against the parent, and nothing else says so."""

    parent = _stable_artifact()
    taxonomy_only = _state(taxonomy=parent.instruction_for("taxonomy") + "\nPrefer the narrower code.")

    assert taxonomy_only.changed_predictors(parent) == ("taxonomy",)
    # A state that also moves a reader-facing Predictor keeps every pairwise stage.
    also_reader_card = _state(
        taxonomy=parent.instruction_for("taxonomy") + "\nPrefer the narrower code.",
        reader_card=parent.instruction_for("reader_card") + "\nKeep the first clause concrete.",
    )
    assert also_reader_card.changed_predictors(parent) == ("taxonomy", "reader_card")
    # An unchanged state changes nothing and is not this class either.
    assert _state().changed_predictors(parent) == ()
    # A demo attached to the taxonomy Predictor is a change, which the three-string patch could not say.
    from tracefold.news.program.artifact import predictor_document

    with_demo = parent.with_predictor_document(
        "taxonomy",
        predictor_document(
            "taxonomy",
            instruction=parent.instruction_for("taxonomy"),
            demos=[{"evidence_json": "<evidence>", "taxonomy": {"subject_codes": []}}],
        ),
    )
    assert with_demo.changed_predictors(parent) == ("taxonomy",)


_GOLD_AXES: dict[str, Any] = {
    "subject_codes": ["medtop:20000205"],
    "event_family": "product_service_change",
    "change_state": "announced",
    "assertion_status": "confirmed",
}


def _axes(**overrides: Any) -> dict[str, Any]:
    return {**_GOLD_AXES, **overrides}


def _taxonomy_evidence(pairs: Sequence[tuple[dict[str, Any], dict[str, Any]]]) -> dict[str, Any]:
    """The evaluator's own release evidence over one (stable, candidate) answer per independent cluster."""

    def arm(axes: dict[str, Any]) -> dict[str, Any]:
        taxonomy = news_taxonomy(**axes).model_dump(mode="json")
        return {"editorial": {"taxonomy": taxonomy}}

    observations = [
        {
            "case_ref": {
                "case_id": f"case-{index}",
                "cluster_id": f"cluster-{index}",
                "review_id": f"review-{index}",
            },
            "stable": arm(stable),
            "candidate": arm(candidate),
        }
        for index, (stable, candidate) in enumerate(pairs)
    ]
    reviews = {
        f"review-{index}": {"payload": {"taxonomy": _GOLD_AXES, "first_bad_owner": None}} for index in range(len(pairs))
    }
    return candidate_evaluator_module._taxonomy_release_evidence(observations, reviews)


def _exact_pair() -> tuple[dict[str, Any], dict[str, Any]]:
    """A cluster both arms answer exactly right: it contributes a zero to every paired delta."""

    return _axes(), _axes()


def _candidate_fixes_family() -> tuple[dict[str, Any], dict[str, Any]]:
    return _axes(event_family="other"), _axes()


def _candidate_breaks_assertion() -> tuple[dict[str, Any], dict[str, Any]]:
    return _axes(), _axes(assertion_status="claimed")


def _candidate_completes_a_card() -> tuple[dict[str, Any], dict[str, Any]]:
    """A card Stable classified all but one axis right, which the candidate gets fully right."""

    return _axes(assertion_status="claimed"), _axes()


def _candidate_worsens_an_already_wrong_card() -> tuple[dict[str, Any], dict[str, Any]]:
    """A card neither arm classifies correctly, on which the candidate loses a second axis."""

    return _axes(event_family="other"), _axes(event_family="other", assertion_status="claimed")


def test_one_cluster_slipping_among_many_is_not_a_taxonomy_axis_regression() -> None:
    """#567: the per-axis rule is the paired bootstrap interval, not the sign of a mean.

    This is candidate `3f7d1e12…` in miniature. Twelve clusters of forty gain the event family Stable got
    wrong, one cluster loses an assertion status Stable had right, and the rest are already exact. Under
    #548 the single slip made `assertion_status_accuracy` negative and the whole release a FAIL. The
    interval around that delta reaches zero — a corpus this size cannot tell one flipped cluster from
    noise — so the axis is not a regression, while the candidate raises the classification partial score
    that admits it since #651 §8 with its whole interval above zero, and the holdout passes.
    """

    evidence = _taxonomy_evidence(
        [_candidate_fixes_family() for _ in range(12)]
        + [_candidate_breaks_assertion()]
        + [_exact_pair() for _ in range(27)]
    )

    assert evidence["schema"] == "tracefold.news.taxonomy_release_evidence.v4"
    # The point delta really is negative on that axis, and the evidence still says so.
    assert evidence["delta"]["assertion_status_accuracy"] < 0
    assert evidence["regressed_axes"] == ["assertion_status_accuracy"]
    # Its interval reaches zero, so the axis is not called a regression.
    slip = evidence["axis_interval_95"]["assertion_status_accuracy"]
    assert slip["n"] == 40
    assert slip["delta"] == pytest.approx(-1 / 40)
    assert slip["lower"] < 0 <= slip["upper"]
    assert evidence["interval_regressed_axes"] == []
    # Twelve cards become fully correct and one stops being so, so the diagnostic exact rate is above
    # zero; the partial score that decides agrees, which is the ordinary case where the two readings do.
    exact = evidence["axis_interval_95"]["four_axis_exact_accuracy"]
    assert exact["delta"] == pytest.approx(11 / 40)
    assert exact["lower"] > 0
    assert evidence["four_axis_exact_improved"] is True
    assert evidence["axis_interval_95"]["taxonomy_overall"]["lower"] > 0
    assert evidence["taxonomy_overall_improved"] is True
    assert candidate_evaluator_module._taxonomy_only_release_codes(evidence, stage="holdout") == ((), ())


def test_a_holdout_that_only_moves_the_joint_exact_rate_is_unknown() -> None:
    """#651 §8: the joint exact rate is published, and it is not what admits a candidate.

    This is candidate `5c559c44…` in miniature. Ten cards of forty that Stable got all but one axis right
    on become fully correct, ten cards neither arm classifies correctly lose a second axis, and the
    classification partial score nets those two movements to exactly zero. Ten more correctly classified
    cards is a real reader gain and the exact rate reports it — but the corpus cannot tell the candidate's
    *classification quality* from Stable's, and UNKNOWN is what that means. #626 admitted this candidate
    on the exact rate alone; the cost was that the same axis movement decided twice, once on its own axis
    and once jointly, which is also how a candidate that improved four axes could be blocked.
    """

    evidence = _taxonomy_evidence(
        [_candidate_completes_a_card() for _ in range(10)]
        + [_candidate_worsens_an_already_wrong_card() for _ in range(10)]
        + [_exact_pair() for _ in range(20)]
    )

    overall = evidence["axis_interval_95"]["taxonomy_overall"]
    assert overall["delta"] == pytest.approx(0.0)
    assert overall["lower"] < 0 <= overall["upper"]
    assert evidence["taxonomy_overall_improved"] is False
    exact = evidence["axis_interval_95"]["four_axis_exact_accuracy"]
    assert exact["delta"] == pytest.approx(10 / 40)
    assert exact["lower"] > 0
    assert evidence["four_axis_exact_improved"] is True
    # `assertion_status` both gained and lost ten cards, so no axis regressed and nothing fails.
    assert evidence["axis_interval_95"]["assertion_status_accuracy"]["delta"] == pytest.approx(0.0)
    assert evidence["interval_regressed_axes"] == []
    assert candidate_evaluator_module._taxonomy_only_release_codes(evidence, stage="holdout") == (
        ("taxonomy_partial_score_not_improved",),
        (),
    )


def test_a_partial_score_gain_admits_a_candidate_no_card_is_yet_fully_correct_under() -> None:
    """#651 §8: the classification partial score is the ruler, and a real axis repair is an improvement.

    Ten cards of forty are wrong on two axes and the candidate fixes one of them. The partial score rises
    by a quarter of those ten with its whole interval above zero, while every one of those cards is still
    misclassified so the joint exact rate does not move at all. Under #626 that was UNKNOWN; it is a
    genuine classification gain on a corpus of cards that need two repairs, and refusing it meant no
    candidate could ever take the first of the two steps.
    """

    evidence = _taxonomy_evidence(
        [(_axes(event_family="other", change_state="effective"), _axes(event_family="other")) for _ in range(10)]
        + [_exact_pair() for _ in range(30)]
    )

    overall = evidence["axis_interval_95"]["taxonomy_overall"]
    assert overall["delta"] == pytest.approx(10 * 0.25 / 40)
    assert overall["lower"] > 0
    assert evidence["taxonomy_overall_improved"] is True
    assert evidence["axis_interval_95"]["four_axis_exact_accuracy"] == {
        "delta": 0.0,
        "lower": 0.0,
        "upper": 0.0,
        "n": 40,
    }
    assert evidence["four_axis_exact_improved"] is False
    assert evidence["interval_regressed_axes"] == []
    assert candidate_evaluator_module._taxonomy_only_release_codes(evidence, stage="holdout") == ((), ())


def test_an_axis_whose_whole_interval_is_below_zero_is_a_taxonomy_axis_regression() -> None:
    """#567: a real regression is one the corpus can separate from zero, and this one fails closed."""

    evidence = _taxonomy_evidence(
        [(_axes(), _axes(change_state="effective")) for _ in range(40)],
    )

    interval = evidence["axis_interval_95"]["change_state_accuracy"]
    assert interval["delta"] == -1.0 and interval["upper"] < 0
    # #651 §8: the joint exact rate moved with `change_state` and is no longer one of the axes read.
    assert evidence["interval_regressed_axes"] == ["change_state_accuracy"]
    assert evidence["axis_interval_95"]["four_axis_exact_accuracy"]["upper"] < 0
    blockers, failures = candidate_evaluator_module._taxonomy_only_release_codes(evidence, stage="holdout")
    assert failures == ("candidate_taxonomy_axis_regression",)
    assert blockers == ("taxonomy_partial_score_not_improved",)


def test_a_partial_score_interval_that_crosses_zero_leaves_the_holdout_unknown() -> None:
    """#567, #651 §8: one card gained and one lost in forty is not an improvement, and not a FAIL."""

    evidence = _taxonomy_evidence(
        [_candidate_fixes_family(), _candidate_breaks_assertion()] + [_exact_pair() for _ in range(38)],
    )

    exact = evidence["axis_interval_95"]["four_axis_exact_accuracy"]
    assert exact["delta"] == pytest.approx(0.0)
    assert exact["lower"] < 0 <= exact["upper"]
    assert evidence["four_axis_exact_improved"] is False
    assert evidence["taxonomy_overall_improved"] is False
    assert evidence["interval_regressed_axes"] == []
    assert candidate_evaluator_module._taxonomy_only_release_codes(evidence, stage="holdout") == (
        ("taxonomy_partial_score_not_improved",),
        (),
    )


def test_a_taxonomy_only_holdout_keeps_its_empty_gold_and_cluster_floor_blockers() -> None:
    """#548's two UNKNOWN blockers survive #567 and #651 §8: the interval decides quality, not adequacy."""

    codes = candidate_evaluator_module._taxonomy_only_release_codes
    floor = int(_PROFILE["validation"]["primary_clusters_min"])

    assert codes(_taxonomy_evidence([_candidate_fixes_family() for _ in range(floor)]), stage="holdout") == ((), ())
    assert codes(_taxonomy_evidence([_candidate_fixes_family() for _ in range(floor - 1)]), stage="holdout") == (
        ("validation_primary_review_insufficient",),
        (),
    )
    assert codes(_taxonomy_evidence([]), stage="holdout") == (
        ("taxonomy_release_evidence_empty", "taxonomy_partial_score_not_improved"),
        (),
    )
    # The 30-cluster floor is the validation profile's, so the offline screen does not read it.
    assert codes(_taxonomy_evidence([_candidate_fixes_family()]), stage="offline") == ((), ())


def test_the_token_guardrail_admits_a_fifth_more_prompt_and_still_refuses_a_third() -> None:
    """#567: prompt length stopped being a proxy for spend, so the cap it enforces moved to 25 %.

    Candidate `3f7d1e12…` grew mean total tokens 19.75 % while its physical call count fell 0.6 %, its p95
    latency did not move and ~94 % of its task tokens were prompt-cache hits on a local model. The two
    guardrails that bill — calls and provider cost — stay at 10 % and are what refuse a candidate that
    actually costs more.
    """

    regressed = candidate_evaluator_module._mean_regressed
    tokens = float(_PROFILE["guardrails"]["mean_total_tokens_growth_pct"])
    assert tokens == 0.25
    assert not regressed([10_000] * 8, [12_000] * 8, growth_pct=tokens)
    assert regressed([10_000] * 8, [13_000] * 8, growth_pct=tokens)

    for guardrail in ("mean_call_growth_pct", "mean_provider_cost_growth_pct"):
        cap = float(_PROFILE["guardrails"][guardrail])
        assert cap == 0.10
        assert regressed([10_000] * 8, [12_000] * 8, growth_pct=cap)


def test_a_taxonomy_only_holdout_pass_promotes_without_canary() -> None:
    """#548: canary measures reader-facing samples this class cannot move. #651 removed shadow."""

    next_stage = candidate_evaluator_module._next_stage

    assert next_stage("holdout", "pass", taxonomy_only=True) == ("promotion", "advance")
    assert next_stage("holdout", "pass", taxonomy_only=False) == ("canary", "advance")
    # Every other transition is the one the release plane already had.
    assert next_stage("offline", "pass", taxonomy_only=True) == ("holdout", "advance")
    assert next_stage("canary", "pass", taxonomy_only=False) == ("promotion", "advance")
    assert next_stage("holdout", "fail", taxonomy_only=True) == ("none", "reject")
    assert next_stage("canary", "fail", taxonomy_only=False) == ("none", "rollback")
    assert next_stage("holdout", "unknown", taxonomy_only=True) == ("none", "hold")
