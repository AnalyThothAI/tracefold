"""Frozen inputs and outputs of one analysis; no provider or storage dependency."""

from __future__ import annotations

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
    data_environment: Literal["live", "demo", "testnet"]
    values: tuple[FeatureValue, ...]


class ExitPlan(Frozen):
    stop_distance_bps: int = Field(gt=0, le=5_000)
    take_profit_bps: int = Field(gt=0, le=20_000)
    max_holding_seconds: int = Field(gt=0, le=86_400)
