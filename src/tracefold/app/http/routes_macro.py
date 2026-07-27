from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from tracefold.app.http import schemas as api_schemas
from tracefold.app.http.dependencies import _authenticated_runtime, _now_ms
from tracefold.app.http.exceptions import ApiBadRequest
from tracefold.app.http.responses import _validated_json
from tracefold.macro import MACRO_MODULE_IDS, MACRO_MODULE_LABELS, resolve_completed_session

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
    with runtime.repositories() as repos:
        module_rows = {row["module_id"]: row for row in repos.macro.all_modules_current()}
        judgment_row = repos.macro.daily_judgment()
        research_row = repos.macro_research.research_state()
    modules = [
        _module_summary(module_id, module_rows.get(module_id))
        for module_id in MACRO_MODULE_IDS
    ]
    readiness_values = {module["readiness"] for module in modules}
    if "blocked" in readiness_values or "missing" in readiness_values:
        overall_readiness = "blocked"
    elif "degraded" in readiness_values:
        overall_readiness = "degraded"
    else:
        overall_readiness = "ready"
    judgment = (
        dict(judgment_row["judgment_json"])
        if judgment_row is not None and isinstance(judgment_row.get("judgment_json"), dict)
        else None
    )
    payload = {
        "schema_version": "macro_overview_v1",
        "read_at_ms": read_at_ms,
        "judgment_cutoff_ms": (
            int(judgment_row["judgment_cutoff_ms"]) if judgment_row is not None else None
        ),
        "latest_fact_at_ms": max(
            (int(module["latest_fact_at_ms"]) for module in modules),
            default=0,
        ),
        "overall_readiness": overall_readiness,
        "daily_judgment": judgment,
        "modules": modules,
        "changes_since_judgment": [
            {"module_id": module["module_id"], "changes": module["top_changes"][:3]}
            for module in modules
        ],
        "research": _compact_research_payload(research_row),
    }
    return _validated_json(
        api_schemas.ApiEnvelope[api_schemas.MacroOverviewReadData],
        {"ok": True, "data": payload},
    )


def _module_route(path: str, module_id: str) -> Any:
    async def endpoint(request: Request) -> JSONResponse:
        runtime = _authenticated_macro_runtime(request)
        _reject_query_params(request)
        with runtime.repositories() as repos:
            row = repos.macro.module_current(module_id)
            judgment = repos.macro.daily_judgment()
        payload = _module_payload(module_id, row, judgment)
        return _validated_json(
            api_schemas.ApiEnvelope[api_schemas.MacroModuleReadData],
            {"ok": True, "data": payload},
        )

    endpoint.__name__ = f"macro_{module_id}"
    router.add_api_route(
        path,
        endpoint,
        methods=["GET"],
        response_model=api_schemas.ApiEnvelope[api_schemas.MacroModuleReadData],
    )
    return endpoint


macro_rates_fed = _module_route("/macro/rates-fed", "rates_fed")
macro_economy_inflation = _module_route(
    "/macro/economy-inflation",
    "economy_inflation",
)
macro_liquidity_funding = _module_route(
    "/macro/liquidity-funding",
    "liquidity_funding",
)
macro_credit = _module_route("/macro/credit", "credit")
macro_volatility = _module_route("/macro/volatility", "volatility")
macro_cross_asset = _module_route("/macro/cross-asset", "cross_asset")


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
    module_id: str,
    row: dict[str, Any] | None,
    judgment: dict[str, Any] | None,
) -> dict[str, Any]:
    if row is not None and isinstance(row.get("payload_json"), dict):
        payload = dict(row["payload_json"])
    else:
        payload = {
            "schema_version": "macro_module_v1",
            "module_id": module_id,
            "label": MACRO_MODULE_LABELS[module_id],
            "readiness": "blocked",
            "judgment_cutoff_ms": None,
            "latest_fact_at_ms": 0,
            "current_state": {
                "headline": "数据链正在初始化",
                "dominant_change": None,
                "feature_count": 0,
                "interpretation": "等待新事实链完成首次采集与投影。",
            },
            "top_changes": [],
            "features": [],
            "charts": [],
            "contradictions": [],
            "falsifiers": [],
            "next_checkpoints": [],
            "gaps": [
                {
                    "dataset_id": "module",
                    "label": "模块投影",
                    "state": "backfilling",
                    "reason": "module_projection_missing",
                    "decision_impact": "blocks_new_judgment",
                }
            ],
            "dataset_states": [],
            "raw_evidence": [],
        }
    if judgment is not None:
        payload["judgment_cutoff_ms"] = int(judgment["judgment_cutoff_ms"])
    return payload


def _module_summary(module_id: str, row: dict[str, Any] | None) -> dict[str, Any]:
    payload = (
        dict(row["payload_json"])
        if row is not None and isinstance(row.get("payload_json"), dict)
        else None
    )
    return {
        "module_id": module_id,
        "label": MACRO_MODULE_LABELS[module_id],
        "readiness": str(payload["readiness"]) if payload else "missing",
        "latest_fact_at_ms": int(payload["latest_fact_at_ms"]) if payload else 0,
        "current_state": payload.get("current_state") if payload else None,
        "top_changes": list(payload.get("top_changes") or ()) if payload else [],
        "gap_count": len(payload.get("gaps") or ()) if payload else 1,
        "href": _MODULE_HREFS[module_id],
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
    state = (
        "generating"
        if run_status in _GENERATING_RUN_STATES
        else "failed"
        if run_status == "failed"
        else "missing"
    )
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
