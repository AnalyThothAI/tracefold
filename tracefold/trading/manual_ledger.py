"""Pure append-only event contracts for the manual Trading ledger."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from .contracts import canonical_sha256


class ManualTradeEventKind(StrEnum):
    SESSION_CREATED = "SESSION_CREATED"
    STRATEGY_SELECTED = "STRATEGY_SELECTED"
    TRADE_MODIFIED = "TRADE_MODIFIED"
    HIGH_RISK_ACKNOWLEDGED = "HIGH_RISK_ACKNOWLEDGED"
    TRADE_CONFIRMED = "TRADE_CONFIRMED"
    TRADE_CANCELLED = "TRADE_CANCELLED"
    ORDER_FENCED = "ORDER_FENCED"
    ORDER_SUBMITTED = "ORDER_SUBMITTED"
    ORDER_REJECTED = "ORDER_REJECTED"
    PROTECTION_REJECTED = "PROTECTION_REJECTED"
    ORDER_AMBIGUOUS = "ORDER_AMBIGUOUS"
    ORDER_RECONCILED = "ORDER_RECONCILED"
    POSITION_OPENED = "POSITION_OPENED"
    TP_CREATED = "TP_CREATED"
    SL_CREATED = "SL_CREATED"
    POSITION_CLOSED = "POSITION_CLOSED"


class ManualTradeEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_version: Literal["manual_trade_event_v1"] = "manual_trade_event_v1"
    event_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    session_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    event_index: int = Field(gt=0)
    event_kind: ManualTradeEventKind
    payload: dict[str, JsonValue]
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at_ms: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_identity(self) -> ManualTradeEvent:
        if self.payload_sha256 != canonical_sha256(self.payload):
            raise ValueError("manual_trade_event_payload_identity_invalid")
        if self.event_id != canonical_sha256(self.identity_payload):
            raise ValueError("manual_trade_event_identity_invalid")
        return self

    @property
    def identity_payload(self) -> dict[str, object]:
        return {
            "version": self.event_version,
            "session_id": self.session_id,
            "event_index": self.event_index,
            "event_kind": self.event_kind,
            "payload_sha256": self.payload_sha256,
            "created_at_ms": self.created_at_ms,
        }


def create_manual_trade_event(
    *,
    session_id: str,
    event_index: int,
    event_kind: ManualTradeEventKind,
    payload: dict[str, JsonValue],
    created_at_ms: int,
) -> ManualTradeEvent:
    payload_sha256 = canonical_sha256(payload)
    identity = {
        "version": "manual_trade_event_v1",
        "session_id": session_id,
        "event_index": event_index,
        "event_kind": event_kind,
        "payload_sha256": payload_sha256,
        "created_at_ms": created_at_ms,
    }
    return ManualTradeEvent(
        event_id=canonical_sha256(identity),
        session_id=session_id,
        event_index=event_index,
        event_kind=event_kind,
        payload=payload,
        payload_sha256=payload_sha256,
        created_at_ms=created_at_ms,
    )


__all__ = ["ManualTradeEvent", "ManualTradeEventKind", "create_manual_trade_event"]
