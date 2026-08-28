"""Read-only Case -> Intent -> Outcome surface for the capital lane."""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime
from typing import Annotated, Any, Final

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from tracefold.app.trading_config import CANDIDATE_GATE_VERSION, trading_settings_gate, trading_settings_strategies
from tracefold.news.oi_signals import METRIC_VERSION as OI_METRIC_VERSION
from tracefold.platform.config.secret_file import secret_file_configured
from tracefold.trading.intent import ACTIVE_INTENT_STATES

from ..dependencies import _authenticated_runtime, _validate_query_params
from ..exceptions import ApiBadRequest
from ..responses import _etagged
from ..schemas import common as api_schemas
from ..schemas import trading as trading_schemas

router = APIRouter()
_StatusEnvelope = api_schemas.ApiEnvelope[trading_schemas.TradingStatusData]
_IntentsEnvelope = api_schemas.ApiEnvelope[trading_schemas.TradingIntentsData]
_EventCaseEnvelope = api_schemas.ApiEnvelope[trading_schemas.TradingEventCaseData]
_GateEnvelope = api_schemas.ApiEnvelope[trading_schemas.TradingGateData]

_WINDOW_MS: Final = 24 * 3_600_000
_ROW_LIMIT: Final = 100
_GATE_LIMIT: Final = 400
_OI_METRIC_VERSION: Final = OI_METRIC_VERSION
_BASE_SYMBOL: Final = re.compile(r"^[A-Z0-9._-]{1,24}$")
_DAY_KEY: Final = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_STATE_FILTERS: Final[frozenset[str]] = frozenset({"active", "closed", "all"})


@router.get("/trading/status", response_model=_StatusEnvelope)
def get_trading_status(request: Request) -> Response:
    _validate_query_params(request, supported={"token"})
    runtime = _authenticated_runtime(request)
    settings = runtime.settings
    now_ms = int(time.time() * 1000)
    with runtime.repositories() as repos:
        state = repos.trading.runtime_state() or {}
        counts = repos.trading.status_counts(
            since_ms=now_ms - _WINDOW_MS,
            now_ms=now_ms,
            day_key=state.get("day_key"),
        )
        admission = repos.trading.candidate_admission_report(now_ms=now_ms)
    key_file = settings.trading_nautilus_api_key_file()
    secret_file = settings.trading_nautilus_api_secret_file()
    return _etagged(
        {
            "budget": {
                "target_notional_usd": str(settings.trading.order.fixed_notional_usd),
                "max_entries_per_utc_day": 1,
            },
            "readiness": {
                "control": str(state.get("control") or "PAUSED"),
                "enabled": settings.trading.enabled,
                "execution_authority": "nautilus",
                "execution_environment": "BINANCE_USDM_DEMO",
                "active_capability_snapshot_sha256": state.get("active_capability_snapshot_sha256"),
                "active_capability_included_count": int(state.get("active_capability_included_count") or 0),
                "blacklist_revision": int(state.get("blacklist_revision") or 0),
                "credentials_configured": secret_file_configured(key_file) and secret_file_configured(secret_file),
                "engine_ready": bool(state.get("nautilus_ready")),
                "engine_readiness_reason": state.get("nautilus_readiness_reason"),
                "unexpected_exposure": bool(state.get("nautilus_unexpected_exposure")),
                "heartbeat_at_ms": state.get("nautilus_heartbeat_at_ms"),
            },
            "floors": {
                "lookback_ms": settings.trading.regime.lookback_seconds * 1000,
                "max_price_move_bps": settings.trading.regime.max_price_move_bps,
                "min_oi_value_usd": str(settings.trading.candidates.min_oi_value_usd),
                "min_price_move_bps": settings.trading.regime.min_price_move_bps,
                "min_whale_long_profit_bps": settings.trading.policy.min_whale_long_profit_bps,
            },
            "gate": _gate_config(settings),
            "strategies": _strategy_configs(settings),
            "counts": {**counts, **admission, "funnel_today": _int_map(state.get("funnel"))},
            "window_hours": _WINDOW_MS // 3_600_000,
            "measured_at_ms": now_ms,
        },
        request,
        envelope=_StatusEnvelope,
    )


