from __future__ import annotations

import pytest

from tracefold.trading.contracts import FixedWindowSourceFactV1, canonical_sha256
from tracefold.trading.evidence_verification import (
    EvidenceVerificationCheckV1,
    FixedWindowAcceptanceV1,
    ProductionReleaseCandidateV1,
    ProductionRollbackReceiptV2,
    ServeRuntimeObservationV1,
    case_verification_checks,
    fixed_window_verification_checks,
    rollback_verification_checks,
    verification_report,
)

START = 1_900_000_000_000
END = START + 7 * 86_400_000


def _window() -> FixedWindowAcceptanceV1:
    return FixedWindowAcceptanceV1(
        start_ms=START,
        end_ms=END,
        drain_cutoff_ms=END + 60_000,
        release_tag="production-v3-rc1",
        git_commit_sha="1" * 40,
        oci_image_digest="tracefold@sha256:" + "3" * 64,
        gate_version="candidate_gate_v1",
        gate_config_digest="a" * 64,
        minimum_source_count=4,
        minimum_case_count=3,
        minimum_intent_count=2,
        minimum_closed_flat_count=1,
    )


def _release_payload() -> dict[str, object]:
    restart = {
        "receipt_version": "canary_restart_drill_receipt_v1",
        "intent_id": "6" * 64,
        "binding": "BINANCE_USDM",
        "protected_runtime_id": "00000000-0000-0000-0000-000000000001",
        "recovered_runtime_id": "00000000-0000-0000-0000-000000000002",
        "stopped_at_ms": START - 10,
        "reconciled_at_ms": START - 5,
        "operator": "operator",
        "statement": "QUERY_FIRST_ZERO_DUPLICATE_AUTHORITATIVE_CLOSED_FLAT_AFTER_PROCESS_RESTART",
    }
    restart["receipt_sha256"] = canonical_sha256(restart)
    return {
        "release_version": "production_v3_release_candidate_v1",
        "release_tag": "production-v3-rc1",
        "git_commit_sha": "1" * 40,
        "git_tree_sha": "2" * 40,
        "oci_image_digest": "tracefold@sha256:" + "3" * 64,
        "migration_head": "20260830_0335",
        "openapi_sha256": "4" * 64,
        "web_assets_sha256": "5" * 64,
        "workers_runtime_revision": "1" * 40,
        "serve_runtime_revision": "1" * 40,
        "nautilus_wheel_sha256": "6" * 64,
        "nautilus_source_git_commit": "7" * 40,
        "execution_contract_receipt_sha256": "8" * 64,
        "execution_policy_sha256": "9" * 64,
        "quote_contract_sha256": "a" * 64,
        "protection_contract_sha256": "b" * 64,
        "policy_config_sha256": "c" * 64,
        "bindings": [
            {
                "binding": "BINANCE_USDM",
                "catalog_snapshot_sha256": "d" * 64,
                "capability_snapshot_sha256": "e" * 64,
                "execution_binding_sha256": "f" * 64,
                "account_identity_sha256": "0" * 64,
                "account_generation": 1,
                "adapter_contract_sha256": "1" * 64,
                "client_runtime_identity": "nautilus-trader==1.231.0",
            }
        ],
        "corpus_receipt_sha256s": ["2" * 64],
        "future_result_receipt_sha256s": ["3" * 64],
        "promotion_grant_sha256s": ["4" * 64],
        "risk_policy_sha256s": ["5" * 64],
        "canary_intent_ids": ["6" * 64],
        "restart_drill": restart,
        "acceptance_window": _window().model_dump(mode="json"),
        "approval_statement": "I_APPROVE_EXACT_RELEASE_CANDIDATE_FOR_FIXED_ACCEPTANCE_WINDOW",
        "approved_by": "operator",
        "approved_at_ms": START - 1,
    }


def test_fixed_window_is_exactly_seven_days_and_has_nonzero_activity_floors() -> None:
    assert len(_window().window_sha256) == 64
    with pytest.raises(ValueError, match="evidence_acceptance_window_not_seven_days"):
        FixedWindowAcceptanceV1(
            **(_window().model_dump() | {"end_ms": END + 1}),
        )


def test_release_approval_digest_covers_every_identity_and_window() -> None:
    payload = _release_payload()
    payload["approval_sha256"] = canonical_sha256(payload)
    release = ProductionReleaseCandidateV1.model_validate(payload)

    assert len(release.release_sha256) == 64
    with pytest.raises(ValueError, match="evidence_release_approval_identity_invalid"):
        ProductionReleaseCandidateV1.model_validate(payload | {"openapi_sha256": "0" * 64})


