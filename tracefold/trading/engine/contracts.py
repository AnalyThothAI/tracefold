"""Frozen inputs and outputs of one analysis; no provider or storage dependency."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator

Action = Literal["TRADE", "NO_TRADE", "WATCH"]
FactorId = Literal[
    "catalyst",
    "price_structure",
    "volume_and_oi",
    "crowding",
    "entry_timing",
    "trading_cost",
]
FactorStatus = Literal["known", "unknown", "not_applicable"]


class Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)


class ExitPlan(Frozen):
    stop_distance_bps: int = Field(gt=0, le=5_000)
    take_profit_bps: int = Field(gt=0, le=20_000)
    max_holding_seconds: int = Field(gt=0, le=86_400)


class Candidate(Frozen):
    candidate_id: str = Field(min_length=1, max_length=80)
    asset_id: str = Field(min_length=1, max_length=128)
    instrument_semantics_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    side: Literal["long", "short"]
    exit_plan: ExitPlan
    required_evidence_refs: tuple[str, ...] = ()


class FactorAssessment(Frozen):
    factor_id: FactorId
    weight_bps: int = Field(ge=0, le=10_000)
    support_score: int | None = Field(default=None, ge=-100, le=100)
    status: FactorStatus
    evidence_refs: tuple[str, ...] = ()
    exclusion_reason: str | None = None

    @model_validator(mode="after")
    def check_status(self) -> FactorAssessment:
        if self.status == "known" and (self.support_score is None or not self.evidence_refs):
            raise ValueError("known_factor_requires_score_and_evidence")
        if self.status != "known" and self.support_score is not None:
            raise ValueError("unknown_factor_has_score")
        if self.status == "not_applicable" and not self.exclusion_reason:
            raise ValueError("excluded_factor_requires_reason")
        return self


class CandidateAssessment(Frozen):
    candidate_id: str
    factors: tuple[FactorAssessment, ...]

    @model_validator(mode="after")
    def check_factors(self) -> CandidateAssessment:
        expected = set(get_args(FactorId))
        seen = {factor.factor_id for factor in self.factors}
        if seen != expected or len(self.factors) != len(expected):
            raise ValueError("assessment_factors_incomplete_or_duplicate")
        if sum(factor.weight_bps for factor in self.factors) != 10_000:
            raise ValueError("assessment_weights_not_10000")
        return self


class WatchCondition(Frozen):
    kind: Literal["price_retest", "flow_turn", "new_fact", "time_check"]
    detail: str = Field(min_length=1, max_length=240)
    due_after_seconds: int = Field(ge=30, le=300)


class AgentAssessment(Frozen):
    assessment_version: Literal["trade_assessment_v1"] = "trade_assessment_v1"
    brief_sha: str = Field(pattern=r"^[a-f0-9]{64}$")
    candidate_menu_sha: str = Field(pattern=r"^[a-f0-9]{64}$")
    action: Action
    selected_candidate_id: str | None = None
    candidate_assessments: tuple[CandidateAssessment, ...] = Field(max_length=2)
    supporting_evidence: tuple[str, ...] = ()
    opposing_evidence: tuple[str, ...] = ()
    public_rationale: str = Field(min_length=1, max_length=2000)
    invalidation_conditions: tuple[str, ...] = ()
    watch_condition: WatchCondition | None = None

    @model_validator(mode="after")
    def check_action(self) -> AgentAssessment:
        if self.action == "TRADE" and not self.selected_candidate_id:
            raise ValueError("trade_candidate_required")
        if self.action != "TRADE" and self.selected_candidate_id is not None:
            raise ValueError("candidate_only_for_trade")
        if self.action == "WATCH" and self.watch_condition is None:
            raise ValueError("watch_condition_required")
        if self.action != "WATCH" and self.watch_condition is not None:
            raise ValueError("watch_condition_only_for_watch")
        return self


class CandidateScore(Frozen):
    candidate_id: str
    value: Decimal | None
    covered_weight_bps: int = Field(ge=0, le=10_000)
    kind: Literal["agent_support"] = "agent_support"
    unit: Literal["score_-100_100"] = "score_-100_100"
    version: Literal["agent_weighted_support_v1"] = "agent_weighted_support_v1"


class Decision(Frozen):
    action: Action
    selected_candidate_id: str | None
    side: Literal["long", "short"] | None
    exit_plan: ExitPlan | None
    scores: tuple[CandidateScore, ...]
    reason: str
    evidence_refs: tuple[str, ...]
    watch_condition: WatchCondition | None = None
