"""#150: each mode names the route it ran, and no mode may borrow another's evidence.

The first baseline had one `live` mode whose receipt did not say which graph answered. That mattered
because the two live graphs disagree by construction: `compile_live` is the optimizer's object — one task
endpoint, no fallback, no retry, no deadline, no breaker — while `runtime_live` is the shipped Program
route. A failure rate measured on the first is not the reader's, and a score measured on the second is not
the one GEPA maximizes. These tests hold the two apart with the same scripted failure.
"""

from __future__ import annotations

import json
from typing import Any

import dspy
import pytest

from tracefold.news.agents.program_baseline import BaselineCase, compile_program_factory, run_baseline
from tracefold.news.agents.program_metric import DevelopmentEpisode
from tracefold.news.agents.semantic_program import (
    DspyNewsSemanticProgram,
    PredictorAdapterError,
    PredictorResponse,
    ProgramArtifact,
    ScriptedPredictorAdapter,
    load_stable_program_artifact,
)
from tracefold.news.artifact_identity import canonical_sha
from tracefold.news.semantic_contract import TriageContext
from tracefold.news.triage_rules import DEFAULT_POLICY

_SEMANTICS: dict[str, Any] = {
    "novelty": "new_fact",
    "restates": -1,
    "event_type": "product",
    "assets": [{"symbol": "TSLA", "market_type": "spot", "role": "primary"}],
    "magnitude": 2,
    "direction": "bullish",
    "actionable": True,
    "audience": "us_equity",
    "scope": "single_name",
    "decision": "push",
    "confidence": 0.9,
}
_CARD: dict[str, Any] = {"headline_zh": "特斯拉承诺新增产线", "why_zh": "新增产能改变该名字的交付预期"}


def _context(index: int, *, title: str | None = None) -> TriageContext:
    headline = title or f"Tesla commits production line {index}"
    return TriageContext.from_card(
        {
            "event_id": f"{index:064x}",
            "evidence_version": 1,
            "evidence_sha256": "a" * 64,
            "focus_fact_id": f"{index:064x}",
            "leader_title": headline,
            "leader_description": "",
            "leader_url": "https://example.invalid/1",
            "reporting_origin": "wire",
            "family": "general",
            "admission": "candidate",
            "priority": "normal",
            "asset_class": "equity_or_commodity",
            "engine_type": "news",
            "ingest_mode": "live",
            "storyline_key": "asset:TSLA",
            "comparison_title": headline.lower(),
            "raw_first_line": headline,
            "grounded_assets": ["TSLA"],
            "watchlist_hits": [],
            "member_count": 1,
            "opened_at_ms": 1787000000000 + index * 1000,
            "expires_at_ms": 1787043200000,
            "last_member_at_ms": 1787000000000 + index * 1000,
            "macro_lexicon": False,
            "provenance": ["1018"],
            "trace_id": "t" * 32,
            "leader_item_id": f"{index:064x}",
            "provider_metadata": {},
        },
        watchlist=(),
        told_rows=[],
        now_ms=1787000000000 + index * 1000,
        queue_lag_ms=0,
    )


def _case(index: int, *, title: str | None = None) -> BaselineCase:
    values = DEFAULT_POLICY.as_dict()
    episode = DevelopmentEpisode(
        case_id=f"{index:064x}",
        cluster_id=f"{index:064x}",
        stratum="delivered",
        context=_context(index, title=title),
        accepted_review={
            "should_push": "should_push",
            "dimensions": {"factual_fidelity": "pass"},
            "novelty": {"judgment": "new_fact", "duplicate_of": ""},
        },
        production_verdict={**_SEMANTICS, **_CARD, "title_zh": ""},
        policy_metric={
            "gate": {"grounded_assets": ["TSLA"], "priority": "normal", "admission": "candidate"},
            "storyline": {"title": "Tesla", "family": "general"},
            "seen": [],
            "policy_version": "news_triage_policy_v8",
            "policy_values": values,
            "policy_sha256": canonical_sha(values),
        },
    )
    return BaselineCase(episode=episode, recorded_action="")


