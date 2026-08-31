"""Read-only capital-lane surface, one route per durable aggregate (#331).

    GET /api/trading/gate              Source / Admission
    GET /api/trading/gate/{event_id}   one Source's admission answer
    GET /api/trading/cases             Case / Decision
    GET /api/trading/intents           Intent / Outcome
    GET /api/trading/status            Decision / Capital / binding runtime

The split is the product's, and it is what the pages are built on. `/intents` used to return
`cases_without_intents` beside its Intents, which put two durable objects behind one contract: a page
could not tell "no Intent" from "no Case", and a request failure fell back to an empty array that
rendered as "the system has no data". `/cases` is the replacement, and the mixed shape is gone in the
same change rather than kept as a second synonym.

Nothing here re-derives a decision. Every threshold a Case was decided by travels with that Case as
frozen evidence; the status surface publishes the identity of the policy a *new* Case would be frozen
under, and never applies it to an existing one.
"""

from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Annotated, Any, Final

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from tracefold.app.trading_config import ADMISSION_VERSION, capital_lane_config
from tracefold.app.trading_status import TRADING_STATUS_WINDOW_MS, read_trading_runtime_status
from tracefold.news.oi_signals import METRIC_VERSION as OI_METRIC_VERSION
from tracefold.trading import (
    EXECUTION_ENABLED_BINDINGS,
    DailyRiskPolicyV1,
    OperatorArmReceiptV1,
    ProductionPromotionGrantV1,
    VenueBindingRuntimeV1,
)
from tracefold.trading.intent import ACTIVE_INTENT_STATES

from ..dependencies import _authenticated_runtime, _validate_query_params
from ..exceptions import ApiBadRequest
from ..responses import _etagged
from ..schemas import common as api_schemas
from ..schemas import trading as trading_schemas

router = APIRouter()
_StatusEnvelope = api_schemas.ApiEnvelope[trading_schemas.TradingStatusData]
_IntentsEnvelope = api_schemas.ApiEnvelope[trading_schemas.TradingIntentsData]
_CasesEnvelope = api_schemas.ApiEnvelope[trading_schemas.TradingCasesData]
_GateEnvelope = api_schemas.ApiEnvelope[trading_schemas.TradingGateData]
_GateSourceEnvelope = api_schemas.ApiEnvelope[trading_schemas.TradingGateSourceData]
_CapabilitiesEnvelope = api_schemas.ApiEnvelope[trading_schemas.TradingCapabilitiesData]
_EvidenceEnvelope = api_schemas.ApiEnvelope[trading_schemas.TradingEvidenceData]

_WINDOW_MS: Final = TRADING_STATUS_WINDOW_MS
_ROW_LIMIT: Final = 100
_GATE_LIMIT: Final = 400
_OI_METRIC_VERSION: Final = OI_METRIC_VERSION
_BASE_SYMBOL: Final = re.compile(r"^[A-Z0-9._-]{1,24}$")
_DAY_KEY: Final = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_INTENT_STATE_FILTERS: Final[frozenset[str]] = frozenset({"active", "closed", "all"})
_CASE_STATE_FILTERS: Final[dict[str, tuple[str, ...]]] = {
    "open": ("PENDING", "RUNNING"),
    "no_trade": ("NO_TRADE",),
    "blocked": ("BLOCKED",),
    "emitted": ("INTENT_EMITTED",),
}
_BINDINGS: Final = frozenset({"BINANCE_USDM", "HYPERLIQUID_PERP"})
_CAPABILITY_DISPOSITIONS: Final = frozenset({"all", "included", "excluded"})
_DETAIL_FILTERS: Final = frozenset({"summary", "entries", "lifecycles"})
_RISK_STATES: Final = frozenset({"RESERVED", "FENCED", "OPEN", "MANUAL_REVIEW", "RELEASED", "SETTLED"})


