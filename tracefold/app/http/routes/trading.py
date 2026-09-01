"""Read-only Source, Case, Signal, Observation, and execution-readiness routes."""

from __future__ import annotations

import base64
import json
import re
import time
from typing import Annotated, Any, Final

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from tracefold.app.trading_config import ADMISSION_VERSION, signal_lane_config
from tracefold.news.oi_signals import METRIC_VERSION as OI_METRIC_VERSION
from tracefold.trading import DecisionRuntimeV1, canonical_sha256

from ..dependencies import _authenticated_runtime, _validate_query_params
from ..exceptions import ApiBadRequest
from ..responses import _etagged
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


@router.get("/trading/status", response_model=_StatusEnvelope)
def get_trading_status(request: Request) -> Response:
    _validate_query_params(request, supported={"token"})
    runtime = _authenticated_runtime(request)
    now_ms = int(time.time() * 1000)
    with runtime.repositories() as repos:
        decision = repos.trading.decision_runtime() or DecisionRuntimeV1(
            state="FAULTED",
            heartbeat_at_ms=None,
            reason="decision_runtime_missing",
            updated_at_ms=now_ms,
        )
        counts = repos.trading.runtime_summary(since_ms=now_ms - _WINDOW_MS, now_ms=now_ms)
    config = signal_lane_config(runtime.settings)
    execution = runtime.settings.trading.execution
    alpha_contract = canonical_sha256(
        {
            "policy_id": config.policy.policy_id,
            "policy_version": config.policy.policy_version,
            "policy_config": config.policy.config_snapshot,
        }
    )
    return _etagged(
        {
            "decision": {
                "state": decision.state,
                "heartbeat_at_ms": decision.heartbeat_at_ms,
                "reason": decision.reason,
            },
            "execution": {
                "mode": execution.mode,
                "profile_id": execution.profile_id,
                "account_slot": execution.account_slot,
                "ready": False,
                "reason": "disabled" if execution.mode == "disabled" else "activation_not_available_before_433e",
            },
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
    profile: Annotated[str, Query(max_length=128)] = "",
    kind: Annotated[str, Query(max_length=32)] = "",
    cursor: Annotated[str, Query(max_length=256)] = "",
) -> Response:
    _validate_query_params(request, supported={"cursor", "kind", "profile", "token"})
    if kind and kind not in _OBSERVATION_KINDS:
        raise ApiBadRequest("trading_observations_kind_invalid", field="kind")
    before = _cursor_pair(cursor, kind="observations", error="trading_observations_cursor_invalid")
    runtime = _authenticated_runtime(request)
    now_ms = int(time.time() * 1000)
    with runtime.repositories() as repos:
        rows = repos.trading.console_execution_observations(
            since_ns=(now_ms - _WINDOW_MS) * 1_000_000,
            runtime_profile_id=profile.strip() or None,
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
    profile: Annotated[str, Query(max_length=128)] = "",
    action: Annotated[str, Query(max_length=32)] = "",
    cursor: Annotated[str, Query(max_length=256)] = "",
) -> Response:
    _validate_query_params(request, supported={"action", "cursor", "profile", "token"})
    if action and action not in _COMMAND_ACTIONS:
        raise ApiBadRequest("trading_commands_action_invalid", field="action")
    before = _cursor_pair(cursor, kind="commands", error="trading_commands_cursor_invalid")
    runtime = _authenticated_runtime(request)
    now_ms = int(time.time() * 1000)
    now_ns = now_ms * 1_000_000
    with runtime.repositories() as repos:
        rows = repos.trading.console_operator_intents(
            since_ns=(now_ms - _WINDOW_MS) * 1_000_000,
            runtime_profile_id=profile.strip() or None,
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
        "research_only": status == "RESEARCH_ONLY",
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
        "runtime_profile_id": str(row["runtime_profile_id"]),
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
        "target_profile_id": str(row["target_profile_id"]),
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
