from __future__ import annotations

import pytest

from tests.postgres_test_utils import connect_postgres_test
from tests.support.news_judgment import news_taxonomy
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.artifact_identity import canonical_sha, runtime_manifest_sha
from tracefold.news.learning.contracts import ArmManifest, CandidateManifest, ProposalReceipt
from tracefold.news.learning.metric import (
    METRIC_ID,
    ProductionRegressionGateEvidenceV1,
    metric_contract_sha256,
)
from tracefold.news.learning.profile import TRUSTED_ROOT_SHA
from tracefold.news.learning.taxonomy import (
    TaxonomyCandidateRegistrationV1,
    TaxonomyEvaluationContextV1,
    build_taxonomy_evaluation_report,
    taxonomy_code_identity,
    verify_taxonomy_active_deployment,
    verify_taxonomy_candidate_registration,
    verify_taxonomy_regression_gates,
)
from tracefold.news.models import TRIAGE_POLICY_VERSION
from tracefold.news.program.artifact import load_stable_program_artifact
from tracefold.news.program.identity import EXECUTION_ENVELOPE_SHA256
from tracefold.news.program.runtime import PROGRAM_VERSION
from tracefold.news.review.desk import REVIEW_RUBRIC_VERSION
from tracefold.news.triage_rules import DEFAULT_POLICY

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_clone_dsn")]