@router.get("/trading/status", response_model=_StatusEnvelope)
def get_trading_status(request: Request) -> Response:
    """Durable Decision, Capital and per-binding runtime facts. No secret or provider reads."""

    _validate_query_params(request, supported={"token"})
    runtime = _authenticated_runtime(request)
    settings = runtime.settings
    now_ms = int(time.time() * 1000)
    with runtime.repositories() as repos:
        status = read_trading_runtime_status(repos.trading, now_ms=now_ms)
    policy = capital_lane_config(settings).policy
    return _etagged(
        {
            "budget": {
                "target_notional_usd": str(settings.trading.order.fixed_notional_usd),
            },
            "decision": {
                "state": status.decision.state,
                "heartbeat_at_ms": status.decision.heartbeat_at_ms,
                "reason": status.decision.reason,
            },
            "capital": {
                "control": status.capital.control,
                "blacklist_revision": status.capital.blacklist_revision,
                "arm_epoch": status.capital.arm_epoch,
            },
            "nautilus": asdict(status.nautilus),
            "bindings": [_binding_runtime(row) for row in status.bindings],
            "policy": {
                "policy_id": policy.policy_id,
                "policy_version": policy.policy_version,
                "config_digest": policy.config_digest,
                "config": {key: str(value) for key, value in sorted(policy.config_snapshot.items())},
            },
            "counts": status.summary,
            "window_hours": _WINDOW_MS // 3_600_000,
            "measured_at_ms": now_ms,
        },
        request,
        envelope=_StatusEnvelope,
    )


