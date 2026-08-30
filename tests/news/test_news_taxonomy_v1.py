from __future__ import annotations

import dspy  # type: ignore[import-untyped]
import pytest
from pydantic import ValidationError

from tracefold.news.artifact_identity import canonical_sha
from tracefold.news.learning.metric import ProductionRegressionGateEvidenceV1
from tracefold.news.learning.taxonomy import (
    TaxonomyCandidateRegistrationV1,
)
from tracefold.news.learning.taxonomy_evaluation import (
    TaxonomyEvaluationContextV1,
    build_taxonomy_evaluation_report,
)
from tracefold.news.learning.taxonomy_shadow import (
    TaxonomyShadowObservationV2,
    TaxonomyShadowPopulationV1,
    TaxonomyShadowProgramV2,
)
from tracefold.news.models import TriageVerdict
from tracefold.news.program.contracts import EditorialEnvelope, ScoredJudgment, TradeRelevanceV1, TriageContext
from tracefold.news.program.lm import (
    AuditedConfiguredLM,
    LMCallLedger,
    RecordedLM,
    RuntimeModelIdentity,
    ScriptedLM,
)
from tracefold.news.review.desk import taxonomy_requires_independent_adjudication
from tracefold.news.taxonomy import (
    IPTC_CODEBOOK_SHA256,
    ModelTaxonomyV1,
    NewsTaxonomyV1,
    source_authority,
    source_authority_from_evidence,
)
from tracefold.news.triage_rules import GateFacts, decide


def _taxonomy(**updates: object) -> NewsTaxonomyV1:
    values: dict[str, object] = {
        "subject_codes": ["medtop:20001279"],
        "event_family": "market_access",
        "change_state": "effective",
        "assertion_status": "confirmed",
        "source_authority": "reputable_secondary",
    }
    values.update(updates)
    return NewsTaxonomyV1.model_validate(values)


def test_taxonomy_critical_predicate_is_shared_by_review_and_evaluation() -> None:
    ordinary = _taxonomy(
        event_family="regulatory_legal",
        change_state="reported",
        assertion_status="claimed",
    )

    assert not taxonomy_requires_independent_adjudication(ordinary)
    assert taxonomy_requires_independent_adjudication(
        _taxonomy(event_family="product_service_change", assertion_status="claimed")
    )
    assert taxonomy_requires_independent_adjudication(
        _taxonomy(event_family="financial_results", assertion_status="claimed")
    )
    assert taxonomy_requires_independent_adjudication(
        ordinary,
        draft_taxonomy=_taxonomy(event_family="regulatory_legal", change_state="effective"),
    )


def _gold_receipt(case_id: str) -> dict[str, object]:
    review_id = canonical_sha({"case_id": case_id})
    return {
        "review_id": review_id,
        "acceptance_id": canonical_sha({"kind": "acceptance", "review_id": review_id}),
        "rubric_version": "news_review_v6",
        "reviewer": "taxonomy-test-reviewer",
        "accepted_at_ms": 1,
        "release_eligible": True,
    }


def _evaluation_context(*, case_n: int = 0) -> TaxonomyEvaluationContextV1:
    registration = TaxonomyCandidateRegistrationV1(
        tested_git_sha="a" * 40,
        program_version="news_semantic_program_v8",
        program_sha256="1" * 64,
        stable_bundle_sha256="9" * 64,
        runtime_manifest_sha256="7" * 64,
        image_digest="sha256:" + "8" * 64,
        deployment_receipt_sha256="0" * 64,
        envelope_sha256="2" * 64,
        metric_id="tracefold.news.production_action_trade_relevance_v6",
        metric_sha256="6" * 64,
        policy_version="news_triage_policy_v11",
        policy_sha256="c" * 64,
        runtime_model_bindings_sha256="b" * 64,
        taxonomy_program_sha256="e" * 64,
        taxonomy_model_binding_sha256="f" * 64,
        registered_at_ms=2_000_000_000_000,
    )
    return TaxonomyEvaluationContextV1(
        candidate_registration_sha256=registration.artifact_sha256,
        candidate_registration=registration,
        gold_ledger_root_sha256="d" * 64,
        regression_gates={
            name: _regression_receipt(name, registration=registration)
            for name in ("production_action", "asset_grounding", "novelty", "trade_relevance")
        },
        shadow_population=TaxonomyShadowPopulationV1(
            eligible_case_n=case_n,
            observation_n=case_n,
            success_n=case_n,
            schema_invalid_n=0,
            provider_failure_n=0,
            budget_deadline_failure_n=0,
            missing_observation_n=0,
            invalid_observation_n=0,
            physical_attempt_n=case_n,
            recorded_attempt_n=case_n,
            schema_invalid_attempt_n=0,
            provider_failure_attempt_n=0,
        ),
    )


