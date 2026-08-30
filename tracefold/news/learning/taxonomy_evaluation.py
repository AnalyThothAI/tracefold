"""Taxonomy evaluation metrics, denominators, and release gates."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..artifact_identity import canonical_sha
from ..taxonomy import IPTC_CODEBOOK_SHA256, IPTC_SUBJECT_CODES, TAXONOMY_VERSION, IPTCCodebookSha, NewsTaxonomyV1
from .metric import PRODUCTION_REGRESSION_GATES
from .taxonomy import (
    TaxonomyCandidateRegistrationV1,
    TaxonomyGoldReceiptV1,
    TaxonomyRegressionGateReceiptV1,
)
from .taxonomy_shadow import TaxonomyShadowPopulationV1

TAXONOMY_EVALUATION_SCHEMA: Final = "tracefold.news.taxonomy_evaluation_report.v3"
_REGRESSION_GATES: Final = PRODUCTION_REGRESSION_GATES


class _ExactModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TaxonomyEvaluationReportV1(_ExactModel):
    schema_id: Literal["tracefold.news.taxonomy_evaluation_report.v3"] = TAXONOMY_EVALUATION_SCHEMA
    taxonomy_version: Literal["news_taxonomy_v1"] = TAXONOMY_VERSION
    codebook_sha256: IPTCCodebookSha = IPTC_CODEBOOK_SHA256
    identity: TaxonomyEvaluationIdentityV1
    case_n: int = Field(ge=0)
    scored_case_n: int = Field(ge=0)
    cluster_n: int = Field(ge=0)
    provider_duplicate_n: int = Field(ge=0)
    population_root_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    split_roots: dict[str, str]
    axes: dict[str, Any]
    subject_codes: dict[str, Any]
    shadow_population: TaxonomyShadowPopulationV1
    abstention_risk_coverage: list[dict[str, Any]]
    slices: dict[str, Any]
    reviewer: dict[str, Any]
    readiness: dict[str, Any]
    quality_gates: dict[str, Any]
    outcome: Literal["PASS", "FAIL", "UNKNOWN"]

    @property
    def report_sha256(self) -> str:
        return canonical_sha(self.model_dump(mode="json"))


class TaxonomyEvaluationContextV1(_ExactModel):
    candidate_registration_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_registration: TaxonomyCandidateRegistrationV1
    gold_ledger_root_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    regression_gates: dict[str, TaxonomyRegressionGateReceiptV1]
    shadow_population: TaxonomyShadowPopulationV1

    @model_validator(mode="after")
    def exact_regression_gates(self) -> TaxonomyEvaluationContextV1:
        if self.candidate_registration_sha256 != self.candidate_registration.artifact_sha256:
            raise ValueError("news_taxonomy_candidate_registration_identity_mismatch")
        if set(self.regression_gates) != set(_REGRESSION_GATES):
            raise ValueError("news_taxonomy_regression_gate_set_invalid")
        if any(receipt.gate != name for name, receipt in self.regression_gates.items()):
            raise ValueError("news_taxonomy_regression_gate_identity_mismatch")
        candidates = {receipt.candidate_sha256 for receipt in self.regression_gates.values()}
        datasets = {receipt.dataset_sha256 for receipt in self.regression_gates.values()}
        metrics = {(receipt.metric_id, receipt.metric_sha256) for receipt in self.regression_gates.values()}
        if (
            len(candidates) != 1
            or len(datasets) != 1
            or metrics != {(self.candidate_registration.metric_id, self.candidate_registration.metric_sha256)}
        ):
            raise ValueError("news_taxonomy_regression_evidence_cohort_mismatch")
        return self


class TaxonomyEvaluationIdentityV1(_ExactModel):
    tested_git_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    program_version: str
    program_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    stable_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    deployment_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    envelope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    taxonomy_version: Literal["news_taxonomy_v1"] = TAXONOMY_VERSION
    codebook_sha256: IPTCCodebookSha = IPTC_CODEBOOK_SHA256
    review_rubric_version: Literal["news_review_v6"] = "news_review_v6"
    metric_id: str
    metric_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_version: str
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_model_bindings_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    taxonomy_program_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    taxonomy_model_binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_registration_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_registered_at_ms: int = Field(gt=0)
    regression_candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    regression_dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    regression_evidence_root_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    cluster_root_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    gold_ledger_root_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class _TaxonomyEvaluationCase(BaseModel):
    """One typed scored case plus its optional preregistration/readiness labels."""

    model_config = ConfigDict(extra="allow", frozen=True)

    case_id: str
    cluster_id: str
    event_id: str
    evidence_version: int = 0
    evidence_sha256: str = ""
    opened_at_ms: int = 0
    split: str = "development"
    gold: NewsTaxonomyV1
    prediction: NewsTaxonomyV1
    gold_receipt: TaxonomyGoldReceiptV1
    prediction_artifact_sha256: str = ""
    primary_taxonomy: NewsTaxonomyV1 | None = None
    candidate_registered_at_ms: int | None = None
    readiness_role: str = ""
    release_stratum: str = ""
    stratum: str = ""
    language: str = ""
    audience: str = ""
    scope: str = ""
    should_push: str = ""
    adjudicated: bool = False
    is_boundary: bool = False
    is_retention: bool = False
    is_negative: bool = False
    is_safety: bool = False
    safety_covered: bool = False
    accepted_primary: bool = False
    eligible: bool = False
    critical_regression: bool = False


_AXES: Final = ("event_family", "change_state", "source_authority", "assertion_status")
_FAMILY_MINIMUMS: Final[dict[str, int]] = {
    "product_service_change": 30,
    "macro_policy_data": 30,
    "geopolitical_conflict": 30,
    "market_flow_price": 30,
    "other": 30,
    "corporate_transaction": 15,
    "financing_capital_allocation": 15,
    "leadership_governance": 15,
    "regulatory_legal": 15,
    "security_operational_incident": 15,
    "market_access": 15,
}


def _safe_div(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def _class_metrics(pairs: Sequence[tuple[str, str]]) -> dict[str, Any]:
    labels = sorted({value for pair in pairs for value in pair})
    confusion = {gold: {pred: 0 for pred in labels} for gold in labels}
    for gold, prediction in pairs:
        confusion[gold][prediction] += 1
    per_class: dict[str, Any] = {}
    for label in labels:
        tp = confusion[label][label]
        support = sum(confusion[label].values())
        predicted = sum(row[label] for row in confusion.values())
        precision = _safe_div(tp, predicted)
        recall = _safe_div(tp, support)
        if support and precision is None:
            precision = 0.0
        f1 = (
            None
            if precision is None or recall is None
            else (0.0 if precision + recall == 0 else round(2 * precision * recall / (precision + recall), 6))
        )
        per_class[label] = {"support": support, "precision": precision, "recall": recall, "f1": f1}
    scored = [value["f1"] for value in per_class.values() if value["support"] and value["f1"] is not None]
    return {
        "confusion_matrix": confusion,
        "per_class": per_class,
        "accuracy": _safe_div(sum(gold == pred for gold, pred in pairs), len(pairs)),
        "macro_f1": round(sum(scored) / len(scored), 6) if scored else None,
    }


def _multilabel_metrics(cases: Sequence[_TaxonomyEvaluationCase]) -> dict[str, Any]:
    tp = fp = fn = 0
    per_code: dict[str, dict[str, int]] = {
        code: {"tp": 0, "fp": 0, "fn": 0, "support": 0} for code in IPTC_SUBJECT_CODES
    }
    exact = 0
    for case in cases:
        gold = set(case.gold.subject_codes)
        predicted = set(case.prediction.subject_codes)
        exact += gold == predicted
        tp += len(gold & predicted)
        fp += len(predicted - gold)
        fn += len(gold - predicted)
        for code in IPTC_SUBJECT_CODES:
            per_code[code]["support"] += code in gold
            per_code[code]["tp"] += code in gold and code in predicted
            per_code[code]["fp"] += code not in gold and code in predicted
            per_code[code]["fn"] += code in gold and code not in predicted
    precision, recall = _safe_div(tp, tp + fp), _safe_div(tp, tp + fn)
    f1 = (
        None
        if precision is None or recall is None
        else (0.0 if precision + recall == 0 else round(2 * precision * recall / (precision + recall), 6))
    )
    return {
        "micro_precision": precision,
        "micro_recall": recall,
        "micro_f1": f1,
        "exact_accuracy": _safe_div(exact, len(cases)),
        "per_code": per_code,
    }


def _slice(cases: Sequence[_TaxonomyEvaluationCase], key: str) -> dict[str, Any]:
    grouped: dict[str, list[_TaxonomyEvaluationCase]] = defaultdict(list)
    for case in cases:
        value = case.gold.source_authority if key == "source_authority" else getattr(case, key)
        grouped[str(value or "unknown")].append(case)
    return {
        value: {
            "n": len(rows),
            "exact_accuracy": _safe_div(
                sum(row.gold == row.prediction for row in rows),
                len(rows),
            ),
        }
        for value, rows in sorted(grouped.items())
    }


def _gate(value: bool | None, *, observed: Any, threshold: str) -> dict[str, Any]:
    return {
        "outcome": "UNKNOWN" if value is None else ("PASS" if value else "FAIL"),
        "observed": observed,
        "threshold": threshold,
    }


def _parse_evaluation_cases(raw_cases: Sequence[Mapping[str, Any]]) -> list[_TaxonomyEvaluationCase]:
    if any(str(raw.get("split") or "development") not in {"development", "future_holdout"} for raw in raw_cases):
        raise ValueError("news_taxonomy_split_invalid")
    parsed = [
        _TaxonomyEvaluationCase.model_validate(
            dict(raw)
            | {
                "case_id": str(raw.get("case_id") or ""),
                "cluster_id": str(raw.get("cluster_id") or raw.get("case_id") or ""),
                "event_id": str(raw.get("event_id") or ""),
            }
        )
        for raw in sorted(
            raw_cases,
            key=lambda row: (int(row.get("opened_at_ms") or 0), str(row.get("case_id") or "")),
        )
    ]
    if any(not case.case_id or not case.cluster_id or not case.event_id for case in parsed):
        raise ValueError("news_taxonomy_case_cluster_identity_required")
    if len({case.case_id for case in parsed}) != len(parsed):
        raise ValueError("news_taxonomy_case_id_duplicate")
    return parsed


def _cluster_representatives(parsed: Sequence[_TaxonomyEvaluationCase]) -> list[_TaxonomyEvaluationCase]:
    cluster_splits: dict[str, set[str]] = defaultdict(set)
    cluster_gold: dict[str, set[str]] = defaultdict(set)
    representatives: dict[str, _TaxonomyEvaluationCase] = {}
    for case in parsed:
        cluster_splits[case.cluster_id].add(case.split)
        cluster_gold[case.cluster_id].add(canonical_sha(case.gold.model_dump(mode="json")))
        representatives.setdefault(case.cluster_id, case)
    if any(len(values) != 1 for values in cluster_splits.values()):
        raise ValueError("news_taxonomy_cluster_split_leakage")
    if any(len(values) != 1 for values in cluster_gold.values()):
        raise ValueError("news_taxonomy_cluster_gold_conflict")
    return list(representatives.values())


@dataclass(frozen=True, slots=True)
class _EvaluationMetrics:
    axes: dict[str, Any]
    subject: dict[str, Any]
    risk_curve: list[dict[str, Any]]
    reviewer: dict[str, Any]
    population_complete: bool


def _evaluation_metrics(
    cases: Sequence[_TaxonomyEvaluationCase],
    scored_cases: Sequence[_TaxonomyEvaluationCase],
    population: TaxonomyShadowPopulationV1,
    *,
    parsed_case_n: int,
) -> _EvaluationMetrics:
    axes = {
        axis: _class_metrics(
            [(str(getattr(case.gold, axis)), str(getattr(case.prediction, axis))) for case in scored_cases]
        )
        for axis in _AXES
    }
    subject = _multilabel_metrics(scored_cases)
    subject.update(
        {
            "schema_cardinality_code_attempt_n": population.physical_attempt_n,
            "schema_cardinality_code_invalid": population.schema_invalid_attempt_n,
        }
    )
    non_abstained = [
        case
        for case in scored_cases
        if case.prediction.subject_codes
        and case.prediction.event_family != "other"
        and case.prediction.change_state != "unknown"
        and case.prediction.source_authority != "unknown"
        and case.prediction.assertion_status != "unknown"
    ]
    risk_curve = [
        {
            "point": "all",
            "coverage": 1.0 if scored_cases else None,
            "error_rate": _safe_div(sum(case.gold != case.prediction for case in scored_cases), len(scored_cases)),
        },
        {
            "point": "non_abstain",
            "coverage": _safe_div(len(non_abstained), len(scored_cases)),
            "error_rate": _safe_div(
                sum(case.gold != case.prediction for case in non_abstained),
                len(non_abstained),
            ),
        },
    ]
    agreement_rows = [case for case in cases if case.primary_taxonomy is not None]
    agreement_n = sum(case.primary_taxonomy == case.gold for case in agreement_rows)
    adjudicated_n = sum(case.adjudicated for case in cases)
    reviewer = {
        "primary_gold_pair_n": len(agreement_rows),
        "exact_agreement_n": agreement_n,
        "exact_agreement_rate": _safe_div(agreement_n, len(agreement_rows)),
        "adjudicated_n": adjudicated_n,
        "adjudication_rate": _safe_div(adjudicated_n, len(cases)),
    }
    return _EvaluationMetrics(
        axes=axes,
        subject=subject,
        risk_curve=risk_curve,
        reviewer=reviewer,
        population_complete=population.complete and population.success_n == parsed_case_n,
    )


@dataclass(frozen=True, slots=True)
class _EvaluationReadiness:
    ready: bool
    development_ready: bool
    holdout_ready: bool
    development_checks: dict[str, Any]
    holdout_checks: dict[str, Any]


def _evaluation_readiness(
    development: Sequence[_TaxonomyEvaluationCase],
    holdout: Sequence[_TaxonomyEvaluationCase],
    registration: TaxonomyCandidateRegistrationV1,
) -> _EvaluationReadiness:
    def family_n(rows: Sequence[_TaxonomyEvaluationCase], labels: set[str]) -> int:
        return sum(case.gold.event_family in labels for case in rows)

    development_checks = {
        "boundary_cluster_n": {
            "observed": sum(case.is_boundary or case.readiness_role == "boundary" for case in development),
            "minimum": 30,
        },
        "retention_cluster_n": {
            "observed": sum(case.is_retention or case.readiness_role == "retention" for case in development),
            "minimum": 100,
        },
        "negative_cluster_n": {
            "observed": sum(case.is_negative or case.readiness_role == "negative" for case in development),
            "minimum": 50,
        },
        "release_strata_n": {
            "observed": len({case.release_stratum or case.stratum for case in development} - {""}),
            "minimum": 3,
        },
        "safety_uncovered_n": {
            "observed": sum(case.is_safety and not case.safety_covered for case in development),
            "maximum": 0,
        },
        "financial_results_plus_guidance": {
            "observed": family_n(development, {"financial_results", "guidance_outlook"}),
            "minimum": 30,
        },
        **{
            family: {"observed": family_n(development, {family}), "minimum": minimum}
            for family, minimum in _FAMILY_MINIMUMS.items()
        },
        "language_zh": {"observed": sum(case.language == "zh" for case in development), "minimum": 30},
        "language_en": {"observed": sum(case.language == "en" for case in development), "minimum": 30},
        "issuer_first_party": {
            "observed": sum(case.gold.source_authority == "issuer_first_party" for case in development),
            "minimum": 30,
        },
        "reputable_secondary": {
            "observed": sum(case.gold.source_authority == "reputable_secondary" for case in development),
            "minimum": 30,
        },
    }
    claimed_registered_values = {
        int(case.candidate_registered_at_ms) for case in holdout if case.candidate_registered_at_ms is not None
    }
    if claimed_registered_values and claimed_registered_values != {registration.registered_at_ms}:
        raise ValueError("news_taxonomy_holdout_candidate_registration_mismatch")
    registered_at_ms = registration.registered_at_ms
    if registered_at_ms and development and max(case.opened_at_ms for case in development) >= registered_at_ms:
        raise ValueError("news_taxonomy_development_not_before_candidate_registration")
    accepted_holdout = [case for case in holdout if case.accepted_primary]
    holdout_checks = {
        "candidate_registration_present": {"observed": int(bool(holdout)), "minimum": 1},
        "post_registration_violation_n": {
            "observed": sum(case.opened_at_ms <= registered_at_ms for case in holdout),
            "maximum": 0,
        },
        "duration_ms": {
            "observed": max(0, max((case.opened_at_ms for case in holdout), default=0) - registered_at_ms),
            "minimum": 24 * 3_600_000,
        },
        "eligible_event_n": {"observed": sum(case.eligible for case in holdout), "minimum": 200},
        "accepted_primary_cluster_n": {"observed": len(accepted_holdout), "minimum": 30},
        "product_service_change": {
            "observed": family_n(accepted_holdout, {"product_service_change"}),
            "minimum": 10,
        },
        "financial_results_plus_guidance": {
            "observed": family_n(accepted_holdout, {"financial_results", "guidance_outlook"}),
            "minimum": 10,
        },
        "macro_policy_data": {"observed": family_n(accepted_holdout, {"macro_policy_data"}), "minimum": 10},
        "geopolitical_conflict": {
            "observed": family_n(accepted_holdout, {"geopolitical_conflict"}),
            "minimum": 10,
        },
    }

    def checks_pass(checks: Mapping[str, Mapping[str, Any]]) -> bool:
        return all(
            value["observed"] <= value["maximum"] if "maximum" in value else value["observed"] >= value["minimum"]
            for value in checks.values()
        )

    development_ready = checks_pass(development_checks)
    holdout_ready = checks_pass(holdout_checks)
    return _EvaluationReadiness(
        ready=development_ready and holdout_ready,
        development_ready=development_ready,
        holdout_ready=holdout_ready,
        development_checks=development_checks,
        holdout_checks=holdout_checks,
    )


def _quality_gates(
    scored_cases: Sequence[_TaxonomyEvaluationCase],
    *,
    context: TaxonomyEvaluationContextV1,
    population: TaxonomyShadowPopulationV1,
    metrics: _EvaluationMetrics,
    ready: bool,
) -> tuple[dict[str, Any], Literal["PASS", "FAIL", "UNKNOWN"]]:
    def family_pr(labels: set[str]) -> tuple[float | None, float | None]:
        tp = sum(case.gold.event_family in labels and case.prediction.event_family in labels for case in scored_cases)
        predicted = sum(case.prediction.event_family in labels for case in scored_cases)
        support = sum(case.gold.event_family in labels for case in scored_cases)
        return _safe_div(tp, predicted), _safe_div(tp, support)

    product_precision, product_recall = family_pr({"product_service_change"})
    financial_precision, financial_recall = family_pr({"financial_results", "guidance_outlook"})
    known_source_cases = [case for case in scored_cases if case.gold.source_authority != "unknown"]
    known_source_accuracy = _safe_div(
        sum(case.gold.source_authority == case.prediction.source_authority for case in known_source_cases),
        len(known_source_cases),
    )
    event_macro_f1 = metrics.axes["event_family"]["macro_f1"]
    non_abstain_coverage = metrics.risk_curve[1]["coverage"]
    non_abstain_error = metrics.risk_curve[1]["error_rate"]
    gates = {
        "schema_cardinality_code_invalid": _gate(
            None if not metrics.population_complete else population.schema_invalid_attempt_n == 0,
            observed={
                "attempt_n": population.physical_attempt_n,
                "invalid_attempt_n": population.schema_invalid_attempt_n,
                "missing_observation_n": population.missing_observation_n,
                "invalid_observation_n": population.invalid_observation_n,
            },
            threshold="invalid_attempt_n = 0 with complete observation/receipt population",
        ),
        "shadow_terminal_outcomes": _gate(
            None
            if not metrics.population_complete
            else population.provider_failure_n == 0 and population.budget_deadline_failure_n == 0,
            observed={
                "eligible_case_n": population.eligible_case_n,
                "success_n": population.success_n,
                "schema_invalid_n": population.schema_invalid_n,
                "provider_failure_n": population.provider_failure_n,
                "budget_deadline_failure_n": population.budget_deadline_failure_n,
            },
            threshold="provider_failure_n = 0 and budget_deadline_failure_n = 0",
        ),
        "event_family_macro_f1": _gate(
            None if not ready or event_macro_f1 is None else event_macro_f1 >= 0.85,
            observed=event_macro_f1,
            threshold=">= 0.85",
        ),
        "product_precision_recall": _gate(
            None
            if not ready or product_precision is None or product_recall is None
            else min(product_precision, product_recall) >= 0.90,
            observed={"precision": product_precision, "recall": product_recall},
            threshold="both >= 0.90",
        ),
        "financial_precision_recall": _gate(
            None
            if not ready or financial_precision is None or financial_recall is None
            else min(financial_precision, financial_recall) >= 0.90,
            observed={"precision": financial_precision, "recall": financial_recall},
            threshold="both >= 0.90",
        ),
        "change_state_accuracy": _gate(
            None if not ready else metrics.axes["change_state"]["accuracy"] >= 0.90,
            observed=metrics.axes["change_state"]["accuracy"],
            threshold=">= 0.90",
        ),
        "source_authority_accuracy": _gate(
            None if not ready or known_source_accuracy is None else known_source_accuracy == 1.0,
            observed=known_source_accuracy,
            threshold="= 1.00",
        ),
        "assertion_status_macro_f1": _gate(
            None
            if not ready or metrics.axes["assertion_status"]["macro_f1"] is None
            else metrics.axes["assertion_status"]["macro_f1"] >= 0.90,
            observed=metrics.axes["assertion_status"]["macro_f1"],
            threshold=">= 0.90",
        ),
        "subject_codes_micro_f1": _gate(
            None if not ready or metrics.subject["micro_f1"] is None else metrics.subject["micro_f1"] >= 0.85,
            observed=metrics.subject["micro_f1"],
            threshold=">= 0.85",
        ),
        "non_abstain_coverage_risk": _gate(
            None
            if not ready or non_abstain_coverage is None or non_abstain_error is None
            else non_abstain_coverage >= 0.80 and non_abstain_error <= 0.08,
            observed={"coverage": non_abstain_coverage, "error_rate": non_abstain_error},
            threshold="coverage >= 0.80 and error_rate <= 0.08",
        ),
        "confirmed_rumor_must_reversal": _gate(
            None
            if not ready
            else not any(
                case.should_push in {"must_push", "must_hold"}
                and {case.gold.assertion_status, case.prediction.assertion_status} == {"confirmed", "rumor"}
                for case in scored_cases
            ),
            observed=sum(
                case.should_push in {"must_push", "must_hold"}
                and {case.gold.assertion_status, case.prediction.assertion_status} == {"confirmed", "rumor"}
                for case in scored_cases
            ),
            threshold="= 0",
        ),
        "candidate_only_critical_regression": _gate(
            None if not ready else not any(case.critical_regression for case in scored_cases),
            observed=sum(case.critical_regression for case in scored_cases),
            threshold="= 0",
        ),
        **{
            f"regression_{name}": {
                "outcome": context.regression_gates[name].outcome,
                "observed": {
                    "denominator_n": context.regression_gates[name].denominator_n,
                    "stable_failure_n": context.regression_gates[name].stable_failure_n,
                    "candidate_failure_n": context.regression_gates[name].candidate_failure_n,
                    "candidate_only_regression_n": context.regression_gates[name].candidate_only_regression_n,
                    "candidate_only_case_ids": list(context.regression_gates[name].candidate_only_case_ids),
                    "gate_evidence_sha256": context.regression_gates[name].gate_evidence_sha256,
                    "release_evidence_sha256": context.regression_gates[name].evidence_sha256,
                },
                "threshold": "candidate_only_regression_n = 0 with denominator_n > 0",
            }
            for name in _REGRESSION_GATES
        },
    }
    outcomes = {value["outcome"] for value in gates.values()}
    outcome: Literal["PASS", "FAIL", "UNKNOWN"] = (
        "UNKNOWN" if "UNKNOWN" in outcomes else ("FAIL" if "FAIL" in outcomes else "PASS")
    )
    return gates, outcome


def build_taxonomy_evaluation_report(
    raw_cases: Sequence[Mapping[str, Any]],
    *,
    context: TaxonomyEvaluationContextV1 | Mapping[str, Any],
) -> TaxonomyEvaluationReportV1:
    """Evaluate one representative per connected fact cluster on one frozen population."""

    evaluation_context = (
        context
        if isinstance(context, TaxonomyEvaluationContextV1)
        else TaxonomyEvaluationContextV1.model_validate(context)
    )

    parsed = _parse_evaluation_cases(raw_cases)
    cases = _cluster_representatives(parsed)
    development = [case for case in cases if case.split == "development"]
    holdout = [case for case in cases if case.split == "future_holdout"]
    scored_cases = holdout or development
    population = evaluation_context.shadow_population
    metrics = _evaluation_metrics(cases, scored_cases, population, parsed_case_n=len(parsed))
    axes = metrics.axes
    subject = metrics.subject
    risk_curve = metrics.risk_curve
    reviewer = metrics.reviewer

    registration = evaluation_context.candidate_registration
    readiness = _evaluation_readiness(development, holdout, registration)
    development_checks = readiness.development_checks
    holdout_checks = readiness.holdout_checks
    development_ready = readiness.development_ready
    holdout_ready = readiness.holdout_ready
    ready = readiness.ready

    gates, outcome = _quality_gates(
        scored_cases,
        context=evaluation_context,
        population=population,
        metrics=metrics,
        ready=ready,
    )
    public_cases: list[dict[str, Any]] = [
        {
            "case_id": case.case_id,
            "cluster_id": case.cluster_id,
            "event_id": case.event_id,
            "evidence_version": case.evidence_version,
            "evidence_sha256": case.evidence_sha256,
            "opened_at_ms": case.opened_at_ms,
            "split": case.split,
            "gold": case.gold.model_dump(mode="json"),
            "prediction": case.prediction.model_dump(mode="json"),
            "prediction_artifact_sha256": case.prediction_artifact_sha256,
            "gold_receipt": case.gold_receipt.model_dump(mode="json"),
        }
        for case in cases
    ]
    splits: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for public_case in public_cases:
        splits[public_case["split"]].append(public_case)
    cluster_root_sha256 = canonical_sha(sorted(public_case["cluster_id"] for public_case in public_cases))
    regression_receipts = list(evaluation_context.regression_gates.values())
    identity = TaxonomyEvaluationIdentityV1(
        tested_git_sha=registration.tested_git_sha,
        program_version=registration.program_version,
        program_sha256=registration.program_sha256,
        stable_bundle_sha256=registration.stable_bundle_sha256,
        runtime_manifest_sha256=registration.runtime_manifest_sha256,
        image_digest=registration.image_digest,
        deployment_receipt_sha256=registration.deployment_receipt_sha256,
        envelope_sha256=registration.envelope_sha256,
        review_rubric_version=registration.review_rubric_version,
        metric_id=registration.metric_id,
        metric_sha256=registration.metric_sha256,
        policy_version=registration.policy_version,
        policy_sha256=registration.policy_sha256,
        runtime_model_bindings_sha256=registration.runtime_model_bindings_sha256,
        taxonomy_program_sha256=registration.taxonomy_program_sha256,
        taxonomy_model_binding_sha256=registration.taxonomy_model_binding_sha256,
        candidate_registration_sha256=evaluation_context.candidate_registration_sha256,
        candidate_registered_at_ms=registration.registered_at_ms,
        regression_candidate_sha256=regression_receipts[0].candidate_sha256,
        regression_dataset_sha256=regression_receipts[0].dataset_sha256,
        regression_evidence_root_sha256=canonical_sha(
            {
                name: receipt.model_dump(mode="json")
                for name, receipt in sorted(evaluation_context.regression_gates.items())
            }
        ),
        dataset_sha256=canonical_sha({"cases": public_cases, "shadow_population": population.model_dump(mode="json")}),
        cluster_root_sha256=cluster_root_sha256,
        gold_ledger_root_sha256=evaluation_context.gold_ledger_root_sha256,
    )
    return TaxonomyEvaluationReportV1(
        identity=identity,
        case_n=population.eligible_case_n,
        scored_case_n=len(cases),
        cluster_n=len(cases),
        provider_duplicate_n=len(parsed) - len(cases),
        population_root_sha256=canonical_sha(
            {"cases": public_cases, "shadow_population": population.model_dump(mode="json")}
        ),
        split_roots={name: canonical_sha(rows) for name, rows in sorted(splits.items())},
        axes=axes,
        subject_codes=subject,
        shadow_population=population,
        abstention_risk_coverage=risk_curve,
        slices={key: _slice(scored_cases, key) for key in ("language", "source_authority", "audience", "scope")},
        reviewer=reviewer,
        readiness={
            "ready": ready,
            "quality_population": "future_holdout" if holdout else "development",
            "development": {"ready": development_ready, "checks": development_checks},
            "future_holdout": {"ready": holdout_ready, "checks": holdout_checks},
        },
        quality_gates=gates,
        outcome=outcome,
    )


__all__ = [
    "TAXONOMY_EVALUATION_SCHEMA",
    "TaxonomyEvaluationContextV1",
    "TaxonomyEvaluationIdentityV1",
    "TaxonomyEvaluationReportV1",
    "build_taxonomy_evaluation_report",
]
