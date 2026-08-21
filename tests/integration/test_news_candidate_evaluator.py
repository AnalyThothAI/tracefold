from __future__ import annotations

import asyncio
import hashlib
import json
import uuid

import pytest

from tests.integration.test_news_review_desk import NOW, PRINCIPAL, _open_event, _rubric
from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tracefold.app.repositories import repositories_for_connection
from tracefold.news import (
    ArmManifest,
    BlindPairwiseSubmission,
    CandidateEvaluator,
    CandidateManifest,
    ClosedWindow,
    DatasetSpec,
    DeskQuery,
    EvaluationRequest,
    ExternalMissSubmission,
    ModelInvocation,
    ModelObservation,
    ProposalReceipt,
    RecordReplayModelAdapter,
    ReviewDesk,
    TaskRef,
)
from tracefold.news.agents.prompts import (
    TRIAGE_PROMPT_SHA256,
    TRIAGE_SCHEMA_SHA256,
    TRIAGE_SYSTEM_PROMPT,
)
from tracefold.news.triage_rules import DEFAULT_POLICY

pytestmark = pytest.mark.integration


@pytest.fixture()
def conn():
    connection = connect_postgres_test(read_only=False)
    migrate(connection)
    stable = _arm()
    with repositories_for_connection(connection).transaction():
        repositories_for_connection(connection).news.register_agent_runtime_manifest(
            manifest_sha=_sha({"stable": stable.bundle_sha, "runtime": "test"}),
            stable_bundle_sha=stable.bundle_sha,
            candidate_shas=(),
            image_digest="sha256:test-image",
            runtime_revision="test-revision",
            now_ms=NOW - 24 * 3_600_000,
        )
    yield connection
    connection.close()