def _regression_receipt(
    name: str,
    *,
    registration: TaxonomyCandidateRegistrationV1,
    outcome: str = "PASS",
) -> dict[str, object]:
    available = outcome != "UNKNOWN"
    gate_evidence = ProductionRegressionGateEvidenceV1.model_validate(
        {
            "gate": name,
            "metric_id": registration.metric_id,
            "metric_sha256": registration.metric_sha256,
            "denominator_n": int(available),
            "stable_failure_n": 0,
            "candidate_failure_n": 0,
            "candidate_only_regression_n": 0,
            "candidate_only_case_ids": (),
            "outcome": outcome.lower(),
        }
    )
    return {
        "gate": name,
        "outcome": outcome,
        "evidence_sha256": canonical_sha({"gate": name}),
        "gate_evidence_sha256": gate_evidence.evidence_sha256,
        "report_sha256": canonical_sha({"report": name}),
        "candidate_sha256": "4" * 64,
        "dataset_sha256": "5" * 64,
        "metric_id": registration.metric_id,
        "metric_sha256": registration.metric_sha256,
        "denominator_n": gate_evidence.denominator_n,
        "stable_failure_n": gate_evidence.stable_failure_n,
        "candidate_failure_n": gate_evidence.candidate_failure_n,
        "candidate_only_regression_n": gate_evidence.candidate_only_regression_n,
        "candidate_only_case_ids": gate_evidence.candidate_only_case_ids,
    }


def _judgment(taxonomy: NewsTaxonomyV1) -> ScoredJudgment:
    relevance = TradeRelevanceV1(
        impact_breadth="single_instrument",
        tradability="direct",
        surprise="unscheduled",
        development_delta="state_change",
        channels=("exchange_access",),
        affected_markets=("single_asset",),
        reader_value="realtime",
    )
    return ScoredJudgment.issue(
        verdict=TriageVerdict(
            novelty="new_fact",
            restates=-1,
            assets=(),
            direction="neutral",
            scope="single_name",
            magnitude=2,
            confidence=0.8,
            headline_zh="某交易所开放新市场",
            why_zh="新增市场改变可交易入口。",
            audience="crypto",
        ),
        editorial=EditorialEnvelope.issue(
            relevance=relevance,
            taxonomy=taxonomy,
        ),
    )


def _shadow_context() -> TriageContext:
    return TriageContext.from_card(
        {
            "event_id": "event-shadow",
            "evidence_version": 3,
            "evidence_sha256": "a" * 64,
            "focus_fact_id": "fact-shadow",
            "reporting_origin": "Reuters",
            "provenance": ["1018"],
            "leader_title": "Exchange opens a new spot market",
            "raw_first_line": "Trading starts tomorrow",
            "leader_description": "The exchange announced the opening.",
            "opened_at_ms": 1_000_000,
            "member_count": 1,
            "dedupe_family": "listing",
            "provider_score_max": 90,
            "provider_metadata": {},
            "queue_priority": "normal",
            "asset_class": "crypto",
            "grounded_assets": ["BTC"],
            "storyline_key": "asset:BTC",
        },
        watchlist=("BTC",),
        told_rows=(),
        now_ms=1_010_000,
        queue_lag_ms=10_000,
    )


def test_taxonomy_schema_is_exact_bounded_and_content_pinned() -> None:
    taxonomy = _taxonomy(subject_codes=["medtop:20001279", "medtop:20000385", "medtop:20001279"])

    assert taxonomy.subject_codes == ("medtop:20000385", "medtop:20001279")
    assert taxonomy.codebook_sha256 == IPTC_CODEBOOK_SHA256
    with pytest.raises(ValidationError):
        ModelTaxonomyV1(
            subject_codes=("medtop:not-real",),
            event_family="market_access",
            change_state="effective",
            assertion_status="confirmed",
        )
    with pytest.raises(ValidationError):
        _taxonomy(unplanned_axis="must fail")
    with pytest.raises(ValidationError, match="news_taxonomy_parent_child_duplicate"):
        _taxonomy(subject_codes=["medtop:04000000", "medtop:20000205"])


