"""#150: each mode names the route it ran, and no mode may borrow another's evidence.

The first baseline had one `live` mode whose receipt did not say which graph answered. That mattered
because the two live routes disagree by construction: `compile_live` is the optimizer's object — one task
endpoint and no fallback, while retaining the Program deadline, breaker and JSON format fallback — and
`runtime_live` is the shipped four-slot Program
route. A failure rate measured on the first is not the reader's, and a score measured on the second is not
the one GEPA maximizes. These tests hold the two apart with the same scripted failure.
"""

from __future__ import annotations

import json
from typing import Any

import dspy  # type: ignore[import-untyped]
import pytest

from tests.support.news_judgment import news_taxonomy, scored_judgment, trade_relevance
from tracefold.news.artifact_identity import canonical_sha
from tracefold.news.learning.baseline import BaselineCase, run_baseline
from tracefold.news.learning.objective import DevelopmentEpisode
from tracefold.news.models import TRIAGE_POLICY_VERSION
from tracefold.news.program.artifact import load_stable_program_artifact
from tracefold.news.program.contracts import TriageContext
from tracefold.news.program.lm import (
    AuditedConfiguredLM,
    RuntimeModelIdentity,
    ScriptedLM,
    lm_request_projection,
)
from tracefold.news.program.module import NativeNewsProgram
from tracefold.news.program.routing import RoutedSemanticJudge, RouteLMs
from tracefold.news.program.runtime import PROGRAM_PRIMARY_BREAKER_FAILURES
from tracefold.news.triage_rules import DEFAULT_POLICY

_SEMANTICS: dict[str, Any] = {
    "novelty": "new_fact",
    "restates": -1,
    "assets": [{"symbol": "TSLA", "market_type": "spot", "role": "primary"}],
    "magnitude": 2,
    "direction": "bullish",
    "audience": "us_equity",
    "scope": "single_name",
    "confidence": 0.9,
    "relevance": trade_relevance().model_dump(mode="json"),
    "taxonomy": {
        "subject_codes": ["medtop:20000205"],
        "event_family": "product_service_change",
        "change_state": "announced",
        "assertion_status": "confirmed",
    },
}
_CARD: dict[str, Any] = {"headline_zh": "特斯拉承诺新增产线", "why_zh": "新增产能改变该名字的交付预期"}

_VERDICT: dict[str, Any] = {
    **{key: value for key, value in _SEMANTICS.items() if key not in {"relevance", "taxonomy"}},
    **_CARD,
}


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
            "dedupe_family": "general",
            "admission": "candidate",
            "queue_priority": "normal",
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
            "dimensions": {"factual_fidelity": "pass", "headline_fidelity": "pass", "magnitude": "pass"},
            "novelty": {"judgment": "new_fact", "duplicate_of": ""},
            "taxonomy": dict(_SEMANTICS["taxonomy"]),
        },
        production_judgment=scored_judgment(
            _VERDICT,
            relevance=trade_relevance(),
            taxonomy=news_taxonomy(**_SEMANTICS["taxonomy"]),
        ),
        policy_metric={
            "gate": {"grounded_assets": ["TSLA"], "admission": "candidate"},
            "storyline": {"title": "Tesla", "dedupe_family": "general"},
            "seen": [],
            "policy_version": TRIAGE_POLICY_VERSION,
            "policy_values": values,
            "policy_source": "active_arm_manifest",
            "policy_sha256": canonical_sha(values),
        },
    )
    return BaselineCase(episode=episode)


def _runtime(cases: list[BaselineCase], program: RoutedSemanticJudge, **kwargs: Any) -> Any:
    artifact = kwargs.pop("artifact", None) or load_stable_program_artifact()
    return run_baseline(cases, mode="runtime_live", artifact=artifact, semantic_judge=program, **kwargs)


