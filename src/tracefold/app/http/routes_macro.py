from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from tracefold.app.http import schemas as api_schemas
from tracefold.app.http.dependencies import _authenticated_runtime, _now_ms
from tracefold.app.http.exceptions import ApiBadRequest
from tracefold.app.http.macro_modules import MACRO_HTTP_MODULE_BY_ID
from tracefold.app.http.responses import _validated_etag_json, _validated_json
from tracefold.app.runtime_capabilities import macro_document_analysis_runtime
from tracefold.macro import (
    MACRO_MODULE_IDS,
    MACRO_MODULE_LABELS,
    MacroModuleId,
    macro_reason,
    schema_version_for_module,
)

router = APIRouter()
_MACRO_ETAG_HEADERS: dict[str, dict[str, object]] = {
    "Cache-Control": {
        "description": "Requires revalidation before reuse.",
        "schema": {"type": "string"},
    },
    "ETag": {
        "description": "Weak semantic validator shared by identity and gzip representations.",
        "schema": {"type": "string"},
    },
}
_MACRO_ETAG_RESPONSES = {
    200: {"headers": _MACRO_ETAG_HEADERS},
    304: {"description": "Not Modified", "headers": _MACRO_ETAG_HEADERS},
}
_MACRO_ETAG_OPENAPI_EXTRA = {
    "parameters": [
        {
            "in": "header",
            "name": "If-None-Match",
            "required": False,
            "schema": {"title": "If-None-Match", "type": "string"},
        }
    ]
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
    modules = [
        _module_summary(
            module_id,
            row=module_rows.get(module_id),
        )
        for module_id in MACRO_MODULE_IDS
    ]
    payload = {
        "schema_version": "macro_overview_v9",
        "read_at_ms": read_at_ms,
        "transport": {
            "state": "current",
            "last_successful_read_at_ms": read_at_ms,
            "reason": None,
        },
        "latest_fact_at_ms": max(
            (int(module["latest_fact_at_ms"]) for module in modules if module["latest_fact_at_ms"] is not None),
            default=0,
        ),
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
    payload, module_reason = _module_payload(module_id, row)
    if payload is None:
        return {
            "schema_version": "macro_module_unavailable_v1",
            "module_id": module_id,
            "label": MACRO_MODULE_LABELS[module_id],
            "availability": "unavailable",
            "reason": module_reason,
            "href": MACRO_HTTP_MODULE_BY_ID[module_id].href,
        }
    if module_id == "rates_fed":
        payload["document_analysis_runtime"] = macro_document_analysis_runtime(runtime.settings)
    payload["availability"] = "available"
    payload["reason"] = _available_module_reason(payload)
    return payload


@router.get(
    "/macro/rates-fed",
    response_model=api_schemas.ApiEnvelope[api_schemas.MacroRatesFedReadData | api_schemas.MacroModuleUnavailableData],
    responses=_MACRO_ETAG_RESPONSES,
    openapi_extra=_MACRO_ETAG_OPENAPI_EXTRA,
)
def macro_rates_fed(request: Request) -> Response:
    return _module_response(request, "rates_fed", api_schemas.MacroRatesFedReadData)


@router.get(
    "/macro/economy-inflation",
    response_model=api_schemas.ApiEnvelope[
        api_schemas.MacroEconomyInflationReadData | api_schemas.MacroModuleUnavailableData
    ],
    responses=_MACRO_ETAG_RESPONSES,
    openapi_extra=_MACRO_ETAG_OPENAPI_EXTRA,
)
def macro_economy_inflation(request: Request) -> Response:
    return _module_response(
        request,
        "economy_inflation",
        api_schemas.MacroEconomyInflationReadData,
    )


@router.get(
    "/macro/liquidity-funding",
    response_model=api_schemas.ApiEnvelope[
        api_schemas.MacroLiquidityFundingReadData | api_schemas.MacroModuleUnavailableData
    ],
    responses=_MACRO_ETAG_RESPONSES,
    openapi_extra=_MACRO_ETAG_OPENAPI_EXTRA,
)
def macro_liquidity_funding(request: Request) -> Response:
    return _module_response(
        request,
        "liquidity_funding",
        api_schemas.MacroLiquidityFundingReadData,
    )


@router.get(
    "/macro/credit",
    response_model=api_schemas.ApiEnvelope[api_schemas.MacroCreditReadData | api_schemas.MacroModuleUnavailableData],
    responses=_MACRO_ETAG_RESPONSES,
    openapi_extra=_MACRO_ETAG_OPENAPI_EXTRA,
)
def macro_credit(request: Request) -> Response:
    return _module_response(request, "credit", api_schemas.MacroCreditReadData)


@router.get(
    "/macro/volatility",
    response_model=api_schemas.ApiEnvelope[
        api_schemas.MacroVolatilityReadData | api_schemas.MacroModuleUnavailableData
    ],
    responses=_MACRO_ETAG_RESPONSES,
    openapi_extra=_MACRO_ETAG_OPENAPI_EXTRA,
)
def macro_volatility(request: Request) -> Response:
    return _module_response(request, "volatility", api_schemas.MacroVolatilityReadData)


@router.get(
    "/macro/cross-asset",
    response_model=api_schemas.ApiEnvelope[
        api_schemas.MacroCrossAssetReadData | api_schemas.MacroModuleUnavailableData
    ],
    responses=_MACRO_ETAG_RESPONSES,
    openapi_extra=_MACRO_ETAG_OPENAPI_EXTRA,
)
def macro_cross_asset(request: Request) -> Response:
    return _module_response(request, "cross_asset", api_schemas.MacroCrossAssetReadData)


def _module_response(
    request: Request,
    module_id: MacroModuleId,
    read_schema: type[api_schemas.ExactApiSchema],
) -> Response:
    envelope = api_schemas.ApiEnvelope[
        read_schema | api_schemas.MacroModuleUnavailableData  # type: ignore[valid-type]
    ]
    data = _read_module(request, module_id)
    return _validated_etag_json(
        envelope,
        {"ok": True, "data": data},
        data=data,
        request=request,
        weak=True,
    )


def _authenticated_macro_runtime(request: Request) -> Any:
    runtime = _authenticated_runtime(request, allow_query_token=False)
    if "token" in request.query_params:
        raise ApiBadRequest("unsupported_query_param", field="token")
    return runtime


def _reject_query_params(request: Request) -> None:
    for name in request.query_params:
        raise ApiBadRequest("unsupported_query_param", field=name)


def _module_payload(
    module_id: MacroModuleId,
    row: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, object] | None]:
    expected_schema = schema_version_for_module(module_id)
    if row is None:
        return None, macro_reason(
            code="macro_module_not_materialized",
            message=f"{MACRO_MODULE_LABELS[module_id]}尚无持久化 read model。",
            impact="blocked",
            retryable=True,
            recovery="operator_action",
            next_action="运行 macro_projection 并重新读取该模块。",
        )
    payload = row.get("payload_json")
    if isinstance(payload, dict) and payload.get("schema_version") == expected_schema:
        try:
            validated = MACRO_HTTP_MODULE_BY_ID[module_id].persisted_schema.model_validate(payload)
        except ValidationError:
            pass
        else:
            return validated.model_dump(mode="json", by_alias=True, exclude_unset=True), None
    return None, macro_reason(
        code="macro_module_schema_mismatch",
        message=f"{MACRO_MODULE_LABELS[module_id]}的持久化 schema 与当前合同不一致。",
        impact="blocked",
        retryable=False,
        recovery="operator_action",
        next_action=f"使用 {expected_schema} 重新投影该模块。",
    )


def _module_summary(
    module_id: MacroModuleId,
    *,
    row: dict[str, Any] | None,
) -> dict[str, Any]:
    payload, reason = _module_payload(module_id, row)
    if payload is None:
        return {
            "module_id": module_id,
            "label": MACRO_MODULE_LABELS[module_id],
            "availability": "unavailable",
            "reason": reason,
            "coverage_state": None,
            "current_health_state": None,
            "history_depth_state": None,
            "backfill_execution": None,
            "latest_fact_at_ms": None,
            "summary": None,
            "coverage_gap_count": 1,
            "current_health_gap_count": 1,
            "history_gap_count": 0,
            "href": MACRO_HTTP_MODULE_BY_ID[module_id].href,
        }
    status = dict(payload["status"])
    coverage = dict(status["coverage"])
    current_health = dict(status["current_health"])
    history_depth = dict(status["history_depth"])
    if module_id == "rates_fed":
        decision = dict(payload["decision"])
        assessments = dict(decision["explanation"]).get("bounded_assessments", ())
        summary = {
            "headline": decision.get("headline"),
            "interpretation": (
                str(assessments[0]["statement"])
                if assessments and isinstance(assessments[0], dict) and assessments[0].get("statement")
                else None
            ),
        }
    else:
        module_summary = dict(payload["summary"])
        summary = {
            "headline": module_summary.get("headline"),
            "interpretation": module_summary.get("interpretation"),
        }
    return {
        "module_id": payload["module_id"],
        "label": payload["label"],
        "availability": "available",
        "reason": _available_module_reason(payload),
        "coverage_state": coverage["state"],
        "current_health_state": current_health["state"],
        "history_depth_state": history_depth["state"],
        "backfill_execution": status["backfill_execution"],
        "latest_fact_at_ms": int(payload["latest_fact_at_ms"]),
        "summary": summary,
        "coverage_gap_count": sum(item["state"] != "available" for item in coverage["capabilities"]),
        "current_health_gap_count": max(
            0,
            int(current_health["tracked_datasets"]) - int(current_health["current_datasets"]),
        ),
        "history_gap_count": max(
            0,
            int(history_depth["tracked_datasets"]) - int(history_depth["complete_datasets"]),
        ),
        "href": MACRO_HTTP_MODULE_BY_ID[module_id].href,
    }


def _available_module_reason(payload: dict[str, Any]) -> dict[str, object] | None:
    status = dict(payload["status"])
    current = dict(status["current_health"])
    history = dict(status["history_depth"])
    states = [item for item in dict(payload["evidence"]).get("dataset_states", ()) if isinstance(item, dict)]
    if current["state"] != "current":
        blocking_states = [
            item
            for item in states
            if item.get("required_for_current") is True and item.get("current_health") != "current"
        ]
        affected = tuple(str(item["dataset_id"]) for item in blocking_states if item.get("dataset_id"))
        terminal_stale = any(
            isinstance(item.get("current_reason"), dict)
            and item["current_reason"].get("code") == "source_terminal_stale"
            for item in blocking_states
        )
        operator_action = any(
            isinstance(item.get("current_reason"), dict) and item["current_reason"].get("recovery") == "operator_action"
            for item in blocking_states
        )
        next_check_at_ms = min(
            (
                int(reason["next_check_at_ms"])
                for item in blocking_states
                if isinstance((reason := item.get("current_reason")), dict)
                and reason.get("next_check_at_ms") is not None
            ),
            default=None,
        )
        return macro_reason(
            code=f"macro_module_current_health_{current['state']}",
            message="当前判断所需的 canonical/required 事实不完整。",
            impact="blocked" if current["state"] == "unavailable" else "limited",
            affected_dataset_ids=affected,
            retryable=not operator_action,
            recovery="operator_action" if operator_action else "automatic",
            next_action=(
                "检查 stale 数据源并由操作员恢复；页面不会自动重试。"
                if terminal_stale
                else "创建或恢复 required source acquisition target 后重新投影。"
                if operator_action
                else "等待采集 worker 完成下一次受限追赶。"
            ),
            next_check_at_ms=None if operator_action else next_check_at_ms,
        )
    if history["state"] in {"partial", "insufficient"}:
        backfill = dict(status["backfill_execution"])
        running = backfill["state"] == "running"
        return macro_reason(
            code=f"macro_module_history_{history['state']}",
            message="当前事实可用，但 required 历史深度仍不足。",
            impact="limited",
            retryable=True,
            recovery="automatic" if running else "operator_action",
            next_action=(
                "等待正在执行的显式历史回填；optional 最大历史不阻断当前判断。"
                if running
                else "检查缺失数据集的 history target，并执行 tracefold macro backfill；当前事实仍可读取。"
            ),
            next_check_at_ms=(
                int(backfill["next_check_at_ms"]) if backfill.get("next_check_at_ms") is not None else None
            ),
        )
    return None


def _data_quality_overview(modules: list[dict[str, Any]]) -> dict[str, Any]:
    coverage_states = [
        str(module["coverage_state"]) if module["coverage_state"] is not None else "partial" for module in modules
    ]
    current_states = [
        str(module["current_health_state"]) if module["current_health_state"] is not None else "unavailable"
        for module in modules
    ]
    history_states = [
        str(module["history_depth_state"]) if module["history_depth_state"] is not None else "insufficient"
        for module in modules
        if module["history_depth_state"] != "not_required"
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