@pytest.mark.parametrize(
    ("sources", "expected"),
    [
        (("sec.gov",), "regulatory_filing"),
        (("@coinbase",), "issuer_first_party"),
        (("https://www.reuters.com/world",), "reputable_secondary"),
        (("Reuters fan account",), "unknown"),
        (("fan:reuters",), "unknown"),
        (("fake|sec",), "unknown"),
        (("notreuters.com",), "unknown"),
        (("https://reuters.com.evil.example/world",), "unknown"),
        (("https://reuters.com@evil.example/world",), "unknown"),
        (("https://wire.reuters.com/world",), "unknown"),
    ],
)
def test_source_authority_is_exact_code_owned_reporting_source(
    sources: tuple[str, ...],
    expected: str,
) -> None:
    assert source_authority(sources) == expected


def test_strategy_routing_ids_cannot_claim_source_authority() -> None:
    assert (
        source_authority_from_evidence(
            {
                "source": "fan account",
                "strategies": ["reuters"],
                "provenance": ["sec"],
            }
        )
        == "unknown"
    )


def test_taxonomy_has_no_delivery_authority() -> None:
    facts = GateFacts(grounded_assets=(), watchlist_symbols=frozenset(), admission="semantic")
    access = decide(_judgment(_taxonomy()), facts, None)
    rumor = decide(
        _judgment(
            _taxonomy(
                subject_codes=(),
                event_family="geopolitical_conflict",
                change_state="unknown",
                assertion_status="rumor",
                source_authority="unknown",
            )
        ),
        facts,
        None,
    )

    assert access == rumor
    assert access.final == "push"


@pytest.mark.parametrize("first_response", [None, "not-json"])
def test_shadow_program_records_and_replays_the_complete_adapter_path(first_response: str | None) -> None:
    model_identity = RuntimeModelIdentity.issue(provider="scripted", model="scripted/taxonomy-shadow")
    ledger = LMCallLedger(max_calls_per_predictor=2, max_calls_per_route=2, max_calls_per_scope=2)
    success = {
        "taxonomy": {
            "subject_codes": ["medtop:20001279"],
            "event_family": "market_access",
            "change_state": "announced",
            "assertion_status": "confirmed",
        }
    }
    delegate = ScriptedLM(
        ([first_response] if first_response is not None else []) + [success],
        model=model_identity.model,
    )
    lm = AuditedConfiguredLM(
        delegate,
        structured_output="json_schema",
        runtime_identity=model_identity,
        predictor="taxonomy_shadow",
        route="shadow",
        model_binding="taxonomy-shadow-v2",
        ledger=ledger,
    )
    program = TaxonomyShadowProgramV2(lm=lm)

    observation = program(_shadow_context())

    assert len(delegate.requests) == (2 if first_response is not None else 1)
    assert observation.release_authority is False
    assert observation.outcome == "success"
    assert observation.taxonomy.source_authority == "reputable_secondary"
    assert observation.shadow_program_sha256 == program.shadow_program_sha256
    assert len(observation.observation_sha256) == 64
    assert [attempt.terminal_disposition for attempt in observation.attempts] == (
        ["adapter_parse_error", "provider_success"] if first_response is not None else ["provider_success"]
    )
    replay_ledger = LMCallLedger(max_calls_per_predictor=2, max_calls_per_route=2, max_calls_per_scope=2)
    replay = RecordedLM(
        {attempt.request_sha256: attempt.recording for attempt in observation.attempts},
        model=model_identity.model,
        runtime_identity=model_identity,
        model_binding=observation.model_binding,
    )
    replay_program = TaxonomyShadowProgramV2(
        lm=AuditedConfiguredLM(
            replay,
            structured_output="json_schema",
            runtime_identity=model_identity,
            predictor="taxonomy_shadow",
            route="shadow",
            model_binding=observation.model_binding,
            ledger=replay_ledger,
        )
    )

    replayed = replay_program(_shadow_context())

    assert replayed.taxonomy == observation.taxonomy
    assert replayed.attempts == observation.attempts


