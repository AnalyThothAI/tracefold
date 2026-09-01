from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections import Counter
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import pytest

from tests.integration.test_news_review_desk import PRINCIPAL, _rubric
from tests.postgres_test_utils import connect_postgres_test
from tests.support.news_judgment import news_taxonomy
from tracefold.app.repository_session import repositories_for_connection
from tracefold.news.artifact_identity import canonical_sha
from tracefold.news.learning import dataset as dataset_module
from tracefold.news.learning.contracts import PromptCandidateV1, PromptPatchV1, epoch_id_for_bundle
from tracefold.news.learning.dataset import DevelopmentDatasetStore
from tracefold.news.learning.evaluate import (
    ArmManifest,
    CandidateManifest,
    ClosedWindow,
    DatasetSpec,
    EvaluationRequest,
    ProposalReceipt,
)
from tracefold.news.learning.evaluate import (
    CandidateEvaluator as _CandidateEvaluator,
)
from tracefold.news.learning.ledger import LearningLedger
from tracefold.news.learning.objective import (
    DevelopmentEpisode,
    GepaObjectivePlan,
    build_gepa_objective_plan,
)
from tracefold.news.learning.optimizer import objective_summary as objective_plan_summary
from tracefold.news.models import TriageVerdict
from tracefold.news.opennews import parse_opennews_message
from tracefold.news.pipeline.admission import admit_item
from tracefold.news.program.artifact import (
    apply_program_patch,
    load_stable_program_artifact,
)
from tracefold.news.program.contracts import (
    JUDGMENT_CONTRACT_VERSION,
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
from tracefold.news.program.identity import EXECUTION_ENVELOPE_SHA256
from tracefold.news.program.runtime import PROGRAM_SCHEMA_VERSION, PROGRAM_VERSION
from tracefold.news.release.canary import (
    CANARY_ELIGIBILITY_PROFILE_SHA,
    CANARY_ROLLING_PROFILE_SHA,
    CANARY_SELECTOR_VERSION,
)
from tracefold.news.review.desk import (
    BlindPairwiseSubmission,
    DeskQuery,
    EventRubricSubmission,
    ExternalMissSubmission,
    Principal,
    TaskRef,
)
from tracefold.news.review.desk import (
    ReviewDesk as _ReviewDesk,
)
from tracefold.news.taxonomy import ModelTaxonomyV1
from tracefold.news.triage_rules import DEFAULT_POLICY

pytestmark = pytest.mark.integration

NOW = 1_800_000_000_000


class _PinnedLedger(LearningLedger):
    """Pin the DB clock beyond this immutable epoch fixture's closed window."""

    def now_ms(self) -> int:
        return NOW + 20 * 60_000


def _datasets(conn: Any, stable: ArmManifest) -> DevelopmentDatasetStore:
    """The dataset store with this suite's pinned clock, built the way production builds it."""

    return DevelopmentDatasetStore(conn, stable=stable, ledger=_PinnedLedger(conn, stable=stable, principal="operator"))


class CandidateEvaluator(_CandidateEvaluator):
    def __init__(self, conn: Any, **kwargs: Any) -> None:
        stable = kwargs["stable"]
        principal = kwargs.get("principal", "operator")
        super().__init__(conn, ledger=_PinnedLedger(conn, stable=stable, principal=principal), **kwargs)


class ReviewDesk(_ReviewDesk):
    """Place accepted fixture evidence inside the simulated future window."""

    def _db_now_ms(self) -> int:
        return NOW


@pytest.fixture()
def conn(postgres_clone_dsn: str):
    connection = connect_postgres_test(read_only=False)
    stable = _arm()
    with repositories_for_connection(connection).transaction():
        repositories_for_connection(connection).news.register_agent_runtime_manifest(
            manifest_sha=_sha({"stable": stable.bundle_sha, "runtime": "test"}),
            stable_bundle_sha=stable.bundle_sha,
            envelope_sha256=stable.envelope_sha256,
            artifact_schema_version=PROGRAM_SCHEMA_VERSION,
            program_version=stable.program_version,
            program_sha256=stable.program_sha256,
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
        "SELECT starts_at_ms FROM news_learning_epochs WHERE bundle_sha = %s",
        (_arm().bundle_sha,),
    ).fetchone()
    return int(row["starts_at_ms"])


def test_the_deployment_that_appoints_an_agent_opens_that_agent_s_epoch(conn) -> None:
    """No migration wrote this row: the startup barrier did, when it appointed the bundle (#314).

    Every column is checked against today's runtime values, which the predecessor could not do. An epoch
    used to be a hand-written label that outlived several Program re-issues, so the row recorded what it
    was *opened* with and the evaluator had to compare against a mirrored copy of those values. Keying the
    epoch to the bundle removes the gap: a re-issued Program, a moved envelope or a re-slotted model is a
    different bundle and therefore a different epoch, so "what opened it" and "what is running" are the
    same fact and disagreement is corruption.
    """

    stable = _arm()
    row = conn.execute(
        "SELECT epoch_id, bundle_sha, envelope_sha256, artifact_schema_version, baseline_program_version, "
        "baseline_program_sha256, prior_evidence_disposition, reset_reason, program_factory_id "
        "FROM news_learning_epochs WHERE bundle_sha = %s",
        (stable.bundle_sha,),
    ).fetchone()

    assert row["epoch_id"] == epoch_id_for_bundle(stable.bundle_sha)
    assert row["envelope_sha256"] == EXECUTION_ENVELOPE_SHA256 == stable.envelope_sha256
    assert row["artifact_schema_version"] == PROGRAM_SCHEMA_VERSION
    assert row["baseline_program_version"] == PROGRAM_VERSION
    assert row["baseline_program_sha256"] == stable.program_sha256
    assert row["prior_evidence_disposition"] == "audit_only"
    assert row["reset_reason"] == "runtime_bundle_identity_change"
    # Nothing declares a factory any more, and the column that used to hold one stays empty rather than
    # being handed a value invented to fill it.
    assert row["program_factory_id"] is None

    evaluator = CandidateEvaluator(conn, stable=stable, judges={})
    assert evaluator._ledger.epoch_started_at_ms() > 0

    # An epoch row that does not describe the running bundle is refused rather than used. The table is
    # append-only by trigger, so the drift is staged as a second bundle's row read through an arm whose
    # envelope does not match it.
    drifted = _arm(program_sha256="d" * 64)
    conn.execute(
        "INSERT INTO news_learning_epochs (epoch_id, starts_at_ms, source_issue, bundle_sha, "
        "envelope_sha256, artifact_schema_version, baseline_program_version, baseline_program_sha256, "
        "prior_evidence_disposition, reset_reason, created_at_ms) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'audit_only', 'runtime_bundle_identity_change', %s)",
        (
            epoch_id_for_bundle(drifted.bundle_sha),
            row_start := _epoch_started_at_ms(conn),
            "https://github.com/AnalyThothAI/tracefold/issues/314",
            drifted.bundle_sha,
            "0" * 64,
            PROGRAM_SCHEMA_VERSION,
            PROGRAM_VERSION,
            drifted.program_sha256,
            row_start,
        ),
    )
    with pytest.raises(ValueError, match="news_learning_epoch_contract_mismatch"):
        CandidateEvaluator(conn, stable=drifted, judges={})._ledger.epoch_started_at_ms()


def test_an_unseen_bundle_has_no_epoch_and_cannot_freeze_evidence(conn) -> None:
    """The refusal a missing epoch has to produce, now that no migration guarantees one exists."""

    never_deployed = _arm(program_sha256="e" * 64)

    with pytest.raises(ValueError, match="news_learning_epoch_not_deployed"):
        CandidateEvaluator(conn, stable=never_deployed, judges={})._ledger.epoch_started_at_ms()


def test_reopening_the_same_bundle_is_idempotent_and_keeps_the_original_start(conn) -> None:
    """A restart must not move the epoch its evidence is measured from."""

    stable = _arm()
    opened_at = _epoch_started_at_ms(conn)
    repositories = repositories_for_connection(conn)
    with repositories.transaction():
        assert (
            repositories.news.open_learning_epoch(
                bundle_sha=stable.bundle_sha,
                envelope_sha256=stable.envelope_sha256,
                artifact_schema_version=PROGRAM_SCHEMA_VERSION,
                program_version=stable.program_version,
                program_sha256=stable.program_sha256,
                now_ms=NOW,
            )
            is False
        )

    assert _epoch_started_at_ms(conn) == opened_at
    assert (
        int(
            conn.execute(
                "SELECT count(*) AS n FROM news_learning_epochs WHERE bundle_sha = %s",
                (stable.bundle_sha,),
            ).fetchone()["n"]
        )
        == 1
    )


def _arm(
    *,
    policy: dict[str, object] | None = None,
    program_version: str = PROGRAM_VERSION,
    program_sha256: str | None = None,
    runtime_model_bindings_sha256: str | None = None,
) -> ArmManifest:
    selected_policy = policy or DEFAULT_POLICY.as_dict()
    return ArmManifest(
        program_version=program_version,
        program_sha256=program_sha256 or _sha({"program": program_version, "fixture": "stable"}),
        envelope_sha256=EXECUTION_ENVELOPE_SHA256,
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
        "assets": [],
        "direction": "bullish",
        "scope": "sector",
        "magnitude": 2,
        "confidence": 0.8,
        "audience": "us_equity",
        "headline_zh": "DRAM 合约价续涨",
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


def _editorial() -> EditorialEnvelope:
    return EditorialEnvelope.issue(
        relevance=_relevance(),
        taxonomy=news_taxonomy(
            event_family="regulatory_legal",
            change_state="reported",
            assertion_status="claimed",
            source_authority="reputable_secondary",
        ),
    )


def _observed_judgment_fields(verdict: dict[str, object]) -> dict[str, object]:
    editorial = _editorial()
    scored = ScoredJudgment.issue(
        verdict=TriageVerdict.model_validate(verdict),
        editorial=editorial,
    )
    return {
        "verdict": verdict,
        "model_editorial": editorial.model_dump(mode="json"),
        "judgment_sha256": scored.scored_judgment_sha256,
    }


def _trace(arm: ArmManifest, context: TriageContext, verdict: dict[str, object]) -> ProgramTrace:
    context_sha = _sha(context.model_dump(mode="json"))
    semantics = {key: value for key, value in verdict.items() if key not in {"headline_zh", "why_zh"}}
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
    calls: list[ProgramCallTrace] = []
    for predictor, output in (("event_semantics", semantics), ("reader_card", card)):
        request = {
            "schema": "tracefold.news.lm_request.v1",
            "model": "fixture-model",
            "messages": [
                {
                    "role": "user",
                    "parts": [{"type": "text", "metadata": {}, "text": f"fixture:{predictor}:{context_sha}"}],
                    "metadata": {},
                }
            ],
            "tools": [],
            "config": {"extensions": {}},
        }
        request_identity = {
            "schema": "tracefold.news.audited_lm_request.v2",
            "endpoint_fingerprint": runtime_model_sha,
            "model_binding": "news_triage_primary",
        }
        request_sha = _sha({**request_identity, "request": request})
        output_field = "semantics" if predictor == "event_semantics" else "card"
        recording = {
            "schema": "tracefold.news.recorded_lm.v1",
            "request_sha256": request_sha,
            "request_identity": request_identity,
            "request": request,
            "response": {
                "model": "fixture-model",
                "text": json.dumps({output_field: output}, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                "finish_reason": "stop",
                "truncated": False,
                "usage": {
                    "input_tokens": 250,
                    "output_tokens": 45,
                    "total_tokens": 295,
                    "cache_read_tokens": 20,
                },
                "cost": 0.0001,
            },
            "error": None,
        }
        calls.append(
            ProgramCallTrace(
                predictor=predictor,
                route="primary",
                attempt=1,
                request_sha256=request_sha,
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
                terminal_disposition="provider_success",
                invocation_sha256=_sha(
                    {
                        "program_sha256": arm.program_sha256,
                        "context_sha256": context_sha,
                        "predictor": predictor,
                        "request_sha256": request_sha,
                    }
                ),
                recording=recording,
            )
        )
    return ProgramTrace(
        program_version=arm.program_version,
        program_sha256=arm.program_sha256,
        context_sha256=context_sha,
        envelope_sha256=EXECUTION_ENVELOPE_SHA256,
        event_semantics_sha256=_sha(semantics),
        reader_card_sha256=_sha(card),
        verdict_sha256=_sha(verdict),
        editorial_sha256=editorial.editorial_sha256,
        answering_route="primary",
        calls=tuple(calls),
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
            verdict.update(magnitude=0)
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


class _FirstCaseMissingProviderCostJudge(_StaticJudge):
    async def judge(self, context: TriageContext) -> SemanticJudgment:
        judgment = await super().judge(context)
        if len(self.calls) > 1:
            return judgment
        calls = tuple(call.model_copy(update={"provider_cost_microusd": None}) for call in judgment.trace.calls)
        return judgment.model_copy(
            update={
                "trace": judgment.trace.model_copy(update={"calls": calls}),
                "usage": judgment.usage.model_copy(update={"provider_cost_microusd": None}),
            }
        )


class _MixedCallPricingJudge(_StaticJudge):
    async def judge(self, context: TriageContext) -> SemanticJudgment:
        judgment = await super().judge(context)
        first, second = judgment.trace.calls
        calls = (first.model_copy(update={"provider_cost_microusd": None}), second)
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
                "terminal_disposition": None,
                "invocation_sha256": None,
                "recording": None,
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


def _objective_plan(conn, *, stable: ArmManifest, development_sha: str) -> GepaObjectivePlan:
    """The plan the release gate will rebuild, asked of the same frozen dataset the fixture froze."""

    exported = _datasets(conn, stable).development_compile_export(development_sha)
    return build_gepa_objective_plan(tuple(DevelopmentEpisode.model_validate(e) for e in exported.episodes))


def _prompt_candidate(
    conn,
    *,
    development_sha: str,
    stable: ArmManifest,
    patch: PromptPatchV1,
    objective_summary: dict[str, object] | None = None,
    **overrides: object,
) -> PromptCandidateV1:
    """One registered write-set: two advisory instructions, and what they were optimized against.

    It replaced a `CompileRecordV1` that carried a sandbox launch receipt, a metered proxy ledger, a
    three-party build attestation and a tariff — none of which said anything about the two instructions.
    The objective summary is the real plan for the real frozen corpus, not a plausible pair of numbers:
    `_validate_candidate_static` rebuilds that plan from the same dataset, so a hand-picked summary here
    would be a fixture asserting its own fiction.
    """

    exported = _datasets(conn, stable).development_compile_export(development_sha)
    plan = build_gepa_objective_plan(tuple(DevelopmentEpisode.model_validate(e) for e in exported.episodes))
    values: dict[str, object] = {
        "parent_program_sha256": stable.program_sha256,
        "development_dataset_sha256": development_sha,
        "target_runtime_manifest_sha256": stable.runtime_model_bindings_sha256,
        "patch": patch,
        "objective_summary": (
            objective_summary
            if objective_summary is not None
            else objective_plan_summary(plan, episode_projection_root_sha256=exported.episode_projection_root_sha256)
        ),
        "optimizer": {"schema": "tracefold.news.compile_optimizer_config_receipt.v5"},
        "model_identities": {"task": {"role": "task"}, "reflection": {"role": "reflection"}},
        "budget": {"max_metric_calls": 12},
        "usage": {"metric_calls": 12},
        "created_at_ms": NOW,
    }
    values.update(overrides)
    return PromptCandidateV1.issue(**values)


def _program_candidate(
    conn,
    *,
    stable: ArmManifest,
    development_sha: str,
    cluster_id: str,
    persist_receipt: bool = True,
    prompt: PromptCandidateV1 | None = None,
    prompt_payload: dict[str, object] | None = None,
    prompt_parent_sha: str | None = None,
    persist_prompt: bool = True,
    generator_kind: str = "model",
    program_version: str | None = None,
    program_sha256: str | None = None,
    failure_cluster_ids: tuple[str, ...] | None = None,
    target_dimensions: tuple[str, ...] | None = None,
    projection_root: str | None = None,
    variant: str = "",
) -> CandidateManifest:
    base = load_stable_program_artifact()
    registered = prompt or _prompt_candidate(
        conn,
        development_sha=development_sha,
        stable=stable,
        # One distinct instruction per cluster and variant, so two fixtures in one test are two different
        # Programs — and two different registration receipts, which the ledger addresses uniquely.
        patch=PromptPatchV1(
            event_semantics_instruction=f"A bounded fixture advisory for {cluster_id}{variant}.",
            reader_card_instruction="Keep reader language direct and evidence-bound.",
        ),
    )
    # The arm's Program identity is *derived* now, not declared: the release gate re-applies the
    # registered patch to the running stable and refuses anything else. A fixture that invented a program
    # SHA was asserting a lineage nobody checked.
    applied = apply_program_patch(base, registered.patch.applied_to(base))
    arm_payload = stable.model_dump(mode="json")
    arm_payload.update(
        program_version=program_version or PROGRAM_VERSION,
        program_sha256=program_sha256 or applied.program_sha256,
    )
    candidate_arm = ArmManifest.model_validate(arm_payload)
    payload = registered.model_dump(mode="json") if prompt_payload is None else prompt_payload
    if persist_prompt:
        _persist_prompt_candidate(
            conn,
            development_sha=prompt_parent_sha or development_sha,
            payload=payload,
            # The address the receipt names. For an honest candidate this is the payload's own root; the
            # tamper cases deliberately store something else there, which is what the evaluator has to
            # catch rather than trusting the key.
            artifact_sha=registered.candidate_sha256,
        )
    exported = _datasets(conn, stable).development_compile_export(development_sha)
    plan = _objective_plan(conn, stable=stable, development_sha=development_sha)
    receipt_values = {
        "development_dataset_sha": development_sha,
        "development_episode_projection_root_sha256": projection_root or exported.episode_projection_root_sha256,
        # The plan's verified Prompt targets, not one cluster the caller happened to name. #199 made this
        # an equality: a candidate that declares anything else was optimized against a different corpus.
        "failure_cluster_ids": failure_cluster_ids or plan.target_failure_cluster_ids or (cluster_id,),
        "generator_kind": generator_kind,
        "registered_at_ms": NOW,
        "declared_target_dimensions": target_dimensions or plan.target_dimensions or ("why_support",),
        "guardrails": ("must_push_recall", "reader_load"),
        "program_parent_sha256": stable.program_sha256,
        "program_candidate_sha256": candidate_arm.program_sha256,
        "prompt_candidate_sha256": registered.candidate_sha256,
    }
    proposal_receipt = _proposal(conn, **receipt_values) if persist_receipt else ProposalReceipt.issue(**receipt_values)
    return CandidateManifest(
        parent_stable_sha=stable.bundle_sha,
        candidate_arm=candidate_arm,
        hypothesis="Remove unsupported priced-in dismissals without changing reader load.",
        target_dimensions=target_dimensions or plan.target_dimensions or ("why_support",),
        development_dataset_sha=development_sha,
        proposal_receipt=proposal_receipt,
    )


def _persist_prompt_candidate(
    conn,
    *,
    development_sha: str,
    payload: dict[str, object],
    artifact_sha: str | None = None,
) -> str:
    """Stage one candidate row directly, so a test can plant an address the document does not answer to.

    `learning register` stores this kind under the candidate's own root, because the document already
    carries an identity and a second address would force every reader to know both. This fixture writes
    the row by hand precisely so the tamper cases can put something *else* in `artifact_sha` — which is
    also why it proves nothing about the real writer, and why
    `test_the_ledger_stores_a_prompt_candidate_under_the_identity_its_receipt_names` exists beside it.
    """

    address = artifact_sha or str(payload["candidate_sha256"])
    conn.execute(
        "INSERT INTO news_learning_artifacts "
        "(artifact_sha, kind, parent_sha, payload, created_by, created_at_ms) "
        "VALUES (%s, 'prompt_candidate', %s, %s::jsonb, 'test', %s) "
        "ON CONFLICT (artifact_sha) DO NOTHING",
        (address, development_sha, json.dumps(payload, sort_keys=True), NOW),
    )
    return address


def _insert_validation_dataset(conn, *, development, candidate: CandidateManifest) -> str:
    payload = {
        "dataset_version": "news_learning_dataset_v3",
        "role": "validation",
        "profile_id": "news_learning_release_v3",
        "learning_epoch": epoch_id_for_bundle(_arm().bundle_sha),
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
    final_decision: str = "push",
    throttled_by: str | None = None,
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
        semantics = {key: value for key, value in verdict.items() if key not in {"headline_zh", "why_zh"}}
        semantics["relevance"] = effective_relevance.model_dump(mode="json")
        card = {key: verdict[key] for key in ("headline_zh", "why_zh")}
        editorial = EditorialEnvelope.issue(
            relevance=effective_relevance,
            taxonomy=news_taxonomy(
                event_family="regulatory_legal",
                change_state="reported",
                assertion_status="claimed",
                source_authority="reputable_secondary",
            ),
        )
        semantics["taxonomy"] = editorial.taxonomy.model_dump(
            mode="json",
            exclude={"taxonomy_version", "source_authority", "codebook_sha256"},
        )
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
                envelope_sha256=stable.envelope_sha256,
                artifact_schema_version=PROGRAM_SCHEMA_VERSION,
                program_version=stable.program_version,
                program_sha256=stable.program_sha256,
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
                "envelope_sha256": EXECUTION_ENVELOPE_SHA256,
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
            policy_version=dataset_module.TRIAGE_POLICY_VERSION,
            judgment_contract_version=JUDGMENT_CONTRACT_VERSION,
            judgment_origin="model",
            rule_baseline_decision="push",
            final_decision=final_decision,
            override_rule="trade_relevance_realtime",
            throttled_by=throttled_by,
            verdict=verdict,
            model_editorial=editorial.model_dump(mode="json"),
            judgment_sha256=scored.scored_judgment_sha256,
            runtime_manifest_sha=str(runtime_manifest_row["manifest_sha"]),
            model="fixture-model",
            program_version=effective_program_version,
            program_sha256=effective_program_sha,
            degraded=False,
            error_code=None,
            trace={
                "program_version": effective_program_version,
                "program_sha256": effective_program_sha,
                "judgment_contract_version": JUDGMENT_CONTRACT_VERSION,
                "judgment_origin": "model",
                "judgment_sha256": scored.scored_judgment_sha256,
                "verdict_sha256": scored.verdict_sha256,
                "editorial_sha256": editorial.editorial_sha256,
                "runtime_manifest_sha": str(runtime_manifest_row["manifest_sha"]),
                "evidence_version": int(evidence["evidence_version"]),
                "evidence_sha256": str(evidence["evidence_sha256"]),
                "focus_fact_id": str(evidence["focus_fact_id"]),
                "told": [],
                "told_count": 0,
                **({"seen_scope": "all"} if throttled_by else {}),
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
    taxonomy_mismatch: bool = False,
    magnitude: str | None = None,
    published_at_ms: int | None = None,
    relevance: TradeRelevanceV1 | None = None,
    delivered: bool = True,
    final_decision: str = "push",
    throttled_by: str | None = None,
    delivery_error_code: str | None = None,
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
        delivered=delivered,
        final_decision=final_decision,
        throttled_by=throttled_by,
    )
    if delivery_error_code is not None:
        repos = repositories_for_connection(conn)
        with repos.transaction():
            assert repos.news.begin_delivery(event_id=event_id, kind="first", card={}, now_ms=NOW - 1000) == "new"
            assert repos.news.settle_delivery(
                event_id=event_id,
                kind="first",
                state="terminal",
                receipt=None,
                error_code=delivery_error_code,
                now_ms=NOW,
            )
    desk = ReviewDesk(conn, now_ms=NOW)
    task = desk.open(
        # Event lookup is deterministic; no random queue sampling is involved.
        DeskQuery(event=event_id),
        principal=PRINCIPAL,
    )["tasks"][0]
    rubric = _rubric(
        why=why,
        should_push=should_push,
        magnitude=magnitude,
        # A taxonomy target is authorized only by this explicit owner; other failed dimensions are
        # diagnostics under #456 and grant no optimizer authority.
        first_bad_owner=first_bad_owner if (why != "pass" or magnitude == "fail" or taxonomy_mismatch) else None,
    )
    if taxonomy_mismatch:
        rubric = EventRubricSubmission.model_validate(
            rubric.model_dump(mode="json")
            | {
                "taxonomy": news_taxonomy(
                    event_family="product_service_change",
                    change_state="reported",
                    assertion_status="claimed",
                    source_authority="reputable_secondary",
                ).model_dump(mode="json")
            }
        )
    with repositories_for_connection(conn).transaction():
        desk.submit(
            TaskRef(task_id=task["task_id"], task_version=task["task_version"]),
            rubric,
            principal=PRINCIPAL,
            idempotency_key=str(uuid.uuid4()),
        )
    return event_id


def test_one_operator_taxonomy_freezes_into_the_existing_episode_and_projection_root(conn) -> None:
    stable = _arm()
    event_id = _open_event(
        conn,
        bundle_sha=stable.bundle_sha,
        program_version=stable.program_version,
        program_sha256=stable.program_sha256,
        hit_id=112099,
        title="Coinbase makes a new institutional custody service generally available",
    )
    desk = ReviewDesk(conn, now_ms=NOW)
    task = desk.open(DeskQuery(event=event_id), principal=PRINCIPAL)["tasks"][0]
    taxonomy = news_taxonomy(
        event_family="product_service_change",
        change_state="effective",
        assertion_status="confirmed",
        source_authority="reputable_secondary",
    )
    submission = EventRubricSubmission.model_validate(
        _rubric(why="pass").model_dump(mode="json") | {"taxonomy": taxonomy.model_dump(mode="json")}
    )
    mismatched_source = EventRubricSubmission.model_validate(
        submission.model_dump(mode="json")
        | {
            "taxonomy": news_taxonomy(
                event_family="product_service_change",
                change_state="effective",
                assertion_status="confirmed",
                source_authority="unknown",
            ).model_dump(mode="json")
        }
    )
    with (
        repositories_for_connection(conn).transaction(),
        pytest.raises(ValueError, match="news_review_taxonomy_source_authority_code_mismatch"),
    ):
        desk.submit(
            TaskRef(task_id=task["task_id"], task_version=task["task_version"]),
            mismatched_source,
            principal=PRINCIPAL,
            idempotency_key=str(uuid.uuid4()),
        )
    self_draft = EventRubricSubmission.model_validate(
        submission.model_dump(mode="json")
        | {
            "taxonomy_review": {
                "label_source": "model_draft",
                "draft_author": PRINCIPAL.subject,
                "review_role": "primary",
                "draft_taxonomy": taxonomy.model_dump(mode="json"),
            }
        }
    )
    with (
        repositories_for_connection(conn).transaction(),
        pytest.raises(ValueError, match="news_review_taxonomy_self_acceptance_forbidden"),
    ):
        desk.submit(
            TaskRef(task_id=task["task_id"], task_version=task["task_version"]),
            self_draft,
            principal=Principal(subject=PRINCIPAL.subject),
            idempotency_key=str(uuid.uuid4()),
        )
    with repositories_for_connection(conn).transaction():
        desk.submit(
            None,
            ExternalMissSubmission(
                source_url="https://example.test/taxonomy-miss",
                title="A separate material fact the receiver never ingested",
                occurred_at_ms=NOW - 60_000,
                rubric=submission,
            ),
            principal=PRINCIPAL,
            idempotency_key=str(uuid.uuid4()),
        )
    with repositories_for_connection(conn).transaction():
        receipt = desk.submit(
            TaskRef(task_id=task["task_id"], task_version=task["task_version"]),
            submission,
            principal=PRINCIPAL,
            idempotency_key=str(uuid.uuid4()),
        )

    assert (
        conn.execute(
            "SELECT release_eligible FROM news_reviews WHERE review_id = %s",
            (receipt["receipt"]["review_id"],),
        ).fetchone()["release_eligible"]
        is True
    )
    manifest = asyncio.run(
        _datasets(conn, stable).freeze_dataset(
            DatasetSpec(role="development", window=ClosedWindow(from_ms=NOW - 6 * 3_600_000, to_ms=NOW))
        )
    )
    export = _datasets(conn, stable).development_compile_export(manifest.artifact_sha)
    assert manifest.counts["case_n"] == manifest.counts["independent_cluster_n"] == 2
    event_episode = next(episode for episode in export.episodes if episode["production_judgment"] is not None)
    assert event_episode["accepted_review"]["taxonomy"] == taxonomy.model_dump(
        mode="json", include=set(ModelTaxonomyV1.model_fields)
    )
    assert export.episode_projection_root_sha256 == canonical_sha(list(export.episodes))
    changed = [dict(episode) for episode in export.episodes]
    changed_event = next(episode for episode in changed if episode["production_judgment"] is not None)
    changed_review = dict(changed_event["accepted_review"])
    changed_taxonomy = dict(changed_review["taxonomy"])
    changed_taxonomy["event_family"] = "other"
    changed_review["taxonomy"] = changed_taxonomy
    changed_event["accepted_review"] = changed_review
    assert canonical_sha(changed) != export.episode_projection_root_sha256


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
        # #456 admits only exact taxonomy mismatches with an explicit taxonomy owner.
        is_target = role == "target" and why != "pass"
        event_ids.append(
            _accepted_event(
                conn,
                why="pass",
                first_bad_owner="taxonomy" if is_target else None,
                taxonomy_mismatch=is_target,
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
            envelope_sha256=prior_runtime.envelope_sha256,
            artifact_schema_version=PROGRAM_SCHEMA_VERSION,
            program_version=prior_runtime.program_version,
            program_sha256=prior_runtime.program_sha256,
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
            envelope_sha256=stable.envelope_sha256,
            artifact_schema_version=PROGRAM_SCHEMA_VERSION,
            program_version=stable.program_version,
            program_sha256=stable.program_sha256,
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
        _datasets(conn, stable).freeze_dataset(
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


def test_freeze_dataset_includes_undelivered_holds_but_excludes_unsafe_pushes(conn) -> None:
    stable = _arm()
    undelivered_drop = _accepted_event(
        conn,
        stable=stable,
        hit_id=112021,
        title="Nintendo keeps its full-year console shipment guidance unchanged",
        delivered=False,
        final_decision="drop",
        should_push="must_hold",
        published_at_ms=NOW - 3_900_000,
    )
    throttled_drop = _accepted_event(
        conn,
        stable=stable,
        hit_id=112022,
        title="Chile repeats its existing lithium royalty schedule",
        delivered=False,
        final_decision="throttled",
        throttled_by="storyline:fixture:seen",
        should_push="must_hold",
        published_at_ms=NOW - 3_800_000,
    )
    undelivered_push = _accepted_event(
        conn,
        stable=stable,
        hit_id=112023,
        title="European Central Bank unexpectedly cuts its deposit rate by 50 basis points",
        delivered=False,
        published_at_ms=NOW - 3_700_000,
    )
    ambiguous_push = _accepted_event(
        conn,
        stable=stable,
        hit_id=112024,
        title="Brazil suspends soybean export licences for two crushing plants",
        delivered=False,
        delivery_error_code="ambiguous_after_crash",
        published_at_ms=NOW - 3_600_000,
    )

    development = asyncio.run(
        _datasets(conn, stable).freeze_dataset(
            DatasetSpec(
                role="development",
                window=ClosedWindow(from_ms=NOW - 6 * 3_600_000, to_ms=NOW),
            )
        )
    )

    case_ids = {case.event_id for case in development.cases}
    assert case_ids == {undelivered_drop, throttled_drop}
    assert undelivered_push not in case_ids
    assert ambiguous_push not in case_ids
    assert development.counts["case_n"] == 2
    assert development.counts["eligible_event_n"] == 4


def test_program_epoch_rejects_old_windows_and_old_artifacts_but_preserves_audit_json(conn) -> None:
    stable = _arm()
    evaluator = CandidateEvaluator(conn, stable=stable, judges={})
    epoch_started_at_ms = _epoch_started_at_ms(conn)
    with pytest.raises(ValueError, match="news_learning_window_precedes_program_epoch"):
        asyncio.run(
            evaluator._datasets.freeze_dataset(
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
        evaluator._datasets.freeze_dataset(
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
        evaluator._datasets.development_compile_episodes(old_sha)
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
        evaluator._datasets.freeze_dataset(
            DatasetSpec(
                role="development",
                window=ClosedWindow(from_ms=NOW - 6 * 3_600_000, to_ms=NOW),
            )
        )
    )

    episodes = evaluator._datasets.development_compile_episodes(development.artifact_sha)

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
        evaluator._datasets.freeze_dataset(
            DatasetSpec(
                role="development",
                window=ClosedWindow(from_ms=NOW - 6 * 3_600_000, to_ms=NOW),
            )
        )
    )

    exported = evaluator._datasets.development_compile_export(development.artifact_sha)

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


def test_frozen_dataset_replays_its_pin_after_new_evidence_without_a_verdict(conn) -> None:
    event_id = _accepted_event(conn, why="pass")
    stable = _arm()
    datasets = _datasets(conn, stable)
    development = asyncio.run(
        datasets.freeze_dataset(
            DatasetSpec(role="development", window=ClosedWindow(from_ms=NOW - 6 * 3_600_000, to_ms=NOW))
        )
    )
    before = datasets.development_compile_export(development.artifact_sha)

    repos = repositories_for_connection(conn)
    with repos.transaction():
        repos.news.set_storyline_key(
            event_id=event_id,
            storyline_key="macro:later-source-context",
            now_ms=NOW + 1,
        )
        newer = repos.news.append_evidence_snapshot(event_id=event_id, now_ms=NOW + 2)

    assert int(newer["evidence_version"]) == int(development.cases[0].evidence_version or 0) + 1
    assert conn.execute("SELECT 1 FROM news_review_task_source_v1 WHERE event_id = %s", (event_id,)).fetchone() is None

    after = datasets.development_compile_export(development.artifact_sha)

    assert after.episodes == before.episodes
    assert after.episode_projection_root_sha256 == before.episode_projection_root_sha256


def test_development_compile_export_rejects_a_forged_dataset_artifact_sha(conn) -> None:
    _accepted_event(conn, why="pass")
    evaluator = CandidateEvaluator(conn, stable=_arm(), judges={})
    development = asyncio.run(
        evaluator._datasets.freeze_dataset(
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
        evaluator._datasets.development_compile_export(forged_sha)


def test_the_ledger_stores_a_prompt_candidate_under_the_identity_its_receipt_names(conn) -> None:
    """The real writer against the real reader, with no fixture standing between them.

    Every other test here stages the row by hand so it can plant a wrong address, which is exactly how the
    writer and the reader came to disagree without a single test noticing: `append_proposal_artifact`
    addressed the row as `sha({kind, payload})` while the reader looked it up by the SHA the receipt
    names. Every record written through registration was reported missing, and no candidate could be
    evaluated. This drives both sides.
    """

    # Two, because the Objective Plan needs a non-empty validation split: a corpus of one cannot be split.
    _accepted_event(conn, why="pass")
    _accepted_event(conn, why="pass", hit_id=112042, title="A second reviewed Event, so the split is real")
    stable = _arm()
    evaluator = CandidateEvaluator(conn, stable=stable, judges={})
    development = asyncio.run(
        evaluator._datasets.freeze_dataset(
            DatasetSpec(role="development", window=ClosedWindow(from_ms=NOW - 6 * 3_600_000, to_ms=NOW))
        )
    )
    base = load_stable_program_artifact()
    registered = _prompt_candidate(
        conn,
        development_sha=development.artifact_sha,
        stable=stable,
        patch=PromptPatchV1(
            event_semantics_instruction="Written through the repository.",
            reader_card_instruction="Keep the mechanism concrete.",
        ),
    )
    written = repositories_for_connection(conn).news.append_proposal_artifact(
        kind="prompt_candidate",
        payload=registered.model_dump(mode="json"),
        parent_sha=development.artifact_sha,
        created_at_ms=NOW,
    )

    assert written == registered.candidate_sha256

    applied = apply_program_patch(base, registered.patch.applied_to(base))
    candidate = CandidateManifest(
        parent_stable_sha=stable.bundle_sha,
        candidate_arm=ArmManifest.model_validate(
            {**stable.model_dump(mode="json"), "program_sha256": applied.program_sha256}
        ),
        hypothesis="Name the comparison base the reader needs.",
        target_dimensions=("why_support",),
        development_dataset_sha=development.artifact_sha,
        proposal_receipt=ProposalReceipt.issue(
            development_dataset_sha=development.artifact_sha,
            development_episode_projection_root_sha256=evaluator._datasets.development_compile_export(
                development.artifact_sha
            ).episode_projection_root_sha256,
            failure_cluster_ids=("cluster-0",),
            generator_kind="model",
            registered_at_ms=NOW,
            declared_target_dimensions=("why_support",),
            guardrails=("must_push_recall", "reader_load"),
            program_parent_sha256=stable.program_sha256,
            program_candidate_sha256=applied.program_sha256,
            prompt_candidate_sha256=registered.candidate_sha256,
        ),
    )

    assert evaluator._registry._prompt_candidate(candidate).candidate_sha256 == registered.candidate_sha256


def test_active_stable_is_checked_before_freeze_or_model_work(conn) -> None:
    stale = _arm(program_sha256=_sha({"program": "other"}))
    judges = _static_judges(stale)
    evaluator = CandidateEvaluator(conn, stable=stale, judges=judges)

    with pytest.raises(ValueError, match="news_learning_active_stable_mismatch"):
        asyncio.run(
            evaluator._datasets.freeze_dataset(
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
            envelope_sha256=legacy.envelope_sha256,
            artifact_schema_version=PROGRAM_SCHEMA_VERSION,
            program_version=legacy.program_version,
            program_sha256=legacy.program_sha256,
            candidate_shas=(),
            image_digest="sha256:legacy-v1",
            runtime_revision="legacy-v1",
            now_ms=NOW,
        )
    evaluator = CandidateEvaluator(conn, stable=legacy, judges={})

    with pytest.raises(ValueError, match="news_learning_program_v1_unsupported"):
        asyncio.run(
            evaluator._datasets.freeze_dataset(
                DatasetSpec(
                    role="development",
                    window=ClosedWindow(from_ms=NOW - 6 * 3_600_000, to_ms=NOW),
                )
            )
        )
    with pytest.raises(ValueError, match="news_learning_program_v1_unsupported"):
        evaluator._datasets.development_compile_episodes("f" * 64)


def test_a_candidate_is_only_as_good_as_the_write_set_it_names(conn) -> None:
    """#202 §7: what a candidate has to prove, and what it no longer has to.

    It must name a typed write-set that validates, and that write-set applied to the running stable must
    be the Program its arm will execute. It does *not* have to have been produced by a trusted compiler —
    a patch a person wrote passes the same static gate as one GEPA wrote, which is the whole point of
    deleting the second generation lifecycle.
    """

    _accepted_compilable_event(conn)
    stable = _arm()
    bootstrap = CandidateEvaluator(conn, stable=stable, judges={})
    development = asyncio.run(
        bootstrap._datasets.freeze_dataset(
            DatasetSpec(
                role="development",
                window=ClosedWindow(from_ms=NOW - 6 * 3_600_000, to_ms=NOW),
            )
        )
    )
    cluster_id = development.cases[0].cluster_id
    invalid_write_set = _program_candidate(
        conn,
        stable=stable,
        development_sha=development.artifact_sha,
        cluster_id=cluster_id,
        variant=" (invalid)",
        # Stored under the root the receipt names, but not a candidate.
        prompt_payload={"development_dataset_sha256": development.artifact_sha},
    )
    human_written = _program_candidate(
        conn,
        stable=stable,
        development_sha=development.artifact_sha,
        cluster_id=cluster_id,
        generator_kind="human",
        prompt=_prompt_candidate(
            conn,
            development_sha=development.artifact_sha,
            stable=stable,
            patch=PromptPatchV1(
                event_semantics_instruction="An operator wrote this advisory by hand.",
                reader_card_instruction="Keep the mechanism concrete.",
            ),
            # No optimizer receipt and no objective summary: an external proposal claims nothing about how
            # it was produced, and registration binds it to the corpus it re-projected.
            optimizer={},
            objective_summary={},
        ),
    )
    forged_identity = _program_candidate(
        conn,
        stable=stable,
        development_sha=development.artifact_sha,
        cluster_id=cluster_id,
        variant=" (forged)",
        program_sha256=_sha({"program": "candidate", "case": "not_the_applied_patch"}),
    )
    legacy_program = _program_candidate(
        conn,
        stable=stable,
        development_sha=development.artifact_sha,
        cluster_id=cluster_id,
        variant=" (legacy)",
        program_version="news_semantic_program_v1",
    )
    judges = _static_judges(stable, invalid_write_set.candidate_arm)
    evaluator = CandidateEvaluator(
        conn,
        stable=stable,
        judges=judges,
        candidate_catalog=(invalid_write_set, human_written, forged_identity, legacy_program),
    )

    for candidate, code in (
        (invalid_write_set, "news_learning_prompt_candidate_invalid"),
        (forged_identity, "news_learning_prompt_candidate_program_identity_mismatch"),
        (legacy_program, "news_learning_program_v1_unsupported"),
    ):
        with pytest.raises(ValueError, match=code):
            evaluator._registry.validate(candidate)
    # The one that must *not* raise. Provenance is audit, not permission.
    evaluator._registry.validate(human_written)
    assert human_written.proposal_receipt.generator_kind == "human"
    assert _judge_call_count(judges) == 0


@pytest.mark.parametrize(
    ("mode", "error_code"),
    [
        ("absent", "news_learning_prompt_candidate_missing"),
        ("tampered_byte", "news_learning_prompt_candidate_invalid"),
        ("re_addressed_tamper", "news_learning_prompt_candidate_identity_mismatch"),
        ("wrong_dataset_parent", "news_learning_prompt_candidate_parent_mismatch"),
    ],
)
def test_a_candidate_requires_the_exact_persisted_write_set(
    conn,
    mode: str,
    error_code: str,
) -> None:
    """The ledger row the receipt names has to be that write-set, byte for byte.

    `candidate_sha256` is both the document's own root and the key it is stored under, so a byte changed
    in the stored payload either stops validating or stops resolving at the key it claims.
    """

    _accepted_compilable_event(conn)
    stable = _arm()
    bootstrap = CandidateEvaluator(conn, stable=stable, judges={})
    development = asyncio.run(
        bootstrap._datasets.freeze_dataset(
            DatasetSpec(
                role="development",
                window=ClosedWindow(from_ms=NOW - 6 * 3_600_000, to_ms=NOW),
            )
        )
    )
    registered = _prompt_candidate(
        conn,
        development_sha=development.artifact_sha,
        stable=stable,
        patch=PromptPatchV1(
            event_semantics_instruction=f"A bounded fixture advisory for {mode}.",
            reader_card_instruction="Keep the mechanism concrete.",
        ),
    )
    payload = registered.model_dump(mode="json")
    tampered = dict(payload, created_at_ms=int(payload["created_at_ms"]) + 1)
    overrides: dict[str, object] = {}
    if mode == "absent":
        overrides = {"persist_prompt": False}
    elif mode == "tampered_byte":
        overrides = {"prompt_payload": tampered}
    elif mode == "re_addressed_tamper":
        # The tamperer re-derives the document's own root, so it validates — and no longer answers to the
        # ledger key the receipt points at.
        re_addressed = dict(tampered)
        re_addressed["candidate_sha256"] = _sha(
            {key: value for key, value in tampered.items() if key != "candidate_sha256"}
        )
        overrides = {"prompt_payload": re_addressed}
    else:
        overrides = {"prompt_parent_sha": _sha({"dataset": "some-other-development-corpus"})}
    candidate = _program_candidate(
        conn,
        stable=stable,
        development_sha=development.artifact_sha,
        cluster_id=development.cases[0].cluster_id,
        prompt=registered,
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


def test_successful_critical_case_cannot_authorize_a_failure_cluster(conn) -> None:
    _accepted_compilable_event(conn, why="pass")
    stable = _arm()
    bootstrap = CandidateEvaluator(conn, stable=stable, judges={})
    development = asyncio.run(
        bootstrap._datasets.freeze_dataset(
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


def test_a_dataset_bound_baseline_scores_the_objective_corpus_and_republishes_its_roots(conn) -> None:
    """#199 §5: readiness, this baseline, the trusted record and the release gate name one corpus.

    The check that matters is the last one — the baseline's split roots and episode root are the record's
    own, so the "before" number a release reads was measured on the corpus the winner was picked from. The
    predecessor had no `--dataset` at all: every baseline was a moving window, and two of them taken a day
    apart compared two different populations under one name.
    """

    from tracefold.news.learning.baseline import build_baseline_cases, run_baseline

    stable_artifact = load_stable_program_artifact()
    stable = _arm(program_sha256=stable_artifact.program_sha256)
    with repositories_for_connection(conn).transaction():
        repositories_for_connection(conn).news.register_agent_runtime_manifest(
            manifest_sha=_sha({"stable": stable.bundle_sha, "runtime": "dataset-baseline-test"}),
            stable_bundle_sha=stable.bundle_sha,
            envelope_sha256=stable.envelope_sha256,
            artifact_schema_version=PROGRAM_SCHEMA_VERSION,
            program_version=stable.program_version,
            program_sha256=stable.program_sha256,
            candidate_shas=(),
            image_digest="sha256:dataset-baseline-test",
            runtime_revision="dataset-baseline-test",
            now_ms=NOW - 23 * 3_600_000,
        )
    _accepted_compilable_event(conn, stable=stable)
    evaluator = CandidateEvaluator(conn, stable=stable, judges={})
    development = asyncio.run(
        evaluator._datasets.freeze_dataset(
            DatasetSpec(role="development", window=ClosedWindow(from_ms=NOW - 6 * 3_600_000, to_ms=NOW))
        )
    )
    export = evaluator._datasets.development_compile_export(development.artifact_sha)
    plan = _objective_plan(conn, stable=stable, development_sha=development.artifact_sha)
    assert plan.split is not None

    optimizer = set(plan.optimizer_case_ids)
    scored = tuple(episode for episode in export.episodes if str(episode["case_id"]) in optimizer)
    assert len(scored) == len(optimizer)

    # `compile_live` is the mode a formal optimizer baseline runs in — `recorded` is refused for a frozen
    # dataset, because the plan classifies under a replayed `decide()` and `recorded` scores against the
    # action that shipped. The graph is stubbed to a fixed judgment so the parity this test is about is
    # measured without a provider; what it exercises is the corpus, the plan and the roots.
    class _FrozenProgram:
        """The Program seam, answering one fixed judgment: no provider, no framework, no network."""

        async def judge(self, context: TriageContext) -> SemanticJudgment:
            verdict = _verdict()
            return SemanticJudgment(
                verdict=verdict,
                editorial=_editorial(),
                program_version=stable.program_version,
                program_sha256=stable.program_sha256,
                trace=_trace(stable, context, verdict),
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

    report = run_baseline(
        build_baseline_cases(scored, action_source="policy"),
        mode="compile_live",
        artifact=stable_artifact,
        semantic_judge=_FrozenProgram(),
        cohort_scope="frozen_development",
        objective=plan,
        dataset_identity={
            "development_dataset_sha": development.artifact_sha,
            "episode_projection_root_sha256": canonical_sha(list(export.episodes)),
        },
        # The corpus that still contains the retrieval misses the plan just excluded.
        retrieval_population=[DevelopmentEpisode.model_validate(episode) for episode in export.episodes],
    )

    # Only target + control reached a denominator; the excluded diagnostics are counted, never scored.
    assert report.population["requested_n"] == len(optimizer)
    assert report.objective["excluded_case_n"] == len(plan.excluded_case_ids)
    assert report.objective["target_failure_cluster_ids"] == list(plan.target_failure_cluster_ids)
    # Three numbers, not one: the selection half is the formal before value.
    assert report.subsets["train"]["case_n"] == len(plan.train_episodes)
    assert report.subsets["development_selection"]["case_n"] == len(plan.development_selection_episodes)
    assert (
        report.subsets["optimizer_union"]["case_n"]
        == report.subsets["train"]["case_n"] + report.subsets["development_selection"]["case_n"]
    )

    # The same corpus, seen by the other reader: what a candidate is registered against has to be what
    # the dataset-bound baseline scored, or the "before" number belongs to a different corpus.
    exported = _datasets(conn, stable).development_compile_export(development.artifact_sha)
    registered = _prompt_candidate(
        conn,
        development_sha=development.artifact_sha,
        stable=stable,
        patch=PromptPatchV1(
            event_semantics_instruction="Dataset baseline parity.",
            reader_card_instruction="Keep the mechanism concrete.",
        ),
    )
    assert exported.episode_projection_root_sha256 == report.identity["episode_projection_root_sha256"]
    assert registered.objective_summary["episode_projection_root_sha256"] == exported.episode_projection_root_sha256
    assert registered.objective_summary["split"] == report.objective["split"] == plan.split


def test_release_register_rejects_a_stale_optimizer_population_before_any_artifact_write(
    conn,
    tmp_path,
    monkeypatch,
) -> None:
    """F2P: registration must reject stale v2 population identity at the real CLI/PG seam."""

    from tracefold.app import learning_runtime
    from tracefold.app.cli.commands import news_learning as news_commands
    from tracefold.app.cli.parser import build_parser

    base = load_stable_program_artifact()
    stable = _arm(program_sha256=base.program_sha256)
    with repositories_for_connection(conn).transaction():
        repositories_for_connection(conn).news.register_agent_runtime_manifest(
            manifest_sha=_sha({"stable": stable.bundle_sha, "runtime": "register-f2p"}),
            stable_bundle_sha=stable.bundle_sha,
            envelope_sha256=stable.envelope_sha256,
            artifact_schema_version=PROGRAM_SCHEMA_VERSION,
            program_version=stable.program_version,
            program_sha256=stable.program_sha256,
            candidate_shas=(),
            image_digest="sha256:register-f2p",
            runtime_revision="register-f2p",
            now_ms=NOW - 23 * 3_600_000,
        )
    _accepted_compilable_event(conn, stable=stable)
    development = asyncio.run(
        _datasets(conn, stable).freeze_dataset(
            DatasetSpec(role="development", window=ClosedWindow(from_ms=NOW - 6 * 3_600_000, to_ms=NOW))
        )
    )
    honest = _prompt_candidate(
        conn,
        development_sha=development.artifact_sha,
        stable=stable,
        patch=PromptPatchV1(
            event_semantics_instruction="A stale population must never be registered.",
            reader_card_instruction="Keep the mechanism concrete.",
        ),
    )
    stale = _prompt_candidate(
        conn,
        development_sha=development.artifact_sha,
        stable=stable,
        patch=honest.patch,
        objective_summary={**honest.objective_summary, "optimizer_case_root_sha256": "0" * 64},
    )
    candidate_path = tmp_path / "candidate.json"
    artifact_root = tmp_path / "program-artifacts"
    output_path = tmp_path / "registered.json"
    candidate_path.write_text(json.dumps(stale.model_dump(mode="json")), encoding="utf-8")
    rows_before = int(conn.execute("SELECT count(*) AS n FROM news_learning_artifacts").fetchone()["n"])
    connection_count = 0

    @contextmanager
    def registered_connection(_settings: Any):
        nonlocal connection_count
        connection_count += 1
        yield conn

    monkeypatch.setattr(news_commands, "load_settings", lambda **_kwargs: object())
    monkeypatch.setattr(learning_runtime, "active_arm_manifest", lambda _settings: stable)
    monkeypatch.setattr("tracefold.app.repository_session.postgres_connection", registered_connection)
    args = build_parser().parse_args(
        [
            "news",
            "release",
            "register",
            "--development",
            development.artifact_sha,
            "--candidate",
            str(candidate_path),
            "--artifact-root",
            str(artifact_root),
            "--out",
            str(output_path),
        ]
    )

    code, payload = news_commands._handle_learning(args)

    assert code == 2
    assert payload["error"] == "news_learning_proposal_optimizer_population_unverified"
    assert connection_count == 1  # Registration failed before opening another database session.
    assert int(conn.execute("SELECT count(*) AS n FROM news_learning_artifacts").fetchone()["n"]) == rows_before
    assert not artifact_root.exists()
    assert not output_path.exists()


def test_a_candidate_cannot_declare_an_objective_the_corpus_does_not_support(conn) -> None:
    """#199: the release gate rebuilds the Objective Plan and holds the candidate to it, exactly.

    All three are the same defect wearing different clothes — a candidate optimized against a corpus other
    than the one it names. Before the plan there was only a subset check against an owner-blind guess, so
    a candidate could declare any cluster whose review mentioned a failure, whoever owned it.
    """

    _accepted_compilable_event(conn)
    stable = _arm()
    development = asyncio.run(
        _datasets(conn, stable).freeze_dataset(
            DatasetSpec(role="development", window=ClosedWindow(from_ms=NOW - 6 * 3_600_000, to_ms=NOW))
        )
    )
    plan = _objective_plan(conn, stable=stable, development_sha=development.artifact_sha)
    assert plan.split is not None and len(plan.target_failure_cluster_ids) == 2

    honest_summary = dict(
        _prompt_candidate(
            conn,
            development_sha=development.artifact_sha,
            stable=stable,
            patch=PromptPatchV1(
                event_semantics_instruction="Objective tamper baseline.",
                reader_card_instruction="Keep the mechanism concrete.",
            ),
        ).objective_summary
    )
    tampered_split = _prompt_candidate(
        conn,
        development_sha=development.artifact_sha,
        stable=stable,
        patch=PromptPatchV1(
            event_semantics_instruction="A split this corpus never produced.",
            reader_card_instruction="Keep the mechanism concrete.",
        ),
        objective_summary={
            **honest_summary,
            "split": {**dict(honest_summary["split"]), "train": {"cluster_root_sha256": "0" * 64}},
        },
    )
    legacy_summary = {key: value for key, value in honest_summary.items() if key != "plan_schema"}
    legacy_summary["schema"] = "tracefold.news.optimization_objective_summary.v1"
    legacy_objective = _prompt_candidate(
        conn,
        development_sha=development.artifact_sha,
        stable=stable,
        patch=PromptPatchV1(
            event_semantics_instruction="A legacy objective identity cannot be re-armed.",
            reader_card_instruction="Keep the mechanism concrete.",
        ),
        objective_summary=legacy_summary,
    )
    tampered_population = _prompt_candidate(
        conn,
        development_sha=development.artifact_sha,
        stable=stable,
        patch=PromptPatchV1(
            event_semantics_instruction="A representative root this corpus never produced.",
            reader_card_instruction="Keep the mechanism concrete.",
        ),
        objective_summary={**honest_summary, "optimizer_case_root_sha256": "0" * 64},
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
            "news_learning_proposal_objective_schema_unverified",
            {"prompt": legacy_objective},
        ),
        (
            "news_learning_proposal_optimizer_population_unverified",
            {"prompt": tampered_population},
        ),
        (
            "news_learning_proposal_split_roots_unverified",
            {"prompt": tampered_split},
        ),
    ]
    for error, overrides in cases:
        # The Program identity is derived from the registered patch, so each case gets its own write-set
        # rather than a forged SHA — a forged one would trip the identity check before the objective one.
        candidate = _program_candidate(
            conn,
            stable=stable,
            development_sha=development.artifact_sha,
            cluster_id=development.cases[0].cluster_id,
            variant=f" ({error})",
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
        bootstrap._datasets.freeze_dataset(
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

    regression_gates = report.evidence["regression_gates"]
    assert set(regression_gates) == {"production_action", "asset_grounding", "novelty", "trade_relevance"}
    assert {gate["gate"] for gate in regression_gates.values()} == set(regression_gates)
    assert {gate["metric_sha256"] for gate in regression_gates.values()} == {report.evidence["metric_sha256"]}

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
    assert all(row["request"]["schema"] == "tracefold.news.lm_request.v1" for row in recordings)
    assert all(row["request"]["model"] == "fixture-model" for row in recordings)
    # Logical Program/arm metadata stays in columns and hashes, never in the
    # exact provider request replay payload.
    assert all("program_sha256" not in row["request"] for row in recordings)
    assert all(row["response"]["schema"] == "tracefold.news.recorded_lm.v1" for row in recordings)
    assert all((row["response"]["response"] is None) != (row["response"]["error"] is None) for row in recordings)
    candidate_artifact = conn.execute(
        "SELECT payload FROM news_learning_artifacts WHERE kind = 'candidate' AND payload->>'candidate_sha' = %s",
        (candidate.candidate_sha,),
    ).fetchone()["payload"]
    assert candidate_artifact["exact_diff"] == {
        "candidate_kind": "prompt",
        "changed_fields": ["program_sha256"],
        "stable_bundle_sha": stable.bundle_sha,
        "candidate_bundle_sha": candidate.candidate_arm.bundle_sha,
        "stable_program_version": stable.program_version,
        "candidate_program_version": candidate.candidate_arm.program_version,
        "stable_program_sha256": stable.program_sha256,
        "candidate_program_sha256": candidate.candidate_arm.program_sha256,
        "prompt_candidate_sha256": candidate.proposal_receipt.prompt_candidate_sha256,
    }


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
            "dedupe_family": "earnings",
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
    replayed_card = json.loads(recordings[1]["response"]["response"]["text"])["card"]
    assert replayed_card["headline_zh"] == "DRAM 合约价续涨"
    assert conn.execute("SELECT count(*) AS n FROM news_learning_cases").fetchone()["n"] == 0


def test_every_physical_terminal_persists_a_complete_replay_document(conn) -> None:
    stable = _arm()
    evaluator = CandidateEvaluator(conn, stable=stable, judges={})
    context = TriageContext.from_card(
        {
            "event_id": "ev-terminal-recordings",
            "evidence_version": 1,
            "evidence_sha256": "a" * 64,
            "focus_fact_id": "fact-terminal-recordings",
            "leader_title": "Terminal recording fixture",
            "opened_at_ms": NOW - 3_600_000,
            "member_count": 1,
            "dedupe_family": "general",
            "queue_priority": "normal",
            "asset_class": "none",
            "storyline_key": "topic:recording",
        },
        watchlist=(),
        told_rows=(),
        now_ms=NOW,
        queue_lag_ms=0,
    )
    call = _trace(stable, context, _verdict()).calls[0]
    assert call.recording is not None
    run_sha = _sha("terminal-recording-run")
    terminals: dict[str, dict[str, Any]] = {}
    for name in ("success", "truncation", "schema_invalid"):
        terminal = json.loads(json.dumps(call.recording))
        if name == "truncation":
            terminal["response"].update(truncated=True, finish_reason="length")
        elif name == "schema_invalid":
            terminal["response"]["text"] = "not-json"
        terminals[name] = terminal
    for name, error in {
        "429": ("LMRateLimitError", 429),
        "503": ("LMServerError", 503),
        "timeout": ("LMTimeoutError", None),
    }.items():
        terminal = json.loads(json.dumps(call.recording))
        terminal["response"] = None
        terminal["error"] = {
            "type": error[0],
            "message": name,
            "code": name,
            "model": "fixture-model",
            "provider": "fixture-provider",
            "provider_code": None,
            "status": error[1],
            "retry_after": None,
        }
        terminals[name] = terminal

    for call_index, (name, terminal) in enumerate(terminals.items()):
        evaluator._persist_program_call(
            run_sha=run_sha,
            case_id=_sha({"terminal": name}),
            arm_name="stable",
            trial=1,
            arm=stable,
            trace={"envelope_sha256": EXECUTION_ENVELOPE_SHA256},
            call_index=call_index,
            raw_call=call.model_dump(mode="json"),
            recording=terminal,
        )

    rows = conn.execute(
        "SELECT request_sha256, response_sha256, request, response FROM news_model_recordings "
        "WHERE run_sha = %s ORDER BY call_index",
        (run_sha,),
    ).fetchall()
    assert len(rows) == len(terminals) == 6
    assert all(row["response_sha256"] for row in rows)
    assert all(row["response"]["schema"] == "tracefold.news.recorded_lm.v1" for row in rows)
    assert all(row["request"] == row["response"]["request"] for row in rows)
    assert all(row["request_sha256"] == row["response"]["request_sha256"] for row in rows)


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
            "dedupe_family": "earnings",
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
        evaluator._datasets.freeze_dataset(
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
        first._datasets.freeze_dataset(
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
        bootstrap._datasets.freeze_dataset(
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
        bootstrap._datasets.freeze_dataset(
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
    assert report.evidence["provider_cost_observation_incomplete_arms"] == ["candidate"]


def test_symmetric_provider_cost_blindness_is_not_a_release_blocker(conn) -> None:
    """#292: neither endpoint this deployment runs on reports a resolvable price.

    The gate is a delta, so two equally blind arms lose no comparative information and the token
    guardrail bounds the same spend concern. What stays a blocker is asymmetry — one priced arm and one
    blind arm cannot be compared honestly, and the test above pins that half.
    """

    _accepted_compilable_event(conn)
    stable = _arm()
    bootstrap = CandidateEvaluator(conn, stable=stable, judges={})
    development = asyncio.run(
        bootstrap._datasets.freeze_dataset(
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
        ("stable", stable.bundle_sha): _MissingProviderCostJudge(stable),
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

    assert "provider_cost_observation_incomplete" not in report.evidence["blockers"]
    assert report.evidence["provider_cost_observation_complete"] is False
    assert report.evidence["provider_cost_observation_incomplete_arms"] == ["candidate", "stable"]
    assert report.evidence["provider_cost_symmetrically_unobservable"] is True
    assert report.evidence["stable_mean_provider_cost_microusd"] is None
    assert report.evidence["candidate_mean_provider_cost_microusd"] is None


def test_partial_provider_cost_blindness_on_both_arms_still_blocks(conn) -> None:
    """#292 exempts *total* symmetric blindness only.

    Both arms here price every case but their first: the cost-mean guardrail would compare the two arms
    over silently different call subsets, so this must stay an evidence gap even though the incomplete-arm
    set looks symmetric.
    """

    _accepted_compilable_event(conn)
    stable = _arm()
    bootstrap = CandidateEvaluator(conn, stable=stable, judges={})
    development = asyncio.run(
        bootstrap._datasets.freeze_dataset(
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
        ("stable", stable.bundle_sha): _FirstCaseMissingProviderCostJudge(stable),
        ("candidate", candidate.candidate_arm.bundle_sha): _FirstCaseMissingProviderCostJudge(
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

    assert "provider_cost_observation_incomplete" in report.evidence["blockers"]
    assert report.evidence["provider_cost_observation_incomplete_arms"] == ["candidate", "stable"]
    assert report.evidence["provider_cost_symmetrically_unobservable"] is False


def test_mixed_call_pricing_inside_every_observation_still_blocks(conn) -> None:
    """#292 total blindness is a call-level fact, not an aggregate one.

    Here every observation on both arms is incomplete (an unpriced primary beside a priced fallback), so
    observation-level accounting alone would read the arms as symmetrically blind. Real prices exist, so
    the honest verdict is partial observability — the operator should finish pricing the endpoint, not
    receive an exemption.
    """

    _accepted_compilable_event(conn)
    stable = _arm()
    bootstrap = CandidateEvaluator(conn, stable=stable, judges={})
    development = asyncio.run(
        bootstrap._datasets.freeze_dataset(
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
        ("stable", stable.bundle_sha): _MixedCallPricingJudge(stable),
        ("candidate", candidate.candidate_arm.bundle_sha): _MixedCallPricingJudge(
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

    assert "provider_cost_observation_incomplete" in report.evidence["blockers"]
    assert report.evidence["provider_cost_observation_incomplete_arms"] == ["candidate", "stable"]
    assert report.evidence["provider_cost_symmetrically_unobservable"] is False


def test_errored_pair_never_scores_the_blind_pairwise_primary(conn) -> None:
    """#294: a card against an errored arm's absence is execution evidence, not preference evidence.

    Without this, every below-cap stable failure would hand the candidate a near-automatic blind win, and
    at holdout 5% of gifted clusters can push `interval_95` past zero. The errored pairs also leave
    `planned_cluster_n`, so an unresolvable cluster cannot hold resolution hostage.
    """

    _accepted_compilable_event(conn)
    stable = _arm()
    bootstrap = CandidateEvaluator(conn, stable=stable, judges={})
    development = asyncio.run(
        bootstrap._datasets.freeze_dataset(
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
        judges={
            ("stable", stable.bundle_sha): _AlwaysUnavailableJudge(stable),
            ("candidate", candidate.candidate_arm.bundle_sha): _StaticJudge(candidate.candidate_arm, candidate=True),
        },
        candidate_catalog=(candidate,),
    )
    request = EvaluationRequest(
        development_dataset_sha=development.artifact_sha,
        candidate_sha=candidate.candidate_sha,
        stage="offline",
    )
    first = asyncio.run(evaluator.evaluate(request))
    assert "stable_or_common_execution_unavailable" in first.evidence["blockers"]
    assert first.evidence["primary"]["planned_cluster_n"] == 0

    desk = ReviewDesk(conn, now_ms=NOW)
    tasks = desk.open(DeskQuery(mode="pairwise"), principal=PRINCIPAL)["tasks"]
    if tasks:
        # The queue may still surface the pair; an accepted candidate-preference on it must not count.
        task = tasks[0]
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
                    evidence_refs=[f"output:{candidate_side}"],
                ),
                principal=PRINCIPAL,
                idempotency_key=str(uuid.uuid4()),
            )

    report = asyncio.run(evaluator.evaluate(request))
    primary = report.evidence["primary"]
    assert primary["planned_cluster_n"] == 0
    assert primary["resolved_cluster_n"] == 0
    assert primary["candidate_win_n"] == 0
    assert primary["net_preference"] is None


def test_one_calendar_day_is_not_a_release_blocker_but_thin_coverage_still_is(conn) -> None:
    """#259: the development gate reads coverage; the calendar is a diagnostic beside it.

    This corpus lives inside a single UTC date — as almost every freeze does, since a frozen dataset only
    admits cases from the *active* Stable bundle and a bundle deployed this morning has no yesterday. The
    old profile turned that into `development_natural_day_n_insufficient` and made every Stable iteration
    wait for midnights it had no way to produce. What refuses this corpus now is what was always wrong
    with it: three accepted reviews cannot carry 30 boundary and 100 retention clusters.

    Both halves are asserted on purpose. Dropping the calendar row without keeping the cluster rows would
    not be this Issue, it would be deleting the gate.
    """

    _accepted_compilable_event(conn)
    stable = _arm()
    bootstrap = CandidateEvaluator(conn, stable=stable, judges={})
    development = asyncio.run(
        bootstrap._datasets.freeze_dataset(
            DatasetSpec(
                role="development",
                window=ClosedWindow(from_ms=NOW - 6 * 3_600_000, to_ms=NOW),
            )
        )
    )
    # The two diagnostics survive the cut: an operator still has to be able to see that this corpus is six
    # hours of one day, they just cannot be refused for it.
    assert development.counts["natural_day_n"] == 1
    assert development.counts["window_duration_hours"] == 6.0

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

    report = asyncio.run(
        evaluator.evaluate(
            EvaluationRequest(
                development_dataset_sha=development.artifact_sha,
                candidate_sha=candidate.candidate_sha,
                stage="offline",
            )
        )
    )

    blockers = set(report.evidence["blockers"])
    assert not any("natural_day" in blocker for blocker in blockers)
    assert not any(blocker.endswith(("_age_days_insufficient", "_stable_age_insufficient")) for blocker in blockers)
    assert {
        "development_boundary_cluster_n_insufficient",
        "development_retention_cluster_n_insufficient",
        "development_negative_cluster_n_insufficient",
    } <= blockers
    assert report.gate_outcome == "unknown"


def test_blind_candidate_critical_error_is_a_release_failure(conn) -> None:
    _accepted_compilable_event(conn)
    stable = _arm()
    bootstrap = CandidateEvaluator(conn, stable=stable, judges={})
    development = asyncio.run(
        bootstrap._datasets.freeze_dataset(
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
        bootstrap._datasets.freeze_dataset(
            DatasetSpec(
                role="development",
                window=ClosedWindow(from_ms=NOW - 6 * 3_600_000, to_ms=NOW),
            )
        )
    )
    assert development.counts["independent_cluster_n"] == 30 + len(_COMPILABLE_CORPUS)
    cases_by_id = {case.case_id: case for case in development.cases}
    episodes = bootstrap._datasets.development_compile_episodes(development.artifact_sha)
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
        bootstrap._datasets.freeze_dataset(
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
        bootstrap._datasets.freeze_dataset(
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
    # Admitted first, the way `learning freeze --role validation` does it: the release plane produces the
    # `AdmittedCandidate`, and only then does the freeze get to refuse the *window*. Before #202 both
    # checks lived inside `freeze_dataset`, which is what made it reach back into candidate validation.
    admitted = evaluator._registry.admit_for_validation(candidate.candidate_sha)
    with pytest.raises(ValueError, match="news_learning_holdout_precedes_candidate_registration"):
        asyncio.run(
            evaluator._datasets.freeze_dataset(
                admitted=admitted,
                spec=DatasetSpec(
                    role="validation",
                    observation_ref=candidate.candidate_sha,
                    window=ClosedWindow(from_ms=NOW - 6 * 3_600_000, to_ms=NOW),
                ),
            )
        )
    assert _judge_call_count(judges) == 0


def test_holdout_cannot_spend_model_budget_before_offline_pass(conn) -> None:
    _accepted_compilable_event(conn)
    stable = _arm()
    bootstrap = CandidateEvaluator(conn, stable=stable, judges={})
    development = asyncio.run(
        bootstrap._datasets.freeze_dataset(
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
    bootstrap = CandidateEvaluator(conn, stable=stable, judges={})
    development = asyncio.run(
        bootstrap._datasets.freeze_dataset(
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
    _accepted_compilable_event(conn)
    stable = _arm()
    bootstrap = CandidateEvaluator(conn, stable=stable, judges={})
    development = asyncio.run(
        bootstrap._datasets.freeze_dataset(
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
    # Canary observations are future evidence, not a mutation of a development case. Reusing the focal
    # frozen Event here used to overwrite its Stable verdict and correctly invalidated the exact dataset pin.
    event_id = _open_event(
        conn,
        hit_id=992001,
        title="A future canary Event receives one durable arm assignment",
        bundle_sha=stable.bundle_sha,
        program_version=stable.program_version,
        program_sha256=stable.program_sha256,
        published_at_ms=NOW - 3_500_000,
    )
    with repos.transaction():
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


def test_an_epoch_label_claimed_by_another_bundle_fails_the_startup_barrier(conn) -> None:
    """The eight-hex label is an abbreviation, and an abbreviation can in principle collide.

    Left alone the collision is silent: the losing deployment starts, runs, and the freeze that needed its
    epoch fails hours later as `news_learning_epoch_not_deployed` — a message about the wrong thing. The
    barrier reads back what its insert lost to and refuses instead.
    """

    stable = _arm()
    impostor = "f" * 8 + "0" * 56
    repositories = repositories_for_connection(conn)
    with repositories.transaction():
        assert (
            repositories.news.open_learning_epoch(
                bundle_sha=impostor,
                envelope_sha256=EXECUTION_ENVELOPE_SHA256,
                artifact_schema_version=PROGRAM_SCHEMA_VERSION,
                program_version=stable.program_version,
                program_sha256=stable.program_sha256,
                now_ms=NOW,
            )
            is True
        )

    colliding = impostor[:8] + "1" * 56
    assert epoch_id_for_bundle(colliding) == epoch_id_for_bundle(impostor)
    with pytest.raises(ValueError, match="news_learning_epoch_id_collision"), repositories.transaction():
        repositories.news.open_learning_epoch(
            bundle_sha=colliding,
            envelope_sha256=EXECUTION_ENVELOPE_SHA256,
            artifact_schema_version=PROGRAM_SCHEMA_VERSION,
            program_version=stable.program_version,
            program_sha256=stable.program_sha256,
            now_ms=NOW + 1,
        )