def _audited_lm(
    steps: list[Any],
    *,
    predictor: str,
    route: str,
    model: str | None = None,
) -> tuple[AuditedConfiguredLM, ScriptedLM]:
    artifact = load_stable_program_artifact()
    model_name = model or f"scripted/{route}-{predictor}"
    delegate = ScriptedLM(steps, model=model_name)
    lm = AuditedConfiguredLM(
        delegate,
        structured_output="json_schema",
        runtime_identity=RuntimeModelIdentity.issue(provider="scripted", model=model_name),
        predictor=predictor,
        route=route,
        model_binding=getattr(getattr(artifact, predictor).model_bindings, route),
    )
    return lm, delegate


def _route(
    *,
    route: str,
    semantics: list[Any],
    cards: list[Any],
    semantics_model: str | None = None,
    card_model: str | None = None,
) -> tuple[RouteLMs, ScriptedLM, ScriptedLM]:
    event_lm, event_delegate = _audited_lm(
        semantics,
        predictor="event_semantics",
        route=route,
        model=semantics_model,
    )
    card_lm, card_delegate = _audited_lm(
        cards,
        predictor="reader_card",
        route=route,
        model=card_model,
    )
    return RouteLMs(event_semantics=event_lm, reader_card=card_lm), event_delegate, card_delegate


def _judge(
    *,
    semantics: list[Any],
    cards: list[Any],
    fallback_semantics: list[Any] | None = None,
    fallback_cards: list[Any] | None = None,
) -> tuple[RoutedSemanticJudge, ScriptedLM, ScriptedLM, ScriptedLM | None, ScriptedLM | None]:
    artifact = load_stable_program_artifact()
    primary, primary_event, primary_card = _route(route="primary", semantics=semantics, cards=cards)
    fallback: RouteLMs | None = None
    fallback_event: ScriptedLM | None = None
    fallback_card: ScriptedLM | None = None
    if fallback_semantics is not None and fallback_cards is not None:
        fallback, fallback_event, fallback_card = _route(
            route="fallback",
            semantics=fallback_semantics,
            cards=fallback_cards,
        )
    return (
        RoutedSemanticJudge(NativeNewsProgram(artifact), primary=primary, fallback=fallback),
        primary_event,
        primary_card,
        fallback_event,
        fallback_card,
    )


