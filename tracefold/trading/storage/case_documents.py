"""Persisted case document shapes; decimal values use lossless strings."""

from decimal import Decimal
from typing import Any

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


def assessment_view(row: Any) -> dict[str, Any]:
    return {
        "case_id": row["case_id"],
        "program_sha": row["program_sha"],
        **AssessmentDocument.model_validate(row["assessment"]).model_dump(),
    }


def policy_view(row: Any) -> list[dict[str, Any]]:
    values = []
    for document in row["policy_decisions"] or ():
        value = PolicyDocument.model_validate(document).model_dump()
        value["expected_r"] = None if value["expected_r"] is None else Decimal(value["expected_r"])
        values.append({"case_id": row["case_id"], "program_sha": row["program_sha"], **value})
    return values


def paper_view(row: Any) -> list[dict[str, Any]]:
    values = []
    for side, document in sorted((row["paper_legs"] or {}).items()):
        if document.get("side", side) != side:
            raise ValueError("trading_paper_side_mismatch")
        value = PaperDocument.model_validate(
            {key: value for key, value in document.items() if key != "side"}
        ).model_dump()
        for key in ("anchor_price", "exit_price", "gross_bps", "cost_bps", "net_r"):
            value[key] = None if value[key] is None else Decimal(value[key])
        values.append({"case_id": row["case_id"], "side": side, **value})
    return values