def _sha(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _arm(*, policy: dict[str, object] | None = None, model: str = "test-model") -> ArmManifest:
    selected_policy = policy or DEFAULT_POLICY.as_dict()
    return ArmManifest(
        prompt_version="news_triage_prompt_v9",
        prompt_text=TRIAGE_SYSTEM_PROMPT,
        prompt_sha256=TRIAGE_PROMPT_SHA256,
        schema_sha256=TRIAGE_SCHEMA_SHA256,
        retrieval_sha256=_sha({"told": "v1", "evidence": "v1"}),
        provider="test",
        model=model,
        model_snapshot_kind="immutable_revision",
        model_revision="test-revision-1",
        model_sha256=_sha({"provider": "test", "model": model, "revision": "test-revision-1"}),
        execution_contract_sha256=_sha({"temperature": 0, "structured": True}),
        policy=selected_policy,
        policy_sha256=_sha(selected_policy),
    )


def _proposal(conn, **values: object) -> ProposalReceipt:
    receipt = ProposalReceipt.issue(**values)
    conn.execute(
        "INSERT INTO news_learning_artifacts "
        "(artifact_sha, kind, parent_sha, payload, created_by, created_at_ms) "
        "VALUES (%s, 'candidate_registration', %s, %s::jsonb, 'test', %s)",
        (
            receipt.registration_receipt_sha,
            receipt.development_dataset_sha,
            json.dumps(receipt.registration_payload, sort_keys=True),
            receipt.registered_at_ms,
        ),
    )
    return receipt


class _StaticModelAdapter:
    def __init__(self) -> None:
        self.calls: list[ModelInvocation] = []

    async def invoke(self, invocation: ModelInvocation) -> ModelObservation:
        self.calls.append(invocation)
        return ModelObservation(
            verdict={
                "novelty": "new_fact",
                "restates": -1,
                "event_type": "macro",
                "assets": [],
                "direction": "bullish",
                "scope": "sector",
                "magnitude": 2,
                "actionable": True,
                "confidence": 0.8,
                "decision": "push",
                "audience": "us_equity",
                "headline_zh": "DRAM 合约价续涨",
                "title_zh": "",
                "why_zh": "行业价格继续改善，但持续性仍需后续数据确认。",
            },
            latency_ms=900,
            input_tokens=500,
            output_tokens=90,
            finish_reason="stop",
        )


class _AlwaysUnavailableAdapter:
    def __init__(self) -> None:
        self.calls: list[ModelInvocation] = []

    async def invoke(self, invocation: ModelInvocation) -> ModelObservation:
        self.calls.append(invocation)
        return ModelObservation(error_code="provider_unavailable")


class _StabilityAdapter(_StaticModelAdapter):
    async def invoke(self, invocation: ModelInvocation) -> ModelObservation:
        observation = await super().invoke(invocation)
        verdict = dict(observation.verdict or {})
        if invocation.arm == "candidate":
            verdict["headline_zh"] = "候选：DRAM 合约价续涨"
        if invocation.arm == "candidate" and invocation.trial == 3:
            verdict.update(magnitude=0, actionable=False, decision="drop")
        return observation.model_copy(update={"verdict": verdict})


def _prompt_candidate(
    conn,
    *,
    stable: ArmManifest,
    development_sha: str,
    cluster_id: str,
) -> CandidateManifest:
    prompt_text = TRIAGE_SYSTEM_PROMPT + "\nNever call a fact priced-in without source or told-ledger evidence."
    arm_payload = stable.model_dump(mode="json")
    arm_payload.update(
        prompt_version="candidate-why-support",
        prompt_text=prompt_text,
        prompt_sha256=hashlib.sha256(prompt_text.encode()).hexdigest(),
    )
    candidate_arm = ArmManifest.model_validate(arm_payload)
    registered_at_ms = int(
        conn.execute("SELECT floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint AS n").fetchone()["n"]
    )
    return CandidateManifest(
        target="prompt",
        parent_stable_sha=stable.bundle_sha,
        candidate_arm=candidate_arm,
        hypothesis="Remove unsupported priced-in dismissals without changing reader load.",
        target_dimensions=("why_support",),
        development_dataset_sha=development_sha,
        proposal_receipt=_proposal(
            conn,
            development_dataset_sha=development_sha,
            failure_cluster_ids=(cluster_id,),
            generator_kind="human",
            registered_at_ms=registered_at_ms,
            candidate_patch_sha=_sha({"prompt_sha": candidate_arm.prompt_sha256}),
            declared_target_dimensions=("why_support",),
            guardrails=("must_push_recall", "reader_load"),
        ),
    )


def _insert_validation_dataset(conn, *, development, candidate: CandidateManifest) -> str:
    payload = {
        "dataset_version": "news_learning_dataset_v1",
        "role": "validation",
        "profile_id": "news_learning_release_v1",
        "window": {"from_ms": NOW - 6 * 3_600_000, "to_ms": NOW},
        "freeze_as_of_ms": NOW + 10_000,
        "settlement_grace_ms": 10 * 60_000,
        "reader_contract_version": development.reader_contract_version,
        "agent_cohort": dict(development.agent_cohort),
        "observation_ref": candidate.candidate_sha,
        "cases": [case.model_dump(mode="json") for case in development.cases],
        "seed_receipts": list(development.seed_receipts),
        "counts": {
            **development.counts,
            "window_duration_hours": 6.0,
            "eligible_event_n": 1,
        },
        "hashes": dict(development.hashes),
    }
    artifact_sha = _sha({"kind": "dataset", "payload": payload})
    conn.execute(
        "INSERT INTO news_learning_artifacts "
        "(artifact_sha, kind, parent_sha, payload, created_by, created_at_ms) "
        "VALUES (%s, 'dataset', %s, %s::jsonb, 'test', %s)",
        (artifact_sha, candidate.candidate_sha, json.dumps(payload, sort_keys=True), NOW),
    )
    return artifact_sha


def _insert_stage_pass(conn, *, candidate_sha: str, stage: str) -> None:
    report_sha = "d" * 64
    payload = {
        "report_sha": report_sha,
        "run_sha": "e" * 64,
        "candidate_sha": candidate_sha,
        "gate_outcome": "pass",
        "stage": stage,
        "trusted_root_sha": "f" * 64,
    }
    conn.execute(
        "INSERT INTO news_learning_artifacts "
        "(artifact_sha, kind, parent_sha, payload, created_by, created_at_ms) "
        "VALUES (%s, 'release_evidence', %s, %s::jsonb, 'test', %s)",
        (_sha({"kind": "release_evidence", "payload": payload}), report_sha, json.dumps(payload), NOW),
    )


def _accepted_event(conn, *, why: str = "fail") -> str:
    event_id = _open_event(conn)
    stable = _arm()
    conn.execute(
        "UPDATE news_verdicts SET trace = trace || %s::jsonb WHERE event_id = %s AND stage = 'triage'",
        (json.dumps({"agent_assignment": {"arm": "stable", "bundle_sha": stable.bundle_sha}}), event_id),
    )
    desk = ReviewDesk(conn, now_ms=NOW)
    task = desk.open(
        # Event lookup is deterministic; no random queue sampling is involved.
        DeskQuery(event=event_id),
        principal=PRINCIPAL,
    )["tasks"][0]
    with repositories_for_connection(conn).transaction():
        desk.submit(
            TaskRef(task_id=task["task_id"], task_version=task["task_version"]),
            _rubric(why=why),
            principal=PRINCIPAL,
            idempotency_key=str(uuid.uuid4()),
        )
    return event_id


def test_policy_candidate_freezes_accepted_evidence_and_uses_zero_model_calls(conn) -> None:
    event_id = _accepted_event(conn)
    stable = _arm()
    adapter = RecordReplayModelAdapter({})
    evaluator = CandidateEvaluator(conn, stable=stable, model_adapter=adapter)
    development = asyncio.run(
        evaluator.freeze_dataset(
            DatasetSpec(
                role="development",
                window=ClosedWindow(from_ms=NOW - 6 * 3_600_000, to_ms=NOW),
            )
        )
    )
    assert development.counts["case_n"] == 1
    assert development.cases[0].event_id == event_id

    candidate_policy = dict(DEFAULT_POLICY.as_dict())
    candidate_policy["similarity_max"] = 0.30
    candidate_arm = _arm(policy=candidate_policy)
    registered_at_ms = int(
        conn.execute("SELECT floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint AS n").fetchone()["n"]
    )
    proposal = _proposal(
        conn,
        development_dataset_sha=development.artifact_sha,
        failure_cluster_ids=(development.cases[0].cluster_id,),
        generator_kind="human",
        registered_at_ms=registered_at_ms,
        candidate_patch_sha=_sha({"similarity_max": candidate_policy["similarity_max"]}),
        declared_target_dimensions=("reader_load",),
        guardrails=("must_push_recall",),
    )
    candidate = CandidateManifest(
        target="policy",
        parent_stable_sha=stable.bundle_sha,
        candidate_arm=candidate_arm,
        hypothesis="Tighten duplicate similarity without changing semantic judgment.",
        target_dimensions=("reader_load",),
        development_dataset_sha=development.artifact_sha,
        proposal_receipt=proposal,
    )
    evaluator = CandidateEvaluator(
        conn,
        stable=stable,
        model_adapter=adapter,
        candidate_catalog=(candidate,),
    )
    report = asyncio.run(
        evaluator.evaluate(
            EvaluationRequest(
                development_dataset_sha=development.artifact_sha,
                candidate_sha=candidate.candidate_sha,
                stage="offline",
            )
        )
    )
    conn.commit()

    assert adapter.calls == 0
    assert report.gate_outcome == "unknown"
    assert report.run_state == "complete"
    assert "development_boundary_cluster_n_insufficient" in report.evidence["blockers"]
    stored = conn.execute(
        "SELECT review_id, opened_at_ms, stable_observation, candidate_observation "
        "FROM news_learning_cases WHERE run_sha = %s",
        (report.run_sha,),
    ).fetchone()
    assert stored["review_id"] == development.cases[0].review_id
    assert stored["opened_at_ms"] == development.cases[0].opened_at_ms
    assert stored["stable_observation"]["delivery"] == "simulated"
    assert stored["candidate_observation"]["delivery"] == "simulated"
    candidate_artifact = conn.execute(
        "SELECT payload FROM news_learning_artifacts WHERE kind = 'candidate' AND payload->>'candidate_sha' = %s",
        (candidate.candidate_sha,),
    ).fetchone()["payload"]
    exact_diff = candidate_artifact["exact_diff"]
    assert exact_diff["target"] == "policy"
    assert exact_diff["changed_fields"] == ["policy", "policy_sha256"]
    assert exact_diff["values"] == {
        "similarity_max": {
            "stable": DEFAULT_POLICY.similarity_max,
            "candidate": candidate_policy["similarity_max"],
        }
    }


def test_active_stable_is_checked_before_freeze_or_model_work(conn) -> None:
    stale = _arm(model="other-model")
    adapter = _StaticModelAdapter()
    evaluator = CandidateEvaluator(conn, stable=stale, model_adapter=adapter)

    with pytest.raises(ValueError, match="news_learning_active_stable_mismatch"):
        asyncio.run(
            evaluator.freeze_dataset(
                DatasetSpec(
                    role="development",
                    window=ClosedWindow(from_ms=NOW - 6 * 3_600_000, to_ms=NOW),
                )
            )
        )
    assert adapter.calls == []


def test_successful_critical_case_cannot_authorize_a_failure_cluster(conn) -> None:
    _accepted_event(conn, why="pass")
    stable = _arm()
    bootstrap = CandidateEvaluator(conn, stable=stable, model_adapter=RecordReplayModelAdapter({}))
    development = asyncio.run(
        bootstrap.freeze_dataset(
            DatasetSpec(
                role="development",
                window=ClosedWindow(from_ms=NOW - 6 * 3_600_000, to_ms=NOW),
            )
        )
    )
    candidate = _prompt_candidate(
        conn,
        stable=stable,
        development_sha=development.artifact_sha,
        cluster_id=development.cases[0].cluster_id,
    )
    evaluator = CandidateEvaluator(
        conn,
        stable=stable,
        model_adapter=_StaticModelAdapter(),
        candidate_catalog=(candidate,),
    )

    with pytest.raises(ValueError, match="news_learning_proposal_failure_cluster_unverified"):
        asyncio.run(
            evaluator.evaluate(
                EvaluationRequest(
                    development_dataset_sha=development.artifact_sha,
                    candidate_sha=candidate.candidate_sha,
                    stage="offline",
                )
            )
        )


def test_k3_stability_reports_each_trial_and_pass_k(conn) -> None:
    _accepted_event(conn)
    stable = _arm()
    bootstrap = CandidateEvaluator(conn, stable=stable, model_adapter=RecordReplayModelAdapter({}))
    development = asyncio.run(
        bootstrap.freeze_dataset(
            DatasetSpec(
                role="development",
                window=ClosedWindow(from_ms=NOW - 6 * 3_600_000, to_ms=NOW),
            )
        )
    )
    candidate = _prompt_candidate(
        conn,
        stable=stable,
        development_sha=development.artifact_sha,
        cluster_id=development.cases[0].cluster_id,
    )
    adapter = _StabilityAdapter()
    evaluator = CandidateEvaluator(
        conn,
        stable=stable,
        model_adapter=adapter,
        candidate_catalog=(candidate,),
    )
    report = asyncio.run(
        evaluator.evaluate(
            EvaluationRequest(
                development_dataset_sha=development.artifact_sha,
                candidate_sha=candidate.candidate_sha,
                stage="offline",
            )
        )
    )

    candidate_stability = report.evidence["stability"]["candidate"]
    assert len(candidate_stability) == 1
    assert candidate_stability[0]["trials"] == 3
    assert candidate_stability[0]["pass_n"] == 2
    assert candidate_stability[0]["pass_k"] is False
    assert len(candidate_stability[0]["trial_results"]) == 3
    assert len(adapter.calls) == 6


def test_exact_one_variable_is_rejected_before_any_model_call(conn) -> None:
    _accepted_event(conn)
    stable = _arm()
    adapter = RecordReplayModelAdapter({})
    evaluator = CandidateEvaluator(conn, stable=stable, model_adapter=adapter)
    development = asyncio.run(
        evaluator.freeze_dataset(
            DatasetSpec(
                role="development",
                window=ClosedWindow(from_ms=NOW - 6 * 3_600_000, to_ms=NOW),
            )
        )
    )
    changed_model = _arm(model="different-model")
    candidate = CandidateManifest(
        target="prompt",
        parent_stable_sha=stable.bundle_sha,
        candidate_arm=changed_model,
        hypothesis="Invalid multi-variable candidate.",
        target_dimensions=("semantic_quality",),
        development_dataset_sha=development.artifact_sha,
        proposal_receipt=_proposal(
            conn,
            development_dataset_sha=development.artifact_sha,
            failure_cluster_ids=(development.cases[0].cluster_id,),
            generator_kind="human",
            registered_at_ms=NOW,
            candidate_patch_sha="1" * 64,
            declared_target_dimensions=("semantic_quality",),
        ),
    )
    evaluator = CandidateEvaluator(
        conn,
        stable=stable,
        model_adapter=adapter,
        candidate_catalog=(candidate,),
    )
    with pytest.raises(ValueError, match="news_learning_exact_one_variable_violation"):
        asyncio.run(
            evaluator.evaluate(
                EvaluationRequest(
                    development_dataset_sha=development.artifact_sha,
                    candidate_sha=candidate.candidate_sha,
                    stage="offline",
                )
            )
        )
    assert adapter.calls == 0


def test_record_replay_miss_is_explicit_unknown_without_live_fallback(conn) -> None:
    _accepted_event(conn)
    stable = _arm()
    prompt_text = TRIAGE_SYSTEM_PROMPT + "\nTreat unsupported priced-in claims as an error."
    candidate_payload = stable.model_dump(mode="json")
    candidate_payload.update(
        prompt_version="candidate-test",
        prompt_text=prompt_text,
        prompt_sha256=hashlib.sha256(prompt_text.encode()).hexdigest(),
    )
    candidate_arm = ArmManifest.model_validate(candidate_payload)
    adapter = RecordReplayModelAdapter({})
    first = CandidateEvaluator(conn, stable=stable, model_adapter=adapter)
    development = asyncio.run(
        first.freeze_dataset(
            DatasetSpec(
                role="development",
                window=ClosedWindow(from_ms=NOW - 6 * 3_600_000, to_ms=NOW),
            )
        )
    )
    candidate = CandidateManifest(
        target="prompt",
        parent_stable_sha=stable.bundle_sha,
        candidate_arm=candidate_arm,
        hypothesis="Forbid unsupported priced-in dismissals.",
        target_dimensions=("why_support",),
        development_dataset_sha=development.artifact_sha,
        proposal_receipt=_proposal(
            conn,
            development_dataset_sha=development.artifact_sha,
            failure_cluster_ids=(development.cases[0].cluster_id,),
            generator_kind="human",
            registered_at_ms=NOW,
            candidate_patch_sha=_sha({"prompt": candidate_arm.prompt_sha256}),
            declared_target_dimensions=("why_support",),
        ),
    )
    evaluator = CandidateEvaluator(
        conn,
        stable=stable,
        model_adapter=adapter,
        candidate_catalog=(candidate,),
    )
    report = asyncio.run(
        evaluator.evaluate(
            EvaluationRequest(
                development_dataset_sha=development.artifact_sha,
                candidate_sha=candidate.candidate_sha,
                stage="offline",
            )
        )
    )
    assert report.gate_outcome == "unknown"
    assert report.run_state == "incomplete"
    assert any(str(item).startswith("news_model_recording_missing:") for item in report.evidence["blockers"])
    assert adapter.calls == 1


def test_common_provider_outage_is_unknown_not_a_vacuous_candidate_pass(conn) -> None:
    _accepted_event(conn)
    stable = _arm()
    bootstrap = CandidateEvaluator(conn, stable=stable, model_adapter=RecordReplayModelAdapter({}))
    development = asyncio.run(
        bootstrap.freeze_dataset(
            DatasetSpec(
                role="development",
                window=ClosedWindow(from_ms=NOW - 6 * 3_600_000, to_ms=NOW),
            )
        )
    )
    candidate = _prompt_candidate(
        conn,
        stable=stable,
        development_sha=development.artifact_sha,
        cluster_id=development.cases[0].cluster_id,
    )
    adapter = _AlwaysUnavailableAdapter()
    evaluator = CandidateEvaluator(
        conn,
        stable=stable,
        model_adapter=adapter,
        candidate_catalog=(candidate,),
    )
    report = asyncio.run(
        evaluator.evaluate(
            EvaluationRequest(
                development_dataset_sha=development.artifact_sha,
                candidate_sha=candidate.candidate_sha,
                stage="offline",
            )
        )
    )

    assert len(adapter.calls) == 2
    assert report.gate_outcome == "unknown"
    assert report.run_state == "incomplete"
    assert report.evidence["common_error_n"] == 1
    assert report.evidence["candidate_only_error_n"] == 0
    assert "stable_or_common_execution_unavailable" in report.evidence["blockers"]
    assert "candidate_schema_or_provider_regression" not in report.evidence["failures"]


def test_blind_candidate_critical_error_is_a_release_failure(conn) -> None:
    _accepted_event(conn)
    stable = _arm()
    bootstrap = CandidateEvaluator(conn, stable=stable, model_adapter=RecordReplayModelAdapter({}))
    development = asyncio.run(
        bootstrap.freeze_dataset(
            DatasetSpec(
                role="development",
                window=ClosedWindow(from_ms=NOW - 6 * 3_600_000, to_ms=NOW),
            )
        )
    )
    candidate = _prompt_candidate(
        conn,
        stable=stable,
        development_sha=development.artifact_sha,
        cluster_id=development.cases[0].cluster_id,
    )
    evaluator = CandidateEvaluator(
        conn,
        stable=stable,
        model_adapter=_StaticModelAdapter(),
        candidate_catalog=(candidate,),
    )
    request = EvaluationRequest(
        development_dataset_sha=development.artifact_sha,
        candidate_sha=candidate.candidate_sha,
        stage="offline",
    )
    first = asyncio.run(evaluator.evaluate(request))
    assert "development_pairwise_review_incomplete" in first.evidence["blockers"]

    desk = ReviewDesk(conn, now_ms=NOW)
    task = desk.open(DeskQuery(mode="pairwise"), principal=PRINCIPAL)["tasks"][0]
    row = conn.execute(
        "SELECT comparison FROM news_learning_cases WHERE run_sha = %s",
        (first.run_sha,),
    ).fetchone()
    candidate_side = "A" if row["comparison"]["pair_order"] == "candidate_A" else "B"
    with repositories_for_connection(conn).transaction():
        desk.submit(
            TaskRef(task_id=task["task_id"], task_version=task["task_version"]),
            BlindPairwiseSubmission(
                preference=candidate_side,
                critical_errors=[f"{candidate_side}:unsupported_fact"],
                evidence_refs=[f"output:{candidate_side}"],
            ),
            principal=PRINCIPAL,
            idempotency_key=str(uuid.uuid4()),
        )

    report = asyncio.run(evaluator.evaluate(request))
    assert report.gate_outcome == "fail"
    assert "candidate_critical_error_regression" in report.evidence["failures"]
    assert report.evidence["primary"]["candidate_only_critical_cluster_ids"] == [development.cases[0].cluster_id]


def test_hidden_holdout_review_budget_exhaustion_is_unknown(conn) -> None:
    desk = ReviewDesk(conn, now_ms=NOW)
    for index in range(30):
        with repositories_for_connection(conn).transaction():
            desk.submit(
                None,
                ExternalMissSubmission(
                    source_url=f"https://example.test/miss/{index}",
                    title=f"Independent material missed fact number {index}",
                    occurred_at_ms=NOW - (index + 1) * 60_000,
                    rubric=_rubric(),
                ),
                principal=PRINCIPAL,
                idempotency_key=str(uuid.uuid4()),
            )
    stable = _arm()
    bootstrap = CandidateEvaluator(conn, stable=stable, model_adapter=RecordReplayModelAdapter({}))
    development = asyncio.run(
        bootstrap.freeze_dataset(
            DatasetSpec(
                role="development",
                window=ClosedWindow(from_ms=NOW - 6 * 3_600_000, to_ms=NOW),
            )
        )
    )
    assert development.counts["independent_cluster_n"] == 30
    candidate = _prompt_candidate(
        conn,
        stable=stable,
        development_sha=development.artifact_sha,
        cluster_id=development.cases[0].cluster_id,
    )
    validation_sha = _insert_validation_dataset(conn, development=development, candidate=candidate)
    _insert_stage_pass(conn, candidate_sha=candidate.candidate_sha, stage="offline")
    evaluator = CandidateEvaluator(
        conn,
        stable=stable,
        model_adapter=_StaticModelAdapter(),
        candidate_catalog=(candidate,),
    )
    request = EvaluationRequest(
        development_dataset_sha=development.artifact_sha,
        validation_dataset_sha=validation_sha,
        candidate_sha=candidate.candidate_sha,
        stage="holdout",
    )
    first = asyncio.run(evaluator.evaluate(request))
    tasks = ReviewDesk(conn, now_ms=NOW).open(DeskQuery(mode="pairwise"), principal=PRINCIPAL)["tasks"]
    assert len(tasks) == 30
    task = tasks[0]
    ref = TaskRef(task_id=task["task_id"], task_version=task["task_version"])
    for _ in range(100):
        with repositories_for_connection(conn).transaction():
            ReviewDesk(conn, now_ms=NOW).submit(
                ref,
                BlindPairwiseSubmission(preference="uncertain"),
                principal=PRINCIPAL,
                idempotency_key=str(uuid.uuid4()),
            )

    report = asyncio.run(evaluator.evaluate(request))
    assert report.run_sha == first.run_sha
    assert report.gate_outcome == "unknown"
    assert report.evidence["primary"]["review_budget_used"] == 100
    assert "validation_review_budget_exhausted" in report.evidence["blockers"]


def test_candidate_requires_a_persisted_registration_before_evaluation(conn) -> None:
    _accepted_event(conn)
    stable = _arm()
    adapter = RecordReplayModelAdapter({})
    bootstrap = CandidateEvaluator(conn, stable=stable, model_adapter=adapter)
    development = asyncio.run(
        bootstrap.freeze_dataset(
            DatasetSpec(
                role="development",
                window=ClosedWindow(from_ms=NOW - 6 * 3_600_000, to_ms=NOW),
            )
        )
    )
    prompt_text = TRIAGE_SYSTEM_PROMPT + "\nReject unsupported priced-in claims."
    arm_payload = stable.model_dump(mode="json")
    arm_payload.update(
        prompt_version="unregistered-candidate",
        prompt_text=prompt_text,
        prompt_sha256=hashlib.sha256(prompt_text.encode()).hexdigest(),
    )
    candidate_arm = ArmManifest.model_validate(arm_payload)
    receipt = ProposalReceipt.issue(
        development_dataset_sha=development.artifact_sha,
        failure_cluster_ids=(development.cases[0].cluster_id,),
        generator_kind="human",
        registered_at_ms=NOW,
        candidate_patch_sha=_sha({"prompt_sha": candidate_arm.prompt_sha256}),
        declared_target_dimensions=("why_support",),
    )
    candidate = CandidateManifest(
        target="prompt",
        parent_stable_sha=stable.bundle_sha,
        candidate_arm=candidate_arm,
        hypothesis="Registration must exist before any candidate execution.",
        target_dimensions=("why_support",),
        development_dataset_sha=development.artifact_sha,
        proposal_receipt=receipt,
    )
    evaluator = CandidateEvaluator(
        conn,
        stable=stable,
        model_adapter=adapter,
        candidate_catalog=(candidate,),
    )
    with pytest.raises(ValueError, match="news_learning_candidate_registration_missing"):
        asyncio.run(
            evaluator.evaluate(
                EvaluationRequest(
                    development_dataset_sha=development.artifact_sha,
                    candidate_sha=candidate.candidate_sha,
                    stage="offline",
                )
            )
        )
    assert adapter.calls == 0


def test_validation_window_must_begin_after_candidate_registration(conn) -> None:
    _accepted_event(conn)
    stable = _arm()
    adapter = RecordReplayModelAdapter({})
    bootstrap = CandidateEvaluator(conn, stable=stable, model_adapter=adapter)
    development = asyncio.run(
        bootstrap.freeze_dataset(
            DatasetSpec(
                role="development",
                window=ClosedWindow(from_ms=NOW - 6 * 3_600_000, to_ms=NOW),
            )
        )
    )
    candidate = _prompt_candidate(
        conn,
        stable=stable,
        development_sha=development.artifact_sha,
        cluster_id=development.cases[0].cluster_id,
    )
    evaluator = CandidateEvaluator(
        conn,
        stable=stable,
        model_adapter=adapter,
        candidate_catalog=(candidate,),
    )
    with pytest.raises(ValueError, match="news_learning_holdout_precedes_candidate_registration"):
        asyncio.run(
            evaluator.freeze_dataset(
                DatasetSpec(
                    role="validation",
                    observation_ref=candidate.candidate_sha,
                    window=ClosedWindow(from_ms=NOW - 6 * 3_600_000, to_ms=NOW),
                )
            )
        )
    assert adapter.calls == 0


def test_holdout_cannot_spend_model_budget_before_offline_pass(conn) -> None:
    _accepted_event(conn)
    stable = _arm()
    adapter = _StaticModelAdapter()
    bootstrap = CandidateEvaluator(conn, stable=stable, model_adapter=RecordReplayModelAdapter({}))
    development = asyncio.run(
        bootstrap.freeze_dataset(
            DatasetSpec(
                role="development",
                window=ClosedWindow(from_ms=NOW - 6 * 3_600_000, to_ms=NOW),
            )
        )
    )
    candidate = _prompt_candidate(
        conn,
        stable=stable,
        development_sha=development.artifact_sha,
        cluster_id=development.cases[0].cluster_id,
    )
    validation_sha = _insert_validation_dataset(conn, development=development, candidate=candidate)
    evaluator = CandidateEvaluator(
        conn,
        stable=stable,
        model_adapter=adapter,
        candidate_catalog=(candidate,),
    )
    with pytest.raises(ValueError, match="news_learning_prior_offline_evidence_not_passed"):
        asyncio.run(
            evaluator.evaluate(
                EvaluationRequest(
                    development_dataset_sha=development.artifact_sha,
                    validation_dataset_sha=validation_sha,
                    candidate_sha=candidate.candidate_sha,
                    stage="holdout",
                )
            )
        )
    assert adapter.calls == []


def test_shadow_collects_real_distribution_without_touching_online_truth(conn) -> None:
    _accepted_event(conn)
    stable = _arm()
    bootstrap = CandidateEvaluator(conn, stable=stable, model_adapter=RecordReplayModelAdapter({}))
    development = asyncio.run(
        bootstrap.freeze_dataset(
            DatasetSpec(
                role="development",
                window=ClosedWindow(from_ms=NOW - 6 * 3_600_000, to_ms=NOW),
            )
        )
    )
    candidate = _prompt_candidate(
        conn,
        stable=stable,
        development_sha=development.artifact_sha,
        cluster_id=development.cases[0].cluster_id,
    )
    validation_sha = _insert_validation_dataset(conn, development=development, candidate=candidate)
    _insert_stage_pass(conn, candidate_sha=candidate.candidate_sha, stage="holdout")
    before = conn.execute(
        "SELECT (SELECT count(*) FROM news_verdicts) AS verdicts, (SELECT count(*) FROM news_deliveries) AS deliveries"
    ).fetchone()
    adapter = _StaticModelAdapter()
    evaluator = CandidateEvaluator(
        conn,
        stable=stable,
        model_adapter=adapter,
        candidate_catalog=(candidate,),
    )
    report = asyncio.run(
        evaluator.evaluate(
            EvaluationRequest(
                development_dataset_sha=development.artifact_sha,
                validation_dataset_sha=validation_sha,
                candidate_sha=candidate.candidate_sha,
                stage="shadow",
            )
        )
    )
    after = conn.execute(
        "SELECT (SELECT count(*) FROM news_verdicts) AS verdicts, (SELECT count(*) FROM news_deliveries) AS deliveries"
    ).fetchone()

    assert after == before
    assert len(adapter.calls) == 1
    assert {call.arm for call in adapter.calls} == {"candidate"}
    assert report.gate_outcome == "unknown"  # fixture is only six hours, not the required 24
    assert report.evidence["observation_n"] == 1
    assert report.evidence["evidence_dimensions"]["observation_scope"] == "all_live_triage_eligible"
    assert report.evidence["observation_manifest_sha"]
    stored = conn.execute(
        "SELECT evaluation_stage, stable_observation, candidate_observation "
        "FROM news_learning_cases WHERE run_sha = %s",
        (report.run_sha,),
    ).fetchone()
    assert stored["evaluation_stage"] == "shadow"
    assert stored["stable_observation"]["delivery"] == "observed_sent"
    assert stored["candidate_observation"]["delivery"] == "simulated"
    manifest = conn.execute(
        "SELECT payload FROM news_learning_artifacts WHERE artifact_sha = %s",
        (report.evidence["observation_manifest_sha"],),
    ).fetchone()["payload"]
    assert manifest["case_n"] == 1
    assert "observations" not in manifest


def test_canary_evaluation_reads_one_arm_assignments_and_receipts(conn) -> None:
    event_id = _accepted_event(conn)
    stable = _arm()
    bootstrap = CandidateEvaluator(conn, stable=stable, model_adapter=RecordReplayModelAdapter({}))
    development = asyncio.run(
        bootstrap.freeze_dataset(
            DatasetSpec(
                role="development",
                window=ClosedWindow(from_ms=NOW - 6 * 3_600_000, to_ms=NOW),
            )
        )
    )
    candidate = _prompt_candidate(
        conn,
        stable=stable,
        development_sha=development.artifact_sha,
        cluster_id=development.cases[0].cluster_id,
    )
    validation_sha = _insert_validation_dataset(conn, development=development, candidate=candidate)
    _insert_stage_pass(conn, candidate_sha=candidate.candidate_sha, stage="shadow")
    repos = repositories_for_connection(conn)
    with repos.transaction():
        repos.news.arm_canary(
            activation_id="a" * 32,
            baseline_bundle_sha=stable.bundle_sha,
            candidate_manifest_sha=candidate.candidate_sha,
            candidate_bundle_sha=candidate.candidate_arm.bundle_sha,
            selector_version="news_canary_selector_v1",
            exposure_bps=10_000,
            eligibility_profile_sha="b" * 64,
            rolling_profile_sha="c" * 64,
            now_ms=NOW - 6 * 3_600_000,
        )
        assignment = repos.news.assign_agent_arm(
            event_id=event_id,
            stable_bundle_sha=stable.bundle_sha,
            admission="candidate",
            priority="normal",
            ingest_mode="live",
            now_ms=NOW - 3_500_000,
        )
    assert assignment["arm"] == "candidate"
    conn.execute(
        "UPDATE news_verdicts SET trace = trace || %s::jsonb WHERE event_id = %s AND stage = 'triage'",
        (json.dumps({"agent_assignment": assignment}), event_id),
    )

    evaluator = CandidateEvaluator(
        conn,
        stable=stable,
        model_adapter=RecordReplayModelAdapter({}),
        candidate_catalog=(candidate,),
    )
    report = asyncio.run(
        evaluator.evaluate(
            EvaluationRequest(
                development_dataset_sha=development.artifact_sha,
                validation_dataset_sha=validation_sha,
                candidate_sha=candidate.candidate_sha,
                stage="canary",
            )
        )
    )

    assert report.gate_outcome == "unknown"
    assert "canary_duration_insufficient" in report.evidence["blockers"]
    assert "canary_candidate_assignment_n_insufficient" in report.evidence["blockers"]
    assert report.evidence["candidate_runtime_observation_n"] == 1
    assert report.evidence["evidence_dimensions"]["candidate_assignment_n"] == 1
    case = conn.execute(
        "SELECT evaluation_stage, stable_observation, candidate_observation "
        "FROM news_learning_cases WHERE run_sha = %s",
        (report.run_sha,),
    ).fetchone()
    assert case["evaluation_stage"] == "canary"
    assert case["stable_observation"]["not_assigned"] is True
    assert case["candidate_observation"]["delivery"] == "observed_sent"
