"""Trading reads plus the one authenticated bounded operator-command append."""

from __future__ import annotations

import base64
import json
import re
import time
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from tracefold.app.execution_status import execution_readiness_projection
from tracefold.app.trading_config import ADMISSION_VERSION, signal_lane_config
from tracefold.news.oi_signals import METRIC_VERSION as OI_METRIC_VERSION
from tracefold.trading import (
    OperatorCommandError,
    canonical_sha256,
    parse_operator_command,
    prepare_parsed_operator_intent,
)

from ..dependencies import _authenticated_runtime, _authenticated_write_runtime, _validate_query_params
from ..exceptions import ApiBadRequest
from ..responses import _etagged, _validated_json
from ..schemas import common as api_schemas
from ..schemas import trading as trading_schemas

router = APIRouter()
_StatusEnvelope = api_schemas.ApiEnvelope[trading_schemas.TradingStatusData]
_GateEnvelope = api_schemas.ApiEnvelope[trading_schemas.TradingGateData]
_GateSourceEnvelope = api_schemas.ApiEnvelope[trading_schemas.TradingGateSourceData]
_CasesEnvelope = api_schemas.ApiEnvelope[trading_schemas.TradingCasesData]
_SignalsEnvelope = api_schemas.ApiEnvelope[trading_schemas.TradingSignalsData]
_ObservationsEnvelope = api_schemas.ApiEnvelope[trading_schemas.TradingExecutionObservationsData]
_CommandsEnvelope = api_schemas.ApiEnvelope[trading_schemas.TradingOperatorIntentsData]
_CommandReceiptEnvelope = api_schemas.ApiEnvelope[trading_schemas.TradingOperatorCommandReceiptData]

_WINDOW_MS: Final = 24 * 3_600_000
_ROW_LIMIT: Final = 100
_GATE_LIMIT: Final = 400
_OI_METRIC_VERSION: Final = OI_METRIC_VERSION
_BASE_SYMBOL: Final = re.compile(r"^[A-Z0-9._-]{1,24}$")
_MARKET_KEY: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_-]{0,127}$")
_CASE_STATE_FILTERS: Final[dict[str, tuple[str, ...]]] = {
    "open": ("PENDING", "RUNNING"),
    "no_trade": ("NO_TRADE",),
    "blocked": ("BLOCKED",),
    "emitted": ("SIGNAL_EMITTED",),
}
_OBSERVATION_KINDS: Final = frozenset(
    {
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
    }
)
_COMMAND_ACTIONS: Final = frozenset({"pause_entries", "resume_entries", "emergency_halt", "flatten", "manual_entry"})
_CONSOLE_COMMAND_ACTIONS: Final = frozenset({"pause_entries", "resume_entries", "flatten"})
_MAX_FUTURE_SKEW_NS: Final = 30_000_000_000
_MAX_COMMAND_REQUEST_BYTES: Final = 2_048
_COMMAND_REQUEST_OPENAPI: Final = {
    "requestBody": {
        "required": True,
        "content": {
            "application/json": {"schema": trading_schemas.TradingOperatorCommandRequestData.model_json_schema()}
        },
    }
}


@router.get("/trading/status", response_model=_StatusEnvelope)
def get_trading_status(request: Request) -> Response:
    _validate_query_params(request, supported={"token"})
    runtime = _authenticated_runtime(request)
    now_ms = int(time.time() * 1000)
    with runtime.repositories() as repos:
        last_case_at_ms = repos.trading.latest_case_created_at_ms()
        counts = repos.trading.runtime_summary(since_ms=now_ms - _WINDOW_MS, now_ms=now_ms)
        execution = runtime.settings.trading.execution
        execution_status = execution_readiness_projection(
            execution,
            repos.trading.execution_runtime_state(execution.account_slot),
            repos.trading.execution_runtime_control_state(execution.account_slot),
            now_ns=now_ms * 1_000_000,
        )
    config = signal_lane_config(runtime.settings)
    alpha_contract = canonical_sha256(
        {
            "policy_id": config.policy.policy_id,
            "policy_version": config.policy.policy_version,
            "policy_config": config.policy.config_snapshot,
        }
    )
    return _etagged(
        {
            "decision": {"last_case_at_ms": last_case_at_ms},
            "execution": execution_status,
            "alpha": {
                "policy_id": config.policy.policy_id,
                "policy_version": config.policy.policy_version,
                "config_digest": config.policy.config_digest,
                "contract_sha256": alpha_contract,
                "config": {key: str(value) for key, value in sorted(config.policy.config_snapshot.items())},
            },
            "counts": counts,
            "window_hours": _WINDOW_MS // 3_600_000,
            "measured_at_ms": now_ms,
        },
        request,
        envelope=_StatusEnvelope,
    )


