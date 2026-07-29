from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from tracefold.app.http import schemas as api_schemas
from tracefold.app.http.dependencies import _authenticated_runtime, _now_ms
from tracefold.app.http.exceptions import ApiBadRequest, ApiUnavailable
from tracefold.app.http.responses import _validated_json
from tracefold.macro import (
    MACRO_MODULE_IDS,
    MacroModuleId,
    resolve_thesis_session,
    schema_version_for_module,
    thesis_cutoff_ms,
)

router = APIRouter()

_GENERATING_RUN_STATES = frozenset({"pending", "running", "retryable"})
_FAILED_RUN_STATES = frozenset({"failed", "config_error"})
_NOT_PUBLISHED_RUN_STATES = frozenset({"not_published"})
_MODULE_HREFS = {
    "rates_fed": "/macro/rates-fed",
    "economy_inflation": "/macro/economy-inflation",
    "liquidity_funding": "/macro/liquidity-funding",
    "credit": "/macro/credit",
    "volatility": "/macro/volatility",
    "cross_asset": "/macro/cross-asset",
}


@router.get(
    "/macro/overview",
    response_model=api_schemas.ApiEnvelope[api_schemas.MacroOverviewReadData],
)
def macro_overview(request: Request) -> JSONResponse:
    runtime = _authenticated_macro_runtime(request)
    _reject_query_params(request)
    read_at_ms = _now_ms()
    session_date = resolve_thesis_session(now_ms=read_at_ms)
    with runtime.repositories() as repos:
        module_rows = {row["module_id"]: row for row in repos.macro.all_modules_current()}
        thesis_state = repos.macro_thesis.state(session_date)
        live_delta, outcome_replay = _follow_up_payloads(repos, thesis_state)

    module_payloads = [_module_payload(module_id, module_rows.get(module_id)) for module_id in MACRO_MODULE_IDS]
    thesis = _thesis_payload(thesis_state)
    role_by_module = {
        str(item["module_id"]): str(item["role"])
        for item in (thesis or {}).get("module_assessments", ())
        if isinstance(item, dict) and item.get("module_id") and item.get("role")
    }
    modules = [
        _module_summary(payload, role=role_by_module.get(str(payload["module_id"]))) for payload in module_payloads
    ]
    payload = {
        "schema_version": "macro_overview_v5",
        "read_at_ms": read_at_ms,
        "transport": {
            "state": "current",
            "last_successful_read_at_ms": read_at_ms,
            "reason": None,
        },
        "session_date": session_date,
        "cutoff_ms": (int(thesis_state["cutoff_ms"]) if thesis_state is not None else thesis_cutoff_ms(session_date)),
        "latest_fact_at_ms": max(
            (int(module["latest_fact_at_ms"]) for module in modules),
            default=0,
        ),
        "thesis_state": _overview_thesis_state(thesis_state),
        "thesis": thesis,
        "live_delta": live_delta,
        "outcome_replay": outcome_replay,
        "modules": modules,
        "data_quality": _data_quality_overview(modules),
    }
    return _validated_json(
        api_schemas.ApiEnvelope[api_schemas.MacroOverviewReadData],
        {"ok": True, "data": payload},
    )


def _read_module(request: Request, module_id: MacroModuleId) -> dict[str, Any]:
    runtime = _authenticated_macro_runtime(request)
    _reject_query_params(request)
    with runtime.repositories() as repos:
        row = repos.macro.module_current(module_id)
    return _module_payload(module_id, row)


@router.get(
    "/macro/rates-fed",
    response_model=api_schemas.ApiEnvelope[api_schemas.MacroRatesFedReadData],
)
def macro_rates_fed(request: Request) -> JSONResponse:
    return _validated_json(
        api_schemas.ApiEnvelope[api_schemas.MacroRatesFedReadData],
        {"ok": True, "data": _read_module(request, "rates_fed")},
    )


@router.get(
    "/macro/economy-inflation",
    response_model=api_schemas.ApiEnvelope[api_schemas.MacroEconomyInflationReadData],
)
def macro_economy_inflation(request: Request) -> JSONResponse:
    return _validated_json(
        api_schemas.ApiEnvelope[api_schemas.MacroEconomyInflationReadData],
        {"ok": True, "data": _read_module(request, "economy_inflation")},
    )