def _artifact_with_execution(**updates: Any) -> ProgramArtifact:
    base = load_stable_program_artifact()
    artifact = base.model_copy(update={"execution": base.execution.model_copy(update=updates)})
    return artifact.model_copy(update={"program_sha256": artifact.computed_sha256()})


def _runtime(cases: list[BaselineCase], program: DspyNewsSemanticProgram, **kwargs: Any) -> Any:
    artifact = kwargs.pop("artifact", None) or load_stable_program_artifact()
    return run_baseline(cases, mode="runtime_live", artifact=artifact, semantic_judge=program, **kwargs)


class _ScriptedLM(dspy.BaseLM):  # type: ignore[misc]
    """One task endpoint for `compile_live`, with no route around a bad answer."""

    def __init__(self, *, break_card: bool) -> None:
        super().__init__(model="scripted/compile")
        self.cache = False
        self.num_retries = 0
        self.kwargs = {"temperature": 0, "max_tokens": 4096}
        self.calls: list[str] = []
        self._break_card = break_card

    def __call__(self, prompt: Any = None, messages: Any = None, **kwargs: Any) -> list[str]:
        text = json.dumps(prompt if isinstance(prompt, str) else messages)
        self.calls.append(text)
        if "semantics_json" in text:
            return [json.dumps({"card": {"nonsense": True} if self._break_card else _CARD})]
        return [json.dumps({"semantics": _SEMANTICS})]


def test_compile_live_fails_where_runtime_live_answers_through_fallback() -> None:
    """The same broken ReaderCard answer: fatal to the optimizer's graph, survivable on the reader's route.

    This is the whole reason the two modes have separate names. `compile_live` has no second endpoint, so
    the case is unanswered and — with no answered case left — the run refuses rather than publishing a 0.0
    that reads like a measured score. Production restarts the graph on the fallback route and ships a card,
    which is what the reader actually experiences.
    """

    case = _case(1)

    with pytest.raises(ValueError, match="news_program_baseline_all_cases_failed:1/1"):
        run_baseline(
            [case],
            mode="compile_live",
            artifact=load_stable_program_artifact(),
            program_factory=compile_program_factory,
            lm=_ScriptedLM(break_card=True),
        )

    program = DspyNewsSemanticProgram(
        load_stable_program_artifact(),
        primary_adapter=ScriptedPredictorAdapter([_SEMANTICS, {"nonsense": True}, {"nonsense": True}]),
        fallback_adapter=ScriptedPredictorAdapter([_SEMANTICS, _CARD]),
    )
    report = _runtime([case], program)

    assert report.population == {"requested_n": 1, "answered_n": 1, "failure_n": 0, "failure_rate": 0.0}
    assert report.route["answered_by"] == {"fallback": 1}
    assert report.cases[0].route == "fallback"


def test_each_mode_publishes_the_route_facts_only_it_can_know() -> None:
    """A `compile_live` receipt must not grow an `answered_by` table it never had a second route for."""

    case = _case(1)
    compiled = run_baseline(
        [case],
        mode="compile_live",
        artifact=load_stable_program_artifact(),
        program_factory=compile_program_factory,
        lm=_ScriptedLM(break_card=False),
    )
    assert compiled.route == {}
    assert compiled.latency_ms.keys() == {"wall_ms", "per_case_mean_ms", "num_threads"}
    assert "no fallback route" in compiled.execution_scope

    program = DspyNewsSemanticProgram(
        load_stable_program_artifact(),
        primary_adapter=ScriptedPredictorAdapter([_SEMANTICS, _CARD]),
    )
    runtime = _runtime([case], program)
    assert runtime.route["answered_by"] == {"primary": 1}
    assert runtime.latency_ms.keys() == {"wall_ms", "p50", "p95", "max", "num_threads"}
    assert any("excludes:" in line for line in runtime.execution_scope), (
        "the runtime mode is the Program route, not the consumer — it must say what it still does not cover"
    )
    assert compiled.identity["case_root_sha256"] == runtime.identity["case_root_sha256"]