def test_shadow_program_persists_fully_observed_schema_invalid_terminal() -> None:
    model_identity = RuntimeModelIdentity.issue(provider="scripted", model="scripted/taxonomy-shadow")
    ledger = LMCallLedger(max_calls_per_predictor=2, max_calls_per_route=2, max_calls_per_scope=2)
    delegate = ScriptedLM(["not-json", "still-not-json"], model=model_identity.model)
    program = TaxonomyShadowProgramV2(
        lm=AuditedConfiguredLM(
            delegate,
            structured_output="json_schema",
            runtime_identity=model_identity,
            predictor="taxonomy_shadow",
            route="shadow",
            model_binding="taxonomy-shadow-v2",
            ledger=ledger,
        )
    )

    observation = program(_shadow_context())

    assert observation.outcome == "schema_invalid"
    assert observation.taxonomy is None
    assert [attempt.terminal_disposition for attempt in observation.attempts] == [
        "adapter_parse_error",
        "adapter_parse_error",
    ]
    assert all(attempt.recording for attempt in observation.attempts)


def test_shadow_program_persists_provider_and_pre_call_budget_terminals() -> None:
    model_identity = RuntimeModelIdentity.issue(provider="scripted", model="scripted/taxonomy-shadow")

    def program(delegate: ScriptedLM, ledger: LMCallLedger) -> TaxonomyShadowProgramV2:
        return TaxonomyShadowProgramV2(
            lm=AuditedConfiguredLM(
                delegate,
                structured_output="json_schema",
                runtime_identity=model_identity,
                predictor="taxonomy_shadow",
                route="shadow",
                model_binding="taxonomy-shadow-v2",
                ledger=ledger,
            )
        )

    provider = program(
        ScriptedLM([dspy.LMServerError("down", code="server")], model=model_identity.model),
        LMCallLedger(max_calls_per_predictor=2, max_calls_per_route=2, max_calls_per_scope=2),
    )(_shadow_context())

    assert provider.outcome == "provider_failure"
    assert [attempt.terminal_disposition for attempt in provider.attempts] == ["provider_error"]
    with pytest.raises(ValidationError, match="news_taxonomy_shadow_success_terminal_invalid"):
        TaxonomyShadowObservationV2.model_validate(
            provider.model_dump(mode="json")
            | {
                "outcome": "success",
                "error_code": None,
                "taxonomy": _taxonomy().model_dump(mode="json"),
            }
        )

    def refuse_call() -> None:
        raise dspy.LMConfigurationError("budget exhausted", code="news_taxonomy_shadow_budget_exhausted")

    budget = program(
        ScriptedLM([], model=model_identity.model),
        LMCallLedger(
            max_calls_per_predictor=2,
            max_calls_per_route=2,
            max_calls_per_scope=2,
            before_call=refuse_call,
        ),
    )(_shadow_context())

    assert budget.outcome == "budget_deadline_failure"
    assert budget.attempts == ()

    transport_timeout = program(
        ScriptedLM(
            [dspy.LMTransportError("timed out", code="upstream_timeout")],
            model=model_identity.model,
        ),
        LMCallLedger(max_calls_per_predictor=2, max_calls_per_route=2, max_calls_per_scope=2),
    )(_shadow_context())

    assert transport_timeout.outcome == "budget_deadline_failure"
    assert [attempt.terminal_disposition for attempt in transport_timeout.attempts] == ["provider_error"]

    truncated_response = dspy.LMResponse.from_text(
        '{"taxonomy":{"assertion_status":"confirmed","change_state":"announced",'
        '"event_family":"market_access","subject_codes":["medtop:20001279"]}}',
        model=model_identity.model,
    )
    truncated_response.outputs[0] = truncated_response.output.model_copy(
        update={"finish_reason": "length", "truncated": True}
    )
    truncated = program(
        ScriptedLM([truncated_response], model=model_identity.model),
        LMCallLedger(max_calls_per_predictor=2, max_calls_per_route=2, max_calls_per_scope=2),
    )(_shadow_context())

    assert truncated.outcome == "provider_failure"
    assert [attempt.terminal_disposition for attempt in truncated.attempts] == ["provider_success"]
    assert truncated.attempts[0].error_code == "news_program_lm_output_truncated"


