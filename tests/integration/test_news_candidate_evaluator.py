from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import pytest

import tracefold.news.learning.evaluator as candidate_evaluator_module
from tests.integration.test_news_review_desk import PRINCIPAL, _rubric
from tests.news.test_news_program_compiler_sandbox import _valid_sandbox_launch_receipt
from tests.postgres_test_utils import connect_postgres_test
from tests.postgres_test_utils import reset_postgres_schema as migrate
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.learning.canary import (
    CANARY_ELIGIBILITY_PROFILE_SHA,
    CANARY_ROLLING_PROFILE_SHA,
    CANARY_SELECTOR_VERSION,
)
from tracefold.news.learning.compiler.security import (
    CompileBudgetV3,
    CompilerBuildAttestation,
    CompileRecordV1,
    CompilerProxyCall,
    CompilerProxyExecution,
    CompilerProxyTariff,
    CompileSpend,
    GepaRunResult,
    ModelExecutionIdentity,
)
from tracefold.news.learning.evaluator import (
    LEARNING_EPOCH,
    ArmManifest,
    CandidateManifest,
    ClosedWindow,
    DatasetSpec,
    EvaluationRequest,
    ProposalReceipt,
)
from tracefold.news.learning.evaluator import (
    CandidateEvaluator as _CandidateEvaluator,
)
from tracefold.news.learning.objective import (
    DevelopmentEpisode,
    GepaObjectivePlan,
    build_gepa_objective_plan,
)
from tracefold.news.learning.replay import ReplayArmSpec, load_recording_replay_capability
from tracefold.news.learning.review import (
    REVIEW_RUBRIC_VERSION,
    BlindPairwiseSubmission,
    DeskQuery,
    ExternalMissSubmission,
    TaskRef,
)
from tracefold.news.learning.review import (
    ReviewDesk as _ReviewDesk,
)
from tracefold.news.models import TriageVerdict
from tracefold.news.opennews import parse_opennews_message
from tracefold.news.pipeline.admission import admit_item
from tracefold.news.program.artifact import (
    apply_program_patch,
    load_stable_program_artifact,
)
from tracefold.news.program.contracts import (
    EditorialEnvelope,
    ProgramCallTrace,
    ProgramTrace,
    ProgramUsage,
    ScoredJudgment,
    SemanticJudgeError,
    SemanticJudgment,
    TradeRelevanceV1,
    TriageContext,
)
from tracefold.news.program.dspy_adapter import ScriptedPredictorAdapter
from tracefold.news.program.graph import (
    DspyCompileProgram,
    DspyNewsSemanticProgram,
    extract_optimizer_patch,
)
from tracefold.news.program.runtime import PROGRAM_FACTORY_ID, PROGRAM_VERSION
from tracefold.news.reader_history import assemble_reader_history
from tracefold.news.triage_rules import DEFAULT_POLICY

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_dsn")]

NOW = 1_800_000_000_000


class CandidateEvaluator(_CandidateEvaluator):
    """Pin the DB clock beyond this immutable epoch fixture's closed window."""

    def _db_now_ms(self) -> int:
        return NOW + 20 * 60_000


class ReviewDesk(_ReviewDesk):
    """Place accepted fixture evidence inside the simulated future window."""

    def _db_now_ms(self) -> int:
        return NOW


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


def _epoch_started_at_ms(conn: object) -> int:
    row = conn.execute(  # type: ignore[attr-defined]
        "SELECT starts_at_ms FROM news_learning_epochs WHERE epoch_id = %s",
        (LEARNING_EPOCH,),
    ).fetchone()
    return int(row["starts_at_ms"])


def test_candidate_evaluator_pins_the_program_v7_epoch_contract(conn) -> None:
    """The evaluator proves the persisted epoch identity — against what the epoch was *opened* with.

    `program_factory_id` and `artifact_schema_version` record what opened the epoch, exactly like
    `baseline_program_sha256` — `news_learning_epochs` is append-only by trigger, so the row can only ever
    be history. #193 replaced `factory_v5` with `factory_v6` without re-opening the epoch, because a
    serialization and identity change does not change which evidence is eligible. Asserting today's
    runtime values against those columns would therefore fail a correctly migrated database; asserting the
    historical ones still catches migration drift and a corrupted ledger row, which is what the check is
    for.
    """

    row = conn.execute(
        "SELECT starts_at_ms, program_factory_id, artifact_schema_version, baseline_program_version, "
        "prior_evidence_disposition, reset_reason FROM news_learning_epochs WHERE epoch_id = %s",
        (LEARNING_EPOCH,),
    ).fetchone()

    assert LEARNING_EPOCH == "program_v7"
    assert candidate_evaluator_module.LEARNING_EPOCH_RESET_REASON == "program_learning_package_split_identity_migration"
    # The three columns the evaluator validates, and nothing else about identity.
    assert row["baseline_program_version"] == PROGRAM_VERSION
    assert row["prior_evidence_disposition"] == "audit_only"
    assert row["reset_reason"] == candidate_evaluator_module.LEARNING_EPOCH_RESET_REASON
    # The two that name what #162 opened the epoch with, and are validated against exactly those.
    assert (
        (row["program_factory_id"], row["artifact_schema_version"])
        == (
            candidate_evaluator_module.LEARNING_EPOCH_OPENED_FACTORY_ID,
            candidate_evaluator_module.LEARNING_EPOCH_OPENED_ARTIFACT_SCHEMA_VERSION,
        )
        == ("tracefold.news.program.factory_v5", "news_semantic_program_artifact_v2")
    )
    assert (
        candidate_evaluator_module.LEARNING_PROGRAM_FACTORY_ID
        == PROGRAM_FACTORY_ID
        == ("tracefold.news.program.factory_v6")
    )
    evaluator = CandidateEvaluator(conn, stable=_arm(), judges={})
    assert evaluator._learning_epoch_started_at_ms() > 0

    # A drifted or corrupted row must not be treated as an eligible epoch. The table is append-only by
    # trigger, so the corruption is staged by making the evaluator read a different epoch id.
    conn.execute(
        "INSERT INTO news_learning_epochs (epoch_id, starts_at_ms, source_issue, program_factory_id, "
        "artifact_schema_version, baseline_program_version, baseline_program_sha256, "
        "prior_evidence_disposition, reset_reason, created_at_ms) "
        "VALUES ('program_v7_corrupted', %s, %s, 'tracefold.news.program.factory_v4', %s, %s, %s, "
        "'audit_only', %s, %s)",
        (
            row["starts_at_ms"],
            "https://github.com/AnalyThothAI/tracefold/issues/193",
            candidate_evaluator_module.LEARNING_EPOCH_OPENED_ARTIFACT_SCHEMA_VERSION,
            PROGRAM_VERSION,
            "a" * 64,
            candidate_evaluator_module.LEARNING_EPOCH_RESET_REASON,
            row["starts_at_ms"],
        ),
    )
    with (
        patch.object(candidate_evaluator_module, "LEARNING_EPOCH", "program_v7_corrupted"),
        pytest.raises(ValueError, match="news_learning_epoch_contract_mismatch"),
    ):
        CandidateEvaluator(conn, stable=_arm(), judges={})._learning_epoch_started_at_ms()


def _arm(
    *,
    policy: dict[str, object] | None = None,
    program_version: str = "news_semantic_program_v5",
    program_sha256: str | None = None,
    runtime_model_bindings_sha256: str | None = None,
) -> ArmManifest:
    selected_policy = policy or DEFAULT_POLICY.as_dict()
    return ArmManifest(
        program_version=program_version,
        program_sha256=program_sha256 or _sha({"program": program_version, "fixture": "stable"}),
        runtime_model_bindings_sha256=runtime_model_bindings_sha256
        or _sha({"model_bindings": "fixture-primary+fallback"}),
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


def _relevance() -> TradeRelevanceV1:
    return TradeRelevanceV1(
        impact_breadth="sector",
        tradability="direct",
        surprise="material_vs_expectation",
        development_delta="state_change",
        channels=("commodity_demand",),
        affected_markets=("us_equity_broad",),
        reader_value="realtime",
    )


def _editorial(*, origin: str = "model") -> EditorialEnvelope:
    if origin == "model":
        return EditorialEnvelope.issue(editorial_origin="model", relevance=_relevance())
    return EditorialEnvelope.issue(editorial_origin="degraded_unavailable", relevance=None)


def _observed_judgment_fields(verdict: dict[str, object], *, origin: str = "model") -> dict[str, object]:
    editorial = _editorial(origin=origin)
    scored = ScoredJudgment.issue(
        verdict=TriageVerdict.model_validate(verdict),
        editorial=editorial,
    )
    return {
        "verdict": verdict,
        "editorial": editorial.model_dump(mode="json"),
        "scored_judgment_sha256": scored.scored_judgment_sha256,
    }


def _trace(arm: ArmManifest, context: TriageContext, verdict: dict[str, object]) -> ProgramTrace:
    context_sha = _sha(context.model_dump(mode="json"))
    semantics = {
        key: value
        for key, value in verdict.items()
        if key not in {"actionable", "decision", "headline_zh", "title_zh", "why_zh"}
    }
    semantics["relevance"] = _relevance().model_dump(mode="json")
    card = {key: verdict[key] for key in ("headline_zh", "why_zh")}
    editorial = _editorial()
    runtime_model_sha = _sha({"provider": "fixture-provider", "model": "fixture-model"})
    runtime_binding_sha = _sha(
        {
            "provider": "fixture-provider",
            "model": "fixture-model",
            "model_sha256": runtime_model_sha,
        }
    )
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
                    "runtime_binding_sha256": runtime_binding_sha,
                }
            ),
            input_sha256=_sha({"context_sha256": context_sha, "predictor": predictor}),
            model_binding="news_triage_primary",
            physical_provider_call=True,
            runtime_provider="fixture-provider",
            runtime_model="fixture-model",
            runtime_model_sha256=runtime_model_sha,
            runtime_binding_sha256=runtime_binding_sha,
            upstream_sha256=None if predictor == "event_semantics" else _sha(semantics),
            output_sha256=_sha(output),
            validated_output=output,
            provider="fixture-provider",
            model="fixture-model",
            model_sha256=runtime_model_sha,
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
        factory_id=PROGRAM_FACTORY_ID,
        event_semantics_sha256=_sha(semantics),
        reader_card_sha256=_sha(card),
        verdict_sha256=_sha(verdict),
        editorial_sha256=editorial.editorial_sha256,
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
            editorial=_editorial(),
            program_version=self.arm.program_version,
            program_sha256=self.arm.program_sha256,
            trace=trace,
            usage=ProgramUsage(
                wall_latency_ms=900,
                call_count=2,
                physical_call_count=2,
                input_tokens=500,
                output_tokens=90,
                cached_tokens=40,
                total_tokens=590,
                provider_cost_microusd=200,
            ),
            answering_model="fixture-model",
        )


