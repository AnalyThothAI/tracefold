from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from tracefold.app.http import schemas as api_schemas
from tracefold.app.http.dependencies import _authenticated_runtime, _now_ms
from tracefold.app.http.exceptions import ApiBadRequest
from tracefold.app.http.responses import _validated_json
from tracefold.macro import (
    DATASET_REGISTRY,
    MACRO_MODULE_IDS,
    MACRO_MODULE_LABELS,
    CurrentThesisState,
    MacroLiveDeltaV2,
    MacroModuleId,
    MacroOutcomeReplayV2,
    MacroThesisV1,
    MacroThesisV2,
    classify_current_thesis_state,
    macro_reason,
    parse_current_thesis_v2,
    project_current_recovery,
    resolve_thesis_session,
    schema_version_for_module,
    thesis_cutoff_ms,
)

router = APIRouter()

_MODULE_HREFS = {
    "rates_fed": "/macro/rates-fed",
    "economy_inflation": "/macro/economy-inflation",
    "liquidity_funding": "/macro/liquidity-funding",
    "credit": "/macro/credit",
    "volatility": "/macro/volatility",
    "cross_asset": "/macro/cross-asset",
}
_MODULE_PERSISTED_SCHEMAS: dict[
    MacroModuleId,
    type[api_schemas.ExactApiSchema],
] = {
    "rates_fed": api_schemas.MacroRatesFedPersistedData,
    "economy_inflation": api_schemas.MacroEconomyInflationPersistedData,
    "liquidity_funding": api_schemas.MacroLiquidityFundingPersistedData,
    "credit": api_schemas.MacroCreditPersistedData,
    "volatility": api_schemas.MacroVolatilityPersistedData,
    "cross_asset": api_schemas.MacroCrossAssetPersistedData,
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
    cutoff_ms = thesis_cutoff_ms(session_date)
    with runtime.repositories() as repos:
        module_rows = {row["module_id"]: row for row in repos.macro.all_modules_current()}
        run = repos.macro_thesis.state(session_date)
        thesis = parse_current_thesis_v2(run)
        live_delta, outcome_replay = _follow_up_payloads(repos, thesis)
        history_modules = [
            dict(row["payload_json"]) for row in module_rows.values() if isinstance(row.get("payload_json"), dict)
        ]
    state = classify_current_thesis_state(run, thesis)
    reason = _current_reason(
        run,
        state=state,
        session_date=session_date,
        cutoff_ms=cutoff_ms,
        read_at_ms=read_at_ms,
    )
    recovery = (
        [
            item.model_dump(mode="json")
            for item in project_current_recovery(
                publication=thesis,
                modules=history_modules,
            )
        ]
        if thesis is not None
        else []
    )
    context_by_module = {
        module_id: _module_thesis_context(
            module_id,
            session_date=session_date,
            cutoff_ms=cutoff_ms,
            state=state,
            thesis=thesis,
            reason=reason,
            recovery=recovery,
        )
        for module_id in MACRO_MODULE_IDS
    }
    modules = [
        _module_summary(
            module_id,
            row=module_rows.get(module_id),
            context=context_by_module[module_id],
        )
        for module_id in MACRO_MODULE_IDS
    ]
    payload = {
        "schema_version": "macro_overview_v8",
        "read_at_ms": read_at_ms,
        "transport": {
            "state": "current",
            "last_successful_read_at_ms": read_at_ms,
            "reason": None,
        },
        "session_date": session_date,
        "cutoff_ms": cutoff_ms,
        "latest_fact_at_ms": max(
            (int(module["latest_fact_at_ms"]) for module in modules if module["latest_fact_at_ms"] is not None),
            default=0,
        ),
        "thesis_state": state,
        "thesis_reason": reason,
        "thesis": thesis.model_dump(mode="json") if thesis is not None else None,
        "run": _run_payload(run, public_state=state, reason=reason),
        "live_delta": live_delta,
        "outcome_replay": outcome_replay,
        "recovery": recovery,
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
    read_at_ms = _now_ms()
    session_date = resolve_thesis_session(now_ms=read_at_ms)
    cutoff_ms = thesis_cutoff_ms(session_date)
    with runtime.repositories() as repos:
        row = repos.macro.module_current(module_id)
        run = repos.macro_thesis.state(session_date)
        thesis = parse_current_thesis_v2(run)
        module_rows = [
            dict(item["payload_json"])
            for item in repos.macro.all_modules_current()
            if isinstance(item.get("payload_json"), dict)
        ]
    state = classify_current_thesis_state(run, thesis)
    reason = _current_reason(
        run,
        state=state,
        session_date=session_date,
        cutoff_ms=cutoff_ms,
        read_at_ms=read_at_ms,
    )
    recovery = (
        [item.model_dump(mode="json") for item in project_current_recovery(publication=thesis, modules=module_rows)]
        if thesis is not None
        else []
    )
    context = _module_thesis_context(
        module_id,
        session_date=session_date,
        cutoff_ms=cutoff_ms,
        state=state,
        thesis=thesis,
        reason=reason,
        recovery=recovery,
    )
    payload, module_reason = _module_payload(module_id, row)
    if payload is None:
        return {
            "schema_version": "macro_module_unavailable_v1",
            "module_id": module_id,
            "label": MACRO_MODULE_LABELS[module_id],
            "availability": "unavailable",
            "reason": module_reason,
            "href": _MODULE_HREFS[module_id],
            "thesis_context": context,
        }
    payload["availability"] = "available"
    payload["reason"] = _available_module_reason(payload)
    payload["thesis_context"] = context
    return payload


@router.get(
    "/macro/rates-fed",
    response_model=api_schemas.ApiEnvelope[api_schemas.MacroRatesFedReadData | api_schemas.MacroModuleUnavailableData],
)
def macro_rates_fed(request: Request) -> JSONResponse:
    return _module_response(request, "rates_fed", api_schemas.MacroRatesFedReadData)


@router.get(
    "/macro/economy-inflation",
    response_model=api_schemas.ApiEnvelope[
        api_schemas.MacroEconomyInflationReadData | api_schemas.MacroModuleUnavailableData
    ],
)
def macro_economy_inflation(request: Request) -> JSONResponse:
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
)
def macro_liquidity_funding(request: Request) -> JSONResponse:
    return _module_response(
        request,
        "liquidity_funding",
        api_schemas.MacroLiquidityFundingReadData,
    )


@router.get(
    "/macro/credit",
    response_model=api_schemas.ApiEnvelope[api_schemas.MacroCreditReadData | api_schemas.MacroModuleUnavailableData],
)
def macro_credit(request: Request) -> JSONResponse:
    return _module_response(request, "credit", api_schemas.MacroCreditReadData)


@router.get(
    "/macro/volatility",
    response_model=api_schemas.ApiEnvelope[
        api_schemas.MacroVolatilityReadData | api_schemas.MacroModuleUnavailableData
    ],
)
def macro_volatility(request: Request) -> JSONResponse:
    return _module_response(request, "volatility", api_schemas.MacroVolatilityReadData)


@router.get(
    "/macro/cross-asset",
    response_model=api_schemas.ApiEnvelope[
        api_schemas.MacroCrossAssetReadData | api_schemas.MacroModuleUnavailableData
    ],
)
def macro_cross_asset(request: Request) -> JSONResponse:
    return _module_response(request, "cross_asset", api_schemas.MacroCrossAssetReadData)


def _module_response(
    request: Request,
    module_id: MacroModuleId,
    read_schema: type[api_schemas.ExactApiSchema],
) -> JSONResponse:
    envelope = api_schemas.ApiEnvelope[
        read_schema | api_schemas.MacroModuleUnavailableData  # type: ignore[valid-type]
    ]
    return _validated_json(
        envelope,
        {"ok": True, "data": _read_module(request, module_id)},
    )


@router.get(
    "/macro/research",
    response_model=api_schemas.ApiEnvelope[
        api_schemas.MacroThesisDetailReadData | api_schemas.MacroThesisArchiveDetailReadData
    ],
)
def macro_research(
    request: Request,
    session_date: Annotated[date | None, Query()] = None,
) -> JSONResponse:
    runtime = _authenticated_macro_runtime(request)
    _validate_research_query_params(request)
    read_at_ms = _now_ms()
    current_session = resolve_thesis_session(now_ms=read_at_ms)
    payload: dict[str, Any]
    with runtime.repositories() as repos:
        history = [_history_payload(item) for item in repos.macro_thesis.publications(limit=30)]
        modules = [
            dict(row["payload_json"])
            for row in repos.macro.all_modules_current()
            if isinstance(row.get("payload_json"), dict)
        ]
        if session_date is not None:
            publication_row = repos.macro_thesis.archive_publication(session_date)
            run = repos.macro_thesis.state(session_date)
            thesis = _archive_thesis(publication_row)
            recovery = (
                [
                    item.model_dump(mode="json")
                    for item in project_current_recovery(
                        publication=thesis,
                        modules=modules,
                    )
                ]
                if thesis is not None
                else []
            )
            payload = {
                "schema_version": "macro_thesis_archive_detail_v2",
                "state": "historical" if thesis is not None else "missing",
                "requested_session_date": session_date,
                "current_session_date": current_session,
                "reason": (
                    None
                    if thesis is not None
                    else macro_reason(
                        code="macro_archive_publication_missing",
                        message="该交易日没有不可变 Macro Thesis publication。",
                        impact="blocked",
                        retryable=False,
                        recovery="none",
                        next_action="选择历史列表中已发布的交易日。",
                    )
                ),
                "thesis": thesis.model_dump(mode="json") if thesis is not None else None,
                "recovery": recovery,
                "run": _run_payload(
                    run,
                    public_state=("published" if thesis is not None else classify_current_thesis_state(run)),
                    reason=None,
                ),
                "history": history,
            }
        else:
            run = repos.macro_thesis.state(current_session)
            thesis = parse_current_thesis_v2(run)
            live_delta, outcome_replay = _follow_up_payloads(repos, thesis)
            state = classify_current_thesis_state(run, thesis)
            cutoff_ms = thesis_cutoff_ms(current_session)
            reason = _current_reason(
                run,
                state=state,
                session_date=current_session,
                cutoff_ms=cutoff_ms,
                read_at_ms=read_at_ms,
            )
            recovery = (
                [
                    item.model_dump(mode="json")
                    for item in project_current_recovery(
                        publication=thesis,
                        modules=modules,
                    )
                ]
                if thesis is not None
                else []
            )
            payload = {
                "schema_version": "macro_thesis_detail_v4",
                "state": state,
                "session_date": current_session,
                "cutoff_ms": cutoff_ms,
                "reason": reason,
                "thesis": thesis.model_dump(mode="json") if thesis is not None else None,
                "live_delta": live_delta,
                "outcome_replay": outcome_replay,
                "recovery": recovery,
                "run": _run_payload(run, public_state=state, reason=reason),
                "history": history,
            }
    response_type = api_schemas.ApiEnvelope[
        api_schemas.MacroThesisDetailReadData | api_schemas.MacroThesisArchiveDetailReadData
    ]
    return _validated_json(response_type, {"ok": True, "data": payload})


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
            validated = _MODULE_PERSISTED_SCHEMAS[module_id].model_validate(payload)
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
    context: dict[str, Any],
) -> dict[str, Any]:
    payload, reason = _module_payload(module_id, row)
    if payload is None:
        return {
            "module_id": module_id,
            "label": MACRO_MODULE_LABELS[module_id],
            "availability": "unavailable",
            "reason": reason,
            "role": context["role"],
            "coverage_state": None,
            "current_health_state": None,
            "history_depth_state": None,
            "backfill_execution": None,
            "latest_fact_at_ms": None,
            "summary": None,
            "coverage_gap_count": 1,
            "current_health_gap_count": 1,
            "history_gap_count": 0,
            "href": _MODULE_HREFS[module_id],
            "thesis_context": context,
        }
    status = dict(payload["status"])
    coverage = dict(status["coverage"])
    current_health = dict(status["current_health"])
    history_depth = dict(status["history_depth"])
    return {
        "module_id": payload["module_id"],
        "label": payload["label"],
        "availability": "available",
        "reason": _available_module_reason(payload),
        "role": context["role"],
        "coverage_state": coverage["state"],
        "current_health_state": current_health["state"],
        "history_depth_state": history_depth["state"],
        "backfill_execution": status["backfill_execution"],
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
        "href": _MODULE_HREFS[module_id],
        "thesis_context": context,
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
        return macro_reason(
            code=f"macro_module_history_{history['state']}",
            message="当前事实可用，但 required 历史深度仍不足。",
            impact="limited",
            retryable=True,
            recovery="automatic",
            next_action="等待 required 历史回填；optional 最大历史不阻断当前判断。",
        )
    return None


def _module_thesis_context(
    module_id: MacroModuleId,
    *,
    session_date: date,
    cutoff_ms: int,
    state: CurrentThesisState,
    thesis: MacroThesisV2 | None,
    reason: dict[str, object] | None,
    recovery: list[dict[str, Any]],
) -> dict[str, Any]:
    assessment = (
        next(
            (item for item in thesis.module_assessments if item.module_id == module_id),
            None,
        )
        if thesis is not None
        else None
    )
    conditions = (
        [item.model_dump(mode="json") for item in thesis.conditions if item.module_id == module_id]
        if thesis is not None
        else []
    )
    module_recovery = [
        item
        for item in recovery
        if (
            item.get("publication", {}).get("dataset_id") in DATASET_REGISTRY
            and DATASET_REGISTRY[str(item["publication"]["dataset_id"])].module_id == module_id
        )
    ]
    return {
        "state": state,
        "session_date": session_date,
        "cutoff_ms": cutoff_ms,
        "role": assessment.role if assessment is not None else "not_material",
        "assessment": (assessment.model_dump(mode="json") if assessment is not None else None),
        "conditions": conditions,
        "recovery": module_recovery,
        "reason": reason if thesis is None else None,
    }


def _archive_thesis(
    row: dict[str, Any] | None,
) -> MacroThesisV1 | MacroThesisV2 | None:
    if row is None or not isinstance(row.get("thesis_json"), dict):
        return None
    schema_version = str(row.get("schema_version") or "")
    model: type[MacroThesisV1] | type[MacroThesisV2]
    if schema_version == "macro_thesis_v1":
        model = MacroThesisV1
    elif schema_version == "macro_thesis_v2":
        model = MacroThesisV2
    else:
        return None
    try:
        return model.model_validate(row["thesis_json"])
    except ValidationError:
        return None


def _current_reason(
    row: dict[str, Any] | None,
    *,
    state: CurrentThesisState,
    session_date: date,
    cutoff_ms: int,
    read_at_ms: int,
) -> dict[str, object] | None:
    if state == "published":
        return None
    if row is None:
        before_cutoff = read_at_ms < cutoff_ms
        return macro_reason(
            code="macro_thesis_run_missing",
            message=(
                "当前交易日尚未到 08:50 ET publication cutoff。"
                if before_cutoff
                else "当前交易日没有持久化 Thesis run。"
            ),
            impact="blocked",
            retryable=True,
            recovery="automatic" if before_cutoff else "operator_action",
            next_action=(
                "等待 cutoff 后由 macro_thesis worker 创建运行。"
                if before_cutoff
                else "检查 macro_thesis worker，并显式恢复当前 session。"
            ),
            next_check_at_ms=cutoff_ms if before_cutoff else None,
        )
    if str(row.get("status") or "") == "published":
        return macro_reason(
            code="macro_thesis_current_contract_not_published",
            message=(
                f"{session_date.isoformat()} 只有历史 v1 publication；当前页面只接受同 session 的 macro_thesis_v2。"
            ),
            impact="blocked",
            retryable=False,
            recovery="next_session",
            next_action="保留 v1 档案，只在显式历史读取中展示；当前页不降级。",
        )
    if state in {"pending", "running", "retryable"}:
        return macro_reason(
            code=f"macro_thesis_{state}",
            message={
                "pending": "ResearchInput 已冻结，等待 Thin Agent 领取。",
                "running": "Thin Agent 正在执行本次唯一模型调用。",
                "retryable": "模型 provider 暂时失败，等待受限重试。",
            }[state],
            impact="limited",
            retryable=True,
            recovery="automatic",
            next_action="页面只读取持久化状态，不启动 Agent。",
            next_check_at_ms=(
                int(row["leased_until_ms"])
                if state == "running" and row.get("leased_until_ms") is not None
                else int(row["due_at_ms"])
                if row.get("due_at_ms") is not None
                else None
            ),
        )
    error_code = str(row.get("last_error_code") or f"macro_thesis_{state}")
    gate = str(row.get("last_gate_category") or "")
    configuration = state == "config_error"
    return macro_reason(
        code=error_code,
        message=(
            "Thin Agent 配置或鉴权失败，候选生成未启动。"
            if configuration
            else f"本 session 未发布；失败边界为 {gate or 'pre-draft run'}。"
        ),
        impact="blocked",
        retryable=False,
        recovery="operator_action" if configuration or gate == "write_safety" else "next_session",
        next_action=(
            "修复 provider/model 配置后显式恢复。"
            if configuration
            else "检查写入事务与身份冲突后显式恢复。"
            if gate == "write_safety"
            else "保留 gate 诊断，下一交易日使用新的冻结输入。"
        ),
    )


def _follow_up_payloads(
    repos: Any,
    thesis: MacroThesisV2 | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if thesis is None:
        return None, None
    live_row = repos.macro_thesis.latest_live_delta(thesis.publication_id)
    outcome_row = repos.macro_thesis.latest_outcome_replay(thesis.publication_id)
    live = (
        MacroLiveDeltaV2.model_validate(live_row["payload_json"]).model_dump(mode="json")
        if live_row is not None and isinstance(live_row.get("payload_json"), dict)
        else None
    )
    outcome = (
        MacroOutcomeReplayV2.model_validate(outcome_row["payload_json"]).model_dump(mode="json")
        if outcome_row is not None and isinstance(outcome_row.get("payload_json"), dict)
        else None
    )
    return live, outcome


def _run_payload(
    row: dict[str, Any] | None,
    *,
    public_state: CurrentThesisState,
    reason: dict[str, object] | None,
) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "session_date": row["session_date"],
        "status": public_state,
        "evidence_pack_id": str(row["evidence_pack_id"]),
        "research_input_id": (str(row["research_input_id"]) if row.get("research_input_id") else None),
        "attempt_count": int(row["attempt_count"]),
        "max_attempts": int(row["max_attempts"]),
        "error_code": row.get("last_error_code"),
        "gate_category": row.get("last_gate_category"),
        "candidate_hash": row.get("last_candidate_hash"),
        "reason": reason,
        "updated_at_ms": int(row["updated_at_ms"]),
    }


def _history_payload(row: dict[str, Any]) -> dict[str, Any]:
    thesis = _archive_thesis(row)
    if thesis is None:
        raise ValueError("macro_publication_history_payload_invalid")
    return {
        "schema_version": "macro_publication_history_item_v2",
        "publication_schema_version": thesis.schema_version,
        "publication_id": thesis.publication_id,
        "session_date": thesis.session_date,
        "cutoff_ms": thesis.cutoff_ms,
        "published_at_ms": thesis.published_at_ms,
        "title": thesis.mainline.title,
        "stance": thesis.mainline.stance,
        "confidence": thesis.mainline.confidence,
        "horizon": thesis.mainline.horizon,
    }


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
