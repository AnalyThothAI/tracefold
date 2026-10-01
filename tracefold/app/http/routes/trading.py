"""Read-only Trading runtime, LIVE Case, scoreboard, and DEMO execution facts."""

from __future__ import annotations

import re
import time
from typing import Annotated, Any, Final

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from tracefold.app.analysis_status import analysis_status_projection
from tracefold.app.execution_status import execution_readiness_projection
from tracefold.trading.stages import execution_stage

from ..dependencies import _authenticated_runtime, _validate_query_params
from ..exceptions import ApiBadRequest
from ..responses import _etagged, _validated_json
from ..schemas import common as api_schemas
from ..schemas import trading as trading_schemas

router = APIRouter()
_StatusEnvelope = api_schemas.ApiEnvelope[trading_schemas.TradingStatusData]
_CasesEnvelope = api_schemas.ApiEnvelope[trading_schemas.TradingCasesData]
_ScoreboardEnvelope = api_schemas.ApiEnvelope[trading_schemas.TradingScoreboardData]
_ExecutionsEnvelope = api_schemas.ApiEnvelope[trading_schemas.TradingExecutionsData]
_WINDOW_MS: Final = 24 * 3_600_000
_DAY_MS: Final = 86_400_000
_ROW_LIMIT: Final = 100
_CASE_ID: Final = re.compile(r"^[0-9a-f]{64}$")


def _case_id(value: str) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if _CASE_ID.fullmatch(raw) is None:
        raise ApiBadRequest("trading_cases_case_id_invalid", field="case_id")
    return raw


def _case(row: dict[str, Any]) -> dict[str, Any]:
    fields = trading_schemas.TradingCaseData.model_fields
    return {key: _decimal_strings(value) for key, value in row.items() if key in fields}


def _decimal_strings(value: Any) -> Any:
    from decimal import Decimal

    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, list):
        return [_decimal_strings(item) for item in value]
    if isinstance(value, dict):
        return {key: _decimal_strings(item) for key, item in value.items()}
    return value