class _NondeterministicJudge(_StaticJudge):
    async def judge(self, context: TriageContext) -> SemanticJudgment:
        self.calls.append(context)
        verdict = _verdict()
        if len(self.calls) > 1:
            verdict["headline_zh"] = "同一请求返回了不同标题"
        return SemanticJudgment(
            verdict=verdict,
            editorial=_editorial(),
            program_version=self.arm.program_version,
            program_sha256=self.arm.program_sha256,
            trace=_trace(self.arm, context, verdict),
            usage=ProgramUsage(
                wall_latency_ms=900,
                call_count=2,
                physical_call_count=2,
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
        raise SemanticJudgeError(
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


class _SyntheticFallbackJudge(_StaticJudge):
    async def judge(self, context: TriageContext) -> SemanticJudgment:
        judgment = await super().judge(context)
        first, second = judgment.trace.calls
        synthetic = first.model_copy(
            update={
                "physical_provider_call": False,
                "runtime_provider": None,
                "runtime_model": None,
                "runtime_model_sha256": None,
                "runtime_binding_sha256": None,
                "output_sha256": None,
                "validated_output": None,
                "provider": None,
                "model": None,
                "model_sha256": None,
                "latency_ms": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cached_tokens": 0,
                "total_tokens": 0,
                "provider_cost_microusd": None,
                "finish_reason": None,
                "error_code": "news_program_model_binding_unresolved",
            }
        )
        calls = (
            synthetic,
            first.model_copy(update={"route": "fallback"}),
            second.model_copy(update={"route": "fallback"}),
        )
        return judgment.model_copy(
            update={
                "trace": judgment.trace.model_copy(
                    update={
                        "answering_route": "fallback",
                        "fallback_from": "news_program_model_binding_unresolved",
                        "calls": calls,
                    }
                ),
                "usage": judgment.usage.model_copy(update={"call_count": 3}),
                "fallback_from": "news_program_model_binding_unresolved",
            }
        )


def _static_judges(
    stable: ArmManifest,
    candidate: ArmManifest | None = None,
    *,
    unstable_candidate: bool = False,
) -> dict[tuple[str, str], _StaticJudge]:
    result = {("stable", stable.bundle_sha): _StaticJudge(stable)}
    if candidate is not None:
        result[("candidate", candidate.bundle_sha)] = _StaticJudge(
            candidate,
            candidate=True,
            unstable=unstable_candidate,
        )
    return result


def _judge_call_count(judges: Mapping[object, object]) -> int:
    return sum(len(judge.calls) for judge in {id(value): value for value in judges.values()}.values())


def _compiled_candidate_artifact(conn, *, development, stable: ArmManifest):
    """A real compiled child artifact, and the one record that says how it was produced."""

    base = load_stable_program_artifact()
    cold = DspyCompileProgram(base)
    cold.event_semantics.signature = cold.event_semantics.signature.with_instructions(
        "A sealed replay integration candidate instruction"
    )
    patch = extract_optimizer_patch(cold, base)
    artifact = apply_program_patch(base, patch)
    record = _compile_record(
        conn,
        development_sha=development.artifact_sha,
        stable=stable,
        program_sha256=artifact.program_sha256,
        patch_payload=patch.model_dump(mode="json"),
    )
    return artifact, record


def _objective_plan(conn, *, stable: ArmManifest, development_sha: str) -> GepaObjectivePlan:
    """The plan the release gate will rebuild, asked of the same frozen dataset the fixture froze."""

    exported = CandidateEvaluator(conn, stable=stable, judges={}).development_compile_export(development_sha)
    return build_gepa_objective_plan(tuple(DevelopmentEpisode.model_validate(e) for e in exported.episodes))


def _compile_record(
    conn,
    *,
    development_sha: str,
    stable: ArmManifest,
    program_sha256: str,
    patch_payload: dict[str, object] | None = None,
    metric_calls: int = 12,
    **overrides: object,
) -> CompileRecordV1:
    """One whole trusted compile, embedded.

    This one document replaced seven content-addressed receipts, a chain root, a runner receipt, an
    optimizer provenance record and a machine diff.  Between them those restated the same four
    identities — parent Program, dataset, runtime manifest, patch — up to four times, cross-bound by
    hashes each party computed from a payload it already held.  Here every operand is a field of one
    object, addressed once by `compile_record_sha256`, which is also the learning ledger's key for it.
    """

    epoch = conn.execute(
        "SELECT starts_at_ms FROM news_learning_epochs WHERE epoch_id = %s",
        (LEARNING_EPOCH,),
    ).fetchone()
    assert epoch is not None
    exported = CandidateEvaluator(conn, stable=stable, judges={}).development_compile_export(development_sha)
    episode_count = len(exported.episodes)
    # The real split of the real optimizer corpus, not a plausible pair of numbers. `run_gepa` derives
    # both from the Objective Plan, and `_validate_candidate_static` rebuilds that plan from the same
    # frozen dataset — so a hand-picked split here would be a fixture asserting its own fiction.
    plan = _objective_plan(conn, stable=stable, development_sha=development_sha)
    # A corpus the plan refuses cannot produce a real split, and several tests build exactly that on
    # purpose to prove the release gate rejects the candidate rather than trusting its receipt. Those get
    # a declared split the plan will not confirm, which is the shape the gate is supposed to catch.
    split = plan.split or {"schema": "tracefold.news.compile_split_receipt.v1", "unverifiable_fixture": True}
    train_count = len(plan.train_episodes) or max(1, episode_count - 1)
    val_count = len(plan.development_selection_episodes) or max(1, episode_count - train_count)
    minibatch = min(2, train_count)
    parent_program_sha256 = str(overrides.pop("parent_program_sha256", stable.program_sha256))
    # The optimizer's whole write-set: a parent identity and the two advisory instructions. Four keys
    # exactly — the record rejects any other key set — and no self-declared digest.
    patch = patch_payload or {
        "schema_version": "news_program_strategy_patch_v1",
        "parent_program_sha256": parent_program_sha256,
        "event_semantics_instruction": "A bounded fixture optimizer strategy.",
        "reader_card_instruction": "Keep reader language direct and evidence-bound.",
    }
    tariff = CompilerProxyTariff(
        tariff_id="candidate-evaluator-fixture-v1",
        input_token_overhead=64,
        task_input_microusd_per_million=1_000_000,
        task_output_microusd_per_million=1_000_000,
        reflection_input_microusd_per_million=1_000_000,
        reflection_output_microusd_per_million=1_000_000,
        metric_judge_input_microusd_per_million=1_000_000,
        metric_judge_output_microusd_per_million=1_000_000,
    )
    models = {
        "task": ModelExecutionIdentity.issue(
            role="task",
            model="fixture/task",
            api_base="https://task.fixture.invalid/v1",
            max_output_tokens=1_200,
            timeout_seconds=20,
            temperature=0,
            model_kwargs={},
        ),
        "reflection": ModelExecutionIdentity.issue(
            role="reflection",
            model="fixture/reflection",
            api_base="https://reflection.fixture.invalid/v1",
            max_output_tokens=32_000,
            timeout_seconds=300,
            temperature=1,
            model_kwargs={},
        ),
        "metric_judge": ModelExecutionIdentity.issue(
            role="metric_judge",
            model="fixture/metric-judge",
            api_base="https://metric-judge.fixture.invalid/v1",
            max_output_tokens=4_096,
            timeout_seconds=120,
            temperature=0,
            model_kwargs={},
        ),
    }
    calls: list[CompilerProxyCall] = []
    for role, count, cost_microusd in (("task", 20, 1_000), ("reflection", 10, 500), ("metric_judge", 2, 250)):
        for sequence in range(1, count + 1):
            request_bytes = 256
            max_output_tokens = models[role].max_output_tokens
            calls.append(
                CompilerProxyCall(
                    role=role,
                    sequence=sequence,
                    request_sha256=_sha({"role": role, "sequence": sequence, "kind": "request"}),
                    response_sha256=_sha({"role": role, "sequence": sequence, "kind": "response"}),
                    responding_model=models[role].model,
                    provider_invoked=True,
                    request_bytes=request_bytes,
                    max_output_tokens=max_output_tokens,
                    # Every physical call is reserved at the trusted worst-case rate before it is made.
                    reserved_cost_microusd=tariff.worst_case_cost_microusd(
                        role=role,
                        request_bytes=request_bytes,
                        max_output_tokens=max_output_tokens,
                    ),
                    input_tokens=10,
                    output_tokens=5,
                    cached_tokens=0,
                    total_tokens=15,
                    provider_cost_microusd=cost_microusd,
                    finish_reason="stop",
                    error_code=None,
                )
            )
    _policy, launch = _valid_sandbox_launch_receipt()
    usage = CompilerProxyExecution(
        # The ledger has to belong to the grant the container was actually launched under, which is the
        # one the sandbox receipt's egress manifest names.
        grant_sha256=str(launch.egress_manifest["proxy_grant_sha256"]),
        task_model_calls=20,
        reflection_model_calls=10,
        metric_judge_model_calls=2,
        task_cost_microusd=20_000,
        reflection_cost_microusd=5_000,
        metric_judge_cost_microusd=500,
        task_failures=0,
        reflection_failures=0,
        metric_judge_failures=0,
        actual_cost_microusd=sum(call.provider_cost_microusd for call in calls),
        reserved_cost_microusd=sum(call.reserved_cost_microusd for call in calls),
        calls=tuple(calls),
        error_codes=(),
    )
    budget = CompileBudgetV3(
        max_metric_calls=20,
        max_task_model_calls=40,
        max_reflection_model_calls=40,
        max_metric_judge_model_calls=40,
        max_cost_microusd=1_000_000,
        max_call_cost_microusd=40_000,
        seed=129,
    )
    preflight = launch.image_preflight
    values: dict[str, object] = {
        "parent_program_sha256": parent_program_sha256,
        "program_sha256": program_sha256,
        "development_dataset_sha256": development_sha,
        "learning_epoch_started_at_ms": int(epoch["starts_at_ms"]),
        "review_rubric_version": REVIEW_RUBRIC_VERSION,
        "episode_count": episode_count,
        # The corpus by content: `development_compile_export` re-projects episodes from live reviews, so
        # a count alone would not notice a review edited between compile and evaluate.
        "episode_projection_root_sha256": _sha(list(exported.episodes)),
        "target_runtime_manifest_sha256": stable.runtime_model_bindings_sha256,
        "task_model": models["task"],
        "reflection_model": models["reflection"],
        "metric_judge_model": models["metric_judge"],
        # One optimization, carried whole. The record embeds the object the runner produced; it used to
        # restate ten of its fields and copy each one across the container boundary by hand.
        "run": GepaRunResult.model_validate(
            {
                "patch": patch,
                "metric": {"metric_id": "accepted_review_feedback_v1"},
                "optimizer_config": {
                    "optimizer": "dspy.GEPA@3.3.0/gepa@0.1.1",
                    "seed": budget.seed,
                    "constructor_scalar_arguments": {
                        "max_metric_calls": budget.max_metric_calls,
                        "reflection_minibatch_size": minibatch,
                    },
                    "compile_call": {
                        # The optimizer corpus, not the frozen corpus: since #199 GEPA is handed
                        # `target + control` only, so an example count taken from the dataset would
                        # describe a run that never happened.
                        "example_count": train_count + val_count,
                        "trainset_count": train_count,
                        "valset_count": val_count,
                    },
                },
                "trajectory": {"schema": "tracefold.news.compile_trajectory_receipt.v1", "best_idx": 0},
                "checkpoint": {
                    "schema": "tracefold.news.compile_checkpoint_receipt.v2",
                    "factory": PROGRAM_FACTORY_ID,
                },
                # Computed in the container and now carried out: the winner was selected on clusters it
                # never trained on, and the model saw the evidence it was scored against.
                "split": split,
                "retrieval": {"schema": "tracefold.news.compile_retrieval_receipt.v1", "target_visible": True},
                "failure_cluster_ids": list(plan.target_failure_cluster_ids) or ["cluster-fixture"],
                "target_dimensions": list(plan.target_dimensions) or ["why_support"],
                "metric_calls": metric_calls,
                "train_count": train_count,
                "val_count": val_count,
            }
        ),
        "budget": budget,
        "tariff": tariff,
        "usage": usage,
        "spend": CompileSpend(
            task_model_calls=usage.task_model_calls,
            reflection_model_calls=usage.reflection_model_calls,
            metric_judge_attempts=2,
            metric_judge_model_calls=usage.metric_judge_model_calls,
            metric_judge_failures=0,
            task_cost_microusd=usage.task_cost_microusd,
            reflection_cost_microusd=usage.reflection_cost_microusd,
            metric_judge_cost_microusd=usage.metric_judge_cost_microusd,
            actual_cost_microusd=(
                usage.task_cost_microusd + usage.reflection_cost_microusd + usage.metric_judge_cost_microusd
            ),
        ),
        "sandbox": launch,
        # The one place two independent parties look at the same thing: the host's tree, the pinned
        # image's copy of it, and what the runner recomputed from inside the container.
        "compiler_build": CompilerBuildAttestation(
            compiler_image_digest=launch.compiler_image_digest,
            proxy_image_digest=launch.proxy_image_digest,
            host_source_sha256=preflight["compiler_source_sha256"],
            host_proxy_source_sha256=preflight["proxy_source_sha256"],
            host_lock_sha256=preflight["compiler_lock_sha256"],
            image_source_sha256=preflight["compiler_source_sha256"],
            image_proxy_source_sha256=preflight["proxy_source_sha256"],
            image_lock_sha256=preflight["compiler_lock_sha256"],
            container_source_sha256=preflight["compiler_source_sha256"],
            container_proxy_source_sha256=preflight["proxy_source_sha256"],
        ),
        "created_at_ms": NOW,
    }
    values.update(overrides)
    return CompileRecordV1.issue(**values)


def _program_candidate(
    conn,
    *,
    stable: ArmManifest,
    development_sha: str,
    cluster_id: str,
    persist_receipt: bool = True,
    record: CompileRecordV1 | None = None,
    record_payload: dict[str, object] | None = None,
    record_parent_sha: str | None = None,
    persist_record: bool = True,
    generator_kind: str = "model",
    program_version: str | None = None,
    program_sha256: str | None = None,
    failure_cluster_ids: tuple[str, ...] | None = None,
    target_dimensions: tuple[str, ...] | None = None,
) -> CandidateManifest:
    arm_payload = stable.model_dump(mode="json")
    arm_payload.update(
        program_version=program_version or PROGRAM_VERSION,
        program_sha256=program_sha256 or _sha({"program": "candidate", "cluster_id": cluster_id}),
    )
    candidate_arm = ArmManifest.model_validate(arm_payload)
    compile_record = record or _compile_record(
        conn,
        development_sha=development_sha,
        stable=stable,
        program_sha256=candidate_arm.program_sha256,
    )
    payload = compile_record.model_dump(mode="json") if record_payload is None else record_payload
    # What the receipt names is the record's own root; where the ledger stores it is a different address.
    record_root = compile_record.compile_record_sha256
    if persist_record:
        _persist_compile_record(
            conn,
            development_sha=record_parent_sha or development_sha,
            payload=payload,
            # The address the receipt names. For an honest record this is the payload's own root; the
            # tamper cases deliberately store something else there, which is what the evaluator has to
            # catch rather than trusting the key.
            artifact_sha=record_root,
        )
    plan = _objective_plan(conn, stable=stable, development_sha=development_sha)
    receipt_values = {
        "development_dataset_sha": development_sha,
        # The plan's verified Prompt targets, not one cluster the caller happened to name. #199 made this
        # an equality: a candidate that declares anything else was optimized against a different corpus.
        "failure_cluster_ids": failure_cluster_ids or plan.target_failure_cluster_ids or (cluster_id,),
        "generator_kind": generator_kind,
        # For a Program candidate the compile record is the generator execution identity: the receipt
        # used to carry a prompt digest and a model digest that re-hashed the same compile twice more.
        "generator_execution_sha": record_root,
        "registered_at_ms": NOW,
        "declared_target_dimensions": target_dimensions or plan.target_dimensions or ("why_support",),
        "guardrails": ("must_push_recall", "reader_load"),
        "program_parent_sha256": stable.program_sha256,
        "program_candidate_sha256": candidate_arm.program_sha256,
        "compile_record_sha256": record_root,
    }
    proposal_receipt = _proposal(conn, **receipt_values) if persist_receipt else ProposalReceipt.issue(**receipt_values)
    return CandidateManifest(
        target="program",
        parent_stable_sha=stable.bundle_sha,
        candidate_arm=candidate_arm,
        hypothesis="Remove unsupported priced-in dismissals without changing reader load.",
        target_dimensions=target_dimensions or plan.target_dimensions or ("why_support",),
        development_dataset_sha=development_sha,
        proposal_receipt=proposal_receipt,
    )


def _persist_compile_record(
    conn,
    *,
    development_sha: str,
    payload: dict[str, object],
    artifact_sha: str | None = None,
) -> str:
    """Stage one record row directly, so a test can plant an address the document does not answer to.

    `append_proposal_artifact` stores this kind under the record's own root, because the document already
    carries an identity and a second address would force every reader to know both. This fixture writes
    the row by hand precisely so the tamper cases can put something *else* in `artifact_sha` — which is
    also why it proves nothing about the real writer, and why
    `test_the_ledger_stores_a_compile_record_under_the_identity_its_receipt_names` exists beside it.
    """

    address = artifact_sha or str(payload["compile_record_sha256"])
    conn.execute(
        "INSERT INTO news_learning_artifacts "
        "(artifact_sha, kind, parent_sha, payload, created_by, created_at_ms) "
        "VALUES (%s, 'compile_record', %s, %s::jsonb, 'test', %s) "
        "ON CONFLICT (artifact_sha) DO NOTHING",
        (address, development_sha, json.dumps(payload, sort_keys=True), NOW),
    )
    return address


def _insert_validation_dataset(conn, *, development, candidate: CandidateManifest) -> str:
    payload = {
        "dataset_version": "news_learning_dataset_v1",
        "role": "validation",
        "profile_id": "news_learning_release_v1",
        "learning_epoch": LEARNING_EPOCH,
        "learning_epoch_started_at_ms": development.learning_epoch_started_at_ms,
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
    program_version: str | None = None,
    program_sha256: str | None = None,
    stale_reask: bool = False,
    # The honest split orders fact clusters by Event time, so a corpus meant to land on a particular side
    # of it has to be able to say when each Event happened. Every fixture Event used to share one instant,
    # which left the split ordered by the hash of the focus fact.
    published_at_ms: int | None = None,
    relevance: TradeRelevanceV1 | None = None,
) -> str:
    stable = _arm()
    effective_bundle = bundle_sha or stable.bundle_sha
    effective_program_version = program_version or stable.program_version
    effective_program_sha = program_sha256 or stable.program_sha256
    repos = repositories_for_connection(conn)
    published_at_ms = NOW - 3_600_000 if published_at_ms is None else published_at_ms
    effective_relevance = relevance or _relevance()
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
    event = parse_opennews_message({"method": "strategy.triggered", "params": wire})
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
        semantics = {
            key: value
            for key, value in verdict.items()
            if key not in {"actionable", "decision", "headline_zh", "title_zh", "why_zh"}
        }
        semantics["relevance"] = effective_relevance.model_dump(mode="json")
        card = {key: verdict[key] for key in ("headline_zh", "why_zh")}
        editorial = EditorialEnvelope.issue(editorial_origin="model", relevance=effective_relevance)
        scored = ScoredJudgment.issue(
            verdict=TriageVerdict.model_validate(verdict),
            editorial=editorial,
        )
        runtime_manifest_row = conn.execute(
            "SELECT manifest_sha FROM news_agent_runtime_manifests "
            "WHERE stable_bundle_sha = %s ORDER BY registered_at_ms DESC, manifest_sha DESC LIMIT 1",
            (effective_bundle,),
        ).fetchone()
        if runtime_manifest_row is None:
            runtime_manifest_sha = _sha({"stable": stable.bundle_sha, "candidate": effective_bundle, "runtime": "test"})
            repos.news.register_agent_runtime_manifest(
                manifest_sha=runtime_manifest_sha,
                stable_bundle_sha=stable.bundle_sha,
                candidate_shas=(effective_bundle,),
                image_digest="sha256:test-image",
                runtime_revision="test-revision-with-candidate",
                now_ms=published_at_ms,
            )
            runtime_manifest_row = {"manifest_sha": runtime_manifest_sha}
        assert runtime_manifest_row is not None
        runtime_model_sha = _sha({"provider": "fixture-provider", "model": "fixture-model"})
        runtime_binding_sha = _sha(
            {
                "provider": "fixture-provider",
                "model": "fixture-model",
                "model_sha256": runtime_model_sha,
            }
        )

        def program_call(predictor: str, marker: str) -> dict[str, object]:
            output = semantics if predictor == "event_semantics" else card
            return {
                "predictor": predictor,
                "route": "primary",
                "attempt": 1,
                "request_sha256": _sha(
                    {
                        "event_id": opened.event_id,
                        "predictor": predictor,
                        "marker": marker,
                        "runtime_binding_sha256": runtime_binding_sha,
                    }
                ),
                "input_sha256": _sha({"event_id": opened.event_id, "predictor": predictor, "marker": marker}),
                "model_binding": "news_triage_primary",
                "physical_provider_call": True,
                "runtime_provider": "fixture-provider",
                "runtime_model": "fixture-model",
                "runtime_model_sha256": runtime_model_sha,
                "runtime_binding_sha256": runtime_binding_sha,
                "upstream_sha256": None if predictor == "event_semantics" else _sha(semantics),
                "output_sha256": _sha(output),
                "validated_output": output,
                "provider": "fixture-provider",
                "model": "fixture-model",
                "model_sha256": runtime_model_sha,
                "latency_ms": 450,
                "input_tokens": 250,
                "output_tokens": 45,
                "cached_tokens": 20,
                "total_tokens": 295,
                "provider_cost_microusd": 100,
                "finish_reason": "stop",
            }

        def program_trace(context: dict[str, object], marker: str) -> dict[str, object]:
            calls = [program_call(predictor, marker) for predictor in ("event_semantics", "reader_card")]
            return {
                "program_version": effective_program_version,
                "program_sha256": effective_program_sha,
                "context_sha256": _sha(context),
                "factory_id": PROGRAM_FACTORY_ID,
                "event_semantics_sha256": _sha(semantics),
                "reader_card_sha256": _sha(card),
                "verdict_sha256": _sha(verdict),
                "editorial_sha256": editorial.editorial_sha256,
                "answering_route": "primary",
                "calls": calls,
            }

        initial_context: dict[str, object] = {
            "event_id": opened.event_id,
            "phase": "initial",
            "evidence_sha256": str(evidence["evidence_sha256"]),
        }
        selected_context = (
            {
                "event_id": opened.event_id,
                "phase": "stale_reask",
                "evidence_sha256": str(evidence["evidence_sha256"]),
            }
            if stale_reask
            else initial_context
        )
        execution_usage = {
            "wall_latency_ms": 900,
            "call_count": 2,
            "physical_call_count": 2,
            "input_tokens": 500,
            "output_tokens": 90,
            "cached_tokens": 40,
            "total_tokens": 590,
            "provider_cost_microusd": 200,
        }
        initial_trace = program_trace(initial_context, "initial")
        selected_trace = program_trace(selected_context, "reask" if stale_reask else "initial")
        executions = [
            {
                "execution_index": 0,
                "phase": "initial",
                "status": "superseded_stale_ledger" if stale_reask else "accepted",
                "context_sha256": initial_trace["context_sha256"],
                "context": initial_context,
                "trace": initial_trace,
                "usage": execution_usage,
                "recording_call_indices": [0, 1],
            }
        ]
        if stale_reask:
            executions.append(
                {
                    "execution_index": 1,
                    "phase": "stale_reask",
                    "status": "accepted",
                    "context_sha256": selected_trace["context_sha256"],
                    "context": selected_context,
                    "trace": selected_trace,
                    "usage": execution_usage,
                    "recording_call_indices": [2, 3],
                }
            )
        else:
            selected_trace = initial_trace
        model_attempts = 4 if stale_reask else 2
        assert repos.news.insert_verdict(
            event_id=opened.event_id,
            stage="triage",
            policy_version=candidate_evaluator_module.TRIAGE_POLICY_VERSION,
            model_decision="push",
            rule_baseline_decision="push",
            final_decision="push",
            override_rule="trade_relevance_realtime",
            throttled_by=None,
            verdict=verdict,
            editorial=editorial.model_dump(mode="json"),
            scored_judgment_sha256=scored.scored_judgment_sha256,
            runtime_manifest_sha=str(runtime_manifest_row["manifest_sha"]),
            model="fixture-model",
            program_version=effective_program_version,
            program_sha256=effective_program_sha,
            degraded=False,
            error_code=None,
            trace={
                "program_version": effective_program_version,
                "program_sha256": effective_program_sha,
                "agent_assignment": {"arm": "stable", "bundle_sha": effective_bundle},
                "program_execution_index": 1 if stale_reask else 0,
                "program_trace": selected_trace,
                "program_executions": executions,
                "latency_ms": 450 * model_attempts,
                "model_attempts": model_attempts,
                "physical_model_attempts": model_attempts,
                "input_tokens": 250 * model_attempts,
                "output_tokens": 45 * model_attempts,
                "cached_tokens": 20 * model_attempts,
                "total_tokens": 295 * model_attempts,
                "provider_cost_microusd": 100 * model_attempts,
            },
            evidence_version=int(evidence["evidence_version"]),
            evidence_sha256=str(evidence["evidence_sha256"]),
            focus_fact_id=str(evidence["focus_fact_id"]),
            now_ms=published_at_ms + 100_000,
        )
        if delivered:
            assert (
                repos.news.begin_delivery(
                    event_id=opened.event_id,
                    kind="first",
                    card={"header": {"title": {"content": str(verdict["headline_zh"])}}},
                    now_ms=published_at_ms + 200_000,
                )
                == "new"
            )
            assert repos.news.settle_delivery(
                event_id=opened.event_id,
                kind="first",
                state="sent",
                receipt={"ok": True},
                error_code=None,
                now_ms=published_at_ms + 300_000,
            )
    return opened.event_id


def _accepted_event(
    conn,
    *,
    why: str = "fail",
    stale_reask: bool = False,
    stable: ArmManifest | None = None,
    hit_id: int = 112001,
    title: str = "Micron says DRAM contract prices rose again in August",
    should_push: str = "must_push",
    first_bad_owner: str | None = "triage_prompt",
    magnitude: str | None = None,
    published_at_ms: int | None = None,
    relevance: TradeRelevanceV1 | None = None,
) -> str:
    selected_stable = stable or _arm()
    event_id = _open_event(
        conn,
        bundle_sha=selected_stable.bundle_sha,
        program_version=selected_stable.program_version,
        program_sha256=selected_stable.program_sha256,
        stale_reask=stale_reask,
        hit_id=hit_id,
        title=title,
        published_at_ms=published_at_ms,
        relevance=relevance,
    )
    conn.execute(
        "UPDATE news_verdicts SET trace = trace || %s::jsonb WHERE event_id = %s AND stage = 'triage'",
        (json.dumps({"agent_assignment": {"arm": "stable", "bundle_sha": selected_stable.bundle_sha}}), event_id),
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
            _rubric(
                why=why,
                should_push=should_push,
                magnitude=magnitude,
                # A failing case only becomes a GEPA target when a human wrote the owner into the
                # submission; a passing one has nothing to attribute.
                first_bad_owner=first_bad_owner if (why != "pass" or magnitude == "fail") else None,
            ),
            principal=PRINCIPAL,
            idempotency_key=str(uuid.uuid4()),
        )
    return event_id


# Six independent facts, in the order the honest split will see them. #199 turned "a compile needs two
# clusters" into something stricter: GEPA is handed `target + control` only, both halves of the split need
# a verified Prompt target, and both still need every required stratum. A corpus of failures alone — which
# is what this fixture used to be — now produces no split at all, which is the correct answer and not one
# a release test can build a candidate on.
#
# `held` carries `reader_value=background`, so the frozen policy resolves it to `drop`: a `must_hold` case
# the stable Program already gets right is what makes `negative_action` a control rather than a failure.
_COMPILABLE_CORPUS: tuple[tuple[str, int, str, str, bool], ...] = (
    # role, hit id, title, should_push, held
    ("target", 112001, "Micron says DRAM contract prices rose again in August", "must_push", False),
    (
        "control",
        912001,
        "European Central Bank unexpectedly cuts its deposit rate by 50 basis points",
        "must_push",
        False,
    ),
    ("control", 912002, "Brazil suspends soybean export licences for two crushing plants", "must_hold", True),
    (
        "control",
        912003,
        "Norway's sovereign wealth fund raises its allocation to listed real estate",
        "must_push",
        False,
    ),
    ("target", 912004, "Taiwan regulator approves an accelerated chip fabrication permit process", "must_push", False),
    (
        "control",
        912005,
        "Chile publishes a revised lithium royalty schedule for existing concessions",
        "must_hold",
        True,
    ),
)


def _accepted_compilable_event(
    conn,
    *,
    why: str = "fail",
    stale_reask: bool = False,
    stable: ArmManifest | None = None,
) -> str:
    """Create the focal Event plus the accepted corpus a real compile could have been run against."""

    selected_stable = stable or _arm()
    event_ids: list[str] = []
    for index, (role, hit_id, title, should_push, held) in enumerate(_COMPILABLE_CORPUS):
        # A typed `magnitude` failure with a stated correct value, not a copy complaint: #199 keeps a
        # failed `why_support` as an excluded diagnostic because the metric has no value to score the
        # repair against, so a corpus of those produces no targets at all.
        is_target = role == "target" and why != "pass"
        event_ids.append(
            _accepted_event(
                conn,
                why="pass",
                magnitude="fail" if is_target else None,
                stale_reask=stale_reask and index == 0,
                stable=selected_stable,
                hit_id=hit_id,
                title=title,
                should_push=should_push,
                published_at_ms=NOW - 3_600_000 + index * 60_000,
                relevance=_relevance().model_copy(update={"reader_value": "background"}) if held else None,
            )
        )
    assert len(set(event_ids)) == len(_COMPILABLE_CORPUS)
    return event_ids[0]


def test_freeze_dataset_keeps_only_the_exact_stable_runtime_bundle(conn) -> None:
    stable = _arm()
    prior_runtime = _arm(runtime_model_bindings_sha256=_sha({"model_bindings": "retired-four-slot-runtime"}))
    repos = repositories_for_connection(conn)
    with repos.transaction():
        repos.news.register_agent_runtime_manifest(
            manifest_sha=_sha({"stable": prior_runtime.bundle_sha, "runtime": "retired-four-slot-runtime"}),
            stable_bundle_sha=prior_runtime.bundle_sha,
            candidate_shas=(),
            image_digest="sha256:retired-test-image",
            runtime_revision="retired-four-slot-runtime",
            now_ms=NOW - 4 * 3_600_000,
        )
    prior_event_id = _accepted_event(
        conn,
        stable=prior_runtime,
        hit_id=112011,
        title="SK Hynix expands advanced packaging capacity in South Korea",
    )
    with repos.transaction():
        repos.news.register_agent_runtime_manifest(
            manifest_sha=_sha({"stable": stable.bundle_sha, "runtime": "current-four-slot-runtime"}),
            stable_bundle_sha=stable.bundle_sha,
            candidate_shas=(),
            image_digest="sha256:current-test-image",
            runtime_revision="current-four-slot-runtime",
            now_ms=NOW - 3 * 3_600_000,
        )
    exact_event_id = _accepted_event(
        conn,
        stable=stable,
        hit_id=112010,
        title="Micron raises its current-quarter DRAM contract price outlook",
    )

    development = asyncio.run(
        CandidateEvaluator(conn, stable=stable, judges={}).freeze_dataset(
            DatasetSpec(
                role="development",
                window=ClosedWindow(from_ms=NOW - 6 * 3_600_000, to_ms=NOW),
            )
        )
    )

    assert [case.event_id for case in development.cases] == [exact_event_id]
    assert prior_event_id not in {case.event_id for case in development.cases}
    assert development.counts["eligible_event_n"] == 1
    assert development.counts["eligibility"]["bundle_sha"] == stable.bundle_sha


def test_seed_receipts_match_production_latest_delivered_verdict_with_multiple_routes(conn) -> None:
    stable = _arm()
    event_id = _open_event(
        conn,
        bundle_sha=stable.bundle_sha,
        program_version=stable.program_version,
        program_sha256=stable.program_sha256,
    )
    repos = repositories_for_connection(conn)
    source = dict(
        conn.execute(
            "SELECT * FROM news_verdicts WHERE event_id = %s AND stage = 'triage' ORDER BY created_at_ms DESC",
            (event_id,),
        ).fetchone()
    )

    def add_route(*, suffix: str, final: str, event_type: str, direction: str, at_ms: int) -> None:
        verdict = dict(source["verdict"])
        verdict.update(
            {
                "event_type": event_type,
                "direction": direction,
                "assets": [{"symbol": "BABA", "role": "primary"}],
            }
        )
        assert repos.news.insert_verdict(
            event_id=event_id,
            stage="triage",
            policy_version=f"news_triage_policy_v10_{suffix}",
            model_decision=final,
            rule_baseline_decision=final,
            final_decision=final,
            override_rule=source["override_rule"],
            throttled_by=source["throttled_by"],
            verdict=verdict,
            editorial=dict(source["editorial"]),
            scored_judgment_sha256=str(source["scored_judgment_sha256"]),
            runtime_manifest_sha=str(source["runtime_manifest_sha"]),
            model=str(source["model"]),
            program_version=str(source["program_version"]),
            program_sha256=str(source["program_sha256"]),
            degraded=bool(source["degraded"]),
            error_code=source["error_code"],
            trace=dict(source["trace"]),
            evidence_version=int(source["evidence_version"]),
            evidence_sha256=str(source["evidence_sha256"]),
            focus_fact_id=str(source["focus_fact_id"]),
            now_ms=at_ms,
        )

    with repos.transaction():
        add_route(
            suffix="latest_delivered",
            final="push",
            event_type="regulation",
            direction="bearish",
            at_ms=NOW - 3_200_000,
        )
        add_route(
            suffix="later_drop",
            final="drop",
            event_type="hack",
            direction="bullish",
            at_ms=NOW - 3_100_000,
        )

    production = repos.news.reader_history(
        event_id="candidate-event",
        now_ms=NOW,
        include_targeted=False,
    )
    seed_rows = CandidateEvaluator(conn, stable=stable, judges={})._seed_receipts(
        NOW,
        epoch_started_at_ms=_epoch_started_at_ms(conn),
    )
    evaluation = assemble_reader_history(recent_rows=seed_rows, now_ms=NOW)

    assert len(seed_rows) == 1
    assert evaluation.recent_seen_rows == production.recent_seen_rows
    assert evaluation.recent_seen_rows[0].event_type == "regulation"
    assert evaluation.recent_seen_rows[0].direction == "bearish"


def test_policy_candidate_freezes_accepted_evidence_and_uses_zero_model_calls(conn) -> None:
    event_id = _accepted_event(conn)
    stable = _arm()
    judges: dict[tuple[str, str], object] = {}
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
    epoch_started_at_ms = _epoch_started_at_ms(conn)
    with pytest.raises(ValueError, match="news_learning_window_precedes_program_epoch"):
        asyncio.run(
            evaluator.freeze_dataset(
                DatasetSpec(
                    role="development",
                    window=ClosedWindow(
                        from_ms=epoch_started_at_ms - 1,
                        to_ms=epoch_started_at_ms + 1,
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
        (old_sha, json.dumps(old_payload, sort_keys=True), epoch_started_at_ms - 1),
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
    assert episodes[0]["production_judgment"]["verdict"]["headline_zh"] == "DRAM 合约价续涨"
    assert episodes[0]["production_judgment"]["editorial"]["relevance"] == _relevance().model_dump(mode="json")
    assert conn.execute("SELECT count(*) AS n FROM news_model_recordings").fetchone()["n"] == 0


def test_development_compile_export_seals_exact_dataset_and_ordered_episodes(conn) -> None:
    _accepted_event(conn, why="pass")
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

    exported = evaluator.development_compile_export(development.artifact_sha)

    assert exported.dataset_sha == development.artifact_sha
    assert _sha({"kind": "dataset", "payload": exported.dataset_payload}) == exported.dataset_sha
    assert exported.dataset_payload["agent_cohort"] == development.agent_cohort
    assert len(exported.episodes) == 1
    episode = exported.episodes[0]
    assert episode["case_id"] == development.cases[0].case_id
    assert episode["cluster_id"] == development.cases[0].cluster_id
    assert episode["accepted_review"]["review_id"] == development.cases[0].review_id
    assert episode["production_judgment"]["verdict"] == _verdict()
    assert episode["production_judgment"]["editorial"] == _editorial().model_dump(mode="json")
    assert conn.execute("SELECT count(*) AS n FROM news_model_recordings").fetchone()["n"] == 0


def test_development_compile_export_rejects_a_forged_dataset_artifact_sha(conn) -> None:
    _accepted_event(conn, why="pass")
    evaluator = CandidateEvaluator(conn, stable=_arm(), judges={})
    development = asyncio.run(
        evaluator.freeze_dataset(
            DatasetSpec(
                role="development",
                window=ClosedWindow(from_ms=NOW - 6 * 3_600_000, to_ms=NOW),
            )
        )
    )
    payload = conn.execute(
        "SELECT payload FROM news_learning_artifacts WHERE artifact_sha = %s",
        (development.artifact_sha,),
    ).fetchone()["payload"]
    forged_sha = "f" * 64
    conn.execute(
        "INSERT INTO news_learning_artifacts "
        "(artifact_sha, kind, parent_sha, payload, created_by, created_at_ms) "
        "VALUES (%s, 'dataset', NULL, %s::jsonb, 'forged', %s)",
        (forged_sha, json.dumps(payload, sort_keys=True), NOW),
    )

    with pytest.raises(ValueError, match="news_learning_dataset_artifact_hash_mismatch"):
        evaluator.development_compile_export(forged_sha)


def test_the_ledger_stores_a_compile_record_under_the_identity_its_receipt_names(conn) -> None:
    """The real writer against the real reader, with no fixture standing between them.

    Every other test here stages the row by hand so it can plant a wrong address, which is exactly how the
    writer and the reader came to disagree without a single test noticing: `append_proposal_artifact`
    addressed the row as `sha({kind, payload})` while `_compile_record` looked it up by
    `receipt.compile_record_sha256`. Every record written through `learning propose` was reported missing,
    and no compiled Program candidate could be evaluated. This drives both sides.
    """

    # Two, because the record's own budget check requires a non-empty validation split: a corpus of one
    # cannot be split, and a record over it is refused before it is ever written.
    _accepted_event(conn, why="pass")
    _accepted_event(conn, why="pass", hit_id=112042, title="A second reviewed Event, so the split is real")
    stable = _arm()
    evaluator = CandidateEvaluator(conn, stable=stable, judges={})
    development = asyncio.run(
        evaluator.freeze_dataset(
            DatasetSpec(role="development", window=ClosedWindow(from_ms=NOW - 6 * 3_600_000, to_ms=NOW))
        )
    )
    record = _compile_record(
        conn,
        development_sha=development.artifact_sha,
        stable=stable,
        program_sha256=_sha({"program": "written-through-the-repository"}),
    )
    written = repositories_for_connection(conn).news.append_proposal_artifact(
        kind="compile_record",
        payload=record.model_dump(mode="json"),
        parent_sha=development.artifact_sha,
        created_at_ms=NOW,
    )

    assert written == record.compile_record_sha256

    candidate = CandidateManifest(
        target="program",
        parent_stable_sha=stable.bundle_sha,
        candidate_arm=ArmManifest.model_validate(
            {**stable.model_dump(mode="json"), "program_sha256": record.program_sha256}
        ),
        hypothesis="Name the comparison base the reader needs.",
        target_dimensions=("why_support",),
        development_dataset_sha=development.artifact_sha,
        proposal_receipt=ProposalReceipt.issue(
            development_dataset_sha=development.artifact_sha,
            failure_cluster_ids=("cluster-0",),
            generator_kind="model",
            generator_execution_sha=record.compile_record_sha256,
            registered_at_ms=NOW,
            declared_target_dimensions=("why_support",),
            guardrails=("must_push_recall", "reader_load"),
            program_parent_sha256=stable.program_sha256,
            program_candidate_sha256=record.program_sha256,
            compile_record_sha256=record.compile_record_sha256,
        ),
    )

    assert evaluator._compile_record(candidate).compile_record_sha256 == record.compile_record_sha256


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


def test_v1_active_program_cannot_freeze_or_compile_current_evidence(conn) -> None:
    legacy = _arm(program_version="news_semantic_program_v1")
    with repositories_for_connection(conn).transaction():
        repositories_for_connection(conn).news.register_agent_runtime_manifest(
            manifest_sha=_sha({"stable": legacy.bundle_sha, "runtime": "legacy-v1"}),
            stable_bundle_sha=legacy.bundle_sha,
            candidate_shas=(),
            image_digest="sha256:legacy-v1",
            runtime_revision="legacy-v1",
            now_ms=NOW,
        )
    evaluator = CandidateEvaluator(conn, stable=legacy, judges={})

    with pytest.raises(ValueError, match="news_learning_program_v1_unsupported"):
        asyncio.run(
            evaluator.freeze_dataset(
                DatasetSpec(
                    role="development",
                    window=ClosedWindow(from_ms=NOW - 6 * 3_600_000, to_ms=NOW),
                )
            )
        )
    with pytest.raises(ValueError, match="news_learning_program_v1_unsupported"):
        evaluator.development_compile_episodes("f" * 64)


def test_program_candidate_requires_a_valid_compile_record(conn) -> None:
    """A Program candidate is only as good as the one record it names.

    A compile needs a train/validation split, so the development corpus here is the two-case one; a
    single-episode corpus cannot produce a record at all.
    """

    _accepted_compilable_event(conn)
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
    invalid_record = _program_candidate(
        conn,
        stable=stable,
        development_sha=development.artifact_sha,
        cluster_id=development.cases[0].cluster_id,
        program_sha256=_sha({"program": "candidate", "case": "invalid_record"}),
        # Stored under the root the receipt names, but not a record.
        record_payload={"development_dataset_sha": development.artifact_sha},
    )
    human_program = _program_candidate(
        conn,
        stable=stable,
        development_sha=development.artifact_sha,
        cluster_id=development.cases[0].cluster_id,
        program_sha256=_sha({"program": "candidate", "case": "human_generator"}),
        generator_kind="human",
    )
    legacy_program = _program_candidate(
        conn,
        stable=stable,
        development_sha=development.artifact_sha,
        cluster_id=development.cases[0].cluster_id,
        program_sha256=_sha({"program": "candidate", "case": "legacy_program_version"}),
        program_version="news_semantic_program_v1",
    )
    judges = _static_judges(stable, invalid_record.candidate_arm)
    evaluator = CandidateEvaluator(
        conn,
        stable=stable,
        judges=judges,
        candidate_catalog=(invalid_record, human_program, legacy_program),
    )

    with pytest.raises(ValueError, match="news_learning_program_compile_record_invalid"):
        asyncio.run(
            evaluator.evaluate(
                EvaluationRequest(
                    development_dataset_sha=development.artifact_sha,
                    candidate_sha=invalid_record.candidate_sha,
                    stage="offline",
                )
            )
        )
    with pytest.raises(ValueError, match="news_learning_program_generator_must_be_model"):
        asyncio.run(
            evaluator.evaluate(
                EvaluationRequest(
                    development_dataset_sha=development.artifact_sha,
                    candidate_sha=human_program.candidate_sha,
                    stage="offline",
                )
            )
        )
    with pytest.raises(ValueError, match="news_learning_program_v1_unsupported"):
        asyncio.run(
            evaluator.evaluate(
                EvaluationRequest(
                    development_dataset_sha=development.artifact_sha,
                    candidate_sha=legacy_program.candidate_sha,
                    stage="offline",
                )
            )
        )
    assert _judge_call_count(judges) == 0


def test_gepa_completed_step_overshoot_is_bounded_by_its_sealed_optimizer_receipt(conn) -> None:
    """The metric ceiling is now arithmetic the record performs on its own fields.

    GEPA checks `max_metric_calls` between steps, so a started step may still spend one reflection
    minibatch and one full validation pass. That overshoot is admissible only when the split it is
    computed from is the split the optimizer was actually constructed with — which the record embeds.
    """

    _accepted_compilable_event(conn)
    stable = _arm()
    development = asyncio.run(
        CandidateEvaluator(conn, stable=stable, judges={}).freeze_dataset(
            DatasetSpec(
                role="development",
                window=ClosedWindow(from_ms=NOW - 6 * 3_600_000, to_ms=NOW),
            )
        )
    )
    # The split the record embeds is the Objective Plan's, so the admissible overshoot is arithmetic on
    # that split rather than on the raw case count.
    plan = _objective_plan(conn, stable=stable, development_sha=development.artifact_sha)
    train_count = len(plan.train_episodes)
    val_count = len(plan.development_selection_episodes)
    metric_call_ceiling = 20 + val_count + min(2, train_count)

    allowed = _compile_record(
        conn,
        development_sha=development.artifact_sha,
        stable=stable,
        program_sha256=_sha({"program": "candidate", "metric_calls": metric_call_ceiling}),
        metric_calls=metric_call_ceiling,
    )
    assert allowed.run.metric_calls == metric_call_ceiling

    with pytest.raises(ValueError, match="news_program_compile_record_budget_exceeded"):
        _compile_record(
            conn,
            development_sha=development.artifact_sha,
            stable=stable,
            program_sha256=_sha({"program": "candidate", "metric_calls": metric_call_ceiling + 1}),
            metric_calls=metric_call_ceiling + 1,
        )


@pytest.mark.parametrize(
    ("mode", "error_code"),
    [
        ("absent", "news_learning_program_compile_record_missing"),
        ("tampered_byte", "news_learning_program_compile_record_invalid"),
        ("re_addressed_tamper", "news_learning_program_compile_record_identity_mismatch"),
        ("wrong_dataset_parent", "news_learning_program_compile_record_parent_mismatch"),
    ],
)
def test_program_candidate_requires_the_exact_persisted_compile_record(
    conn,
    mode: str,
    error_code: str,
) -> None:
    """The ledger row the receipt names has to be that compile, byte for byte.

    `compile_record_sha256` is both the record's own root and the key it is stored under, so a byte
    changed in the stored payload either stops validating or stops resolving at the key it claims.
    """

    _accepted_compilable_event(conn)
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
    program_sha256 = _sha({"program": "candidate", "mode": mode})
    record = _compile_record(
        conn,
        development_sha=development.artifact_sha,
        stable=stable,
        program_sha256=program_sha256,
    )
    payload = record.model_dump(mode="json")
    tampered = dict(payload, run=dict(payload["run"], metric_calls=payload["run"]["metric_calls"] + 1))
    overrides: dict[str, object] = {}
    if mode == "absent":
        overrides = {"persist_record": False}
    elif mode == "tampered_byte":
        overrides = {"record_payload": tampered}
    elif mode == "re_addressed_tamper":
        # The tamperer re-derives the record's own root, so the document validates — and no longer
        # answers to the ledger key the receipt points at.
        re_addressed = dict(tampered)
        re_addressed["compile_record_sha256"] = _sha(
            {key: value for key, value in tampered.items() if key != "compile_record_sha256"}
        )
        overrides = {"record_payload": re_addressed}
    else:
        overrides = {"record_parent_sha": _sha({"dataset": "some-other-development-corpus"})}
    candidate = _program_candidate(
        conn,
        stable=stable,
        development_sha=development.artifact_sha,
        cluster_id=development.cases[0].cluster_id,
        record=record,
        program_sha256=program_sha256,
        **overrides,
    )
    judges = _static_judges(stable, candidate.candidate_arm)
    evaluator = CandidateEvaluator(
        conn,
        stable=stable,
        judges=judges,
        candidate_catalog=(candidate,),
    )

    with pytest.raises(ValueError, match=error_code):
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


@pytest.mark.parametrize(
    "mismatch",
    ["parent_program_sha256", "development_dataset_sha256", "target_runtime_manifest_sha256", "program_sha256"],
)
def test_compile_record_identity_must_match_the_candidate_that_claims_it(conn, mismatch: str) -> None:
    """The four identities the compile has to prove, each checked once against the candidate.

    It ran against this parent Program, this development corpus and this runtime target, and it
    produced this Program. `program_sha256` is the artifact rebuilt from the record's own patch, so a
    record whose Program identity disagrees is a candidate artifact that does not come from that patch.
    """

    _accepted_compilable_event(conn)
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
    program_sha256 = _sha({"program": "candidate", "mismatch": mismatch})
    record_values: dict[str, object] = {
        "development_sha": development.artifact_sha,
        "stable": stable,
        "program_sha256": program_sha256,
    }
    # One field at a time, replaced by an identity that belongs to some other compile.
    record_values[mismatch] = _sha({"identity": "belongs-to-another-compile", "field": mismatch})
    record = _compile_record(conn, **record_values)
    candidate = _program_candidate(
        conn,
        stable=stable,
        development_sha=development.artifact_sha,
        cluster_id=development.cases[0].cluster_id,
        record=record,
        program_sha256=program_sha256,
    )
    judges = _static_judges(stable, candidate.candidate_arm)
    evaluator = CandidateEvaluator(
        conn,
        stable=stable,
        judges=judges,
        candidate_catalog=(candidate,),
    )

    with pytest.raises(ValueError, match="news_learning_program_compile_record_invalid"):
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


def test_compile_record_binds_the_per_call_ledger_to_the_container_that_was_launched(conn) -> None:
    """The ledger and the sandbox have to name the same grant, and the compile has to have finished.

    The sidecar writes its execution receipt whether it served a call or refused one, so a refusal in
    that receipt is evidence the boundary held rather than a malformed document.  A *record*, though,
    only exists for a compile that completed: a task or reflection call that never reached the provider
    means the optimizer did not run to the end, and the metric judge is the one role whose refusal the
    metric already scores as zero.
    """

    _accepted_compilable_event(conn)
    stable = _arm()
    development = asyncio.run(
        CandidateEvaluator(conn, stable=stable, judges={}).freeze_dataset(
            DatasetSpec(
                role="development",
                window=ClosedWindow(from_ms=NOW - 6 * 3_600_000, to_ms=NOW),
            )
        )
    )
    program_sha256 = _sha({"program": "candidate", "case": "proxy-grant-binding"})
    record = _compile_record(
        conn,
        development_sha=development.artifact_sha,
        stable=stable,
        program_sha256=program_sha256,
    )
    _policy, launch = _valid_sandbox_launch_receipt()
    assert record.usage.grant_sha256 == launch.egress_manifest["proxy_grant_sha256"]

    foreign_ledger = record.usage.model_copy(update={"grant_sha256": _sha({"grant": "some-other-compile"})})
    with pytest.raises(ValueError, match="news_program_compile_record_proxy_grant_mismatch"):
        _compile_record(
            conn,
            development_sha=development.artifact_sha,
            stable=stable,
            program_sha256=program_sha256,
            usage=foreign_ledger,
        )

    refused = record.usage.calls[0]
    unfinished = record.usage.model_copy(
        update={
            "calls": (
                refused.model_copy(
                    update={
                        "provider_invoked": False,
                        "reserved_cost_microusd": 0,
                        "provider_cost_microusd": 0,
                        "total_tokens": 0,
                        "finish_reason": None,
                        "error_code": "news_program_compile_proxy_call_refused",
                    }
                ),
                *record.usage.calls[1:],
            ),
            "task_model_calls": record.usage.task_model_calls - 1,
            "task_cost_microusd": record.usage.task_cost_microusd - refused.provider_cost_microusd,
            "task_failures": 1,
            "actual_cost_microusd": record.usage.actual_cost_microusd - refused.provider_cost_microusd,
            "reserved_cost_microusd": record.usage.reserved_cost_microusd - refused.reserved_cost_microusd,
            "error_codes": ("news_program_compile_proxy_call_refused",),
        }
    )
    assert unfinished.task_failures == 1
    with pytest.raises(ValueError, match="news_program_compile_record_task_call_failed"):
        _compile_record(
            conn,
            development_sha=development.artifact_sha,
            stable=stable,
            program_sha256=program_sha256,
            usage=unfinished,
        )


def test_successful_critical_case_cannot_authorize_a_failure_cluster(conn) -> None:
    _accepted_compilable_event(conn, why="pass")
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


def test_a_candidate_cannot_declare_an_objective_the_corpus_does_not_support(conn) -> None:
    """#199: the release gate rebuilds the Objective Plan and holds the candidate to it, exactly.

    All three are the same defect wearing different clothes — a candidate optimized against a corpus other
    than the one it names. Before the plan there was only a subset check against an owner-blind guess, so
    a candidate could declare any cluster whose review mentioned a failure, whoever owned it.
    """

    _accepted_compilable_event(conn)
    stable = _arm()
    development = asyncio.run(
        CandidateEvaluator(conn, stable=stable, judges={}).freeze_dataset(
            DatasetSpec(role="development", window=ClosedWindow(from_ms=NOW - 6 * 3_600_000, to_ms=NOW))
        )
    )
    plan = _objective_plan(conn, stable=stable, development_sha=development.artifact_sha)
    assert plan.split is not None and len(plan.target_failure_cluster_ids) == 2

    honest = _compile_record(
        conn,
        development_sha=development.artifact_sha,
        stable=stable,
        program_sha256=_sha({"program": "candidate", "case": "objective-tamper"}),
    )
    tampered_split = _compile_record(
        conn,
        development_sha=development.artifact_sha,
        stable=stable,
        program_sha256=_sha({"program": "candidate", "case": "split-tamper"}),
        run=honest.run.model_copy(
            update={"split": {**dict(honest.run.split), "train": {"cluster_root_sha256": "0" * 64}}}
        ),
    )

    cases: list[tuple[str, dict[str, Any]]] = [
        (
            "news_learning_proposal_failure_cluster_unverified",
            {"failure_cluster_ids": (plan.control_cluster_ids[0],)},
        ),
        (
            "news_learning_proposal_target_dimensions_unverified",
            {"target_dimensions": ("direction",)},
        ),
        (
            "news_learning_proposal_split_roots_unverified",
            {"record": tampered_split},
        ),
    ]
    for error, overrides in cases:
        candidate = _program_candidate(
            conn,
            stable=stable,
            development_sha=development.artifact_sha,
            cluster_id=development.cases[0].cluster_id,
            program_sha256=(
                overrides["record"].program_sha256
                if "record" in overrides
                else _sha({"program": "candidate", "case": error})
            ),
            **overrides,
        )
        evaluator = CandidateEvaluator(
            conn,
            stable=stable,
            judges=_static_judges(stable, candidate.candidate_arm),
            candidate_catalog=(candidate,),
        )
        with pytest.raises(ValueError, match=error):
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
    _accepted_compilable_event(conn)
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
    assert len(candidate_stability) == len(development.cases) == len(_COMPILABLE_CORPUS)
    assert {item["trials"] for item in candidate_stability} == {3}
    # `pass_k` is exactly "all three trials agreed", and the corpus now produces both answers: the
    # #199 controls are cases the arms agree on, which is the first time this assertion has had a
    # `True` to check at all.
    pass_counts = [item["pass_n"] for item in candidate_stability]
    assert min(pass_counts) == 0 and max(pass_counts) == 3
    assert [item["pass_k"] for item in candidate_stability] == [count == 3 for count in pass_counts]
    assert {len(item["trial_results"]) for item in candidate_stability} == {3}
    assert _judge_call_count(judges) == 6 * len(development.cases)
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
    assert len(recordings) == 12 * len(development.cases)
    assert {row["predictor_name"] for row in recordings} == {"event_semantics", "reader_card"}
    assert {row["call_index"] for row in recordings} == {0, 1}
    assert {row["attempt"] for row in recordings} == {1}
    assert {row["route"] for row in recordings} == {"primary"}
    assert {row["provider"] for row in recordings} == {"fixture-provider"}
    assert {row["cached_tokens"] for row in recordings} == {20}
    assert {row["total_tokens"] for row in recordings} == {295}
    assert {row["provider_cost_microusd"] for row in recordings} == {100}
    assert all(row["request"]["runtime_model_bindings_sha256"] for row in recordings)
    # Every recorded request is bound to the exact Program identity of the arm that produced it.
    assert {row["request"]["program_sha256"] for row in recordings} == {
        stable.program_sha256,
        candidate.candidate_arm.program_sha256,
    }
    assert all(row["response"]["output"] for row in recordings)
    candidate_artifact = conn.execute(
        "SELECT payload FROM news_learning_artifacts WHERE kind = 'candidate' AND payload->>'candidate_sha' = %s",
        (candidate.candidate_sha,),
    ).fetchone()["payload"]
    assert candidate_artifact["exact_diff"] == {
        "target": "program",
        "changed_fields": ["program_sha256"],
        "stable_bundle_sha": stable.bundle_sha,
        "candidate_bundle_sha": candidate.candidate_arm.bundle_sha,
        "stable_program_version": stable.program_version,
        "candidate_program_version": candidate.candidate_arm.program_version,
        "stable_program_sha256": stable.program_sha256,
        "candidate_program_sha256": candidate.candidate_arm.program_sha256,
        "compile_record_sha256": candidate.proposal_receipt.compile_record_sha256,
    }


def test_strict_recording_verification_reexecutes_real_program_graph_without_new_truth(conn) -> None:
    stable_artifact = load_stable_program_artifact()
    stable = _arm(program_sha256=stable_artifact.program_sha256)
    with repositories_for_connection(conn).transaction():
        repositories_for_connection(conn).news.register_agent_runtime_manifest(
            manifest_sha=_sha({"stable": stable.bundle_sha, "runtime": "record-replay-test"}),
            stable_bundle_sha=stable.bundle_sha,
            candidate_shas=(),
            image_digest="sha256:record-replay-test",
            runtime_revision="record-replay-test",
            now_ms=NOW - 23 * 3_600_000,
        )
    _accepted_compilable_event(conn, stable=stable)
    bootstrap = CandidateEvaluator(conn, stable=stable, judges={})
    development = asyncio.run(
        bootstrap.freeze_dataset(
            DatasetSpec(
                role="development",
                window=ClosedWindow(from_ms=NOW - 6 * 3_600_000, to_ms=NOW),
            )
        )
    )
    candidate_artifact, record = _compiled_candidate_artifact(conn, development=development, stable=stable)
    candidate = _program_candidate(
        conn,
        stable=stable,
        development_sha=development.artifact_sha,
        cluster_id=development.cases[0].cluster_id,
        record=record,
        program_sha256=candidate_artifact.program_sha256,
    )
    semantics = {
        key: value
        for key, value in _verdict().items()
        if key not in {"actionable", "decision", "headline_zh", "title_zh", "why_zh"}
    }
    semantics["relevance"] = _relevance().model_dump(mode="json")
    card = {key: _verdict()[key] for key in ("headline_zh", "why_zh")}
    # Three k3 stability trials per case, two Predictors each. Sized from the corpus rather than pinned:
    # the script exhausting is how this test detects an extra provider call, so the number has to track
    # the number of cases instead of standing for it.
    scripted_calls = 3 * len(_COMPILABLE_CORPUS)
    stable_adapter = ScriptedPredictorAdapter([value for _ in range(scripted_calls) for value in (semantics, card)])
    candidate_adapter = ScriptedPredictorAdapter([value for _ in range(scripted_calls) for value in (semantics, card)])
    judges = {
        ("stable", stable.bundle_sha): DspyNewsSemanticProgram(
            stable_artifact,
            primary_adapter=stable_adapter,
        ),
        ("candidate", candidate.candidate_arm.bundle_sha): DspyNewsSemanticProgram(
            candidate_artifact,
            primary_adapter=candidate_adapter,
        ),
    }
    evaluator = CandidateEvaluator(
        conn,
        stable=stable,
        judges=judges,
        candidate_catalog=(candidate,),
    )
    request = EvaluationRequest(
        development_dataset_sha=development.artifact_sha,
        candidate_sha=candidate.candidate_sha,
        stage="offline",
    )
    first = asyncio.run(evaluator.evaluate(request))
    before = conn.execute(
        "SELECT "
        "(SELECT count(*) FROM news_learning_cases WHERE run_sha = %s) AS cases, "
        "(SELECT count(*) FROM news_model_recordings WHERE run_sha = %s) AS recordings",
        (first.run_sha, first.run_sha),
    ).fetchone()

    capability = load_recording_replay_capability(
        conn,
        run_sha=first.run_sha,
        arms=(
            ReplayArmSpec(arm="stable", bundle_sha=stable.bundle_sha, artifact=stable_artifact),
            ReplayArmSpec(
                arm="candidate",
                bundle_sha=candidate.candidate_arm.bundle_sha,
                artifact=candidate_artifact,
            ),
        ),
    )
    replay_evaluator = CandidateEvaluator(
        conn,
        stable=stable,
        judges={},
        candidate_catalog=(candidate,),
    )
    ordinary_repeat = asyncio.run(replay_evaluator.evaluate(request))
    verified = asyncio.run(replay_evaluator.evaluate(request, recording_replay=capability))
    after = conn.execute(
        "SELECT "
        "(SELECT count(*) FROM news_learning_cases WHERE run_sha = %s) AS cases, "
        "(SELECT count(*) FROM news_model_recordings WHERE run_sha = %s) AS recordings",
        (first.run_sha, first.run_sha),
    ).fetchone()

    assert ordinary_repeat.run_sha == first.run_sha == verified.run_sha
    assert after == before
    verification = verified.evidence["recording_verification"]
    assert set(verification) == {
        "mode",
        "run_sha",
        "case_n",
        "observation_root",
        "recording_n",
        "recording_corpus_root",
    }
    assert verification["mode"] == "strict_record_replay_v1"
    assert verification["run_sha"] == first.run_sha
    assert verification["case_n"] == len(development.cases) == len(_COMPILABLE_CORPUS)
    assert len(verification["observation_root"]) == 64
    assert verification["recording_n"] == before["recordings"]
    assert len(verification["recording_corpus_root"]) == 64


@pytest.mark.parametrize(
    ("missing_kind", "blocker"),
    (
        ("corpus", "news_learning_recording_replay_corpus_missing"),
        ("call", "news_learning_recording_replay_call_missing"),
    ),
)
def test_strict_recording_verification_reports_replay_misses_as_unknown_without_live_fallback(
    conn,
    missing_kind: str,
    blocker: str,
) -> None:
    stable_artifact = load_stable_program_artifact()
    stable = _arm(program_sha256=stable_artifact.program_sha256)
    with repositories_for_connection(conn).transaction():
        repositories_for_connection(conn).news.register_agent_runtime_manifest(
            manifest_sha=_sha({"stable": stable.bundle_sha, "runtime": "missing-replay-test"}),
            stable_bundle_sha=stable.bundle_sha,
            candidate_shas=(),
            image_digest="sha256:missing-replay-test",
            runtime_revision="missing-replay-test",
            now_ms=NOW - 23 * 3_600_000,
        )
    _accepted_compilable_event(conn, stable=stable)
    bootstrap = CandidateEvaluator(conn, stable=stable, judges={})
    development = asyncio.run(
        bootstrap.freeze_dataset(
            DatasetSpec(
                role="development",
                window=ClosedWindow(from_ms=NOW - 6 * 3_600_000, to_ms=NOW),
            )
        )
    )
    candidate_artifact, record = _compiled_candidate_artifact(conn, development=development, stable=stable)
    candidate = _program_candidate(
        conn,
        stable=stable,
        development_sha=development.artifact_sha,
        cluster_id=development.cases[0].cluster_id,
        record=record,
        program_sha256=candidate_artifact.program_sha256,
    )
    judges = _static_judges(stable, candidate.candidate_arm)
    if missing_kind == "corpus":
        judges = {
            ("stable", stable.bundle_sha): _StaticJudge(stable),
            ("candidate", candidate.candidate_arm.bundle_sha): _AlwaysUnavailableJudge(candidate.candidate_arm),
        }
    evaluator = CandidateEvaluator(
        conn,
        stable=stable,
        judges=judges,
        candidate_catalog=(candidate,),
    )
    request = EvaluationRequest(
        development_dataset_sha=development.artifact_sha,
        candidate_sha=candidate.candidate_sha,
        stage="offline",
    )
    first = asyncio.run(evaluator.evaluate(request))
    if missing_kind == "corpus":
        assert first.gate_outcome == "fail"
        assert "candidate_schema_or_provider_regression" in first.evidence["failures"]
    calls_before_replay = _judge_call_count(judges)

    replay_rows: list[Mapping[str, object]] = []
    if missing_kind == "call":

        def move_to_absent_case(row: Mapping[str, object]) -> dict[str, object]:
            moved = {**dict(row), "case_id": f"absent-{row['case_id']}"}
            moved["recording_sha"] = _sha(
                {
                    key: moved[key]
                    for key in (
                        "run_sha",
                        "case_id",
                        "arm",
                        "trial",
                        "predictor_name",
                        "call_index",
                        "attempt",
                        "request_sha256",
                    )
                }
            )
            return moved

        replay_rows = [
            move_to_absent_case(row)
            for row in conn.execute(
                """
                SELECT *
                  FROM news_model_recordings
                 WHERE run_sha = %s
                 ORDER BY case_id, arm, trial, call_index, attempt, recording_sha
                """,
                (first.run_sha,),
            ).fetchall()
        ]

    class _ReplayCorpus:
        def execute(self, query: str, params: tuple[str]) -> object:
            assert "WHERE run_sha = %s" in query
            assert params == (first.run_sha,)
            return self

        def fetchall(self) -> list[Mapping[str, object]]:
            return replay_rows

    capability = load_recording_replay_capability(
        _ReplayCorpus(),
        run_sha=first.run_sha,
        arms=(
            ReplayArmSpec(arm="stable", bundle_sha=stable.bundle_sha, artifact=stable_artifact),
            ReplayArmSpec(
                arm="candidate",
                bundle_sha=candidate.candidate_arm.bundle_sha,
                artifact=candidate_artifact,
            ),
        ),
    )

    report = asyncio.run(evaluator.evaluate(request, recording_replay=capability))

    assert report.gate_outcome == "unknown"
    assert report.run_state == "incomplete"
    assert report.recommended_action == "hold"
    assert report.next_stage == "none"
    assert report.evidence["execution_incomplete"] is True
    assert blocker in report.evidence["blockers"]
    if missing_kind == "corpus":
        assert "candidate_schema_or_provider_regression" in report.evidence["failures"]
    assert "recording_verification" not in report.evidence
    assert _judge_call_count(judges) == calls_before_replay


def test_strict_recording_verification_rejects_an_unsealed_judge_map(conn) -> None:
    with pytest.raises(ValueError, match="news_learning_recording_replay_capability_invalid"):
        asyncio.run(
            CandidateEvaluator(conn, stable=_arm(), judges={}).evaluate(  # type: ignore[arg-type]
                EvaluationRequest(
                    development_dataset_sha="d" * 64,
                    candidate_sha="c" * 64,
                    stage="offline",
                ),
                recording_replay={"judges": _static_judges(_arm())},
            )
        )


def test_model_recording_conflict_rejects_nondeterministic_response(conn) -> None:
    stable = _arm()
    judge = _NondeterministicJudge(stable)
    evaluator = CandidateEvaluator(conn, stable=stable, judges={("stable", stable.bundle_sha): judge})
    context = TriageContext.from_card(
        {
            "event_id": "ev-recording-conflict",
            "evidence_version": 1,
            "evidence_sha256": "a" * 64,
            "focus_fact_id": "fact-recording-conflict",
            "reporting_origin": "Reuters",
            "leader_title": "Micron says DRAM contract prices rose again",
            "leader_description": "Contract prices rose in August.",
            "opened_at_ms": NOW - 3_600_000,
            "member_count": 1,
            "family": "earnings",
            "queue_priority": "normal",
            "asset_class": "equity",
            "storyline_key": "asset:MU",
        },
        watchlist=(),
        told_rows=(),
        now_ms=NOW,
        queue_lag_ms=0,
    )
    invocation = {
        "run_sha": _sha("nondeterministic-recording-run"),
        "case_id": _sha("nondeterministic-recording-case"),
        "arm_name": "stable",
        "arm": stable,
        "context": context,
        "trial": 1,
    }

    asyncio.run(evaluator._invoke_and_record(**invocation))
    with pytest.raises(ValueError, match="news_model_recording_conflict"):
        asyncio.run(evaluator._invoke_and_record(**invocation))
    composite_identity_conflict = {**invocation, "context": context.model_copy(update={"queue_lag_ms": 1})}
    with pytest.raises(ValueError, match="news_model_recording_conflict"):
        asyncio.run(evaluator._invoke_and_record(**composite_identity_conflict))

    recordings = conn.execute(
        "SELECT predictor_name, response FROM news_model_recordings WHERE run_sha = %s ORDER BY call_index",
        (invocation["run_sha"],),
    ).fetchall()
    assert [row["predictor_name"] for row in recordings] == ["event_semantics", "reader_card"]
    assert recordings[1]["response"]["output"]["card"]["headline_zh"] == "DRAM 合约价续涨"
    assert conn.execute("SELECT count(*) AS n FROM news_learning_cases").fetchone()["n"] == 0


def test_synthetic_trace_entry_is_audited_but_not_recorded_or_charged(conn) -> None:
    stable = _arm()
    judge = _SyntheticFallbackJudge(stable)
    evaluator = CandidateEvaluator(
        conn,
        stable=stable,
        judges={("stable", stable.bundle_sha): judge},
    )
    context = TriageContext.from_card(
        {
            "event_id": "ev-synthetic-fallback",
            "evidence_version": 1,
            "evidence_sha256": "a" * 64,
            "focus_fact_id": "fact-synthetic-fallback",
            "reporting_origin": "Reuters",
            "leader_title": "Micron says DRAM contract prices rose again",
            "leader_description": "Contract prices rose in August.",
            "opened_at_ms": NOW - 3_600_000,
            "member_count": 1,
            "family": "earnings",
            "queue_priority": "normal",
            "asset_class": "equity",
            "storyline_key": "asset:MU",
        },
        watchlist=(),
        told_rows=(),
        now_ms=NOW,
        queue_lag_ms=0,
    )
    run_sha = _sha("synthetic-fallback-run")
    observation = asyncio.run(
        evaluator._invoke_and_record(
            run_sha=run_sha,
            case_id=_sha("synthetic-fallback-case"),
            arm_name="stable",
            arm=stable,
            context=context,
            trial=1,
        )
    )

    assert observation["usage"]["call_count"] == 3
    assert observation["usage"]["physical_call_count"] == 2
    assert observation["usage"]["provider_cost_microusd"] == 200
    assert [call["physical_provider_call"] for call in observation["calls"]] == [False, True, True]
    recordings = conn.execute(
        "SELECT call_index, route, provider_cost_microusd FROM news_model_recordings "
        "WHERE run_sha = %s ORDER BY call_index",
        (run_sha,),
    ).fetchall()
    assert [(row["call_index"], row["route"], row["provider_cost_microusd"]) for row in recordings] == [
        (1, "fallback", 100),
        (2, "fallback", 100),
    ]


def test_exact_one_variable_is_rejected_before_any_model_call(conn) -> None:
    # The two-case corpus is what a compile record needs: GEPA is constructed with a train/validation
    # split, and the record's budget arithmetic reads that split back out of its own optimizer config.
    _accepted_compilable_event(conn)
    stable = _arm()
    judges: dict[tuple[str, str], object] = {}
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
    _accepted_compilable_event(conn)
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
        ("stable", stable.bundle_sha): _AlwaysUnavailableJudge(stable, recording_missing=True),
        ("candidate", candidate.candidate_arm.bundle_sha): _AlwaysUnavailableJudge(
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
    _accepted_compilable_event(conn)
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
        ("stable", stable.bundle_sha): _AlwaysUnavailableJudge(stable),
        ("candidate", candidate.candidate_arm.bundle_sha): _AlwaysUnavailableJudge(candidate.candidate_arm),
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

    assert _judge_call_count(judges) == 2 * len(development.cases)
    assert report.gate_outcome == "unknown"
    assert report.run_state == "incomplete"
    assert report.evidence["common_error_n"] == len(development.cases)
    assert report.evidence["candidate_only_error_n"] == 0
    assert "stable_or_common_execution_unavailable" in report.evidence["blockers"]
    assert "candidate_schema_or_provider_regression" not in report.evidence["failures"]


def test_missing_per_call_provider_cost_blocks_program_release(conn) -> None:
    _accepted_compilable_event(conn)
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
        ("stable", stable.bundle_sha): _StaticJudge(stable),
        ("candidate", candidate.candidate_arm.bundle_sha): _MissingProviderCostJudge(
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
    _accepted_compilable_event(conn)
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
    # `pair.<run_sha>.<case_id>`: the queue chose the case, so the arm order has to be read for *that*
    # case. With a one-case corpus an unqualified `fetchone()` happened to be the right row.
    reviewed_case_id = str(task["task_id"]).rsplit(".", 1)[-1]
    row = conn.execute(
        "SELECT comparison FROM news_learning_cases WHERE run_sha = %s AND case_id = %s",
        (first.run_sha, reviewed_case_id),
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
    reviewed_cluster = next(case.cluster_id for case in development.cases if case.case_id == reviewed_case_id)
    assert report.evidence["primary"]["candidate_only_critical_cluster_ids"] == [reviewed_cluster]


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
    # #199: a corpus of external misses alone cannot authorize a Program candidate — there is no stable
    # output on any of them, so the Objective Plan has neither a target nor a control. The misses are what
    # this test is about; the compilable corpus is what makes a Program candidate legal at all.
    _accepted_compilable_event(conn)
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
    assert development.counts["independent_cluster_n"] == 30 + len(_COMPILABLE_CORPUS)
    cases_by_id = {case.case_id: case for case in development.cases}
    episodes = bootstrap.development_compile_episodes(development.artifact_sha)
    assert len(episodes) == 30 + len(_COMPILABLE_CORPUS)
    miss_ids = {case.case_id for case in development.cases if case.subject_kind == "external_miss"}
    assert len(miss_ids) == 30
    for episode in episodes:
        if episode["case_id"] not in miss_ids:
            continue
        case = cases_by_id[episode["case_id"]]
        evidence = episode["context"]["evidence"]
        assert evidence["event_id"] == case.case_id
        assert evidence["evidence_version"] == 0
        assert evidence["evidence_sha256"] == case.evidence_sha256
        assert evidence["focus_fact_id"] == case.case_id
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
    # One page of the pairwise queue — `DeskQuery.limit` defaults to 30 — not the size of the corpus.
    # The holdout plan pre-registers one representative per fact cluster, and there are 36 of them.
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
    _accepted_compilable_event(conn)
    stable = _arm()
    judges: dict[tuple[str, str], object] = {}
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
    _accepted_compilable_event(conn)
    stable = _arm()
    judges: dict[tuple[str, str], object] = {}
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
    _accepted_compilable_event(conn)
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
    event_id = _accepted_compilable_event(conn, stale_reask=True)
    stable = _arm()
    _open_event(
        conn,
        hit_id=112002,
        title="An event produced by a different deployed bundle",
        bundle_sha=_sha("other-bundle"),
    )
    _open_event(
        conn,
        hit_id=112003,
        title="An event produced by an older semantic program",
        program_version="news_semantic_program_retired",
        program_sha256=_sha("retired-program"),
    )
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
    assert (
        len(judges[("candidate", candidate.candidate_arm.bundle_sha)].calls)
        == len(development.cases)
        == len(_COMPILABLE_CORPUS)
    )
    assert report.gate_outcome == "unknown"  # fixture is only six hours, not the required 24
    assert report.evidence["observation_n"] == len(development.cases) == len(_COMPILABLE_CORPUS)
    assert report.evidence["evidence_dimensions"]["observation_scope"] == "all_live_triage_eligible"
    assert report.evidence["observation_manifest_sha"]
    stored = conn.execute(
        "SELECT event_id, evaluation_stage, stable_observation, candidate_observation "
        "FROM news_learning_cases WHERE run_sha = %s AND event_id = %s",
        (report.run_sha, event_id),
    ).fetchone()
    assert stored["event_id"] == event_id
    assert stored["evaluation_stage"] == "shadow"
    assert stored["stable_observation"]["delivery"] == "observed_sent"
    assert stored["candidate_observation"]["delivery"] == "simulated"
    observed_program = stored["stable_observation"]["program"][0]
    execution_context_shas = [execution["context_sha256"] for execution in observed_program["executions"]]
    assert execution_context_shas == [_sha(execution["context"]) for execution in observed_program["executions"]]
    assert observed_program["trace"]["context_sha256"] == execution_context_shas[1]
    assert [call["execution_index"] for call in observed_program["calls"]] == [0, 0, 1, 1]
    assert [call["execution_phase"] for call in observed_program["calls"]] == [
        "initial",
        "initial",
        "stale_reask",
        "stale_reask",
    ]
    assert [call["execution_status"] for call in observed_program["calls"]] == [
        "superseded_stale_ledger",
        "superseded_stale_ledger",
        "accepted",
        "accepted",
    ]
    assert [call["recording_call_index"] for call in observed_program["calls"]] == [0, 1, 2, 3]
    assert [call["execution_context_sha256"] for call in observed_program["calls"]] == [
        execution_context_shas[0],
        execution_context_shas[0],
        execution_context_shas[1],
        execution_context_shas[1],
    ]
    assert observed_program["usage"]["call_count"] == 4
    assert observed_program["usage"]["physical_call_count"] == 4
    assert [execution["status"] for execution in observed_program["executions"]] == [
        "superseded_stale_ledger",
        "accepted",
    ]
    recordings = conn.execute(
        "SELECT arm, predictor_name FROM news_model_recordings WHERE run_sha = %s ORDER BY call_index",
        (report.run_sha,),
    ).fetchall()
    # One candidate call per Predictor per case, the two Predictors in graph order. Only the candidate is
    # re-run: the stable arm's calls are what production already recorded.
    assert [(row["arm"], row["predictor_name"]) for row in recordings] == [
        *[("candidate", "event_semantics")] * len(_COMPILABLE_CORPUS),
        *[("candidate", "reader_card")] * len(_COMPILABLE_CORPUS),
    ]
    assert Counter(row["arm"] for row in recordings) == Counter({"candidate": 2 * len(_COMPILABLE_CORPUS)})
    manifest = conn.execute(
        "SELECT payload FROM news_learning_artifacts WHERE artifact_sha = %s",
        (report.evidence["observation_manifest_sha"],),
    ).fetchone()["payload"]
    assert manifest["case_n"] == len(development.cases) == len(_COMPILABLE_CORPUS)
    assert "observations" not in manifest


@pytest.mark.parametrize("program_matches_assignment", [True, False])
def test_canary_evaluation_reads_one_arm_assignments_and_receipts(conn, *, program_matches_assignment: bool) -> None:
    event_id = _accepted_compilable_event(conn)
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
            selector_version=CANARY_SELECTOR_VERSION,
            exposure_bps=10_000,
            eligibility_profile_sha=CANARY_ELIGIBILITY_PROFILE_SHA,
            rolling_profile_sha=CANARY_ROLLING_PROFILE_SHA,
            now_ms=NOW - 6 * 3_600_000,
        )
        assignment = repos.news.assign_agent_arm(
            event_id=event_id,
            stable_bundle_sha=stable.bundle_sha,
            admission="candidate",
            ingest_mode="live",
            now_ms=NOW - 3_500_000,
        )
    assert assignment["arm"] == "candidate"
    verdict_agent = candidate.candidate_arm if program_matches_assignment else stable
    verdict_trace = dict(
        conn.execute(
            "SELECT trace FROM news_verdicts WHERE event_id = %s AND stage = 'triage'",
            (event_id,),
        ).fetchone()["trace"]
    )
    verdict_trace.update(
        agent_assignment=assignment,
        program_version=verdict_agent.program_version,
        program_sha256=verdict_agent.program_sha256,
    )
    selected_trace = dict(verdict_trace["program_trace"])
    selected_trace.update(
        program_version=verdict_agent.program_version,
        program_sha256=verdict_agent.program_sha256,
    )
    verdict_trace["program_trace"] = selected_trace
    for execution in verdict_trace["program_executions"]:
        execution["trace"]["program_version"] = verdict_agent.program_version
        execution["trace"]["program_sha256"] = verdict_agent.program_sha256
    conn.execute(
        "UPDATE news_verdicts SET trace = %s::jsonb, program_version = %s, program_sha256 = %s "
        "WHERE event_id = %s AND stage = 'triage'",
        (
            json.dumps(verdict_trace),
            verdict_agent.program_version,
            verdict_agent.program_sha256,
            event_id,
        ),
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

    assert report.gate_outcome == ("unknown" if program_matches_assignment else "fail")
    assert "canary_duration_insufficient" in report.evidence["blockers"]
    assert "canary_candidate_assignment_n_insufficient" in report.evidence["blockers"]
    assert report.evidence["candidate_runtime_observation_n"] == 1
    assert report.evidence["evidence_dimensions"]["candidate_assignment_n"] == 1
    assert report.evidence["evidence_dimensions"]["assignment_invariant_breach_event_ids"] == (
        [] if program_matches_assignment else [event_id]
    )
    assert ("canary_one_arm_assignment_invariant_breach" in report.evidence["failures"]) is (
        not program_matches_assignment
    )
    case = conn.execute(
        "SELECT evaluation_stage, stable_observation, candidate_observation "
        "FROM news_learning_cases WHERE run_sha = %s",
        (report.run_sha,),
    ).fetchone()
    assert case["evaluation_stage"] == "canary"
    assert case["stable_observation"]["not_assigned"] is True
    assert case["candidate_observation"]["delivery"] == "observed_sent"
