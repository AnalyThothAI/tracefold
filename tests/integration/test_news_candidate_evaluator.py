from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import UTC, datetime

import pytest

from tests.integration.test_news_review_desk import PRINCIPAL, _rubric
from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tracefold.app.repositories import repositories_for_connection
from tracefold.news import (
    LEARNING_EPOCH,
    LEARNING_EPOCH_STARTED_AT_MS,
    ArmManifest,
    BlindPairwiseSubmission,
    CandidateManifest,
    ClosedWindow,
    DatasetSpec,
    DeskQuery,
    EvaluationRequest,
    ExternalMissSubmission,
    ProgramTrace,
    ProgramUsage,
    ProposalReceipt,
    ReviewDesk,
    SemanticJudgment,
    TaskRef,
    TriageContext,
)
from tracefold.news import (
    CandidateEvaluator as _CandidateEvaluator,
)
from tracefold.news.agents.semantic_program import ProgramCallTrace, SemanticProgramError
from tracefold.news.events import admit_item
from tracefold.news.opennews import parse_opennews_message
from tracefold.news.triage_rules import DEFAULT_POLICY

pytestmark = pytest.mark.integration

NOW = LEARNING_EPOCH_STARTED_AT_MS + 8 * 3_600_000


class CandidateEvaluator(_CandidateEvaluator):
    """Pin the DB clock beyond this immutable epoch fixture's closed window."""

    def _db_now_ms(self) -> int:
        return NOW + 20 * 60_000


def test_arm_manifest_identity_is_program_native() -> None:
    policy = DEFAULT_POLICY.as_dict()
    arm = ArmManifest(
        program_version="news_semantic_program_v1",
        program_sha256="a" * 64,
        runtime_model_bindings_sha256="c" * 64,
        retrieval_sha256="b" * 64,
        policy=policy,
        policy_sha256=_sha(policy),
    )

    assert arm.bundle_sha == _sha(arm.model_dump(mode="json"))
    assert set(arm.model_dump()) == {
        "program_version",
        "program_sha256",
        "runtime_model_bindings_sha256",
        "retrieval_sha256",
        "policy",
        "policy_sha256",
    }


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