@router.get("/trading/gate", response_model=_GateEnvelope)
def get_trading_gate(request: Request) -> Response:
    """Every Source the lane saw in the window, and the one durable answer each received."""

    _validate_query_params(request, supported={"token"})
    runtime = _authenticated_runtime(request)
    now_ms = int(time.time() * 1000)
    with runtime.repositories() as repos:
        rows = repos.trading.gate_decisions_since(since_ms=now_ms - _WINDOW_MS, limit=_GATE_LIMIT + 1)
        report = repos.trading.candidate_admission_report(now_ms=now_ms)
    admission = capital_lane_config(runtime.settings).admission
    return _etagged(
        {
            "config": {
                "version": ADMISSION_VERSION,
                "config_digest": admission.digest,
                **admission.snapshot,
            },
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
    """One Source's admission answer. `joinable=false` when the question cannot be asked at all.

    Only the deterministic OI lane's source key is reconstructible from an Event id
    (`oi:{event_id}:{metric_version}`), so a caller asking about anything else is told the question is
    unanswerable rather than shown a refusal that never happened.
    """

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
        {
            "event_id": event_id,
            "joinable": True,
            "decision": None if row is None else _gate_decision(row),
        },
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
    """Frozen Cases and the frozen evidence each was decided on."""

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
        capital_reasons = repos.trading.case_capital_reason_counts(since_ms=now_ms - _WINDOW_MS)
    return _etagged(
        {
            "cases": [_case(row) for row in rows[:_ROW_LIMIT]],
            "state_counts_24h": states,
            "reason_counts_24h": reasons,
            "capital_reason_counts_24h": capital_reasons,
            "complete": len(rows) <= _ROW_LIMIT,
            "next_cursor": _next_pair_cursor(rows, kind="cases", time_key="case_created_at_ms", id_key="case_id"),
            "window_hours": _WINDOW_MS // 3_600_000,
            "measured_at_ms": now_ms,
        },
        request,
        envelope=_CasesEnvelope,
    )


@router.get("/trading/intents", response_model=_IntentsEnvelope)
def get_trading_intents(
    request: Request,
    day: Annotated[str, Query(max_length=10)] = "",
    underlying: Annotated[str, Query(max_length=32)] = "",
    state: Annotated[str, Query(max_length=16)] = "",
    cursor: Annotated[str, Query(max_length=256)] = "",
) -> Response:
    """Immutable capital requests and their execution outcomes. Cases live at `/trading/cases`."""

    _validate_query_params(request, supported={"cursor", "day", "state", "token", "underlying"})
    if state and state not in _INTENT_STATE_FILTERS:
        raise ApiBadRequest("trading_intents_state_invalid", field="state")
    underlying_key = _underlying_key(underlying, error="trading_intents_underlying_invalid")
    before = _cursor_pair(cursor, kind="intents", error="trading_intents_cursor_invalid")
    closed_from_ms: int | None = None
    closed_until_ms: int | None = None
    if day:
        try:
            if _DAY_KEY.fullmatch(day) is None:
                raise ValueError
            closed_from_ms = int(datetime.fromisoformat(day).replace(tzinfo=UTC).timestamp() * 1000)
        except ValueError as exc:
            raise ApiBadRequest("trading_intents_day_invalid", field="day") from exc
        closed_until_ms = closed_from_ms + 86_400_000
    runtime = _authenticated_runtime(request)
    now_ms = int(time.time() * 1000)
    states: tuple[str, ...] = ()
    if state == "active":
        states = ACTIVE_INTENT_STATES
    elif state == "closed":
        states = ("TERMINAL",)
    with runtime.repositories() as repos:
        intents = repos.trading.console_intents(
            since_ms=now_ms - _WINDOW_MS,
            closed_from_ms=closed_from_ms,
            closed_until_ms=closed_until_ms,
            underlying_key=underlying_key,
            states=states,
            before=before,
            limit=_ROW_LIMIT + 1,
        )
        counts = repos.trading.intent_counts(since_ms=now_ms - _WINDOW_MS)
    return _etagged(
        {
            "intents": [_intent(row) for row in intents[:_ROW_LIMIT]],
            "state_counts_24h": counts["by_state"],
            "outcome_counts_24h": counts["by_outcome"],
            "reason_counts_24h": counts["by_reason"],
            "complete": len(intents) <= _ROW_LIMIT,
            "next_cursor": _next_pair_cursor(intents, kind="intents", time_key="created_at_ms", id_key="intent_id"),
            "window_hours": _WINDOW_MS // 3_600_000,
            "measured_at_ms": now_ms,
        },
        request,
        envelope=_IntentsEnvelope,
    )


@router.get("/trading/capabilities", response_model=_CapabilitiesEnvelope)
def get_trading_capabilities(
    request: Request,
    binding: Annotated[str, Query(max_length=32)] = "",
    disposition: Annotated[str, Query(max_length=16)] = "all",
    detail: Annotated[str, Query(max_length=16)] = "entries",
    cursor: Annotated[str, Query(max_length=256)] = "",
) -> Response:
    """Current durable V2 partition; never compiles capabilities or contacts a venue."""

    _validate_query_params(request, supported={"binding", "cursor", "detail", "disposition", "token"})
    if binding and binding not in _BINDINGS:
        raise ApiBadRequest("trading_capabilities_binding_invalid", field="binding")
    if disposition not in _CAPABILITY_DISPOSITIONS:
        raise ApiBadRequest("trading_capabilities_disposition_invalid", field="disposition")
    if detail not in {"summary", "entries"}:
        raise ApiBadRequest("trading_capabilities_detail_invalid", field="detail")
    after = _cursor_values(cursor, kind="capabilities", count=3, error="trading_capabilities_cursor_invalid")
    if detail == "summary" and after is not None:
        raise ApiBadRequest("trading_capabilities_cursor_invalid", field="cursor")
    runtime = _authenticated_runtime(request)
    now_ms = int(time.time() * 1000)
    summaries: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = []
    with runtime.repositories() as repos:
        for row in repos.trading.binding_runtime_rows(now_ms=now_ms):
            if binding and row.binding != binding:
                continue
            snapshot = repos.trading.active_execution_capability_snapshot(binding=row.binding)
            summaries.append(
                {
                    "binding": row.binding,
                    "capability_state": row.capability_state,
                    "snapshot_sha256": None if snapshot is None else snapshot.snapshot_sha256,
                    "catalog_snapshot_sha256": None if snapshot is None else snapshot.catalog_snapshot_sha256,
                    "catalog_instrument_count": 0 if snapshot is None else snapshot.catalog_instrument_count,
                    "included_count": 0 if snapshot is None else snapshot.included_count,
                    "excluded_count": 0 if snapshot is None else snapshot.excluded_count,
                    "partition_sha256": None if snapshot is None else snapshot.partition_sha256,
                    "compiled_at_ms": row.capability_compiled_at_ms,
                    "compile_error": row.capability_compile_error,
                    "last_known_good": snapshot is not None,
                }
            )
            if detail == "entries" and snapshot is not None:
                if disposition in {"all", "included"}:
                    entries.extend(
                        _included_capability(row.binding, key, value) for key, value in snapshot.included.items()
                    )
                if disposition in {"all", "excluded"}:
                    entries.extend(
                        _excluded_capability(row.binding, key, value) for key, value in snapshot.excluded.items()
                    )
    entries.sort(key=_capability_cursor_key)
    if after is not None:
        marker = tuple(str(value) for value in after)
        entries = [item for item in entries if _capability_cursor_key(item) > marker]
    page = entries[:_ROW_LIMIT]
    complete = len(entries) <= _ROW_LIMIT
    next_cursor = None
    if not complete and page:
        next_cursor = _encode_cursor("capabilities", *_capability_cursor_key(page[-1]))
    return _etagged(
        {
            "bindings": summaries,
            "entries": page,
            "complete": complete,
            "next_cursor": next_cursor,
            "measured_at_ms": now_ms,
        },
        request,
        envelope=_CapabilitiesEnvelope,
    )


@router.get("/trading/evidence", response_model=_EvidenceEnvelope)
def get_trading_evidence(
    request: Request,
    binding: Annotated[str, Query(max_length=32)] = "",
    state: Annotated[str, Query(max_length=24)] = "",
    detail: Annotated[str, Query(max_length=16)] = "lifecycles",
    cursor: Annotated[str, Query(max_length=256)] = "",
) -> Response:
    """Redacted grant/arm/risk proof and bounded capital lifecycle rows from PostgreSQL."""

    _validate_query_params(request, supported={"binding", "cursor", "detail", "state", "token"})
    if binding and binding not in _BINDINGS:
        raise ApiBadRequest("trading_evidence_binding_invalid", field="binding")
    normalized_state = state.upper()
    if normalized_state and normalized_state not in _RISK_STATES:
        raise ApiBadRequest("trading_evidence_state_invalid", field="state")
    if detail not in {"summary", "lifecycles"}:
        raise ApiBadRequest("trading_evidence_detail_invalid", field="detail")
    before = _cursor_pair(cursor, kind="evidence", error="trading_evidence_cursor_invalid")
    if detail == "summary" and before is not None:
        raise ApiBadRequest("trading_evidence_cursor_invalid", field="cursor")
    runtime = _authenticated_runtime(request)
    now_ms = int(time.time() * 1000)
    with runtime.repositories() as repos:
        authority_rows = repos.trading.authority_projection()
        rows = (
            []
            if detail == "summary"
            else repos.trading.console_capital_evidence(
                binding=binding or None,
                statuses=() if not normalized_state else (normalized_state,),
                before=before,
                limit=_ROW_LIMIT + 1,
            )
        )
    if binding:
        authority_rows = [row for row in authority_rows if row["binding"] == binding]
    return _etagged(
        {
            "authorities": [_authority_evidence(row, now_ms=now_ms) for row in authority_rows],
            "lifecycles": [_capital_lifecycle(row) for row in rows[:_ROW_LIMIT]],
            "complete": len(rows) <= _ROW_LIMIT,
            "next_cursor": _next_pair_cursor(
                rows, kind="evidence", time_key="updated_at_ms", id_key="reservation_sha256"
            ),
            "measured_at_ms": now_ms,
        },
        request,
        envelope=_EvidenceEnvelope,
    )


def _encode_cursor(kind: str, *values: object) -> str:
    raw = json.dumps(
        {"v": 1, "kind": kind, "values": list(values)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _cursor_values(cursor: str, *, kind: str, count: int, error: str) -> list[object] | None:
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
        or len(payload["values"]) != count
    ):
        raise ApiBadRequest(error, field="cursor")
    return list(payload["values"])


def _cursor_pair(cursor: str, *, kind: str, error: str) -> tuple[int, str] | None:
    values = _cursor_values(cursor, kind=kind, count=2, error=error)
    if values is None:
        return None
    timestamp, identity = values
    if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 0:
        raise ApiBadRequest(error, field="cursor")
    if not isinstance(identity, str) or not identity or len(identity) > 256:
        raise ApiBadRequest(error, field="cursor")
    return timestamp, identity


def _next_pair_cursor(
    rows: list[dict[str, Any]],
    *,
    kind: str,
    time_key: str,
    id_key: str,
) -> str | None:
    if len(rows) <= _ROW_LIMIT:
        return None
    last = rows[_ROW_LIMIT - 1]
    return _encode_cursor(kind, int(last[time_key]), str(last[id_key]))


def _included_capability(binding: str, key: str, value: Any) -> dict[str, Any]:
    return {
        "binding": binding,
        "catalog_entry_id": key,
        "disposition": "included",
        "provider_instrument_id": value.provider_instrument_id,
        "instrument_id": value.instrument_id,
        "canonical_asset": value.canonical_asset,
        "canonical_namespace": value.canonical_namespace,
        "settlement_asset": value.settlement_asset,
        "price_increment": value.price_increment,
        "size_increment": value.size_increment,
        "min_quantity": value.min_quantity,
        "min_notional": value.min_notional,
        "exclusion_reason": None,
    }


def _excluded_capability(binding: str, key: str, value: Any) -> dict[str, Any]:
    return {
        "binding": binding,
        "catalog_entry_id": key,
        "disposition": "excluded",
        "provider_instrument_id": value.provider_instrument_id,
        "instrument_id": None,
        "canonical_asset": None,
        "canonical_namespace": None,
        "settlement_asset": None,
        "price_increment": None,
        "size_increment": None,
        "min_quantity": None,
        "min_notional": None,
        "exclusion_reason": value.reason,
    }


def _capability_cursor_key(item: dict[str, Any]) -> tuple[str, str, str]:
    return str(item["binding"]), str(item["disposition"]), str(item["catalog_entry_id"])


def _authority_evidence(row: dict[str, Any], *, now_ms: int) -> dict[str, Any]:
    arm_payload = row.get("arm_payload")
    grant_payload = row.get("grant_payload")
    policy_payload = row.get("policy_payload")
    status = "absent"
    arm = None
    grant = None
    policy = None
    if row.get("active_arm_receipt_sha256") is not None:
        status = "invalid"
        try:
            arm = OperatorArmReceiptV1.model_validate(arm_payload)
            grant = ProductionPromotionGrantV1.model_validate(grant_payload)
            policy = DailyRiskPolicyV1.model_validate(policy_payload)
        except ValueError:
            pass
        else:
            exact = (
                arm.binding == row["binding"]
                and grant.binding == row["binding"]
                and arm.grant_sha256 == grant.grant_sha256
                and arm.risk_policy_sha256 == policy.risk_policy_sha256
                and grant.risk_policy_sha256 == policy.risk_policy_sha256
                and arm.approved_release == grant.approved_release == policy.approved_release
            )
            if not exact:
                status = "invalid"
            elif row.get("revocation_payload") is not None:
                status = "revoked"
            elif min(arm.expires_at_ms, grant.expires_at_ms, policy.expires_at_ms) <= now_ms:
                status = "expired"
            else:
                status = "active"
    return {
        "binding": str(row["binding"]),
        "status": status,
        "active_arm_receipt_sha256": row.get("active_arm_receipt_sha256"),
        "arm_expires_at_ms": None if arm is None else arm.expires_at_ms,
        "grant_sha256": None if grant is None else grant.grant_sha256,
        "grant_expires_at_ms": None if grant is None else grant.expires_at_ms,
        "risk_policy_sha256": None if policy is None else policy.risk_policy_sha256,
        "risk_policy_expires_at_ms": None if policy is None else policy.expires_at_ms,
        "approved_release": None if policy is None else policy.approved_release,
        "settlement_limits": (
            []
            if policy is None
            else [
                {
                    "settlement_asset": limit.settlement_asset,
                    "max_planned_risk_amount": str(limit.max_planned_risk_amount),
                    "max_realized_loss_amount": str(limit.max_realized_loss_amount),
                    "fee_slippage_reserve_bps": limit.fee_slippage_reserve_bps,
                }
                for limit in policy.settlement_limits
            ]
        ),
    }


def _capital_lifecycle(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "reservation_sha256": str(row["reservation_sha256"]),
        "authorization_receipt_sha256": str(row["authorization_receipt_sha256"]),
        "case_id": str(row["case_id"]),
        "intent_id": str(row["intent_id"]),
        "economic_lifecycle_id": str(row["economic_lifecycle_id"]),
        "binding": str(row["binding"]),
        "settlement_asset": str(row["settlement_asset"]),
        "risk_policy_sha256": str(row["risk_policy_sha256"]),
        "grant_sha256": str(row["grant_sha256"]),
        "arm_receipt_sha256": str(row["arm_receipt_sha256"]),
        "risk_day_start_ms": int(row["risk_day_start_ms"]),
        "risk_day_end_ms": int(row["risk_day_end_ms"]),
        "target_notional": str(row["target_notional"]),
        "initial_planned_risk_amount": str(row["planned_risk_amount"]),
        "current_planned_risk_amount": str(row["current_planned_risk_amount"]),
        "risk_status": str(row["status"]),
        "attempt_consumed": bool(row["attempt_consumed"]),
        "attempt_day_start_ms": _int(row.get("attempt_day_start_ms")),
        "attempt_day_end_ms": _int(row.get("attempt_day_end_ms")),
        "settlement_known": bool(row["settlement_known"]),
        "execution_state": str(row["execution_state"]),
        "execution_phase": row.get("execution_phase"),
        "terminal_outcome": row.get("terminal_outcome"),
        "reason_code": row.get("reason_code"),
        "flat_verified_at_ms": _int(row.get("flat_verified_at_ms")),
        "updated_at_ms": int(row["updated_at_ms"]),
    }


def _binding_runtime(row: VenueBindingRuntimeV1) -> dict[str, Any]:
    return {
        "binding": row.binding,
        "execution_enabled": row.binding in EXECUTION_ENABLED_BINDINGS,
        "execution_environment": "demo" if row.binding == "BINANCE_USDM" else None,
        "credential_state": row.credential_state,
        "credential_fingerprint": row.credential_fingerprint,
        "runtime_state": row.runtime_state,
        "account_state": row.account_state,
        "account_generation": row.account_generation,
        "catalog_state": row.catalog_state,
        "catalog_snapshot_sha256": row.catalog_snapshot_sha256,
        "catalog_captured_at_ms": row.catalog_captured_at_ms,
        "capability_state": row.capability_state,
        "capability_snapshot_sha256": row.capability_snapshot_sha256,
        "capability_compiled_at_ms": row.capability_compiled_at_ms,
        "capability_compile_error": row.capability_compile_error,
        "execution_binding_sha256": row.execution_binding_sha256,
        "active_arm_receipt_sha256": row.active_arm_receipt_sha256,
        "heartbeat_at_ms": row.heartbeat_at_ms,
        "reason": row.reason,
    }


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


def _underlying_key(value: str, *, error: str) -> str | None:
    raw = str(value or "").strip().upper().removeprefix("XYZ-")
    if not raw:
        return None
    base = raw.removeprefix("CRYPTO:")
    if not _BASE_SYMBOL.fullmatch(base):
        raise ApiBadRequest(error, field="underlying")
    return f"crypto:{base}"


def _base_symbol(underlying_key: object) -> str:
    return str(underlying_key or "").split(":", 1)[-1]


def _decimal(value: object) -> str | None:
    return None if value is None else str(value)


def _oi_event_id(primary_source_key: object) -> str | None:
    raw = str(primary_source_key or "")
    prefix = "oi:"
    suffix = f":{_OI_METRIC_VERSION}"
    if not raw.startswith(prefix) or not raw.endswith(suffix):
        return None
    event_id = raw[len(prefix) : -len(suffix)]
    return event_id if event_id and raw == f"oi:{event_id}:{_OI_METRIC_VERSION}" else None


def _int(value: Any) -> int | None:
    return None if value is None else int(value)


def _case(row: dict[str, Any]) -> dict[str, Any]:
    """One frozen Case. Every threshold shown here is the one the Case itself carries."""

    return {
        "case_id": str(row["case_id"]),
        "event_id": _oi_event_id(row.get("primary_source_key")),
        "underlying_key": str(row["underlying_key"]),
        "base_symbol": _base_symbol(row.get("underlying_key")),
        "provider_symbol": row.get("provider_symbol"),
        "trigger_kind": str(row["trigger_kind"]),
        "manifest_version": row.get("manifest_version"),
        # `strategy_*` are the storage column names; the product word is `policy`, and the read model
        # is where the two meet rather than in a rename migration over 228 historical rows.
        "policy_id": str(row["strategy_id"]),
        "policy_version": str(row["strategy_version"]),
        "policy_config_digest": str(row["strategy_config_digest"]),
        "policy_config": _frozen_config(row.get("policy_config")),
        "policy_checks": _policy_checks(row.get("policy_checks")),
        "state": str(row["state"]),
        "policy_decision": row.get("policy_decision"),
        "policy_reason": row.get("policy_reason"),
        "capital_disposition": str(row["capital_disposition"]),
        "capital_reason": row.get("capital_reason"),
        "mark_price": _decimal(row.get("mark_price")),
        "pre_move_bps": _int(row.get("pre_move_bps")),
        "oi_change_bps": _int(row.get("oi_change_bps")),
        "oi_value_usd": _int(row.get("oi_value_usd")),
        "whale_oi_ratio_bps": _int(row.get("whale_oi_ratio_bps")),
        "whale_long_profit_bps": _int(row.get("whale_long_profit_bps")),
        "observed_at_ms": int(row["observed_at_ms"]),
        "created_at_ms": int(row["case_created_at_ms"]),
        "decided_at_ms": _int(row.get("decided_at_ms")),
        "intent_id": row.get("intent_id"),
    }


def _policy_checks(value: Any) -> list[dict[str, Any]]:
    """The frozen per-check evidence, or an empty list for a Case written before it existed."""

    if not isinstance(value, dict):
        return []
    checks = value.get("checks")
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


def _intent(row: dict[str, Any]) -> dict[str, Any]:
    decimal_fields = (
        "target_notional_usd",
        "target_notional",
        "max_risk_amount",
        "reference_price",
        "actual_quantity",
        "protected_quantity",
        "avg_entry_price",
        "avg_exit_price",
        "stop_price",
        "realized_pnl_amount",
    )
    result = {
        key: row.get(key)
        for key in (
            "intent_id",
            "intent_version",
            "case_id",
            "execution_environment",
            "source_venue",
            "source_identity",
            "canonical_asset",
            "binding",
            "account_generation",
            "execution_binding_sha256",
            "venue_catalog_snapshot_sha256",
            "execution_capability_snapshot_sha256",
            "capability_entry_id",
            "provider_instrument_id",
            "settlement_asset",
            "intent_policy_sha256",
            "execution_policy_sha256",
            "quote_contract_sha256",
            "protection_contract_sha256",
            "capital_authorization_receipt_sha256",
            "blacklist_revision_at_emission",
            "blacklist_snapshot_sha256_at_emission",
            "instrument_id",
            "side",
            "leverage",
            "risk_currency",
            "economic_lifecycle_id",
            "entry_leg_id",
            "protection_leg_id",
            "close_leg_id",
            "valid_until_ms",
            "execution_state",
            "execution_phase",
            "terminal_outcome",
            "reason_code",
            "entry_fenced_at_ms",
            "opened_at_ms",
            "protected_at_ms",
            "closed_at_ms",
            "flat_verified_at_ms",
            "realized_pnl_currency",
            "commissions_by_currency",
            "funding_by_currency",
            "created_at_ms",
            "updated_at_ms",
        )
    }
    result.update({key: _decimal(row.get(key)) for key in decimal_fields})
    result.update(
        {
            "event_id": _oi_event_id(row.get("primary_source_key")),
            "underlying_key": str(row["underlying_key"]),
            "base_symbol": _base_symbol(row.get("underlying_key")),
            "policy_id": str(row["strategy_id"]),
            "policy_version": str(row["strategy_version"]),
        }
    )
    return result


__all__ = ["router"]
