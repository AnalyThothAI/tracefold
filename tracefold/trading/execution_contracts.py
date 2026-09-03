"""Engine-neutral facts crossing the Tracefold/Nautilus process boundary.

These values carry Alpha and operator intent into an execution Runtime and carry
normalized audit observations back.  They deliberately contain no sizing,
account, venue-order, protection, or OMS state.
"""

from __future__ import annotations

import json
import re
from typing import Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# The three identity shapes every durable Trading fact and every Runtime identity is checked
# against, stated once. They were re-typed in the Runtime config, the storage adapter and here, so a
# tightened bound could pass one side and be refused by the other (#510 E).
SHA256_PATTERN = r"^[0-9a-f]{64}$"
IDENTITY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9:._/-]{0,127}$"
MARKET_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9:_-]{0,127}$"
# What one durable Observation append may carry. The Runtime's in-memory flush has to stop at the
# same numbers the durable writer accepts, or a batch it assembles is refused on arrival.
MAX_OBSERVATION_APPEND_BATCH = 128
MAX_OBSERVATION_APPEND_BYTES = 1_048_576
_METADATA_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_MAX_METADATA_ENTRIES = 16
_MAX_METADATA_BYTES = 2_048
_MAX_METADATA_STRING_LENGTH = 256
_MAX_OPERATOR_INTENT_TTL_NS = 3_600_000_000_000
_HIGH_RISK_ACTIONS = frozenset({"resume_entries", "emergency_halt", "flatten"})

MetadataScalar = str | int | bool
ExecutionAction = Literal[
    "pause_entries",
    "resume_entries",
    "emergency_halt",
    "flatten",
    "manual_entry",
]
ObservationKind = Literal[
    "signal_disposition",
    "control_disposition",
    "risk",
    "order",
    "fill",
    "position",
    "protection",
    "reconciliation",
    "readiness",
    "audit_gap",
]


class _FrozenContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False, strict=True)


def postgres_text_valid(value: str) -> bool:
    """Text PostgreSQL will actually store: no NUL, encodable as UTF-8."""

    if "\x00" in value:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _jsonb_text_size(value: object) -> int:
    """Match PostgreSQL's UTF-8 `jsonb::text` separators for shared byte bounds."""

    return len(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8"))


def _validate_metadata(value: object) -> dict[str, MetadataScalar]:
    if type(value) is not dict:
        raise ValueError("execution_metadata_invalid")
    if len(value) > _MAX_METADATA_ENTRIES:
        raise ValueError("execution_metadata_invalid")
    for key, item in value.items():
        if _METADATA_KEY.fullmatch(key) is None:
            raise ValueError("execution_metadata_invalid")
        if type(item) not in (str, int, bool):
            raise ValueError("execution_metadata_invalid")
        if isinstance(item, str) and (len(item) > _MAX_METADATA_STRING_LENGTH or not postgres_text_valid(item)):
            raise ValueError("execution_metadata_invalid")
        if type(item) is int and not -(2**63) <= item < 2**63:
            raise ValueError("execution_metadata_invalid")
    if _jsonb_text_size(value) > _MAX_METADATA_BYTES:
        raise ValueError("execution_metadata_invalid")
    return cast(dict[str, MetadataScalar], value)


class TradeSignalV1(_FrozenContract):
    """A time-bounded Alpha conclusion; never an order or capital instruction."""

    signal_version: Literal["trade_signal_v1"] = "trade_signal_v1"
    seq: int = Field(ge=1)
    signal_id: str = Field(pattern=SHA256_PATTERN)
    case_id: str = Field(min_length=1, max_length=128)
    alpha_contract_sha256: str = Field(pattern=SHA256_PATTERN)
    market_key: str = Field(pattern=MARKET_KEY_PATTERN)
    direction: Literal["long", "short"]
    observed_at_ns: int = Field(gt=0)
    expires_at_ns: int = Field(gt=0)
    evidence_sha256: str = Field(pattern=SHA256_PATTERN)
    alpha_metadata: dict[str, MetadataScalar] = Field(default_factory=dict)

    @field_validator("case_id")
    @classmethod
    def validate_case_id(cls, value: str) -> str:
        if not postgres_text_valid(value):
            raise ValueError("trade_signal_case_invalid")
        return value

    @field_validator("alpha_metadata", mode="before")
    @classmethod
    def validate_alpha_metadata(cls, value: object) -> dict[str, MetadataScalar]:
        return _validate_metadata(value)

    @model_validator(mode="after")
    def validate_clock(self) -> Self:
        if self.expires_at_ns <= self.observed_at_ns:
            raise ValueError("trade_signal_clock_invalid")
        return self


class OperatorIntentV1(_FrozenContract):
    """An authenticated, expiring control or manual Alpha request."""

    intent_version: Literal["operator_intent_v1"] = "operator_intent_v1"
    seq: int = Field(ge=1)
    command_id: str = Field(pattern=SHA256_PATTERN)
    account_slot: str = Field(pattern=IDENTITY_PATTERN)
    action: ExecutionAction
    scope: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=256)
    operator_identity: str = Field(min_length=1, max_length=128)
    authentication_identity: str = Field(min_length=1, max_length=256)
    requested_at_ns: int = Field(gt=0)
    expires_at_ns: int = Field(gt=0)
    confirmation_identity: str | None = Field(default=None, pattern=SHA256_PATTERN)
    market_key: str | None = Field(default=None, pattern=MARKET_KEY_PATTERN)
    direction: Literal["long", "short"] | None = None

    @field_validator("scope", "reason", "operator_identity", "authentication_identity")
    @classmethod
    def validate_text_fields(cls, value: str) -> str:
        if not postgres_text_valid(value):
            raise ValueError("operator_intent_text_invalid")
        return value

    @model_validator(mode="after")
    def validate_intent(self) -> Self:
        ttl_ns = self.expires_at_ns - self.requested_at_ns
        if ttl_ns <= 0 or ttl_ns > _MAX_OPERATOR_INTENT_TTL_NS:
            raise ValueError("operator_intent_clock_invalid")
        if self.action in _HIGH_RISK_ACTIONS and self.confirmation_identity is None:
            raise ValueError("operator_intent_confirmation_required")
        if self.action not in _HIGH_RISK_ACTIONS and self.confirmation_identity is not None:
            raise ValueError("operator_intent_confirmation_not_allowed")
        if self.action == "manual_entry":
            if self.market_key is None or self.direction is None:
                raise ValueError("operator_manual_entry_market_required")
        elif self.market_key is not None or self.direction is not None:
            raise ValueError("operator_control_market_not_allowed")
        return self