def _response(
    output: dict[str, Any],
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_microusd: int | None = None,
) -> dspy.LMResponse:
    return dspy.LMResponse.from_text(
        json.dumps(output, ensure_ascii=False),
        model="scripted/actual",
        usage={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
        cost=None if cost_microusd is None else cost_microusd / 1_000_000,
    )


def _compile_program(*, break_card: bool) -> RoutedSemanticJudge:
    """One task endpoint for `compile_live`, with no route around a bad answer.

    Since #306 Phase 3 that is the whole difference between the two live modes: the same production
    `NativeNewsProgram`, bound to one route instead of two and with no fallback route.
    """

    cards: list[Any] = [{"nonsense": True}, {"nonsense": True}] if break_card else [{"card": _CARD}]
    program, *_ = _judge(semantics=[{"semantics": _SEMANTICS}], cards=cards)
    return program


def test_compile_live_fails_where_runtime_live_answers_through_fallback() -> None:
    """The same broken ReaderCard answer: fatal to the optimizer's graph, survivable on the reader's route.

    This is the whole reason the two modes have separate names. `compile_live` has no second endpoint, so
    the case is unanswered and — with no answered case left — the run refuses rather than publishing a 0.0
    that reads like a measured score. Production restarts the graph on the fallback route and ships a card,
    which is what the reader actually experiences.
    """

    case = _case(1)

    compiled = run_baseline(
        [case],
        mode="compile_live",
        artifact=load_stable_program_artifact(),
        semantic_judge=_compile_program(break_card=True),
    )
    assert compiled.population == {"requested_n": 1, "answered_n": 0, "failure_n": 1, "failure_rate": 1.0}
    assert compiled.scores["case_macro_answered"] is None

    program, *_ = _judge(
        semantics=[{"semantics": _SEMANTICS}],
        cards=[{"nonsense": True}, {"nonsense": True}],
        fallback_semantics=[{"semantics": _SEMANTICS}],
        fallback_cards=[{"card": _CARD}],
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
        semantic_judge=_compile_program(break_card=False),
    )
    # Both live modes publish route facts from the same native Module. compile_live disables the
    # cross-case production availability controls GEPA does not run.
    assert compiled.route["answered_by"] == {"primary": 1}
    assert "no fallback route" in compiled.execution_scope
    assert any("JSONAdapter" in line and "format fallback" in line for line in compiled.execution_scope)
    assert any("no whole-route deadline" in line for line in compiled.execution_scope)
    assert any("per-call timeout" in line for line in compiled.execution_scope)

    program, *_ = _judge(semantics=[{"semantics": _SEMANTICS}], cards=[{"card": _CARD}])
    runtime = _runtime([case], program)
    assert runtime.route["answered_by"] == {"primary": 1}
    assert {"wall_ms", "p50", "p95", "max", "num_threads"} <= runtime.latency_ms.keys()
    assert {"p95_with_failures", "max_with_failures"} <= runtime.latency_ms.keys()
    assert any("excludes:" in line for line in runtime.execution_scope), (
        "the runtime mode is the Program route, not the consumer — it must say what it still does not cover"
    )
    assert compiled.identity["case_root_sha256"] == runtime.identity["case_root_sha256"]


def test_runtime_live_consults_the_dedicated_reader_card_endpoint() -> None:
    """The ReaderCard slot is its own model binding, and the baseline must exercise it rather than scoring
    a card the EventSemantics endpoint happened to write."""

    artifact = load_stable_program_artifact()
    route, semantics_lm, reader_lm = _route(
        route="primary",
        semantics=[{"semantics": _SEMANTICS}],
        cards=[{"card": _CARD}],
        semantics_model="semantics-only",
        card_model="reader-only",
    )
    program = RoutedSemanticJudge(NativeNewsProgram(artifact), primary=route)

    report = _runtime([_case(1)], program)

    assert len(semantics_lm.requests) == len(reader_lm.requests) == 1
    assert route.event_semantics.model_binding == "event_semantics.primary"
    assert route.reader_card.model_binding == "reader_card.primary"
    # The scored card is the one the reader endpoint wrote.
    assert report.cases[0].score > 0
    assert report.route["physical_call_count"] == 2


def test_a_normal_runtime_case_is_exactly_two_physical_calls() -> None:
    program, *_ = _judge(
        semantics=[_response({"semantics": _SEMANTICS}, input_tokens=10, output_tokens=5, cost_microusd=17)],
        cards=[_response({"card": _CARD}, input_tokens=12, output_tokens=6, cost_microusd=19)],
    )
    report = _runtime([_case(1)], program)

    assert report.route["call_count"] == report.route["physical_call_count"] == 2
    assert report.route["total_tokens"] == 33
    assert report.route["provider_cost_microusd_known"] == 36
    assert report.route["cost_unknown_n"] == 0
    assert report.cases[0].physical_calls == 2


def test_json_adapter_fallback_stays_inside_the_four_call_route_ceiling() -> None:
    """A parse failure can spend one public JSONAdapter format fallback for that Predictor.

    The ceiling is code owned by the factory now, not a number copied into the Artifact and hashed there, so
    this asserts the executed budget directly instead of comparing it against a published field.
    """

    program, _event, card, *_ = _judge(
        semantics=[{"semantics": _SEMANTICS}],
        cards=[{"nonsense": True}, {"card": _CARD}],
    )
    report = _runtime([_case(1)], program)

    assert report.route["answered_by"] == {"primary": 1}
    assert report.route["physical_call_count"] == 3
    assert len(card.requests) == 2


def test_an_exhausted_chain_is_published_as_a_failure_not_as_a_low_score() -> None:
    """Eight physical calls is the whole chain budget. A case that spends it answered nothing, and a baseline
    that scored it 0 would be indistinguishable from a card the reader disliked."""

    program, primary_event, primary_card, fallback_event, fallback_card = _judge(
        semantics=["not-json", {"semantics": _SEMANTICS}],
        cards=[{"nonsense": True}, {"nonsense": True}],
        fallback_semantics=["not-json", {"semantics": _SEMANTICS}],
        fallback_cards=[{"nonsense": True}, {"nonsense": True}],
    )

    report = _runtime([_case(1)], program)

    assert report.population["failure_n"] == 1
    assert report.scores["case_macro_answered"] is None, "nothing answered, so there is nothing to average"
    assert report.scores["case_macro_failure_as_zero"] == 0.0
    delegates = (primary_event, primary_card, fallback_event, fallback_card)
    assert all(delegate is not None for delegate in delegates)
    assert sum(len(delegate.requests) for delegate in delegates if delegate is not None) == 8
    # Every one of those eight calls is published, not silently dropped with the case.
    assert report.route["physical_call_count"] == 8
    assert report.cases[0].physical_calls == 8


def test_runtime_failures_keep_their_own_error_code_beside_a_real_score() -> None:
    program, *_ = _judge(
        semantics=[
            {"semantics": _SEMANTICS},
            dspy.LMServerError("provider busy", code="provider_busy"),
        ],
        cards=[{"card": _CARD}],
    )
    mixed = _runtime(
        [_case(1), _case(2)],
        program,
    )
    assert mixed.population == {
        "requested_n": 2,
        "answered_n": 1,
        "failure_n": 1,
        "failure_rate": pytest.approx(0.5),
    }
    assert mixed.failures["by_code"] == {"news_program_lm_provider_busy": 1}


def test_runtime_cases_run_sequentially_in_one_deterministic_order() -> None:
    """Order is `(opened_at_ms, case_id)`, not input order, and never concurrent.

    The primary breaker is per-Program state: with concurrent cases, "was the breaker open when case N
    ran?" would depend on the event loop's scheduling rather than on the run, and two runs over the same
    corpus could publish different route mixes.
    """

    cases = [_case(index, title=f"Tesla line {index}") for index in (3, 1, 2)]
    program, primary, _card, *_ = _judge(
        semantics=[{"semantics": _SEMANTICS}] * 3,
        cards=[{"card": _CARD}] * 3,
    )

    report = _runtime(cases, program)

    assert [result.case_id for result in report.cases] == [f"{index:064x}" for index in (1, 2, 3)]
    seen = [json.dumps(lm_request_projection(request), ensure_ascii=False) for request in primary.requests]
    assert ["Tesla line 1" in blob for blob in seen] == [True, False, False]
    assert ["Tesla line 3" in blob for blob in seen] == [False, False, True]
    assert report.latency_ms["num_threads"] == 1


def test_the_primary_breaker_carries_across_cases_within_one_run() -> None:
    """The breaker is what makes the order matter: once the primary route opens it, later cases go to fallback
    without a primary attempt, and the report shows both routes rather than one.

    The threshold is `PROGRAM_PRIMARY_BREAKER_FAILURES`, code owned by the factory. It used to be an Artifact
    field a test could lower to 1, so this drives the shipped number instead: N failing cases open it, and the
    case after that never reaches the primary LM at all.
    """

    assert PROGRAM_PRIMARY_BREAKER_FAILURES == 3
    artifact = load_stable_program_artifact()
    cases = [_case(index) for index in range(1, PROGRAM_PRIMARY_BREAKER_FAILURES + 2)]
    primary_route, primary, _primary_card = _route(
        route="primary",
        semantics=[
            dspy.LMServerError("provider busy", code="provider_busy") for _ in range(PROGRAM_PRIMARY_BREAKER_FAILURES)
        ],
        cards=[],
    )
    fallback_route, fallback, fallback_card = _route(
        route="fallback",
        semantics=[{"semantics": _SEMANTICS}] * len(cases),
        cards=[{"card": _CARD}] * len(cases),
    )
    program = RoutedSemanticJudge(
        NativeNewsProgram(artifact),
        primary=primary_route,
        fallback=fallback_route,
    )

    report = run_baseline(cases, mode="runtime_live", artifact=artifact, semantic_judge=program)

    assert report.route["answered_by"] == {"fallback": len(cases)}
    # The primary was tried for the first three cases and then left alone: the breaker is real state, not a
    # label. The fourth case spends nothing on a primary route that is already known to be down.
    assert len(primary.requests) == PROGRAM_PRIMARY_BREAKER_FAILURES
    assert len(fallback.requests) == len(fallback_card.requests) == len(cases)


def test_a_runtime_receipt_names_no_endpoint_credential_or_url() -> None:
    """The report is meant to be pasted into an issue. Cost is published as a number plus an explicit
    unknown count, and the identity names model bindings — never an api_base or a key."""

    program, *_ = _judge(
        semantics=[_response({"semantics": _SEMANTICS})],
        cards=[_response({"card": _CARD})],
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
        program, *_ = _judge(semantics=[{"semantics": _SEMANTICS}], cards=[{"card": card}])
        return _runtime([_case(1)], program)

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


def test_a_live_report_names_the_policy_it_replayed_and_moves_with_it() -> None:
    """`recorded` names no policy because it replays none. A live mode does replay one, so it must name that
    exact policy — and a knob change must move the address, or two receipts are indistinguishable."""

    def run(case: BaselineCase) -> Any:
        program, *_ = _judge(semantics=[{"semantics": _SEMANTICS}], cards=[{"card": _CARD}])
        return _runtime([case], program)

    base = run(_case(1))
    assert base.identity["policy_sha256"] == canonical_sha(DEFAULT_POLICY.as_dict())
    # `active_arm_manifest`, not the arm each retired episode ran — the receipt says which question was asked
    # instead of letting a verified hash imply the other one.
    assert base.identity["policy_source"] == "active_arm_manifest"

    values = {**DEFAULT_POLICY.as_dict(), "similarity_max": 0.9}
    drifted = _case(1)
    projection = {**drifted.episode.policy_metric, "policy_values": values, "policy_sha256": canonical_sha(values)}
    other = run(BaselineCase(episode=drifted.episode.model_copy(update={"policy_metric": projection})))

    assert other.identity["policy_sha256"] == canonical_sha(values)
    assert other.report_sha256 != base.report_sha256


def test_the_published_address_is_the_measurement_and_not_the_stopwatch() -> None:
    """Two live runs with byte-identical predictions must publish the same `report_sha256`.

    Hashing `latency_ms` made the address a per-run nonce in exactly the two modes that have real latency, so
    the one question a content address exists to answer — "is this the same measurement?" — was the one it
    could not. Timings are still published, and still addressable, under `latency_sha256`.
    """

    def run() -> Any:
        program, *_ = _judge(semantics=[{"semantics": _SEMANTICS}], cards=[{"card": _CARD}])
        return _runtime([_case(1)], program)

    first, second = run(), run()
    assert first.latency_ms["wall_ms"] >= 0 and second.latency_ms["wall_ms"] >= 0
    assert first.report_sha256 == second.report_sha256
    # ...and the timings themselves are still content-addressed, just separately.
    assert first.latency_sha256 and second.latency_sha256


def test_prediction_dimensions_follow_the_candidate_while_labels_do_not() -> None:
    """The central claim of the v2 report, checked where the candidate can actually differ.

    In `recorded` mode the prediction *is* the stored verdict, so a recorded-only test can only assert that
    the two tables agree — which is what the first version of this check did, pinning byte-identity under a
    docstring describing the opposite. Here two scripted candidates answer the same case differently.
    """

    def run(card: dict[str, Any]) -> Any:
        program, *_ = _judge(semantics=[{"semantics": _SEMANTICS}], cards=[{"card": card}])
        return _runtime([_case(1)], program)

    kept = run(_CARD)
    changed = run({"headline_zh": "完全不同的另一条新闻", "why_zh": "与原卡片毫无关系的理由"})

    assert kept.review_label_distribution == changed.review_label_distribution, (
        "the corpus did not change, so what reviewers labelled cannot change"
    )
    assert kept.prediction_dimensions != changed.prediction_dimensions
    assert kept.prediction_dimensions["headline_fidelity"]["retention_hit"] == 1
    assert changed.prediction_dimensions["headline_fidelity"].get("retention_hit", 0) == 0


@pytest.mark.parametrize(
    ("damage", "code"),
    [
        ("missing", "news_program_metric_policy_values_missing"),
        ("tampered", "news_program_metric_policy_sha256_mismatch"),
    ],
)
def test_an_unusable_policy_is_refused_before_the_first_provider_call(damage: str, code: str) -> None:
    """A corpus that cannot verify its own policy costs nothing to reject.

    The predecessor discovered it inside the metric — after two Predictor calls per case — and filed it as an
    ordinary failed case, so "the policy is unverifiable" was published as "the route did not answer". Both
    inputs are pure functions of `cases`, so the check belongs before the first request.
    """

    projection = dict(_case(1).episode.policy_metric)
    if damage == "missing":
        projection.pop("policy_values")
        projection.pop("policy_sha256")
    else:
        projection["policy_values"] = {**projection["policy_values"], "similarity_max": 0.9}
    broken = BaselineCase(episode=_case(1).episode.model_copy(update={"policy_metric": projection}))

    program, primary, card, *_ = _judge(
        semantics=[{"semantics": _SEMANTICS}],
        cards=[{"card": _CARD}],
    )
    with pytest.raises(ValueError, match=f"news_program_baseline_policy_unusable:.*{code}"):
        _runtime([broken], program)
    assert primary.requests == card.requests == [], "the corpus was rejected before anything was spent on it"


def test_the_report_address_covers_the_corpus_content_not_only_its_ids() -> None:
    """Two runs over the same case ids and different evidence must not share one address."""

    program, *_ = _judge(semantics=[{"semantics": _SEMANTICS}], cards=[{"card": _CARD}])
    base = _runtime([_case(1)], program)

    edited = _case(1)
    context = edited.episode.context.model_copy(update={"queue_lag_ms": edited.episode.context.queue_lag_ms + 9_000})
    other = _runtime(
        [BaselineCase(episode=edited.episode.model_copy(update={"context": context}))],
        _judge(semantics=[{"semantics": _SEMANTICS}], cards=[{"card": _CARD}])[0],
    )

    assert base.identity["case_root_sha256"] == other.identity["case_root_sha256"], "same cases"
    assert base.identity["corpus_sha256"] != other.identity["corpus_sha256"], "different inputs"
    assert base.report_sha256 != other.report_sha256


def test_a_policy_without_a_version_is_refused_like_any_other_unusable_policy() -> None:
    """The receipt names a `policy_version`. Scoring without one publishes provenance the example never had."""

    projection = dict(_case(1).episode.policy_metric)
    projection.pop("policy_version")
    case = BaselineCase(episode=_case(1).episode.model_copy(update={"policy_metric": projection}))
    program, *_ = _judge(semantics=[{"semantics": _SEMANTICS}], cards=[{"card": _CARD}])
    with pytest.raises(ValueError, match="news_program_metric_policy_version_missing"):
        _runtime([case], program)


def test_the_route_publishes_its_retries_and_both_latency_populations() -> None:
    """A retry is spend and a failure is the slowest case there is; the receipt has to say both."""

    retried, *_ = _judge(
        semantics=[{"semantics": _SEMANTICS}],
        cards=[{"nonsense": True}, {"card": _CARD}],
    )
    report = _runtime([_case(1)], retried)
    assert report.route["retry_count"] == 1
    assert report.route["physical_call_count"] == 3

    clean, *_ = _judge(semantics=[{"semantics": _SEMANTICS}], cards=[{"card": _CARD}])
    quiet = _runtime([_case(2)], clean)
    assert quiet.route["retry_count"] == 0
    # p50/p95/max cover answered cases, as the spec asks; the failure tail is published beside them.
    assert "answered cases" in quiet.latency_ms["population"]
    assert quiet.latency_ms["max_with_failures"] >= quiet.latency_ms["max"]


def test_the_runtime_scope_names_the_told_context_it_replayed() -> None:
    """It feeds each case the ToldContext frozen at production time, not a ledger rebuilt from this run's own
    outputs. Without that line a reader may take the mode for a continuous production simulation."""

    program, *_ = _judge(semantics=[{"semantics": _SEMANTICS}], cards=[{"card": _CARD}])
    scope = _runtime([_case(1)], program).execution_scope
    assert any("frozen production ToldContext" in line for line in scope)
    assert any("no arm-local ledger replay" in line for line in scope)