@router.get("/trading/intents", response_model=_IntentsEnvelope)
def get_trading_intents(
    request: Request,
    day: Annotated[str, Query(max_length=10)] = "",
    underlying: Annotated[str, Query(max_length=32)] = "",
    state: Annotated[str, Query(max_length=16)] = "",
) -> Response:
    """Intent outcomes plus cases that did not emit an intent; legacy orders are excluded."""

    _validate_query_params(request, supported={"day", "state", "token", "underlying"})
    if state and state not in _STATE_FILTERS:
        raise ApiBadRequest("trading_intents_state_invalid", field="state")
    underlying_key = _underlying_key(underlying)
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
        cases = (
            []
            if state
            else repos.trading.console_cases_without_intents(
                since_ms=now_ms - _WINDOW_MS,
                underlying_key=underlying_key,
                limit=_ROW_LIMIT + 1,
            )
        )
    return _etagged(
        {
            "intents": [_intent(row) for row in intents[:_ROW_LIMIT]],
            "cases_without_intents": [_case(row) for row in cases[:_ROW_LIMIT]],
            "complete": len(intents) <= _ROW_LIMIT and len(cases) <= _ROW_LIMIT,
            "window_hours": _WINDOW_MS // 3_600_000,
            "measured_at_ms": now_ms,
        },
        request,
        envelope=_IntentsEnvelope,
    )


@router.get("/trading/gate", response_model=_GateEnvelope)
def get_trading_gate(request: Request) -> Response:
    _validate_query_params(request, supported={"token"})
    runtime = _authenticated_runtime(request)
    now_ms = int(time.time() * 1000)
    with runtime.repositories() as repos:
        rows = repos.trading.gate_decisions_since(since_ms=now_ms - _WINDOW_MS, limit=_GATE_LIMIT + 1)
    return _etagged(
        {
            "decisions": [_gate_decision(row) for row in rows[:_GATE_LIMIT]],
            "complete": len(rows) <= _GATE_LIMIT,
            "window_hours": _WINDOW_MS // 3_600_000,
            "measured_at_ms": now_ms,
        },
        request,
        envelope=_GateEnvelope,
    )


@router.get("/trading/events/{event_id}", response_model=_EventCaseEnvelope)
def get_trading_event_case(request: Request, event_id: str) -> Response:
    _validate_query_params(request, supported={"lane", "token"})
    if not event_id or len(event_id) > 128:
        raise ApiBadRequest("trading_event_id_invalid", field="event_id")
    lane = request.query_params.get("lane", "")
    if lane and lane != "oi":
        raise ApiBadRequest("trading_event_lane_invalid", field="lane")
    runtime = _authenticated_runtime(request)
    if lane != "oi":
        return _etagged({"event_id": event_id, "joinable": False}, request, envelope=_EventCaseEnvelope)
    source_key = f"oi:{event_id}:{_OI_METRIC_VERSION}"
    with runtime.repositories() as repos:
        row = repos.trading.console_case_for_source_key(primary_source_key=source_key)
        gate = repos.trading.gate_decision_for_source_key(source_key=source_key)
    if row is None:
        return _etagged(
            {"event_id": event_id, "joinable": True, **_gate(gate)},
            request,
            envelope=_EventCaseEnvelope,
        )
    return _etagged(
        {
            "event_id": event_id,
            "joinable": True,
            "case": _case(row),
            "intent": _intent(row) if row.get("intent_id") is not None else None,
            **_gate(gate),
        },
        request,
        envelope=_EventCaseEnvelope,
    )


def _gate_config(settings: Any) -> dict[str, Any]:
    config = trading_settings_gate(settings)
    return {"version": CANDIDATE_GATE_VERSION, "config_digest": config.digest, **config.snapshot}