@router.get("/trading/gate", response_model=_GateEnvelope)
def get_trading_gate(request: Request) -> Response:
    _validate_query_params(request, supported={"token"})
    runtime = _authenticated_runtime(request)
    now_ms = int(time.time() * 1000)
    with runtime.repositories() as repos:
        rows = repos.trading.gate_decisions_since(since_ms=now_ms - _WINDOW_MS, limit=_GATE_LIMIT + 1)
        report = repos.trading.candidate_admission_report(now_ms=now_ms)
    admission = signal_lane_config(runtime.settings).admission
    return _etagged(
        {
            "config": {"version": ADMISSION_VERSION, "config_digest": admission.digest, **admission.snapshot},
            "decisions": [_gate_decision(row) for row in rows[:_GATE_LIMIT]],
            "status_counts_24h": report["candidate_counts_24h"],
            "reason_counts_24h": report["candidate_reasons_24h"],
            "latest_source_at_ms": report["latest_source_at_ms"],
            "latest_gate_eligible_at_ms": report["latest_gate_eligible_at_ms"],
            "complete": len(rows) <= _GATE_LIMIT,
            "window_hours": _WINDOW_MS // 3_600_000,
            "measured_at_ms": now_ms,
        },
        request,
        envelope=_GateEnvelope,
    )


@router.get("/trading/gate/{event_id}", response_model=_GateSourceEnvelope)
def get_trading_gate_source(request: Request, event_id: str) -> Response:
    _validate_query_params(request, supported={"lane", "token"})
    if not event_id or len(event_id) > 128:
        raise ApiBadRequest("trading_event_id_invalid", field="event_id")
    lane = request.query_params.get("lane", "")
    if lane and lane != "oi":
        raise ApiBadRequest("trading_event_lane_invalid", field="lane")
    runtime = _authenticated_runtime(request)
    if lane != "oi":
        return _etagged(
            {"event_id": event_id, "joinable": False, "decision": None},
            request,
            envelope=_GateSourceEnvelope,
        )
    source_key = f"oi:{event_id}:{_OI_METRIC_VERSION}"
    with runtime.repositories() as repos:
        row = repos.trading.gate_decision_for_source_key(source_key=source_key)
    return _etagged(
        {"event_id": event_id, "joinable": True, "decision": None if row is None else _gate_decision(row)},
        request,
        envelope=_GateSourceEnvelope,
    )


@router.get("/trading/cases", response_model=_CasesEnvelope)
def get_trading_cases(
    request: Request,
    underlying: Annotated[str, Query(max_length=32)] = "",
    state: Annotated[str, Query(max_length=16)] = "",
    cursor: Annotated[str, Query(max_length=256)] = "",
) -> Response:
    _validate_query_params(request, supported={"cursor", "state", "token", "underlying"})
    if state and state not in _CASE_STATE_FILTERS:
        raise ApiBadRequest("trading_cases_state_invalid", field="state")
    underlying_key = _underlying_key(underlying, error="trading_cases_underlying_invalid")
    before = _cursor_pair(cursor, kind="cases", error="trading_cases_cursor_invalid")
    runtime = _authenticated_runtime(request)
    now_ms = int(time.time() * 1000)
    with runtime.repositories() as repos:
        rows = repos.trading.console_cases(
            since_ms=now_ms - _WINDOW_MS,
            underlying_key=underlying_key,
            states=_CASE_STATE_FILTERS.get(state, ()),
            before=before,
            limit=_ROW_LIMIT + 1,
        )
        states = repos.trading.case_counts(since_ms=now_ms - _WINDOW_MS)
        reasons = repos.trading.case_reason_counts(since_ms=now_ms - _WINDOW_MS)
    return _etagged(
        {
            "cases": [_case(row) for row in rows[:_ROW_LIMIT]],
            "state_counts_24h": states,
            "reason_counts_24h": reasons,
            "complete": len(rows) <= _ROW_LIMIT,
            "next_cursor": _next_cursor(rows, kind="cases", time_key="case_created_at_ms", id_key="case_id"),
            "window_hours": _WINDOW_MS // 3_600_000,
            "measured_at_ms": now_ms,
        },
        request,
        envelope=_CasesEnvelope,
    )


