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

import re
import time
from datetime import UTC, datetime
from typing import Annotated, Any, Final

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from tracefold.app.trading_config import ADMISSION_VERSION, capital_lane_config
from tracefold.news.oi_signals import METRIC_VERSION as OI_METRIC_VERSION
from tracefold.trading import CapitalRuntimeV1, DecisionRuntimeV1, VenueBindingRuntimeV1
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

_WINDOW_MS: Final = 24 * 3_600_000
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


@router.get("/trading/status", response_model=_StatusEnvelope)
def get_trading_status(request: Request) -> Response:
    """Durable Decision, Capital and per-binding runtime facts. No secret or provider reads."""

    _validate_query_params(request, supported={"token"})
    runtime = _authenticated_runtime(request)
    settings = runtime.settings
    now_ms = int(time.time() * 1000)
    with runtime.repositories() as repos:
        decision = repos.trading.decision_runtime() or DecisionRuntimeV1(
            state="FAULTED",
            heartbeat_at_ms=None,
            reason="decision_runtime_missing",
            updated_at_ms=now_ms,
        )
        capital = repos.trading.capital_runtime() or CapitalRuntimeV1(
            control="PAUSED", blacklist_revision=0, updated_at_ms=now_ms
        )
        bindings = repos.trading.binding_runtime_rows(now_ms=now_ms)
        counts = repos.trading.runtime_summary(since_ms=now_ms - _WINDOW_MS, now_ms=now_ms)
    policy = capital_lane_config(settings).policy
    return _etagged(
        {
            "budget": {
                "target_notional_usd": str(settings.trading.order.fixed_notional_usd),
            },
            "decision": {
                "state": decision.state,
                "heartbeat_at_ms": decision.heartbeat_at_ms,
                "reason": decision.reason,
            },
            "capital": {
                "control": capital.control,
                "blacklist_revision": capital.blacklist_revision,
            },
            "bindings": [_binding_runtime(row) for row in bindings],
            "policy": {
                "policy_id": policy.policy_id,
                "policy_version": policy.policy_version,
                "config_digest": policy.config_digest,
                "config": {key: str(value) for key, value in sorted(policy.config_snapshot.items())},
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
) -> Response:
    """Frozen Cases and the frozen evidence each was decided on."""

    _validate_query_params(request, supported={"state", "token", "underlying"})
    if state and state not in _CASE_STATE_FILTERS:
        raise ApiBadRequest("trading_cases_state_invalid", field="state")
    underlying_key = _underlying_key(underlying, error="trading_cases_underlying_invalid")
    runtime = _authenticated_runtime(request)
    now_ms = int(time.time() * 1000)
    with runtime.repositories() as repos:
        rows = repos.trading.console_cases(
            since_ms=now_ms - _WINDOW_MS,
            underlying_key=underlying_key,
            states=_CASE_STATE_FILTERS.get(state, ()),
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
) -> Response:
    """Immutable capital requests and their execution outcomes. Cases live at `/trading/cases`."""

    _validate_query_params(request, supported={"day", "state", "token", "underlying"})
    if state and state not in _INTENT_STATE_FILTERS:
        raise ApiBadRequest("trading_intents_state_invalid", field="state")
    underlying_key = _underlying_key(underlying, error="trading_intents_underlying_invalid")
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
            "window_hours": _WINDOW_MS // 3_600_000,
            "measured_at_ms": now_ms,
        },
        request,
        envelope=_IntentsEnvelope,
    )


def _binding_runtime(row: VenueBindingRuntimeV1) -> dict[str, Any]:
    return {
        "binding": row.binding,
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