def _gate_receipt(name: str, registration: TaxonomyCandidateRegistrationV1) -> dict[str, object]:
    gate_evidence = ProductionRegressionGateEvidenceV1.model_validate(
        {
            "gate": name,
            "metric_id": registration.metric_id,
            "metric_sha256": registration.metric_sha256,
            "denominator_n": 1,
            "stable_failure_n": 0,
            "candidate_failure_n": 0,
            "candidate_only_regression_n": 0,
            "candidate_only_case_ids": (),
            "outcome": "pass",
        }
    )
    return {
        "gate": name,
        "outcome": "PASS",
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


def test_taxonomy_candidate_registration_uses_durable_content_and_database_time() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        stable_artifact = load_stable_program_artifact()
        code_identity = taxonomy_code_identity()
        manifest_sha = runtime_manifest_sha(
            stable_bundle_sha="9" * 64,
            candidate_shas=(),
            image_digest="sha256:" + "b" * 64,
            runtime_revision="a" * 40,
        )
        repos = repositories_for_connection(conn)
        with repos.transaction():
            repos.news.register_agent_runtime_manifest(
                manifest_sha="8" * 64,
                stable_bundle_sha="9" * 64,
                envelope_sha256=EXECUTION_ENVELOPE_SHA256,
                artifact_schema_version="news_program_strategy_artifact_v1",
                program_version=PROGRAM_VERSION,
                program_sha256=stable_artifact.program_sha256,
                candidate_shas=(),
                image_digest="sha256:" + "b" * 64,
                runtime_revision="a" * 40,
                now_ms=800,
            )
        with pytest.raises(ValueError, match="news_taxonomy_active_deployment_mismatch"):
            verify_taxonomy_active_deployment(conn, stable_bundle_sha256="9" * 64)
        with repos.transaction():
            repos.news.register_agent_runtime_manifest(
                manifest_sha=manifest_sha,
                stable_bundle_sha="9" * 64,
                envelope_sha256=EXECUTION_ENVELOPE_SHA256,
                artifact_schema_version="news_program_strategy_artifact_v1",
                program_version=PROGRAM_VERSION,
                program_sha256=stable_artifact.program_sha256,
                candidate_shas=(),
                image_digest="sha256:" + "b" * 64,
                runtime_revision="a" * 40,
                now_ms=900,
            )
            deployment = verify_taxonomy_active_deployment(
                conn,
                stable_bundle_sha256="9" * 64,
            )
            registration = TaxonomyCandidateRegistrationV1.issue(
                code_identity=code_identity,
                deployment=deployment,
                policy_sha256="b" * 64,
                runtime_model_bindings_sha256="c" * 64,
                taxonomy_program_sha256="d" * 64,
                taxonomy_model_binding_sha256="e" * 64,
                registered_at_ms=1_000,
            )
            artifact_sha = repos.news.append_proposal_artifact(
                kind="candidate_registration",
                payload=registration.model_dump(mode="json"),
                parent_sha=None,
                created_at_ms=registration.registered_at_ms,
            )

        verified = verify_taxonomy_candidate_registration(
            conn,
            artifact_sha,
            code_identity=code_identity,
            stable_bundle_sha256="9" * 64,
            runtime_model_bindings_sha256="c" * 64,
            policy_sha256="b" * 64,
        )

        assert verified == registration
        with pytest.raises(ValueError, match="news_taxonomy_candidate_registration_missing"):
            verify_taxonomy_candidate_registration(
                conn,
                "f" * 64,
                code_identity=code_identity,
                stable_bundle_sha256="9" * 64,
                runtime_model_bindings_sha256="c" * 64,
                policy_sha256="b" * 64,
            )
        active = conn.execute(
            "SELECT artifact_sha, payload FROM news_learning_artifacts "
            "WHERE kind = 'active_agent' ORDER BY created_at_ms DESC LIMIT 1"
        ).fetchone()
        with repos.transaction():
            repos.news.learning_artifact_read_back(
                "deployment_receipt",
                {
                    "action": "runtime_deploy",
                    "active_agent_sha": str(active["artifact_sha"]),
                    "stable_sha": "9" * 64,
                    "image_digest": "sha256:" + "b" * 64,
                    "runtime_revision": "f" * 40,
                    "previous_stable_sha": None,
                    "previous_image_digest": None,
                    "deployed_at_ms": 900,
                    "rollback_available_until_ms": 900 + 24 * 3_600_000,
                },
                parent_sha=str(active["artifact_sha"]),
                created_by="taxonomy-integration-test",
                now_ms=901,
            )
        with pytest.raises(ValueError, match="news_taxonomy_active_deployment_mismatch"):
            verify_taxonomy_active_deployment(conn, stable_bundle_sha256="9" * 64)

        replacement_candidates = ("d" * 64,)
        replacement_manifest_sha = runtime_manifest_sha(
            stable_bundle_sha="9" * 64,
            candidate_shas=replacement_candidates,
            image_digest="sha256:" + "b" * 64,
            runtime_revision="a" * 40,
        )
        with repos.transaction():
            repos.news.register_agent_runtime_manifest(
                manifest_sha=replacement_manifest_sha,
                stable_bundle_sha="9" * 64,
                envelope_sha256=EXECUTION_ENVELOPE_SHA256,
                artifact_schema_version="news_program_strategy_artifact_v1",
                program_version=PROGRAM_VERSION,
                program_sha256=stable_artifact.program_sha256,
                candidate_shas=replacement_candidates,
                image_digest="sha256:" + "b" * 64,
                runtime_revision="a" * 40,
                now_ms=951,
            )
        verify_taxonomy_active_deployment(conn, stable_bundle_sha256="9" * 64)

        complete = conn.execute(
            "SELECT artifact_sha, payload FROM news_learning_artifacts "
            "WHERE kind = 'active_agent' ORDER BY created_at_ms DESC LIMIT 1"
        ).fetchone()
        with repos.transaction():
            repos.news.learning_artifact_read_back(
                "active_agent",
                {
                    **dict(complete["payload"]),
                    "registered_at_ms": 952,
                },
                parent_sha=str(complete["artifact_sha"]),
                created_by="taxonomy-integration-test",
                now_ms=952,
            )
        with pytest.raises(ValueError, match="news_taxonomy_active_deployment_mismatch"):
            verify_taxonomy_active_deployment(conn, stable_bundle_sha256="9" * 64)
    finally:
        conn.close()


def test_taxonomy_evaluation_appends_through_the_existing_learning_ledger() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        taxonomy = news_taxonomy(
            subject_codes=("medtop:20000205",),
            event_family="product_service_change",
            change_state="effective",
            assertion_status="confirmed",
            source_authority="issuer_first_party",
        )
        registration = TaxonomyCandidateRegistrationV1(
            tested_git_sha="a" * 40,
            program_version="news_semantic_program_v7",
            program_sha256="1" * 64,
            stable_bundle_sha256="9" * 64,
            runtime_manifest_sha256="7" * 64,
            image_digest="sha256:" + "8" * 64,
            deployment_receipt_sha256="0" * 64,
            envelope_sha256="2" * 64,
            metric_id="tracefold.news.production_action_trade_relevance_v6",
            metric_sha256="6" * 64,
            policy_version="news_triage_policy_v10",
            policy_sha256="c" * 64,
            runtime_model_bindings_sha256="b" * 64,
            taxonomy_program_sha256="e" * 64,
            taxonomy_model_binding_sha256="f" * 64,
            registered_at_ms=2_000_000_000_000,
        )
        report = build_taxonomy_evaluation_report(
            [
                {
                    "case_id": "taxonomy-case-1",
                    "cluster_id": "taxonomy-cluster-1",
                    "event_id": "taxonomy-event-1",
                    "split": "development",
                    "opened_at_ms": 1,
                    "gold": taxonomy.model_dump(mode="json"),
                    "prediction": taxonomy.model_dump(mode="json"),
                    "gold_receipt": {
                        "review_id": canonical_sha({"case_id": "taxonomy-case-1"}),
                        "acceptance_id": canonical_sha(
                            {
                                "kind": "acceptance",
                                "review_id": canonical_sha({"case_id": "taxonomy-case-1"}),
                            }
                        ),
                        "rubric_version": "news_review_v5",
                        "reviewer": "taxonomy-integration-reviewer",
                        "accepted_at_ms": 1,
                        "release_eligible": True,
                    },
                }
            ],
            context=TaxonomyEvaluationContextV1(
                candidate_registration_sha256=registration.artifact_sha256,
                candidate_registration=registration,
                gold_ledger_root_sha256="d" * 64,
                regression_gates={
                    name: _gate_receipt(name, registration)
                    for name in ("production_action", "asset_grounding", "novelty", "trade_relevance")
                },
            ),
        )
        repos = repositories_for_connection(conn)
        with repos.transaction():
            artifact_sha = repos.news.learning_artifact_read_back(
                "evaluation_report",
                report.model_dump(mode="json"),
                parent_sha=None,
                created_by="taxonomy-integration-test",
                now_ms=2,
            )

        row = conn.execute(
            "SELECT kind, payload, created_by FROM news_learning_artifacts WHERE artifact_sha=%s",
            (artifact_sha,),
        ).fetchone()
        assert row["kind"] == "evaluation_report"
        assert row["payload"]["schema_id"] == "tracefold.news.taxonomy_evaluation_report.v1"
        assert row["payload"]["outcome"] == "UNKNOWN"
        assert row["created_by"] == "taxonomy-integration-test"
    finally:
        conn.close()


def test_taxonomy_regression_outcomes_are_derived_from_durable_current_evidence() -> None:
    conn = connect_postgres_test(read_only=False)
    try:
        code_identity = taxonomy_code_identity()
        policy = DEFAULT_POLICY.as_dict()
        stable = ArmManifest(
            program_version=PROGRAM_VERSION,
            program_sha256=load_stable_program_artifact().program_sha256,
            envelope_sha256=EXECUTION_ENVELOPE_SHA256,
            runtime_model_bindings_sha256="c" * 64,
            retrieval_sha256="7" * 64,
            policy=policy,
            policy_sha256=canonical_sha(policy),
        )
        development_payload = {"role": "development", "case_ids": ["case-development"]}
        validation_payload = {"role": "validation", "case_ids": ["case-holdout"]}
        development_sha = canonical_sha({"kind": "dataset", "payload": development_payload})
        validation_sha = canonical_sha({"kind": "dataset", "payload": validation_payload})
        candidate_arm = ArmManifest.model_validate(stable.model_dump(mode="json") | {"program_sha256": "8" * 64})
        proposal = ProposalReceipt.issue(
            development_dataset_sha=development_sha,
            development_episode_projection_root_sha256="1" * 64,
            failure_cluster_ids=("taxonomy",),
            generator_kind="human",
            registered_at_ms=1,
            declared_target_dimensions=("taxonomy_event_family",),
            guardrails=("production_action",),
            program_parent_sha256=stable.program_sha256,
            program_candidate_sha256=candidate_arm.program_sha256,
            prompt_candidate_sha256="2" * 64,
        )
        candidate = CandidateManifest(
            parent_stable_sha=stable.bundle_sha,
            candidate_arm=candidate_arm,
            hypothesis="Verify taxonomy without regressing current hard gates.",
            target_dimensions=("taxonomy_event_family",),
            development_dataset_sha=development_sha,
            proposal_receipt=proposal,
        )
        candidate_payload = {
            "candidate_sha": candidate.candidate_sha,
            "candidate_bundle_sha": candidate.candidate_arm.bundle_sha,
            "proposal_sha": "3" * 64,
            "manifest": candidate.model_dump(mode="json"),
            "exact_diff": {"program_sha256": candidate.candidate_arm.program_sha256},
        }
        metric_sha = metric_contract_sha256(review_rubric_version=REVIEW_RUBRIC_VERSION)
        report_payload = {
            "run_sha": "4" * 64,
            "run_state": "complete",
            "gate_outcome": "pass",
            "eligibility": "current",
            "next_stage": "shadow",
            "recommended_action": "advance",
            "evidence": {
                "trusted_root_sha": TRUSTED_ROOT_SHA,
                "stable_sha": stable.bundle_sha,
                "candidate_sha": candidate.candidate_sha,
                "development_dataset_sha": development_sha,
                "validation_dataset_sha": validation_sha,
                "metric_id": METRIC_ID,
                "metric_sha256": metric_sha,
                "regression_gates": {
                    name: ProductionRegressionGateEvidenceV1(
                        gate=name,
                        metric_sha256=metric_sha,
                        denominator_n=1,
                        stable_failure_n=0,
                        candidate_failure_n=int(name == "novelty"),
                        candidate_only_regression_n=int(name == "novelty"),
                        candidate_only_case_ids=(("case-holdout",) if name == "novelty" else ()),
                        outcome="fail" if name == "novelty" else "pass",
                    ).model_dump(mode="json")
                    for name in ("production_action", "asset_grounding", "novelty", "trade_relevance")
                },
            },
        }
        report_sha = canonical_sha({"kind": "evaluation_report", "payload": report_payload})
        release_payload = {
            "report_sha": report_sha,
            "run_sha": report_payload["run_sha"],
            "candidate_sha": candidate.candidate_sha,
            "gate_outcome": "pass",
            "stage": "holdout",
            "trusted_root_sha": TRUSTED_ROOT_SHA,
        }
        registration = TaxonomyCandidateRegistrationV1(
            tested_git_sha="a" * 40,
            program_version=PROGRAM_VERSION,
            program_sha256=stable.program_sha256,
            stable_bundle_sha256=stable.bundle_sha,
            runtime_manifest_sha256="7" * 64,
            image_digest="sha256:" + "8" * 64,
            deployment_receipt_sha256="0" * 64,
            envelope_sha256=EXECUTION_ENVELOPE_SHA256,
            metric_id=METRIC_ID,
            metric_sha256=metric_sha,
            policy_version=TRIAGE_POLICY_VERSION,
            policy_sha256=stable.policy_sha256,
            runtime_model_bindings_sha256=stable.runtime_model_bindings_sha256,
            taxonomy_program_sha256="d" * 64,
            taxonomy_model_binding_sha256="e" * 64,
            registered_at_ms=1,
        )
        repos = repositories_for_connection(conn)
        with repos.transaction():
            repos.news.learning_artifact_read_back(
                "dataset", development_payload, parent_sha=None, created_by="test", now_ms=1
            )
            repos.news.learning_artifact_read_back(
                "dataset", validation_payload, parent_sha=candidate.candidate_sha, created_by="test", now_ms=2
            )
            repos.news.learning_artifact_read_back(
                "candidate", candidate_payload, parent_sha=stable.bundle_sha, created_by="test", now_ms=3
            )
            repos.news.learning_artifact_read_back(
                "evaluation_report",
                report_payload,
                parent_sha=candidate.candidate_sha,
                created_by="test",
                now_ms=4,
            )
            evidence_sha = repos.news.learning_artifact_read_back(
                "release_evidence", release_payload, parent_sha=report_sha, created_by="test", now_ms=5
            )

        references = {
            name: {"evidence_sha256": evidence_sha}
            for name in ("production_action", "asset_grounding", "novelty", "trade_relevance")
        }
        verified = verify_taxonomy_regression_gates(
            conn,
            references,
            code_identity=code_identity,
            registration=registration,
        )

        assert verified["novelty"].outcome == "FAIL"
        assert {verified[name].outcome for name in references if name != "novelty"} == {"PASS"}
        assert len({receipt.gate_evidence_sha256 for receipt in verified.values()}) == 4
        assert {receipt.candidate_sha256 for receipt in verified.values()} == {candidate.candidate_sha}
        assert {receipt.dataset_sha256 for receipt in verified.values()} == {validation_sha}
        with pytest.raises(ValueError, match="news_taxonomy_regression_evidence_missing"):
            verify_taxonomy_regression_gates(
                conn,
                references | {"novelty": {"evidence_sha256": "f" * 64}},
                code_identity=code_identity,
                registration=registration,
            )
    finally:
        conn.close()