@router.get(
    "/macro/liquidity-funding",
    response_model=api_schemas.ApiEnvelope[api_schemas.MacroLiquidityFundingReadData],
)
def macro_liquidity_funding(request: Request) -> JSONResponse:
    return _validated_json(
        api_schemas.ApiEnvelope[api_schemas.MacroLiquidityFundingReadData],
        {"ok": True, "data": _read_module(request, "liquidity_funding")},
    )


@router.get(
    "/macro/credit",
    response_model=api_schemas.ApiEnvelope[api_schemas.MacroCreditReadData],
)
def macro_credit(request: Request) -> JSONResponse:
    return _validated_json(
        api_schemas.ApiEnvelope[api_schemas.MacroCreditReadData],
        {"ok": True, "data": _read_module(request, "credit")},
    )


@router.get(
    "/macro/volatility",
    response_model=api_schemas.ApiEnvelope[api_schemas.MacroVolatilityReadData],
)
def macro_volatility(request: Request) -> JSONResponse:
    return _validated_json(
        api_schemas.ApiEnvelope[api_schemas.MacroVolatilityReadData],
        {"ok": True, "data": _read_module(request, "volatility")},
    )


@router.get(
    "/macro/cross-asset",
    response_model=api_schemas.ApiEnvelope[api_schemas.MacroCrossAssetReadData],
)
def macro_cross_asset(request: Request) -> JSONResponse:
    return _validated_json(
        api_schemas.ApiEnvelope[api_schemas.MacroCrossAssetReadData],
        {"ok": True, "data": _read_module(request, "cross_asset")},
    )


@router.get(
    "/macro/research",
    response_model=api_schemas.ApiEnvelope[api_schemas.MacroThesisDetailReadData],
)
def macro_research(
    request: Request,
    session_date: Annotated[date | None, Query()] = None,
) -> JSONResponse:
    runtime = _authenticated_macro_runtime(request)
    _validate_research_query_params(request)
    current_session = resolve_thesis_session(now_ms=_now_ms())
    target_session = session_date or current_session
    with runtime.repositories() as repos:
        row = repos.macro_thesis.state(target_session)
        live_delta, outcome_replay = _follow_up_payloads(repos, row)
        history = [_history_payload(item) for item in repos.macro_thesis.publications(limit=30)]
    payload = {
        "state": _detail_state(
            row,
            requested_session=target_session,
            current_session=current_session,
        ),
        "requested_session_date": target_session,
        "current_session_date": current_session,
        "thesis": _thesis_payload(row),
        "live_delta": live_delta,
        "outcome_replay": outcome_replay,
        "run": _run_payload(row),
        "history": history,
    }
    return _validated_json(
        api_schemas.ApiEnvelope[api_schemas.MacroThesisDetailReadData],
        {"ok": True, "data": payload},
    )


def _authenticated_macro_runtime(request: Request) -> Any:
    runtime = _authenticated_runtime(request, allow_query_token=False)
    if "token" in request.query_params:
        raise ApiBadRequest("unsupported_query_param", field="token")
    return runtime


def _reject_query_params(request: Request) -> None:
    for name in request.query_params:
        raise ApiBadRequest("unsupported_query_param", field=name)


def _validate_research_query_params(request: Request) -> None:
    for name in request.query_params:
        if name != "session_date":
            raise ApiBadRequest("unsupported_query_param", field=name)


def _module_payload(
    module_id: MacroModuleId,
    row: dict[str, Any] | None,
) -> dict[str, Any]:
    expected_schema = schema_version_for_module(module_id)
    if (
        row is not None
        and isinstance(row.get("payload_json"), dict)
        and row["payload_json"].get("schema_version") == expected_schema
    ):
        return dict(row["payload_json"])
    if row is None:
        raise ApiUnavailable(f"macro_module_not_materialized:{module_id}")
    raise ApiUnavailable(f"macro_module_schema_mismatch:{module_id}")


def _module_summary(
    payload: dict[str, Any],
    *,
    role: str | None,
) -> dict[str, Any]:
    status = dict(payload["status"])
    coverage = dict(status["coverage"])
    current_health = dict(status["current_health"])
    history_depth = dict(status["history_depth"])
    return {
        "module_id": payload["module_id"],
        "label": payload["label"],
        "role": role,
        "coverage_state": coverage["state"],
        "current_health_state": current_health["state"],
        "history_depth_state": history_depth["state"],
        "latest_fact_at_ms": int(payload["latest_fact_at_ms"]),
        "summary": payload["summary"],
        "coverage_gap_count": sum(item["state"] != "available" for item in coverage["capabilities"]),
        "current_health_gap_count": max(
            0,
            int(current_health["tracked_datasets"]) - int(current_health["current_datasets"]),
        ),
        "history_gap_count": max(
            0,
            int(history_depth["tracked_datasets"]) - int(history_depth["complete_datasets"]),
        ),
        "href": _MODULE_HREFS[str(payload["module_id"])],
    }


