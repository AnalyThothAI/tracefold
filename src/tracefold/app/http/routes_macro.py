from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from tracefold.app.http import schemas as api_schemas
from tracefold.app.http.dependencies import _authenticated_runtime, _now_ms
from tracefold.app.http.exceptions import ApiBadRequest
from tracefold.app.http.responses import _validated_json
from tracefold.macro import (
    MACRO_MODULE_IDS,
    MacroModuleId,
    build_typed_module_payload,
    judgment_cutoff_ms,
    resolve_completed_session,
    resolve_judgment_session,
    schema_version_for_module,
)

router = APIRouter()

_GENERATING_RUN_STATES = frozenset({"pending", "running", "retryable"})
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
    judgment_session = resolve_judgment_session(now_ms=read_at_ms)
    with runtime.repositories() as repos:
        module_rows = {row["module_id"]: row for row in repos.macro.all_modules_current()}
        judgment_row = repos.macro.daily_judgment(judgment_session)
        judgment_status_row = repos.macro.judgment_status(judgment_session)
        research_row = repos.macro_research.research_state()
    module_payloads = [
        _module_payload(
            module_id,
            module_rows.get(module_id),
            judgment_row,
            judgment_status_row,
            read_at_ms,
        )
        for module_id in MACRO_MODULE_IDS
    ]
    modules = [_module_summary(payload) for payload in module_payloads]
    coverage_values = {module["coverage_state"] for module in modules}
    coverage_state = (
        "partial"
        if "partial" in coverage_values or "missing" in coverage_values
        else "licensed_unavailable"
        if "licensed_unavailable" in coverage_values
        else "complete"
    )
    health_values = {module["data_health_state"] for module in modules}
    data_health_state = (
        "current" if health_values == {"current"} else "unavailable" if health_values == {"unavailable"} else "mixed"
    )
    judgment_state = "current" if judgment_row is not None else "missing"
    judgment = (
        dict(judgment_row["judgment_json"])
        if judgment_row is not None and isinstance(judgment_row.get("judgment_json"), dict)
        else None
    )
    payload = {
        "schema_version": "macro_overview_v4",
        "read_at_ms": read_at_ms,
        "judgment_session_date": judgment_session,
        "judgment_cutoff_ms": (
            int(judgment_row["judgment_cutoff_ms"])
            if judgment_row is not None
            else int(judgment_status_row["judgment_cutoff_ms"])
            if judgment_status_row is not None
            else judgment_cutoff_ms(judgment_session)
        ),
        "latest_fact_at_ms": max(
            (int(module["latest_fact_at_ms"]) for module in modules),
            default=0,
        ),
        "coverage_state": coverage_state,
        "data_health_state": data_health_state,
        "judgment_state": judgment_state,
        "judgment_status": _judgment_status_payload(judgment_status_row),
        "daily_judgment": judgment,
        "modules": modules,
        "changes_since_judgment": [
            {"module_id": module["module_id"], "changes": module["top_changes"][:3]} for module in modules
        ],
        "research": _compact_research_payload(research_row),
    }
    return _validated_json(
        api_schemas.ApiEnvelope[api_schemas.MacroOverviewReadData],
        {"ok": True, "data": payload},
    )


def _judgment_status_payload(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "session_date": row["session_date"],
        "judgment_cutoff_ms": int(row["judgment_cutoff_ms"]),
        "state": str(row["state"]),
        "reason_code": str(row["reason_code"]),
        "details": dict(row["details_json"] or {}),
        "attempted_at_ms": int(row["attempted_at_ms"]),
    }


def _read_module(request: Request, module_id: str) -> dict[str, Any]:
    runtime = _authenticated_macro_runtime(request)
    _reject_query_params(request)
    read_at_ms = _now_ms()
    judgment_session = resolve_judgment_session(now_ms=read_at_ms)
    with runtime.repositories() as repos:
        row = repos.macro.module_current(module_id)
        judgment = repos.macro.daily_judgment(judgment_session)
        judgment_status = repos.macro.judgment_status(judgment_session)
    return _module_payload(module_id, row, judgment, judgment_status, read_at_ms)


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
    response_model=api_schemas.ApiEnvelope[api_schemas.MacroResearchReadData],
)
def macro_research(
    request: Request,
    session_date: Annotated[date | None, Query()] = None,
) -> JSONResponse:
    runtime = _authenticated_macro_runtime(request)
    _validate_research_query_params(request)
    current_session = resolve_completed_session(
        now_ms=_now_ms(),
        settle_delay_seconds=0,
    )
    target_session = session_date or current_session
    with runtime.repositories() as repos:
        payload = _research_payload(
            repos.macro_research.research_state(target_session),
            requested_session=target_session,
            current_session=current_session,
        )
    return _validated_json(
        api_schemas.ApiEnvelope[api_schemas.MacroResearchReadData],
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
    judgment: dict[str, Any] | None,
    judgment_status: dict[str, Any] | None,
    read_at_ms: int,
) -> dict[str, Any]:
    expected_schema = schema_version_for_module(module_id)
    if (
        row is not None
        and isinstance(row.get("payload_json"), dict)
        and row["payload_json"].get("schema_version") == expected_schema
    ):
        payload = dict(row["payload_json"])
    else:
        payload = build_typed_module_payload(
            module_id=module_id,
            now_ms=read_at_ms,
            series_rows=[],
            market_rows=[],
            position_rows=[],
            settlement_rows=[],
            release_rows=[],
            document_rows=[],
            target_states=[],
        )
        payload["summary"]["headline"] = "数据链正在初始化"
        payload["summary"]["interpretation"] = "等待新事实链完成首次采集与投影。"
    if judgment is not None:
        payload["status"]["judgment"] = {
            "state": "current",
            "cutoff_ms": int(judgment["judgment_cutoff_ms"]),
        }
    else:
        payload["status"]["judgment"] = {
            "state": "missing",
            "cutoff_ms": (int(judgment_status["judgment_cutoff_ms"]) if judgment_status is not None else None),
        }
    return payload


