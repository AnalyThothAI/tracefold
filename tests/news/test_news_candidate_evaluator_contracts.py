from __future__ import annotations

import hashlib
import json

import pytest

import tracefold.news.learning.evaluate as candidate_evaluator_module
from tracefold.news.learning.evaluate import ArmManifest, development_coverage_blockers
from tracefold.news.learning.profile import _PROFILE
from tracefold.news.models import TriageVerdict
from tracefold.news.program.contracts import EditorialEnvelope, ScoredJudgment, TradeRelevanceV1
from tracefold.news.program.runtime import PROGRAM_FACTORY_ID
from tracefold.news.triage_rules import DEFAULT_POLICY


def test_arm_manifest_identity_is_program_native() -> None:
    policy = DEFAULT_POLICY.as_dict()
    arm = ArmManifest(
        program_version="news_semantic_program_v5",
        program_sha256="a" * 64,
        runtime_model_bindings_sha256="c" * 64,
        retrieval_sha256="b" * 64,
        policy=policy,
        policy_sha256=_sha(policy),
    )

    assert arm.bundle_sha == _sha(arm.model_dump(mode="json"))
    assert set(arm.model_dump()) == {
        "program_version",
        "program_sha256",
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
    # The trace-level identity a physical call must carry is the factory id: it is now the whole of the
    # code-owned surface, so an observation produced by any other factory cannot be scored against this one.
    assert candidate_evaluator_module._program_call_provenance_complete(
        {
            "trace": {"factory_id": PROGRAM_FACTORY_ID},
            "calls": [call],
            "usage": {"physical_call_count": 1},
        }
    )
    assert not candidate_evaluator_module._program_call_provenance_complete(
        {
            "trace": {"factory_id": "tracefold.news.program.factory_v5"},
            "calls": [call],
            "usage": {"physical_call_count": 1},
        }
    )
    assert not candidate_evaluator_module._program_call_provenance_complete(
        {
            "calls": [{key: value for key, value in call.items() if key != "runtime_binding_sha256"}],
            "trace": {"factory_id": PROGRAM_FACTORY_ID},
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
    trace = {"factory_id": PROGRAM_FACTORY_ID, "calls": [synthetic, fallback_semantics, fallback_card]}
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


def test_observed_degraded_program_keeps_unselected_failed_execution_audit() -> None:
    context = {"event_id": "event-degraded", "phase": "stale_reask"}
    context_sha = _sha(context)
    failed_call = {
        "predictor": "event_semantics",
        "route": "primary",
        "attempt": 1,
        "physical_provider_call": True,
        "error_code": "provider_unavailable",
    }
    execution = {
        "execution_index": 0,
        "phase": "stale_reask",
        "status": "failed",
        "context_sha256": context_sha,
        "context": context,
        "trace": {"context_sha256": context_sha, "calls": [failed_call]},
        "usage": {"call_count": 1, "physical_call_count": 1},
        "recording_call_indices": [0],
    }
    row = {
        **_observed_judgment_fields(_verdict(), origin="degraded_unavailable"),
        "degraded": True,
        "verdict_error_code": "provider_unavailable",
        "trace": {
            "program_executions": [execution],
            "model_attempts": 1,
            "physical_model_attempts": 1,
        },
    }

    observed = candidate_evaluator_module._observed_production_output(row)

    program = observed["program"][0]
    assert program["trace"] == {}
    assert program["executions"] == [execution]
    assert program["usage"]["call_count"] == 1
    assert program["usage"]["physical_call_count"] == 1
    assert program["calls"] == [
        {
            **failed_call,
            "execution_index": 0,
            "execution_phase": "stale_reask",
            "execution_status": "failed",
            "execution_context_sha256": context_sha,
            "recording_call_index": 0,
        }
    ]


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


# One UTC date, 21 hours, and every count the release profile actually asks for. Before #259 this exact
# corpus was refused — for the calendar, and for nothing else.
_ONE_DAY_SUFFICIENT = {
    "case_n": 168,
    "independent_cluster_n": 141,
    "boundary_cluster_n": 34,
    "retention_cluster_n": 107,
    "negative_cluster_n": 55,
    "safety_cluster_n": 9,
    "stratum_n": 4,
    "eligible_event_n": 733,
    "natural_day_n": 1,
    "window_duration_hours": 21.0,
}


def test_a_single_calendar_day_with_real_coverage_is_not_blocked() -> None:
    """#259: `natural_day_n = 1` is a fact about midnights, not about evidence.

    The counts below are the ones the profile has always asked for and they are all met; the only thing
    separating this corpus from the pre-#259 refusal is that its 21 hours happened not to cross a UTC
    boundary. A Development Experiment that cannot start until the calendar catches up is a deployment
    delay wearing a statistics costume.
    """

    assert development_coverage_blockers(_ONE_DAY_SUFFICIENT) == ()


def test_three_calendar_dates_are_not_evidence_when_the_corpus_is_thin() -> None:
    """The mirror case, and the one that shows the old gate measured the wrong variable.

    Four minutes either side of a midnight is three UTC dates by the old count and three restatements of
    one storyline by any honest one. The refusal has to come from the clusters, and its vocabulary must
    never name a day again — an operator told to wait for the calendar would wait forever.
    """

    thin = {
        **_ONE_DAY_SUFFICIENT,
        "case_n": 6,
        "independent_cluster_n": 3,
        "boundary_cluster_n": 2,
        "retention_cluster_n": 1,
        "negative_cluster_n": 1,
        "safety_cluster_n": 1,
        "stratum_n": 1,
        "natural_day_n": 3,
        "window_duration_hours": 0.067,
    }

    blockers = development_coverage_blockers(thin)

    assert set(blockers) == {
        "development_boundary_cluster_n_insufficient",
        "development_retention_cluster_n_insufficient",
        "development_negative_cluster_n_insufficient",
        "development_stratum_n_insufficient",
    }
    assert not any("day" in blocker for blocker in blockers)


def test_raw_event_volume_is_not_an_effective_sample_size() -> None:
    """#259 §3: the unit of evidence is the connected fact cluster, not the Event row.

    A busy news day produces thousands of eligible Events and can still carry a handful of separable
    facts. `eligible_event_n` is reported so an operator can see the window's traffic; it buys nothing.
    """

    loud = {
        **_ONE_DAY_SUFFICIENT,
        "eligible_event_n": 40_000,
        "case_n": 40_000,
        "independent_cluster_n": 12,
        "boundary_cluster_n": 4,
        "retention_cluster_n": 8,
        "negative_cluster_n": 3,
        "stratum_n": 2,
    }

    assert set(development_coverage_blockers(loud)) == {
        "development_boundary_cluster_n_insufficient",
        "development_retention_cluster_n_insufficient",
        "development_negative_cluster_n_insufficient",
        "development_stratum_n_insufficient",
    }


def test_a_corpus_with_no_safety_case_is_still_refused() -> None:
    """The one non-threshold rule in the development profile, unchanged by #259."""

    assert development_coverage_blockers({**_ONE_DAY_SUFFICIENT, "safety_cluster_n": 0}) == (
        "development_safety_empty",
    )


def test_the_development_profile_admits_no_temporal_gate_at_all() -> None:
    """#259 §6: `natural_days_min` is gone and nothing age-shaped may take its place.

    Written against the key set rather than one deleted name on purpose: the failure this guards against
    is not "somebody restored `natural_days_min`", it is "somebody added `stable_age_days` and called it
    a different rule".
    """

    development = _PROFILE["development"]

    assert set(development) == {
        "boundary_clusters_min",
        "retention_clusters_min",
        "negative_clusters_min",
        "strata_min",
        "safety_required",
    }
    assert not any(
        word in key for key in development for word in ("day", "age", "hour", "duration", "window", "natural")
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
        "event_type": "macro",
        "assets": [],
        "direction": "bullish",
        "scope": "sector",
        "magnitude": 2,
        "actionable": True,
        "confidence": 0.8,
        "decision": "push",
        "audience": "us_equity",
        "headline_zh": "DRAM 合约价续涨",
        "title_zh": "",
        "why_zh": "行业价格继续改善，但持续性仍需后续数据确认。",
    }


def _observed_judgment_fields(verdict: dict[str, object], *, origin: str = "model") -> dict[str, object]:
    relevance = TradeRelevanceV1(
        impact_breadth="sector",
        tradability="direct",
        surprise="material_vs_expectation",
        development_delta="state_change",
        channels=("commodity_demand",),
        affected_markets=("us_equity_broad",),
        reader_value="realtime",
    )
    editorial = (
        EditorialEnvelope.issue(editorial_origin="model", relevance=relevance)
        if origin == "model"
        else EditorialEnvelope.issue(editorial_origin="degraded_unavailable", relevance=None)
    )
    scored = ScoredJudgment.issue(
        verdict=TriageVerdict.model_validate(verdict),
        editorial=editorial,
    )
    return {
        "verdict": verdict,
        "editorial": editorial.model_dump(mode="json"),
        "scored_judgment_sha256": scored.scored_judgment_sha256,
    }
