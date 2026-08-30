"""Closed inputs and stable results for the single Production V3 read-only verifier.

The verifier never grants capital and never calls a venue.  Operators hand it immutable evidence
identities; it reads PostgreSQL truth and returns one canonical set of checks.  A failed check is a
named terminal, not a warning that a caller may reinterpret as success.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .bindings import binding_for_source_venue
from .contracts import FixedWindowSourceFactV1, VenueBinding, canonical_sha256
from .evidence_clock import (
    CANDIDATE_DECISION_ADAPTER,
    CandidateDecisionReceiptV1,
    CandidateLockedV1,
    DiscoveryCorpusArtifactV1,
    DiscoveryCorpusReceiptV1,
    EvidenceCaptureArtifactV1,
    EvidenceDrainArtifactV1,
    FutureCaptureReceiptV1,
    FutureDrainReceiptV1,
    FutureHoldoutResultReceiptV1,
    FutureHoldoutResultV1,
)

SEVEN_DAYS_MS = 7 * 86_400_000


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class FixedWindowAcceptanceV1(_Frozen):
    """The preregistered, non-moving operational accounting window."""

    window_version: Literal["production_v3_fixed_window_v1"] = "production_v3_fixed_window_v1"
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    drain_cutoff_ms: int = Field(gt=0)
    release_tag: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    git_commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    oci_image_digest: str = Field(pattern=r"^(?:[^@\s]+@)?sha256:[0-9a-f]{64}$")
    gate_version: str = Field(min_length=1, max_length=128)
    gate_config_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    minimum_source_count: int = Field(ge=1)
    minimum_case_count: int = Field(ge=1)
    minimum_intent_count: int = Field(ge=1)
    minimum_closed_flat_count: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_fixed_window(self) -> Self:
        if self.end_ms - self.start_ms != SEVEN_DAYS_MS:
            raise ValueError("evidence_acceptance_window_not_seven_days")
        if self.drain_cutoff_ms < self.end_ms:
            raise ValueError("evidence_acceptance_drain_before_window_end")
        if not (
            self.minimum_source_count
            >= self.minimum_case_count
            >= self.minimum_intent_count
            >= self.minimum_closed_flat_count
        ):
            raise ValueError("evidence_acceptance_activity_floor_invalid")
        return self

    @property
    def window_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


class ReleaseBindingIdentityV1(_Frozen):
    binding: VenueBinding
    catalog_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    capability_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    account_identity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    account_generation: int = Field(ge=1)
    adapter_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    client_runtime_identity: str = Field(min_length=1, max_length=256)


class NautilusRuntimeStartV1(_Frozen):
    """One append-only process generation used to prove a real restart drill."""

    start_version: Literal["nautilus_runtime_start_v1"] = "nautilus_runtime_start_v1"
    runtime_id: UUID
    runtime_revision: str = Field(min_length=1, max_length=256)
    image_digest: str = Field(min_length=1, max_length=256)
    nautilus_version: str = Field(min_length=1, max_length=64)
    nautilus_source_git_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    nautilus_wheel_identity: str = Field(min_length=1, max_length=256)
    started_at_ms: int = Field(gt=0)

    @property
    def start_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


class CanaryRestartDrillReceiptV1(_Frozen):
    """Operator receipt bound to two durable Nautilus process generations and one canary."""

    receipt_version: Literal["canary_restart_drill_receipt_v1"] = "canary_restart_drill_receipt_v1"
    intent_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    binding: VenueBinding
    protected_runtime_id: UUID
    recovered_runtime_id: UUID
    stopped_at_ms: int = Field(gt=0)
    reconciled_at_ms: int = Field(gt=0)
    operator: str = Field(min_length=1, max_length=128)
    statement: Literal["QUERY_FIRST_ZERO_DUPLICATE_AUTHORITATIVE_CLOSED_FLAT_AFTER_PROCESS_RESTART"]
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_restart(self) -> Self:
        if self.protected_runtime_id == self.recovered_runtime_id:
            raise ValueError("evidence_restart_runtime_not_changed")
        if self.stopped_at_ms >= self.reconciled_at_ms:
            raise ValueError("evidence_restart_clock_invalid")
        unsigned = self.model_dump(mode="json", exclude={"receipt_sha256"})
        if self.receipt_sha256 != canonical_sha256(unsigned):
            raise ValueError("evidence_restart_receipt_identity_invalid")
        return self


class ProductionReleaseCandidateV1(_Frozen):
    """Exact immutable release observed by one fixed acceptance window."""

    release_version: Literal["production_v3_release_candidate_v1"] = "production_v3_release_candidate_v1"
    release_tag: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    git_commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    git_tree_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    oci_image_digest: str = Field(pattern=r"^(?:[^@\s]+@)?sha256:[0-9a-f]{64}$")
    migration_head: str = Field(pattern=r"^[0-9]{8}_[0-9]{4}$")
    openapi_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    web_assets_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    workers_runtime_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    serve_runtime_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    nautilus_wheel_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    nautilus_source_git_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    execution_contract_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    quote_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    protection_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bindings: tuple[ReleaseBindingIdentityV1, ...] = Field(min_length=1, max_length=2)
    corpus_receipt_sha256s: tuple[str, ...] = Field(min_length=1)
    future_result_receipt_sha256s: tuple[str, ...] = Field(min_length=1)
    promotion_grant_sha256s: tuple[str, ...] = Field(min_length=1)
    risk_policy_sha256s: tuple[str, ...] = Field(min_length=1)
    canary_intent_ids: tuple[str, ...] = Field(min_length=1)
    restart_drill: CanaryRestartDrillReceiptV1
    acceptance_window: FixedWindowAcceptanceV1
    approval_statement: Literal["I_APPROVE_EXACT_RELEASE_CANDIDATE_FOR_FIXED_ACCEPTANCE_WINDOW"]
    approved_by: str = Field(min_length=1, max_length=128)
    approved_at_ms: int = Field(gt=0)
    approval_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_release(self) -> Self:
        if self.workers_runtime_revision != self.git_commit_sha or self.serve_runtime_revision != self.git_commit_sha:
            raise ValueError("evidence_release_runtime_revision_mismatch")
        if self.bindings != tuple(sorted(self.bindings, key=lambda row: row.binding)):
            raise ValueError("evidence_release_bindings_not_canonical")
        if len({row.binding for row in self.bindings}) != len(self.bindings):
            raise ValueError("evidence_release_binding_duplicate")
        for values in (
            self.corpus_receipt_sha256s,
            self.future_result_receipt_sha256s,
            self.promotion_grant_sha256s,
            self.risk_policy_sha256s,
            self.canary_intent_ids,
        ):
            if values != tuple(sorted(set(values))):
                raise ValueError("evidence_release_receipts_not_canonical")
        if self.restart_drill.intent_id not in self.canary_intent_ids:
            raise ValueError("evidence_release_restart_canary_missing")
        if self.restart_drill.binding not in {row.binding for row in self.bindings}:
            raise ValueError("evidence_release_restart_binding_missing")
        if self.restart_drill.reconciled_at_ms > self.approved_at_ms:
            raise ValueError("evidence_release_approved_before_restart_reconciled")
        if (
            self.acceptance_window.release_tag != self.release_tag
            or self.acceptance_window.git_commit_sha != self.git_commit_sha
            or self.acceptance_window.oci_image_digest != self.oci_image_digest
            or self.acceptance_window.start_ms < self.approved_at_ms
        ):
            raise ValueError("evidence_release_acceptance_identity_mismatch")
        unsigned = self.model_dump(mode="json", exclude={"approval_sha256"})
        if self.approval_sha256 != canonical_sha256(unsigned):
            raise ValueError("evidence_release_approval_identity_invalid")
        return self

    @property
    def release_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


class ServeRuntimeObservationV1(_Frozen):
    """Authenticated read-only observation of the actual Serve process under acceptance."""

    runtime_id: UUID
    runtime_revision: str = Field(min_length=1, max_length=256)
    image_digest: str = Field(min_length=1, max_length=256)
    started_at_ms: int = Field(gt=0)
    measured_at_ms: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_clock(self) -> Self:
        if self.measured_at_ms < self.started_at_ms:
            raise ValueError("evidence_serve_runtime_clock_invalid")
        return self


class ReleaseVerificationObservationsV1(_Frozen):
    """Typed non-PostgreSQL release facts mechanically observed by App."""

    tag_signature_valid: bool
    tag_commit: str | None = None
    tag_tree: str | None = None
    runtime_revision: str
    image_digest: str
    openapi_sha256: str | None = None
    web_assets_sha256: str | None = None
    nautilus_wheel_sha256: str | None = None
    nautilus_source_git_commit: str
    execution_contract_receipt_sha256: str
    execution_policy_sha256: str
    quote_contract_sha256: str
    protection_contract_sha256: str
    policy_config_sha256: str
    receipt_chains_valid: bool
    serve_runtime: ServeRuntimeObservationV1


@dataclass(frozen=True, slots=True)
class PreparedProductionReleaseRegistration:
    """Canonical release/window registration bytes prepared outside the write transaction."""

    release_sha256: str
    window_sha256: str
    release_tag: str
    git_commit_sha: str
    oci_image_digest: str
    window_start_ms: int
    window_end_ms: int
    serve_runtime_id: UUID
    serve_runtime_revision: str
    serve_image_digest: str
    serve_started_at_ms: int
    serve_measured_at_ms: int
    payload_json: str

    def __post_init__(self) -> None:
        try:
            payload = json.loads(self.payload_json)
            release = ProductionReleaseCandidateV1.model_validate(payload["release"])
            serve = ServeRuntimeObservationV1.model_validate(payload["serve_runtime"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("evidence_release_registration_prepared_identity_invalid") from exc
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        if (
            payload.get("registration_version"),
            payload.get("release_sha256"),
            payload.get("window_sha256"),
            release.release_sha256,
            release.acceptance_window.window_sha256,
            release.release_tag,
            release.git_commit_sha,
            release.oci_image_digest,
            release.acceptance_window.start_ms,
            release.acceptance_window.end_ms,
            serve.runtime_id,
            serve.runtime_revision,
            serve.image_digest,
            serve.started_at_ms,
            serve.measured_at_ms,
            canonical,
        ) != (
            "production_release_registration_v1",
            self.release_sha256,
            self.window_sha256,
            self.release_sha256,
            self.window_sha256,
            self.release_tag,
            self.git_commit_sha,
            self.oci_image_digest,
            self.window_start_ms,
            self.window_end_ms,
            self.serve_runtime_id,
            self.serve_runtime_revision,
            self.serve_image_digest,
            self.serve_started_at_ms,
            self.serve_measured_at_ms,
            self.payload_json,
        ):
            raise ValueError("evidence_release_registration_prepared_identity_invalid")


def prepare_production_release_registration(
    release: ProductionReleaseCandidateV1,
    serve: ServeRuntimeObservationV1,
) -> PreparedProductionReleaseRegistration:
    payload = {
        "registration_version": "production_release_registration_v1",
        "release_sha256": release.release_sha256,
        "window_sha256": release.acceptance_window.window_sha256,
        "release": release.model_dump(mode="json"),
        "serve_runtime": serve.model_dump(mode="json"),
    }
    payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return PreparedProductionReleaseRegistration(
        release_sha256=release.release_sha256,
        window_sha256=release.acceptance_window.window_sha256,
        release_tag=release.release_tag,
        git_commit_sha=release.git_commit_sha,
        oci_image_digest=release.oci_image_digest,
        window_start_ms=release.acceptance_window.start_ms,
        window_end_ms=release.acceptance_window.end_ms,
        serve_runtime_id=serve.runtime_id,
        serve_runtime_revision=serve.runtime_revision,
        serve_image_digest=serve.image_digest,
        serve_started_at_ms=serve.started_at_ms,
        serve_measured_at_ms=serve.measured_at_ms,
        payload_json=payload_json,
    )


class ProductionRollbackReceiptV2(_Frozen):
    rollback_version: Literal["production_v3_rollback_receipt_v2"] = "production_v3_rollback_receipt_v2"
    release_candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    release_candidate_artifact_path: str = Field(min_length=1, max_length=1024)
    bindings: tuple[VenueBinding, ...] = Field(min_length=1, max_length=2)
    grant_sha256s: tuple[str, ...] = Field(min_length=1)
    rolled_back_at_ms: int = Field(gt=0)
    restart_workers_runtime_id: UUID
    restart_serve_runtime_id: UUID
    rolled_back_by: str = Field(min_length=1, max_length=128)
    statement: Literal["ALL_ENABLED_VENUES_FLAT_GRANTS_REVOKED_CAPITAL_PAUSED_NO_TERMINAL_INTENT_REVIVAL"]
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_rollback(self) -> Self:
        if self.bindings != tuple(sorted(set(self.bindings))):
            raise ValueError("evidence_rollback_bindings_not_canonical")
        if self.grant_sha256s != tuple(sorted(set(self.grant_sha256s))):
            raise ValueError("evidence_rollback_grants_not_canonical")
        unsigned = self.model_dump(mode="json", exclude={"receipt_sha256"})
        if self.receipt_sha256 != canonical_sha256(unsigned):
            raise ValueError("evidence_rollback_receipt_identity_invalid")
        return self


class EvidenceVerificationCheckV1(_Frozen):
    code: str = Field(pattern=r"^[a-z0-9_]{3,128}$")
    passed: bool
    evidence: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True, slots=True, order=True)
class ReceiptArtifactRequestV1:
    """One raw file read requested by Trading and performed mechanically by App."""

    receipt_sha256: str
    artifact_path: str


_RECEIPT_CHAIN_BY_KIND: dict[str, tuple[str, ...]] = {
    "DISCOVERY_CORPUS": ("DISCOVERY_CORPUS",),
    "CANDIDATE_DECISION": ("CANDIDATE_DECISION", "DISCOVERY_CORPUS"),
    "FUTURE_CAPTURE": ("FUTURE_CAPTURE", "CANDIDATE_DECISION", "DISCOVERY_CORPUS"),
    "FUTURE_DRAIN": ("FUTURE_DRAIN", "FUTURE_CAPTURE", "CANDIDATE_DECISION", "DISCOVERY_CORPUS"),
    "FUTURE_RESULT": (
        "FUTURE_RESULT",
        "FUTURE_DRAIN",
        "FUTURE_CAPTURE",
        "CANDIDATE_DECISION",
        "DISCOVERY_CORPUS",
    ),
}


def receipt_artifact_requests(rows: list[dict[str, Any]]) -> tuple[ReceiptArtifactRequestV1, ...]:
    requests: list[ReceiptArtifactRequestV1] = []
    for row in rows:
        try:
            receipt, _artifact_type = _receipt_contract(row)
            requests.append(
                ReceiptArtifactRequestV1(
                    receipt_sha256=str(row["receipt_sha256"]),
                    artifact_path=str(receipt.artifact_path),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return tuple(sorted(requests))


def receipt_chain_verification_checks(
    receipt_sha256: str,
    rows: list[dict[str, Any]],
    artifact_bytes: dict[str, bytes | None],
) -> list[EvidenceVerificationCheckV1]:
    """Own all receipt-kind, parent, model, artifact and canonical-identity interpretation."""

    by_sha = {str(row["receipt_sha256"]): row for row in rows}
    root = by_sha.get(receipt_sha256)
    if root is None:
        return [verification_check("receipt_root_exists", False)]
    expected_kinds = _RECEIPT_CHAIN_BY_KIND.get(str(root.get("receipt_kind")))
    if expected_kinds is None:
        return [verification_check("receipt_root_kind_known", False)]
    checks: list[EvidenceVerificationCheckV1] = []
    current: str | None = receipt_sha256
    seen: set[str] = set()
    for index, expected_kind in enumerate(expected_kinds):
        row = None if current is None or current in seen else by_sha.get(current)
        checks.append(
            verification_check(
                f"receipt_{index}_kind",
                row is not None and row.get("receipt_kind") == expected_kind,
                expected_kind=expected_kind,
            )
        )
        if row is None:
            current = None
            continue
        seen.add(str(row["receipt_sha256"]))
        checks.append(
            verification_check(
                f"receipt_{index}_row_identity",
                _receipt_row_identity_valid(row),
                kind=expected_kind,
            )
        )
        checks.append(
            verification_check(
                f"receipt_{index}_artifact_identity",
                _receipt_artifact_identity_valid(row, artifact_bytes.get(str(row["receipt_sha256"]))),
                kind=expected_kind,
            )
        )
        parent = row.get("parent_receipt_sha256")
        expected_parent = None if index + 1 == len(expected_kinds) else parent
        checks.append(
            verification_check(
                f"receipt_{index}_parent_link",
                (parent is None and expected_parent is None)
                if index + 1 == len(expected_kinds)
                else parent is not None and str(parent) in by_sha,
                kind=expected_kind,
            )
        )
        current = None if parent is None else str(parent)
    checks.append(verification_check("receipt_chain_complete", current is None))
    return checks


def receipt_chains_valid(
    receipt_sha256s: tuple[str, ...],
    rows: list[dict[str, Any]],
    artifact_bytes: dict[str, bytes | None],
) -> bool:
    return all(
        all(check.passed for check in receipt_chain_verification_checks(digest, rows, artifact_bytes))
        for digest in receipt_sha256s
    )


def _receipt_contract(row: dict[str, Any]) -> tuple[Any, Any]:
    payload = dict(row["payload"])
    receipt_payload = dict(payload["receipt"])
    kind = str(row["receipt_kind"])
    if kind == "DISCOVERY_CORPUS":
        return DiscoveryCorpusReceiptV1.model_validate(receipt_payload), DiscoveryCorpusArtifactV1
    if kind == "CANDIDATE_DECISION":
        artifact_type = CandidateLockedV1 if row["terminal"] == "CANDIDATE_LOCKED" else CANDIDATE_DECISION_ADAPTER
        return CandidateDecisionReceiptV1.model_validate(receipt_payload), artifact_type
    if kind == "FUTURE_CAPTURE":
        return FutureCaptureReceiptV1.model_validate(receipt_payload), EvidenceCaptureArtifactV1
    if kind == "FUTURE_DRAIN":
        return FutureDrainReceiptV1.model_validate(receipt_payload), EvidenceDrainArtifactV1
    if kind == "FUTURE_RESULT":
        return FutureHoldoutResultReceiptV1.model_validate(receipt_payload), FutureHoldoutResultV1
    raise ValueError("evidence_receipt_kind_unknown")


def _receipt_row_identity_valid(row: dict[str, Any]) -> bool:
    try:
        payload = dict(row["payload"])
        receipt, artifact_type = _receipt_contract(row)
        if artifact_type is CANDIDATE_DECISION_ADAPTER:
            decision = CANDIDATE_DECISION_ADAPTER.validate_python(payload["evidence"])
            if canonical_sha256(decision.model_dump(mode="json")) != row["artifact_sha256"]:
                return False
        if row["receipt_kind"] == "FUTURE_RESULT":
            result = FutureHoldoutResultV1.model_validate(payload["evidence"])
            if result.report_sha256 != row["artifact_sha256"]:
                return False
        return bool(
            receipt.receipt_sha256 == row["receipt_sha256"]
            and receipt.created_at_ms == row["created_at_ms"]
            and int(row["recorded_at_ms"]) >= receipt.created_at_ms
            and payload.get("receipt_sha256") == row["receipt_sha256"]
            and payload.get("receipt_kind") == row["receipt_kind"]
            and payload.get("terminal") == row["terminal"]
            and payload.get("binding") == row["binding"]
            and payload.get("parent_receipt_sha256") == row["parent_receipt_sha256"]
            and payload.get("artifact_sha256") == row["artifact_sha256"]
            and payload.get("corpus_sha256") == row["corpus_sha256"]
            and payload.get("protocol_sha256") == row["protocol_sha256"]
        )
    except (KeyError, TypeError, ValueError):
        return False


def _receipt_artifact_identity_valid(row: dict[str, Any], raw: bytes | None) -> bool:
    if raw is None or hashlib.sha256(raw).hexdigest() != row.get("artifact_sha256"):
        return False
    try:
        _receipt, artifact_type = _receipt_contract(row)
        if artifact_type is CANDIDATE_DECISION_ADAPTER:
            artifact = CANDIDATE_DECISION_ADAPTER.validate_json(raw)
        else:
            artifact = artifact_type.model_validate_json(raw)
        canonical = json.dumps(
            artifact.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        return bool(canonical == raw and canonical_sha256(artifact.model_dump(mode="json")) == row["artifact_sha256"])
    except (TypeError, ValueError):
        return False


class EvidenceVerificationReportV1(_Frozen):
    report_version: Literal["production_v3_verification_report_v1"] = "production_v3_verification_report_v1"
    terminal: Literal["VERIFIED", "FAILED"]
    subject: str = Field(min_length=1, max_length=256)
    verified_at_ms: int = Field(gt=0)
    checks: tuple[EvidenceVerificationCheckV1, ...]
    failure_codes: tuple[str, ...]
    binding_report_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_report(self) -> Self:
        if self.checks != tuple(sorted(self.checks, key=lambda row: row.code)):
            raise ValueError("evidence_verification_checks_not_canonical")
        if len({row.code for row in self.checks}) != len(self.checks):
            raise ValueError("evidence_verification_check_duplicate")
        expected = tuple(row.code for row in self.checks if not row.passed)
        if self.failure_codes != expected:
            raise ValueError("evidence_verification_failure_codes_invalid")
        if (self.terminal == "VERIFIED") != (not self.failure_codes):
            raise ValueError("evidence_verification_terminal_invalid")
        binds_window = self.subject.startswith("fixed-window:") or self.subject.startswith("release:")
        if binds_window != (self.binding_report_sha256 is not None):
            raise ValueError("evidence_verification_binding_report_identity_invalid")
        return self

    @property
    def report_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


def verification_report(
    *,
    subject: str,
    verified_at_ms: int,
    checks: list[EvidenceVerificationCheckV1],
    binding_report: Sequence[Mapping[str, Any]] | None = None,
) -> EvidenceVerificationReportV1:
    ordered = tuple(sorted(checks, key=lambda row: row.code))
    failures = tuple(row.code for row in ordered if not row.passed)
    return EvidenceVerificationReportV1(
        terminal="FAILED" if failures else "VERIFIED",
        subject=subject,
        verified_at_ms=verified_at_ms,
        checks=ordered,
        failure_codes=failures,
        binding_report_sha256=None if binding_report is None else canonical_sha256(binding_report),
    )


def verification_check(code: str, passed: bool, **evidence: Any) -> EvidenceVerificationCheckV1:
    """Build one typed verifier result without giving the App business semantics."""

    return EvidenceVerificationCheckV1(code=code, passed=bool(passed), evidence=evidence)


def intent_verification_checks(intent: dict[str, Any]) -> list[EvidenceVerificationCheckV1]:
    """Verify the conserved, protected and authoritative terminal shape of one Intent."""

    terminal = intent["execution_state"] == "TERMINAL"
    closed_flat = intent["terminal_outcome"] == "CLOSED_FLAT"
    return [
        verification_check("intent_current_contract", intent["intent_version"] == "trade_intent_v3"),
        verification_check("intent_terminal", terminal, execution_state=intent["execution_state"]),
        verification_check(
            "intent_zero_provider_write_before_fence",
            intent["entry_submitted_at_ms"] is None
            or (
                intent["entry_fenced_at_ms"] is not None
                and intent["entry_submitted_at_ms"] >= intent["entry_fenced_at_ms"]
            ),
        ),
        verification_check(
            "intent_no_unprotected_fill",
            intent["opened_at_ms"] is None
            or (intent["protected_at_ms"] is not None and intent["protection_order_id"] is not None),
        ),
        verification_check(
            "intent_closed_flat_proof",
            not closed_flat
            or (
                intent["closed_at_ms"] is not None
                and intent["flat_verified_at_ms"] is not None
                and intent["risk_status"] == "SETTLED"
                and intent["settlement_known"] is True
            ),
        ),
    ]


def case_verification_checks(snapshot: dict[str, Any] | None) -> list[EvidenceVerificationCheckV1]:
    """Verify Case admission/disposition/Intent conservation from a read-only DB snapshot."""

    if snapshot is None:
        return [verification_check("case_exists", False)]
    case = snapshot["case"]
    intents = snapshot["intents"]
    allowed = case["capital_disposition"] == "allowed"
    checks = [
        verification_check("case_exists", True),
        verification_check(
            "case_exactly_one_admission_link", snapshot["gate_count"] == 1, count=snapshot["gate_count"]
        ),
        verification_check(
            "case_policy_capital_disposition_complete",
            case["policy_decision"] is not None and case["capital_disposition"] is not None,
        ),
        verification_check(
            "case_intent_conservation",
            len(intents) == (1 if allowed else 0),
            intent_count=len(intents),
            capital_allowed=allowed,
        ),
    ]
    if intents:
        checks.extend(intent_verification_checks(intents[0]))
    return checks


def fixed_window_verification_checks(
    spec: FixedWindowAcceptanceV1,
    snapshot: dict[str, Any],
    *,
    sources: tuple[FixedWindowSourceFactV1, ...],
    serve: ServeRuntimeObservationV1,
    now_ms: int,
) -> list[EvidenceVerificationCheckV1]:
    """Verify one exact-release, non-moving seven-day operational window."""

    counts = snapshot["counts"]
    workers_runtime = snapshot.get("workers_runtime")
    registration = snapshot.get("release_registration")
    source_keys = tuple(sorted(source.source_key for source in sources))
    gate_source_keys = tuple(snapshot.get("gate_source_keys") or ())
    return [
        verification_check("window_drain_cutoff_reached", now_ms >= spec.drain_cutoff_ms),
        verification_check(
            "window_preregistered_before_start",
            registration is not None
            and registration["window_sha256"] == spec.window_sha256
            and registration["release_tag"] == spec.release_tag
            and registration["git_commit_sha"] == spec.git_commit_sha
            and registration["oci_image_digest"] == spec.oci_image_digest
            and registration["registered_at_ms"] < spec.start_ms,
        ),
        verification_check(
            "window_exact_workers_release",
            registration is not None
            and workers_runtime is not None
            and workers_runtime["runtime_id"] == str(registration["workers_runtime_id"])
            and workers_runtime["runtime_revision"] == spec.git_commit_sha
            and workers_runtime["image_digest"] == spec.oci_image_digest
            and workers_runtime["lifecycle_state"] == "running"
            and workers_runtime["started_at_ms"] == registration["workers_started_at_ms"]
            and workers_runtime["heartbeat_at_ms"] >= spec.end_ms,
        ),
        verification_check(
            "window_exact_serve_release",
            registration is not None
            and str(serve.runtime_id) == str(registration["serve_runtime_id"])
            and serve.runtime_revision == spec.git_commit_sha
            and serve.image_digest == spec.oci_image_digest
            and serve.started_at_ms == registration["serve_started_at_ms"]
            and serve.measured_at_ms >= spec.end_ms,
        ),
        verification_check("window_source_release_identity", counts["wrong_release_source_count"] == 0),
        verification_check("window_gate_contract_identity", counts["wrong_gate_contract_count"] == 0),
        verification_check("window_intent_release_identity", counts["intent_release_mismatch_count"] == 0),
        verification_check(
            "window_minimum_sources",
            len(source_keys) >= spec.minimum_source_count,
            count=len(source_keys),
        ),
        verification_check(
            "window_minimum_cases", counts["case_count"] >= spec.minimum_case_count, count=counts["case_count"]
        ),
        verification_check(
            "window_minimum_intents",
            counts["intent_count"] >= spec.minimum_intent_count,
            count=counts["intent_count"],
        ),
        verification_check(
            "window_minimum_closed_flat",
            counts["closed_flat_count"] >= spec.minimum_closed_flat_count,
            count=counts["closed_flat_count"],
        ),
        verification_check("window_source_universe_unique", len(source_keys) == len(set(source_keys))),
        verification_check(
            "window_source_gate_conservation",
            source_keys == gate_source_keys,
            source_count=len(source_keys),
            gate_source_count=len(gate_source_keys),
        ),
        verification_check(
            "window_source_admission_unique",
            counts["gate_source_count"] == counts["unique_source_count"],
        ),
        verification_check(
            "window_source_disposition_conservation",
            counts["gate_source_count"]
            == counts["admitted_source_count"] + counts["rejected_or_deferred_source_count"],
        ),
        verification_check(
            "window_admitted_source_case_conservation", counts["admitted_source_count"] == counts["case_count"]
        ),
        verification_check("window_gate_links_valid", counts["invalid_gate_link_count"] == 0),
        verification_check("window_case_links_complete", counts["case_without_gate_count"] == 0),
        verification_check("window_case_dispositions_complete", counts["case_disposition_missing_count"] == 0),
        verification_check(
            "window_allowed_case_intent_conservation", counts["allowed_case_intent_mismatch_count"] == 0
        ),
        verification_check("window_blocked_case_zero_intent", counts["blocked_case_intent_mismatch_count"] == 0),
        verification_check("window_only_v3_intents", counts["non_v3_intent_count"] == 0),
        verification_check("window_all_intents_terminal", counts["nonterminal_intent_count"] == 0),
        verification_check("window_zero_unknown_exposure", counts["exposure_unknown_or_active_count"] == 0),
        verification_check("window_zero_provider_write_before_fence", counts["provider_write_before_fence_count"] == 0),
        verification_check("window_zero_unprotected_fill", counts["unprotected_fill_count"] == 0),
        verification_check("window_closed_flat_proven", counts["closed_flat_proof_missing_count"] == 0),
        verification_check("window_financial_accounting_complete", counts["financial_accounting_missing_count"] == 0),
    ]


def fixed_window_binding_report(
    rows: list[dict[str, Any]],
    sources: tuple[FixedWindowSourceFactV1, ...],
    gate_source_keys: tuple[str, ...],
) -> list[dict[str, Any]]:
    """Attach the complete source universe and per-venue missingness to the DB-owned report rows."""

    by_binding = {str(row["binding"]): dict(row) for row in rows}
    gate_keys = set(gate_source_keys)
    for source in sources:
        binding = binding_for_source_venue(source.source_venue) or "UNBOUND"
        row = by_binding.setdefault(binding, {"binding": binding})
        row["source_count"] = int(row.get("source_count") or 0) + 1
        if source.source_key not in gate_keys:
            row["source_without_gate_count"] = int(row.get("source_without_gate_count") or 0) + 1
    for row in by_binding.values():
        row.setdefault("source_count", 0)
        row.setdefault("source_without_gate_count", 0)
    return [by_binding[key] for key in sorted(by_binding)]


def canary_closed_flat(row: dict[str, Any]) -> bool:
    """Return whether a canary has the complete authoritative CLOSED_FLAT proof."""

    return bool(
        row["intent_version"] == "trade_intent_v3"
        and row["execution_state"] == "TERMINAL"
        and row["terminal_outcome"] == "CLOSED_FLAT"
        and row["entry_fenced_at_ms"] is not None
        and row["entry_submitted_at_ms"] is not None
        and row["entry_submitted_at_ms"] >= row["entry_fenced_at_ms"]
        and row["opened_at_ms"] is not None
        and row["protected_at_ms"] is not None
        and row["protection_order_id"] is not None
        and row["closed_at_ms"] is not None
        and row["flat_verified_at_ms"] is not None
        and row["realized_pnl_amount"] is not None
        and row["realized_pnl_currency"] is not None
        and row["commissions_by_currency"] is not None
        and row["funding_by_currency"] is not None
        and row["risk_status"] == "SETTLED"
        and row["settlement_known"] is True
    )


def rollback_verification_checks(
    receipt: ProductionRollbackReceiptV2,
    release: ProductionReleaseCandidateV1,
    snapshot: dict[str, Any],
    *,
    serve: ServeRuntimeObservationV1,
    now_ms: int,
) -> list[EvidenceVerificationCheckV1]:
    """Verify rollback scope, ordering, revoked authority and authoritative flat state."""

    bindings = {str(row["binding"]): row for row in snapshot["bindings"]}
    grants = {str(row["grant_sha256"]): row for row in snapshot["grants"]}
    decision_runtime = snapshot.get("decision_runtime")
    workers_runtime = snapshot.get("workers_runtime")
    registration = snapshot.get("release_registration")
    return [
        verification_check(
            "rollback_release_candidate_identity", release.release_sha256 == receipt.release_candidate_sha256
        ),
        verification_check(
            "rollback_release_scope",
            receipt.bindings == tuple(row.binding for row in release.bindings)
            and receipt.grant_sha256s == release.promotion_grant_sha256s,
        ),
        verification_check(
            "rollback_after_release_window", receipt.rolled_back_at_ms >= release.acceptance_window.drain_cutoff_ms
        ),
        verification_check("rollback_not_before_receipt", now_ms >= receipt.rolled_back_at_ms),
        verification_check("rollback_capital_paused", snapshot["control"] == "PAUSED"),
        verification_check(
            "rollback_decision_continues",
            decision_runtime is not None
            and decision_runtime["state"] == "RUNNING"
            and decision_runtime["heartbeat_at_ms"] >= receipt.rolled_back_at_ms,
        ),
        verification_check(
            "rollback_workers_restarted",
            registration is not None
            and workers_runtime is not None
            and workers_runtime["runtime_id"] == str(receipt.restart_workers_runtime_id)
            and workers_runtime["runtime_id"] != str(registration["workers_runtime_id"])
            and workers_runtime["started_at_ms"] >= receipt.rolled_back_at_ms
            and workers_runtime["lifecycle_state"] == "running"
            and workers_runtime["heartbeat_at_ms"] >= receipt.rolled_back_at_ms,
        ),
        verification_check(
            "rollback_observer_continues",
            registration is not None
            and str(serve.runtime_id) == str(receipt.restart_serve_runtime_id)
            and str(serve.runtime_id) != str(registration["serve_runtime_id"])
            and serve.started_at_ms >= receipt.rolled_back_at_ms
            and serve.measured_at_ms >= receipt.rolled_back_at_ms,
        ),
        verification_check(
            "rollback_workers_continues",
            workers_runtime is not None
            and workers_runtime["lifecycle_state"] == "running"
            and workers_runtime["heartbeat_at_ms"] >= receipt.rolled_back_at_ms,
        ),
        verification_check("rollback_zero_active_intents", snapshot["active_intent_count"] == 0),
        verification_check("rollback_zero_active_risk", snapshot["active_risk_count"] == 0),
        verification_check("rollback_restart_zero_refill", snapshot["post_rollback_intent_count"] == 0),
        verification_check("rollback_zero_provider_write", snapshot["post_rollback_submission_count"] == 0),
        verification_check(
            "rollback_bindings_authoritatively_flat",
            workers_runtime is not None
            and all(
                binding in bindings
                and bindings[binding]["runtime_state"] == "ready"
                and bindings[binding]["account_state"] == "reconciled_flat"
                and bindings[binding]["active_arm_receipt_sha256"] is None
                and bindings[binding]["heartbeat_at_ms"] is not None
                and int(bindings[binding]["heartbeat_at_ms"]) >= receipt.rolled_back_at_ms
                and int(bindings[binding]["heartbeat_at_ms"]) >= int(workers_runtime["started_at_ms"])
                and bindings[binding]["catalog_state"] in {"ready", "stale", "error", "missing"}
                and bindings[binding]["capability_state"] in {"ready", "stale", "error", "missing"}
                for binding in receipt.bindings
            ),
        ),
        verification_check(
            "rollback_grants_revoked_or_expired",
            all(
                digest in grants
                and (
                    grants[digest]["expires_at_ms"] <= receipt.rolled_back_at_ms
                    or (
                        grants[digest]["revoked_at_ms"] is not None
                        and grants[digest]["revoked_at_ms"] <= receipt.rolled_back_at_ms
                    )
                )
                for digest in receipt.grant_sha256s
            ),
        ),
    ]


def release_verification_checks(
    release: ProductionReleaseCandidateV1,
    snapshot: dict[str, Any],
    window_snapshot: dict[str, Any],
    observations: ReleaseVerificationObservationsV1,
    *,
    window_sources: tuple[FixedWindowSourceFactV1, ...],
    now_ms: int,
) -> list[EvidenceVerificationCheckV1]:
    """Verify one exact immutable release against DB and locally observed identities.

    The App owns collection of filesystem, Git, runtime and PostgreSQL facts. Trading owns every
    interpretation and terminal check.
    """

    receipt_rows = {str(row["receipt_sha256"]): row for row in snapshot["receipts"]}
    grant_rows = {str(row["grant_sha256"]): row for row in snapshot["grants"]}
    risk_rows = {str(row["risk_policy_sha256"]): row for row in snapshot["risk_policies"]}
    binding_rows = {str(row["binding"]): row for row in snapshot["bindings"]}
    canary_rows = {str(row["intent_id"]): row for row in snapshot["canaries"]}
    runtime_rows = {str(row["runtime_id"]): row for row in snapshot["runtime_starts"]}
    corpus_artifacts = {
        str(receipt_rows[digest]["artifact_sha256"])
        for digest in release.corpus_receipt_sha256s
        if digest in receipt_rows
    }
    future_artifacts = {
        str(receipt_rows[digest]["artifact_sha256"])
        for digest in release.future_result_receipt_sha256s
        if digest in receipt_rows
    }
    release_bindings = {row.binding for row in release.bindings}
    checks = [
        verification_check("release_tag_signature_valid", observations.tag_signature_valid),
        verification_check("release_tag_commit_identity", observations.tag_commit == release.git_commit_sha),
        verification_check("release_tag_tree_identity", observations.tag_tree == release.git_tree_sha),
        verification_check("release_migration_head", snapshot["migration_head"] == release.migration_head),
        verification_check("release_runtime_revision", observations.runtime_revision == release.git_commit_sha),
        verification_check("release_image_digest", observations.image_digest == release.oci_image_digest),
        verification_check("release_openapi_identity", observations.openapi_sha256 == release.openapi_sha256),
        verification_check("release_web_assets_identity", observations.web_assets_sha256 == release.web_assets_sha256),
        verification_check(
            "release_nautilus_wheel_identity",
            observations.nautilus_wheel_sha256 == release.nautilus_wheel_sha256,
        ),
        verification_check(
            "release_nautilus_source_identity",
            observations.nautilus_source_git_commit == release.nautilus_source_git_commit,
        ),
        verification_check(
            "release_execution_contract_receipt",
            observations.execution_contract_receipt_sha256 == release.execution_contract_receipt_sha256,
        ),
        verification_check(
            "release_execution_policy_identity",
            observations.execution_policy_sha256 == release.execution_policy_sha256,
        ),
        verification_check(
            "release_quote_contract_identity", observations.quote_contract_sha256 == release.quote_contract_sha256
        ),
        verification_check(
            "release_protection_contract_identity",
            observations.protection_contract_sha256 == release.protection_contract_sha256,
        ),
        verification_check(
            "release_policy_config_identity", observations.policy_config_sha256 == release.policy_config_sha256
        ),
        verification_check(
            "release_registration_identity",
            window_snapshot.get("release_registration") is not None
            and window_snapshot["release_registration"]["release_sha256"] == release.release_sha256,
        ),
        verification_check("release_evidence_receipt_chains_valid", observations.receipt_chains_valid),
        verification_check(
            "release_corpus_receipts_complete",
            all(
                digest in receipt_rows and receipt_rows[digest]["receipt_kind"] == "DISCOVERY_CORPUS"
                for digest in release.corpus_receipt_sha256s
            ),
            expected=len(release.corpus_receipt_sha256s),
        ),
        verification_check(
            "release_future_results_promote",
            all(
                digest in receipt_rows
                and receipt_rows[digest]["receipt_kind"] == "FUTURE_RESULT"
                and receipt_rows[digest]["terminal"] == "PROMOTE"
                for digest in release.future_result_receipt_sha256s
            ),
            expected=len(release.future_result_receipt_sha256s),
        ),
        verification_check(
            "release_promotion_grants_complete", set(grant_rows) == set(release.promotion_grant_sha256s)
        ),
        verification_check("release_risk_policies_complete", set(risk_rows) == set(release.risk_policy_sha256s)),
        verification_check(
            "release_risk_policies_target_release",
            all(row["approved_release"] == release.release_tag for row in risk_rows.values()),
        ),
        verification_check(
            "release_grant_evidence_chain",
            all(
                (payload := dict(row.get("payload") or {})).get("approved_release") == release.release_tag
                and row["binding"] in release_bindings
                and row["risk_policy_sha256"] in release.risk_policy_sha256s
                and row["sealed_corpus_sha256"] in corpus_artifacts
                and row["locked_future_report_sha256"] in future_artifacts
                and payload.get("policy_config_sha256") == release.policy_config_sha256
                and payload.get("execution_policy_sha256") == release.execution_policy_sha256
                and payload.get("quote_contract_sha256") == release.quote_contract_sha256
                and payload.get("protection_contract_sha256") == release.protection_contract_sha256
                for row in grant_rows.values()
            ),
        ),
        verification_check("release_canary_intents_complete", set(canary_rows) == set(release.canary_intent_ids)),
        verification_check(
            "release_canary_binding_coverage",
            {str(row["binding"]) for row in canary_rows.values()} == release_bindings,
        ),
        verification_check(
            "release_canary_authority_chain",
            all(
                row["grant_sha256"] in release.promotion_grant_sha256s
                and row["risk_policy_sha256"] in release.risk_policy_sha256s
                and row["sealed_corpus_sha256"] in corpus_artifacts
                and row["locked_future_report_sha256"] in future_artifacts
                for row in canary_rows.values()
            ),
        ),
        verification_check(
            "release_canaries_closed_flat", all(canary_closed_flat(row) for row in canary_rows.values())
        ),
    ]
    for binding in release.bindings:
        row = binding_rows.get(binding.binding)
        payload = {} if row is None else dict(row.get("execution_binding") or {})
        checks.append(
            verification_check(
                f"release_binding_{binding.binding.lower()}",
                row is not None
                and row["catalog_snapshot_sha256"] == binding.catalog_snapshot_sha256
                and row["capability_snapshot_sha256"] == binding.capability_snapshot_sha256
                and row["execution_binding_sha256"] == binding.execution_binding_sha256
                and int(row["account_generation"]) == binding.account_generation
                and payload.get("account_identity_sha256") == binding.account_identity_sha256
                and payload.get("adapter_contract_sha256") == binding.adapter_contract_sha256
                and payload.get("quote_contract_sha256") == release.quote_contract_sha256
                and payload.get("protection_contract_sha256") == release.protection_contract_sha256
                and payload.get("client_runtime_identity") == binding.client_runtime_identity,
            )
        )
    drill = release.restart_drill
    protected_runtime = runtime_rows.get(str(drill.protected_runtime_id))
    recovered_runtime = runtime_rows.get(str(drill.recovered_runtime_id))
    drill_intent = canary_rows.get(drill.intent_id)
    checks.extend(
        [
            verification_check(
                "release_restart_runtime_receipts_complete",
                set(runtime_rows) == {str(drill.protected_runtime_id), str(drill.recovered_runtime_id)},
            ),
            verification_check(
                "release_restart_exact_runtime_identity",
                all(
                    row is not None
                    and row["runtime_revision"] == release.git_commit_sha
                    and row["image_digest"] == release.oci_image_digest
                    and row["nautilus_source_git_commit"] == release.nautilus_source_git_commit
                    and _wheel_sha256(str(row["nautilus_wheel_identity"])) == release.nautilus_wheel_sha256
                    for row in (protected_runtime, recovered_runtime)
                ),
            ),
            verification_check(
                "release_restart_after_protection_before_flat",
                drill_intent is not None
                and protected_runtime is not None
                and recovered_runtime is not None
                and drill.binding == drill_intent["binding"]
                and protected_runtime["started_at_ms"] <= drill_intent["protected_at_ms"]
                and drill_intent["protected_at_ms"] <= drill.stopped_at_ms
                and drill.stopped_at_ms < recovered_runtime["started_at_ms"]
                and recovered_runtime["started_at_ms"] <= drill.reconciled_at_ms
                and drill.reconciled_at_ms == drill_intent["flat_verified_at_ms"],
            ),
        ]
    )
    checks.extend(
        fixed_window_verification_checks(
            release.acceptance_window,
            window_snapshot,
            sources=window_sources,
            serve=observations.serve_runtime,
            now_ms=now_ms,
        )
    )
    return checks


def _wheel_sha256(identity: str) -> str | None:
    return identity.rsplit("sha256:", 1)[-1] if "sha256:" in identity else None


__all__ = [
    "CanaryRestartDrillReceiptV1",
    "EvidenceVerificationCheckV1",
    "EvidenceVerificationReportV1",
    "FixedWindowAcceptanceV1",
    "FixedWindowSourceFactV1",
    "NautilusRuntimeStartV1",
    "PreparedProductionReleaseRegistration",
    "ProductionReleaseCandidateV1",
    "ProductionRollbackReceiptV2",
    "ReleaseBindingIdentityV1",
    "ReleaseVerificationObservationsV1",
    "ServeRuntimeObservationV1",
    "canary_closed_flat",
    "case_verification_checks",
    "fixed_window_binding_report",
    "fixed_window_verification_checks",
    "intent_verification_checks",
    "prepare_production_release_registration",
    "release_verification_checks",
    "rollback_verification_checks",
    "verification_check",
    "verification_report",
]
