"""Closed operator command grammar for the engine-neutral execution boundary.

The parser accepts only operator intent.  It never accepts quantity, notional,
leverage, order type, venue, or any other capital parameter.

Authority is the authenticated ingress plus a reason; a typed `CONFIRM` suffix was a second one
that stood between the operator and the two commands that *reduce* risk (#520 PR-B).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


@dataclass(frozen=True, slots=True)
class ControlEffect:
    paused: bool | None = None
    halted: bool | None = None
    flatten: bool = False


CONTROL_EFFECTS = {
    "pause_entries": ControlEffect(paused=True),
    "emergency_halt": ControlEffect(paused=True, halted=True),
    "flatten": ControlEffect(paused=True, flatten=True),
    "resume_entries": ControlEffect(paused=False, halted=False),
    "manual_entry": ControlEffect(),
}
ENTRY_STOP_ACTIONS = tuple(action for action, effect in CONTROL_EFFECTS.items() if effect.paused is True)


def control_entry_block(control: Any, *, unexpected_exposure: bool = False) -> str | None:
    if unexpected_exposure:
        return "unexpected_exposure"
    if control["emergency_halted"]:
        return "emergency_halt"
    if control.get("flatten_command_id") is not None:
        return "flatten_in_progress"
    if control["entries_paused"]:
        return "entries_paused"
    return None


_COMMAND_MAX_BYTES = 1_024
_CONTROL_TTL_SECONDS = 300
_SHORT_TTL_MIN_SECONDS = 5
_SHORT_TTL_MAX_SECONDS = 120
_MARKET_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_-]{0,127}$")
# How far ahead of the ingress's own clock a caller-sealed `requested_at_ns` may be. One number, so
# the HTTP console and the local CLI cannot disagree about which sealed commands are from the future.
_MAX_FUTURE_SKEW_NS = 30_000_000_000


class OperatorIntentV1(BaseModel):
    """Authenticated, expiring operator input with no capital or order parameters."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False, strict=True)

    intent_version: Literal["operator_intent_v1"] = "operator_intent_v1"
    seq: int = Field(ge=1)
    command_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    account_slot: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9:._/-]{0,127}$")
    action: Literal["pause_entries", "resume_entries", "emergency_halt", "flatten", "manual_entry"]
    scope: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=256)
    operator_identity: str = Field(min_length=1, max_length=128)
    authentication_identity: str = Field(min_length=1, max_length=256)
    requested_at_ns: int = Field(gt=0)
    expires_at_ns: int = Field(gt=0)
    market_key: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9:_-]{0,127}$")
    direction: Literal["long", "short"] | None = None

    @field_validator("scope", "reason", "operator_identity", "authentication_identity")
    @classmethod
    def validate_text_fields(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("operator_intent_text_invalid")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("operator_intent_text_invalid") from exc
        return value

    @model_validator(mode="after")
    def validate_intent(self) -> OperatorIntentV1:
        ttl_ns = self.expires_at_ns - self.requested_at_ns
        if ttl_ns <= 0 or ttl_ns > 3_600_000_000_000:
            raise ValueError("operator_intent_clock_invalid")
        if self.action == "manual_entry":
            if self.market_key is None or self.direction is None:
                raise ValueError("operator_manual_entry_market_required")
        elif self.market_key is not None or self.direction is not None:
            raise ValueError("operator_control_market_not_allowed")
        return self


def _canonical_sha256(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class OperatorCommandError(ValueError):
    """A stable, sanitized command refusal."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ParsedOperatorCommand:
    """One parsed intent. There is no second kind of parse result.

    `/status` parsed to `kind="status"` and every caller then handed it to
    `prepare_parsed_operator_intent`, whose first act was to refuse it as
    `operator_command_has_no_intent`. A command word whose only possible outcome is the refusal of
    the one thing this grammar exists to produce is not a command (#589 PR-2); `tracefold trading
    status` and `GET /api/trading/status` are what answer that question.
    """

    action: str
    scope: str
    reason: str
    ttl_seconds: int
    market_key: str | None = None
    direction: Literal["long", "short"] | None = None


@dataclass(frozen=True, slots=True)
class PreparedOperatorIntent:
    value: OperatorIntentV1
    payload_json: str


def prepare_operator_intent(**fields: object) -> PreparedOperatorIntent:
    value = OperatorIntentV1.model_validate({"seq": 1, **fields})
    return PreparedOperatorIntent(
        value=value,
        payload_json=json.dumps(value.model_dump(mode="json", exclude={"seq"}), sort_keys=True, separators=(",", ":")),
    )


def parse_operator_command(text: str) -> ParsedOperatorCommand:
    """Parse the code-owned command language without accepting aliases or options."""

    if not isinstance(text, str) or not text or len(text.encode("utf-8")) > _COMMAND_MAX_BYTES:
        raise OperatorCommandError("operator_command_invalid")
    if text != text.strip() or "\x00" in text:
        raise OperatorCommandError("operator_command_invalid")
    tokens = text.split()
    if not tokens or not tokens[0].startswith("/"):
        raise OperatorCommandError("operator_command_invalid")
    command = tokens[0]
    if command == "/pause" and len(tokens) >= 2:
        return _control("pause_entries", "entries", " ".join(tokens[1:]))
    if command in {"/resume", "/halt"} and len(tokens) >= 2:
        action = "resume_entries" if command == "/resume" else "emergency_halt"
        scope = "entries" if command == "/resume" else "account"
        return _control(action, scope, " ".join(tokens[1:]))
    if command == "/flatten" and len(tokens) == 3 and tokens[1] == "account":
        scope = tokens[1]
        return ParsedOperatorCommand(
            action="flatten",
            scope=scope,
            reason=f"flatten {scope}",
            ttl_seconds=_short_ttl(tokens[2]),
        )
    if command in {"/long", "/short"} and len(tokens) == 3:
        direction: Literal["long", "short"] = "long" if command == "/long" else "short"
        market_key = tokens[1]
        if _MARKET_KEY.fullmatch(market_key) is None:
            raise OperatorCommandError("operator_command_market_invalid")
        return ParsedOperatorCommand(
            action="manual_entry",
            scope="market",
            reason=f"manual {direction}",
            ttl_seconds=_short_ttl(tokens[2]),
            market_key=market_key,
            direction=direction,
        )
    raise OperatorCommandError("operator_command_invalid")


def prepare_parsed_operator_intent(
    parsed: ParsedOperatorCommand,
    *,
    source: str,
    source_command_id: str,
    account_slot: str,
    operator_identity: str,
    authentication_identity: str,
    requested_at_ns: int,
    now_ns: int,
) -> PreparedOperatorIntent:
    """Bind one parsed command to an authenticated, deterministic source identity and one clock.

    `now_ns` is the ingress's own reading of the wall clock, and the two rules measured against it
    live here rather than beside each caller: a sealed request may not claim a future beyond
    `_MAX_FUTURE_SKEW_NS`, and one that has already expired is not an intent to record. The HTTP
    ingress and the CLI each carried their own copy of both, with their own skew constant, so the
    same sealed command could be accepted by one and refused by the other (#589 PR-2). The refusal
    codes are unchanged.
    """

    command_id = _canonical_sha256(
        {
            "contract": "operator-command-source-v1",
            "source": source,
            "source_command_id": source_command_id,
        }
    )
    try:
        prepared = prepare_operator_intent(
            command_id=command_id,
            account_slot=account_slot,
            action=parsed.action,
            scope=parsed.scope,
            reason=parsed.reason,
            operator_identity=operator_identity,
            authentication_identity=authentication_identity,
            requested_at_ns=requested_at_ns,
            expires_at_ns=requested_at_ns + parsed.ttl_seconds * 1_000_000_000,
            market_key=parsed.market_key,
            direction=parsed.direction,
        )
    except ValueError:
        raise OperatorCommandError("operator_command_invalid") from None
    if requested_at_ns > now_ns + _MAX_FUTURE_SKEW_NS:
        raise OperatorCommandError("operator_command_clock_invalid")
    if prepared.value.expires_at_ns <= now_ns:
        raise OperatorCommandError("operator_command_expired")
    return prepared


def _control(action: str, scope: str, reason: str) -> ParsedOperatorCommand:
    if not reason or len(reason) > 256:
        raise OperatorCommandError("operator_command_reason_invalid")
    return ParsedOperatorCommand(
        action=action,
        scope=scope,
        reason=reason,
        ttl_seconds=_CONTROL_TTL_SECONDS,
    )


def _short_ttl(value: str) -> int:
    try:
        ttl_seconds = int(value)
    except ValueError:
        raise OperatorCommandError("operator_command_ttl_invalid") from None
    if not _SHORT_TTL_MIN_SECONDS <= ttl_seconds <= _SHORT_TTL_MAX_SECONDS:
        raise OperatorCommandError("operator_command_ttl_invalid")
    return ttl_seconds


__all__ = [
    "OperatorCommandError",
    "OperatorIntentV1",
    "ParsedOperatorCommand",
    "parse_operator_command",
    "prepare_parsed_operator_intent",
]