@router.get("/trading/signals", response_model=_SignalsEnvelope)
def get_trading_signals(
    request: Request,
    market: Annotated[str, Query(max_length=128)] = "",
    cursor: Annotated[str, Query(max_length=256)] = "",
) -> Response:
    _validate_query_params(request, supported={"cursor", "market", "token"})
    market_value = market.strip()
    if market_value and _MARKET_KEY.fullmatch(market_value) is None:
        raise ApiBadRequest("trading_signals_market_invalid", field="market")
    before = _cursor_pair(cursor, kind="signals", error="trading_signals_cursor_invalid")
    runtime = _authenticated_runtime(request)
    now_ms = int(time.time() * 1000)
    with runtime.repositories() as repos:
        rows = repos.trading.console_signals(
            since_ns=(now_ms - _WINDOW_MS) * 1_000_000,
            market_key=market_value or None,
            before=before,
            limit=_ROW_LIMIT + 1,
        )
    return _etagged(
        {
            "signals": [_signal(row, now_ns=now_ms * 1_000_000) for row in rows[:_ROW_LIMIT]],
            "complete": len(rows) <= _ROW_LIMIT,
            "next_cursor": _next_cursor(rows, kind="signals", time_key="observed_at_ns", id_key="signal_id"),
            "window_hours": _WINDOW_MS // 3_600_000,
            "measured_at_ms": now_ms,
        },
        request,
        envelope=_SignalsEnvelope,
    )


@router.get("/trading/execution/observations", response_model=_ObservationsEnvelope)
def get_execution_observations(
    request: Request,
    slot: Annotated[str, Query(max_length=128)] = "",
    kind: Annotated[str, Query(max_length=32)] = "",
    cursor: Annotated[str, Query(max_length=256)] = "",
) -> Response:
    _validate_query_params(request, supported={"cursor", "kind", "slot", "token"})
    if kind and kind not in _OBSERVATION_KINDS:
        raise ApiBadRequest("trading_observations_kind_invalid", field="kind")
    before = _cursor_pair(cursor, kind="observations", error="trading_observations_cursor_invalid")
    runtime = _authenticated_runtime(request)
    now_ms = int(time.time() * 1000)
    with runtime.repositories() as repos:
        rows = repos.trading.console_execution_observations(
            since_ns=(now_ms - _WINDOW_MS) * 1_000_000,
            account_slot=slot.strip() or None,
            normalized_kind=kind or None,
            before=before,
            limit=_ROW_LIMIT + 1,
        )
    return _etagged(
        {
            "observations": [_observation(row) for row in rows[:_ROW_LIMIT]],
            "complete": len(rows) <= _ROW_LIMIT,
            "next_cursor": _next_cursor(rows, kind="observations", time_key="observed_at_ns", id_key="event_id"),
            "window_hours": _WINDOW_MS // 3_600_000,
            "measured_at_ms": now_ms,
        },
        request,
        envelope=_ObservationsEnvelope,
    )