def test_verification_report_is_canonical_and_any_failed_check_is_terminal() -> None:
    binding_report = ({"binding": "BINANCE_USDM", "case_count": 1},)
    report = verification_report(
        subject="fixed-window:test",
        verified_at_ms=START,
        checks=[
            EvidenceVerificationCheckV1(code="z_pass", passed=True),
            EvidenceVerificationCheckV1(code="a_failure", passed=False),
        ],
        binding_report=binding_report,
    )

    assert report.terminal == "FAILED"
    assert report.failure_codes == ("a_failure",)
    assert tuple(check.code for check in report.checks) == ("a_failure", "z_pass")
    changed = verification_report(
        subject="fixed-window:test",
        verified_at_ms=START,
        checks=list(report.checks),
        binding_report=({"binding": "BINANCE_USDM", "case_count": 2},),
    )
    assert changed.binding_report_sha256 != report.binding_report_sha256
    assert changed.report_sha256 != report.report_sha256


def test_rollback_requires_new_workers_and_serve_generations() -> None:
    payload = _release_payload()
    payload["approval_sha256"] = canonical_sha256(payload)
    release = ProductionReleaseCandidateV1.model_validate(payload)
    rolled_back_at_ms = release.acceptance_window.drain_cutoff_ms
    unsigned = {
        "rollback_version": "production_v3_rollback_receipt_v2",
        "release_candidate_sha256": release.release_sha256,
        "release_candidate_artifact_path": "release.json",
        "bindings": ["BINANCE_USDM"],
        "grant_sha256s": ["4" * 64],
        "rolled_back_at_ms": rolled_back_at_ms,
        "restart_workers_runtime_id": "00000000-0000-0000-0000-000000000020",
        "restart_serve_runtime_id": "00000000-0000-0000-0000-000000000021",
        "rolled_back_by": "operator",
        "statement": "ALL_ENABLED_VENUES_FLAT_GRANTS_REVOKED_CAPITAL_PAUSED_NO_TERMINAL_INTENT_REVIVAL",
    }
    receipt = ProductionRollbackReceiptV2.model_validate(unsigned | {"receipt_sha256": canonical_sha256(unsigned)})
    snapshot = {
        "control": "PAUSED",
        "decision_runtime": {"state": "RUNNING", "heartbeat_at_ms": rolled_back_at_ms},
        "workers_runtime": {
            "runtime_id": str(receipt.restart_workers_runtime_id),
            "lifecycle_state": "running",
            "started_at_ms": rolled_back_at_ms,
            "heartbeat_at_ms": rolled_back_at_ms,
        },
        "release_registration": {
            "workers_runtime_id": "00000000-0000-0000-0000-000000000010",
            "serve_runtime_id": "00000000-0000-0000-0000-000000000011",
        },
        "active_intent_count": 0,
        "active_risk_count": 0,
        "post_rollback_intent_count": 0,
        "post_rollback_submission_count": 0,
        "bindings": [
            {
                "binding": "BINANCE_USDM",
                "runtime_state": "ready",
                "account_state": "reconciled_flat",
                "active_arm_receipt_sha256": None,
                "heartbeat_at_ms": rolled_back_at_ms,
                "catalog_state": "ready",
                "capability_state": "ready",
            }
        ],
        "grants": [{"grant_sha256": "4" * 64, "expires_at_ms": rolled_back_at_ms, "revoked_at_ms": None}],
    }
    serve = ServeRuntimeObservationV1(
        runtime_id=receipt.restart_serve_runtime_id,
        runtime_revision=release.git_commit_sha,
        image_digest=release.oci_image_digest,
        started_at_ms=rolled_back_at_ms,
        measured_at_ms=rolled_back_at_ms,
    )

    checks = rollback_verification_checks(receipt, release, snapshot, serve=serve, now_ms=rolled_back_at_ms)
    assert all(check.passed for check in checks)
    old_serve = serve.model_copy(update={"runtime_id": snapshot["release_registration"]["serve_runtime_id"]})
    failed = rollback_verification_checks(receipt, release, snapshot, serve=old_serve, now_ms=rolled_back_at_ms)
    assert {check.code: check.passed for check in failed}["rollback_observer_continues"] is False
    stale_flat = {
        **snapshot,
        "bindings": [{**snapshot["bindings"][0], "heartbeat_at_ms": rolled_back_at_ms - 1}],
    }
    stale_checks = rollback_verification_checks(receipt, release, stale_flat, serve=serve, now_ms=rolled_back_at_ms)
    assert {check.code: check.passed for check in stale_checks}["rollback_bindings_authoritatively_flat"] is False
    missing_heartbeat = {
        **snapshot,
        "bindings": [{**snapshot["bindings"][0], "heartbeat_at_ms": None}],
    }
    missing_checks = rollback_verification_checks(
        receipt, release, missing_heartbeat, serve=serve, now_ms=rolled_back_at_ms
    )
    assert {check.code: check.passed for check in missing_checks}["rollback_bindings_authoritatively_flat"] is False


