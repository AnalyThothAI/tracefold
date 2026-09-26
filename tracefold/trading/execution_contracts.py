"""Engine-neutral facts crossing the Tracefold/Nautilus process boundary.

These values carry Alpha and operator intent into an execution Runtime and carry
normalized audit observations back.  They deliberately contain no sizing,
account, venue-order, protection, or OMS state.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
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
# Stable execution stream identity retained for existing journal history.
EXECUTION_STRATEGY_ID = "oi_nautilus_v1"
_METADATA_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_MAX_METADATA_ENTRIES = 16
_MAX_METADATA_BYTES = 2_048
_MAX_METADATA_STRING_LENGTH = 256
_MAX_OPERATOR_INTENT_TTL_NS = 3_600_000_000_000

MetadataScalar = str | int | bool
ExecutionAction = Literal[
    "pause_entries",
    "resume_entries",
    "emergency_halt",
    "flatten",
    "manual_entry",
]
# What a Runtime writes, and nothing else (#680): the verdicts on its two inputs, the day-start and
# exposure risk facts, and the Nautilus order, fill, position and protective-order events. The private
# account proof (`reconciliation`), the lifecycle stages (`readiness`) and the audit-loss records
# (`audit_gap`) went with the machinery that wrote them; Nautilus reconciles the venue itself.
ObservationKind = Literal[
    "signal_disposition",
    "control_disposition",
    "risk",
    "order",
    "fill",
    "position",
    "protection",
    "funding",
    "funding_coverage",
    "native_fill",
    "native_fill_cost",
    "native_fill_binding",
    "native_order_result",
]


class _FrozenContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False, strict=True)


def market_key(base_symbol: str) -> str:
    """The venue-neutral perpetual market identity carried across the execution boundary.

    Analysis spells it onto each Signal and the Runtime's route catalogue spells it
    onto every instrument it can reach; the entry path joins the two by string equality. Both sides
    used to write the same f-string, so one of them could be edited alone and every Signal would be
    answered `instrument_unmapped` with no test red anywhere (#604 T2). It lives here, beside the
    `MARKET_KEY_PATTERN` the result has to satisfy.
    """

    return f"crypto:perp:{base_symbol}:USDT"


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


class SignalExitPlanV1(_FrozenContract):
    version: Literal["analysis_dynamic_v1"] = "analysis_dynamic_v1"
    stop_distance_bps: int = Field(ge=1, le=5_000)
    take_profit_bps: int = Field(ge=1, le=20_000)
    max_holding_ns: int = Field(gt=0, le=86_400_000_000_000)


class SignalEntryEnvelopeV3(_FrozenContract):
    version: Literal["entry_envelope_v3"] = "entry_envelope_v3"
    plan_id: str = Field(pattern=SHA256_PATTERN)
    entry_kind: Literal["immediate_entry_v1", "closed_bar_cross_v1"]
    root_expires_at_ns: int = Field(gt=0)
    reference_price: Decimal = Field(gt=0)
    structure_level: Decimal | None = Field(default=None, gt=0)
    parent_plan_id: str | None = Field(default=None, pattern=SHA256_PATTERN)
    max_price_drift_bps: int = Field(ge=1, le=2_000)
    universe_version: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_kind(self) -> Self:
        conditional = self.entry_kind == "closed_bar_cross_v1"
        if conditional != (self.structure_level is not None and self.parent_plan_id is not None):
            raise ValueError("entry_envelope_condition_invalid")
        return self


def entry_structure_allows(*, direction: Literal["long", "short"], executable: Decimal, level: Decimal) -> bool:
    return executable > level if direction == "long" else executable < level


def entry_condition_allows(
    *,
    direction: Literal["long", "short"],
    executable: Decimal,
    envelope: SignalEntryEnvelopeV3,
) -> bool:
    """Apply a structure gate only when the frozen plan actually declares one."""
    if envelope.entry_kind == "immediate_entry_v1":
        return True
    level = envelope.structure_level
    return level is not None and entry_structure_allows(direction=direction, executable=executable, level=level)


class TradeSignalV3(_FrozenContract):
    """A selected v4 Analysis plan with explicit immediate or activated condition semantics."""

    signal_version: Literal["trade_signal_v3"] = "trade_signal_v3"
    seq: int = Field(ge=1)
    signal_id: str = Field(pattern=SHA256_PATTERN)
    case_id: str = Field(min_length=1, max_length=128)
    decision_id: str = Field(pattern=SHA256_PATTERN)
    account_slot: str = Field(pattern=IDENTITY_PATTERN)
    entry_scope_id: str = Field(pattern=SHA256_PATTERN)
    asset_id: str = Field(pattern=r"^crypto:[A-Z0-9._-]{1,32}$")
    market_key: str = Field(pattern=MARKET_KEY_PATTERN)
    native_symbol: str = Field(pattern=r"^[A-Z0-9]{2,32}$")
    mapping_semantics_digest: str = Field(pattern=SHA256_PATTERN)
    direction: Literal["long", "short"]
    observed_at_ns: int = Field(gt=0)
    expires_at_ns: int = Field(gt=0)
    exit_plan: SignalExitPlanV1
    entry_envelope: SignalEntryEnvelopeV3

    @field_validator("case_id")
    @classmethod
    def validate_case_id(cls, value: str) -> str:
        if not postgres_text_valid(value):
            raise ValueError("trade_signal_case_invalid")
        return value

    @model_validator(mode="after")
    def validate_v3(self) -> Self:
        if self.expires_at_ns <= self.observed_at_ns:
            raise ValueError("trade_signal_clock_invalid")
        if self.expires_at_ns > self.entry_envelope.root_expires_at_ns:
            raise ValueError("trade_signal_root_expiry_invalid")
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
        if self.action == "manual_entry":
            if self.market_key is None or self.direction is None:
                raise ValueError("operator_manual_entry_market_required")
        elif self.market_key is not None or self.direction is not None:
            raise ValueError("operator_control_market_not_allowed")
        return self


class ExecutionObservationV1(_FrozenContract):
    """A bounded append-only audit projection of a native Runtime event.

    It carried `runtime_release` too -- one build-time literal, identical on every row this Runtime
    has ever written, stored as a column and again inside `payload`, and read by nothing but the
    `/execution/observations` JSON. `account_slot` and `execution_strategy` are the two identities a
    reader actually correlates on, and both are still here (#537 PR-4).
    """

    observation_version: Literal["execution_observation_v1"] = "execution_observation_v1"
    event_id: str = Field(pattern=SHA256_PATTERN)
    account_slot: str = Field(pattern=IDENTITY_PATTERN)
    execution_strategy: str = Field(pattern=IDENTITY_PATTERN)
    signal_id: str | None = Field(default=None, pattern=SHA256_PATTERN)
    command_id: str | None = Field(default=None, pattern=SHA256_PATTERN)
    normalized_kind: ObservationKind
    occurred_at_ns: int = Field(gt=0)
    observed_at_ns: int = Field(gt=0)
    native_identity_references: tuple[str, ...] = Field(default=(), max_length=16)
    summary: dict[str, MetadataScalar] = Field(default_factory=dict)

    @field_validator("native_identity_references", mode="before")
    @classmethod
    def validate_native_references(cls, value: object) -> tuple[str, ...]:
        """Normalize the reference set here, because nothing downstream checks it again.

        Nautilus hands these out in whatever order the callback saw them and in whatever case the
        venue uses -- lower-case client order ids beside upper-case Binance position ids. Sorting and
        de-duplicating is the contract's job now that no CHECK restates it (#520 PR-C); what stays a
        refusal is content the store cannot hold.
        """

        if not isinstance(value, list | tuple) or any(not isinstance(item, str) for item in value):
            raise ValueError("execution_observation_native_identity_invalid")
        references = tuple(sorted(set(value)))
        if any(not item or len(item) > 256 or not postgres_text_valid(item) for item in references):
            raise ValueError("execution_observation_native_identity_invalid")
        if _jsonb_text_size(references) > 4_096:
            raise ValueError("execution_observation_native_identity_invalid")
        return references

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
        if self.normalized_kind in {"native_fill", "native_fill_cost", "native_fill_binding", "native_order_result"}:
            self._validate_native_fact()
        # A `signal_disposition` that also carried a command id, and a `control_disposition` that also
        # carried a signal id, were two more `correlation_ambiguous` raises here. Neither could fire:
        # the mutual-exclusion rule above rejects any row holding both identities before either kind
        # rule is reached, and each kind rule requires the identity of its own half (#589 PR-2).
        return self

    def _validate_native_fact(self) -> None:
        import re
        from decimal import Decimal, InvalidOperation

        summary = self.summary
        if (
            summary.get("venue_environment") not in {"LIVE", "DEMO", "TESTNET"}
            or not isinstance(summary.get("native_instrument"), str)
            or re.fullmatch(r"[A-Z0-9]+", str(summary["native_instrument"])) is None
            or re.fullmatch(r"[1-9][0-9]*", str(summary.get("venue_order_id", ""))) is None
        ):
            raise ValueError("execution_native_identity_invalid")
        if not isinstance(summary.get("venue_order_id"), str):
            raise ValueError("execution_native_identity_invalid")
        if self.normalized_kind == "native_order_result":
            if (
                self.signal_id is not None
                or self.command_id is not None
                or summary.get("source") != "signed_order_trades_v1"
                or summary.get("status") not in {"FILLED", "CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED"}
                or type(summary.get("trade_count")) is not int
                or int(str(summary["trade_count"])) < 0
                or not isinstance(summary.get("trade_digest"), str)
                or re.fullmatch(r"[0-9a-f]{64}", str(summary["trade_digest"])) is None
            ):
                raise ValueError("execution_native_order_result_invalid")
            try:
                raw = summary.get("executed_quantity")
                quantity = Decimal(raw) if isinstance(raw, str) else Decimal("NaN")
            except InvalidOperation as exc:
                raise ValueError("execution_native_order_result_invalid") from exc
            if not quantity.is_finite() or quantity < 0 or ((quantity == 0) != (summary["trade_count"] == 0)):
                raise ValueError("execution_native_order_result_invalid")
            if (
                quantity == 0
                and summary["trade_digest"] != "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
            ):
                raise ValueError("execution_native_order_result_invalid")
            text = format(quantity, "f")
            summary["executed_quantity"] = (
                "0" if quantity == 0 else text.rstrip("0").rstrip(".") if "." in text else text
            )
            return
        if (
            not isinstance(summary.get("native_trade_id"), str)
            or re.fullmatch(r"0|[1-9][0-9]*", str(summary.get("native_trade_id", ""))) is None
        ):
            raise ValueError("execution_native_identity_invalid")
        decimal_fields = (
            ("last_quantity", "last_price")
            if self.normalized_kind == "native_fill"
            else ("commission",)
            if self.normalized_kind == "native_fill_cost"
            else ()
        )
        for key in decimal_fields:
            raw = summary.get(key)
            try:
                value = Decimal(raw) if isinstance(raw, str) else Decimal("NaN")
            except InvalidOperation as exc:
                raise ValueError("execution_native_economics_invalid") from exc
            if not value.is_finite() or (key != "commission" and value <= 0):
                raise ValueError("execution_native_economics_invalid")
            text = format(value, "f")
            summary[key] = "0" if value == 0 else text.rstrip("0").rstrip(".") if "." in text else text
        if self.normalized_kind == "native_fill" and summary.get("side") not in {"BUY", "SELL"}:
            raise ValueError("execution_native_side_invalid")
        if self.normalized_kind == "native_fill_cost" and (
            not isinstance(summary.get("commission_currency"), str) or not summary["commission_currency"]
        ):
            raise ValueError("execution_native_cost_invalid")
        correlated = self.signal_id is not None or self.command_id is not None
        if (self.normalized_kind == "native_fill_binding") != correlated:
            raise ValueError("execution_native_binding_invalid")
        if self.normalized_kind == "native_fill_binding":
            if not isinstance(summary.get("client_order_id"), str) or not summary["client_order_id"]:
                raise ValueError("execution_native_binding_invalid")
            purpose = {"entry": None, "stop": "stop_filled", "take_profit": "take_profit"}
            leg = summary.get("leg")
            if not isinstance(leg, str):
                raise ValueError("execution_native_binding_invalid")
            reason = summary.get("exit_reason")
            if (
                leg not in {"entry", "stop", "take_profit", "exit"}
                or (leg in purpose and reason != purpose[leg])
                or (leg == "exit" and reason not in {"time_exit", "operator_flatten"})
            ):
                raise ValueError("execution_native_binding_invalid")


__all__ = [
    "EXECUTION_STRATEGY_ID",
    "IDENTITY_PATTERN",
    "MARKET_KEY_PATTERN",
    "MAX_OBSERVATION_APPEND_BATCH",
    "MAX_OBSERVATION_APPEND_BYTES",
    "SHA256_PATTERN",
    "ExecutionObservationV1",
    "OperatorIntentV1",
    "SignalEntryEnvelopeV3",
    "SignalExitPlanV1",
    "TradeSignalV3",
    "entry_condition_allows",
    "market_key",
    "postgres_text_valid",
]
