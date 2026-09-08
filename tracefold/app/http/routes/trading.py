"""Read-only trading monitoring; operator commands have no HTTP ingress (#624).

Three reads. `GET /api/trading/signals` and the two `GET /api/trading/execution/*`
projections were three more public shapes over ledgers the desk already reads folded: the Signal list
is `executions[]` with its venue outcome attached, the raw observation stream is what that fold reads,
and the Command list was the console control ledger. Nothing in the browser called any of the three, and
`tracefold trading signals | observations | commands` reads the same repository directly (#537 PR-5).

`GET /api/trading/gate` and `GET /api/trading/gate/{event_id}` left on the same terms (#589 PR-2).
#553 PR-1 removed the OI frame table that joined each admission row to its Event, and with it the
only browser reader either route ever had; what remained was a public HTTP shape over the admission
ledger with no caller. `tracefold trading gate [--source-key KEY] [--since-ms N]` reads the same two
repository statements directly, and the runbook in Operations reads the row in SQL.

The admission ledger comes back here as a distribution rather than as rows (#604 T3): `cases`
publishes `admission_counts_24h`, a `count(*)` per `(status, reason)` pair over the same window its
two Case distributions use. That is the funnel's top -- how many frames the lane looked at and what
admission answered -- and it is not the per-frame `decisions[]` #589 PR-2 deleted; no frame identity,
evidence blob or Case link travels with a count.
"""

from __future__ import annotations

import re
import time
from typing import Annotated, Any, Final, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from tracefold.app.execution_status import execution_readiness_projection
from tracefold.news.oi_signals import METRIC_VERSION as OI_METRIC_VERSION
from tracefold.trading import execution_stage

from ..dependencies import _authenticated_runtime, _validate_query_params
from ..exceptions import ApiBadRequest
from ..read_cursor import decode_read_cursor, encode_read_cursor
from ..responses import _etagged
from ..schemas import common as api_schemas
from ..schemas import trading as trading_schemas

router = APIRouter()
_StatusEnvelope = api_schemas.ApiEnvelope[trading_schemas.TradingStatusData]
_CasesEnvelope = api_schemas.ApiEnvelope[trading_schemas.TradingCasesData]
_ExecutionsEnvelope = api_schemas.ApiEnvelope[trading_schemas.TradingExecutionsData]

_WINDOW_MS: Final = 24 * 3_600_000
_DAY_MS: Final = 86_400_000
_ROW_LIMIT: Final = 100
_OI_METRIC_VERSION: Final = OI_METRIC_VERSION
# The identity shape every Case the lane has ever written has (`uuid4().hex`), widened to the bounded
# identity alphabet the rest of the Trading ledgers use so a Case frozen under an older naming still
# opens. It is a primary key, so anything outside it cannot name a row and is refused rather than read.
_CASE_ID: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,127}$")


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
    case_id: Annotated[str, Query(max_length=256)] = "",
    view: Literal["summary", "list"] = "summary",
    state: Literal["", "PENDING", "RUNNING", "NO_TRADE", "SIGNAL_EMITTED", "BLOCKED"] = "",
    asset: Annotated[str, Query(max_length=64)] = "",
    reason: Annotated[str, Query(max_length=128)] = "",
    source_item_id: Annotated[str, Query(max_length=64, pattern=r"^([0-9a-f]{64})?$")] = "",
    cursor: Annotated[str, Query(max_length=512)] = "",
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> Response:
    """Frozen decisions by identity or a scope-bound keyset list; distributions remain independent."""
    _validate_query_params(
        request, supported={"case_id", "token", "view", "state", "asset", "reason", "source_item_id", "cursor", "limit"}
    )
    identity = _case_id(case_id)
    if identity and (state or asset or reason or source_item_id or cursor):
        raise ApiBadRequest("trading_cases_scope_invalid", field="case_id")
    scope = [state, asset.strip().upper(), reason, source_item_id, view]
    position = decode_read_cursor(cursor, scope, error="trading_cases_cursor_invalid")
    runtime = _authenticated_runtime(request)
    now_ms = int(time.time() * 1000)
    window_to = position[0] if position else now_ms
    since_ms = 0 if source_item_id else window_to - _WINDOW_MS
    filters = dict(
        states=(state,) if state else (),
        to_ms=window_to,
        asset=asset.strip().upper() or None,
        reason=reason or None,
        source_item_id=source_item_id or None,
    )
    with runtime.repositories() as repos:
        if identity:
            row = repos.trading.console_case(case_id=identity)
            rows = [] if row is None else [row]
            total = len(rows)
        elif view == "list" or source_item_id:
            rows = repos.trading.console_cases(
                since_ms=since_ms,
                limit=limit + 1,
                cursor_at_ms=position[2] if position else None,
                cursor_id=position[3] if position else "",
                **filters,
            )
            total = repos.trading.console_case_total(since_ms=since_ms, **filters)
        else:
            rows, total = [], 0
        states = repos.trading.case_counts(since_ms=now_ms - _WINDOW_MS)
        reasons = repos.trading.case_reason_counts(since_ms=now_ms - _WINDOW_MS)
        admissions = repos.trading.gate_counts(since_ms=now_ms - _WINDOW_MS)
    next_cursor = None
    if len(rows) > limit:
        last = rows[limit - 1]
        next_cursor = encode_read_cursor(
            scope, to_ms=window_to, value=0, at_ms=last["case_created_at_ms"], identity=last["case_id"]
        )
    return _etagged(
        {
            "cases": [_case(row) for row in rows[:limit]],
            "total": total,
            "next_cursor": next_cursor,
            "window_from_ms": since_ms,
            "window_to_ms": window_to,
            "state_counts_24h": states,
            "reason_counts_24h": reasons,
            "admission_counts_24h": admissions,
            "complete": next_cursor is None,
            "window_hours": _WINDOW_MS // 3_600_000,
        },
        request,
        envelope=_CasesEnvelope,
    )