def test_runtime_live_consults_the_dedicated_reader_card_endpoint() -> None:
    """The ReaderCard slot is its own model binding, and the baseline must exercise it rather than scoring
    a card the EventSemantics endpoint happened to write."""

    semantics_adapter = ScriptedPredictorAdapter([_SEMANTICS], model_name="semantics-only")
    reader_adapter = ScriptedPredictorAdapter([_CARD], model_name="reader-only")
    program = DspyNewsSemanticProgram(
        load_stable_program_artifact(),
        primary_adapter={
            "event_semantics.primary": semantics_adapter,
            "reader_card.primary": reader_adapter,
        },
    )

    report = _runtime([_case(1)], program)

    assert [request.predictor for request in semantics_adapter.requests] == ["event_semantics"]
    assert [request.predictor for request in reader_adapter.requests] == ["reader_card"]
    assert reader_adapter.requests[0].model_binding == "reader_card.primary"
    # The scored card is the one the reader endpoint wrote.
    assert report.cases[0].score > 0
    assert report.route["physical_call_count"] == 2


def test_a_normal_runtime_case_is_exactly_two_physical_calls() -> None:
    program = DspyNewsSemanticProgram(
        load_stable_program_artifact(),
        primary_adapter=ScriptedPredictorAdapter(
            [
                PredictorResponse(
                    output=_SEMANTICS, input_tokens=10, output_tokens=5, total_tokens=15, provider_cost_microusd=17
                ),
                PredictorResponse(
                    output=_CARD, input_tokens=12, output_tokens=6, total_tokens=18, provider_cost_microusd=19
                ),
            ]
        ),
    )
    report = _runtime([_case(1)], program)

    assert report.route["call_count"] == report.route["physical_call_count"] == 2
    assert report.route["total_tokens"] == 33
    assert report.route["provider_cost_microusd_known"] == 36
    assert report.route["cost_unknown_n"] == 0
    assert report.cases[0].physical_calls == 2


def test_one_fast_retry_stays_inside_the_three_call_route_ceiling() -> None:
    """`max_calls_per_route` is 3: two calls plus the single shared fast retry."""

    primary = ScriptedPredictorAdapter([_SEMANTICS, {"nonsense": True}, _CARD])
    program = DspyNewsSemanticProgram(load_stable_program_artifact(), primary_adapter=primary)
    report = _runtime([_case(1)], program)

    assert report.route["answered_by"] == {"primary": 1}
    assert report.route["physical_call_count"] == 3 == load_stable_program_artifact().execution.max_calls_per_route
    assert len(primary.requests) == 3


def test_an_exhausted_chain_is_published_as_a_failure_not_as_a_low_score() -> None:
    """Six physical calls is the whole chain budget. A case that spends it answered nothing, and a baseline
    that scored it 0 would be indistinguishable from a card the reader disliked."""

    execution = load_stable_program_artifact().execution
    primary = ScriptedPredictorAdapter([_SEMANTICS, {"nonsense": True}, {"nonsense": True}])
    fallback = ScriptedPredictorAdapter([_SEMANTICS, {"nonsense": True}, {"nonsense": True}])
    program = DspyNewsSemanticProgram(
        load_stable_program_artifact(), primary_adapter=primary, fallback_adapter=fallback
    )

    with pytest.raises(ValueError, match="news_program_baseline_all_cases_failed:1/1"):
        _runtime([_case(1)], program)

    assert len(primary.requests) + len(fallback.requests) == 6 == execution.max_calls_per_chain


def test_runtime_failures_keep_their_own_error_code_beside_a_real_score() -> None:
    mixed = _runtime(
        [_case(1), _case(2)],
        DspyNewsSemanticProgram(
            load_stable_program_artifact(),
            primary_adapter=ScriptedPredictorAdapter(
                [
                    _SEMANTICS,
                    _CARD,
                    PredictorAdapterError("provider_busy", retryable=True),
                    PredictorAdapterError("provider_busy", retryable=True),
                ]
            ),
        ),
    )
    assert mixed.population == {
        "requested_n": 2,
        "answered_n": 1,
        "failure_n": 1,
        "failure_rate": pytest.approx(0.5),
    }
    assert mixed.failures["by_code"] == {"provider_busy": 1}