@router.get("/trading/execution/commands", response_model=_CommandsEnvelope)
def get_operator_intents(
    request: Request,
    slot: Annotated[str, Query(max_length=128)] = "",
    action: Annotated[str, Query(max_length=32)] = "",
    cursor: Annotated[str, Query(max_length=256)] = "",
) -> Response:
    _validate_query_params(request, supported={"action", "cursor", "slot", "token"})
    if action and action not in _COMMAND_ACTIONS:
        raise ApiBadRequest("trading_commands_action_invalid", field="action")
    before = _cursor_pair(cursor, kind="commands", error="trading_commands_cursor_invalid")
    runtime = _authenticated_runtime(request)
    now_ms = int(time.time() * 1000)
    now_ns = now_ms * 1_000_000
    with runtime.repositories() as repos:
        rows = repos.trading.console_operator_intents(
            since_ns=(now_ms - _WINDOW_MS) * 1_000_000,
            account_slot=slot.strip() or None,
            action=action or None,
            before=before,
            limit=_ROW_LIMIT + 1,
        )
    return _etagged(
        {
            "commands": [_command(row, now_ns=now_ns) for row in rows[:_ROW_LIMIT]],
            "complete": len(rows) <= _ROW_LIMIT,
            "next_cursor": _next_cursor(
                rows,
                kind="commands",
                time_key="requested_at_ns",
                id_key="command_id",
            ),
            "window_hours": _WINDOW_MS // 3_600_000,
            "measured_at_ms": now_ms,
        },
        request,
        envelope=_CommandsEnvelope,
    )


@router.post(
    "/trading/execution/commands",
    response_model=_CommandReceiptEnvelope,
    openapi_extra=_COMMAND_REQUEST_OPENAPI,
)
async def post_operator_intent(
    request: Request,
    runtime: Annotated[Any, Depends(_authenticated_write_runtime)],
) -> Response:
    """Persist one bounded console intent; Runtime and venue outcomes remain separate facts."""

    _validate_query_params(request, supported=set())
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > _MAX_COMMAND_REQUEST_BYTES:
            raise ApiBadRequest("operator_command_request_too_large")
    try:
        command = trading_schemas.TradingOperatorCommandRequestData.model_validate_json(bytes(body))
    except ValidationError:
        raise ApiBadRequest("operator_command_request_invalid") from None
    requested_at_ns = command.requested_at_ms * 1_000_000
    now_ns = time.time_ns()
    try:
        parsed = parse_operator_command(command.text)
        if parsed.action not in _CONSOLE_COMMAND_ACTIONS:
            raise OperatorCommandError("operator_console_action_unsupported")
        prepared = prepare_parsed_operator_intent(
            parsed,
            source="http:operator-console:v1",
            source_command_id=command.request_id,
            account_slot=runtime.settings.trading.execution.account_slot,
            operator_identity="operator-console",
            authentication_identity="http-operator-write-token:v1",
            requested_at_ns=requested_at_ns,
        )
        if requested_at_ns > now_ns + _MAX_FUTURE_SKEW_NS:
            raise OperatorCommandError("operator_command_clock_invalid")
        if prepared.value.expires_at_ns <= now_ns:
            raise OperatorCommandError("operator_command_expired")
    except OperatorCommandError as exc:
        raise ApiBadRequest(exc.code, field="text") from None
    receipt = await run_in_threadpool(runtime.persist_operator_intent, prepared)
    return _validated_json(
        _CommandReceiptEnvelope,
        {
            "ok": True,
            "data": {
                "command_id": receipt.command_id,
                "seq": receipt.seq,
                "requested_at_ns": requested_at_ns,
                "disposition": receipt.disposition,
                "reason": receipt.reason,
                "truth": "intent_recorded_not_runtime_or_venue",
            },
        },
    )


def _encode_cursor(kind: str, timestamp: int, identity: str) -> str:
    raw = json.dumps(
        {"v": 1, "kind": kind, "values": [timestamp, identity]},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _cursor_pair(cursor: str, *, kind: str, error: str) -> tuple[int, str] | None:
    if not cursor:
        return None
    try:
        padding = "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode((cursor + padding).encode()).decode())
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise ApiBadRequest(error, field="cursor") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"v", "kind", "values"}
        or payload.get("v") != 1
        or payload.get("kind") != kind
        or not isinstance(payload.get("values"), list)
        or len(payload["values"]) != 2
    ):
        raise ApiBadRequest(error, field="cursor")
    timestamp, identity = payload["values"]
    if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 0:
        raise ApiBadRequest(error, field="cursor")
    if not isinstance(identity, str) or not identity or len(identity) > 256:
        raise ApiBadRequest(error, field="cursor")
    return timestamp, identity


