"""Trading reads plus the one authenticated bounded operator-command append.

Three reads and one write. `GET /api/trading/signals` and the two `GET /api/trading/execution/*`
projections were three more public shapes over ledgers the desk already reads folded: the Signal list
is `executions[]` with its venue outcome attached, the raw observation stream is what that fold reads,
and the Command list is `executions[].commands`. Nothing in the browser called any of the three, and
`tracefold trading signals | observations | commands` reads the same repository directly (#537 PR-5).

`GET /api/trading/gate` and `GET /api/trading/gate/{event_id}` left on the same terms (#589 PR-2).
#553 PR-1 removed the OI frame table that joined each admission row to its Event, and with it the
only browser reader either route ever had; what remained was a public HTTP shape over the admission
ledger with no caller. `tracefold trading gate [--source-key KEY] [--since-ms N]` reads the same two
repository statements directly, and the runbook in Operations reads the row in SQL.
"""

from __future__ import annotations

import re
import time
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from tracefold.app.execution_status import execution_readiness_projection
from tracefold.news.oi_signals import METRIC_VERSION as OI_METRIC_VERSION
from tracefold.trading import (
    OperatorCommandError,
    command_stage,
    execution_stage,
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
_CasesEnvelope = api_schemas.ApiEnvelope[trading_schemas.TradingCasesData]
_ExecutionsEnvelope = api_schemas.ApiEnvelope[trading_schemas.TradingExecutionsData]
_CommandReceiptEnvelope = api_schemas.ApiEnvelope[trading_schemas.TradingOperatorCommandReceiptData]

_WINDOW_MS: Final = 24 * 3_600_000
_ROW_LIMIT: Final = 100
_OI_METRIC_VERSION: Final = OI_METRIC_VERSION
_BASE_SYMBOL: Final = re.compile(r"^[A-Z0-9._-]{1,24}$")
_CASE_STATE_FILTERS: Final[dict[str, tuple[str, ...]]] = {
    "open": ("PENDING", "RUNNING"),
    "no_trade": ("NO_TRADE",),
    "blocked": ("BLOCKED",),
    "emitted": ("SIGNAL_EMITTED",),
}
_CONSOLE_COMMAND_ACTIONS: Final = frozenset({"pause_entries", "resume_entries", "flatten"})
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
        execution = runtime.settings.trading.execution
        execution_status = execution_readiness_projection(
            execution,
            repos.trading.execution_runtime_state(execution.account_slot),
            repos.trading.execution_runtime_control_state(execution.account_slot),
            now_ns=now_ms * 1_000_000,
        )
    return _etagged(
        {"decision": {"last_case_at_ms": last_case_at_ms}, "execution": execution_status},
        request,
        envelope=_StatusEnvelope,
    )


@router.get("/trading/cases", response_model=_CasesEnvelope)
def get_trading_cases(
    request: Request,
    underlying: Annotated[str, Query(max_length=32)] = "",
    state: Annotated[str, Query(max_length=16)] = "",
) -> Response:
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
    return _etagged(
        {
            "cases": [_case(row) for row in rows[:_ROW_LIMIT]],
            "state_counts_24h": states,
            "reason_counts_24h": reasons,
            "complete": len(rows) <= _ROW_LIMIT,
            "window_hours": _WINDOW_MS // 3_600_000,
        },
        request,
        envelope=_CasesEnvelope,
    )


@router.get("/trading/executions", response_model=_ExecutionsEnvelope)
def get_trading_executions(request: Request) -> Response:
    """Today's desk table: one row per entry identity, plus one per operator Command (#528 PR-3)."""

    _validate_query_params(request, supported={"token"})
    runtime = _authenticated_runtime(request)
    now_ms = int(time.time() * 1000)
    now_ns = now_ms * 1_000_000
    since_ns = (now_ms - _WINDOW_MS) * 1_000_000
    with runtime.repositories() as repos:
        rows = repos.trading.console_executions(since_ns=since_ns, limit=_ROW_LIMIT + 1)
        commands = repos.trading.console_operator_intents(since_ns=since_ns, action=None, limit=_ROW_LIMIT)
    return _etagged(
        {
            "executions": [_execution(row) for row in rows[:_ROW_LIMIT]],
            "commands": [_execution_command(row, now_ns=now_ns) for row in commands],
            "complete": len(rows) <= _ROW_LIMIT,
        },
        request,
        envelope=_ExecutionsEnvelope,
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
            now_ns=now_ns,
        )
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


def _case(row: dict[str, Any]) -> dict[str, Any]:
    manifest_value = row.get("manifest")
    manifest: dict[str, Any] = manifest_value if isinstance(manifest_value, dict) else {}
    contexts_value = manifest.get("contexts")
    contexts: dict[str, Any] = contexts_value if isinstance(contexts_value, dict) else {}
    market_value = contexts.get("market")
    market: dict[str, Any] = market_value if isinstance(market_value, dict) else {}
    return {
        "case_id": str(row["case_id"]),
        "event_id": _oi_event_id(row.get("primary_source_key")),
        "base_symbol": _base_symbol(row.get("underlying_key")),
        "market_key": manifest.get("market_key"),
        "manifest_version": manifest.get("manifest_version"),
        # From the manifest, which is what the lane compares a Case against before it decides one.
        # Three columns beside it said the same thing and nothing read them (#537 PR-3).
        "policy_id": _string_or_none(manifest.get("policy_id")),
        "policy_config_digest": _string_or_none(manifest.get("policy_config_digest")),
        "policy_config": _frozen_config(manifest.get("policy_config")),
        "policy_checks": _policy_checks(row.get("policy_checks")),
        "state": str(row["state"]),
        "policy_reason": row.get("policy_reason"),
        "mark_price": _string_or_none(market.get("mark_price")),
        "pre_move_bps": _int_or_none(market.get("pre_move_bps")),
        "observed_at_ms": int(row["observed_at_ms"]),
        "created_at_ms": int(row["case_created_at_ms"]),
        "decided_at_ms": _int_or_none(row.get("decided_at_ms")),
    }


def _execution(row: dict[str, Any]) -> dict[str, Any]:
    reason = _string_or_none(row.get("disposition_reason"))
    fill_quantity = _string_or_none(row.get("fill_quantity"))
    stop_trigger_price = _string_or_none(row.get("stop_trigger_price"))
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
        "exit_price": _string_or_none(row.get("exit_price")),
        "realized_pnl_usd": _string_or_none(row.get("realized_pnl_usd")),
        "exit_reason": _string_or_none(row.get("exit_reason")),
        # The venue's own `order_status` and `position_status` are inputs to this word, not a second
        # answer beside it: the table renders the stage, and publishing both let a reader compare a
        # raw venue string against the server's derivation of the same row (#537 PR-5).
        "stage": execution_stage(
            disposition_reason=reason,
            order_status=_string_or_none(row.get("order_status")),
            fill_quantity=fill_quantity,
            stop_trigger_price=stop_trigger_price,
            position_status=_string_or_none(row.get("position_status")),
        ),
    }


def _execution_command(row: dict[str, Any], *, now_ns: int) -> dict[str, Any]:
    return {
        "command_id": str(row["command_id"]),
        "action": str(row["action"]),
        "requested_at_ns": int(row["requested_at_ns"]),
        "stage": command_stage(
            disposition=_string_or_none(row.get("disposition")),
            disposition_reason=_string_or_none(row.get("disposition_reason")),
            expires_at_ns=int(row["expires_at_ns"]),
            now_ns=now_ns,
        ),
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
