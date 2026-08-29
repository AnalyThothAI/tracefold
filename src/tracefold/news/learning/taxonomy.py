"""Release-neutral taxonomy shadow execution and evaluation (#117).

This module can call one offline Predictor and can seal observations through the
existing append-only learning ledger.  It has no Event, verdict, card, delivery,
Trading, candidate, canary, or promotion writer.
"""

from __future__ import annotations

import importlib.metadata
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any, Final, Literal

import dspy  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..artifact_identity import canonical_sha, runtime_manifest_sha
from ..models import TRIAGE_POLICY_VERSION
from ..program.artifact import load_stable_program_artifact, render_model_evidence_json
from ..program.contracts import TriageContext
from ..program.identity import EXECUTION_ENVELOPE_SHA256
from ..program.lm import (
    AuditedConfiguredLM,
    LMCallContext,
    RecordedLM,
    RuntimeModelIdentity,
    program_json_adapter,
)
from ..program.runtime import PROGRAM_VERSION
from ..review.desk import REVIEW_RUBRIC_VERSION, taxonomy_requires_independent_adjudication
from ..taxonomy import (
    IPTC_CODEBOOK_SHA256,
    IPTC_SUBJECT_CODES,
    TAXONOMY_VERSION,
    IPTCCodebookSha,
    ModelTaxonomyV1,
    NewsTaxonomyV1,
    project_legacy_event_type,
    source_authority_from_evidence,
)
from .metric import (
    METRIC_ID,
    PRODUCTION_REGRESSION_GATES,
    ProductionRegressionGateEvidenceV1,
    metric_contract_sha256,
)

TAXONOMY_SHADOW_SCHEMA: Final = "tracefold.news.taxonomy_shadow_observation.v1"
TAXONOMY_CANDIDATE_REGISTRATION_SCHEMA: Final = "tracefold.news.taxonomy_candidate_registration.v1"
TAXONOMY_EVALUATION_SCHEMA: Final = "tracefold.news.taxonomy_evaluation_report.v1"
TAXONOMY_SHADOW_INSTRUCTION: Final = """Classify one bounded ordinary News Event under news_taxonomy_v1.
Return only the typed taxonomy. Choose at most three allowed IPTC subject qcodes. event_family describes what
happened, never source format, rumor status, actor type, noise, or delivery value. filing is a source container;
classify its underlying financial/product/corporate/regulatory event. change_state distinguishes announced,
scheduled, effective, reported, updated, delayed, cancelled, recalled, and unknown. assertion_status is confirmed
only when bounded evidence directly establishes the fact; otherwise claimed, rumor, conflicted, or unknown. Use
other/unknown as honest abstentions. Do not output source_authority; code derives it from provenance. Use no tools,
retrieval, external knowledge, confidence, delivery recommendation, or trading recommendation."""


class _ExactModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TaxonomyShadowSignature(dspy.Signature):  # type: ignore[misc]
    evidence_json: str = dspy.InputField(desc="Production-bounded EventSemantics evidence JSON")
    taxonomy: ModelTaxonomyV1 = dspy.OutputField(desc="The four model-owned news_taxonomy_v1 axes")


class TaxonomyShadowObservationV1(_ExactModel):
    schema_id: Literal["tracefold.news.taxonomy_shadow_observation.v1"] = TAXONOMY_SHADOW_SCHEMA
    release_authority: Literal[False] = False
    event_id: str
    evidence_version: int = Field(ge=0)
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    shadow_program_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_identity: RuntimeModelIdentity
    model_binding: str = Field(min_length=1)
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    invocation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    recording_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    recording: dict[str, Any]
    taxonomy: NewsTaxonomyV1

    @property
    def observation_sha256(self) -> str:
        return canonical_sha(self.model_dump(mode="json"))

    @model_validator(mode="after")
    def recording_is_exactly_replayable(self) -> TaxonomyShadowObservationV1:
        if self.recording_sha256 != canonical_sha(self.recording):
            raise ValueError("news_taxonomy_shadow_recording_identity_mismatch")
        RecordedLM(
            {self.request_sha256: self.recording},
            model=self.model_identity.model,
            runtime_identity=self.model_identity,
            model_binding=self.model_binding,
        )
        expected_invocation = canonical_sha(
            {
                "program_version": TAXONOMY_SHADOW_SCHEMA,
                "program_sha256": self.shadow_program_sha256,
                "context_sha256": self.context_sha256,
                "predictor": "taxonomy_shadow",
                "route": "shadow",
                "attempt": 1,
                "model_binding": self.model_binding,
                "runtime_binding_sha256": self.model_identity.binding_sha256,
                "request_sha256": self.request_sha256,
            }
        )
        if self.invocation_sha256 != expected_invocation:
            raise ValueError("news_taxonomy_shadow_invocation_identity_mismatch")
        return self


class TaxonomyShadowProgramV1(dspy.Module):  # type: ignore[misc]
    """One offline Predictor; never composed into the production route."""

    def __init__(self, *, lm: AuditedConfiguredLM, max_tokens: int = 800) -> None:
        super().__init__()
        if lm.predictor != "taxonomy_shadow" or lm.route != "shadow" or lm.ledger is None:
            raise dspy.LMConfigurationError("news_taxonomy_shadow_audited_lm_required")
        self.lm = lm
        self.model_identity = lm.runtime_identity
        self.model_binding = lm.model_binding
        self.max_tokens = int(max_tokens)
        self.classify = dspy.Predict(
            TaxonomyShadowSignature.with_instructions(TAXONOMY_SHADOW_INSTRUCTION),
            max_tokens=self.max_tokens,
        )
        self.shadow_program_sha256 = canonical_sha(
            {
                "schema": TAXONOMY_SHADOW_SCHEMA,
                "instruction": TAXONOMY_SHADOW_INSTRUCTION,
                "signature": TaxonomyShadowSignature.dump_state(),
                "output_schema": ModelTaxonomyV1.model_json_schema(),
                "codebook_sha256": IPTC_CODEBOOK_SHA256,
                "model_identity": self.model_identity.model_dump(mode="json"),
                "model_binding": self.model_binding,
                "dspy": importlib.metadata.version("dspy"),
                "adapter": "tracefold.news.program.lm.program_json_adapter",
                "max_tokens": self.max_tokens,
            }
        )

    def forward(
        self,
        context: TriageContext | Mapping[str, Any],
    ) -> TaxonomyShadowObservationV1:
        typed = context if isinstance(context, TriageContext) else TriageContext.model_validate(context)
        evidence_json = render_model_evidence_json(typed.event_semantics_payload(), predictor="event_semantics")
        context_sha256 = canonical_sha(typed.event_semantics_payload())
        ledger = self.lm.ledger
        if ledger is None:  # Constructor rejects this; keep the type boundary explicit.
            raise dspy.LMConfigurationError("news_taxonomy_shadow_audited_lm_required")
        start_index = len(ledger.receipts)
        with (
            ledger.scope(
                LMCallContext(
                    program_version=TAXONOMY_SHADOW_SCHEMA,
                    program_sha256=self.shadow_program_sha256,
                    context_sha256=context_sha256,
                )
            ),
            dspy.context(lm=self.lm, adapter=program_json_adapter()),
        ):
            prediction = self.classify(evidence_json=evidence_json)
        receipts = ledger.receipts[start_index:]
        if len(receipts) != 1 or receipts[0].recording is None:
            raise ValueError("news_taxonomy_shadow_recording_missing")
        receipt = receipts[0]
        labels = (
            prediction.taxonomy
            if isinstance(prediction.taxonomy, ModelTaxonomyV1)
            else ModelTaxonomyV1.model_validate(prediction.taxonomy)
        )
        return TaxonomyShadowObservationV1(
            event_id=typed.evidence.event_id,
            evidence_version=typed.evidence.evidence_version,
            evidence_sha256=typed.evidence.evidence_sha256,
            context_sha256=context_sha256,
            shadow_program_sha256=self.shadow_program_sha256,
            model_identity=self.model_identity,
            model_binding=self.model_binding,
            request_sha256=receipt.request_sha256,
            invocation_sha256=receipt.invocation_sha256,
            recording_sha256=canonical_sha(receipt.recording),
            recording=receipt.recording,
            taxonomy=NewsTaxonomyV1.issue(
                labels,
                source_authority=source_authority_from_evidence(typed.evidence),
            ),
        )