def test_case_verifier_rejects_an_allowed_case_without_its_one_intent() -> None:
    checks = case_verification_checks(
        {
            "case": {"policy_decision": "long", "capital_disposition": "allowed"},
            "gate_count": 1,
            "intents": [],
        }
    )

    by_code = {check.code: check for check in checks}
    assert by_code["case_intent_conservation"].passed is False


def test_fixed_window_verifier_binds_the_exact_workers_release() -> None:
    counts = {
        "wrong_gate_contract_count": 0,
        "wrong_release_source_count": 0,
        "intent_release_mismatch_count": 0,
        "gate_source_count": 4,
        "unique_source_count": 4,
        "admitted_source_count": 3,
        "rejected_or_deferred_source_count": 1,
        "case_count": 3,
        "intent_count": 2,
        "closed_flat_count": 1,
        "invalid_gate_link_count": 0,
        "case_without_gate_count": 0,
        "case_disposition_missing_count": 0,
        "allowed_case_intent_mismatch_count": 0,
        "blocked_case_intent_mismatch_count": 0,
        "non_v3_intent_count": 0,
        "nonterminal_intent_count": 0,
        "exposure_unknown_or_active_count": 0,
        "provider_write_before_fence_count": 0,
        "unprotected_fill_count": 0,
        "closed_flat_proof_missing_count": 0,
        "financial_accounting_missing_count": 0,
    }
    snapshot = {
        "counts": counts,
        "gate_source_keys": tuple(f"oi:event-{index}:open_interest_delta_v1" for index in range(4)),
        "workers_runtime": {
            "runtime_id": "00000000-0000-0000-0000-000000000010",
            "runtime_revision": "0" * 40,
            "image_digest": _window().oci_image_digest,
            "lifecycle_state": "running",
            "started_at_ms": START - 100,
            "heartbeat_at_ms": END,
        },
        "release_registration": {
            "release_sha256": "f" * 64,
            "window_sha256": _window().window_sha256,
            "release_tag": _window().release_tag,
            "git_commit_sha": _window().git_commit_sha,
            "oci_image_digest": _window().oci_image_digest,
            "registered_at_ms": START - 1,
            "workers_runtime_id": "00000000-0000-0000-0000-000000000010",
            "workers_started_at_ms": START - 100,
            "serve_runtime_id": "00000000-0000-0000-0000-000000000011",
            "serve_started_at_ms": START - 100,
        },
    }

    sources = tuple(
        FixedWindowSourceFactV1(
            event_id=f"event-{index}",
            metric_version="open_interest_delta_v1",
            source_venue="binance.perp",
            observed_at_ms=START + index,
            available_at_ms=START + index,
            verdict_created_at_ms=START + index,
        )
        for index in range(4)
    )
    serve = ServeRuntimeObservationV1(
        runtime_id="00000000-0000-0000-0000-000000000011",
        runtime_revision="1" * 40,
        image_digest=_window().oci_image_digest,
        started_at_ms=START - 100,
        measured_at_ms=END,
    )
    checks = fixed_window_verification_checks(
        _window(), snapshot, sources=sources, serve=serve, now_ms=_window().drain_cutoff_ms
    )

    by_code = {check.code: check for check in checks}
    assert by_code["window_exact_workers_release"].passed is False
    assert all(check.passed for code, check in by_code.items() if code != "window_exact_workers_release")

    missing_gate_snapshot = {**snapshot, "gate_source_keys": snapshot["gate_source_keys"][:-1]}
    missing_gate_checks = fixed_window_verification_checks(
        _window(), missing_gate_snapshot, sources=sources, serve=serve, now_ms=_window().drain_cutoff_ms
    )
    assert {check.code: check.passed for check in missing_gate_checks}["window_source_gate_conservation"] is False
