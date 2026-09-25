"""Frozen inputs and outputs of one analysis; no provider or storage dependency."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Action = Literal["TRADE", "NO_TRADE", "WATCH"]


class Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)


class FeatureValue(Frozen):
    feature_id: str = Field(min_length=1, max_length=96)
    value: str | None
    unit: str = Field(min_length=1, max_length=64)
    status: Literal["ok", "missing"]
    source_ref: str = Field(pattern=r"^[a-f0-9]{64}$")
    event_at_ms: int | None
    received_at_ms: int | None
    feature_version: str

    @model_validator(mode="after")
    def check_value(self) -> FeatureValue:
        if (self.status == "ok") != (
            self.value is not None and self.event_at_ms is not None and self.received_at_ms is not None
        ):
            raise ValueError("feature_value_status_inconsistent")
        return self


class FrozenEvidence(Frozen):
    snapshot_ref: str = Field(pattern=r"^[a-f0-9]{64}$")
    knowledge_cutoff_ms: int
    data_environment: Literal["live", "demo"]
    execution_environment: Literal["disabled", "paper", "live"] | None
    values: tuple[FeatureValue, ...]


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
    strategy_family: Literal["event_price_confirmation"] = "event_price_confirmation"
    strategy_version: Literal["event_price_confirmation_v1"] = "event_price_confirmation_v1"
    entry_feature_id: Literal["perp_close_1m"] = "perp_close_1m"
    entry_operator: Literal["gt", "lt"] = "gt"
    entry_level: Decimal = Decimal(0)
    entry_observed: Decimal = Decimal(0)
    previous_close: Decimal = Decimal(0)
    entry_observed_at_ms: int = 0
    source_first_visible_at_ms: int = 0
    entry_ready: bool = True
    source_gate_ready: bool = True
    watch_eligible: bool = False
    strategy_gate_reason: str | None = None

    @model_validator(mode="after")
    def check_entry(self) -> Candidate:
        crossed = (
            self.previous_close <= self.entry_level and self.entry_observed > self.entry_level
            if self.entry_operator == "gt"
            else self.previous_close >= self.entry_level and self.entry_observed < self.entry_level
        )
        crossed = crossed and self.entry_observed_at_ms > self.source_first_visible_at_ms
        if self.entry_ready != (self.source_gate_ready and crossed):
            raise ValueError("candidate_entry_state_inconsistent")
        if self.watch_eligible and (not self.source_gate_ready or self.entry_ready):
            raise ValueError("candidate_watch_state_inconsistent")
        return self


class WatchCondition(Frozen):
    kind: Literal["closed_1m_range_cross"]
    upper_level: Decimal = Field(gt=0)
    lower_level: Decimal = Field(gt=0)
    previous_close: Decimal = Field(gt=0)
    exit_plan: ExitPlan
    source_first_visible_at_ms: int
    unit: str
    frozen_at_ms: int
    expires_at_ms: int

    @model_validator(mode="after")
    def check_event(self) -> WatchCondition:
        if self.expires_at_ms <= self.frozen_at_ms:
            raise ValueError("watch_expiry_invalid")
        if self.lower_level >= self.upper_level or not self.unit:
            raise ValueError("watch_range_invalid")
        return self


class AgentAssessment(Frozen):
    assessment_version: Literal["trade_assessment_v3"] = "trade_assessment_v3"
    action: Action
    hypothesis_side: Literal["long", "short"] | None = None
    entry_candidate_id: str | None = None
    supporting_evidence: tuple[str, ...] = ()
    opposing_evidence: tuple[str, ...] = ()
    public_rationale: str = Field(min_length=1, max_length=2000)
    research_notes: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def check_action(self) -> AgentAssessment:
        if self.action == "TRADE" and not self.entry_candidate_id:
            raise ValueError("trade_candidate_required")
        if self.action != "TRADE" and self.entry_candidate_id is not None:
            raise ValueError("candidate_only_for_trade")
        return self


class Decision(Frozen):
    action: Action
    entry_candidate_id: str | None
    side: Literal["long", "short"] | None
    exit_plan: ExitPlan | None
    reason: str
    reason_code: str
    evidence_refs: tuple[str, ...]
    watch_condition: WatchCondition | None = None
    hypothesis_side: Literal["long", "short"] | None = None
    research_notes: str | None = None