def test_runtime_cases_run_sequentially_in_one_deterministic_order() -> None:
    """Order is `(opened_at_ms, case_id)`, not input order, and never concurrent.

    The primary breaker is per-Program state: with concurrent cases, "was the breaker open when case N
    ran?" would depend on the event loop's scheduling rather than on the run, and two runs over the same
    corpus could publish different route mixes.
    """

    cases = [_case(index, title=f"Tesla line {index}") for index in (3, 1, 2)]
    primary = ScriptedPredictorAdapter([_SEMANTICS, _CARD] * 3)
    program = DspyNewsSemanticProgram(load_stable_program_artifact(), primary_adapter=primary)

    report = _runtime(cases, program)

    assert [result.case_id for result in report.cases] == [f"{index:064x}" for index in (1, 2, 3)]
    seen = [
        json.dumps(request.inputs, ensure_ascii=False)
        for request in primary.requests
        if request.predictor == "event_semantics"
    ]
    assert ["Tesla line 1" in blob for blob in seen] == [True, False, False]
    assert ["Tesla line 3" in blob for blob in seen] == [False, False, True]
    assert report.latency_ms["num_threads"] == 1


def test_the_primary_breaker_carries_across_cases_within_one_run() -> None:
    """The breaker is what makes the order matter: once transport opens it, later cases go to fallback
    without a primary attempt, and the report shows both routes rather than one."""

    artifact = _artifact_with_execution(primary_breaker_failures=1)
    primary = ScriptedPredictorAdapter(
        [
            PredictorAdapterError("provider_busy", retryable=True),
            PredictorAdapterError("provider_busy", retryable=True),
        ]
    )
    fallback = ScriptedPredictorAdapter([_SEMANTICS, _CARD] * 3)
    program = DspyNewsSemanticProgram(artifact, primary_adapter=primary, fallback_adapter=fallback)

    report = run_baseline(
        [_case(1), _case(2), _case(3)],
        mode="runtime_live",
        artifact=artifact,
        semantic_judge=program,
    )

    assert report.route["answered_by"] == {"fallback": 3}
    # The primary was tried once (two attempts) and then left alone: the breaker is real state, not a label.
    assert len(primary.requests) == 2
    assert len(fallback.requests) == 6


def test_a_runtime_receipt_names_no_endpoint_credential_or_url() -> None:
    """The report is meant to be pasted into an issue. Cost is published as a number plus an explicit
    unknown count, and the identity names model bindings — never an api_base or a key."""

    program = DspyNewsSemanticProgram(
        load_stable_program_artifact(),
        primary_adapter=ScriptedPredictorAdapter(
            [
                PredictorResponse(output=_SEMANTICS, provider_cost_microusd=None),
                PredictorResponse(output=_CARD, provider_cost_microusd=None),
            ]
        ),
    )
    report = _runtime(
        [_case(1)],
        program,
        runtime_identity={"provider": "scripted", "model": "scripted/test"},
    )

    assert report.route["cost_unknown_n"] == 1
    assert report.route["provider_cost_microusd_known"] == 0
    assert report.cases[0].provider_cost_microusd is None

    blob = json.dumps(report.model_dump(mode="json"), ensure_ascii=False)
    for secret in ("api_base", "api_key", "http://", "https://", "Bearer ", "sk-"):
        assert secret not in blob, f"the receipt leaked {secret!r}"


def test_report_sha_is_stable_across_runs_and_moves_with_the_answer() -> None:
    def run(card: dict[str, Any]) -> Any:
        return _runtime(
            [_case(1)],
            DspyNewsSemanticProgram(
                load_stable_program_artifact(), primary_adapter=ScriptedPredictorAdapter([_SEMANTICS, card])
            ),
        )

    def stable(report: Any) -> str:
        # Latency is wall-clock and cannot repeat, so it is excluded from the comparison rather than from
        # the report: an operator needs to see it, a diff must not be dominated by it.
        payload = report.model_dump(mode="json")
        payload["latency_ms"] = {}
        payload["cases"] = [{**case, "latency_ms": 0} for case in payload["cases"]]
        return canonical_sha(payload)

    first = run(_CARD)
    second = run(_CARD)
    assert stable(first) == stable(second)
    assert stable(run({**_CARD, "headline_zh": "另一种说法"})) != stable(first)