class TaxonomyEvaluationReportV1(_ExactModel):
    schema_id: Literal["tracefold.news.taxonomy_evaluation_report.v1"] = TAXONOMY_EVALUATION_SCHEMA
    taxonomy_version: Literal["news_taxonomy_v1"] = TAXONOMY_VERSION
    codebook_sha256: IPTCCodebookSha = IPTC_CODEBOOK_SHA256
    identity: TaxonomyEvaluationIdentityV1
    case_n: int = Field(ge=0)
    cluster_n: int = Field(ge=0)
    provider_duplicate_n: int = Field(ge=0)
    population_root_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    split_roots: dict[str, str]
    axes: dict[str, Any]
    subject_codes: dict[str, Any]
    legacy_baseline: dict[str, Any]
    abstention_risk_coverage: list[dict[str, Any]]
    slices: dict[str, Any]
    reviewer: dict[str, Any]
    readiness: dict[str, Any]
    quality_gates: dict[str, Any]
    outcome: Literal["PASS", "FAIL", "UNKNOWN"]

    @property
    def report_sha256(self) -> str:
        return canonical_sha(self.model_dump(mode="json"))


class TaxonomyGoldReceiptV1(_ExactModel):
    review_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    acceptance_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    rubric_version: Literal["news_review_v5"] = "news_review_v5"
    reviewer: str = Field(min_length=1, max_length=128)
    accepted_at_ms: int = Field(ge=0)
    release_eligible: Literal[True]

    @model_validator(mode="after")
    def acceptance_addresses_review(self) -> TaxonomyGoldReceiptV1:
        expected = canonical_sha({"kind": "acceptance", "review_id": self.review_id})
        if self.acceptance_id != expected:
            raise ValueError("news_taxonomy_gold_acceptance_identity_mismatch")
        return self


class TaxonomyGoldVerificationV1(_ExactModel):
    ledger_root_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    cases: tuple[dict[str, Any], ...]


TaxonomyRegressionGateName = Literal["production_action", "asset_grounding", "novelty", "trade_relevance"]


class TaxonomyRegressionGateReferenceV1(_ExactModel):
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class TaxonomyRegressionGateReceiptV1(_ExactModel):
    gate: TaxonomyRegressionGateName
    outcome: Literal["PASS", "FAIL", "UNKNOWN"]
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    gate_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    metric_id: str
    metric_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    denominator_n: int = Field(ge=0)
    stable_failure_n: int = Field(ge=0)
    candidate_failure_n: int = Field(ge=0)
    candidate_only_regression_n: int = Field(ge=0)
    candidate_only_case_ids: tuple[str, ...]

    @model_validator(mode="after")
    def embeds_exact_gate_evidence(self) -> TaxonomyRegressionGateReceiptV1:
        gate_evidence = ProductionRegressionGateEvidenceV1.model_validate(
            {
                "gate": self.gate,
                "metric_id": self.metric_id,
                "metric_sha256": self.metric_sha256,
                "denominator_n": self.denominator_n,
                "stable_failure_n": self.stable_failure_n,
                "candidate_failure_n": self.candidate_failure_n,
                "candidate_only_regression_n": self.candidate_only_regression_n,
                "candidate_only_case_ids": self.candidate_only_case_ids,
                "outcome": self.outcome.lower(),
            }
        )
        if self.gate_evidence_sha256 != gate_evidence.evidence_sha256:
            raise ValueError("news_taxonomy_regression_gate_evidence_identity_mismatch")
        return self


_REGRESSION_GATES: Final = PRODUCTION_REGRESSION_GATES


class TaxonomyDeploymentReceiptV1(_ExactModel):
    tested_git_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    stable_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    deployment_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class TaxonomyCodeIdentityV1(_ExactModel):
    """Code-owned identities computed before any database transaction opens."""

    program_version: str
    program_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    envelope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    taxonomy_version: Literal["news_taxonomy_v1"] = TAXONOMY_VERSION
    codebook_sha256: IPTCCodebookSha = IPTC_CODEBOOK_SHA256
    review_rubric_version: Literal["news_review_v5"] = "news_review_v5"
    metric_id: str
    metric_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_version: str


def taxonomy_code_identity() -> TaxonomyCodeIdentityV1:
    """Read source-backed code identities while no PostgreSQL locks are held."""

    return TaxonomyCodeIdentityV1(
        program_version=PROGRAM_VERSION,
        program_sha256=load_stable_program_artifact().program_sha256,
        envelope_sha256=EXECUTION_ENVELOPE_SHA256,
        review_rubric_version=REVIEW_RUBRIC_VERSION,
        metric_id=METRIC_ID,
        metric_sha256=metric_contract_sha256(review_rubric_version=REVIEW_RUBRIC_VERSION),
        policy_version=TRIAGE_POLICY_VERSION,
    )