def test_underpowered_evaluation_is_unknown_and_clusters_provider_duplicates() -> None:
    gold = _taxonomy()
    cases = [
        {
            "case_id": "provider-a",
            "cluster_id": "fact-1",
            "event_id": "event-provider-a",
            "opened_at_ms": 1,
            "language": "en",
            "source_slice": "wire",
            "audience": "crypto",
            "gold": gold.model_dump(mode="json"),
            "prediction": gold.model_dump(mode="json"),
            "gold_receipt": _gold_receipt("provider-a"),
        },
        {
            "case_id": "provider-b",
            "cluster_id": "fact-1",
            "event_id": "event-provider-b",
            "opened_at_ms": 2,
            "language": "en",
            "source_slice": "wire",
            "audience": "crypto",
            "gold": gold.model_dump(mode="json"),
            "prediction": gold.model_dump(mode="json"),
            "gold_receipt": _gold_receipt("provider-b"),
        },
    ]

    report = build_taxonomy_evaluation_report(cases, context=_evaluation_context(case_n=len(cases)))

    assert report.case_n == 2
    assert report.scored_case_n == report.cluster_n == 1
    assert report.provider_duplicate_n == 1
    assert report.outcome == "UNKNOWN"
    assert report.readiness["ready"] is False
    assert report.quality_gates["event_family_macro_f1"]["outcome"] == "UNKNOWN"
    assert len(report.report_sha256) == 64


def test_schema_invalid_gate_uses_physical_attempts_and_missing_population_is_unknown() -> None:
    taxonomy = _taxonomy().model_dump(mode="json")
    case = {
        "case_id": "attempt-case",
        "cluster_id": "attempt-case",
        "event_id": "attempt-event",
        "opened_at_ms": 1,
        "gold": taxonomy,
        "prediction": taxonomy,
        "gold_receipt": _gold_receipt("attempt-case"),
    }
    first_invalid_then_success = TaxonomyEvaluationContextV1.model_validate(
        _evaluation_context(case_n=1).model_dump(mode="json")
        | {
            "shadow_population": {
                **_evaluation_context(case_n=1).shadow_population.model_dump(mode="json"),
                "physical_attempt_n": 2,
                "recorded_attempt_n": 2,
                "schema_invalid_attempt_n": 1,
            }
        }
    )

    observed = build_taxonomy_evaluation_report([case], context=first_invalid_then_success)

    assert observed.subject_codes["schema_cardinality_code_attempt_n"] == 2
    assert observed.subject_codes["schema_cardinality_code_invalid"] == 1
    assert observed.quality_gates["schema_cardinality_code_invalid"]["outcome"] == "FAIL"

    missing = TaxonomyEvaluationContextV1.model_validate(
        _evaluation_context().model_dump(mode="json")
        | {
            "shadow_population": {
                "eligible_case_n": 1,
                "observation_n": 0,
                "success_n": 0,
                "schema_invalid_n": 0,
                "provider_failure_n": 0,
                "budget_deadline_failure_n": 0,
                "missing_observation_n": 1,
                "invalid_observation_n": 0,
                "physical_attempt_n": 0,
                "recorded_attempt_n": 0,
                "schema_invalid_attempt_n": 0,
                "provider_failure_attempt_n": 0,
            }
        }
    )

    unknown = build_taxonomy_evaluation_report([], context=missing)

    assert unknown.quality_gates["schema_cardinality_code_invalid"]["outcome"] == "UNKNOWN"
    assert unknown.quality_gates["shadow_terminal_outcomes"]["outcome"] == "UNKNOWN"
    assert unknown.outcome == "UNKNOWN"


