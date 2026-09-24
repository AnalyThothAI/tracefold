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
    strategy_family: Literal["oi_price_confirmation"] = "oi_price_confirmation"
    strategy_version: Literal["oi_price_confirmation_v1"] = "oi_price_confirmation_v1"
    entry_feature_id: Literal["perp_close_1m"] = "perp_close_1m"
    entry_operator: Literal["gte", "lte"] = "gte"
    entry_level: Decimal = Decimal(0)
    entry_observed: Decimal = Decimal(0)
    entry_observed_at_ms: int = 0
    entry_ready: bool = True
    source_gate_ready: bool = True
    watch_eligible: bool = False
    strategy_gate_reason: str | None = None

    @model_validator(mode="after")
    def check_entry(self) -> Candidate:
        crossed = (
            self.entry_observed >= self.entry_level
            if self.entry_operator == "gte"
            else self.entry_observed <= self.entry_level
        )
        if self.entry_ready != (self.source_gate_ready and crossed):
            raise ValueError("candidate_entry_state_inconsistent")
        if self.watch_eligible and (not self.source_gate_ready or self.entry_ready):
            raise ValueError("candidate_watch_state_inconsistent")
        return self


class FactorAssessment(Frozen):
    factor_id: FactorId
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
        return self


class WatchCondition(Frozen):
    kind: Literal["closed_1m_price_crosses"]
    candidate_id: str | None = None
    feature_id: str | None = None
    operator: Literal["gte", "lte"] | None = None
    level: Decimal | None = None
    unit: str | None = None
    frozen_at_ms: int
    expires_at_ms: int

    @model_validator(mode="after")
    def check_event(self) -> WatchCondition:
        if self.expires_at_ms <= self.frozen_at_ms:
            raise ValueError("watch_expiry_invalid")
        if self.kind == "closed_1m_price_crosses" and (
            not self.candidate_id
            or self.feature_id != "perp_close_1m"
            or self.operator not in ("gte", "lte")
            or self.level is None
            or not self.unit
        ):
            raise ValueError("watch_price_condition_incomplete")
        return self


class AgentAssessment(Frozen):
    assessment_version: Literal["trade_assessment_v2"] = "trade_assessment_v2"
    brief_sha: str = Field(pattern=r"^[a-f0-9]{64}$")
    candidate_menu_sha: str = Field(pattern=r"^[a-f0-9]{64}$")
    action: Action
    hypothesis_side: Literal["long", "short"] | None = None
    entry_candidate_id: str | None = None
    candidate_assessments: tuple[CandidateAssessment, ...] = Field(max_length=2)
    supporting_evidence: tuple[str, ...] = ()
    opposing_evidence: tuple[str, ...] = ()
    public_rationale: str = Field(min_length=1, max_length=2000)
    invalidation_conditions: tuple[str, ...] = ()
    observation_note: str | None = Field(default=None, max_length=500)
    watch_intent: Literal["closed_1m_price_crosses"] | None = None

    @model_validator(mode="after")
    def check_action(self) -> AgentAssessment:
        if self.action == "TRADE" and not self.entry_candidate_id:
            raise ValueError("trade_candidate_required")
        if self.action != "TRADE" and self.entry_candidate_id is not None:
            raise ValueError("candidate_only_for_trade")
        if self.action != "WATCH" and self.watch_intent is not None:
            raise ValueError("watch_intent_only_for_watch")
        return self


class CandidateScore(Frozen):
    candidate_id: str
    value: Decimal | None
    known_factors: int = Field(ge=0, le=6)
    kind: Literal["agent_support"] = "agent_support"
    unit: Literal["score_-100_100"] = "score_-100_100"
    version: Literal["agent_equal_factor_support_v2"] = "agent_equal_factor_support_v2"


class Decision(Frozen):
    action: Action
    entry_candidate_id: str | None
    side: Literal["long", "short"] | None
    exit_plan: ExitPlan | None
    scores: tuple[CandidateScore, ...]
    reason: str
    evidence_refs: tuple[str, ...]
    watch_condition: WatchCondition | None = None
    hypothesis_side: Literal["long", "short"] | None = None
    observation_note: str | None = None