def _next_cursor(rows: list[dict[str, Any]], *, kind: str, time_key: str, id_key: str) -> str | None:
    if len(rows) <= _ROW_LIMIT:
        return None
    last = rows[_ROW_LIMIT - 1]
    return _encode_cursor(kind, int(last[time_key]), str(last[id_key]))


def _gate_decision(row: dict[str, Any]) -> dict[str, Any]:
    status = str(row["status"])
    return {
        "source_key": str(row["source_key"]),
        "event_id": _oi_event_id(row.get("source_key")),
        "underlying_key": row.get("underlying_key"),
        "base_symbol": _base_symbol(row.get("underlying_key")),
        "trigger_kind": str(row["trigger_kind"]),
        "source_observed_at_ms": int(row["source_observed_at_ms"]),
        "case_id": row.get("case_id"),
        "gate_status": status,
        "gate_stage": str(row["stage"]),
        "gate_reason": str(row["reason"]),
        "gate_retryable": bool(row["retryable"]),
        "gate_version": str(row["gate_version"]),
        "gate_config_digest": str(row["gate_config_digest"]).strip(),
        "gate_evidence": row.get("evidence") or {},
        "gate_first_evaluated_at_ms": int(row["first_evaluated_at_ms"]),
        "gate_last_evaluated_at_ms": int(row["last_evaluated_at_ms"]),
        "gate_attempt_count": int(row["attempt_count"]),
    }


def _case(row: dict[str, Any]) -> dict[str, Any]:
    manifest_value = row.get("manifest")
    manifest: dict[str, Any] = manifest_value if isinstance(manifest_value, dict) else {}
    contexts_value = manifest.get("contexts")
    contexts: dict[str, Any] = contexts_value if isinstance(contexts_value, dict) else {}
    oi_value = contexts.get("oi")
    oi: dict[str, Any] = oi_value if isinstance(oi_value, dict) else {}
    market_value = contexts.get("market")
    market: dict[str, Any] = market_value if isinstance(market_value, dict) else {}
    trigger_value = manifest.get("primary_trigger")
    trigger: dict[str, Any] = trigger_value if isinstance(trigger_value, dict) else {}
    return {
        "case_id": str(row["case_id"]),
        "event_id": _oi_event_id(row.get("primary_source_key")),
        "underlying_key": str(row["underlying_key"]),
        "base_symbol": _base_symbol(row.get("underlying_key")),
        "market_key": manifest.get("market_key"),
        "source_venue": trigger.get("venue"),
        "trigger_kind": str(row["trigger_kind"]),
        "manifest_version": manifest.get("manifest_version"),
        "policy_id": str(row["strategy_id"]),
        "policy_version": str(row["strategy_version"]),
        "policy_config_digest": str(row["strategy_config_digest"]),
        "policy_config": _frozen_config(manifest.get("policy_config")),
        "policy_checks": _policy_checks(row.get("policy_checks")),
        "state": str(row["state"]),
        "policy_decision": row.get("policy_decision"),
        "policy_reason": row.get("policy_reason"),
        "mark_price": _string_or_none(market.get("mark_price")),
        "pre_move_bps": _int_or_none(market.get("pre_move_bps")),
        "oi_change_bps": _int_or_none(oi.get("oi_change_bps")),
        "oi_value_usd": _int_or_none(oi.get("oi_value_usd")),
        "whale_oi_ratio_bps": _int_or_none(oi.get("whale_oi_ratio_bps")),
        "whale_long_profit_bps": _int_or_none(oi.get("whale_long_profit_bps")),
        "observed_at_ms": int(row["observed_at_ms"]),
        "created_at_ms": int(row["case_created_at_ms"]),
        "decided_at_ms": _int_or_none(row.get("decided_at_ms")),
    }