class TaxonomyCandidateRegistrationV1(_ExactModel):
    schema_id: Literal["tracefold.news.taxonomy_candidate_registration.v1"] = TAXONOMY_CANDIDATE_REGISTRATION_SCHEMA
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
    review_rubric_version: Literal["news_review_v5"] = "news_review_v5"
    metric_id: str
    metric_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_version: str
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_model_bindings_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    taxonomy_program_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    taxonomy_model_binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    registered_at_ms: int = Field(gt=0)

    @classmethod
    def issue(
        cls,
        *,
        code_identity: TaxonomyCodeIdentityV1,
        deployment: TaxonomyDeploymentReceiptV1,
        policy_sha256: str,
        runtime_model_bindings_sha256: str,
        taxonomy_program_sha256: str,
        taxonomy_model_binding_sha256: str,
        registered_at_ms: int,
    ) -> TaxonomyCandidateRegistrationV1:
        """Bind one shadow candidate to every code-owned and active runtime identity."""

        return cls(
            tested_git_sha=deployment.tested_git_sha,
            program_version=code_identity.program_version,
            program_sha256=code_identity.program_sha256,
            stable_bundle_sha256=deployment.stable_bundle_sha256,
            runtime_manifest_sha256=deployment.runtime_manifest_sha256,
            image_digest=deployment.image_digest,
            deployment_receipt_sha256=deployment.deployment_receipt_sha256,
            envelope_sha256=code_identity.envelope_sha256,
            taxonomy_version=code_identity.taxonomy_version,
            codebook_sha256=code_identity.codebook_sha256,
            review_rubric_version=code_identity.review_rubric_version,
            metric_id=code_identity.metric_id,
            metric_sha256=code_identity.metric_sha256,
            policy_version=code_identity.policy_version,
            policy_sha256=policy_sha256,
            runtime_model_bindings_sha256=runtime_model_bindings_sha256,
            taxonomy_program_sha256=taxonomy_program_sha256,
            taxonomy_model_binding_sha256=taxonomy_model_binding_sha256,
            registered_at_ms=registered_at_ms,
        )

    @property
    def artifact_sha256(self) -> str:
        return canonical_sha({"kind": "candidate_registration", "payload": self.model_dump(mode="json")})


class TaxonomyEvaluationContextV1(_ExactModel):
    candidate_registration_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_registration: TaxonomyCandidateRegistrationV1
    gold_ledger_root_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    regression_gates: dict[str, TaxonomyRegressionGateReceiptV1]

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
    review_rubric_version: Literal["news_review_v5"] = "news_review_v5"
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


def _multilabel_metrics(cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    tp = fp = fn = 0
    per_code: dict[str, dict[str, int]] = {
        code: {"tp": 0, "fp": 0, "fn": 0, "support": 0} for code in IPTC_SUBJECT_CODES
    }
    exact = 0
    for case in cases:
        gold = set(case["gold"].subject_codes)
        predicted = set(case["prediction"].subject_codes)
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
        "schema_cardinality_code_invalid": 0,
    }