def test_evaluation_rejects_unknown_split_and_pregistration_holdout_stays_unknown() -> None:
    taxonomy = _taxonomy().model_dump(mode="json")
    with pytest.raises(ValueError, match="news_taxonomy_split_invalid"):
        build_taxonomy_evaluation_report(
            [{"case_id": "bad", "split": "test", "gold": taxonomy, "prediction": taxonomy}],
            context=_evaluation_context(),
        )

    report = build_taxonomy_evaluation_report(
        [
            {
                "case_id": f"holdout-{index}",
                "cluster_id": f"holdout-{index}",
                "event_id": f"event-holdout-{index}",
                "split": "future_holdout",
                "opened_at_ms": 86_400_000 + index,
                "eligible": True,
                "accepted_primary": True,
                "gold": taxonomy,
                "prediction": taxonomy,
                "gold_receipt": _gold_receipt(f"holdout-{index}"),
            }
            for index in range(200)
        ],
        context=_evaluation_context(case_n=200),
    )

    assert report.readiness["future_holdout"]["ready"] is False
    assert report.readiness["future_holdout"]["checks"]["post_registration_violation_n"]["observed"] == 200
    assert report.outcome == "UNKNOWN"


def test_evaluation_rejects_cluster_leakage_conflicting_gold_and_partial_registration() -> None:
    gold = _taxonomy().model_dump(mode="json")
    other = _taxonomy(event_family="other").model_dump(mode="json")
    common = {
        "cluster_id": "same-fact",
        "event_id": "event-same-fact",
        "opened_at_ms": 2,
        "gold": gold,
        "prediction": gold,
        "gold_receipt": _gold_receipt("common"),
    }
    with pytest.raises(ValueError, match="news_taxonomy_cluster_split_leakage"):
        build_taxonomy_evaluation_report(
            [
                {
                    **common,
                    "case_id": "development",
                    "split": "development",
                    "gold_receipt": _gold_receipt("development"),
                },
                {
                    **common,
                    "case_id": "holdout",
                    "split": "future_holdout",
                    "candidate_registered_at_ms": 2_000_000_000_000,
                    "gold_receipt": _gold_receipt("holdout"),
                },
            ],
            context=_evaluation_context(),
        )
    with pytest.raises(ValueError, match="news_taxonomy_cluster_gold_conflict"):
        build_taxonomy_evaluation_report(
            [
                {**common, "case_id": "provider-a", "gold_receipt": _gold_receipt("provider-a")},
                {
                    **common,
                    "case_id": "provider-b",
                    "gold": other,
                    "gold_receipt": _gold_receipt("provider-b"),
                },
            ],
            context=_evaluation_context(),
        )
    with pytest.raises(ValueError, match="news_taxonomy_holdout_candidate_registration_mismatch"):
        build_taxonomy_evaluation_report(
            [
                {
                    **common,
                    "case_id": "registered",
                    "cluster_id": "registered",
                    "split": "future_holdout",
                    "candidate_registered_at_ms": 2_000_000_000_000,
                    "gold_receipt": _gold_receipt("registered"),
                },
                {
                    **common,
                    "case_id": "missing",
                    "cluster_id": "missing",
                    "split": "future_holdout",
                    "candidate_registered_at_ms": 1,
                    "gold_receipt": _gold_receipt("missing"),
                },
            ],
            context=_evaluation_context(),
        )


