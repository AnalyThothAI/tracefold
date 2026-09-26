"""The operator-facing ReviewDesk: human review of what News actually sent (#112, #706).

Callers see virtual review tasks, evidence views, and append-only receipts. They do not know how Events,
verdicts, delivery truth, sampling strata, or accepted corrections are joined. Ordinary Event tasks are
deterministic and content-addressed; opening a queue never writes. A task's reader receipt is the Event's
earliest sent News intent, a legacy `first` card or an `update`.

Since #706 a review states no taxonomy and there is no candidate, pairwise or release plane to review.
The task source is still `news_review_task_source_v1`, which pairs each Event with its legacy model verdict,
so only Events that carry one are offered; the `news_review_v8` row contract it writes under still names
the two retired taxonomy keys, and `_V8_NO_TAXONOMY` states them as absent.
"""

from __future__ import annotations

import base64
import copy
import difflib
import hashlib
import json
import math

# S608 exemptions below compose fixed ReviewDesk CTE/filter fragments; every request value remains a parameter.
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from ..artifact_identity import canonical_json, canonical_sha
from ..events.identity import comparison_title as normalize_comparison_title
from ..market_review.storage import MarketReviewCohort, PriceRepository
from ..models import DROP_FACT_KINDS, FACT_KINDS, FactKind, MarketType
from ..outcome import decision_zh

REVIEW_RUBRIC_VERSION = "news_review_v8"
# v7 (#651 §7.2) makes a review task-level: a reviewer answers the questions this Event actually poses
# and leaves the rest out, instead of having to state a taxonomy, a novelty judgment and a push verdict
# before one factual defect can be recorded. Earlier rows stay append-only audit history and stay
# readable through `news_review_records_v1`; they are not eligible for a new dataset, because a v6 row
# means "every dimension below was answered" and a v7 row does not, so mixing the two contracts would
# let an absent answer read as a stated one.
# v8 (#675 §1) changes which questions exist. `magnitude` and the six `trade_*` dimensions labelled
# fields the Program no longer produces, and `fact_kind` -- a closed ten-value observation of the text --
# is what replaced them. A v7 row is audit history for the same reason a v6 row is: its `magnitude`
# label is an accepted answer about a field that is gone, and reading it as supervision for anything
# current would teach against the Program that exists. The reader contract does not move with it: v8
# pairs with `reader_contract_v3`, because what the reader is promised did not change here.
READER_CONTRACT_VERSION = "reader_contract_v3"
# This is product truth, not prompt advice.  v2 was the operator-approved
# no-quota contract: a distinct fact that satisfies push/escalate reaches
# delivery regardless of prior card volume.  v3 (#651 §6.3) keeps that and
# corrects the duplicate sentence, which still promised a reversal exemption
# `grounded_restatement` no longer honours: a card the model labels a
# restatement of an entry the reader already received is dropped whichever way
# it read the direction, and a genuine reversal is not a restatement at all --
# it is a new action, so it arrives as `progression` or `new_fact`, which is
# where the reversal exemption still lives.  Changing this text requires a new
# version and invalidates old development/validation manifests.
#
# Its "or the per-storyline budget" clause describes policy v12-v16. Policy v17
# deleted the budget (owner decision 2026-09-23), and the clause is deliberately
# left in place: this text is hashed, never sent to a model, and it is the identity
# every accepted `reader_contract_v3` review was written under. Rewriting it means
# `reader_contract_v4`, which the review CHECK function does not admit without a
# migration, which moves every review task id, and which puts every accepted v3
# label outside `accepted_event_reviews_in_window` and every v3 dataset outside
# `evaluate`. Move it with the next contract change that has to pay that cost.
READER_CONTRACT_TEXT = (
    "Audience: Chinese market-research operator.\n"
    "Coverage: crypto; global macro/geopolitics with broad risk-asset impact; US-listed securities/ADRs; "
    "watchlist names.\n"
    "Single-name boundary: a non-US unlisted/private name is held unless it is a systemic sector or macro fact.\n"
    "Delivery: every distinct fact satisfying push or escalate proceeds to delivery; prior 1h/2h/4h card counts "
    "never veto it.\n"
    "Duplicate evidence: a card restating an entry the sent-reader ledger already holds is dropped, whichever "
    "direction it reads; a normal push may also be held by a same-fact title match or the per-storyline budget, "
    "and a card reversing the newest directional entry, an escalate and the degraded fallback are exempt from "
    "those two.\n"
    "Market reaction: post-event price is discovery evidence, never reward, causality, or should-push truth.\n"
)
READER_CONTRACT_SHA256 = hashlib.sha256(READER_CONTRACT_TEXT.encode()).hexdigest()
REVIEW_TASK_VERSION = "news_review_task_v2"
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
    "unknown",
]
EvidenceRef = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]