def _slice(cases: Sequence[Mapping[str, Any]], key: str) -> dict[str, Any]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for case in cases:
        value = case["gold"].source_authority if key == "source_authority" else case.get(key)
        grouped[str(value or "unknown")].append(case)
    return {
        value: {
            "n": len(rows),
            "exact_accuracy": _safe_div(
                sum(row["gold"] == row["prediction"] for row in rows),
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

    invalid_splits = {
        str(raw.get("split") or "development")
        for raw in raw_cases
        if str(raw.get("split") or "development") not in {"development", "future_holdout"}
    }
    if invalid_splits:
        raise ValueError("news_taxonomy_split_invalid")

    parsed = [
        {
            **dict(raw),
            "case_id": str(raw.get("case_id") or ""),
            "cluster_id": str(raw.get("cluster_id") or raw.get("case_id") or ""),
            "event_id": str(raw.get("event_id") or ""),
            "gold": NewsTaxonomyV1.model_validate(raw.get("gold")),
            "prediction": NewsTaxonomyV1.model_validate(raw.get("prediction")),
            "gold_receipt": TaxonomyGoldReceiptV1.model_validate(raw.get("gold_receipt")),
        }
        for raw in sorted(
            raw_cases,
            key=lambda row: (int(row.get("opened_at_ms") or 0), str(row.get("case_id") or "")),
        )
    ]
    if any(not case["case_id"] or not case["cluster_id"] or not case["event_id"] for case in parsed):
        raise ValueError("news_taxonomy_case_cluster_identity_required")
    if len({case["case_id"] for case in parsed}) != len(parsed):
        raise ValueError("news_taxonomy_case_id_duplicate")
    cluster_splits: dict[str, set[str]] = defaultdict(set)
    cluster_gold: dict[str, set[str]] = defaultdict(set)
    for case in parsed:
        cluster_splits[case["cluster_id"]].add(str(case.get("split") or "development"))
        cluster_gold[case["cluster_id"]].add(canonical_sha(case["gold"].model_dump(mode="json")))
    if any(len(values) != 1 for values in cluster_splits.values()):
        raise ValueError("news_taxonomy_cluster_split_leakage")
    if any(len(values) != 1 for values in cluster_gold.values()):
        raise ValueError("news_taxonomy_cluster_gold_conflict")
    representatives: dict[str, dict[str, Any]] = {}
    for case in parsed:
        representatives.setdefault(case["cluster_id"], case)
    cases = list(representatives.values())
    development = [case for case in cases if str(case.get("split") or "development") == "development"]
    holdout = [case for case in cases if str(case.get("split") or "development") == "future_holdout"]
    scored_cases = holdout or development
    axes = {
        axis: _class_metrics(
            [(str(getattr(case["gold"], axis)), str(getattr(case["prediction"], axis))) for case in scored_cases]
        )
        for axis in _AXES
    }
    subject = _multilabel_metrics(scored_cases)
    legacy_pairs = [
        (case["gold"].event_family, project_legacy_event_type(str(case.get("legacy_event_type") or "")).event_family)
        for case in scored_cases
    ]
    legacy = _class_metrics([(str(gold), str(prediction)) for gold, prediction in legacy_pairs])

    def exact(case: Mapping[str, Any]) -> bool:
        return bool(case["gold"] == case["prediction"])

    non_abstained = [
        case
        for case in scored_cases
        if case["prediction"].subject_codes
        and case["prediction"].event_family != "other"
        and case["prediction"].change_state != "unknown"
        and case["prediction"].source_authority != "unknown"
        and case["prediction"].assertion_status != "unknown"
    ]
    risk_curve: list[dict[str, Any]] = [
        {
            "point": "all",
            "coverage": 1.0 if scored_cases else None,
            "error_rate": _safe_div(sum(not exact(case) for case in scored_cases), len(scored_cases)),
        },
        {
            "point": "non_abstain",
            "coverage": _safe_div(len(non_abstained), len(scored_cases)),
            "error_rate": _safe_div(sum(not exact(case) for case in non_abstained), len(non_abstained)),
        },
    ]
    agreement_rows = [case for case in cases if case.get("primary_taxonomy") is not None]
    agreement_n = sum(
        NewsTaxonomyV1.model_validate(case["primary_taxonomy"]) == case["gold"] for case in agreement_rows
    )
    adjudicated_n = sum(bool(case.get("adjudicated")) for case in cases)
    reviewer = {
        "primary_gold_pair_n": len(agreement_rows),
        "exact_agreement_n": agreement_n,
        "exact_agreement_rate": _safe_div(agreement_n, len(agreement_rows)),
        "adjudicated_n": adjudicated_n,
        "adjudication_rate": _safe_div(adjudicated_n, len(cases)),
    }

    def family_n(rows: Sequence[Mapping[str, Any]], labels: set[str]) -> int:
        return sum(case["gold"].event_family in labels for case in rows)

    development_checks = {
        "boundary_cluster_n": {
            "observed": sum(
                bool(case.get("is_boundary")) or str(case.get("readiness_role") or "") == "boundary"
                for case in development
            ),
            "minimum": 30,
        },
        "retention_cluster_n": {
            "observed": sum(
                bool(case.get("is_retention")) or str(case.get("readiness_role") or "") == "retention"
                for case in development
            ),
            "minimum": 100,
        },
        "negative_cluster_n": {
            "observed": sum(
                bool(case.get("is_negative")) or str(case.get("readiness_role") or "") == "negative"
                for case in development
            ),
            "minimum": 50,
        },
        "release_strata_n": {
            "observed": len(
                {str(case.get("release_stratum") or case.get("stratum") or "") for case in development} - {""}
            ),
            "minimum": 3,
        },
        "safety_uncovered_n": {
            "observed": sum(
                bool(case.get("is_safety")) and not bool(case.get("safety_covered")) for case in development
            ),
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
        "language_zh": {
            "observed": sum(str(case.get("language")) == "zh" for case in development),
            "minimum": 30,
        },
        "language_en": {
            "observed": sum(str(case.get("language")) == "en" for case in development),
            "minimum": 30,
        },
        "issuer_first_party": {
            "observed": sum(case["gold"].source_authority == "issuer_first_party" for case in development),
            "minimum": 30,
        },
        "reputable_secondary": {
            "observed": sum(case["gold"].source_authority == "reputable_secondary" for case in development),
            "minimum": 30,
        },
    }
    registration = evaluation_context.candidate_registration
    claimed_registered_values = {
        int(case["candidate_registered_at_ms"])
        for case in holdout
        if case.get("candidate_registered_at_ms") is not None
    }
    if claimed_registered_values and claimed_registered_values != {registration.registered_at_ms}:
        raise ValueError("news_taxonomy_holdout_candidate_registration_mismatch")
    registered_at_ms = registration.registered_at_ms
    if (
        registered_at_ms
        and development
        and max(int(case.get("opened_at_ms") or 0) for case in development) >= registered_at_ms
    ):
        raise ValueError("news_taxonomy_development_not_before_candidate_registration")
    holdout_duration_ms = max((int(case.get("opened_at_ms") or 0) for case in holdout), default=0) - registered_at_ms
    accepted_holdout = [case for case in holdout if bool(case.get("accepted_primary"))]
    holdout_checks = {
        "candidate_registration_present": {
            "observed": int(bool(holdout)),
            "minimum": 1,
        },
        "post_registration_violation_n": {
            "observed": sum(int(case.get("opened_at_ms") or 0) <= registered_at_ms for case in holdout),
            "maximum": 0,
        },
        "duration_ms": {"observed": max(0, holdout_duration_ms), "minimum": 24 * 3_600_000},
        "eligible_event_n": {
            "observed": sum(bool(case.get("eligible")) for case in holdout),
            "minimum": 200,
        },
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
    ready = development_ready and holdout_ready

    def family_pr(labels: set[str]) -> tuple[float | None, float | None]:
        tp = sum(
            case["gold"].event_family in labels and case["prediction"].event_family in labels for case in scored_cases
        )
        predicted = sum(case["prediction"].event_family in labels for case in scored_cases)
        support = sum(case["gold"].event_family in labels for case in scored_cases)
        return _safe_div(tp, predicted), _safe_div(tp, support)

    product_precision, product_recall = family_pr({"product_service_change"})
    financial_precision, financial_recall = family_pr({"financial_results", "guidance_outlook"})
    known_source_cases = [case for case in scored_cases if case["gold"].source_authority != "unknown"]
    known_source_accuracy = _safe_div(
        sum(case["gold"].source_authority == case["prediction"].source_authority for case in known_source_cases),
        len(known_source_cases),
    )
    event_macro_f1 = axes["event_family"]["macro_f1"]
    legacy_macro_f1 = legacy["macro_f1"]
    non_abstain_coverage = risk_curve[1]["coverage"]
    non_abstain_error = risk_curve[1]["error_rate"]
    gates = {
        "schema_cardinality_code_invalid": _gate(True, observed=0, threshold="= 0"),
        "event_family_macro_f1": _gate(
            None if not ready or event_macro_f1 is None else event_macro_f1 >= 0.85,
            observed=event_macro_f1,
            threshold=">= 0.85",
        ),
        "legacy_macro_f1_delta": _gate(
            None
            if not ready or event_macro_f1 is None or legacy_macro_f1 is None
            else event_macro_f1 - legacy_macro_f1 >= 0.05,
            observed=(
                None
                if event_macro_f1 is None or legacy_macro_f1 is None
                else round(event_macro_f1 - legacy_macro_f1, 6)
            ),
            threshold=">= 0.05",
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
            None if not ready else axes["change_state"]["accuracy"] >= 0.90,
            observed=axes["change_state"]["accuracy"],
            threshold=">= 0.90",
        ),
        "source_authority_accuracy": _gate(
            None if not ready or known_source_accuracy is None else known_source_accuracy == 1.0,
            observed=known_source_accuracy,
            threshold="= 1.00",
        ),
        "assertion_status_macro_f1": _gate(
            None
            if not ready or axes["assertion_status"]["macro_f1"] is None
            else axes["assertion_status"]["macro_f1"] >= 0.90,
            observed=axes["assertion_status"]["macro_f1"],
            threshold=">= 0.90",
        ),
        "subject_codes_micro_f1": _gate(
            None if not ready or subject["micro_f1"] is None else subject["micro_f1"] >= 0.85,
            observed=subject["micro_f1"],
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
                str(case.get("should_push") or "") in {"must_push", "must_hold"}
                and {case["gold"].assertion_status, case["prediction"].assertion_status} == {"confirmed", "rumor"}
                for case in scored_cases
            ),
            observed=sum(
                str(case.get("should_push") or "") in {"must_push", "must_hold"}
                and {case["gold"].assertion_status, case["prediction"].assertion_status} == {"confirmed", "rumor"}
                for case in scored_cases
            ),
            threshold="= 0",
        ),
        "candidate_only_critical_regression": _gate(
            None if not ready else not any(bool(case.get("critical_regression")) for case in scored_cases),
            observed=sum(bool(case.get("critical_regression")) for case in scored_cases),
            threshold="= 0",
        ),
        **{
            f"regression_{name}": {
                "outcome": evaluation_context.regression_gates[name].outcome,
                "observed": {
                    "denominator_n": evaluation_context.regression_gates[name].denominator_n,
                    "stable_failure_n": evaluation_context.regression_gates[name].stable_failure_n,
                    "candidate_failure_n": evaluation_context.regression_gates[name].candidate_failure_n,
                    "candidate_only_regression_n": evaluation_context.regression_gates[
                        name
                    ].candidate_only_regression_n,
                    "candidate_only_case_ids": list(evaluation_context.regression_gates[name].candidate_only_case_ids),
                    "gate_evidence_sha256": evaluation_context.regression_gates[name].gate_evidence_sha256,
                    "release_evidence_sha256": evaluation_context.regression_gates[name].evidence_sha256,
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
    public_cases = [
        {
            "case_id": case["case_id"],
            "cluster_id": case["cluster_id"],
            "event_id": case["event_id"],
            "evidence_version": int(case.get("evidence_version") or 0),
            "evidence_sha256": str(case.get("evidence_sha256") or ""),
            "opened_at_ms": int(case.get("opened_at_ms") or 0),
            "split": str(case.get("split") or "development"),
            "gold": case["gold"].model_dump(mode="json"),
            "prediction": case["prediction"].model_dump(mode="json"),
            "prediction_artifact_sha256": str(case.get("prediction_artifact_sha256") or ""),
            "gold_receipt": case["gold_receipt"].model_dump(mode="json"),
        }
        for case in cases
    ]
    splits: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for case in public_cases:
        splits[case["split"]].append(case)
    cluster_root_sha256 = canonical_sha(sorted(case["cluster_id"] for case in public_cases))
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
        dataset_sha256=canonical_sha(public_cases),
        cluster_root_sha256=cluster_root_sha256,
        gold_ledger_root_sha256=evaluation_context.gold_ledger_root_sha256,
    )
    return TaxonomyEvaluationReportV1(
        identity=identity,
        case_n=len(cases),
        cluster_n=len(cases),
        provider_duplicate_n=len(parsed) - len(cases),
        population_root_sha256=canonical_sha(public_cases),
        split_roots={name: canonical_sha(rows) for name, rows in sorted(splits.items())},
        axes=axes,
        subject_codes=subject,
        legacy_baseline=legacy,
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


def verify_taxonomy_gold_receipts(
    connection: Any,
    raw_cases: Sequence[Mapping[str, Any]],
) -> TaxonomyGoldVerificationV1:
    """Project evaluation cases from accepted PostgreSQL facts, never operator-declared denominators."""

    expected: dict[str, tuple[TaxonomyGoldReceiptV1, str, NewsTaxonomyV1, Mapping[str, Any]]] = {}
    for raw in raw_cases:
        receipt = TaxonomyGoldReceiptV1.model_validate(raw.get("gold_receipt"))
        event_id = str(raw.get("event_id") or "")
        if not event_id:
            raise ValueError("news_taxonomy_gold_event_id_required")
        gold = NewsTaxonomyV1.model_validate(raw.get("gold"))
        if receipt.acceptance_id in expected:
            raise ValueError("news_taxonomy_gold_acceptance_duplicate")
        expected[receipt.acceptance_id] = (receipt, event_id, gold, raw)
    from ..storage.root import NewsRepository

    repository = NewsRepository(connection)
    rows = repository.taxonomy_gold_sources(list(expected))
    if len(rows) != len(expected):
        raise ValueError("news_taxonomy_gold_acceptance_missing")
    from .contracts import DatasetCaseRef
    from .dataset import _fact_cluster
    from .projection import _connected_fact_clusters

    verified: list[dict[str, Any]] = []
    drafts: list[tuple[DatasetCaseRef, str, str]] = []
    raw_by_review: dict[str, Mapping[str, Any]] = {}
    row_by_review: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        acceptance_id = str(row["acceptance_id"])
        receipt, event_id, gold, raw = expected[acceptance_id]
        payload = dict(row["payload"] or {})
        actual_taxonomy = NewsTaxonomyV1.model_validate(payload.get("taxonomy"))
        if (
            str(row["accepts_review_id"] or "") != receipt.review_id
            or str(row["review_id"] or "") != receipt.review_id
            or str(row["event_id"] or "") != event_id
            or str(row["rubric_version"] or "") != receipt.rubric_version
            or str(row["reviewer"] or "") != receipt.reviewer
            or int(row["accepted_at_ms"] or 0) != receipt.accepted_at_ms
            or not bool(row["acceptance_release_eligible"])
            or not bool(row["judgment_release_eligible"])
            or actual_taxonomy != gold
            or str(row["event_kind"] or "") != "news"
            or str(row["ingest_mode"] or "") != "live"
            or not bool(row["evidence_release_eligible"])
            or int(raw.get("opened_at_ms") or -1) != int(row["opened_at_ms"])
            or int(raw.get("evidence_version") or -1) != int(row["evidence_version"])
        ):
            raise ValueError("news_taxonomy_gold_acceptance_mismatch")
        snapshot = dict(row["evidence_snapshot"] or {})
        focus_fact = dict(snapshot.get("focus_fact") or {})
        case_id = canonical_sha(
            {
                "subject_kind": "event",
                "event_id": event_id,
                "external_snapshot_id": None,
                "evidence_sha256": str(row["evidence_sha256"]),
                "review_id": receipt.review_id,
            }
        )
        if str(raw.get("case_id") or "") != case_id:
            raise ValueError("news_taxonomy_gold_case_identity_mismatch")
        selection = dict(row["selection"] or {})
        novelty = dict(row["novelty"] or {})
        case_ref = DatasetCaseRef(
            case_id=case_id,
            subject_kind="event",
            event_id=event_id,
            evidence_version=int(row["evidence_version"]),
            evidence_sha256=str(row["evidence_sha256"]),
            review_id=receipt.review_id,
            cluster_id=_fact_cluster(str(focus_fact.get("text") or "")),
            stratum=str(selection.get("stratum") or "eventless_miss"),
            should_push=str(row["should_push"] or "uncertain"),
            opened_at_ms=int(row["opened_at_ms"]),
            delivery_truth="unknown",
        )
        duplicate_of = (
            str(novelty.get("duplicate_of") or "") if str(novelty.get("judgment") or "") == "restatement" else ""
        )
        source_identity = canonical_sha(
            {
                "url": (snapshot.get("card") or {}).get("leader_url"),
                "focus_fact_id": focus_fact.get("fact_id"),
            }
        )
        drafts.append((case_ref, duplicate_of, source_identity))
        raw_by_review[receipt.review_id] = raw
        row_by_review[receipt.review_id] = dict(row)
        verified.append(
            {
                "acceptance_id": acceptance_id,
                "review_id": receipt.review_id,
                "event_id": event_id,
                "rubric_version": receipt.rubric_version,
                "reviewer": receipt.reviewer,
                "accepted_at_ms": receipt.accepted_at_ms,
                "taxonomy": gold.model_dump(mode="json"),
                "case_id": case_id,
                "evidence_version": int(row["evidence_version"]),
                "evidence_sha256": str(row["evidence_sha256"]),
                "opened_at_ms": int(row["opened_at_ms"]),
            }
        )
    connected = _connected_fact_clusters(drafts)
    sealed_cases: list[dict[str, Any]] = []
    for case_ref in connected:
        raw = raw_by_review[case_ref.review_id]
        row = dict(row_by_review[case_ref.review_id])
        if str(raw.get("cluster_id") or "") != case_ref.cluster_id:
            raise ValueError("news_taxonomy_gold_cluster_identity_mismatch")
        dimensions = dict(row.get("dimensions") or {})
        novelty = dict(row.get("novelty") or {})
        payload = dict(row.get("payload") or {})
        taxonomy_review = dict(payload.get("taxonomy_review") or {})
        should_push = str(row.get("should_push") or "uncertain")
        is_boundary = (
            should_push in {"must_push", "must_hold"}
            or "fail" in dimensions.values()
            or bool(row.get("expected_correction"))
        )
        is_negative = should_push in {"should_hold", "must_hold"} or novelty.get("judgment") == "restatement"
        is_safety = should_push in {"must_push", "must_hold"} or dimensions.get("factual_fidelity") == "fail"
        snapshot = dict(row.get("evidence_snapshot") or {})
        focus_text = str((snapshot.get("focus_fact") or {}).get("text") or "")
        verdict = dict(row.get("verdict") or {})
        primary_payload = dict(row.get("primary_payload") or {})
        gold_taxonomy = NewsTaxonomyV1.model_validate(payload.get("taxonomy"))
        prediction = NewsTaxonomyV1.model_validate(raw.get("prediction"))
        primary_taxonomy = (
            NewsTaxonomyV1.model_validate(primary_payload.get("taxonomy"))
            if primary_payload.get("taxonomy") is not None
            else None
        )
        critical_regression = bool(
            primary_taxonomy == gold_taxonomy
            and prediction != gold_taxonomy
            and taxonomy_requires_independent_adjudication(
                gold_taxonomy,
                legacy_event_type=str(verdict.get("event_type") or ""),
                draft_taxonomy=prediction,
            )
        )
        sealed_cases.append(
            {
                "case_id": case_ref.case_id,
                "cluster_id": case_ref.cluster_id,
                "event_id": case_ref.event_id,
                "evidence_version": case_ref.evidence_version,
                "evidence_sha256": case_ref.evidence_sha256,
                "opened_at_ms": case_ref.opened_at_ms,
                "split": str(raw.get("split") or "development"),
                "candidate_registered_at_ms": raw.get("candidate_registered_at_ms"),
                "release_stratum": case_ref.stratum,
                "is_boundary": is_boundary,
                "is_retention": not is_boundary,
                "is_negative": is_negative,
                "is_safety": is_safety,
                "safety_covered": not is_safety or bool(row.get("judgment_release_eligible")),
                "eligible": True,
                "accepted_primary": True,
                "language": "zh" if any("\u4e00" <= char <= "\u9fff" for char in focus_text) else "en",
                "source_authority": payload["taxonomy"]["source_authority"],
                "audience": verdict.get("audience") or "unknown",
                "scope": verdict.get("scope") or "unknown",
                "legacy_event_type": verdict.get("event_type") or "",
                "should_push": should_push,
                "primary_taxonomy": (
                    primary_taxonomy.model_dump(mode="json") if primary_taxonomy is not None else None
                ),
                "adjudicated": taxonomy_review.get("review_role") == "adjudication",
                "critical_regression": critical_regression,
                "gold": payload["taxonomy"],
                "prediction": prediction.model_dump(mode="json"),
                "gold_receipt": raw["gold_receipt"],
            }
        )
    return TaxonomyGoldVerificationV1(
        ledger_root_sha256=canonical_sha(verified),
        cases=tuple(sealed_cases),
    )


def verify_taxonomy_active_deployment(
    connection: Any,
    *,
    stable_bundle_sha256: str,
) -> TaxonomyDeploymentReceiptV1:
    """Bind candidate registration to the exact image Workers appointed in PostgreSQL."""

    from ..storage.root import NewsRepository

    row = NewsRepository(connection).taxonomy_active_deployment()
    if row is None:
        raise ValueError("news_taxonomy_active_deployment_missing")
    active = dict(row.get("active_agent_payload") or {})
    deployment = dict(row.get("deployment_payload") or {})
    active_sha = str(row.get("active_agent_sha") or "")
    deployment_sha = str(row.get("deployment_receipt_sha") or "")
    runtime_revision = str(active.get("runtime_revision") or "")
    image_digest = str(active.get("image_digest") or "")
    runtime_manifest = str(active.get("runtime_manifest_sha") or "")
    candidate_shas = sorted(str(value) for value in active.get("candidate_shas") or ())
    manifest_candidate_shas = sorted(str(value) for value in row.get("manifest_candidate_shas") or ())
    registered_at_ms = int(active.get("registered_at_ms") or -1)
    if (
        active_sha != canonical_sha({"kind": "active_agent", "payload": active})
        or deployment_sha != canonical_sha({"kind": "deployment_receipt", "payload": deployment})
        or str(row.get("deployment_parent_sha") or "") != active_sha
        or deployment.get("action") != "runtime_deploy"
        or str(active.get("stable_sha") or "") != stable_bundle_sha256
        or str(deployment.get("stable_sha") or "") != stable_bundle_sha256
        or str(deployment.get("active_agent_sha") or "") != active_sha
        or str(deployment.get("image_digest") or "") != image_digest
        or str(deployment.get("runtime_revision") or "") != runtime_revision
        or int(deployment.get("deployed_at_ms") or -1) != registered_at_ms
        or str(row.get("manifest_sha") or "") != runtime_manifest
        or str(row.get("manifest_stable_bundle_sha") or "") != stable_bundle_sha256
        or manifest_candidate_shas != candidate_shas
        or str(row.get("manifest_image_digest") or "") != image_digest
        or str(row.get("manifest_runtime_revision") or "") != runtime_revision
        or int(row.get("manifest_registered_at_ms") or -1) != registered_at_ms
        or runtime_manifest
        != runtime_manifest_sha(
            stable_bundle_sha=stable_bundle_sha256,
            candidate_shas=candidate_shas,
            image_digest=image_digest,
            runtime_revision=runtime_revision,
        )
    ):
        raise ValueError("news_taxonomy_active_deployment_mismatch")
    try:
        return TaxonomyDeploymentReceiptV1(
            tested_git_sha=runtime_revision,
            stable_bundle_sha256=stable_bundle_sha256,
            runtime_manifest_sha256=runtime_manifest,
            image_digest=image_digest,
            deployment_receipt_sha256=deployment_sha,
        )
    except ValueError as exc:
        raise ValueError("news_taxonomy_active_deployment_unversioned") from exc


def verify_taxonomy_candidate_registration(
    connection: Any,
    artifact_sha256: str,
    *,
    code_identity: TaxonomyCodeIdentityV1,
    stable_bundle_sha256: str,
    runtime_model_bindings_sha256: str,
    policy_sha256: str,
) -> TaxonomyCandidateRegistrationV1:
    """Load one durable pre-holdout registration and bind it to this executable bundle."""

    from ..storage.root import NewsRepository

    row = NewsRepository(connection).taxonomy_candidate_registration(artifact_sha256)
    if row is None or str(row["kind"]) != "candidate_registration":
        raise ValueError("news_taxonomy_candidate_registration_missing")
    payload = dict(row["payload"] or {})
    registration = TaxonomyCandidateRegistrationV1.model_validate(payload)
    deployment = verify_taxonomy_active_deployment(
        connection,
        stable_bundle_sha256=stable_bundle_sha256,
    )
    expected = TaxonomyCandidateRegistrationV1.issue(
        code_identity=code_identity,
        deployment=deployment,
        policy_sha256=policy_sha256,
        runtime_model_bindings_sha256=runtime_model_bindings_sha256,
        taxonomy_program_sha256=registration.taxonomy_program_sha256,
        taxonomy_model_binding_sha256=registration.taxonomy_model_binding_sha256,
        registered_at_ms=registration.registered_at_ms,
    )
    if (
        registration.artifact_sha256 != artifact_sha256
        or registration.model_dump(mode="json") != payload
        or registration.registered_at_ms != int(row["created_at_ms"])
        or registration != expected
    ):
        raise ValueError("news_taxonomy_candidate_registration_mismatch")
    return registration


def verify_taxonomy_regression_gates(
    connection: Any,
    raw_gates: Mapping[str, Any],
    *,
    code_identity: TaxonomyCodeIdentityV1,
    registration: TaxonomyCandidateRegistrationV1,
) -> dict[str, TaxonomyRegressionGateReceiptV1]:
    """Derive four regression receipts from current content-addressed PostgreSQL evidence."""

    if not isinstance(raw_gates, Mapping) or set(raw_gates) != set(_REGRESSION_GATES):
        raise ValueError("news_taxonomy_regression_gate_set_invalid")
    references = {name: TaxonomyRegressionGateReferenceV1.model_validate(raw_gates[name]) for name in _REGRESSION_GATES}
    from ..storage.root import NewsRepository

    rows = NewsRepository(connection).taxonomy_regression_sources(
        sorted({reference.evidence_sha256 for reference in references.values()})
    )
    by_sha = {str(row["evidence_sha"]): dict(row) for row in rows}
    if set(by_sha) != {reference.evidence_sha256 for reference in references.values()}:
        raise ValueError("news_taxonomy_regression_evidence_missing")

    from .contracts import CandidateManifest
    from .profile import TRUSTED_ROOT_SHA

    verified: dict[str, TaxonomyRegressionGateReceiptV1] = {}
    for name, reference in references.items():
        row = by_sha[reference.evidence_sha256]
        release = dict(row.get("evidence_payload") or {})
        report = dict(row.get("report_payload") or {})
        evidence = dict(report.get("evidence") or {})
        raw_regression_gates = dict(evidence.get("regression_gates") or {})
        try:
            gate_evidence = ProductionRegressionGateEvidenceV1.model_validate(raw_regression_gates.get(name))
        except ValueError as exc:
            raise ValueError("news_taxonomy_regression_gate_evidence_invalid") from exc
        candidate_payload = dict(row.get("candidate_payload") or {})
        stage = str(release.get("stage") or "")
        development_sha = str(evidence.get("development_dataset_sha") or "")
        development_payload = dict(row.get("development_payload") or {})
        dataset_sha = str(development_sha if stage == "offline" else evidence.get("validation_dataset_sha") or "")
        dataset_payload = dict(
            (row.get("development_payload") if stage == "offline" else row.get("validation_payload")) or {}
        )
        candidate = CandidateManifest.model_validate(candidate_payload.get("manifest"))
        report_sha = str(row.get("report_sha") or "")
        candidate_sha = str(release.get("candidate_sha") or "")
        release_outcome = str(release.get("gate_outcome") or "")
        if (
            reference.evidence_sha256 != canonical_sha({"kind": "release_evidence", "payload": release})
            or str(row.get("evidence_parent_sha") or "") != report_sha
            or str(release.get("report_sha") or "") != report_sha
            or report_sha != canonical_sha({"kind": "evaluation_report", "payload": report})
            or str(row.get("report_parent_sha") or "") != candidate_sha
            or str(report.get("gate_outcome") or "") != release_outcome
            or release_outcome not in {"pass", "fail", "unknown"}
            or stage not in {"offline", "holdout", "shadow", "canary"}
            or (release_outcome == "pass" and report.get("run_state") != "complete")
            or (release_outcome == "pass" and report.get("eligibility") != "current")
            or str(release.get("trusted_root_sha") or "") != TRUSTED_ROOT_SHA
            or str(evidence.get("trusted_root_sha") or "") != TRUSTED_ROOT_SHA
            or str(evidence.get("candidate_sha") or "") != candidate_sha
            or candidate.candidate_sha != candidate_sha
            or str(candidate_payload.get("candidate_sha") or "") != candidate_sha
            or str(row.get("candidate_parent_sha") or "") != candidate.parent_stable_sha
            or str(evidence.get("stable_sha") or "") != registration.stable_bundle_sha256
            or candidate.parent_stable_sha != registration.stable_bundle_sha256
            or candidate.development_dataset_sha != development_sha
            or str(row.get("development_artifact_sha") or "") != development_sha
            or development_sha != canonical_sha({"kind": "dataset", "payload": development_payload})
            or not dataset_sha
            or str(
                row.get("development_artifact_sha") if stage == "offline" else row.get("validation_artifact_sha") or ""
            )
            != dataset_sha
            or dataset_sha != canonical_sha({"kind": "dataset", "payload": dataset_payload})
            or str(evidence.get("metric_id") or "") != registration.metric_id
            or str(evidence.get("metric_sha256") or "") != registration.metric_sha256
            or set(raw_regression_gates) != set(_REGRESSION_GATES)
            or gate_evidence.gate != name
            or gate_evidence.metric_id != registration.metric_id
            or gate_evidence.metric_sha256 != registration.metric_sha256
            or registration.metric_id != code_identity.metric_id
            or registration.metric_sha256 != code_identity.metric_sha256
            or str(row.get("candidate_artifact_sha") or "")
            != canonical_sha({"kind": "candidate", "payload": candidate_payload})
        ):
            raise ValueError("news_taxonomy_regression_evidence_mismatch")
        verified[name] = TaxonomyRegressionGateReceiptV1(
            gate=name,
            outcome=gate_evidence.outcome.upper(),
            evidence_sha256=reference.evidence_sha256,
            gate_evidence_sha256=gate_evidence.evidence_sha256,
            report_sha256=report_sha,
            candidate_sha256=candidate_sha,
            dataset_sha256=dataset_sha,
            metric_id=registration.metric_id,
            metric_sha256=registration.metric_sha256,
            denominator_n=gate_evidence.denominator_n,
            stable_failure_n=gate_evidence.stable_failure_n,
            candidate_failure_n=gate_evidence.candidate_failure_n,
            candidate_only_regression_n=gate_evidence.candidate_only_regression_n,
            candidate_only_case_ids=gate_evidence.candidate_only_case_ids,
        )
    TaxonomyEvaluationContextV1(
        candidate_registration_sha256=registration.artifact_sha256,
        candidate_registration=registration,
        gold_ledger_root_sha256="0" * 64,
        regression_gates=verified,
    )
    return verified


def verify_taxonomy_evaluation_cases(
    connection: Any,
    raw_cases: Sequence[Mapping[str, Any]],
    *,
    registration: TaxonomyCandidateRegistrationV1,
) -> TaxonomyGoldVerificationV1:
    """Bind every score to an exact replayable shadow artifact and accepted Gold projection."""

    gold = verify_taxonomy_gold_receipts(connection, raw_cases)
    raw_by_case = {str(raw.get("case_id") or ""): raw for raw in raw_cases}
    prediction_shas = [str(raw.get("prediction_artifact_sha256") or "") for raw in raw_cases]
    if any(not value for value in prediction_shas) or len(set(prediction_shas)) != len(prediction_shas):
        raise ValueError("news_taxonomy_shadow_artifact_identity_required")
    from ..storage.root import NewsRepository

    artifact_rows = NewsRepository(connection).taxonomy_shadow_artifacts(prediction_shas)
    artifacts = {str(row["artifact_sha"]): dict(row) for row in artifact_rows}
    if len(artifacts) != len(prediction_shas):
        raise ValueError("news_taxonomy_shadow_artifact_missing")
    sealed_cases: list[dict[str, Any]] = []
    for case in gold.cases:
        raw = raw_by_case[str(case["case_id"])]
        artifact_sha = str(raw["prediction_artifact_sha256"])
        artifact = artifacts[artifact_sha]
        payload = dict(artifact["payload"] or {})
        if str(artifact["kind"]) != "shadow_observation" or artifact_sha != canonical_sha(
            {"kind": "shadow_observation", "payload": payload}
        ):
            raise ValueError("news_taxonomy_shadow_artifact_identity_mismatch")
        observation = TaxonomyShadowObservationV1.model_validate(payload)
        model_binding_sha256 = canonical_sha(
            {
                "model_identity": observation.model_identity.model_dump(mode="json"),
                "model_binding": observation.model_binding,
            }
        )
        if (
            observation.event_id != str(case["event_id"])
            or observation.evidence_version != int(case["evidence_version"])
            or observation.evidence_sha256 != str(case["evidence_sha256"])
            or observation.shadow_program_sha256 != registration.taxonomy_program_sha256
            or model_binding_sha256 != registration.taxonomy_model_binding_sha256
            or int(artifact["created_at_ms"]) < registration.registered_at_ms
            or NewsTaxonomyV1.model_validate(raw.get("prediction")) != observation.taxonomy
        ):
            raise ValueError("news_taxonomy_shadow_artifact_mismatch")
        sealed_cases.append(
            {
                **case,
                "prediction": observation.taxonomy.model_dump(mode="json"),
                "prediction_artifact_sha256": artifact_sha,
            }
        )
    return TaxonomyGoldVerificationV1(
        ledger_root_sha256=gold.ledger_root_sha256,
        cases=tuple(sealed_cases),
    )


__all__ = [
    "TAXONOMY_CANDIDATE_REGISTRATION_SCHEMA",
    "TAXONOMY_EVALUATION_SCHEMA",
    "TAXONOMY_SHADOW_INSTRUCTION",
    "TAXONOMY_SHADOW_SCHEMA",
    "TaxonomyCandidateRegistrationV1",
    "TaxonomyCodeIdentityV1",
    "TaxonomyDeploymentReceiptV1",
    "TaxonomyEvaluationContextV1",
    "TaxonomyEvaluationIdentityV1",
    "TaxonomyEvaluationReportV1",
    "TaxonomyGoldReceiptV1",
    "TaxonomyGoldVerificationV1",
    "TaxonomyRegressionGateReceiptV1",
    "TaxonomyRegressionGateReferenceV1",
    "TaxonomyShadowObservationV1",
    "TaxonomyShadowProgramV1",
    "TaxonomyShadowSignature",
    "build_taxonomy_evaluation_report",
    "taxonomy_code_identity",
    "verify_taxonomy_active_deployment",
    "verify_taxonomy_candidate_registration",
    "verify_taxonomy_evaluation_cases",
    "verify_taxonomy_gold_receipts",
    "verify_taxonomy_regression_gates",
]