@router.get("/trading/status", response_model=_StatusEnvelope)
def get_trading_status(request: Request) -> Response:
    _validate_query_params(request, supported={"token"})
    runtime = _authenticated_runtime(request)
    now_ms = int(time.time() * 1_000)
    with runtime.repositories() as repos:
        execution = runtime.settings.trading.execution
        analysis_runtime = repos.runtime.analysis_detail(execution.account_slot)
        last_case_at_ms = repos.trading.latest_case_created_at_ms()
        state = repos.trading.account(execution.account_slot)
        control = repos.trading.control(execution.account_slot)
        process = repos.runtime.process(kind="executor", key=execution.account_slot).read()
    response = _validated_json(
        _StatusEnvelope,
        {
            "ok": True,
            "data": {
                "decision": analysis_status_projection(
                    runtime.settings, analysis_runtime, now_ms=now_ms, last_case_at_ms=last_case_at_ms
                ),
                "execution": execution_readiness_projection(
                    execution,
                    state,
                    control,
                    now_ns=time.time_ns(),
                    process=process,
                ),
            },
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/trading/cases", response_model=_CasesEnvelope)
def get_trading_cases(
    request: Request,
    case_id: Annotated[str, Query(max_length=64)] = "",
    source_item_id: Annotated[str, Query(max_length=256)] = "",
    state: Annotated[str, Query(max_length=16)] = "",
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> Response:
    _validate_query_params(request, supported={"token", "case_id", "source_item_id", "state", "limit"})
    identity = _case_id(case_id)
    if identity and source_item_id:
        raise ApiBadRequest("trading_cases_identity_ambiguous", field="case_id")
    if state and state not in ("pending", "running", "complete", "failed"):
        raise ApiBadRequest("trading_cases_state_invalid", field="state")
    runtime = _authenticated_runtime(request)
    with runtime.repositories() as repos:
        if identity:
            item = repos.trading.analysis_case(identity)
            rows = [] if item is None else [item]
        else:
            rows = repos.trading.analysis_cases(
                since_ms=int(time.time() * 1_000) - _WINDOW_MS,
                limit=limit,
                state=state or None,
                source_item_id=source_item_id or None,
            )
    return _etagged(
        {"cases": [_case(row) for row in rows], "total": len(rows), "complete": True}, request, envelope=_CasesEnvelope
    )


@router.get("/trading/scoreboard", response_model=_ScoreboardEnvelope)
def get_trading_scoreboard(
    request: Request,
    since_ms: Annotated[int | None, Query(ge=0)] = None,
    until_ms: Annotated[int | None, Query(ge=1)] = None,
    program: Annotated[str, Query(max_length=64)] = "",
) -> Response:
    _validate_query_params(request, supported={"token", "since_ms", "until_ms", "program"})
    runtime = _authenticated_runtime(request)
    until = int(time.time() * 1_000) if until_ms is None else until_ms
    since = until - 7 * _DAY_MS if since_ms is None else since_ms
    if since >= until or until - since > 90 * _DAY_MS:
        raise ApiBadRequest("trading_scoreboard_window_invalid", field="since_ms")
    if program and _CASE_ID.fullmatch(program) is None:
        raise ApiBadRequest("trading_scoreboard_program_invalid", field="program")
    with runtime.repositories() as repos:
        result = repos.trading.scoreboard(since_ms=since, until_ms=until, program_sha=program or None)
    return _etagged(result, request, envelope=_ScoreboardEnvelope)


@router.get("/trading/executions", response_model=_ExecutionsEnvelope)
def get_trading_executions(request: Request, case_id: Annotated[str, Query(max_length=64)] = "") -> Response:
    _validate_query_params(request, supported={"token", "case_id"})
    runtime = _authenticated_runtime(request)
    identity = _case_id(case_id)
    now_ms = int(time.time() * 1_000)
    now_ns = now_ms * 1_000_000
    with runtime.repositories() as repos:
        rows = repos.trading.console_executions(
            since_ns=0 if identity else (now_ms - _WINDOW_MS) * 1_000_000,
            limit=_ROW_LIMIT + 1,
            case_id=identity,
        )
        day_start_ns = (now_ms - now_ms % _DAY_MS) * 1_000_000
        totals = repos.trading.console_realized_totals(
            account_slot=runtime.settings.trading.execution.account_slot,
            day_start_ns=day_start_ns,
            day_end_ns=day_start_ns + _DAY_MS * 1_000_000,
        )
    return _etagged(
        {
            "executions": [_execution(row, now_ns=now_ns) for row in rows[:_ROW_LIMIT]],
            "totals": _totals(totals),
            "complete": len(rows) <= _ROW_LIMIT,
        },
        request,
        envelope=_ExecutionsEnvelope,
    )


def _execution(row: dict[str, Any], *, now_ns: int) -> dict[str, Any]:
    reason = _string_or_none(row.get("disposition_reason"))
    fill_quantity = _string_or_none(row.get("fill_quantity"))
    stop_trigger_price = _string_or_none(row.get("stop_trigger_price"))
    take_profit_trigger_price = _string_or_none(row.get("take_profit_trigger_price"))
    realized = _string_or_none(row.get("realized_pnl_usd"))
    net = _string_or_none(row.get("net_pnl_usd"))
    return {
        "source": str(row["source"]),
        "entry_id": str(row["entry_id"]),
        "case_id": _string_or_none(row.get("case_id")),
        "market_key": str(row["market_key"]),
        "direction": str(row["direction"]),
        "observed_at_ns": int(row["observed_at_ns"]),
        "disposition_reason": reason,
        "fill_quantity": fill_quantity,
        "fill_avg_price": _string_or_none(row.get("fill_avg_price")),
        "stop_trigger_price": stop_trigger_price,
        "take_profit_trigger_price": take_profit_trigger_price,
        "entry_filled_at_ns": _int_or_none(row.get("entry_filled_at_ns")),
        "position_closed_at_ns": _int_or_none(row.get("position_closed_at_ns")),
        "exit_price": _string_or_none(row.get("exit_price")),
        "realized_pnl_usd": realized,
        "fees_usd": _string_or_none(row.get("fees_usd")),
        "net_pnl_usd": net,
        "exit_reason": _string_or_none(row.get("exit_reason")),
        "pnl_status": _string_or_none(row.get("pnl_status")),
        "plan_status": _string_or_none(row.get("plan_status")),
        "account_slot": _string_or_none(row.get("account_slot")),
        "instrument_id": _string_or_none(row.get("instrument_id")),
        "entry_client_order_id": _string_or_none(row.get("entry_client_order_id")),
        "entry_error_code": _int_or_none(row.get("entry_error_code")),
        "stop_distance_bps": _int_or_none(row.get("stop_distance_bps")),
        "take_profit_bps": _int_or_none(row.get("take_profit_bps")),
        "max_holding_ns": _int_or_none(row.get("max_holding_ns")),
        "duration_ns": _int_or_none(row.get("duration_ns")),
        # The venue's own `order_status` and `position_status` are inputs to this word, not a second
        # answer beside it (#537 PR-5). The Signal's own TTL is an input for the same reason (#604 T3).
        "stage": execution_stage(
            plan_status=_string_or_none(row.get("plan_status")),
            exit_reason=_string_or_none(row.get("exit_reason")),
            disposition_reason=reason,
            order_status=_string_or_none(row.get("order_status")),
            fill_quantity=fill_quantity,
            stop_trigger_price=stop_trigger_price,
            take_profit_trigger_price=take_profit_trigger_price,
            position_status=_string_or_none(row.get("position_status")),
            expires_at_ns=_int_or_none(row.get("expires_at_ns")),
            now_ns=now_ns,
        ),
    }


def _totals(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "realized_known_today_usd": _string_or_none(row["realized_known_today_usd"]),
        "realized_known_total_usd": _string_or_none(row["realized_known_total_usd"]),
        "net_known_today_usd": _string_or_none(row["net_known_today_usd"]),
        "net_known_total_usd": _string_or_none(row["net_known_total_usd"]),
        **{
            key: int(row[key])
            for key in (
                "closed_today",
                "closed_total",
                "pnl_known_today",
                "pnl_known_total",
                "pnl_missing_today",
                "pnl_missing_total",
                "net_known_today",
                "net_known_total",
                "net_missing_today",
                "net_missing_total",
            )
        },
    }


def _int_or_none(value: Any) -> int | None:
    return None if value is None else int(value)


def _string_or_none(value: Any) -> str | None:
    return None if value is None else str(value)


__all__ = ["router"]
