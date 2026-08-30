"""Closed inputs and stable results for the single Production V3 read-only verifier.

The verifier never grants capital and never calls a venue.  Operators hand it immutable evidence
identities; it reads PostgreSQL truth and returns one canonical set of checks.  A failed check is a
named terminal, not a warning that a caller may reinterpret as success.
"""

from __future__ import annotations

from typing import Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .contracts import VenueBinding, canonical_sha256

SEVEN_DAYS_MS = 7 * 86_400_000


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class FixedWindowAcceptanceV1(_Frozen):
    """The preregistered, non-moving operational accounting window."""

    window_version: Literal["production_v3_fixed_window_v1"] = "production_v3_fixed_window_v1"
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    drain_cutoff_ms: int = Field(gt=0)
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
        unsigned = self.model_dump(mode="json", exclude={"approval_sha256"})
        if self.approval_sha256 != canonical_sha256(unsigned):
            raise ValueError("evidence_release_approval_identity_invalid")
        return self

    @property
    def release_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


class ProductionRollbackReceiptV1(_Frozen):
    rollback_version: Literal["production_v3_rollback_receipt_v1"] = "production_v3_rollback_receipt_v1"
    release_candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bindings: tuple[VenueBinding, ...] = Field(min_length=1, max_length=2)
    grant_sha256s: tuple[str, ...] = Field(min_length=1)
    rolled_back_at_ms: int = Field(gt=0)
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


class EvidenceVerificationReportV1(_Frozen):
    report_version: Literal["production_v3_verification_report_v1"] = "production_v3_verification_report_v1"
    terminal: Literal["VERIFIED", "FAILED"]
    subject: str = Field(min_length=1, max_length=256)
    verified_at_ms: int = Field(gt=0)
    checks: tuple[EvidenceVerificationCheckV1, ...]
    failure_codes: tuple[str, ...]

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
        return self

    @property
    def report_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


def verification_report(
    *,
    subject: str,
    verified_at_ms: int,
    checks: list[EvidenceVerificationCheckV1],
) -> EvidenceVerificationReportV1:
    ordered = tuple(sorted(checks, key=lambda row: row.code))
    failures = tuple(row.code for row in ordered if not row.passed)
    return EvidenceVerificationReportV1(
        terminal="FAILED" if failures else "VERIFIED",
        subject=subject,
        verified_at_ms=verified_at_ms,
        checks=ordered,
        failure_codes=failures,
    )


__all__ = [
    "CanaryRestartDrillReceiptV1",
    "EvidenceVerificationCheckV1",
    "EvidenceVerificationReportV1",
    "FixedWindowAcceptanceV1",
    "NautilusRuntimeStartV1",
    "ProductionReleaseCandidateV1",
    "ProductionRollbackReceiptV1",
    "ReleaseBindingIdentityV1",
    "verification_report",
]
