"""Persisted case document shapes; decimal values use lossless strings."""

from pydantic import BaseModel, ConfigDict


class CaseDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Probabilities(CaseDocument):
    p_tp: str
    p_sl: str
    p_timeout: str


class ForecastValue(CaseDocument):
    long: Probabilities
    short: Probabilities


class DriverValue(CaseDocument):
    ref: str
    leans: str
    note: str


class AssessmentDocument(CaseDocument):
    route: str
    status: str
    forecast: ForecastValue | None
    drivers: list[DriverValue]
    notes: list[str]
    input_tokens: int | None
    output_tokens: int | None
    started_at_ms: int
    ended_at_ms: int


class PolicyDocument(CaseDocument):
    policy_id: str
    policy_version: str
    calibrator_version: str
    action: str
    reason: str
    expected_r: str | None
    publish_status: str
    signal_id: str | None
    decided_at_ms: int


class PaperDocument(CaseDocument):
    geometry_version: str
    status: str
    outcome: str | None
    reason: str | None
    anchor_at_ms: int | None
    exit_at_ms: int | None
    anchor_price: str | None
    exit_price: str | None
    gross_bps: str | None
    cost_bps: str | None
    net_r: str | None
    labeled_at_ms: int