def _signal(row: dict[str, Any], *, now_ns: int) -> dict[str, Any]:
    return {
        "seq": int(row["seq"]),
        "signal_id": str(row["signal_id"]),
        "case_id": str(row["case_id"]),
        "alpha_contract_sha256": str(row["alpha_contract_sha256"]),
        "market_key": str(row["market_key"]),
        "direction": str(row["direction"]),
        "observed_at_ns": int(row["observed_at_ns"]),
        "expires_at_ns": int(row["expires_at_ns"]),
        "expired": int(row["expires_at_ns"]) <= now_ns,
        "evidence_sha256": str(row["evidence_sha256"]),
        "alpha_metadata": row.get("alpha_metadata") or {},
    }


def _observation(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "seq": int(row["seq"]),
        "event_id": str(row["event_id"]),
        "account_slot": str(row["account_slot"]),
        "runtime_release": str(row["runtime_release"]),
        "execution_strategy": str(row["execution_strategy"]),
        "signal_id": row.get("signal_id"),
        "command_id": row.get("command_id"),
        "normalized_kind": str(row["normalized_kind"]),
        "occurred_at_ns": int(row["occurred_at_ns"]),
        "observed_at_ns": int(row["observed_at_ns"]),
        "native_identity_references": list(row.get("native_identity_references") or []),
        "summary": row.get("summary") or {},
        "payload_digest": str(row["payload_digest"]),
    }


def _command(row: dict[str, Any], *, now_ns: int) -> dict[str, Any]:
    return {
        "seq": int(row["seq"]),
        "command_id": str(row["command_id"]),
        "account_slot": str(row["account_slot"]),
        "action": str(row["action"]),
        "scope": str(row["scope"]),
        "reason": str(row["reason"]),
        "operator_identity": str(row["operator_identity"]),
        "requested_at_ns": int(row["requested_at_ns"]),
        "expires_at_ns": int(row["expires_at_ns"]),
        "expired": int(row["expires_at_ns"]) <= now_ns,
        "confirmed": bool(row["confirmed"]),
        "market_key": row.get("market_key"),
        "direction": row.get("direction"),
        "disposition": row.get("disposition"),
        "disposition_reason": row.get("disposition_reason"),
    }


def _underlying_key(value: str, *, error: str) -> str | None:
    raw = str(value or "").strip().upper().removeprefix("XYZ-")
    if not raw:
        return None
    base = raw.removeprefix("CRYPTO:")
    if _BASE_SYMBOL.fullmatch(base) is None:
        raise ApiBadRequest(error, field="underlying")
    return f"crypto:{base}"


def _base_symbol(underlying_key: object) -> str:
    return str(underlying_key or "").split(":", 1)[-1]


def _oi_event_id(primary_source_key: object) -> str | None:
    raw = str(primary_source_key or "")
    prefix = "oi:"
    suffix = f":{_OI_METRIC_VERSION}"
    if not raw.startswith(prefix) or not raw.endswith(suffix):
        return None
    event_id = raw[len(prefix) : -len(suffix)]
    return event_id if event_id and raw == f"oi:{event_id}:{_OI_METRIC_VERSION}" else None


def _policy_checks(value: Any) -> list[dict[str, Any]]:
    checks = value.get("checks") if isinstance(value, dict) else None
    if not isinstance(checks, list):
        return []
    return [
        {
            "check": str(item.get("check") or ""),
            "operator": str(item.get("operator") or ""),
            "threshold": str(item.get("threshold") or ""),
            "measured": None if item.get("measured") is None else str(item.get("measured")),
            "passed": bool(item.get("passed")),
        }
        for item in checks
        if isinstance(item, dict)
    ]


def _frozen_config(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in sorted(value.items())}


def _int_or_none(value: Any) -> int | None:
    return None if value is None else int(value)


def _string_or_none(value: Any) -> str | None:
    return None if value is None else str(value)


__all__ = ["router"]