class ExecutionObservationV1(_FrozenContract):
    """A bounded append-only audit projection of a native Runtime event."""

    observation_version: Literal["execution_observation_v1"] = "execution_observation_v1"
    event_id: str = Field(pattern=SHA256_PATTERN)
    account_slot: str = Field(pattern=IDENTITY_PATTERN)
    runtime_release: str = Field(min_length=1, max_length=128)
    execution_strategy: str = Field(pattern=IDENTITY_PATTERN)
    signal_id: str | None = Field(default=None, pattern=SHA256_PATTERN)
    command_id: str | None = Field(default=None, pattern=SHA256_PATTERN)
    normalized_kind: ObservationKind
    occurred_at_ns: int = Field(gt=0)
    observed_at_ns: int = Field(gt=0)
    native_identity_references: tuple[str, ...] = Field(default=(), max_length=16)
    summary: dict[str, MetadataScalar] = Field(default_factory=dict)
    payload_digest: str = Field(pattern=SHA256_PATTERN)

    @field_validator("runtime_release")
    @classmethod
    def validate_runtime_release(cls, value: str) -> str:
        if not postgres_text_valid(value):
            raise ValueError("execution_observation_release_invalid")
        return value

    @field_validator("native_identity_references", mode="before")
    @classmethod
    def validate_native_references(cls, value: object) -> tuple[str, ...]:
        if not isinstance(value, list | tuple) or any(not isinstance(item, str) for item in value):
            raise ValueError("execution_observation_native_identity_invalid")
        value = tuple(value)
        if value != tuple(sorted(value)) or len(set(value)) != len(value):
            raise ValueError("execution_observation_native_identity_invalid")
        if any(not item or len(item) > 256 or not postgres_text_valid(item) for item in value):
            raise ValueError("execution_observation_native_identity_invalid")
        if _jsonb_text_size(value) > 4_096:
            raise ValueError("execution_observation_native_identity_invalid")
        return value

    @field_validator("summary", mode="before")
    @classmethod
    def validate_summary(cls, value: object) -> dict[str, MetadataScalar]:
        return _validate_metadata(value)

    @model_validator(mode="after")
    def validate_observation(self) -> Self:
        if self.observed_at_ns < self.occurred_at_ns:
            raise ValueError("execution_observation_clock_invalid")
        if self.signal_id is not None and self.command_id is not None:
            raise ValueError("execution_observation_correlation_ambiguous")
        if self.normalized_kind == "signal_disposition" and self.signal_id is None:
            raise ValueError("execution_observation_signal_identity_required")
        if self.normalized_kind == "control_disposition" and self.command_id is None:
            raise ValueError("execution_observation_command_identity_required")
        if self.normalized_kind == "signal_disposition" and self.command_id is not None:
            raise ValueError("execution_observation_correlation_ambiguous")
        if self.normalized_kind == "control_disposition" and self.signal_id is not None:
            raise ValueError("execution_observation_correlation_ambiguous")
        return self


__all__ = [
    "IDENTITY_PATTERN",
    "MARKET_KEY_PATTERN",
    "MAX_OBSERVATION_APPEND_BATCH",
    "MAX_OBSERVATION_APPEND_BYTES",
    "SHA256_PATTERN",
    "ExecutionObservationV1",
    "OperatorIntentV1",
    "TradeSignalV1",
    "postgres_text_valid",
]
