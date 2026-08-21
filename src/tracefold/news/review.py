"""The operator-facing ReviewDesk deep module for News learning (#112).

Callers see virtual review tasks, evidence views, and append-only receipts.
They do not know how Events, verdicts, delivery truth, sampling strata, or
accepted corrections are joined.  Ordinary Event tasks are deterministic and
content-addressed; opening a queue never writes.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .artifact_identity import canonical_json, canonical_sha
from .outcome import decision_zh
from .price_repository import MarketReviewCohort, PriceRepository

REVIEW_RUBRIC_VERSION = "news_review_v2"
READER_CONTRACT_VERSION = "reader_contract_v2"
# This is product truth, not prompt advice.  v2 is the operator-approved
# no-quota contract: a distinct fact that satisfies push/escalate reaches
# delivery regardless of prior card volume.  Changing this text requires a new
# version and invalidates old development/validation manifests.
READER_CONTRACT_TEXT = (
    "Audience: Chinese market-research operator.\n"
    "Coverage: crypto; global macro/geopolitics with broad risk-asset impact; US-listed securities/ADRs; "
    "watchlist names.\n"
    "Single-name boundary: a non-US unlisted/private name is held unless it is a systemic sector or macro fact.\n"
    "Delivery: every distinct fact satisfying push or escalate proceeds to delivery; prior 1h/2h/4h card counts "
    "never veto it.\n"
    "Duplicate evidence: a normal push may be held only when the sent-reader ledger shows the same fact; reversal, "
    "escalate and degraded fallback are exempt.\n"
    "Market reaction: post-event price is discovery evidence, never reward, causality, or should-push truth.\n"
)
READER_CONTRACT_SHA256 = hashlib.sha256(READER_CONTRACT_TEXT.encode()).hexdigest()
REVIEW_TASK_VERSION = "news_review_task_v1"
REVIEW_QUEUE_MAX = 100
REVIEW_BODY_TEXT_MAX = 20_000
REVIEW_HIGH_REACTION_DISCOVERY_BPS = 300
REVIEW_MARKET_MAX_HOURS = 168

ShouldPush = Literal["must_push", "should_push", "should_hold", "must_hold", "uncertain"]
DimensionResult = Literal["pass", "fail", "uncertain", "not_applicable"]
FirstBadOwner = Literal[
    "receiver",
    "deduper",
    "event_evidence",
    "gate",
    "retrieval",
    "storyline",
    "triage_prompt",
    "model",
    "policy",
    "delivery",
    "taxonomy",
    "unknown",
]
EvidenceRef = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]

_DIMENSIONS = {
    "factual_fidelity",
    "headline_fidelity",
    "asset_grounding",
    "direction",
    "magnitude",
    "why_support",
    "why_value",
    "timeliness",
}
_NOVELTY = {"new_fact", "progression", "restatement", "uncertain"}
_OWNER_BY_DIMENSION: dict[str, FirstBadOwner] = {
    "asset_grounding": "gate",
    "timeliness": "delivery",
    "direction": "triage_prompt",
    "magnitude": "triage_prompt",
    "factual_fidelity": "triage_prompt",
    "headline_fidelity": "triage_prompt",
    "why_support": "triage_prompt",
    "why_value": "triage_prompt",
}

_STRATUM_ZH = {
    "delivery_ambiguous": "送达状态未知",
    "delivery_failed": "送达明确失败",
    "critical": "重点事件",
    "throttled": "历史拦截或同事实重复",
    "gate_suppress": "入口被拦截",
    "model_drop": "模型判断不推",
    "delivered": "已送达抽样",
    "high_reaction": "高波动发现样本（非成绩）",
    "random_control": "随机对照",
    "eventless_miss": "系统外漏报",
    "blind_pairwise": "匿名候选对比",
    "development_pairwise": "开发集候选对比",
}
_SELECTION_REASON_ZH = {
    "delivery_truth_unknown": "投递结果无法确定",
    "delivery_terminal_failure": "投递已明确失败",
    "high_priority_or_escalate": "高优先级或重点推送",
    "duplicate_or_historical_throttle": "同事实重复或历史版本数量拦截",
    "sent_quality_sample": "从真实送达中抽样",
    "market_discovery_only": "仅因事后波动进入发现队列",
    "semantic_or_policy_hold": "语义或策略判断不送达",
    "upstream_recall_sample": "入口召回抽样",
    "coverage_control": "随机覆盖对照",
}
_RELEASE_STAGE_ZH = {
    "offline": "离线开发集",
    "holdout": "未来留出集",
    "shadow": "影子运行",
    "canary": "小流量上线",
}
_RELEASE_OUTCOME_ZH = {"pass": "通过", "fail": "失败", "unknown": "证据不足"}
_PROPOSAL_STATUS_ZH = {
    "audit_only": "仅供历史审计",
    "proposed": "已提案",
    "evaluating": "评估中",
    "review_required": "需要更多证据",
    "rejected": "已拒绝",
    "shadow_ready": "可进入影子运行",
    "canary_ready": "可进入小流量上线",
    "canary": "小流量运行中",
    "canary_closed": "小流量已关闭",
    "promotion_ready": "等待人工发布",
    "rolled_back": "已回滚",
}
_TARGET_ZH = {"prompt": "提示词（历史审计）", "program": "DSPy Program", "policy": "确定性策略"}
_DIMENSION_ZH = {
    "should_push": "是否应送达",
    "asset_grounding": "标的对应",
    "direction": "方向与机制",
    "factual_fidelity": "事实忠实",
    "headline_fidelity": "标题忠实",
    "magnitude": "重要程度",
    "timeliness": "送达时效",
    "why_support": "Why 证据支持",
    "why_value": "Why 读者价值",
    "novelty": "新颖性",
}
_RELEASE_CODE_ZH = {
    "active_stable_changed": "运行期间稳定版已变化",
    "development_safety_empty": "开发集缺少安全样本",
    "development_pairwise_review_empty": "开发集匿名对比尚无人工判断",
    "development_pairwise_review_incomplete": "开发集匿名对比尚未完成",
    "development_target_improvement_not_observed": "开发集尚未观察到目标改善",
    "development_pairwise_regression": "开发集匿名对比出现回归",
    "must_push_regression": "候选漏掉必须送达的事实",
    "candidate_schema_or_provider_regression": "候选出现新增结构或调用错误",
    "candidate_token_cost_regression": "候选模型成本超出边界",
    "candidate_latency_slo_regression": "候选延迟超出边界",
    "candidate_schema_contract_breach": "候选违反输出结构合同",
    "candidate_degraded_or_error_slo_regression": "候选降级或错误率超出边界",
    "candidate_critical_error_regression": "候选新增关键质量错误",
    "stable_or_common_execution_unavailable": "稳定版或共同模型调用不可用",
    "validation_duration_insufficient": "未来留出集时间不足",
    "validation_eligible_events_insufficient": "未来留出集事件数不足",
    "validation_primary_review_insufficient": "未来留出集独立事实簇不足",
    "validation_primary_review_incomplete": "未来留出集匿名判断未完成",
    "validation_review_budget_exhausted": "人工判断预算已用完但结论仍不确定",
    "validation_primary_interval_crosses_zero": "改善区间跨过零，无法证明提升",
    "shadow_duration_insufficient": "影子运行时间不足",
    "shadow_observations_empty": "影子运行没有观测",
    "canary_duration_insufficient": "小流量运行时间不足",
    "canary_observations_empty": "小流量运行没有观测",
    "canary_candidate_assignment_n_insufficient": "候选分臂样本不足",
    "canary_one_arm_assignment_invariant_breach": "一个事件被错误分到多个分臂",
}


class Principal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    subject: str = Field(min_length=1, max_length=64)
    can_review: bool = True


class DeskQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    view: Literal["queue", "coverage", "proposals", "market"] = "queue"
    mode: Literal["event", "pairwise"] = "event"
    cohort: str = Field(default="", max_length=160)
    stratum: str = Field(default="", max_length=64)
    proposal: str = Field(default="", max_length=128)
    task: str = Field(default="", max_length=300)
    event: str = Field(default="", max_length=128)
    status: str = Field(default="pending", max_length=32)
    hours: int = Field(default=24, ge=1, le=720)
    limit: int = Field(default=30, ge=1, le=REVIEW_QUEUE_MAX)
    cursor: str = Field(default="", max_length=300)


class TaskRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(min_length=1, max_length=300)
    task_version: str = Field(pattern=r"^[0-9a-f]{64}$")


class NoveltyJudgment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    judgment: Literal["new_fact", "progression", "restatement", "uncertain"]
    duplicate_of: str = Field(default="", max_length=128)

    @model_validator(mode="after")
    def require_duplicate_for_restatement(self) -> NoveltyJudgment:
        if self.judgment == "restatement" and not self.duplicate_of.strip():
            raise ValueError("news_review_duplicate_of_required")
        if self.judgment != "restatement" and self.duplicate_of.strip():
            raise ValueError("news_review_duplicate_of_not_allowed")
        return self


class EventRubricSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["event_rubric"] = "event_rubric"
    should_push: ShouldPush
    dimensions: dict[str, DimensionResult]
    novelty: NoveltyJudgment
    first_bad_owner: FirstBadOwner | None = None
    evidence_refs: list[EvidenceRef] = Field(default_factory=list, max_length=32)
    expected_correction: str = Field(default="", max_length=2_000)
    note: str = Field(default="", max_length=2_000)

    @model_validator(mode="after")
    def validate_rubric(self) -> EventRubricSubmission:
        unknown = set(self.dimensions) - _DIMENSIONS
        if unknown:
            raise ValueError(f"news_review_dimension_unknown:{sorted(unknown)[0]}")
        if "factual_fidelity" not in self.dimensions:
            raise ValueError("news_review_factual_fidelity_required")
        if self.should_push in {"must_push", "should_push"} and "timeliness" not in self.dimensions:
            raise ValueError("news_review_timeliness_required_for_push")
        if any(value == "fail" for value in self.dimensions.values()) and not self.evidence_refs:
            raise ValueError("news_review_fail_evidence_ref_required")
        return self


class BlindPairwiseSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["blind_pairwise"] = "blind_pairwise"
    preference: Literal["A", "B", "tie", "both_bad", "uncertain"]
    critical_errors: list[
        Literal[
            "A:unsupported_fact",
            "A:wrong_entity",
            "A:wrong_direction",
            "A:missed_key_fact",
            "A:near_duplicate",
            "A:injection_obedience",
            "B:unsupported_fact",
            "B:wrong_entity",
            "B:wrong_direction",
            "B:missed_key_fact",
            "B:near_duplicate",
            "B:injection_obedience",
        ]
    ] = Field(default_factory=list, max_length=12)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list, max_length=32)
    note: str = Field(default="", max_length=2_000)

    @model_validator(mode="after")
    def validate_critical_errors(self) -> BlindPairwiseSubmission:
        if len(set(self.critical_errors)) != len(self.critical_errors):
            raise ValueError("news_review_duplicate_critical_error")
        if self.critical_errors and not self.evidence_refs:
            raise ValueError("news_review_critical_error_evidence_ref_required")
        return self


class ExternalMissSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["external_miss"] = "external_miss"
    source_url: str = Field(min_length=1, max_length=2_000)
    title: str = Field(min_length=1, max_length=1_000)
    body: str = Field(default="", max_length=REVIEW_BODY_TEXT_MAX)
    occurred_at_ms: int = Field(ge=0)
    rubric: EventRubricSubmission


ReviewSubmission = EventRubricSubmission | BlindPairwiseSubmission | ExternalMissSubmission


@dataclass(frozen=True, slots=True)
class _VirtualTask:
    task_id: str
    task_version: str
    row: Mapping[str, Any]
    selection: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ReviewReadStatement:
    """One bounded ReviewDesk read shared by serving and query audit."""

    name: str
    sql: str
    params: tuple[Any, ...]


def _event_queue_statement(
    *,
    lower_ms: int,
    upper_ms: int,
    cohort_sha: str,
    cursor: tuple[int, str] | None,
    limit: int,
) -> ReviewReadStatement:
    filters = [
        "opened_at_ms >= %s",
        "opened_at_ms < %s",
        "ingest_mode = 'live'",
        "COALESCE(trace #>> '{agent_assignment,bundle_sha}', '') = %s",
    ]
    params: list[Any] = [int(lower_ms), int(upper_ms), cohort_sha]
    if cursor is not None:
        filters.append("(opened_at_ms, event_id) < (%s, %s)")
        params.extend(cursor)
    params.append(int(limit))
    return ReviewReadStatement(
        name="news_review_task_queue",
        sql=f"""
            SELECT * FROM news_review_task_source_v1
             WHERE {" AND ".join(filters)}
             ORDER BY opened_at_ms DESC, event_id DESC
             LIMIT %s
        """,
        params=tuple(params),
    )


def _event_task_statement(event_id: str, *, evidence_version: int | None) -> ReviewReadStatement:
    if evidence_version is None:
        return ReviewReadStatement(
            name="news_review_task_evidence",
            sql=("SELECT * FROM news_review_task_source_v1 WHERE event_id = %s ORDER BY evidence_version DESC LIMIT 1"),
            params=(event_id,),
        )
    return ReviewReadStatement(
        name="news_review_task_evidence_version",
        sql="SELECT * FROM news_review_task_source_v1 WHERE event_id = %s AND evidence_version = %s",
        params=(event_id, int(evidence_version)),
    )


def _coverage_statement(*, lower_ms: int, upper_ms: int, learning_epoch: str) -> ReviewReadStatement:
    return ReviewReadStatement(
        name="news_review_coverage_source",
        sql="""
            WITH current_epoch AS (
              SELECT starts_at_ms
                FROM news_learning_epochs
               WHERE epoch_id = %s
            ),
            active_agent AS (
              SELECT stable_sha
                FROM news_review_active_agent_v1
               ORDER BY created_at_ms DESC
               LIMIT 1
            )
            SELECT source.*
              FROM news_review_task_source_v1 source
              JOIN current_epoch ON true
              JOIN active_agent ON true
             WHERE source.opened_at_ms >= greatest(%s, current_epoch.starts_at_ms)
               AND source.opened_at_ms < %s
               AND source.ingest_mode = 'live'
               AND COALESCE(source.trace #>> '{agent_assignment,bundle_sha}', '') = active_agent.stable_sha
        """,
        params=(learning_epoch, int(lower_ms), int(upper_ms)),
    )


def _current_learning_epoch() -> str:
    # CandidateEvaluator imports the reader/rubric contract from this module,
    # so resolve its epoch lazily rather than creating an import cycle.
    from .candidate_evaluator import LEARNING_EPOCH

    return LEARNING_EPOCH


def _pairwise_queue_statement(
    *,
    proposal: str,
    status: str,
    learning_epoch: str,
    cursor: tuple[int, int, str] | None,
    limit: int,
) -> ReviewReadStatement:
    filters = ["true"]
    params: list[Any] = []
    if proposal:
        filters.append("c.run_sha = %s")
        params.append(proposal)
    if cursor is not None:
        filters.append(
            "(CASE WHEN c.dataset_role = 'validation' THEN 0 ELSE 1 END, c.created_at_ms, c.case_id) > (%s, %s, %s)"
        )
        params.extend(cursor)
    if status == "pending":
        filters.append("accepted_pair.review_id IS NULL")
        filters.append("dataset.payload ->> 'learning_epoch' = %s")
        params.append(learning_epoch)
    elif status == "accepted":
        filters.append("accepted_pair.review_id IS NOT NULL")
        filters.append("dataset.payload ->> 'learning_epoch' = %s")
        params.append(learning_epoch)
    elif status != "all":
        raise ValueError("news_review_status_invalid")
    params.append(int(limit) + 1)
    return ReviewReadStatement(
        name="news_review_pairwise_queue",
        sql=f"""
            WITH accepted_pair AS (
              SELECT DISTINCT ON (j.pairwise_case_id)
                     j.pairwise_case_id, j.review_id
                FROM news_review_records_v1 a
                JOIN news_review_records_v1 j ON j.review_id = a.accepts_review_id
               WHERE a.review_kind = 'acceptance' AND j.subject_kind = 'pairwise'
               ORDER BY j.pairwise_case_id, a.created_at_ms DESC, a.review_id DESC
            )
            SELECT c.*, accepted_pair.review_id AS accepted_review_id,
                   dataset.payload ->> 'learning_epoch' AS learning_epoch
              FROM news_review_pairwise_tasks_v1 c
              LEFT JOIN news_learning_artifacts dataset
                ON dataset.kind = 'dataset' AND dataset.artifact_sha = c.dataset_sha
              LEFT JOIN accepted_pair ON accepted_pair.pairwise_case_id = c.run_sha || ':' || c.case_id
             WHERE {" AND ".join(filters)}
             ORDER BY CASE WHEN c.dataset_role = 'validation' THEN 0 ELSE 1 END,
                      c.created_at_ms, c.case_id
             LIMIT %s
        """,
        params=tuple(params),
    )


def _proposal_candidates_statement(limit: int) -> ReviewReadStatement:
    return ReviewReadStatement(
        name="news_review_proposal_candidates",
        sql="""
            SELECT candidate.artifact_sha, candidate.parent_sha, candidate.payload,
                   candidate.created_at_ms, dataset.payload ->> 'learning_epoch' AS learning_epoch
              FROM news_learning_artifacts candidate
              LEFT JOIN news_learning_artifacts dataset
                ON dataset.kind = 'dataset'
               AND dataset.artifact_sha = candidate.payload #>> '{manifest,development_dataset_sha}'
             WHERE candidate.kind = 'candidate'
             ORDER BY candidate.created_at_ms DESC
             LIMIT %s
        """,
        params=(int(limit),),
    )


def _proposal_releases_statement() -> ReviewReadStatement:
    return ReviewReadStatement(
        name="news_review_proposal_releases",
        sql=(
            "SELECT artifact_sha, parent_sha, payload, created_at_ms "
            "FROM news_learning_artifacts WHERE kind = 'release_evidence' ORDER BY created_at_ms"
        ),
        params=(),
    )


def _proposal_reports_statement() -> ReviewReadStatement:
    return ReviewReadStatement(
        name="news_review_proposal_reports",
        sql=(
            "SELECT artifact_sha, parent_sha, payload, created_at_ms "
            "FROM news_learning_artifacts WHERE kind = 'evaluation_report'"
        ),
        params=(),
    )


def _proposal_activations_statement() -> ReviewReadStatement:
    return ReviewReadStatement(
        name="news_review_proposal_activations",
        sql="SELECT * FROM news_canary_activations ORDER BY created_at_ms",
        params=(),
    )


def _active_agent_statement() -> ReviewReadStatement:
    return ReviewReadStatement(
        name="news_review_active_agent",
        sql="SELECT stable_sha FROM news_review_active_agent_v1 ORDER BY created_at_ms DESC LIMIT 1",
        params=(),
    )


class ReviewDesk:
    """One narrow interface for HTTP, CLI, dataset freeze, and tests."""

    def __init__(self, conn: Any, *, now_ms: int | None = None) -> None:
        self._conn = conn
        self._now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)

    def open(self, query: DeskQuery, *, principal: Principal) -> dict[str, Any]:
        self._require_principal(principal)
        if query.view == "queue":
            return self._open_queue(query)
        if query.view == "coverage":
            return self._coverage(query)
        if query.view == "market":
            return self._market(query)
        return self._proposals(query)

    def evidence(self, task: TaskRef, *, principal: Principal) -> dict[str, Any]:
        self._require_principal(principal)
        if task.task_id.startswith("evt."):
            event_id, evidence_version = _parse_event_task_id(task.task_id)
            virtual = self._event_task(event_id, evidence_version=evidence_version)
            if virtual is None:
                raise ValueError("news_review_task_not_found")
            if virtual.task_version != task.task_version:
                raise ValueError("news_review_task_version_conflict")
            row = virtual.row
            accepted = self._latest_accepted(virtual)
            reactions = PriceRepository(self._conn).event_reactions(event_id)
            trace = dict(row.get("trace") or {})
            return {
                "task": _task_public(virtual, accepted=accepted),
                "disclosure": {
                    "outcome_revealed": True,
                    "pairing": "unpaired",
                    "dataset_role": "discovery",
                    "market_revealed": accepted is not None,
                },
                "evidence": row["evidence_snapshot"],
                "agent": {
                    "verdict": row.get("verdict"),
                    "final_decision": row.get("final_decision"),
                    "override_rule": row.get("override_rule"),
                    "throttled_by": row.get("throttled_by"),
                    "degraded": bool(row.get("degraded")),
                    "cohort": _cohort(row),
                    "agent_cohort": _agent_identity(row),
                    "trace": {
                        "input_sha256": trace.get("input_sha256"),
                        "input_text": trace.get("input_text"),
                        "told": trace.get("told") or [],
                        "status": trace.get("status") or {},
                        "policy": trace.get("policy") or {},
                        "agent_assignment": trace.get("agent_assignment") or {},
                    },
                    "verifier_flags": _verifier_flags(row),
                },
                "reader_receipt": _receipt_public(row),
                "market_reactions": reactions if accepted is not None else [],
                "accepted_review": accepted,
                "rubric": _rubric_contract(row),
                "versions": {
                    "rubric": REVIEW_RUBRIC_VERSION,
                    "reader_contract": READER_CONTRACT_VERSION,
                    "reader_contract_sha256": READER_CONTRACT_SHA256,
                    "evidence_sha256": row["evidence_sha256"],
                },
            }
        if task.task_id.startswith("pair."):
            virtual = self._pairwise_task(task.task_id)
            if virtual is None:
                raise ValueError("news_review_task_not_found")
            if virtual.task_version != task.task_version:
                raise ValueError("news_review_task_version_conflict")
            accepted = self._latest_accepted(virtual)
            reveal = self._pairwise_reveal(virtual, accepted=accepted)
            return _pairwise_evidence(
                virtual,
                source=self._pairwise_source(virtual.row),
                accepted=accepted,
                reveal=reveal,
            )
        raise ValueError("news_review_task_kind_unsupported")

    def submit(
        self,
        task: TaskRef | None,
        submission: ReviewSubmission,
        *,
        principal: Principal,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._require_principal(principal)
        key = _idempotency_key(idempotency_key)
        request_sha = _sha(
            {
                "task": task.model_dump(mode="json") if task is not None else None,
                "submission": submission.model_dump(mode="json"),
            }
        )
        existing = self._idempotent_receipt(principal.subject, key, request_sha=request_sha)
        if existing is not None:
            return existing
        if isinstance(submission, ExternalMissSubmission):
            if task is not None:
                raise ValueError("news_review_external_miss_task_not_allowed")
            return self._submit_external(
                submission,
                principal=principal,
                idempotency_key=key,
                idempotency_request_sha=request_sha,
            )
        if task is None:
            raise ValueError("news_review_task_required")
        if task.task_id.startswith("evt.") and isinstance(submission, EventRubricSubmission):
            return self._submit_event(
                task,
                submission,
                principal=principal,
                idempotency_key=key,
                idempotency_request_sha=request_sha,
            )
        if task.task_id.startswith("pair.") and isinstance(submission, BlindPairwiseSubmission):
            return self._submit_pairwise(
                task,
                submission,
                principal=principal,
                idempotency_key=key,
                idempotency_request_sha=request_sha,
            )
        raise ValueError("news_review_submission_kind_mismatch")

    def _open_queue(self, query: DeskQuery) -> dict[str, Any]:
        if query.task:
            if query.task.startswith("pair."):
                task = self._pairwise_task(query.task)
                accepted = None if task is None else self._latest_accepted(task)
                tasks = [] if task is None else [_pairwise_task_public(task, accepted=accepted)]
                return self._queue_response(query.model_copy(update={"mode": "pairwise"}), tasks, next_cursor=None)
            if query.task.startswith("evt."):
                event_id, evidence_version = _parse_event_task_id(query.task)
                task = self._event_task(event_id, evidence_version=evidence_version)
                accepted = None if task is None else self._latest_accepted(task)
                tasks = [] if task is None else [_task_public(task, accepted=accepted)]
                return self._queue_response(query.model_copy(update={"mode": "event"}), tasks, next_cursor=None)
            raise ValueError("news_review_task_id_invalid")
        if query.mode == "pairwise":
            return self._open_pairwise_queue(query)
        if query.event:
            task = self._event_task(query.event)
            single_tasks = [] if task is None else [_task_public(task, accepted=self._latest_accepted(task))]
            return self._queue_response(query, single_tasks, next_cursor=None)

        lower = self._now_ms - int(query.hours) * 3_600_000
        cohort_sha = _parse_agent_cohort_sha(query.cohort) if query.cohort else self._active_agent_cohort_sha()
        if cohort_sha is None:
            return self._queue_response(query, [], next_cursor=None)
        cursor = _decode_cursor(query.cursor) if query.cursor else None
        statement = _event_queue_statement(
            lower_ms=lower,
            upper_ms=self._now_ms,
            cohort_sha=cohort_sha,
            cursor=cursor,
            limit=min(2_000, query.limit * 50 + 100),
        )
        rows = self._conn.execute(statement.sql, statement.params).fetchall()
        accepted_by_task = self._accepted_event_tasks([str(row["event_id"]) for row in rows])
        ranked_tasks: list[tuple[int, _VirtualTask, dict[str, Any] | None]] = []
        for row in rows:
            task = _virtual_task(row)
            if not _sampler_selected(task):
                continue
            accepted = accepted_by_task.get((task.task_id, task.task_version))
            stratum = str(task.selection["stratum"])
            if query.stratum and stratum != query.stratum:
                continue
            if query.status == "pending" and accepted is not None:
                continue
            if query.status == "accepted" and accepted is None:
                continue
            ranked_tasks.append((_stratum_rank(stratum), task, accepted))
        ranked_tasks.sort(key=lambda item: (item[0], -int(item[1].row["opened_at_ms"]), item[1].task_id))
        page = ranked_tasks[: query.limit]
        public = [_task_public(task, accepted=accepted) for _, task, accepted in page]
        next_cursor = None
        if len(ranked_tasks) > query.limit and page:
            last = page[-1][1].row
            next_cursor = _encode_cursor(int(last["opened_at_ms"]), str(last["event_id"]))
        return self._queue_response(query, public, next_cursor=next_cursor)

    def _open_pairwise_queue(self, query: DeskQuery) -> dict[str, Any]:
        cursor = _decode_pairwise_cursor(query.cursor) if query.cursor else None
        statement = _pairwise_queue_statement(
            proposal=query.proposal,
            status=query.status,
            learning_epoch=_current_learning_epoch(),
            cursor=cursor,
            limit=query.limit,
        )
        rows = self._conn.execute(statement.sql, statement.params).fetchall()
        has_more = len(rows) > query.limit
        page = rows[: query.limit]
        tasks = []
        for row in page:
            virtual = _pairwise_virtual(row)
            accepted = self._latest_accepted(virtual) if row.get("accepted_review_id") is not None else None
            tasks.append(_pairwise_task_public(virtual, accepted=accepted))
        next_cursor = None
        if has_more and page:
            last = page[-1]
            next_cursor = _encode_pairwise_cursor(
                0 if last.get("dataset_role") == "validation" else 1,
                int(last["created_at_ms"]),
                str(last["case_id"]),
            )
        return {
            "view": "queue",
            "mode": "pairwise",
            "status": "ready" if tasks else "insufficient_evidence",
            "tasks": tasks,
            "next_cursor": next_cursor,
            "counts": {query.status: len(tasks)},
            "message_zh": None if tasks else "尚无 CandidateEvaluator 生成的盲测任务",
            "disclosure": {
                "outcome_revealed": False,
                "arm_identity_revealed": False,
                "pairing": "paired",
            },
        }

    def _proposals(self, query: DeskQuery) -> dict[str, Any]:
        current_epoch = _current_learning_epoch()
        candidate_statement = _proposal_candidates_statement(query.limit)
        candidates = self._conn.execute(candidate_statement.sql, candidate_statement.params).fetchall()
        release_statement = _proposal_releases_statement()
        releases = self._conn.execute(release_statement.sql, release_statement.params).fetchall()
        report_statement = _proposal_reports_statement()
        reports = {
            str(row["artifact_sha"]): dict(row)
            for row in self._conn.execute(report_statement.sql, report_statement.params).fetchall()
        }
        activation_statement = _proposal_activations_statement()
        activations = {
            str(row["candidate_manifest_sha"]): dict(row)
            for row in self._conn.execute(activation_statement.sql, activation_statement.params).fetchall()
        }
        public: list[dict[str, Any]] = []
        for row in candidates:
            payload = dict(row["payload"] or {})
            candidate_sha = str(payload.get("candidate_sha") or "")
            if query.proposal and candidate_sha != query.proposal:
                continue
            manifest = dict(payload.get("manifest") or {})
            receipt = dict(manifest.get("proposal_receipt") or {})
            candidate_arm = dict(manifest.get("candidate_arm") or {})
            learning_epoch = str(row.get("learning_epoch") or "") or None
            evidence_disposition = "current" if learning_epoch == current_epoch else "audit_only"
            timeline: list[dict[str, Any]] = []
            for release_row in releases:
                release = dict(release_row["payload"] or {})
                if str(release.get("candidate_sha") or "") != candidate_sha:
                    continue
                report = reports.get(str(release.get("report_sha") or ""), {})
                report_payload = dict(report.get("payload") or {})
                timeline.append(
                    {
                        "stage": release.get("stage"),
                        "stage_zh": _RELEASE_STAGE_ZH.get(str(release.get("stage") or ""), "未知阶段"),
                        "outcome": release.get("gate_outcome"),
                        "outcome_zh": _RELEASE_OUTCOME_ZH.get(str(release.get("gate_outcome") or ""), "证据状态未知"),
                        "report_sha": release.get("report_sha"),
                        "run_sha": release.get("run_sha"),
                        "recommended_action": report_payload.get("recommended_action"),
                        "evidence_disposition": evidence_disposition,
                        "blockers": (report_payload.get("evidence") or {}).get("blockers", []),
                        "blockers_zh": [
                            _release_code_zh(str(code))
                            for code in (report_payload.get("evidence") or {}).get("blockers", [])
                        ],
                        "failures": (report_payload.get("evidence") or {}).get("failures", []),
                        "failures_zh": [
                            _release_code_zh(str(code))
                            for code in (report_payload.get("evidence") or {}).get("failures", [])
                        ],
                        "created_at_ms": int(release_row["created_at_ms"]),
                    }
                )
            activation = activations.get(candidate_sha)
            status = _proposal_status(timeline, activation) if evidence_disposition == "current" else "audit_only"
            reveal_diff = self._candidate_diff_reveal_allowed(candidate_sha)
            public.append(
                {
                    "candidate_sha": candidate_sha,
                    "candidate_bundle_sha": candidate_arm.get("bundle_sha"),
                    "parent_stable_sha": manifest.get("parent_stable_sha"),
                    "target": manifest.get("target"),
                    "target_zh": _TARGET_ZH.get(str(manifest.get("target") or ""), "未知变更"),
                    "hypothesis": manifest.get("hypothesis"),
                    "target_dimensions": manifest.get("target_dimensions", []),
                    "target_dimensions_zh": [
                        _DIMENSION_ZH.get(str(value), "未识别维度") for value in manifest.get("target_dimensions", [])
                    ],
                    "failure_cluster_ids": receipt.get("failure_cluster_ids", []),
                    "guardrails": receipt.get("guardrails", []),
                    "development_dataset_sha": manifest.get("development_dataset_sha"),
                    "learning_epoch": learning_epoch,
                    "evidence_disposition": evidence_disposition,
                    "candidate_patch_sha": receipt.get("candidate_patch_sha"),
                    "exact_diff": payload.get("exact_diff") if reveal_diff else None,
                    "diff_withheld_reason": None if reveal_diff else "hidden_validation_in_progress",
                    "created_at_ms": int(row["created_at_ms"]),
                    "status": status,
                    "status_zh": _PROPOSAL_STATUS_ZH.get(status, "证据状态未知"),
                    "timeline": timeline,
                    "canary": activation,
                }
            )
        return {
            "view": "proposals",
            "status": "ready" if public else "insufficient_evidence",
            "proposals": public,
            "message_zh": None if public else "尚无已封存的候选评估",
        }

    def _pairwise_reveal(self, task: _VirtualTask, *, accepted: Mapping[str, Any] | None) -> dict[str, Any] | None:
        """Reveal a pair only after its disclosure contract permits it.

        Development cases reveal after acceptance so an operator can learn from
        the exact candidate delta.  Validation stays blind until the whole run
        is accepted and a later evaluator report re-seals those judgments.
        """

        if accepted is None:
            return None
        run_sha = str(task.row["run_sha"])
        if task.row.get("dataset_role") == "validation" and not self._pairwise_run_reveal_ready(run_sha):
            return None
        case = self._conn.execute(
            "SELECT comparison FROM news_learning_cases WHERE run_sha = %s AND case_id = %s",
            (run_sha, task.row["case_id"]),
        ).fetchone()
        if case is None:
            return None
        comparison = dict(case["comparison"] or {})
        candidate_a = comparison.get("pair_order") == "candidate_A"
        report = self._conn.execute(
            "SELECT parent_sha, payload FROM news_learning_artifacts "
            "WHERE kind = 'evaluation_report' AND payload->>'run_sha' = %s "
            "ORDER BY created_at_ms DESC LIMIT 1",
            (run_sha,),
        ).fetchone()
        candidate_sha = str((report or {}).get("parent_sha") or "")
        artifact = self._conn.execute(
            "SELECT payload FROM news_learning_artifacts "
            "WHERE kind = 'candidate' AND payload->>'candidate_sha' = %s "
            "ORDER BY created_at_ms DESC LIMIT 1",
            (candidate_sha,),
        ).fetchone()
        candidate_payload = dict((artifact or {}).get("payload") or {})
        manifest = dict(candidate_payload.get("manifest") or {})
        preference = str((accepted.get("payload") or {}).get("preference") or "uncertain")
        if preference in {"A", "B"}:
            preferred_arm = "candidate" if (preference == "A") == candidate_a else "stable"
        else:
            preferred_arm = preference
        return {
            "arm_identity_revealed": True,
            "outcome_revealed": True,
            "stable_side": "B" if candidate_a else "A",
            "candidate_side": "A" if candidate_a else "B",
            "accepted_preference": preference,
            "preferred_arm": preferred_arm,
            "candidate_sha": candidate_sha or None,
            "target": manifest.get("target"),
            "hypothesis": manifest.get("hypothesis"),
            "exact_diff": candidate_payload.get("exact_diff"),
        }

    def _pairwise_run_reveal_ready(self, run_sha: str) -> bool:
        counts = self._conn.execute(
            """
            WITH accepted_pair AS (
              SELECT j.pairwise_case_id, max(a.created_at_ms) AS accepted_at_ms
                FROM news_review_records_v1 a
                JOIN news_review_records_v1 j ON j.review_id = a.accepts_review_id
               WHERE a.review_kind = 'acceptance' AND j.subject_kind = 'pairwise'
               GROUP BY j.pairwise_case_id
            )
            SELECT count(*) AS planned_n,
                   count(*) FILTER (WHERE accepted_pair.pairwise_case_id IS NOT NULL) AS accepted_n,
                   max(accepted_pair.accepted_at_ms) AS latest_acceptance_ms
              FROM news_review_pairwise_tasks_v1 c
              LEFT JOIN accepted_pair ON accepted_pair.pairwise_case_id = c.run_sha || ':' || c.case_id
             WHERE c.run_sha = %s AND c.dataset_role = 'validation'
            """,
            (run_sha,),
        ).fetchone()
        planned = int((counts or {}).get("planned_n") or 0)
        if planned == 0 or int((counts or {}).get("accepted_n") or 0) != planned:
            return False
        report = self._conn.execute(
            "SELECT payload, created_at_ms FROM news_learning_artifacts "
            "WHERE kind = 'evaluation_report' AND payload->>'run_sha' = %s "
            "ORDER BY created_at_ms DESC LIMIT 1",
            (run_sha,),
        ).fetchone()
        if report is None or int(report["created_at_ms"]) < int(counts.get("latest_acceptance_ms") or 0):
            return False
        primary = dict((dict(report["payload"] or {}).get("evidence") or {}).get("primary") or {})
        return int(primary.get("planned_cluster_n") or 0) > 0 and int(primary.get("resolved_cluster_n") or 0) >= int(
            primary.get("planned_cluster_n") or 0
        )

    def _candidate_diff_reveal_allowed(self, candidate_sha: str) -> bool:
        runs = self._conn.execute(
            """
            SELECT DISTINCT c.run_sha
              FROM news_learning_cases c
              JOIN news_learning_artifacts r
                ON r.kind = 'evaluation_report' AND r.payload->>'run_sha' = c.run_sha
             WHERE c.dataset_role = 'validation' AND r.parent_sha = %s
            """,
            (candidate_sha,),
        ).fetchall()
        return not runs or all(self._pairwise_run_reveal_ready(str(row["run_sha"])) for row in runs)

    def _queue_response(
        self, query: DeskQuery, tasks: Sequence[Mapping[str, Any]], *, next_cursor: str | None
    ) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for task in tasks:
            key = str(task["selection"]["stratum"])
            counts[key] = counts.get(key, 0) + 1
        return {
            "view": "queue",
            "mode": query.mode,
            "status": "ready" if tasks else "insufficient_evidence",
            "reader_contract_version": READER_CONTRACT_VERSION,
            "rubric_version": REVIEW_RUBRIC_VERSION,
            "tasks": list(tasks),
            "next_cursor": next_cursor,
            "counts": counts,
        }

    def _coverage(self, query: DeskQuery) -> dict[str, Any]:
        lower = self._now_ms - int(query.hours) * 3_600_000
        current_epoch = _current_learning_epoch()
        statement = _coverage_statement(
            lower_ms=lower,
            upper_ms=self._now_ms,
            learning_epoch=current_epoch,
        )
        rows = self._conn.execute(statement.sql, statement.params).fetchall()
        accepted_by_task = self._accepted_event_tasks([str(row["event_id"]) for row in rows])
        cohorts: dict[str, dict[str, Any]] = {}
        strata: dict[str, dict[str, Any]] = {}
        reviewed = 0
        accepted = 0
        release_eligible = 0
        received = 0
        for row in rows:
            agent_identity = _agent_identity(row)
            cohort = str(agent_identity["cohort_sha256"])
            stratum = _selection(row)["stratum"]
            virtual = _virtual_task(row)
            accepted_row = accepted_by_task.get((virtual.task_id, virtual.task_version))
            bucket = cohorts.setdefault(
                cohort,
                {
                    "events": 0,
                    "received": 0,
                    "reviewed": 0,
                    "accepted": 0,
                    "legacy_cohort": _cohort(row),
                    "agent": agent_identity,
                },
            )
            stratum_bucket = strata.setdefault(stratum, {"events": 0, "accepted": 0})
            bucket["events"] += 1
            stratum_bucket["events"] += 1
            if row.get("delivery_state") == "sent":
                bucket["received"] += 1
                received += 1
            if bool(row.get("evidence_release_eligible")):
                release_eligible += 1
            if accepted_row is not None:
                reviewed += 1
                accepted += 1
                bucket["reviewed"] += 1
                bucket["accepted"] += 1
                stratum_bucket["accepted"] += 1
        for bucket in [*cohorts.values(), *strata.values()]:
            n = int(bucket["events"])
            k = int(bucket["accepted"])
            bucket["accepted_pct"] = _pct(k, n)
            bucket["accepted_interval_95"] = _wilson(k, n)
        external = self._conn.execute(
            """
            WITH current_epoch AS (
              SELECT greatest(%s, starts_at_ms) AS lower_ms
                FROM news_learning_epochs
               WHERE epoch_id = %s
            )
            SELECT count(source.snapshot_id) AS n, current_epoch.lower_ms
              FROM current_epoch
              LEFT JOIN news_review_external_source_v1 source
                ON source.created_at_ms >= current_epoch.lower_ms
               AND source.created_at_ms < %s
             GROUP BY current_epoch.lower_ms
            """,
            (lower, current_epoch, self._now_ms),
        ).fetchone()
        if external is None:
            raise RuntimeError("news_review_learning_epoch_missing")
        blind = self._conn.execute(
            """
            WITH accepted_pair AS (
              SELECT DISTINCT ON (j.pairwise_case_id)
                     j.pairwise_case_id, j.payload
                FROM news_review_records_v1 a
                JOIN news_review_records_v1 j ON j.review_id = a.accepts_review_id
               WHERE a.review_kind = 'acceptance' AND j.subject_kind = 'pairwise'
               ORDER BY j.pairwise_case_id, a.created_at_ms DESC, a.review_id DESC
            )
            SELECT count(*) AS case_n,
                   count(DISTINCT c.cluster_id) AS cluster_n,
                   count(*) FILTER (
                     WHERE accepted_pair.pairwise_case_id IS NOT NULL
                       AND COALESCE(accepted_pair.payload ->> 'preference', 'uncertain') <> 'uncertain'
                   ) AS accepted_case_n,
                   count(DISTINCT c.cluster_id) FILTER (
                     WHERE accepted_pair.pairwise_case_id IS NOT NULL
                       AND COALESCE(accepted_pair.payload ->> 'preference', 'uncertain') <> 'uncertain'
                   ) AS accepted_cluster_n
              FROM news_review_pairwise_tasks_v1 c
              JOIN news_learning_artifacts dataset
                ON dataset.kind = 'dataset' AND dataset.artifact_sha = c.dataset_sha
              LEFT JOIN accepted_pair
                ON accepted_pair.pairwise_case_id = c.run_sha || ':' || c.case_id
             WHERE c.dataset_role = 'validation'
               AND dataset.payload ->> 'learning_epoch' = %s
            """,
            (_current_learning_epoch(),),
        ).fetchone()
        blind_case_n = int(blind["case_n"] or 0)
        blind_accepted_n = int(blind["accepted_case_n"] or 0)
        total = len(rows)
        evidence_ready = total > 0 and release_eligible > 0 and accepted > 0
        return {
            "view": "coverage",
            "status": "ready" if evidence_ready else "insufficient_evidence",
            "message_zh": None if evidence_ready else "证据不足：需要真实 observed evidence 和已接受复盘",
            "window": {"from_ms": int(external["lower_ms"]), "to_ms": self._now_ms, "hours": query.hours},
            "funnel": {
                "received": received,
                "replayable": release_eligible,
                "reviewed": reviewed,
                "accepted": accepted,
                "holdout_ready": int(blind["accepted_cluster_n"] or 0),
                "total": total,
                "external_misses": int(external["n"] or 0),
            },
            "cohorts": [{"cohort": name, **data} for name, data in sorted(cohorts.items())],
            "strata": [
                {"stratum": name, "stratum_zh": _STRATUM_ZH.get(name, "未识别复盘分层"), **data}
                for name, data in sorted(strata.items())
            ],
            "holdout": {
                "status": "ready" if blind_case_n and blind_accepted_n == blind_case_n else "insufficient_evidence",
                "case_n": blind_case_n,
                "cluster_n": int(blind["cluster_n"] or 0),
                "accepted_case_n": blind_accepted_n,
                "accepted_cluster_n": int(blind["accepted_cluster_n"] or 0),
                "coverage_pct": _pct(blind_accepted_n, blind_case_n),
                "coverage_interval_95": _wilson(blind_accepted_n, blind_case_n),
            },
            "reader_contract_version": READER_CONTRACT_VERSION,
            "reader_contract_sha256": READER_CONTRACT_SHA256,
            "rubric_version": REVIEW_RUBRIC_VERSION,
        }

    def _market(self, query: DeskQuery) -> dict[str, Any]:
        if query.hours > REVIEW_MARKET_MAX_HOURS:
            raise ValueError("news_review_market_hours_too_large")
        cohort = self._market_cohort(query.cohort, hours=query.hours)
        review = PriceRepository(self._conn).review(
            hours=query.hours,
            now_ms=self._now_ms,
            cohort=cohort,
        )
        # Taxonomy v1 is demonstrably overloaded (for example regulation/whale/funding).  Keep the diagnostic
        # adapter intact, but do not expose a quality ranking that operators could mistake for learning truth.
        review["event_types"] = []
        return {
            "view": "market",
            "status": "ready" if cohort else "insufficient_evidence",
            "title_zh": "事后市场观察",
            "disclaimer_zh": "价格变化只是观察证据，不是新闻因果、奖励或 should-push 真值。",
            "reaction": review,
            "message_zh": None if cohort else "当前窗口没有可比较的同版本 Agent cohort。",
        }

    def _market_cohort(self, cohort_sha: str, *, hours: int) -> MarketReviewCohort | None:
        lower = self._now_ms - int(hours) * 3_600_000
        selected_sha = _parse_agent_cohort_sha(cohort_sha) if cohort_sha else self._active_agent_cohort_sha()
        if selected_sha is None:
            return None
        row = self._conn.execute(
            """
            SELECT program_version, program_sha256, policy_version, model
              FROM news_review_task_source_v1
             WHERE program_version IS NOT NULL AND program_sha256 IS NOT NULL
               AND policy_version IS NOT NULL AND model IS NOT NULL
               AND opened_at_ms >= %s AND opened_at_ms < %s
               AND COALESCE(trace #>> '{agent_assignment,bundle_sha}', '') = %s
             ORDER BY verdict_created_at_ms DESC NULLS LAST
             LIMIT 1
            """,
            (lower, self._now_ms, selected_sha),
        ).fetchone()
        if row is None:
            return None
        return MarketReviewCohort(
            bundle_sha256=selected_sha,
            program_version=str(row["program_version"]),
            program_sha256=str(row["program_sha256"]),
            policy_version=str(row["policy_version"]),
            model=str(row["model"]),
        )

    def _event_task(self, event_id: str, *, evidence_version: int | None = None) -> _VirtualTask | None:
        statement = _event_task_statement(event_id, evidence_version=evidence_version)
        row = self._conn.execute(statement.sql, statement.params).fetchone()
        return _virtual_task(row) if row is not None else None

    def _pairwise_task(self, task_id: str) -> _VirtualTask | None:
        run_sha, case_id = _parse_pairwise_task_id(task_id)
        row = self._conn.execute(
            "SELECT c.*, dataset.payload ->> 'learning_epoch' AS learning_epoch "
            "FROM news_review_pairwise_tasks_v1 c "
            "LEFT JOIN news_learning_artifacts dataset "
            "ON dataset.kind = 'dataset' AND dataset.artifact_sha = c.dataset_sha "
            "WHERE c.run_sha = %s AND c.case_id = %s",
            (run_sha, case_id),
        ).fetchone()
        return _pairwise_virtual(row) if row is not None else None

    def _pairwise_source(self, row: Mapping[str, Any]) -> Mapping[str, Any]:
        if row.get("event_id"):
            source = self._conn.execute(
                "SELECT evidence_snapshot FROM news_review_task_source_v1 "
                "WHERE event_id = %s AND evidence_version = %s",
                (row["event_id"], row["evidence_version"]),
            ).fetchone()
            if source is None:
                raise ValueError("news_review_pairwise_evidence_missing")
            return dict(source["evidence_snapshot"] or {})
        source = self._conn.execute(
            "SELECT snapshot FROM news_review_external_source_v1 WHERE snapshot_id = %s",
            (row["external_snapshot_id"],),
        ).fetchone()
        if source is None:
            raise ValueError("news_review_pairwise_evidence_missing")
        return dict(source["snapshot"] or {})

    def _submit_event(
        self,
        task_ref: TaskRef,
        submission: EventRubricSubmission,
        *,
        principal: Principal,
        idempotency_key: str,
        idempotency_request_sha: str,
    ) -> dict[str, Any]:
        event_id, evidence_version = _parse_event_task_id(task_ref.task_id)
        task = self._event_task(event_id, evidence_version=evidence_version)
        if task is None:
            raise ValueError("news_review_task_not_found")
        if task.task_version != task_ref.task_version:
            raise ValueError("news_review_task_version_conflict")
        previous = self._latest_accepted(task)
        owner = submission.first_bad_owner or _derive_owner(submission)
        created_at = self._db_now_ms()
        payload = submission.model_dump(mode="json")
        review_id = _sha(
            {
                "kind": "judgment",
                "task_id": task.task_id,
                "task_version": task.task_version,
                "reviewer": principal.subject,
                "idempotency_key": idempotency_key,
                "payload": payload,
            }
        )
        accepted_id = _sha({"kind": "acceptance", "review_id": review_id})
        release_eligible = (
            bool(task.row.get("evidence_release_eligible")) and str(task.selection.get("stratum")) != "high_reaction"
        )
        self._conn.execute(
            """
            INSERT INTO news_reviews (
              review_id, idempotency_key, idempotency_request_sha, review_kind, subject_kind, task_id, task_version,
              event_id, evidence_version, rubric_version, reader_contract_version, reviewer,
              should_push, dimensions, novelty, first_bad_owner, evidence_refs,
              expected_correction, note, selection, payload, supersedes_review_id,
              release_eligible, created_at_ms
            ) VALUES (
              %s, %s, %s, 'judgment', 'event', %s, %s, %s, %s, %s, %s, %s,
              %s, %s::jsonb, %s::jsonb, %s, %s::jsonb, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s
            )
            """,
            (
                review_id,
                idempotency_key,
                idempotency_request_sha,
                task.task_id,
                task.task_version,
                event_id,
                evidence_version,
                REVIEW_RUBRIC_VERSION,
                READER_CONTRACT_VERSION,
                principal.subject,
                submission.should_push,
                _json(submission.dimensions),
                _json(submission.novelty.model_dump(mode="json")),
                owner,
                _json(submission.evidence_refs),
                submission.expected_correction,
                submission.note,
                _json(task.selection),
                _json(payload),
                previous["review_id"] if previous else None,
                release_eligible,
                created_at,
            ),
        )
        self._append_acceptance(
            acceptance_id=accepted_id,
            judgment_id=review_id,
            task_id=task.task_id,
            task_version=task.task_version,
            subject_kind="event",
            event_id=event_id,
            evidence_version=evidence_version,
            external_snapshot_id=None,
            pairwise_case_id=None,
            principal=principal,
            created_at_ms=created_at,
            release_eligible=release_eligible,
        )
        return self._submission_receipt(review_id, accepted_id, task=task, idempotent=False)

    def _submit_pairwise(
        self,
        task_ref: TaskRef,
        submission: BlindPairwiseSubmission,
        *,
        principal: Principal,
        idempotency_key: str,
        idempotency_request_sha: str,
    ) -> dict[str, Any]:
        task = self._pairwise_task(task_ref.task_id)
        if task is None:
            raise ValueError("news_review_task_not_found")
        if task.task_version != task_ref.task_version:
            raise ValueError("news_review_task_version_conflict")
        if _pairwise_evidence_disposition(task.row) != "current":
            raise ValueError("news_review_pairwise_task_audit_only")
        run_sha, case_id = _parse_pairwise_task_id(task.task_id)
        pairwise_case_id = f"{run_sha}:{case_id}"
        previous = self._latest_accepted(task)
        created_at = self._db_now_ms()
        payload = submission.model_dump(mode="json")
        review_id = _sha(
            {
                "kind": "blind_pairwise",
                "task_id": task.task_id,
                "task_version": task.task_version,
                "reviewer": principal.subject,
                "idempotency_key": idempotency_key,
                "payload": payload,
            }
        )
        accepted_id = _sha({"kind": "acceptance", "review_id": review_id})
        self._conn.execute(
            """
            INSERT INTO news_reviews (
              review_id, idempotency_key, idempotency_request_sha, review_kind, subject_kind, task_id, task_version,
              pairwise_case_id, rubric_version, reader_contract_version, reviewer,
              evidence_refs, note, selection, payload, supersedes_review_id,
              release_eligible, created_at_ms
            ) VALUES (
              %s, %s, %s, 'judgment', 'pairwise', %s, %s, %s, %s, %s, %s,
              %s::jsonb, %s, %s::jsonb, %s::jsonb, %s, true, %s
            )
            """,
            (
                review_id,
                idempotency_key,
                idempotency_request_sha,
                task.task_id,
                task.task_version,
                pairwise_case_id,
                REVIEW_RUBRIC_VERSION,
                READER_CONTRACT_VERSION,
                principal.subject,
                _json(submission.evidence_refs),
                submission.note,
                _json(task.selection),
                _json(payload),
                previous["review_id"] if previous else None,
                created_at,
            ),
        )
        self._append_acceptance(
            acceptance_id=accepted_id,
            judgment_id=review_id,
            task_id=task.task_id,
            task_version=task.task_version,
            subject_kind="pairwise",
            event_id=None,
            evidence_version=None,
            external_snapshot_id=None,
            pairwise_case_id=pairwise_case_id,
            principal=principal,
            created_at_ms=created_at,
            release_eligible=True,
        )
        return self._submission_receipt(review_id, accepted_id, task=task, idempotent=False)

    def _submit_external(
        self,
        submission: ExternalMissSubmission,
        *,
        principal: Principal,
        idempotency_key: str,
        idempotency_request_sha: str,
    ) -> dict[str, Any]:
        created_at = self._db_now_ms()
        if submission.occurred_at_ms > created_at:
            raise ValueError("news_review_external_miss_future")
        evidence = {
            "schema_version": "news_external_miss_v1",
            "source_url": submission.source_url,
            "title": submission.title,
            "body": submission.body,
            "occurred_at_ms": submission.occurred_at_ms,
            "observed_at_ms": created_at,
            # V1 has one authenticated operator principal.  Provenance is a
            # server-owned fact; accepting it from the body would let a caller
            # impersonate a provider, reviewer, or collection path.
            "provenance": "operator_reported",
        }
        evidence_sha = _sha(evidence)
        snapshot_id = _sha({"evidence_sha256": evidence_sha, "creator": principal.subject})
        task_id = f"external.{snapshot_id}"
        task_version = _sha(
            {
                "task": REVIEW_TASK_VERSION,
                "snapshot_id": snapshot_id,
                "rubric": REVIEW_RUBRIC_VERSION,
                "reader_contract": READER_CONTRACT_VERSION,
            }
        )
        rubric = submission.rubric
        owner = rubric.first_bad_owner or _derive_owner(rubric, external=True)
        payload = rubric.model_dump(mode="json")
        review_id = _sha(
            {
                "kind": "external_miss_judgment",
                "snapshot_id": snapshot_id,
                "reviewer": principal.subject,
                "idempotency_key": idempotency_key,
                "payload": payload,
            }
        )
        accepted_id = _sha({"kind": "acceptance", "review_id": review_id})
        self._conn.execute(
            """
            INSERT INTO news_external_miss_snapshots (
              snapshot_id, evidence_sha256, source_url, title, body, occurred_at_ms, observed_at_ms,
              provenance, snapshot, created_by, created_at_ms
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
            """,
            (
                snapshot_id,
                evidence_sha,
                submission.source_url,
                submission.title,
                submission.body,
                submission.occurred_at_ms,
                created_at,
                "operator_reported",
                _json(evidence),
                principal.subject,
                created_at,
            ),
        )
        selection = {"stratum": "eventless_miss", "sampling_probability": 1.0, "reason": "operator_created"}
        self._conn.execute(
            """
            INSERT INTO news_reviews (
              review_id, idempotency_key, idempotency_request_sha, review_kind, subject_kind, task_id, task_version,
              external_snapshot_id, rubric_version, reader_contract_version, reviewer,
              should_push, dimensions, novelty, first_bad_owner, evidence_refs,
              expected_correction, note, selection, payload, release_eligible, created_at_ms
            ) VALUES (
              %s, %s, %s, 'judgment', 'external_miss', %s, %s, %s, %s, %s, %s,
              %s, %s::jsonb, %s::jsonb, %s, %s::jsonb, %s, %s, %s::jsonb, %s::jsonb, true, %s
            )
            """,
            (
                review_id,
                idempotency_key,
                idempotency_request_sha,
                task_id,
                task_version,
                snapshot_id,
                REVIEW_RUBRIC_VERSION,
                READER_CONTRACT_VERSION,
                principal.subject,
                rubric.should_push,
                _json(rubric.dimensions),
                _json(rubric.novelty.model_dump(mode="json")),
                owner,
                _json(rubric.evidence_refs),
                rubric.expected_correction,
                rubric.note,
                _json(selection),
                _json(payload),
                created_at,
            ),
        )
        self._append_acceptance(
            acceptance_id=accepted_id,
            judgment_id=review_id,
            task_id=task_id,
            task_version=task_version,
            subject_kind="external_miss",
            event_id=None,
            evidence_version=None,
            external_snapshot_id=snapshot_id,
            pairwise_case_id=None,
            principal=principal,
            created_at_ms=created_at,
            release_eligible=True,
        )
        return {
            "idempotent": False,
            "receipt": {
                "review_id": review_id,
                "acceptance_id": accepted_id,
                "external_snapshot_id": snapshot_id,
                "task_id": task_id,
                "task_version": task_version,
                "created_at_ms": created_at,
            },
            "next_task": None,
            "updated_queue_counts": {},
        }

    def _append_acceptance(
        self,
        *,
        acceptance_id: str,
        judgment_id: str,
        task_id: str,
        task_version: str,
        subject_kind: str,
        event_id: str | None,
        evidence_version: int | None,
        external_snapshot_id: str | None,
        pairwise_case_id: str | None,
        principal: Principal,
        created_at_ms: int,
        release_eligible: bool,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO news_reviews (
              review_id, review_kind, subject_kind, task_id, task_version, event_id, evidence_version,
              external_snapshot_id, pairwise_case_id, rubric_version, reader_contract_version, reviewer,
              accepts_review_id, release_eligible, created_at_ms
            ) VALUES (%s, 'acceptance', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                acceptance_id,
                subject_kind,
                task_id,
                task_version,
                event_id,
                evidence_version,
                external_snapshot_id,
                pairwise_case_id,
                REVIEW_RUBRIC_VERSION,
                READER_CONTRACT_VERSION,
                principal.subject,
                judgment_id,
                release_eligible,
                created_at_ms,
            ),
        )

    def _submission_receipt(
        self, review_id: str, acceptance_id: str, *, task: _VirtualTask, idempotent: bool
    ) -> dict[str, Any]:
        mode = "pairwise" if task.task_id.startswith("pair.") else "event"
        queue = self._open_queue(DeskQuery(mode=mode, status="pending", limit=1))
        tasks = list(queue.get("tasks") or [])
        return {
            "idempotent": idempotent,
            "receipt": {
                "review_id": review_id,
                "acceptance_id": acceptance_id,
                "task_id": task.task_id,
                "task_version": task.task_version,
            },
            "next_task": tasks[0] if tasks else None,
            "updated_queue_counts": dict(queue.get("counts") or {}),
        }

    def _latest_accepted(self, task: _VirtualTask) -> dict[str, Any] | None:
        row = self._conn.execute(
            """
            SELECT j.*
             FROM news_review_records_v1 a
              JOIN news_review_records_v1 j ON j.review_id = a.accepts_review_id
             WHERE a.review_kind = 'acceptance'
               AND j.task_id = %s AND j.task_version = %s
               AND j.reader_contract_version = %s
             ORDER BY a.created_at_ms DESC, a.review_id DESC LIMIT 1
            """,
            (task.task_id, task.task_version, READER_CONTRACT_VERSION),
        ).fetchone()
        return _review_public(row) if row is not None else None

    def _accepted_event_tasks(self, event_ids: Sequence[str]) -> dict[tuple[str, str], dict[str, Any]]:
        if not event_ids:
            return {}
        rows = self._conn.execute(
            """
            SELECT DISTINCT ON (j.task_id, j.task_version) j.*, a.created_at_ms AS accepted_at_ms
              FROM news_review_records_v1 a
              JOIN news_review_records_v1 j ON j.review_id = a.accepts_review_id
             WHERE a.review_kind = 'acceptance' AND j.event_id = ANY(%s)
               AND j.reader_contract_version = %s
             ORDER BY j.task_id, j.task_version, a.created_at_ms DESC, a.review_id DESC
            """,
            (list(event_ids), READER_CONTRACT_VERSION),
        ).fetchall()
        return {(str(row["task_id"]), str(row["task_version"])): _review_public(row) for row in rows}

    def _active_agent_cohort_sha(self) -> str | None:
        statement = _active_agent_statement()
        row = self._conn.execute(statement.sql, statement.params).fetchone()
        if row is None:
            return None
        stable_sha = str(row.get("stable_sha") or "")
        return stable_sha if _is_sha256(stable_sha) else None

    def _idempotent_receipt(self, reviewer: str, idempotency_key: str, *, request_sha: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM news_review_records_v1 WHERE reviewer = %s AND idempotency_key = %s",
            (reviewer, idempotency_key),
        ).fetchone()
        if row is None:
            return None
        if str(row.get("idempotency_request_sha") or "") != request_sha:
            raise ValueError("news_review_idempotency_conflict")
        acceptance = self._conn.execute(
            "SELECT review_id FROM news_review_records_v1 WHERE review_kind = 'acceptance' AND accepts_review_id = %s",
            (row["review_id"],),
        ).fetchone()
        return {
            "idempotent": True,
            "receipt": {
                "review_id": row["review_id"],
                "acceptance_id": acceptance["review_id"] if acceptance else None,
                "task_id": row["task_id"],
                "task_version": row["task_version"],
                "external_snapshot_id": row.get("external_snapshot_id"),
                "created_at_ms": row["created_at_ms"],
            },
            "next_task": None,
            "updated_queue_counts": {},
        }

    def _db_now_ms(self) -> int:
        row = self._conn.execute(
            "SELECT floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint AS now_ms"
        ).fetchone()
        return int(row["now_ms"])

    @staticmethod
    def _require_principal(principal: Principal) -> None:
        if not principal.can_review:
            raise PermissionError("news_review_forbidden")


def _virtual_task(row: Mapping[str, Any]) -> _VirtualTask:
    selection = _selection(row)
    agent_identity = _agent_identity(row)
    event_id = str(row["event_id"])
    evidence_version = int(row["evidence_version"])
    identity = _sha(
        {
            "task": REVIEW_TASK_VERSION,
            "event_id": event_id,
            "evidence_version": evidence_version,
            "rubric": REVIEW_RUBRIC_VERSION,
            "reader_contract": READER_CONTRACT_VERSION,
            "reader_contract_sha256": READER_CONTRACT_SHA256,
            "agent_cohort_sha256": agent_identity["cohort_sha256"],
        }
    )
    task_id = f"evt.{event_id}.{evidence_version}.{identity[:16]}"
    task_version = _sha(
        {
            "identity": identity,
            "evidence_sha256": row["evidence_sha256"],
            "verdict": row.get("verdict"),
            "final_decision": row.get("final_decision"),
            "delivery_state": row.get("delivery_state"),
            "delivery_card": row.get("delivery_card"),
            "selection": selection,
            "agent": agent_identity,
        }
    )
    return _VirtualTask(task_id=task_id, task_version=task_version, row=row, selection=selection)


def _pairwise_virtual(row: Mapping[str, Any]) -> _VirtualTask:
    run_sha = str(row["run_sha"])
    case_id = str(row["case_id"])
    selection = {
        "stratum": "blind_pairwise" if row.get("dataset_role") == "validation" else "development_pairwise",
        "stratum_zh": (
            _STRATUM_ZH["blind_pairwise"]
            if row.get("dataset_role") == "validation"
            else _STRATUM_ZH["development_pairwise"]
        ),
        "sampling_probability": 1.0,
        "selection_version": "news_blind_pairwise_v1",
    }
    task_id = f"pair.{run_sha}.{case_id}"
    task_version = _sha(
        {
            "task": "news_blind_pairwise_v1",
            "run_sha": run_sha,
            "case_id": case_id,
            "evidence_sha256": row["evidence_sha256"],
            "output_A": row.get("output_a"),
            "output_B": row.get("output_b"),
            "disclosure": row.get("disclosure"),
            "rubric": REVIEW_RUBRIC_VERSION,
        }
    )
    return _VirtualTask(task_id=task_id, task_version=task_version, row=row, selection=selection)


def _pairwise_task_public(task: _VirtualTask, *, accepted: Mapping[str, Any] | None) -> dict[str, Any]:
    learning_epoch = str(task.row.get("learning_epoch") or "") or None
    evidence_disposition = _pairwise_evidence_disposition(task.row)
    return {
        "task_id": task.task_id,
        "task_version": task.task_version,
        "mode": "pairwise",
        "learning_epoch": learning_epoch,
        "evidence_disposition": evidence_disposition,
        "selection": dict(task.selection),
        "review_status": ("accepted" if accepted is not None else "pending")
        if evidence_disposition == "current"
        else "audit_only",
        "accepted_review": accepted,
        "disclosure": {
            "outcome_revealed": False,
            "arm_identity_revealed": False,
            "dataset_role": (
                "hidden_temporal_holdout" if task.row.get("dataset_role") == "validation" else "development"
            ),
        },
    }


def _pairwise_evidence_disposition(row: Mapping[str, Any]) -> str:
    return "current" if row.get("learning_epoch") == _current_learning_epoch() else "audit_only"


def _pairwise_evidence(
    task: _VirtualTask,
    *,
    source: Mapping[str, Any],
    accepted: Mapping[str, Any] | None,
    reveal: Mapping[str, Any] | None,
) -> dict[str, Any]:
    row = task.row
    arm_a = row.get("output_a") or {}
    arm_b = row.get("output_b") or {}
    return {
        "task": _pairwise_task_public(task, accepted=accepted),
        "disclosure": {
            "outcome_revealed": reveal is not None,
            "arm_identity_revealed": reveal is not None,
            "pairing": "paired",
            "dataset_role": ("hidden_temporal_holdout" if row.get("dataset_role") == "validation" else "development"),
        },
        "source_evidence": dict(source),
        "output_A": _blind_output(arm_a),
        "output_B": _blind_output(arm_b),
        "rubric": {
            "preference_values": ["A", "B", "tie", "both_bad", "uncertain"],
            "critical_error_examples": [
                "unsupported_fact",
                "wrong_entity",
                "wrong_direction",
                "missed_key_fact",
            ],
        },
        "reveal": dict(reveal) if reveal is not None else None,
    }


def _blind_output(observation: Mapping[str, Any]) -> dict[str, Any]:
    verdict = dict(observation.get("verdict") or {})
    return {
        "headline_zh": verdict.get("headline_zh") or "",
        "why_zh": verdict.get("why_zh") or "",
        "direction": verdict.get("direction"),
        "magnitude": verdict.get("magnitude"),
        "actionable": verdict.get("actionable"),
        "final_decision": observation.get("final_decision"),
        "final_decision_zh": _review_decision_zh(observation.get("final_decision")),
        "error_code": observation.get("error_code"),
    }


def _selection(row: Mapping[str, Any]) -> dict[str, Any]:
    if row.get("delivery_error_code") == "ambiguous_after_crash":
        stratum, reason, probability = "delivery_ambiguous", "delivery_truth_unknown", 1.0
    elif row.get("delivery_state") == "terminal":
        stratum, reason, probability = "delivery_failed", "delivery_terminal_failure", 1.0
    elif row.get("priority") == "high" or row.get("final_decision") == "escalate":
        stratum, reason, probability = "critical", "high_priority_or_escalate", 1.0
    elif row.get("final_decision") == "throttled":
        stratum, reason, probability = "throttled", "duplicate_or_historical_throttle", 1.0
    elif row.get("delivery_state") == "sent":
        stratum, reason, probability = "delivered", "sent_quality_sample", 0.25
    elif int(row.get("max_abs_return_1h_bps") or 0) >= REVIEW_HIGH_REACTION_DISCOVERY_BPS:
        stratum, reason, probability = "high_reaction", "market_discovery_only", 1.0
    elif row.get("final_decision") == "drop":
        stratum, reason, probability = "model_drop", "semantic_or_policy_hold", 0.10
    elif str(row.get("admission") or "").startswith("suppressed"):
        stratum, reason, probability = "gate_suppress", "upstream_recall_sample", 0.10
    else:
        stratum, reason, probability = "random_control", "coverage_control", 0.02
    return {
        "stratum": stratum,
        "stratum_zh": _STRATUM_ZH.get(stratum, "未识别复盘分层"),
        "reason": reason,
        "reason_zh": _SELECTION_REASON_ZH.get(reason, "未识别抽样原因"),
        "sampling_probability": probability,
        "selection_version": "news_review_sampler_v1",
    }


def _task_public(task: _VirtualTask, *, accepted: Mapping[str, Any] | None) -> dict[str, Any]:
    row = task.row
    snapshot = row["evidence_snapshot"]
    card = snapshot.get("card") or {}
    focus = snapshot.get("focus_fact") or {}
    verdict = row.get("verdict") or {}
    return {
        "task_id": task.task_id,
        "task_version": task.task_version,
        "mode": "event",
        "event_id": row["event_id"],
        "evidence_version": row["evidence_version"],
        "verdict_evidence_version": row.get("verdict_evidence_version"),
        "opened_at_ms": row["opened_at_ms"],
        "headline": focus.get("text") or card.get("leader_title") or "",
        "agent_headline": verdict.get("headline_zh") or "",
        "agent_why": verdict.get("why_zh") or "",
        "final_decision": row.get("final_decision"),
        "final_decision_zh": _review_decision_zh(row.get("final_decision")),
        "reader_receipt": _receipt_public(row),
        "cohort": _cohort(row),
        "agent_cohort": _agent_identity(row),
        "selection": dict(task.selection),
        "evidence_ready": bool(row.get("evidence_release_eligible")),
        "review_status": "accepted" if accepted is not None else "pending",
        "accepted_review": accepted,
    }


def _receipt_public(row: Mapping[str, Any]) -> dict[str, Any]:
    state = row.get("delivery_state")
    if state == "sent":
        truth = "received"
    elif row.get("delivery_error_code") == "ambiguous_after_crash":
        truth = "unknown"
    else:
        truth = "not_received"
    return {
        "truth": truth,
        "truth_zh": {"received": "读者已收到", "not_received": "读者未收到", "unknown": "送达未知"}[truth],
        "state": state,
        "settled_at_ms": row.get("settled_at_ms"),
        "rendered_card": row.get("delivery_card") if state == "sent" else None,
        "error_code": row.get("delivery_error_code"),
    }


def _rubric_contract(row: Mapping[str, Any]) -> dict[str, Any]:
    verdict = row.get("verdict") or {}
    dimensions = ["factual_fidelity", "headline_fidelity", "why_support", "why_value"]
    if verdict.get("assets"):
        dimensions.append("asset_grounding")
    if verdict.get("direction") in {"bullish", "bearish"}:
        dimensions.extend(["direction", "magnitude"])
    dimensions.append("timeliness")
    return {
        "should_push_values": ["must_push", "should_push", "should_hold", "must_hold", "uncertain"],
        "dimensions": dimensions,
        "dimension_values": ["pass", "fail", "uncertain", "not_applicable"],
        "novelty_values": sorted(_NOVELTY),
        "first_bad_owner_values": list(FirstBadOwner.__args__),  # type: ignore[attr-defined]
    }


def _verifier_flags(row: Mapping[str, Any]) -> list[dict[str, str]]:
    verdict = dict(row.get("verdict") or {})
    final = str(row.get("final_decision") or "")
    flags: list[dict[str, str]] = []
    if final in {"push", "escalate"} and verdict.get("actionable") is False:
        flags.append(
            {
                "code": "pushed_non_actionable",
                "severity": "warning",
                "message_zh": "最终推送，但语义输出 actionable=false。",
            }
        )
    if verdict.get("event_type") == "noise" and final not in {"drop", "throttled"}:
        flags.append(
            {
                "code": "noise_not_held",
                "severity": "critical",
                "message_zh": "语义输出为 noise，但最终没有拦截。",
            }
        )
    if verdict.get("novelty") == "restatement" and final in {"push", "escalate"}:
        flags.append(
            {
                "code": "restatement_delivered",
                "severity": "warning",
                "message_zh": "模型称为复述，但最终送达读者。",
            }
        )
    return flags


def _derive_owner(submission: EventRubricSubmission, *, external: bool = False) -> FirstBadOwner:
    if external:
        return "receiver"
    for dimension, value in submission.dimensions.items():
        if value == "fail":
            return _OWNER_BY_DIMENSION.get(dimension, "unknown")
    if submission.novelty.judgment == "restatement":
        return "retrieval"
    return "unknown"


def _review_public(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "review_id": row["review_id"],
        "subject_kind": row["subject_kind"],
        "event_id": row.get("event_id"),
        "external_snapshot_id": row.get("external_snapshot_id"),
        "pairwise_case_id": row.get("pairwise_case_id"),
        "should_push": row.get("should_push"),
        "dimensions": row.get("dimensions") or {},
        "novelty": row.get("novelty") or {},
        "first_bad_owner": row.get("first_bad_owner"),
        "evidence_refs": row.get("evidence_refs") or [],
        "expected_correction": row.get("expected_correction") or "",
        "note": row.get("note") or "",
        "payload": row.get("payload") or {},
        "reviewer": row["reviewer"],
        "created_at_ms": row["created_at_ms"],
        "rubric_version": row["rubric_version"],
        "reader_contract_version": row["reader_contract_version"],
    }


def _cohort(row: Mapping[str, Any]) -> str:
    generation_version = row.get("program_version") or row.get("prompt_version")
    return "/".join(
        [
            str(generation_version or "no_generation"),
            str(row.get("policy_version") or "no_policy"),
            str(row.get("model") or "no_model"),
        ]
    )


def _agent_identity(row: Mapping[str, Any]) -> dict[str, str]:
    """The exact decision system behind one historical verdict.

    New rows carry the content-addressed deployment bundle.  Older rows did
    not; for those, hash every frozen component that is actually available
    instead of pretending a prompt/policy/model display label is an Agent.
    """

    trace = dict(row.get("trace") or {})
    assignment = dict(trace.get("agent_assignment") or {})
    bundle_sha = str(assignment.get("bundle_sha") or "")
    policy = dict(trace.get("policy") or {})
    identity = {
        "bundle_sha": bundle_sha,
        "program_version": str(row.get("program_version") or ""),
        "program_sha256": str(row.get("program_sha256") or trace.get("program_sha256") or ""),
        # Kept only when explaining immutable Prompt-era audit rows.
        "prompt_version": str(row.get("prompt_version") or ""),
        "prompt_sha256": str(trace.get("prompt_sha256") or ""),
        "schema_sha256": str(trace.get("schema_sha256") or ""),
        "policy_version": str(row.get("policy_version") or ""),
        "policy_sha256": _sha(policy) if policy else "",
        "model": str(row.get("model") or ""),
        "gate_policy_version": str(trace.get("gate_policy_version") or ""),
        "reader_contract_version": READER_CONTRACT_VERSION,
        "reader_contract_sha256": READER_CONTRACT_SHA256,
    }
    identity["cohort_sha256"] = bundle_sha if _is_sha256(bundle_sha) else _sha(identity)
    return identity


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _review_decision_zh(value: object) -> str:
    return "同事实重复未推" if str(value or "") == "throttled" else decision_zh(str(value or ""))


def _release_code_zh(code: str) -> str:
    if code in _RELEASE_CODE_ZH:
        return _RELEASE_CODE_ZH[code]
    if code.startswith("development_") and code.endswith("_insufficient"):
        field = code.removeprefix("development_").removesuffix("_insufficient")
        field_zh = {
            "boundary_cluster_n": "边界事实簇",
            "retention_cluster_n": "保留集事实簇",
            "negative_cluster_n": "负例事实簇",
            "natural_day_n": "自然日",
            "stratum_n": "抽样分层",
        }.get(field, "开发集证据")
        return f"{field_zh}数量不足"
    if code.startswith("prior_") and code.endswith("_evidence_not_passed"):
        stage = code.removeprefix("prior_").removesuffix("_evidence_not_passed")
        return f"前一阶段{_RELEASE_STAGE_ZH.get(stage, '评估')}尚未通过"
    return "未识别的发布证据原因"


def _parse_agent_cohort_sha(value: str) -> str:
    normalized = value.strip().lower()
    if not _is_sha256(normalized):
        raise ValueError("news_review_cohort_invalid")
    return normalized


def _parse_event_task_id(task_id: str) -> tuple[str, int]:
    parts = task_id.split(".")
    if len(parts) != 4 or parts[0] != "evt" or not parts[1] or len(parts[3]) != 16:
        raise ValueError("news_review_task_id_invalid")
    try:
        evidence_version = int(parts[2])
    except ValueError as exc:
        raise ValueError("news_review_task_id_invalid") from exc
    return parts[1], evidence_version


def _parse_pairwise_task_id(task_id: str) -> tuple[str, str]:
    parts = task_id.split(".")
    if len(parts) != 3 or parts[0] != "pair":
        raise ValueError("news_review_task_id_invalid")
    if any(len(value) != 64 or any(char not in "0123456789abcdef" for char in value) for value in parts[1:]):
        raise ValueError("news_review_task_id_invalid")
    return parts[1], parts[2]


def _encode_cursor(opened_at_ms: int, event_id: str) -> str:
    raw = json.dumps([int(opened_at_ms), event_id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[int, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        value = json.loads(raw)
        if not isinstance(value, list) or len(value) != 2:
            raise ValueError
        return int(value[0]), str(value[1])
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("news_review_cursor_invalid") from exc


def _encode_pairwise_cursor(rank: int, created_at_ms: int, case_id: str) -> str:
    raw = json.dumps([int(rank), int(created_at_ms), case_id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_pairwise_cursor(cursor: str) -> tuple[int, int, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        value = json.loads(raw)
        if not isinstance(value, list) or len(value) != 3:
            raise ValueError
        rank, created_at_ms, case_id = int(value[0]), int(value[1]), str(value[2])
        if rank not in {0, 1} or len(case_id) != 64:
            raise ValueError
        return rank, created_at_ms, case_id
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("news_review_cursor_invalid") from exc


def _idempotency_key(value: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 128:
        raise ValueError("news_review_idempotency_key_invalid")
    return normalized


def _sha(value: Any) -> str:
    return canonical_sha(value)


def _json(value: Any) -> str:
    return canonical_json(value)


def _pct(numerator: int, denominator: int) -> float | None:
    return None if denominator <= 0 else round(numerator * 100.0 / denominator, 1)


def _wilson(successes: int, total: int) -> dict[str, float] | None:
    if total <= 0:
        return None
    z = 1.959963984540054
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denominator
    return {
        "lower_pct": round(max(0.0, centre - margin) * 100, 1),
        "upper_pct": round(min(1.0, centre + margin) * 100, 1),
    }


def _stratum_rank(value: str) -> int:
    order = {
        "delivery_ambiguous": 0,
        "delivery_failed": 1,
        "critical": 2,
        "throttled": 3,
        "gate_suppress": 4,
        "model_drop": 5,
        "delivered": 6,
        "high_reaction": 7,
        "random_control": 8,
    }
    return order.get(value, 99)


def _sampler_selected(task: _VirtualTask) -> bool:
    probability = float(task.selection.get("sampling_probability") or 0)
    if probability >= 1:
        return True
    if probability <= 0:
        return False
    bucket = int(
        _sha(
            {
                "selection_version": task.selection.get("selection_version"),
                "task_id": task.task_id,
            }
        )[:16],
        16,
    )
    return bucket < int(probability * (1 << 64))


def _proposal_status(timeline: Sequence[Mapping[str, Any]], activation: Mapping[str, Any] | None) -> str:
    if activation is not None:
        state = str(activation.get("state") or "")
        if state == "tripped":
            return "rolled_back"
        if state == "active":
            return "canary"
        if state == "closed":
            return "canary_closed"
    if any(item.get("outcome") == "fail" for item in timeline):
        return "rejected"
    latest = timeline[-1] if timeline else None
    if latest is None:
        return "proposed"
    if latest.get("outcome") != "pass":
        return "review_required"
    return {
        "offline": "evaluating",
        "holdout": "shadow_ready",
        "shadow": "canary_ready",
        "canary": "promotion_ready",
    }.get(str(latest.get("stage") or ""), "review_required")


def review_read_statements(*, now_ms: int) -> tuple[ReviewReadStatement, ...]:
    """Exact bounded ReviewDesk statements for PostgreSQL query-plan audit."""

    lower = int(now_ms) - 24 * 3_600_000
    market_sql, market_params, *_ = PriceRepository.review_statement(
        hours=24,
        now_ms=int(now_ms),
        cohort=MarketReviewCohort(
            bundle_sha256="0" * 64,
            program_version="news_semantic_program_v1",
            program_sha256="1" * 64,
            policy_version="news_triage_policy_v7",
            model="audit-model",
        ),
    )
    return (
        _event_queue_statement(
            lower_ms=lower,
            upper_ms=int(now_ms),
            cohort_sha="0" * 64,
            cursor=None,
            limit=100,
        ),
        _event_task_statement("event", evidence_version=None),
        _event_task_statement("event", evidence_version=1),
        _coverage_statement(
            lower_ms=lower,
            upper_ms=int(now_ms),
            learning_epoch=_current_learning_epoch(),
        ),
        _pairwise_queue_statement(
            proposal="",
            status="pending",
            learning_epoch=_current_learning_epoch(),
            cursor=None,
            limit=30,
        ),
        _proposal_candidates_statement(100),
        _proposal_releases_statement(),
        _proposal_reports_statement(),
        _proposal_activations_statement(),
        _active_agent_statement(),
        ReviewReadStatement(name="news_review_market", sql=market_sql, params=market_params),
    )


__all__ = [
    "READER_CONTRACT_SHA256",
    "READER_CONTRACT_TEXT",
    "READER_CONTRACT_VERSION",
    "REVIEW_RUBRIC_VERSION",
    "BlindPairwiseSubmission",
    "DeskQuery",
    "EventRubricSubmission",
    "ExternalMissSubmission",
    "Principal",
    "ReviewDesk",
    "ReviewReadStatement",
    "ReviewSubmission",
    "TaskRef",
    "review_read_statements",
]