_DIMENSIONS = {
    "factual_fidelity",
    "headline_fidelity",
    "asset_grounding",
    "direction",
    "fact_kind",
    "why_support",
    "why_value",
    "timeliness",
}
_NOVELTY = {"new_fact", "progression", "restatement", "uncertain"}
# The four `fact_kind` values the legacy decision table never pushed on their own (#679 review 10).
_NON_FACT_KINDS: Final[frozenset[str]] = DROP_FACT_KINDS
# The legacy decision table named every row `fact_kind_<kind>`; a legacy verdict's override rule is read
# back through this prefix.
_FACT_KIND_RULE_PREFIX: Final[str] = "fact_kind_"
_OWNER_BY_DIMENSION: dict[str, FirstBadOwner] = {
    "asset_grounding": "gate",
    "timeliness": "delivery",
    "direction": "triage_prompt",
    "fact_kind": "triage_prompt",
    "factual_fidelity": "triage_prompt",
    "headline_fidelity": "triage_prompt",
    "why_support": "triage_prompt",
    "why_value": "triage_prompt",
}
# The `news_review_v8` row contract (the database CHECK) requires both keys on every rubric payload. #706
# deleted the taxonomy a reviewer could state, so every new row states it as absent and human-authored.
_V8_NO_TAXONOMY: Final[dict[str, Any]] = {
    "taxonomy": None,
    "taxonomy_review": {
        "label_source": "human",
        "draft_author": "",
        "review_role": "primary",
        "adjudicates_review_id": "",
        "draft_taxonomy": None,
    },
}


# The card dimensions an explanation block is evidence about. It is supervision for the copy, so a
# submission that carries one without judging any copy dimension is describing nothing.
EXPLANATION_DIMENSIONS: Final[frozenset[str]] = frozenset(
    {"why_support", "why_value", "factual_fidelity", "headline_fidelity"}
)

# #675 §1: the six `trade_relevance_targeted_stratum` branches are gone with the relevance codes they
# read. Their labels stay for the audit rows already registered under them; the sampler cannot produce
# them again, and the cascade now opens at `delivery_ambiguous`.
_STRATUM_ZH = {
    "local_macro_false_interrupt": "局部宏观误打断（v15 及以前）",
    "systemic_macro_must_interrupt": "系统性宏观必须打断（v15 及以前）",
    "regional_direct_exception": "区域事件直接交易例外（v15 及以前）",
    "scheduled_or_in_line_macro": "计划内或符合预期宏观（v15 及以前）",
    "color_only_progression": "仅补充背景的后续（v15 及以前）",
    "macro_random_control": "宏观随机对照（v15 及以前）",
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
}
_SELECTION_REASON_ZH = {
    # Retired with the six relevance strata above; kept so an audit row still renders (#675 §1).
    "trade_relevance_targeted_stratum": "按交易相关性边界定向抽样（v15 及以前）",
    "macro_coverage_control": "宏观交易相关性随机对照（v15 及以前）",
    "delivery_truth_unknown": "投递结果无法确定",
    "delivery_terminal_failure": "投递已明确失败",
    "semantic_escalation": "语义判断为即时重点推送",
    "duplicate_or_historical_throttle": "同事实重复或历史版本数量拦截",
    "sent_quality_sample": "从真实送达中抽样",
    "market_discovery_only": "仅因事后波动进入发现队列",
    "semantic_or_policy_hold": "语义或策略判断不送达",
    "upstream_recall_sample": "入口召回抽样",
    "coverage_control": "随机覆盖对照",
}


class Principal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    subject: str = Field(min_length=1, max_length=64)
    can_review: bool = True


class DeskQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    view: Literal["queue", "coverage", "market"] = "queue"
    cohort: str = Field(default="", max_length=160)
    stratum: str = Field(default="", max_length=64)
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
    equivalent_targets: tuple[str, ...] = Field(default=(), max_length=32)
    duplicate_of: str = Field(default="", max_length=128)

    @model_validator(mode="after")
    def require_duplicate_for_restatement(self) -> NoveltyJudgment:
        if self.judgment == "restatement" and not self.duplicate_of.strip():
            raise ValueError("news_review_duplicate_of_required")
        if any(
            not target.strip() or target != target.strip() or len(target) > 128 for target in self.equivalent_targets
        ):
            raise ValueError("news_review_equivalent_target_invalid")
        if self.judgment != "restatement" and (self.duplicate_of.strip() or self.equivalent_targets):
            raise ValueError("news_review_duplicate_of_not_allowed")
        return self


class ExpectedAsset(BaseModel):
    """One asset a reviewer states as the correct answer, in the market vocabulary (#651 §6.2).

    Required, because a symbol without a market is not an answer to "which instrument is this about":
    `SEI/crypto` and `SEI/equity` were byte-identical gold, so a candidate that named the wrong one of
    the two scored a hit. A review accepted before #651 says nothing here and reads as `unknown`, which
    cannot contradict and therefore still scores exactly as it did.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str = Field(min_length=1, max_length=32)
    market_type: MarketType
    role: Literal["primary", "mentioned"] = "primary"


class ExpectedCorrection(BaseModel):
    """The reviewer's stated correct values — `news_review_v6` exact gold.

    Without this the metric can only ask "did the candidate change the field the reviewer failed?", which
    scores a coin flip as highly as a repair. Every field is optional because a reviewer often knows one
    answer and not the others, and because the copy dimensions (`why_*`, `headline_fidelity`) have no value a
    rubric could hold — "the correct Chinese sentence" is not a label.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    # #675 §1: three corrections, one per semantic dimension a reviewer can fail. `magnitude` and the
    # six `trade_*` fields left with the Program output they corrected; `fact_kind` is the closed
    # vocabulary that replaced them, and it is what gives the understanding target its Gold.
    direction: Literal["bullish", "bearish", "neutral", "unclear"] | None = None
    assets: list[ExpectedAsset] | None = Field(default=None, max_length=16)
    fact_kind: FactKind | None = None

    # No `novelty` field: the accepted novelty already *is* gold — `novelty.judgment` is the reviewer's own
    # answer, not a pass/fail on someone else's — and the metric scores against it directly. A second place to
    # state the same thing could only disagree with the first.
    #
    # `should_reach_reader` is deliberately absent for the same reason: `should_push` already carries it, with
    # the must/should distinction the hard gates depend on.