def _arm(
    *,
    policy: dict[str, object] | None = None,
    program_version: str = "news_semantic_program_v1",
    program_sha256: str | None = None,
) -> ArmManifest:
    selected_policy = policy or DEFAULT_POLICY.as_dict()
    return ArmManifest(
        program_version=program_version,
        program_sha256=program_sha256 or _sha({"program": program_version, "fixture": "stable"}),
        runtime_model_bindings_sha256=_sha({"model_bindings": "fixture-primary+fallback"}),
        retrieval_sha256=_sha({"told": "v1", "evidence": "v1"}),
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


def _verdict() -> dict[str, object]:
    return {
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
    }


def _trace(arm: ArmManifest, context: TriageContext, verdict: dict[str, object]) -> ProgramTrace:
    context_sha = _sha(context.model_dump(mode="json"))
    semantics = {key: value for key, value in verdict.items() if key not in {"headline_zh", "title_zh", "why_zh"}}
    card = {key: verdict[key] for key in ("headline_zh", "title_zh", "why_zh")}
    calls = tuple(
        ProgramCallTrace(
            predictor=predictor,
            route="primary",
            attempt=1,
            request_sha256=_sha(
                {
                    "program_sha256": arm.program_sha256,
                    "context_sha256": context_sha,
                    "predictor": predictor,
                }
            ),
            input_sha256=_sha({"context_sha256": context_sha, "predictor": predictor}),
            signature_sha256=_sha({"signature": predictor}),
            instruction_sha256=_sha({"instruction": predictor, "program": arm.program_sha256}),
            demos_sha256=_sha({"demos": predictor, "program": arm.program_sha256}),
            model_binding="news_triage_primary",
            upstream_sha256=None if predictor == "event_semantics" else _sha(semantics),
            output_sha256=_sha(output),
            validated_output=output,
            provider="fixture-provider",
            model="fixture-model",
            model_sha256=_sha({"model": "fixture-model"}),
            latency_ms=450,
            input_tokens=250,
            output_tokens=45,
            cached_tokens=20,
            total_tokens=295,
            provider_cost_microusd=100,
            finish_reason="stop",
        )
        for predictor, output in (("event_semantics", semantics), ("reader_card", card))
    )
    return ProgramTrace(
        program_version=arm.program_version,
        program_sha256=arm.program_sha256,
        context_sha256=context_sha,
        factory_id="news_semantic_program_v1",
        topology_sha256=_sha("topology"),
        adapter_sha256=_sha("adapter"),
        assembler_sha256=_sha("assembler"),
        event_semantics_sha256=_sha(semantics),
        reader_card_sha256=_sha(card),
        verdict_sha256=_sha(verdict),
        answering_route="primary",
        calls=calls,
    )


class _StaticJudge:
    def __init__(self, arm: ArmManifest, *, candidate: bool = False, unstable: bool = False) -> None:
        self.arm = arm
        self.candidate = candidate
        self.unstable = unstable
        self.calls: list[TriageContext] = []

    async def judge(self, context: TriageContext) -> SemanticJudgment:
        self.calls.append(context)
        verdict = _verdict()
        if self.candidate:
            verdict["headline_zh"] = "候选：DRAM 合约价续涨"
        if self.candidate and self.unstable and len(self.calls) == 3:
            verdict.update(magnitude=0, actionable=False, decision="drop")
        trace = _trace(self.arm, context, verdict)
        return SemanticJudgment(
            verdict=verdict,
            program_version=self.arm.program_version,
            program_sha256=self.arm.program_sha256,
            trace=trace,
            usage=ProgramUsage(
                wall_latency_ms=900,
                call_count=2,
                input_tokens=500,
                output_tokens=90,
                cached_tokens=40,
                total_tokens=590,
                provider_cost_microusd=200,
            ),
            answering_model="fixture-model",
        )


class _AlwaysUnavailableJudge:
    def __init__(self, arm: ArmManifest, *, recording_missing: bool = False) -> None:
        self.arm = arm
        self.recording_missing = recording_missing
        self.calls: list[TriageContext] = []

    async def judge(self, context: TriageContext) -> SemanticJudgment:
        self.calls.append(context)
        raise SemanticProgramError(
            "news_program_recording_missing" if self.recording_missing else "provider_unavailable",
            retryable=False,
            output_failure=False,
            attempts=1,
            partial_trace=_trace(self.arm, context, _verdict()).model_copy(update={"calls": ()}),
        )


class _MissingProviderCostJudge(_StaticJudge):
    async def judge(self, context: TriageContext) -> SemanticJudgment:
        judgment = await super().judge(context)
        calls = tuple(call.model_copy(update={"provider_cost_microusd": None}) for call in judgment.trace.calls)
        return judgment.model_copy(
            update={
                "trace": judgment.trace.model_copy(update={"calls": calls}),
                "usage": judgment.usage.model_copy(update={"provider_cost_microusd": None}),
            }
        )


def _static_judges(
    stable: ArmManifest,
    candidate: ArmManifest | None = None,
    *,
    unstable_candidate: bool = False,
) -> dict[str, _StaticJudge]:
    result = {stable.program_sha256: _StaticJudge(stable)}
    if candidate is not None and candidate.program_sha256 != stable.program_sha256:
        result[candidate.program_sha256] = _StaticJudge(
            candidate,
            candidate=True,
            unstable=unstable_candidate,
        )
    return result


def _judge_call_count(judges: dict[str, object]) -> int:
    return sum(len(judge.calls) for judge in {id(value): value for value in judges.values()}.values())


def _program_candidate(
    conn,
    *,
    stable: ArmManifest,
    development_sha: str,
    cluster_id: str,
    persist_receipt: bool = True,
) -> CandidateManifest:
    arm_payload = stable.model_dump(mode="json")
    arm_payload.update(
        program_version="news_semantic_program_v1_gepa_why_support",
        program_sha256=_sha({"program": "candidate", "cluster_id": cluster_id}),
    )
    candidate_arm = ArmManifest.model_validate(arm_payload)
    registered_at_ms = NOW
    machine_diff = {
        "event_semantics": {
            "instruction_sha256": {"stable": "1" * 64, "candidate": "2" * 64},
            "demo_order_sha256": {"stable": "3" * 64, "candidate": "4" * 64},
        }
    }
    compile_provenance = {
        "development_dataset_sha": development_sha,
        "optimizer": "fixture-gepa",
        "metric_sha256": "5" * 64,
        "seed": 129,
    }
    patch_sha = _sha(
        {
            "parent_program_sha256": stable.program_sha256,
            "candidate_program_sha256": candidate_arm.program_sha256,
            "machine_diff": machine_diff,
            "compile_provenance": compile_provenance,
        }
    )
    receipt_values = {
        "development_dataset_sha": development_sha,
        "failure_cluster_ids": (cluster_id,),
        "generator_kind": "human",
        "registered_at_ms": registered_at_ms,
        "candidate_patch_sha": patch_sha,
        "declared_target_dimensions": ("why_support",),
        "guardrails": ("must_push_recall", "reader_load"),
        "program_parent_sha256": stable.program_sha256,
        "program_candidate_sha256": candidate_arm.program_sha256,
        "program_machine_diff": machine_diff,
        "compile_provenance": compile_provenance,
    }
    proposal_receipt = _proposal(conn, **receipt_values) if persist_receipt else ProposalReceipt.issue(**receipt_values)
    return CandidateManifest(
        target="program",
        parent_stable_sha=stable.bundle_sha,
        candidate_arm=candidate_arm,
        hypothesis="Remove unsupported priced-in dismissals without changing reader load.",
        target_dimensions=("why_support",),
        development_dataset_sha=development_sha,
        proposal_receipt=proposal_receipt,
    )


def _insert_validation_dataset(conn, *, development, candidate: CandidateManifest) -> str:
    payload = {
        "dataset_version": "news_learning_dataset_v1",
        "role": "validation",
        "profile_id": "news_learning_release_v1",
        "learning_epoch": LEARNING_EPOCH,
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


def _open_event(
    conn,
    *,
    delivered: bool = True,
    hit_id: int = 112001,
    title: str = "Micron says DRAM contract prices rose again in August",
    bundle_sha: str | None = None,
) -> str:
    stable = _arm()
    effective_bundle = bundle_sha or stable.bundle_sha
    repos = repositories_for_connection(conn)
    published_at_ms = NOW - 3_600_000
    wire = {
        "id": hit_id,
        "text": title,
        "link": f"https://example.test/{hit_id}",
        "source": "Reuters",
        "newsType": "news",
        "engineType": "news",
        "ts": datetime.fromtimestamp(published_at_ms / 1000, tz=UTC).isoformat(),
        "aiRating": {"score": 82, "signal": "long", "status": "done"},
        "coins": [],
        "strategy": {"id": 1018, "name": "News Score > 70", "engine_type": "news", "source_type": "news"},
    }
    event = parse_opennews_message(
        {"method": "strategy.triggered", "params": wire},
        strategy_ids=frozenset({"1018"}),
    )
    assert event is not None
    with repos.transaction():
        opened = admit_item(
            repos,
            event=event,
            ingest_mode="live",
            observed_at_ms=published_at_ms,
            trace_id="candidate-evaluator-test",
            watchlist_symbols=frozenset(),
            now_ms=published_at_ms,
        )
        evidence = repos.news.latest_evidence_snapshot(opened.event_id)
        assert evidence is not None
        verdict = _verdict()
        assert repos.news.insert_verdict(
            event_id=opened.event_id,
            stage="triage",
            policy_version="v6",
            model_decision="push",
            rule_baseline_decision="push",
            final_decision="push",
            override_rule="model_push_actionable",
            throttled_by=None,
            verdict=verdict,
            model="fixture-model",
            program_version=stable.program_version,
            program_sha256=stable.program_sha256,
            degraded=False,
            error_code=None,
            trace={
                "program_version": stable.program_version,
                "program_sha256": stable.program_sha256,
                "agent_assignment": {"arm": "stable", "bundle_sha": effective_bundle},
            },
            evidence_version=int(evidence["evidence_version"]),
            evidence_sha256=str(evidence["evidence_sha256"]),
            focus_fact_id=str(evidence["focus_fact_id"]),
            now_ms=NOW - 3_500_000,
        )
        if delivered:
            assert (
                repos.news.begin_delivery(
                    event_id=opened.event_id,
                    kind="first",
                    card={"header": {"title": {"content": str(verdict["headline_zh"])}}},
                    now_ms=NOW - 3_400_000,
                )
                == "new"
            )
            assert repos.news.settle_delivery(
                event_id=opened.event_id,
                kind="first",
                state="sent",
                receipt={"ok": True},
                error_code=None,
                now_ms=NOW - 3_300_000,
            )
    return opened.event_id


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
    judges: dict[str, object] = {}
    evaluator = CandidateEvaluator(conn, stable=stable, judges=judges)
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
        judges=judges,
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

    assert _judge_call_count(judges) == 0
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


def test_program_epoch_rejects_old_windows_and_old_artifacts_but_preserves_audit_json(conn) -> None:
    stable = _arm()
    evaluator = CandidateEvaluator(conn, stable=stable, judges={})
    with pytest.raises(ValueError, match="news_learning_window_precedes_program_epoch"):
        asyncio.run(
            evaluator.freeze_dataset(
                DatasetSpec(
                    role="development",
                    window=ClosedWindow(
                        from_ms=LEARNING_EPOCH_STARTED_AT_MS - 1,
                        to_ms=LEARNING_EPOCH_STARTED_AT_MS + 1,
                    ),
                )
            )
        )

    _accepted_event(conn)
    development = asyncio.run(
        evaluator.freeze_dataset(
            DatasetSpec(
                role="development",
                window=ClosedWindow(from_ms=NOW - 6 * 3_600_000, to_ms=NOW),
            )
        )
    )
    row = conn.execute(
        "SELECT payload FROM news_learning_artifacts WHERE artifact_sha = %s",
        (development.artifact_sha,),
    ).fetchone()
    old_payload = dict(row["payload"])
    old_payload.pop("learning_epoch")
    old_sha = _sha({"kind": "dataset", "payload": old_payload})
    conn.execute(
        "INSERT INTO news_learning_artifacts "
        "(artifact_sha, kind, parent_sha, payload, created_by, created_at_ms) "
        "VALUES (%s, 'dataset', NULL, %s::jsonb, 'legacy-audit', %s)",
        (old_sha, json.dumps(old_payload, sort_keys=True), LEARNING_EPOCH_STARTED_AT_MS - 1),
    )
    with pytest.raises(ValueError, match="news_learning_epoch_mismatch"):
        evaluator.development_compile_episodes(old_sha)
    assert (
        conn.execute(
            "SELECT payload FROM news_learning_artifacts WHERE artifact_sha = %s",
            (old_sha,),
        ).fetchone()["payload"]
        == old_payload
    )


def test_development_compile_episodes_is_current_epoch_read_only_interface(conn) -> None:
    _accepted_event(conn)
    stable = _arm()
    evaluator = CandidateEvaluator(conn, stable=stable, judges={})
    development = asyncio.run(
        evaluator.freeze_dataset(
            DatasetSpec(
                role="development",
                window=ClosedWindow(from_ms=NOW - 6 * 3_600_000, to_ms=NOW),
            )
        )
    )

    episodes = evaluator.development_compile_episodes(development.artifact_sha)

    assert len(episodes) == 1
    assert episodes[0]["case_id"] == development.cases[0].case_id
    assert episodes[0]["cluster_id"] == development.cases[0].cluster_id
    assert episodes[0]["stratum"] == development.cases[0].stratum
    assert episodes[0]["context"]["now_ms"] == development.cases[0].opened_at_ms
    assert episodes[0]["context"]["told"]["entries"] == []
    assert episodes[0]["accepted_review"]["should_push"] == "must_push"
    assert episodes[0]["accepted_review"]["dimensions"]["why_support"] == "fail"
    assert episodes[0]["production_verdict"]["headline_zh"] == "DRAM 合约价续涨"
    assert conn.execute("SELECT count(*) AS n FROM news_model_recordings").fetchone()["n"] == 0


def test_active_stable_is_checked_before_freeze_or_model_work(conn) -> None:
    stale = _arm(program_sha256=_sha({"program": "other"}))
    judges = _static_judges(stale)
    evaluator = CandidateEvaluator(conn, stable=stale, judges=judges)

    with pytest.raises(ValueError, match="news_learning_active_stable_mismatch"):
        asyncio.run(
            evaluator.freeze_dataset(
                DatasetSpec(
                    role="development",
                    window=ClosedWindow(from_ms=NOW - 6 * 3_600_000, to_ms=NOW),
                )
            )
        )
    assert _judge_call_count(judges) == 0


def test_successful_critical_case_cannot_authorize_a_failure_cluster(conn) -> None:
    _accepted_event(conn, why="pass")
    stable = _arm()
    bootstrap = CandidateEvaluator(conn, stable=stable, judges={})
    development = asyncio.run(
        bootstrap.freeze_dataset(
            DatasetSpec(
                role="development",
                window=ClosedWindow(from_ms=NOW - 6 * 3_600_000, to_ms=NOW),
            )
        )
    )
    candidate = _program_candidate(
        conn,
        stable=stable,
        development_sha=development.artifact_sha,
        cluster_id=development.cases[0].cluster_id,
    )
    evaluator = CandidateEvaluator(
        conn,
        stable=stable,
        judges=_static_judges(stable, candidate.candidate_arm),
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
    bootstrap = CandidateEvaluator(conn, stable=stable, judges={})
    development = asyncio.run(
        bootstrap.freeze_dataset(
            DatasetSpec(
                role="development",
                window=ClosedWindow(from_ms=NOW - 6 * 3_600_000, to_ms=NOW),
            )
        )
    )
    candidate = _program_candidate(
        conn,
        stable=stable,
        development_sha=development.artifact_sha,
        cluster_id=development.cases[0].cluster_id,
    )
    judges = _static_judges(stable, candidate.candidate_arm, unstable_candidate=True)
    evaluator = CandidateEvaluator(
        conn,
        stable=stable,
        judges=judges,
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
    assert _judge_call_count(judges) == 6
    assert report.evidence["stable_mean_total_tokens"] == 590
    assert report.evidence["candidate_mean_total_tokens"] == 590
    assert report.evidence["stable_mean_call_count"] == 2
    assert report.evidence["candidate_mean_call_count"] == 2
    assert report.evidence["stable_mean_provider_cost_microusd"] == 200
    assert report.evidence["candidate_mean_provider_cost_microusd"] == 200
    assert set(report.evidence["program_cost_by_predictor"]["candidate"]) == {
        "event_semantics:primary",
        "reader_card:primary",
    }
    recordings = conn.execute(
        "SELECT arm, trial, predictor_name, call_index, attempt, route, request, response, provider, "
        "cached_tokens, total_tokens, provider_cost_microusd "
        "FROM news_model_recordings WHERE run_sha = %s ORDER BY arm, trial, call_index",
        (report.run_sha,),
    ).fetchall()
    assert len(recordings) == 12
    assert {row["predictor_name"] for row in recordings} == {"event_semantics", "reader_card"}
    assert {row["call_index"] for row in recordings} == {0, 1}
    assert {row["attempt"] for row in recordings} == {1}
    assert {row["route"] for row in recordings} == {"primary"}
    assert {row["provider"] for row in recordings} == {"fixture-provider"}
    assert {row["cached_tokens"] for row in recordings} == {20}
    assert {row["total_tokens"] for row in recordings} == {295}
    assert {row["provider_cost_microusd"] for row in recordings} == {100}
    assert all(row["request"]["runtime_model_bindings_sha256"] for row in recordings)
    assert all(row["response"]["output"] for row in recordings)
    candidate_artifact = conn.execute(
        "SELECT payload FROM news_learning_artifacts WHERE kind = 'candidate' AND payload->>'candidate_sha' = %s",
        (candidate.candidate_sha,),
    ).fetchone()["payload"]
    assert candidate_artifact["exact_diff"] == {
        "target": "program",
        "changed_fields": ["program_sha256", "program_version"],
        "stable_bundle_sha": stable.bundle_sha,
        "candidate_bundle_sha": candidate.candidate_arm.bundle_sha,
        "stable_program_version": stable.program_version,
        "candidate_program_version": candidate.candidate_arm.program_version,
        "stable_program_sha256": stable.program_sha256,
        "candidate_program_sha256": candidate.candidate_arm.program_sha256,
        "machine_diff": candidate.proposal_receipt.program_machine_diff,
        "compile_provenance": candidate.proposal_receipt.compile_provenance,
    }


def test_exact_one_variable_is_rejected_before_any_model_call(conn) -> None:
    _accepted_event(conn)
    stable = _arm()
    judges: dict[str, object] = {}
    evaluator = CandidateEvaluator(conn, stable=stable, judges=judges)
    development = asyncio.run(
        evaluator.freeze_dataset(
            DatasetSpec(
                role="development",
                window=ClosedWindow(from_ms=NOW - 6 * 3_600_000, to_ms=NOW),
            )
        )
    )
    valid = _program_candidate(
        conn,
        stable=stable,
        development_sha=development.artifact_sha,
        cluster_id=development.cases[0].cluster_id,
    )
    changed_payload = valid.candidate_arm.model_dump(mode="json")
    changed_payload["runtime_model_bindings_sha256"] = _sha({"runtime": "different"})
    candidate = valid.model_copy(update={"candidate_arm": ArmManifest.model_validate(changed_payload)})
    evaluator = CandidateEvaluator(
        conn,
        stable=stable,
        judges=judges,
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
    assert _judge_call_count(judges) == 0


def test_record_replay_miss_is_explicit_unknown_without_live_fallback(conn) -> None:
    _accepted_event(conn)
    stable = _arm()
    first = CandidateEvaluator(conn, stable=stable, judges={})
    development = asyncio.run(
        first.freeze_dataset(
            DatasetSpec(
                role="development",
                window=ClosedWindow(from_ms=NOW - 6 * 3_600_000, to_ms=NOW),
            )
        )
    )
    candidate = _program_candidate(
        conn,
        stable=stable,
        development_sha=development.artifact_sha,
        cluster_id=development.cases[0].cluster_id,
    )
    judges = {
        stable.program_sha256: _AlwaysUnavailableJudge(stable, recording_missing=True),
        candidate.candidate_arm.program_sha256: _AlwaysUnavailableJudge(
            candidate.candidate_arm,
            recording_missing=True,
        ),
    }
    evaluator = CandidateEvaluator(
        conn,
        stable=stable,
        judges=judges,
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
    assert "news_program_recording_missing" in report.evidence["blockers"]
    assert _judge_call_count(judges) == 1


def test_common_provider_outage_is_unknown_not_a_vacuous_candidate_pass(conn) -> None:
    _accepted_event(conn)
    stable = _arm()
    bootstrap = CandidateEvaluator(conn, stable=stable, judges={})
    development = asyncio.run(
        bootstrap.freeze_dataset(
            DatasetSpec(
                role="development",
                window=ClosedWindow(from_ms=NOW - 6 * 3_600_000, to_ms=NOW),
            )
        )
    )
    candidate = _program_candidate(
        conn,
        stable=stable,
        development_sha=development.artifact_sha,
        cluster_id=development.cases[0].cluster_id,
    )
    judges = {
        stable.program_sha256: _AlwaysUnavailableJudge(stable),
        candidate.candidate_arm.program_sha256: _AlwaysUnavailableJudge(candidate.candidate_arm),
    }
    evaluator = CandidateEvaluator(
        conn,
        stable=stable,
        judges=judges,
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

    assert _judge_call_count(judges) == 2
    assert report.gate_outcome == "unknown"
    assert report.run_state == "incomplete"
    assert report.evidence["common_error_n"] == 1
    assert report.evidence["candidate_only_error_n"] == 0
    assert "stable_or_common_execution_unavailable" in report.evidence["blockers"]
    assert "candidate_schema_or_provider_regression" not in report.evidence["failures"]


def test_missing_per_call_provider_cost_blocks_program_release(conn) -> None:
    _accepted_event(conn)
    stable = _arm()
    bootstrap = CandidateEvaluator(conn, stable=stable, judges={})
    development = asyncio.run(
        bootstrap.freeze_dataset(
            DatasetSpec(
                role="development",
                window=ClosedWindow(from_ms=NOW - 6 * 3_600_000, to_ms=NOW),
            )
        )
    )
    candidate = _program_candidate(
        conn,
        stable=stable,
        development_sha=development.artifact_sha,
        cluster_id=development.cases[0].cluster_id,
    )
    judges = {
        stable.program_sha256: _StaticJudge(stable),
        candidate.candidate_arm.program_sha256: _MissingProviderCostJudge(
            candidate.candidate_arm,
            candidate=True,
        ),
    }
    evaluator = CandidateEvaluator(
        conn,
        stable=stable,
        judges=judges,
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
    assert report.evidence["provider_cost_observation_complete"] is False
    assert "provider_cost_observation_incomplete" in report.evidence["blockers"]


def test_blind_candidate_critical_error_is_a_release_failure(conn) -> None:
    _accepted_event(conn)
    stable = _arm()
    bootstrap = CandidateEvaluator(conn, stable=stable, judges={})
    development = asyncio.run(
        bootstrap.freeze_dataset(
            DatasetSpec(
                role="development",
                window=ClosedWindow(from_ms=NOW - 6 * 3_600_000, to_ms=NOW),
            )
        )
    )
    candidate = _program_candidate(
        conn,
        stable=stable,
        development_sha=development.artifact_sha,
        cluster_id=development.cases[0].cluster_id,
    )
    evaluator = CandidateEvaluator(
        conn,
        stable=stable,
        judges=_static_judges(stable, candidate.candidate_arm),
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
    bootstrap = CandidateEvaluator(conn, stable=stable, judges={})
    development = asyncio.run(
        bootstrap.freeze_dataset(
            DatasetSpec(
                role="development",
                window=ClosedWindow(from_ms=NOW - 6 * 3_600_000, to_ms=NOW),
            )
        )
    )
    assert development.counts["independent_cluster_n"] == 30
    candidate = _program_candidate(
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
        judges=_static_judges(stable, candidate.candidate_arm),
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
    judges: dict[str, object] = {}
    bootstrap = CandidateEvaluator(conn, stable=stable, judges=judges)
    development = asyncio.run(
        bootstrap.freeze_dataset(
            DatasetSpec(
                role="development",
                window=ClosedWindow(from_ms=NOW - 6 * 3_600_000, to_ms=NOW),
            )
        )
    )
    candidate = _program_candidate(
        conn,
        stable=stable,
        development_sha=development.artifact_sha,
        cluster_id=development.cases[0].cluster_id,
        persist_receipt=False,
    )
    evaluator = CandidateEvaluator(
        conn,
        stable=stable,
        judges=judges,
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
    assert _judge_call_count(judges) == 0


def test_validation_window_must_begin_after_candidate_registration(conn) -> None:
    _accepted_event(conn)
    stable = _arm()
    judges: dict[str, object] = {}
    bootstrap = CandidateEvaluator(conn, stable=stable, judges=judges)
    development = asyncio.run(
        bootstrap.freeze_dataset(
            DatasetSpec(
                role="development",
                window=ClosedWindow(from_ms=NOW - 6 * 3_600_000, to_ms=NOW),
            )
        )
    )
    candidate = _program_candidate(
        conn,
        stable=stable,
        development_sha=development.artifact_sha,
        cluster_id=development.cases[0].cluster_id,
    )
    evaluator = CandidateEvaluator(
        conn,
        stable=stable,
        judges=judges,
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
    assert _judge_call_count(judges) == 0


def test_holdout_cannot_spend_model_budget_before_offline_pass(conn) -> None:
    _accepted_event(conn)
    stable = _arm()
    bootstrap = CandidateEvaluator(conn, stable=stable, judges={})
    development = asyncio.run(
        bootstrap.freeze_dataset(
            DatasetSpec(
                role="development",
                window=ClosedWindow(from_ms=NOW - 6 * 3_600_000, to_ms=NOW),
            )
        )
    )
    candidate = _program_candidate(
        conn,
        stable=stable,
        development_sha=development.artifact_sha,
        cluster_id=development.cases[0].cluster_id,
    )
    validation_sha = _insert_validation_dataset(conn, development=development, candidate=candidate)
    judges = _static_judges(stable, candidate.candidate_arm)
    evaluator = CandidateEvaluator(
        conn,
        stable=stable,
        judges=judges,
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
    assert _judge_call_count(judges) == 0


def test_shadow_collects_real_distribution_without_touching_online_truth(conn) -> None:
    _accepted_event(conn)
    stable = _arm()
    bootstrap = CandidateEvaluator(conn, stable=stable, judges={})
    development = asyncio.run(
        bootstrap.freeze_dataset(
            DatasetSpec(
                role="development",
                window=ClosedWindow(from_ms=NOW - 6 * 3_600_000, to_ms=NOW),
            )
        )
    )
    candidate = _program_candidate(
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
    judges = _static_judges(stable, candidate.candidate_arm)
    evaluator = CandidateEvaluator(
        conn,
        stable=stable,
        judges=judges,
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
    assert len(judges[candidate.candidate_arm.program_sha256].calls) == 1
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
    bootstrap = CandidateEvaluator(conn, stable=stable, judges={})
    development = asyncio.run(
        bootstrap.freeze_dataset(
            DatasetSpec(
                role="development",
                window=ClosedWindow(from_ms=NOW - 6 * 3_600_000, to_ms=NOW),
            )
        )
    )
    candidate = _program_candidate(
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
        judges={},
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