@router.get("/trading/executions", response_model=_ExecutionsEnvelope)
def get_trading_executions(request: Request, case_id: Annotated[str, Query(max_length=256)] = "") -> Response:
    """One retained row per entry identity with its audited venue outcome."""

    _validate_query_params(request, supported={"token", "case_id"})
    runtime = _authenticated_runtime(request)
    identity = _case_id(case_id)
    now_ms = int(time.time() * 1000)
    now_ns = now_ms * 1_000_000
    since_ns = (now_ms - _WINDOW_MS) * 1_000_000
    with runtime.repositories() as repos:
        rows = repos.trading.console_executions(
            since_ns=0 if identity else since_ns, limit=_ROW_LIMIT + 1, case_id=identity
        )
        # Midnight UTC of the instant this request was served, and the next one. One clock, floored
        # once, so "today" is the same day for the sums and the counts; bounded on both sides because
        # `occurred_at_ns` is the venue's clock and a venue running ahead of this host would otherwise
        # file tomorrow's close under today and leave it there.
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


def _case(row: dict[str, Any]) -> dict[str, Any]:
    manifest_value = row.get("manifest")
    manifest: dict[str, Any] = manifest_value if isinstance(manifest_value, dict) else {}
    contexts_value = manifest.get("contexts")
    contexts: dict[str, Any] = contexts_value if isinstance(contexts_value, dict) else {}
    oi_value = contexts.get("oi")
    oi = oi_value if isinstance(oi_value, dict) else {}
    market_value = contexts.get("market")
    market: dict[str, Any] = market_value if isinstance(market_value, dict) else {}
    return {
        "case_id": str(row["case_id"]),
        "event_id": _oi_event_id(row.get("primary_source_key")),
        "source_item_id": _string_or_none(oi.get("source_item_id")),
        "base_symbol": _base_symbol(row.get("underlying_key")),
        "market_key": manifest.get("market_key"),
        "manifest_version": manifest.get("manifest_version"),
        # From the manifest, which is what the lane compares a Case against before it decides one.
        # Three columns beside it said the same thing and nothing read them (#537 PR-3).
        "policy_id": _string_or_none(manifest.get("policy_id")),
        "policy_config_digest": _string_or_none(manifest.get("policy_config_digest")),
        "policy_checks": _policy_checks(row.get("policy_checks")),
        "state": str(row["state"]),
        "policy_reason": row.get("policy_reason"),
        "mark_price": _string_or_none(market.get("mark_price")),
        "pre_move_bps": _int_or_none(market.get("pre_move_bps")),
        "observed_at_ms": int(row["observed_at_ms"]),
        "created_at_ms": int(row["case_created_at_ms"]),
        "decided_at_ms": _int_or_none(row.get("decided_at_ms")),
    }


def _execution(row: dict[str, Any], *, now_ns: int) -> dict[str, Any]:
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
        "order_reject_reason": _string_or_none(row.get("order_reject_reason")),
        "fill_quantity": fill_quantity,
        "fill_avg_price": _string_or_none(row.get("fill_avg_price")),
        "stop_trigger_price": stop_trigger_price,
        "entry_filled_at_ns": _int_or_none(row.get("entry_filled_at_ns")),
        "position_closed_at_ns": _int_or_none(row.get("position_closed_at_ns")),
        "exit_price": _string_or_none(row.get("exit_price")),
        "realized_pnl_usd": _string_or_none(row.get("realized_pnl_usd")),
        "exit_reason": _string_or_none(row.get("exit_reason")),
        # The venue's own `order_status` and `position_status` are inputs to this word, not a second
        # answer beside it: the table renders the stage, and publishing both let a reader compare a
        # raw venue string against the server's derivation of the same row (#537 PR-5). The Signal's
        # own TTL is an input for the same reason: a Signal that expired without a disposition is
        # `expired`, not work still pending (#604 T3).
        "stage": execution_stage(
            disposition_reason=reason,
            order_status=_string_or_none(row.get("order_status")),
            fill_quantity=fill_quantity,
            stop_trigger_price=stop_trigger_price,
            position_status=_string_or_none(row.get("position_status")),
            expires_at_ns=_int_or_none(row.get("expires_at_ns")),
            now_ns=now_ns,
        ),
    }


def _totals(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "realized_today_usd": str(row.get("realized_today_usd") or "0"),
        "realized_total_usd": str(row.get("realized_total_usd") or "0"),
        "closed_today": int(row.get("closed_today") or 0),
        "closed_total": int(row.get("closed_total") or 0),
    }


def _case_id(value: str) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if _CASE_ID.fullmatch(raw) is None:
        raise ApiBadRequest("trading_cases_case_id_invalid", field="case_id")
    return raw


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


def _int_or_none(value: Any) -> int | None:
    return None if value is None else int(value)


def _string_or_none(value: Any) -> str | None:
    return None if value is None else str(value)


__all__ = ["router"]