ExplanationErrorType = Literal[
    "entity",
    "number_unit",
    "condition",
    "status_plan_vs_executed",
    "attribution",
    "unsupported_cause",
    "other",
]


class ExplanationCorrectionV1(BaseModel):
    """What a reviewer knows about the *why* of one card, in a form a ruler can score (#651 §7.2).

    `why_support` has never had gold. "The correct Chinese sentence" is not a label, so a failed
    `why_support` could only teach "change something", and an optimizer banks that by rewriting one wrong
    sentence into another. This block states the three things a reviewer actually knows and a metric can
    check without a second opinion: which spans of the frozen evidence carry the claim, which facts the
    card must keep, and which assertions it must not make.

    `source_spans` are verbatim excerpts, validated at submit against the task's own evidence, because a
    span nobody can find in the evidence is not a citation. `reference_why_zh` is explicitly *not* gold:
    it is one reviewer's phrasing, kept so a later reader can see what they had in mind, and no metric
    may score equality against it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_spans: list[str] = Field(default_factory=list, max_length=8)
    key_facts: list[str] = Field(default_factory=list, max_length=6)
    forbidden_claims: list[str] = Field(default_factory=list, max_length=6)
    error_types: list[ExplanationErrorType] = Field(default_factory=list, max_length=7)
    reference_why_zh: str = Field(default="", max_length=500)

    @field_validator("source_spans", "key_facts", "forbidden_claims", mode="after")
    @classmethod
    def non_empty_bounded_entries(cls, value: list[str]) -> list[str]:
        cleaned = [entry.strip() for entry in value]
        if any(not entry for entry in cleaned):
            raise ValueError("news_review_explanation_entry_empty")
        if any(len(entry) > 500 for entry in cleaned):
            raise ValueError("news_review_explanation_entry_too_long")
        return cleaned

    @field_validator("error_types", mode="after")
    @classmethod
    def distinct_error_types(cls, value: list[ExplanationErrorType]) -> list[ExplanationErrorType]:
        if len(set(value)) != len(value):
            raise ValueError("news_review_explanation_duplicate_error_type")
        return value

    @model_validator(mode="after")
    def states_something(self) -> ExplanationCorrectionV1:
        if not (self.source_spans or self.key_facts or self.forbidden_claims or self.error_types):
            raise ValueError("news_review_explanation_must_state_a_value")
        return self


class EventRubricSubmission(BaseModel):
    """One review of one task, answering only what this task actually poses (#651 §7.2).

    Every field except `dimensions` is optional, and that is the whole change from v6. v6 required a
    complete taxonomy, a novelty judgment, a push verdict and `factual_fidelity` on every submission, so
    a reviewer who had noticed one wrong number had to invent four taxonomy axes and a delivery opinion
    before the defect could be recorded -- and every one of those invented answers then entered the
    corpus as accepted truth. What a reviewer leaves out is now absent rather than defaulted: nothing
    downstream may read a missing answer as a `pass`. Since #706 there is no taxonomy to state at all.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["event_rubric"] = "event_rubric"
    should_push: ShouldPush | None = None
    dimensions: dict[str, DimensionResult]
    novelty: NoveltyJudgment | None = None
    first_bad_owner: FirstBadOwner | None = None
    evidence_refs: list[EvidenceRef] = Field(default_factory=list, max_length=32)
    expected: ExpectedCorrection | None = None
    explanation: ExplanationCorrectionV1 | None = None
    # Server-derived, never accepted from the body: whether this review carries the explanation a
    # `why_support` failure needs to be actionable. `pending` rows are stored, visible and countable.
    explanation_supervision: Literal["present", "pending", "not_applicable"] = "not_applicable"
    expected_correction: str = Field(default="", max_length=2_000)
    note: str = Field(default="", max_length=2_000)

    @model_validator(mode="before")
    @classmethod
    def derive_explanation_supervision(cls, value: Any) -> Any:
        """Compute the supervision state here, so a caller cannot declare one the payload contradicts."""

        if not isinstance(value, Mapping):
            return value
        dimensions = value.get("dimensions")
        why_support = dict(dimensions).get("why_support") if isinstance(dimensions, Mapping) else None
        if value.get("explanation") is not None:
            state = "present"
        elif why_support == "fail":
            state = "pending"
        else:
            state = "not_applicable"
        return {**value, "explanation_supervision": state}

    @model_validator(mode="after")
    def validate_rubric(self) -> EventRubricSubmission:
        unknown = set(self.dimensions) - _DIMENSIONS
        if unknown:
            raise ValueError(f"news_review_dimension_unknown:{sorted(unknown)[0]}")
        if not self.dimensions and self.novelty is None and self.should_push is None:
            raise ValueError("news_review_dimensions_required")
        # Gold is a repair instruction. Stating one for a dimension the reviewer passed would silently move the
        # accepted value, which is the one thing an append-only review plane must never let a submission do.
        if self.expected is not None:
            for field, dimension in (
                ("direction", "direction"),
                ("assets", "asset_grounding"),
                ("fact_kind", "fact_kind"),
            ):
                if getattr(self.expected, field) is not None and self.dimensions.get(dimension) != "fail":
                    raise ValueError(f"news_review_expected_requires_failed_dimension:{dimension}")
            if self.expected.model_dump(exclude_none=True) == {}:
                raise ValueError("news_review_expected_must_state_a_value")
        if self.explanation is not None and not (set(self.dimensions) & EXPLANATION_DIMENSIONS):
            raise ValueError("news_review_explanation_requires_card_dimension")
        if self.should_push in {"must_push", "should_push"} and "timeliness" not in self.dimensions:
            raise ValueError("news_review_timeliness_required_for_push")
        if any(value == "fail" for value in self.dimensions.values()) and not self.evidence_refs:
            raise ValueError("news_review_fail_evidence_ref_required")
        return self


class ExternalMissSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["external_miss"] = "external_miss"
    source_url: str = Field(min_length=1, max_length=2_000)
    title: str = Field(min_length=1, max_length=1_000)
    body: str = Field(default="", max_length=REVIEW_BODY_TEXT_MAX)
    occurred_at_ms: int = Field(ge=0)
    rubric: EventRubricSubmission


ReviewSubmission = EventRubricSubmission | ExternalMissSubmission


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
    cohort_sha: str | None,
    cursor: tuple[int, str] | None,
    limit: int,
) -> ReviewReadStatement:
    """The review queue over one closed window, optionally narrowed to one Agent cohort.

    `cohort_sha` is a filter an operator may ask for, not a fence (#651 §9). It used to be mandatory and
    defaulted to the running bundle, which meant every deployment emptied the queue: the Events that most
    needed review were the ones the *previous* arm had answered, and they became unreachable the moment a
    new arm was appointed.
    """

    filters = [
        "opened_at_ms >= %s",
        "opened_at_ms < %s",
        "ingest_mode = 'live'",
    ]
    params: list[Any] = [int(lower_ms), int(upper_ms)]
    if cohort_sha is not None:
        filters.append("COALESCE(trace #>> '{agent_assignment,bundle_sha}', '') = %s")
        params.append(cohort_sha)
    if cursor is not None:
        filters.append("(opened_at_ms, event_id) < (%s, %s)")
        params.extend(cursor)
    params.append(int(limit))
    return ReviewReadStatement(
        # Two shapes, two names: the audit plans both the unfiltered queue an operator opens by default
        # and the narrowed one they get by naming a cohort, and a shared name would plan only one.
        name="news_review_task_queue" if cohort_sha is None else "news_review_task_queue_cohort",
        sql=f"""
            SELECT * FROM news_review_task_source_v1
             WHERE {" AND ".join(filters)}
             ORDER BY opened_at_ms DESC, event_id DESC
             LIMIT %s
        """,  # noqa: S608
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


# No current-epoch CTE (#651 §9). `news_learning_epochs` and `news_review_active_agent_v1` remain the
# runtime's own identity and audit rows, and the release plane still reads them; the review plane does
# not. A review is about the words a reader saw, and clamping the queue and the coverage funnel to the
# epoch the running bundle opened meant every deploy reset the visible corpus to zero — the corpus that
# reviewers had just spent the previous days building.


def _coverage_statement(*, lower_ms: int, upper_ms: int, cohort_sha: str | None) -> ReviewReadStatement:
    filters = [
        "source.opened_at_ms >= %s",
        "source.opened_at_ms < %s",
        "source.ingest_mode = 'live'",
    ]
    params: list[Any] = [int(lower_ms), int(upper_ms)]
    if cohort_sha is not None:
        filters.append("COALESCE(source.trace #>> '{agent_assignment,bundle_sha}', '') = %s")
        params.append(cohort_sha)
    return ReviewReadStatement(
        name="news_review_coverage_source",
        sql=f"""
            SELECT source.*
              FROM news_review_task_source_v1 source
             WHERE {" AND ".join(filters)}
        """,  # noqa: S608
        params=tuple(params),
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
        return self._market(query)

    def evidence(self, task: TaskRef, *, principal: Principal, source_only: bool = False) -> dict[str, Any]:
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
            if source_only:
                return source_only_event_projection(row)
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
                "duplicate_hints": self._duplicate_hints(row),
                "rubric": _rubric_contract(row),
                "versions": {
                    "rubric": REVIEW_RUBRIC_VERSION,
                    "reader_contract": READER_CONTRACT_VERSION,
                    "reader_contract_sha256": READER_CONTRACT_SHA256,
                    "evidence_sha256": row["evidence_sha256"],
                },
            }
        raise ValueError("news_review_task_kind_unsupported")

    def _duplicate_hints(self, row: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Bounded reviewer hints only; never persisted, counted, or unioned."""

        storyline_key = str(row.get("storyline_key") or "")
        if not storyline_key:
            return []
        opened_at_ms = int(row.get("opened_at_ms") or 0)
        candidates = self._conn.execute(
            "SELECT * "
            "FROM news_review_task_source_v1 "
            "WHERE event_id <> %s AND storyline_key = %s "
            "AND opened_at_ms BETWEEN %s AND %s "
            "ORDER BY opened_at_ms DESC, event_id LIMIT 50",
            (
                row["event_id"],
                storyline_key,
                opened_at_ms - 24 * 3_600_000,
                opened_at_ms + 24 * 3_600_000,
            ),
        ).fetchall()
        source_title = _comparison_title(row["evidence_snapshot"])
        ranked: list[dict[str, Any]] = []
        for candidate in candidates:
            candidate_title = _comparison_title(candidate["evidence_snapshot"])
            similarity = difflib.SequenceMatcher(None, source_title.casefold(), candidate_title.casefold()).ratio()
            if similarity < 0.35:
                continue
            ranked.append(
                {
                    "task_id": _virtual_task(candidate).task_id,
                    "event_id": str(candidate["event_id"]),
                    "evidence_version": int(candidate["evidence_version"]),
                    "evidence_sha256": str(candidate["evidence_sha256"]),
                    "comparison_title": candidate_title,
                    "similarity": round(similarity, 6),
                    "selection_reason": "same_storyline_within_24h_title_similarity",
                }
            )
        return sorted(ranked, key=lambda hint: (-hint["similarity"], hint["task_id"]))[:5]

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
        raise ValueError("news_review_submission_kind_mismatch")

    def _open_queue(self, query: DeskQuery) -> dict[str, Any]:
        if query.task:
            if query.task.startswith("evt."):
                event_id, evidence_version = _parse_event_task_id(query.task)
                task = self._event_task(event_id, evidence_version=evidence_version)
                accepted = None if task is None else self._latest_accepted(task)
                tasks = [] if task is None else [_task_public(task, accepted=accepted)]
                return self._queue_response(tasks, next_cursor=None)
            raise ValueError("news_review_task_id_invalid")
        if query.event:
            task = self._event_task(query.event)
            single_tasks = [] if task is None else [_task_public(task, accepted=self._latest_accepted(task))]
            return self._queue_response(single_tasks, next_cursor=None)

        cohort_sha = _parse_agent_cohort_sha(query.cohort) if query.cohort else None
        decoded = _decode_cursor(query.cursor) if query.cursor else None
        if decoded is None:
            upper_ms, raw_cursor = self._now_ms, None
        else:
            upper_ms, cursor_opened_at_ms, cursor_event_id = decoded
            raw_cursor = (cursor_opened_at_ms, cursor_event_id)
        lower_ms = upper_ms - int(query.hours) * 3_600_000
        eligible: list[tuple[_VirtualTask, dict[str, Any] | None]] = []
        raw_limit = min(2_000, query.limit * 50 + 100)
        # Selection can be as sparse as 2%. One raw prefix therefore cannot prove that a task page is
        # exhausted. Scan bounded, durable-time chunks until there is one item of look-ahead or the closed
        # window is actually exhausted. The returned order and the cursor now use the same relation.
        while len(eligible) <= query.limit:
            statement = _event_queue_statement(
                lower_ms=lower_ms,
                upper_ms=upper_ms,
                cohort_sha=cohort_sha,
                cursor=raw_cursor,
                limit=raw_limit,
            )
            rows = self._conn.execute(statement.sql, statement.params).fetchall()
            if not rows:
                break
            accepted_by_task = self._accepted_event_tasks([str(row["event_id"]) for row in rows])
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
                eligible.append((task, accepted))
            if len(rows) < raw_limit:
                break
            last_raw = rows[-1]
            raw_cursor = (int(last_raw["opened_at_ms"]), str(last_raw["event_id"]))
        page = eligible[: query.limit]
        public = [_task_public(task, accepted=accepted) for task, accepted in page]
        next_cursor = None
        if len(eligible) > query.limit and page:
            last = page[-1][0].row
            next_cursor = _encode_cursor(upper_ms, int(last["opened_at_ms"]), str(last["event_id"]))
        return self._queue_response(public, next_cursor=next_cursor)

    def _queue_response(self, tasks: Sequence[Mapping[str, Any]], *, next_cursor: str | None) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for task in tasks:
            key = str(task["selection"]["stratum"])
            counts[key] = counts.get(key, 0) + 1
        return {
            "view": "queue",
            "status": "ready" if tasks else "insufficient_evidence",
            "reader_contract_version": READER_CONTRACT_VERSION,
            "rubric_version": REVIEW_RUBRIC_VERSION,
            "tasks": list(tasks),
            "next_cursor": next_cursor,
            "counts": counts,
        }

    def _coverage(self, query: DeskQuery) -> dict[str, Any]:
        lower = self._now_ms - int(query.hours) * 3_600_000
        # No epoch branch any more (#651 §9). Coverage used to refuse to answer until the running
        # deployment had opened an epoch, and then clamped its window to that epoch's start, so the funnel
        # read zero for every Event the previous arm had produced. The window an operator asks for is now
        # the window they get, and `cohort` narrows it only when they ask for one.
        cohort_sha = _parse_agent_cohort_sha(query.cohort) if query.cohort else None
        statement = _coverage_statement(lower_ms=lower, upper_ms=self._now_ms, cohort_sha=cohort_sha)
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
            SELECT count(source.snapshot_id) AS n
              FROM news_review_external_source_v1 source
             WHERE source.occurred_at_ms >= %s
               AND source.occurred_at_ms < %s
            """,
            (lower, self._now_ms),
        ).fetchone()
        total = len(rows)
        evidence_ready = total > 0 and release_eligible > 0 and accepted > 0
        return {
            "view": "coverage",
            "status": "ready" if evidence_ready else "insufficient_evidence",
            "message_zh": None if evidence_ready else "证据不足：需要真实 observed evidence 和已接受复盘",
            "window": {"from_ms": lower, "to_ms": self._now_ms, "hours": query.hours},
            "funnel": {
                "received": received,
                "replayable": release_eligible,
                "reviewed": reviewed,
                "accepted": accepted,
                "total": total,
                "external_misses": int(external["n"] or 0),
            },
            "cohorts": [{"cohort": name, **data} for name, data in sorted(cohorts.items())],
            "strata": [
                {"stratum": name, "stratum_zh": _STRATUM_ZH.get(name, "未识别复盘分层"), **data}
                for name, data in sorted(strata.items())
            ],
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
        _require_grounded_source_spans(submission, _evidence_text(dict(task.row.get("evidence_snapshot") or {})))
        owner = submission.first_bad_owner or _derive_owner(submission)
        created_at = self._db_now_ms()
        payload = {**submission.model_dump(mode="json"), **copy.deepcopy(_V8_NO_TAXONOMY)}
        payload["reviewed_source"] = {
            **dict(task.row),
            "verdict_evidence_version": dict(task.row.get("trace") or {}).get("evidence_version"),
            "focus_fact_id": dict(dict(task.row.get("evidence_snapshot") or {}).get("focus_fact") or {}).get("fact_id"),
        }
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
        # The sampling reason never decides acceptance eligibility (#504 D7): a `high_reaction` task was chosen
        # because of a post-event price move, but the reviewer labels `should_push` from the evidence alone, so
        # its accepted review is corpus truth like any other stratum's.
        #
        # Nor does the running bundle (#651 §9). Eligibility is a property of the *evidence*: a frozen,
        # release-eligible observed snapshot is replayable whichever Program answered it, and which arm
        # happened to be deployed that hour says nothing about whether the reviewer read the same words.
        # The arm is recorded as provenance on the frozen case instead.
        release_eligible = bool(task.row.get("evidence_release_eligible"))
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
                _json(payload["dimensions"]),
                _json({} if submission.novelty is None else submission.novelty.model_dump(mode="json")),
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
            principal=principal,
            created_at_ms=created_at,
            release_eligible=release_eligible,
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
        _require_grounded_source_spans(rubric, _evidence_text({"title": submission.title, "body": submission.body}))
        owner = rubric.first_bad_owner or _derive_owner(rubric, external=True)
        payload = {**rubric.model_dump(mode="json"), **copy.deepcopy(_V8_NO_TAXONOMY)}
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
        # The operator's own snapshot is the evidence, and it is written in this transaction, so it is
        # eligible by construction. It used to be gated on the running epoch (#651 §9 removes that): a
        # miss the system never saw is not evidence about a bundle in the first place.
        release_eligible = True
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
              %s, %s::jsonb, %s::jsonb, %s, %s::jsonb, %s, %s, %s::jsonb, %s::jsonb, %s, %s
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
                _json({} if rubric.novelty is None else rubric.novelty.model_dump(mode="json")),
                owner,
                _json(rubric.evidence_refs),
                rubric.expected_correction,
                rubric.note,
                _json(selection),
                _json(payload),
                release_eligible,
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
            principal=principal,
            created_at_ms=created_at,
            release_eligible=release_eligible,
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
        principal: Principal,
        created_at_ms: int,
        release_eligible: bool,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO news_reviews (
              review_id, review_kind, subject_kind, task_id, task_version, event_id, evidence_version,
              external_snapshot_id, rubric_version, reader_contract_version, reviewer,
              accepts_review_id, release_eligible, created_at_ms
            ) VALUES (%s, 'acceptance', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                acceptance_id,
                subject_kind,
                task_id,
                task_version,
                event_id,
                evidence_version,
                external_snapshot_id,
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
        queue = self._open_queue(DeskQuery(status="pending", limit=1))
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
               AND a.release_eligible AND j.release_eligible
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
            "judgment_sha256": row.get("judgment_sha256"),
            "selected_context_sha256": dict(row.get("trace") or {}).get("selected_context_sha256"),
            "final_decision": row.get("final_decision"),
            "delivery_state": row.get("delivery_state"),
            "delivery_card": row.get("delivery_card"),
            "selection": selection,
            "agent": agent_identity,
        }
    )
    return _VirtualTask(task_id=task_id, task_version=task_version, row=row, selection=selection)


def source_only_event_projection(row: Mapping[str, Any]) -> dict[str, Any]:
    """Project one exact Event source without outcome, agent, or reviewer hints."""

    task = _virtual_task(row)
    source = {
        "schema": "tracefold.news.review_source_only.v1",
        "task": {
            "task_id": task.task_id,
            "task_version": task.task_version,
            "mode": "event",
            "event_id": row["event_id"],
            "evidence_version": row["evidence_version"],
        },
        "evidence": row["evidence_snapshot"],
        "evidence_sha256": row["evidence_sha256"],
    }
    return {**source, "projection_sha256": canonical_sha(source)}


def _selection(row: Mapping[str, Any]) -> dict[str, Any]:
    """Which review stratum one row belongs to, and with what probability it is offered.

    Six branches opened this cascade until #675 §1 and every one of them read a `TradeRelevanceV1`
    code. They are deleted rather than left unreachable: five claimed p=1.0 on conditions no judgment
    can meet any more, and between them they took `macro` rows out of the `delivered` and `model_drop`
    samples that the daily audit (#675 §4) is built from. The cascade now opens on delivery truth.
    """

    if row.get("delivery_error_code") == "ambiguous_after_crash":
        stratum, reason, probability = "delivery_ambiguous", "delivery_truth_unknown", 1.0
    elif row.get("delivery_state") == "terminal":
        stratum, reason, probability = "delivery_failed", "delivery_terminal_failure", 1.0
    elif row.get("final_decision") == "escalate":
        stratum, reason, probability = "critical", "semantic_escalation", 1.0
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
        "selection_version": "news_review_sampler_v4",
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
    """The questions this task poses, and which of them a reviewer may leave unanswered.

    Under v7 the list is an offer rather than a requirement (#651 §7.2): a submission has to carry at
    least one dimension and nothing more, so `required` is empty and every consumer reading this contract
    must treat an absent answer as absent.
    """

    verdict = row.get("verdict") or {}
    dimensions = ["factual_fidelity", "headline_fidelity", "why_support", "why_value"]
    if verdict.get("assets"):
        dimensions.append("asset_grounding")
    if verdict.get("direction") in {"bullish", "bearish"}:
        dimensions.append("direction")
    # Offered whenever the judgment stated one. A `news_judgment_v2` row and a degraded row carry no
    # `fact_kind`, and a reviewer cannot correct an answer that was never given (#675 §1).
    if verdict.get("fact_kind"):
        dimensions.append("fact_kind")
    dimensions.append("timeliness")
    return {
        "rubric_version": REVIEW_RUBRIC_VERSION,
        "should_push_values": ["must_push", "should_push", "should_hold", "must_hold", "uncertain"],
        "dimensions": dimensions,
        "required_dimensions": [],
        "required_fields": ["dimensions"],
        "dimension_values": ["pass", "fail", "uncertain", "not_applicable"],
        "novelty_values": sorted(_NOVELTY),
        "first_bad_owner_values": list(FirstBadOwner.__args__),  # type: ignore[attr-defined]
        "explanation": {
            "applies_to": sorted(EXPLANATION_DIMENSIONS),
            "error_types": list(ExplanationErrorType.__args__),  # type: ignore[attr-defined]
            "source_spans": "verbatim excerpts of this task\u2019s frozen evidence; checked at submit",
            "required_for": "why_support=fail, to be actionable explanation feedback",
        },
    }


def _acted_fact_kind(rule: str, verdict: Mapping[str, Any]) -> str:
    """The `fact_kind` the stored decision acted on: the override rule's, or else the verdict's own."""

    kind = rule.removeprefix(_FACT_KIND_RULE_PREFIX) if rule.startswith(_FACT_KIND_RULE_PREFIX) else ""
    return kind if kind in FACT_KINDS else str(verdict.get("fact_kind") or "")


def _verifier_flags(row: Mapping[str, Any]) -> list[dict[str, str]]:
    verdict = dict(row.get("verdict") or {})
    final = str(row.get("final_decision") or "")
    rule = str(row.get("override_rule") or "")
    # The thresholds this verdict actually ran under, not today's defaults: a stored decision
    # carries its own policy numbers (#81) so an older row is judged by the rules it obeyed.
    policy = dict(dict(row.get("trace") or {}).get("policy") or {})
    flags: list[dict[str, str]] = []
    objective_rule = rule in {
        "listing_deterministic",
        "telemetry_deterministic",
        "watchlist_objective_guard",
        "degraded_listing_objective",
        "degraded_telemetry_objective",
        "degraded_watchlist_objective",
    }
    # #675 §1: what `background_delivered` used to catch -- a card the model itself called background
    # reaching the reader anyway -- cannot happen under v16, because the model states no reader value
    # and `decide()` produces the action it names. A `statement`, `recap`, `schedule` or `promotion`
    # that still reaches a reader did so through an objective guard, which is what this flag says.
    #
    # The kind compared is the one `decide()` acted on, not the one the model wrote (#679 review 5). The
    # two differ on exactly one path: `confirmed_fact_kind` re-reads a `market_flow_price` report against
    # its own text, and the >= 5% commodity/index exception can carry a card the model called a
    # `statement` through as a `new_quantity`. `decide()` already records which kind it acted on -- a
    # `fact_kind_*` override rule names it -- so the ledger is read rather than a second copy persisted
    # beside it. A row whose rule is an objective guard has no such record and falls back to the verdict,
    # which is the case this flag was written for.
    if _acted_fact_kind(rule, verdict) in _NON_FACT_KINDS and final in {"push", "escalate"}:
        flags.append(
            {
                "code": "non_fact_delivered",
                "severity": "info" if objective_rule else "critical",
                "message_zh": (
                    "事实类型不是新事实，但命中了上架或自选标的客观保护。"
                    if objective_rule
                    else "事实类型不是新事实，却在没有客观保护时送达。"
                ),
            }
        )
    if verdict.get("novelty") == "restatement" and final in {"push", "escalate"}:
        # Only claim the exemption as the reason when the row actually ran under it.
        listing_exempt = str(row.get("admission") or "") == "listing_deterministic" and bool(
            policy.get("listing_exempt_from_duplicate")
        )
        flags.append(
            {
                "code": "restatement_delivered",
                "severity": "info" if listing_exempt else "warning",
                "message_zh": (
                    "模型称为复述，但这是交易所上/下架帧，按不同标的放行。"
                    if listing_exempt
                    else "模型称为复述，但最终送达读者。"
                ),
            }
        )
    return flags


def _derive_owner(submission: EventRubricSubmission, *, external: bool = False) -> FirstBadOwner:
    if external:
        return "receiver"
    for dimension, value in submission.dimensions.items():
        if value == "fail":
            return _OWNER_BY_DIMENSION.get(dimension, "unknown")
    if submission.novelty is not None and submission.novelty.judgment == "restatement":
        return "retrieval"
    return "unknown"


def _evidence_text(snapshot: Mapping[str, Any]) -> str:
    """Every word of one frozen evidence snapshot a reviewer could be quoting, in one haystack.

    Both shapes it is called with are here rather than in two callers: an Event snapshot
    (`focus_fact` plus `card`) and an external miss (`title` plus `body`). A span is checked against the
    snapshot the task froze, not against today's Event, so a later evidence version cannot retroactively
    ground or unground a citation that was accepted.
    """

    card = dict(snapshot.get("card") or {})
    focus = dict(snapshot.get("focus_fact") or {})
    parts = (
        focus.get("text"),
        focus.get("context"),
        card.get("leader_title"),
        card.get("leader_description"),
        card.get("raw_first_line"),
        card.get("comparison_title"),
        snapshot.get("title"),
        snapshot.get("body"),
    )
    return "\n".join(str(part) for part in parts if part)


def _collapse_whitespace(value: str) -> str:
    return " ".join(str(value).split())


def _require_grounded_source_spans(submission: EventRubricSubmission, evidence_text: str) -> None:
    """Refuse a citation the frozen evidence does not contain.

    A `source_span` exists so the explanation ruler can point at the words that support a claim. A span
    nobody can find in the evidence supports nothing, and once it is accepted it is corpus truth that a
    later reader has no way to check -- so this fails at submit rather than at freeze. Whitespace is
    collapsed on both sides, because a reviewer copying from a rendered card picks up line breaks the
    stored text does not have; nothing else about the excerpt is normalized.
    """

    explanation = submission.explanation
    if explanation is None:
        return
    haystack = _collapse_whitespace(evidence_text)
    for span in explanation.source_spans:
        if _collapse_whitespace(span) not in haystack:
            raise ValueError("news_review_explanation_source_span_not_in_evidence")


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
    return "/".join(
        [
            str(row.get("program_version") or "no_generation"),
            str(row.get("policy_version") or "no_policy"),
            str(row.get("model") or "no_model"),
        ]
    )


def _agent_identity(row: Mapping[str, Any]) -> dict[str, str]:
    """The exact current decision system behind one verdict."""

    trace = dict(row.get("trace") or {})
    assignment = dict(trace.get("agent_assignment") or {})
    bundle_sha = str(assignment.get("bundle_sha") or "")
    policy = dict(trace.get("policy") or {})
    identity = {
        "bundle_sha": bundle_sha,
        "program_version": str(row.get("program_version") or ""),
        "program_sha256": str(row.get("program_sha256") or ""),
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


def _parse_agent_cohort_sha(value: str) -> str:
    normalized = value.strip().lower()
    if not _is_sha256(normalized):
        raise ValueError("news_review_cohort_invalid")
    return normalized


def _comparison_title(snapshot: Mapping[str, Any]) -> str:
    card = dict(snapshot.get("card") or {})
    focus = dict(snapshot.get("focus_fact") or {})
    return normalize_comparison_title(
        str(card.get("comparison_title") or focus.get("text") or card.get("leader_title") or "")
    )


def _parse_event_task_id(task_id: str) -> tuple[str, int]:
    parts = task_id.split(".")
    if len(parts) != 4 or parts[0] != "evt" or not parts[1] or len(parts[3]) != 16:
        raise ValueError("news_review_task_id_invalid")
    try:
        evidence_version = int(parts[2])
    except ValueError as exc:
        raise ValueError("news_review_task_id_invalid") from exc
    return parts[1], evidence_version


def _encode_cursor(upper_ms: int, opened_at_ms: int, event_id: str) -> str:
    raw = json.dumps([int(upper_ms), int(opened_at_ms), event_id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[int, int, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        value = json.loads(raw)
        if not isinstance(value, list) or len(value) != 3:
            raise ValueError
        upper_ms, opened_at_ms, event_id = int(value[0]), int(value[1]), str(value[2])
        if upper_ms < 0 or opened_at_ms < 0 or not event_id:
            raise ValueError
        return upper_ms, opened_at_ms, event_id
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


def review_read_statements(*, now_ms: int) -> tuple[ReviewReadStatement, ...]:
    """Exact bounded ReviewDesk statements for PostgreSQL query-plan audit."""

    lower = int(now_ms) - 24 * 3_600_000
    market_sql, market_params, *_ = PriceRepository.review_statement(
        hours=24,
        now_ms=int(now_ms),
        cohort=MarketReviewCohort(
            bundle_sha256="0" * 64,
            program_version="news_semantic_program_v8",
            program_sha256="1" * 64,
            policy_version="news_triage_policy_v13",
            model="audit-model",
        ),
    )
    return (
        _event_queue_statement(
            lower_ms=lower,
            upper_ms=int(now_ms),
            cohort_sha=None,
            cursor=None,
            limit=100,
        ),
        _event_queue_statement(
            lower_ms=lower,
            upper_ms=int(now_ms),
            cohort_sha="0" * 64,
            cursor=None,
            limit=100,
        ),
        _event_task_statement("event", evidence_version=None),
        _event_task_statement("event", evidence_version=1),
        _coverage_statement(lower_ms=lower, upper_ms=int(now_ms), cohort_sha=None),
        _active_agent_statement(),
        ReviewReadStatement(name="news_review_market", sql=market_sql, params=market_params),
    )


__all__ = [
    "EXPLANATION_DIMENSIONS",
    "READER_CONTRACT_SHA256",
    "READER_CONTRACT_TEXT",
    "READER_CONTRACT_VERSION",
    "REVIEW_RUBRIC_VERSION",
    "DeskQuery",
    "EventRubricSubmission",
    "ExplanationCorrectionV1",
    "ExternalMissSubmission",
    "Principal",
    "ReviewDesk",
    "ReviewReadStatement",
    "ReviewSubmission",
    "TaskRef",
    "review_read_statements",
]