def test_preregistered_denominators_can_produce_pass_only_with_future_holdout() -> None:
    development_families = [
        *("product_service_change" for _ in range(30)),
        *("macro_policy_data" for _ in range(30)),
        *("geopolitical_conflict" for _ in range(30)),
        *("market_flow_price" for _ in range(30)),
        *("other" for _ in range(30)),
        *("financial_results" for _ in range(15)),
        *("guidance_outlook" for _ in range(15)),
        *("corporate_transaction" for _ in range(15)),
        *("financing_capital_allocation" for _ in range(15)),
        *("leadership_governance" for _ in range(15)),
        *("regulatory_legal" for _ in range(15)),
        *("security_operational_incident" for _ in range(15)),
        *("market_access" for _ in range(15)),
    ]

    def labels(family: str, index: int) -> dict[str, object]:
        return _taxonomy(
            event_family=family,
            change_state="reported",
            assertion_status="claimed",
            source_authority="issuer_first_party" if index % 2 else "reputable_secondary",
        ).model_dump(mode="json")

    cases: list[dict[str, object]] = []
    for index, family in enumerate(development_families):
        taxonomy = labels(family, index)
        readiness_role = "boundary" if index < 30 else ("retention" if index < 130 else "negative")
        cases.append(
            {
                "case_id": f"development-{index}",
                "cluster_id": f"development-{index}",
                "event_id": f"event-development-{index}",
                "split": "development",
                "opened_at_ms": index,
                "readiness_role": readiness_role,
                "release_stratum": f"stratum-{index % 3}",
                "safety_covered": True,
                "language": "zh" if index % 2 else "en",
                "audience": "macro" if "macro" in family or "geopolitical" in family else "us_equity",
                "scope": "macro" if "macro" in family or "geopolitical" in family else "single_name",
                "gold": taxonomy,
                "prediction": taxonomy,
                "gold_receipt": _gold_receipt(f"development-{index}"),
            }
        )

    holdout_families = (
        ["product_service_change"] * 10
        + ["financial_results"] * 10
        + ["macro_policy_data"] * 10
        + ["geopolitical_conflict"] * 10
        + ["market_access"] * 160
    )
    registered_at_ms = _evaluation_context().candidate_registration.registered_at_ms
    for index, family in enumerate(holdout_families):
        taxonomy = labels(family, index)
        cases.append(
            {
                "case_id": f"holdout-{index}",
                "cluster_id": f"holdout-{index}",
                "event_id": f"event-holdout-{index}",
                "split": "future_holdout",
                "candidate_registered_at_ms": registered_at_ms,
                "opened_at_ms": registered_at_ms + 24 * 3_600_000 + index,
                "eligible": True,
                "accepted_primary": index < 40,
                "language": "zh" if index % 2 else "en",
                "audience": "macro" if "macro" in family or "geopolitical" in family else "us_equity",
                "scope": "macro" if "macro" in family or "geopolitical" in family else "single_name",
                "gold": taxonomy,
                "prediction": taxonomy,
                "gold_receipt": _gold_receipt(f"holdout-{index}"),
            }
        )

    report = build_taxonomy_evaluation_report(cases, context=_evaluation_context(case_n=len(cases)))

    assert report.readiness["development"]["ready"] is True
    assert report.readiness["future_holdout"]["ready"] is True
    assert report.outcome == "PASS"
    assert {gate["outcome"] for gate in report.quality_gates.values()} == {"PASS"}
    assert report.identity.tested_git_sha == "a" * 40
    assert report.identity.taxonomy_program_sha256 == "e" * 64
    assert report.identity.taxonomy_model_binding_sha256 == "f" * 64

    missing_regression = TaxonomyEvaluationContextV1.model_validate(
        _evaluation_context(case_n=len(cases)).model_dump(mode="json")
        | {
            "regression_gates": {
                **_evaluation_context(case_n=len(cases)).model_dump(mode="json")["regression_gates"],
                "novelty": _regression_receipt(
                    "novelty",
                    registration=_evaluation_context().candidate_registration,
                    outcome="UNKNOWN",
                ),
            }
        }
    )
    unknown = build_taxonomy_evaluation_report(cases, context=missing_regression)
    assert unknown.quality_gates["regression_novelty"]["outcome"] == "UNKNOWN"
    assert unknown.outcome == "UNKNOWN"

    abstained_cases = [
        {
            **case,
            "prediction": {
                **case["prediction"],
                "subject_codes": [],
                "source_authority": "unknown",
            },
        }
        for case in cases
    ]
    abstained = build_taxonomy_evaluation_report(
        abstained_cases,
        context=_evaluation_context(case_n=len(abstained_cases)),
    )
    assert abstained.abstention_risk_coverage[1]["coverage"] == 0.0

    critical_cases = [{**case, "critical_regression": case.get("split") == "future_holdout"} for case in cases]
    critical = build_taxonomy_evaluation_report(
        critical_cases,
        context=_evaluation_context(case_n=len(critical_cases)),
    )
    assert critical.quality_gates["candidate_only_critical_regression"]["outcome"] == "FAIL"

    for case in cases:
        case["prediction"] = {
            **case["prediction"],
            "subject_codes": ["medtop:20000178"],
        }
    failed = build_taxonomy_evaluation_report(cases, context=_evaluation_context(case_n=len(cases)))

    assert failed.subject_codes["micro_f1"] == 0.0
    assert failed.quality_gates["subject_codes_micro_f1"]["outcome"] == "FAIL"
    assert failed.outcome == "FAIL"
