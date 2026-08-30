"""Taxonomy candidate registration and durable release-evidence verification."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..artifact_identity import canonical_sha, runtime_manifest_sha
from ..models import TRIAGE_POLICY_VERSION
from ..program.artifact import load_stable_program_artifact
from ..program.identity import EXECUTION_ENVELOPE_SHA256
from ..program.runtime import PROGRAM_VERSION
from ..review.desk import REVIEW_RUBRIC_VERSION, taxonomy_requires_independent_adjudication
from ..taxonomy import (
    IPTC_CODEBOOK_SHA256,
    TAXONOMY_VERSION,
    IPTCCodebookSha,
    NewsTaxonomyV1,
)
from .metric import (
    METRIC_ID,
    PRODUCTION_REGRESSION_GATES,
    ProductionRegressionGateEvidenceV1,
    metric_contract_sha256,
)
from .taxonomy_shadow import TaxonomyShadowObservationV2, TaxonomyShadowPopulationV1

TAXONOMY_CANDIDATE_REGISTRATION_SCHEMA: Final = "tracefold.news.taxonomy_candidate_registration.v1"


class _ExactModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TaxonomyGoldReceiptV1(_ExactModel):
    review_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    acceptance_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    rubric_version: Literal["news_review_v6"] = "news_review_v6"
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
    shadow_population: TaxonomyShadowPopulationV1 | None = None


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
    review_rubric_version: Literal["news_review_v6"] = "news_review_v6"
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
    review_rubric_version: Literal["news_review_v6"] = "news_review_v6"
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
        primary_taxonomy = (
            NewsTaxonomyV1.model_validate(primary_payload.get("taxonomy"))
            if primary_payload.get("taxonomy") is not None
            else None
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
                "should_push": should_push,
                "primary_taxonomy": (
                    primary_taxonomy.model_dump(mode="json") if primary_taxonomy is not None else None
                ),
                "adjudicated": taxonomy_review.get("review_role") == "adjudication",
                "gold": payload["taxonomy"],
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
    duplicate_shas = {value for value in prediction_shas if value and prediction_shas.count(value) > 1}
    from ..storage.root import NewsRepository

    artifact_rows = NewsRepository(connection).taxonomy_shadow_artifacts(sorted(set(prediction_shas) - {""}))
    artifacts = {str(row["artifact_sha"]): dict(row) for row in artifact_rows}
    sealed_cases: list[dict[str, Any]] = []
    observations: list[TaxonomyShadowObservationV2] = []
    missing_observation_n = 0
    invalid_observation_n = 0
    for case in gold.cases:
        raw = raw_by_case[str(case["case_id"])]
        artifact_sha = str(raw.get("prediction_artifact_sha256") or "")
        artifact = artifacts.get(artifact_sha)
        if not artifact_sha or artifact is None:
            missing_observation_n += 1
            continue
        if artifact_sha in duplicate_shas:
            invalid_observation_n += 1
            continue
        try:
            payload = dict(artifact["payload"] or {})
            if str(artifact["kind"]) != "shadow_observation" or artifact_sha != canonical_sha(
                {"kind": "shadow_observation", "payload": payload}
            ):
                raise ValueError("news_taxonomy_shadow_artifact_identity_mismatch")
            observation = TaxonomyShadowObservationV2.model_validate(payload)
        except (KeyError, TypeError, ValueError):
            invalid_observation_n += 1
            continue
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
        ):
            invalid_observation_n += 1
            continue
        observations.append(observation)
        if observation.outcome != "success":
            continue
        prediction = observation.taxonomy
        if prediction is None:  # pragma: no cover - observation validation owns this invariant
            invalid_observation_n += 1
            continue
        gold_taxonomy = NewsTaxonomyV1.model_validate(case["gold"])
        primary_taxonomy = (
            NewsTaxonomyV1.model_validate(case["primary_taxonomy"])
            if case.get("primary_taxonomy") is not None
            else None
        )
        sealed_cases.append(
            {
                **case,
                "prediction": prediction.model_dump(mode="json"),
                "prediction_artifact_sha256": artifact_sha,
                "critical_regression": bool(
                    primary_taxonomy == gold_taxonomy
                    and prediction != gold_taxonomy
                    and taxonomy_requires_independent_adjudication(
                        gold_taxonomy,
                        draft_taxonomy=prediction,
                    )
                ),
            }
        )
    population = TaxonomyShadowPopulationV1.issue(
        observations,
        eligible_case_n=len(gold.cases),
        missing_observation_n=missing_observation_n,
        invalid_observation_n=invalid_observation_n,
    )
    return TaxonomyGoldVerificationV1(
        ledger_root_sha256=gold.ledger_root_sha256,
        cases=tuple(sealed_cases),
        shadow_population=population,
    )


__all__ = [
    "TAXONOMY_CANDIDATE_REGISTRATION_SCHEMA",
    "TaxonomyCandidateRegistrationV1",
    "TaxonomyCodeIdentityV1",
    "TaxonomyDeploymentReceiptV1",
    "TaxonomyGoldReceiptV1",
    "TaxonomyGoldVerificationV1",
    "TaxonomyRegressionGateReceiptV1",
    "TaxonomyRegressionGateReferenceV1",
    "taxonomy_code_identity",
    "verify_taxonomy_active_deployment",
    "verify_taxonomy_candidate_registration",
    "verify_taxonomy_evaluation_cases",
    "verify_taxonomy_gold_receipts",
    "verify_taxonomy_regression_gates",
]