def _module_summary(payload: dict[str, Any]) -> dict[str, Any]:
    coverage = payload["status"]["coverage"]
    health = payload["status"]["data_health"]
    judgment = payload["status"]["judgment"]
    return {
        "module_id": payload["module_id"],
        "label": payload["label"],
        "coverage_state": coverage["state"],
        "data_health_state": health["state"],
        "judgment_state": judgment["state"],
        "latest_fact_at_ms": int(payload["latest_fact_at_ms"]),
        "summary": payload["summary"],
        "top_changes": list(payload["summary"]["top_changes"]),
        "coverage_gap_count": sum(item["state"] != "available" for item in coverage["capabilities"]),
        "health_gap_count": max(0, int(health["tracked_datasets"]) - int(health["current_datasets"])),
        "href": _MODULE_HREFS[payload["module_id"]],
    }


def _compact_research_payload(row: dict[str, Any] | None) -> dict[str, Any]:
    artifact = row.get("artifact_json") if row is not None else None
    if isinstance(artifact, dict):
        return {
            "state": "current",
            "session_date": artifact.get("session_date"),
            "evidence_pack_id": row.get("evidence_pack_id"),
            "market_cutoff_ms": artifact.get("market_cutoff_ms"),
            "title": artifact.get("title"),
            "executive_summary": artifact.get("executive_summary"),
            "reviewer_disposition": artifact.get("reviewer_disposition"),
            "href": "/macro/research",
        }
    run_status = str(row.get("run_status") or "") if row is not None else ""
    state = "generating" if run_status in _GENERATING_RUN_STATES else "failed" if run_status == "failed" else "missing"
    return {
        "state": state,
        "session_date": row.get("session_date") if row is not None else None,
        "evidence_pack_id": row.get("evidence_pack_id") if row is not None else None,
        "market_cutoff_ms": row.get("market_cutoff_ms") if row is not None else None,
        "title": None,
        "executive_summary": None,
        "reviewer_disposition": row.get("run_reviewer_disposition") if row is not None else None,
        "href": "/macro/research",
    }


def _research_payload(
    row: dict[str, Any] | None,
    *,
    requested_session: date,
    current_session: date,
) -> dict[str, Any]:
    if row is None:
        return {
            "state": "missing",
            "requested_session_date": requested_session,
            "current_session_date": current_session,
            "publication": None,
            "run": None,
        }
    publication = _publication_payload(row)
    if publication is not None:
        state = "current" if requested_session == current_session else "historical"
    elif str(row.get("run_status") or "") in _GENERATING_RUN_STATES:
        state = "generating"
    elif str(row.get("run_status") or "") == "failed":
        state = "failed"
    else:
        state = "missing"
    return {
        "state": state,
        "requested_session_date": requested_session,
        "current_session_date": current_session,
        "publication": publication,
        "run": _run_payload(row),
    }


def _publication_payload(row: dict[str, Any]) -> dict[str, Any] | None:
    artifact = row.get("artifact_json")
    if not isinstance(artifact, dict):
        return None
    return {
        "schema_version": artifact["schema_version"],
        "session_date": artifact["session_date"],
        "market_cutoff_ms": artifact["market_cutoff_ms"],
        "title": artifact["title"],
        "executive_summary": artifact["executive_summary"],
        "sections": artifact["sections"],
        "evidence_gaps": artifact["gaps"],
        "citations": [
            {
                "citation_id": citation["citation_id"],
                "source_type": citation["source_type"],
                "source_ref": citation["source_ref"],
                "source_label": citation["source_label"],
                "available_at_ms": citation["available_at_ms"],
                "observed_at": citation.get("observed_at"),
                "published_at_ms": citation.get("published_at_ms"),
                "source_url": citation.get("url"),
                "lineage": citation.get("lineage") or {},
            }
            for citation in artifact["citations"]
        ],
        "reviewer_disposition": artifact["reviewer_disposition"],
        "reviewer_notes": artifact["reviewer_notes"],
        "audit": row.get("audit_json") or {},
        "published_at_ms": row.get("published_at_ms"),
        "evidence_pack_id": row["evidence_pack_id"],
    }


def _run_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_date": row["session_date"],
        "evidence_pack_id": row["evidence_pack_id"],
        "status": row["run_status"],
        "attempt_count": int(row["attempt_count"]),
        "max_attempts": int(row["max_attempts"]),
        "last_error": row.get("last_error_message"),
        "updated_at_ms": int(row["updated_at_ms"]),
    }


__all__ = ["router"]
