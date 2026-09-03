"""Closed operator command grammar for the engine-neutral execution boundary.

The parser accepts only operator intent.  It never accepts quantity, notional,
leverage, order type, venue, or any other capital parameter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from tracefold.trading.contracts import canonical_sha256
from tracefold.trading.storage.execution_stream import PreparedOperatorIntent, prepare_operator_intent

_COMMAND_MAX_BYTES = 1_024
_CONTROL_TTL_SECONDS = 300
_SHORT_TTL_MIN_SECONDS = 5
_SHORT_TTL_MAX_SECONDS = 120
_CONFIRMATION_TOKEN = "CONFIRM"
_MARKET_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_-]{0,127}$")


class OperatorCommandError(ValueError):
    """A stable, sanitized command refusal."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ParsedOperatorCommand:
    kind: Literal["status", "intent"]
    action: str | None = None
    scope: str | None = None
    reason: str | None = None
    ttl_seconds: int | None = None
    confirmed: bool = False
    market_key: str | None = None
    direction: Literal["long", "short"] | None = None


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
    if command == "/status" and len(tokens) == 1:
        return ParsedOperatorCommand(kind="status")
    if command == "/pause" and len(tokens) >= 2:
        return _control("pause_entries", "entries", " ".join(tokens[1:]), confirmed=False)
    if command in {"/resume", "/halt"} and len(tokens) >= 3 and tokens[-1] == _CONFIRMATION_TOKEN:
        action = "resume_entries" if command == "/resume" else "emergency_halt"
        scope = "entries" if command == "/resume" else "account"
        return _control(action, scope, " ".join(tokens[1:-1]), confirmed=True)
    if command == "/flatten" and len(tokens) == 4 and tokens[1] == "account" and tokens[-1] == _CONFIRMATION_TOKEN:
        scope = tokens[1]
        ttl_seconds = _short_ttl(tokens[2])
        return ParsedOperatorCommand(
            kind="intent",
            action="flatten",
            scope=scope,
            reason=f"flatten {scope}",
            ttl_seconds=ttl_seconds,
            confirmed=True,
        )
    if command in {"/long", "/short"} and len(tokens) == 3:
        direction: Literal["long", "short"] = "long" if command == "/long" else "short"
        market_key = tokens[1]
        if _MARKET_KEY.fullmatch(market_key) is None:
            raise OperatorCommandError("operator_command_market_invalid")
        return ParsedOperatorCommand(
            kind="intent",
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
) -> PreparedOperatorIntent:
    """Bind one parsed command to an authenticated, deterministic source identity."""

    if parsed.kind != "intent" or parsed.action is None or parsed.scope is None or parsed.reason is None:
        raise OperatorCommandError("operator_command_has_no_intent")
    if parsed.ttl_seconds is None:
        raise OperatorCommandError("operator_command_invalid")
    command_id = canonical_sha256(
        {
            "contract": "operator-command-source-v1",
            "source": source,
            "source_command_id": source_command_id,
        }
    )
    confirmation_identity = (
        canonical_sha256(
            {
                "contract": "operator-command-confirmation-v1",
                "command_id": command_id,
                "token": _CONFIRMATION_TOKEN,
            }
        )
        if parsed.confirmed
        else None
    )
    try:
        return prepare_operator_intent(
            command_id=command_id,
            account_slot=account_slot,
            action=parsed.action,
            scope=parsed.scope,
            reason=parsed.reason,
            operator_identity=operator_identity,
            authentication_identity=authentication_identity,
            requested_at_ns=requested_at_ns,
            expires_at_ns=requested_at_ns + parsed.ttl_seconds * 1_000_000_000,
            confirmation_identity=confirmation_identity,
            market_key=parsed.market_key,
            direction=parsed.direction,
        )
    except ValueError:
        raise OperatorCommandError("operator_command_invalid") from None


def _control(action: str, scope: str, reason: str, *, confirmed: bool) -> ParsedOperatorCommand:
    if not reason or len(reason) > 256:
        raise OperatorCommandError("operator_command_reason_invalid")
    return ParsedOperatorCommand(
        kind="intent",
        action=action,
        scope=scope,
        reason=reason,
        ttl_seconds=_CONTROL_TTL_SECONDS,
        confirmed=confirmed,
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
    "ParsedOperatorCommand",
    "parse_operator_command",
    "prepare_parsed_operator_intent",
]