def _strategy_configs(settings: Any) -> list[dict[str, Any]]:
    return [
        {
            "strategy_id": strategy.strategy_id,
            "strategy_version": strategy.strategy_version,
            "config_digest": strategy.config_digest,
            "permission": strategy.permission,
            "trigger_kinds": sorted(strategy.trigger_kinds),
            "config": {key: str(value) for key, value in sorted(strategy.config_snapshot.items())},
        }
        for strategy in trading_settings_strategies(settings)
    ]


def _gate_decision(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_key": str(row["source_key"]),
        "event_id": _oi_event_id(row.get("source_key")),
        "underlying_key": row.get("underlying_key"),
        "base_symbol": _base_symbol(row.get("underlying_key")),
        "trigger_kind": str(row["trigger_kind"]),
        "source_observed_at_ms": int(row["source_observed_at_ms"]),
        "case_id": row.get("case_id"),
        **_gate(row),
    }


def _gate(row: dict[str, Any] | None) -> dict[str, Any]:
    if row is None:
        return {"gate_status": None}
    return {
        "gate_status": str(row["status"]),
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


def _underlying_key(value: str) -> str | None:
    raw = str(value or "").strip().upper().removeprefix("XYZ-")
    if not raw:
        return None
    base = raw.removeprefix("CRYPTO:")
    if not _BASE_SYMBOL.fullmatch(base):
        raise ApiBadRequest("trading_intents_underlying_invalid", field="underlying")
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


def _int_map(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, int] = {}
    for key, count in value.items():
        try:
            out[str(key)] = int(count)
        except (TypeError, ValueError):
            continue
    return out


def _intent(row: dict[str, Any]) -> dict[str, Any]:
    decimal_fields = (
        "target_notional_usd",
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
            "execution_capability_snapshot_sha256",
            "blacklist_revision_at_emission",
            "blacklist_snapshot_sha256_at_emission",
            "instrument_id",
            "side",
            "valid_until_ms",
            "execution_state",
            "execution_phase",
            "terminal_outcome",
            "reason_code",
            "opened_at_ms",
            "protected_at_ms",
            "closed_at_ms",
            "flat_verified_at_ms",
            "realized_pnl_currency",
            "commissions_by_currency",
            "created_at_ms",
            "updated_at_ms",
            "trigger_kind",
            "strategy_id",
            "strategy_version",
            "case_state",
            "regime",
            "policy_decision",
            "policy_reason",
            "pre_move_bps",
            "regime_reason",
            "case_observed_at_ms",
        )
    }
    result.update({key: _decimal(row.get(key)) for key in decimal_fields})
    result.update(
        {
            "event_id": _oi_event_id(row.get("primary_source_key")),
            "underlying_key": str(row["underlying_key"]),
            "base_symbol": _base_symbol(row.get("underlying_key")),
            "strategy_config": _frozen_config(row.get("strategy_config")),
        }
    )
    return result


def _case(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "base_symbol": _base_symbol(row.get("underlying_key")),
        "case_id": str(row["case_id"]),
        "event_id": _oi_event_id(row.get("primary_source_key")),
        "strategy_id": str(row["strategy_id"]),
        "strategy_version": str(row["strategy_version"]),
        "trigger_kind": str(row["trigger_kind"]),
        "created_at_ms": int(row["case_created_at_ms"] if "case_created_at_ms" in row else row["created_at_ms"]),
        "decided_at_ms": row.get("decided_at_ms"),
        "observed_at_ms": int(row["observed_at_ms"]),
        "policy_decision": row.get("policy_decision"),
        "policy_reason": row.get("policy_reason"),
        "pre_move_bps": row.get("pre_move_bps"),
        "regime": row.get("regime"),
        "regime_reason": row.get("regime_reason"),
        "state": str(row["state"]),
        "strategy_config": _frozen_config(row.get("strategy_config")),
        "underlying_key": str(row["underlying_key"]),
    }


def _frozen_config(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in sorted(value.items())}


__all__ = ["router"]