def _data_quality_overview(modules: list[dict[str, Any]]) -> dict[str, Any]:
    coverage_states = [str(module["coverage_state"]) for module in modules]
    current_states = [str(module["current_health_state"]) for module in modules]
    history_states = [
        str(module["history_depth_state"]) for module in modules if module["history_depth_state"] != "not_required"
    ]
    coverage_state = "complete" if set(coverage_states) == {"complete"} else "partial"
    if set(current_states) == {"current"}:
        current_health_state = "current"
    elif set(current_states) == {"unavailable"}:
        current_health_state = "unavailable"
    else:
        current_health_state = "degraded"
    if not history_states:
        history_depth_state = "not_required"
    elif set(history_states) == {"complete"}:
        history_depth_state = "complete"
    elif set(history_states) == {"insufficient"}:
        history_depth_state = "insufficient"
    else:
        history_depth_state = "partial"
    return {
        "coverage_state": coverage_state,
        "current_health_state": current_health_state,
        "history_depth_state": history_depth_state,
        "coverage_gap_count": sum(int(module["coverage_gap_count"]) for module in modules),
        "current_health_gap_count": sum(int(module["current_health_gap_count"]) for module in modules),
        "history_gap_count": sum(int(module["history_gap_count"]) for module in modules),
    }


def _overview_thesis_state(row: dict[str, Any] | None) -> str:
    if row is None:
        return "missing"
    if isinstance(row.get("thesis_json"), dict):
        return "published"
    status = str(row.get("status") or "missing")
    if status in (_GENERATING_RUN_STATES | _FAILED_RUN_STATES | _NOT_PUBLISHED_RUN_STATES):
        return status
    return "missing"


def _detail_state(
    row: dict[str, Any] | None,
    *,
    requested_session: date,
    current_session: date,
) -> str:
    if row is None:
        return "missing"
    if isinstance(row.get("thesis_json"), dict):
        return "current" if requested_session == current_session else "historical"
    status = str(row.get("status") or "")
    if status in _GENERATING_RUN_STATES:
        return "generating"
    if status in _FAILED_RUN_STATES:
        return "failed"
    if status in _NOT_PUBLISHED_RUN_STATES:
        return "not_published"
    return "missing"


def _thesis_payload(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None or not isinstance(row.get("thesis_json"), dict):
        return None
    return dict(row["thesis_json"])


def _follow_up_payloads(
    repos: Any,
    state: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if state is None or not state.get("publication_id"):
        return None, None
    publication_id = str(state["publication_id"])
    live_row = repos.macro_thesis.latest_live_delta(publication_id)
    outcome_row = repos.macro_thesis.latest_outcome_replay(publication_id)
    live_delta = (
        dict(live_row["payload_json"])
        if live_row is not None and isinstance(live_row.get("payload_json"), dict)
        else None
    )
    outcome_replay = (
        dict(outcome_row["payload_json"])
        if outcome_row is not None and isinstance(outcome_row.get("payload_json"), dict)
        else None
    )
    return live_delta, outcome_replay


def _run_payload(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "session_date": row["session_date"],
        "status": str(row["status"]),
        "evidence_pack_id": str(row["evidence_pack_id"]),
        "attempt_count": int(row["attempt_count"]),
        "max_attempts": int(row["max_attempts"]),
        "error_code": row.get("last_error_code"),
        "error_message": row.get("last_error_message"),
        "updated_at_ms": int(row["updated_at_ms"]),
    }


def _history_payload(row: dict[str, Any]) -> dict[str, Any]:
    thesis = dict(row["thesis_json"])
    mainline = dict(thesis["mainline"])
    return {
        "publication_id": thesis["publication_id"],
        "session_date": thesis["session_date"],
        "cutoff_ms": int(thesis["cutoff_ms"]),
        "published_at_ms": int(thesis["published_at_ms"]),
        "title": mainline["title"],
        "stance": mainline["stance"],
        "confidence": mainline["confidence"],
        "horizon": mainline["horizon"],
    }
